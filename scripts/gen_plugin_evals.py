#!/usr/bin/env python3
"""Generate the `claude plugin eval` case directories from the skill's evals.json.

The two eval harnesses want different files for the same test cases. The skill-creator plugin reads
one `evals/evals.json` inside the skill, and `claude plugin eval` reads a directory per case at the
plugin root, each with a `prompt.md`, a `case.yaml` and one file per grader. Writing both by hand
means keeping two copies of every prompt in step, so `evals.json` is the source and this script
writes the case directories from it.

    python3 scripts/gen_plugin_evals.py            # write the cases
    python3 scripts/gen_plugin_evals.py --check    # exit 1 if the cases are stale

Run the cases with both operator flags, or every step of the skill is impossible:

    EVAL_CEA_PAPERS="$PWD/evals/papers" \\
        claude plugin eval ./ --scaffold --allow-tools Bash Write Edit

`allowed_tools` in a prompt.md only asks; `claude plugin eval` grants Bash, Write and Edit through
--allow-tools, and runs a case's scaffold_script only under --scaffold. A run inherits only an
allowlist from the shell plus EVAL_* variables, which is why the papers directory is named by
EVAL_CEA_PAPERS. Without all three the workspace holds no paper and the skill cannot run.

The test suite runs `--check`, so a prompt edited in `evals.json` and not regenerated here fails.

The grader sets differ in granularity on purpose. skill-creator grades each expectation separately.
`claude plugin eval` charges three judge calls per `llm` grader per run, so the expectations become
one rubric, beside two graders that cost nothing: one that the skill was invoked, and one that the
validator accepted the record.
"""

import argparse
import json
import re
import shutil
import sys
import unicodedata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EVALS_JSON = REPO / "skills" / "extract-claims" / "evals" / "evals.json"
OUT = REPO / "evals"
SKILL = "extract-claims"

# The papers are third-party and not committed, so a case copies one in from evals/papers/ rather
# than carrying it. Scaffold scripts run only under `claude plugin eval --scaffold`.
FIXTURE = """#!/bin/sh
# Copy the paper this case reads into the run's workspace. The papers are not committed, so the
# operator names their directory. A run inherits only an allowlist plus EVAL_* from the shell, so
# the variable has to carry that prefix. No absolute path is written here: this file is committed.
set -e
papers="${EVAL_CEA_PAPERS:-}"
if [ -z "$papers" ]; then
    echo "set EVAL_CEA_PAPERS to the directory holding the eval papers" >&2
    exit 1
fi
if [ ! -f "$papers/%(pdf)s" ]; then
    echo "missing $papers/%(pdf)s" >&2
    exit 1
fi
mkdir -p evals/papers
cp "$papers/%(pdf)s" evals/papers/%(pdf)s
"""


# An expectation the record alone cannot settle: it speaks of the validator's output, or of
# the paper's text, neither of which the grader is shown.
_NEEDS_MORE_THAN_THE_RECORD = re.compile(
    r"prints CEA_VALID|validate prints|the paper's text|found on its stated page", re.I)


def paper_id(pdf: str) -> str:
    """The directory `extract` writes the record into, folded as `cmd_extract` folds it.

    The grader has to name a literal path, so the fold has to match. A publisher's file name
    carries spaces and accents; the id names a directory in the site.
    """
    folded = unicodedata.normalize("NFKD", Path(pdf).stem)
    return re.sub(r"[^A-Za-z0-9._-]+", "-",
                  "".join(c for c in folded if not unicodedata.combining(c))).strip("-._")


def case_name(pdf: str) -> str:
    """A directory name that says which paper the case reads."""
    return f"{SKILL}-{pdf[:-4]}"


def files(entry: dict) -> dict[str, str]:
    """Every file of one case, by its path inside the case directory."""
    pdf = Path(entry["files"][0]).name
    # The llm grader is shown claims.json and nothing else, so an expectation about the
    # validator's output or about the paper's text is one it cannot check. Left in, a strict
    # judge fails every run on them and a lenient one drops them silently. They keep their place
    # in evals.json, where skill-creator grades each separately, and the regex grader already
    # proves the validator ran.
    judged = [e for e in entry["expectations"] if not _NEEDS_MORE_THAN_THE_RECORD.search(e)]
    expectations = "\n".join(f"- {e}" for e in judged)
    return {
        "prompt.md": (
            "---\n"
            "max_turns: 60\n"
            "timeout_seconds: 1800\n"
            "allowed_tools: [Bash, Read, Write, Edit, Glob, Grep, Skill]\n"
            "---\n\n"
            f"{entry['prompt']}\n"
        ),
        "case.yaml": (
            'schema_version: "1.1"\n'
            f"name: {case_name(pdf)}\n"
            "tags: [cea, extract-claims]\n"
            "context:\n"
            "  scaffold_script: fixture.sh\n"
        ),
        "fixture.sh": FIXTURE % {"pdf": pdf},
        "graders/skill-fired.md": (
            "---\n"
            "type: tool_used\n"
            "tool: Skill\n"
            f"input_match: '\"skill\"\\s*:\\s*\"(?:[\\w-]+:)?{SKILL}\"'\n"
            "---\n"
        ),
        "graders/record-validates.md": (
            "---\n"
            "type: regex\n"
            "target: trace\n"
            # Not bare CEA_VALID: SKILL.md names that marker, and the Skill tool puts the skill
            # body into the trace, so the bare word matches whether or not the validator ran.
            "pattern: 'every quote found on its page'\n"
            "---\n\n"
            "The validator itself printed its success line, so it ran and accepted the record.\n"
        ),
        # `focus` names the file to read. Without it an llm grader judges Claude's closing
        # message, which is the default, so every statement here about "the record the run
        # produced" was being checked against a summary of it. A run that wrote a wrong record
        # and a confident summary passed; one that wrote a perfect record and said "Done."
        # failed. This is the only grader that looks at what the record says.
        "graders/criteria.md": (
            "---\n"
            "type: llm\n"
            "focus:\n"
            "  source: file\n"
            f"  path: cea-out/{paper_id(pdf)}/claims.json\n"
            "---\n\n"
            "PASS only if every statement below holds of the record in the file you are shown.\n"
            "FAIL if any of them does not.\n\n"
            f"{expectations}\n"
        ),
    }


def generate() -> dict[Path, str]:
    """Every generated file, by its path under the plugin root."""
    data = json.loads(EVALS_JSON.read_text(encoding="utf-8"))
    if data.get("skill_name") != SKILL:
        raise SystemExit(f"{EVALS_JSON} names skill {data.get('skill_name')!r}, expected {SKILL!r}")
    out: dict[Path, str] = {}
    seen: dict[str, int] = {}
    for entry in data["evals"]:
        if len(entry.get("files") or []) != 1:
            raise SystemExit(f"eval {entry['id']} must name exactly one input file")
        name = case_name(Path(entry["files"][0]).name)
        if name in seen:
            raise SystemExit(f"eval {entry['id']} and eval {seen[name]} would both write {name}")
        seen[name] = entry["id"]
        base = OUT / name
        for filename, text in files(entry).items():
            out[base / filename] = text
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report stale or missing cases instead of writing them")
    args = ap.parse_args(argv)
    wanted = generate()
    # A case directory that no eval asks for any more still runs, and still bills judge calls.
    cases = {p for w in wanted for p in w.parents if p.parent == OUT}
    orphans = sorted(d for d in OUT.glob(f"{SKILL}-*") if d.is_dir() and d not in cases)
    # A renamed grader leaves its old file in a directory that is still wanted, and it still runs.
    # Finder leaves .DS_Store in any folder it shows; a dot file is nobody's eval case.
    orphans += sorted(f for d in cases for f in d.rglob("*")
                      if f.is_file() and f not in wanted and not f.name.startswith("."))
    if args.check:
        stale = [p for p, text in wanted.items()
                 if not p.is_file() or p.read_text(encoding="utf-8") != text]
        for d in orphans:
            print(f"CEA_FAILED: {d.relative_to(REPO)} is a case no eval asks for; remove it")
        if orphans:
            return 1
        if stale:
            print("CEA_FAILED: these eval cases are stale; run "
                  "python3 scripts/gen_plugin_evals.py")
            for p in sorted(stale):
                print(f"- {p.relative_to(REPO)}")
            return 1
        print(f"CEA_OK: {len(wanted)} generated eval file(s) are current")
        return 0
    for d in orphans:
        shutil.rmtree(d) if d.is_dir() else d.unlink()
        print(f"removed {d.relative_to(REPO)}, which no eval asks for")
    for p, text in wanted.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        if p.suffix == ".sh":
            p.chmod(0o755)
    print(f"CEA_OK: wrote {len(wanted)} file(s) under {OUT.relative_to(REPO)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
