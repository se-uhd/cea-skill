"""Bring a format-1 record to format 2.

Format 2 renamed the terms and the ids: `broad_statements` became `main_results` and `rejected`
became `excluded`, a `B` id became `R`, and an `R` id became `E`. This rewrites a record in place.

    python3 scripts/migrate_format_2.py <record directory>...
    python3 scripts/migrate_format_2.py --check <record directory>...
    python3 scripts/migrate_format_2.py --terms <record directory>...

Only an id the record actually holds is rewritten, so a `reason` that says "vitamin B12" keeps its
word. The two id renames run in one pass over a map, never as two passes, because B->R followed by
R->E would carry every old B id through to E.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

RENAMED = {"broad_statements": "main_results", "rejected": "excluded"}
ID_FIELDS = ("serves", "duplicate_of", "breaks_down")
PROSE_FIELDS = ("reason", "note", "selection_reason", "states")


def id_map(data: dict) -> dict[str, str]:
    """Every old id in the record, mapped to its format-2 name."""
    out = {}
    for old_key, prefix in (("broad_statements", "R"), ("rejected", "E")):
        for e in data.get(old_key) or []:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                out[e["id"]] = prefix + e["id"][1:]
    return out


# The terms moved with the fields, and a record's own prose names them: a `reason` saying "no broad
# statement states a main result about X" reaches the published page, where the term no longer
# means anything.
TERMS = (("broad statements", "main results"), ("Broad statements", "Main results"),
         ("broad statement", "main result"), ("Broad statement", "Main result"),
         ("narrow claims", "claims"), ("Narrow claims", "Claims"),
         ("narrow claim", "claim"), ("Narrow claim", "Claim"),
         ("rejected candidates", "excluded candidates"),
         ("Rejected candidates", "Excluded candidates"),
         ("rejected candidate", "excluded candidate"),
         ("Rejected candidate", "Excluded candidate"))


def rewrite(text: str, ids: dict[str, str]) -> str:
    """Rename the ids this record holds, and the terms that moved with them.

    Nothing that merely looks like an id is touched, so a `reason` naming vitamin B12 in a record
    that has no B12 entry keeps its word.
    """
    for a, b in TERMS:
        text = re.sub(rf"\b{re.escape(a)}\b", b, text)
    if not ids:
        return text
    pattern = re.compile(r"\b(" + "|".join(sorted(ids, key=len, reverse=True)) + r")\b")
    return pattern.sub(lambda m: ids[m.group(1)], text)


def migrate(data: dict) -> dict:
    ids = id_map(data)
    out: dict = {}
    for key, value in data.items():
        out[RENAMED.get(key, key)] = value
    out["format"] = 2
    for key in ("main_results", "claims", "excluded"):
        for e in out.get(key) or []:
            if not isinstance(e, dict):
                continue
            if isinstance(e.get("id"), str) and e["id"] in ids:
                e["id"] = ids[e["id"]]
            for f in ID_FIELDS:
                if isinstance(e.get(f), list):
                    e[f] = [ids.get(v, v) if isinstance(v, str) else v for v in e[f]]
            for f in PROSE_FIELDS:
                if isinstance(e.get(f), str):
                    e[f] = rewrite(e[f], ids)
    # `format` first, then the lists in the order the record reads in
    order = ["format", "paper", "main_results", "claims", "excluded"]
    return {k: out[k] for k in order if k in out} | {k: v for k, v in out.items() if k not in order}


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        print(__doc__)
        return 2
    done = 0
    for d in paths:
        record = d / "claims.json" if d.is_dir() else d
        if not record.is_file():
            print(f"CEA_FAILED: no claims.json at {record}")
            return 2
        data = json.loads(record.read_text(encoding="utf-8"))
        if data.get("format") == 2 and "--terms" not in argv:
            print(f"CEA_OK: {record} is already format 2")
            continue
        new = migrate(data)
        if check:
            print(f"CEA_OK: {record} would become format 2, "
                  f"{len(id_map(data))} id(s) renamed")
        else:
            record.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n",
                              encoding="utf-8")
            print(f"CEA_OK: {record} is format 2, {len(id_map(data))} id(s) renamed")
        done += 1
    print(f"CEA_MIGRATED: {done} record(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
