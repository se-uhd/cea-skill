"""What the scripts say about a wide set of records, in one comparable file.

Every regression this codebase has shipped passed the whole test suite and changed no verdict on
the 30 real records. Both signals are blind to it, because a test asks about the case it was
written for and the corpus holds only the shapes its authors happened to write. The signal that
was missing is a differential one: run everything over a large, deliberately varied set of
records, write down every answer, and after a change look at what moved.

    python3 scripts/behavior.py --out before.json     # on the tree as it stands
    ... make a change ...
    python3 scripts/behavior.py --diff before.json    # every answer that moved, and why

A move is not a failure. It is the question to answer: did I mean to change this? An unintended
move is a regression, found in seconds rather than in the next review round.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cea_claims as C  # noqa: E402
import cea_page as P  # noqa: E402

WORKSPACE = Path(__file__).resolve().parent.parent / "skills" / "cea-extract-claims-workspace"


def seeds():
    """Every real record, as the starting point for the cases."""
    for path in sorted(WORKSPACE.rglob("claims.json")):
        record = path.parent
        if not (record / "text.txt").is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and "paper" in data:
            yield str(record.relative_to(WORKSPACE)), record, data


# Taken from the module under test, not written out here: this harness named the record's three
# entry lists itself, and a field rename left it looking for fields no record had. It went on
# reporting success, with 250 cases where there had been 466.
ENTRY_KEYS = tuple(C._ID_PREFIX)
RESULTS_KEY = next(k for k, letter in C._ID_PREFIX.items() if letter == "R")


def _entries(data):
    for key in ENTRY_KEYS:
        for i, e in enumerate(data.get(key, [])):
            if isinstance(e, dict):
                yield key, i, e


def variants(data):
    """The record as it stands, and a set of one-field edits across the whole field space.

    Each edit is a shape a record could really take, not a random mutation: the point is to ask
    the scripts a wide range of questions, not to fuzz them.
    """
    yield "as recorded", data
    edits = [
        ("title dropped", lambda d: d["paper"].update(title="A Title The Paper Does Not Print")),
        ("format dropped", lambda d: d.pop("format", None)),
        ("pdf renamed", lambda d: d["paper"].update(pdf="somewhere-else.pdf")),
    ]
    for name, edit in edits:
        one = copy.deepcopy(data)
        try:
            edit(one)
        except Exception:  # a record without that field
            continue
        yield name, one

    # One edit per field that reaches the page, on the first entry of each kind.
    seen = set()
    for key, i, e in _entries(data):
        if key in seen:
            continue
        seen.add(key)
        for field in ("states", "section", "source", "note", "reason", "selection_reason"):
            if field not in e:
                continue
            one = copy.deepcopy(data)
            target = one[key][i]
            if field == "section":
                target[field] = "IX Some Other Section"
            elif field == "source":
                target[field] = "rq_answer" if target[field] != "rq_answer" else "abstract"
            elif field == "states":
                target[field] = " ".join(reversed(str(target[field]).split()))
            else:
                target[field] = "Changed."
            yield f"{key}[{i}].{field}", one
        # The id is built from the prefix map rather than written out: `B1` was a well-formed
        # main-result id before format 2 and is a malformed one after, so these cases went from
        # asking what the validator does with a reference to asking what it does with a bad shape.
        a_result_id = f"{C._ID_PREFIX[RESULTS_KEY]}1"
        for field, value in (("duplicate_of", [a_result_id]), ("breaks_down", [a_result_id]),
                             ("split_from", "S9")):
            if key == RESULTS_KEY:
                continue
            one = copy.deepcopy(data)
            one[key][i][field] = value
            yield f"{key}[{i}].{field} added", one


def answers(record: Path, data: dict) -> dict:
    """Everything the scripts say about one record."""
    out: dict[str, object] = {}
    with tempfile.TemporaryDirectory() as tmp:
        here = Path(tmp)
        (here / "text.txt").write_bytes((record / "text.txt").read_bytes())
        (here / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        problems, parsed = C.validate(here)
        out["problems"] = sorted(problems)
        if problems or parsed is None:
            return out
        out["warnings"] = sorted(C.advisories(here, parsed))
        out["unsettled"] = sorted(C.unsettled(parsed))
        try:
            out["claims_md"] = C.render(parsed)
        except Exception as e:  # a crash is an answer too
            out["claims_md"] = f"<raised {type(e).__name__}: {e}>"
        try:
            page = P.build(parsed, here / "claims.json", here / "index.html")
        except Exception as e:
            out["page"] = f"<raised {type(e).__name__}: {e}>"
        else:
            # The page's own assertions, not its markup: the labels and counts it applies.
            out["page"] = sorted(set(re.findall(
                r"<h3>([^<]{0,80})</h3>|class=\"taglabel\">([^<]{0,40})<|"
                r"stat-value\">(\d+)</div><div class=\"stat-label\">([^<]{0,60})<|"
                r"stated in ([^<·]{0,40})", page)))
    return out


def collect() -> tuple[dict, dict]:
    """The answers, and how many of the corpus's entries the harness reached.

    The per-key counts are returned because a key no record holds is the shape of this harness
    having come apart from the record. Counting entries instead is not enough: `claims` kept its
    name through the format-2 rename, so two wrong keys out of three still found entries and the
    run reported success over 250 cases where there had been 466.
    """
    found, holding = {}, {k: 0 for k in ENTRY_KEYS}
    for name, record, data in seeds():
        for k in ENTRY_KEYS:
            holding[k] += 1 if isinstance(data.get(k), list) else 0
        for label, one in variants(data):
            found[f"{name} :: {label}"] = answers(record, one)
    return found, holding


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write the answers here")
    ap.add_argument("--diff", help="compare the answers with a file written earlier")
    args = ap.parse_args(argv)

    with redirect_stdout(io.StringIO()):
        now, holding = collect()

    # The diff runs before the write, and neither returns early: passing both used to write the
    # snapshot and silently skip the comparison, so a run that looked like a check was not one.
    absent = [k for k, n in holding.items() if not n]
    if absent:
        print(f"CEA_FAILED: no record holds {', '.join(absent)}, so every case over those entries "
              f"is missing and {len(now)} case(s) remain. The harness and the record have come "
              f"apart.")
        return 1

    if args.diff:
        was = json.loads(Path(args.diff).read_text(encoding="utf-8"))
        # Through JSON first: the baseline is read back as lists and the fresh answers hold
        # tuples, so comparing them directly reported every tuple as a change. 147 of 466 cases
        # "moved" on a tree where nothing had.
        now = json.loads(json.dumps(now))
        moved = 0
        for case in sorted(set(was) | set(now)):
            before, after = was.get(case), now.get(case)
            if before == after:
                continue
            moved += 1
            print(f"\n=== {case}")
            for field in sorted(set(before or {}) | set(after or {})):
                a, b = (before or {}).get(field), (after or {}).get(field)
                if a == b:
                    continue
                if isinstance(a, list) and isinstance(b, list):
                    for gone in sorted(set(map(str, a)) - set(map(str, b))):
                        print(f"  - {field}: {gone[:150]}")
                    for came in sorted(set(map(str, b)) - set(map(str, a))):
                        print(f"  + {field}: {came[:150]}")
                else:
                    print(f"  ~ {field} changed ({len(str(a))} -> {len(str(b))} chars)")
        print(f"\nCEA_BEHAVIOR: {moved} of {len(now)} case(s) moved")

    if args.out:
        Path(args.out).write_text(json.dumps(now, indent=1, sort_keys=True), encoding="utf-8")
        print(f"CEA_BEHAVIOR: {len(now)} case(s) written to {args.out}")

    if not args.diff and not args.out:
        print(f"CEA_BEHAVIOR: {len(now)} case(s); pass --out or --diff")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
