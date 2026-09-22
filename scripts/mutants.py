"""Mutation coverage for the record modules: change one decision, see whether a test notices.

    python3 scripts/mutants.py --module cea_page.py --sample 25
    python3 scripts/mutants.py --sample 90 --out /tmp/mutants.json

A surviving mutant is a decision the suite does not hold. Not every survivor is a hole: a flipped
comparison can be equivalent for every input the records hold. The survivors are a list to triage, by
applying one and reading what the page or the record does differently.

The run starts with a control: the frozen tree, unmutated, must pass. Without it a broken
environment reads as perfect coverage. The first campaign of this kind ran with a PATH whose
`python3` was too old for `check.sh`, so the two tests that run the gate failed for all 153 mutants
and the run reported 152 of them killed, having measured nothing. The run also refuses a result
where one test killed nearly every mutant, which is what that looks like from the outside.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
MODULES = ("cea_claims.py", "cea_page.py", "cea_site.py", "pdf_text.py")
# One test killing more than this share of mutants is an environment fault, not coverage.
ONE_TEST_CEILING = 0.9


class Flip(ast.NodeTransformer):
    """Apply exactly the n-th mutation of one kind, and say what it was."""

    def __init__(self, kind: str, n: int):
        self.kind, self.n, self.i, self.did = kind, n, 0, None

    def _hit(self, label: str) -> bool:
        if self.i == self.n:
            self.did = label
            self.i += 1
            return True
        self.i += 1
        return False

    def visit_Compare(self, node):
        self.generic_visit(node)
        swap = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt,
                ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.In: ast.NotIn, ast.NotIn: ast.In}
        if self.kind != "cmp" or len(node.ops) != 1 or type(node.ops[0]) not in swap:
            return node
        t = type(node.ops[0])
        if self._hit(f"L{node.lineno} {t.__name__}->{swap[t].__name__}"):
            node.ops = [swap[t]()]
        return node

    def visit_Constant(self, node):
        if self.kind != "bool" or not isinstance(node.value, bool):
            return node
        if self._hit(f"L{node.lineno} {node.value}->{not node.value}"):
            return ast.copy_location(ast.Constant(value=not node.value), node)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if self.kind != "boolop":
            return node
        if self._hit(f"L{node.lineno} {type(node.op).__name__} flipped"):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
        return node


def sites(source: str, kind: str) -> int:
    f = Flip(kind, -1)
    f.visit(ast.parse(source))
    return f.i


def mutate(source: str, kind: str, n: int) -> tuple[str, str | None]:
    f = Flip(kind, n)
    tree = f.visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree), f.did


def env() -> dict:
    """Everything the suite needs, and nothing that costs money."""
    path = [str(Path(sys.executable).parent)]
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):
        if (Path(d) / "pdftotext").exists():
            path.append(d)
    return {"PATH": ":".join(path + ["/usr/bin", "/bin"]), "HOME": str(Path.home())}


# Which module each of the others imports, so a mutant is put to every test that can reach it:
# a change in cea_page shows up through cea_site, which builds pages with it.
IMPORTED_BY = {"cea_claims.py": ("cea_claims", "cea_page", "cea_site"),
               "cea_page.py": ("cea_page", "cea_site"),
               "cea_site.py": ("cea_site",),
               "pdf_text.py": ("pdf_text", "cea_claims", "cea_page", "cea_site")}


def tests_reaching(tree: Path, module: str) -> list[str]:
    """The test classes that can reach `module`, by it or by anything importing it.

    Running the whole suite for every mutant costs 18.8 seconds where the classes that can reach
    cea_page cost 1.2. The names are a superset of what can kill a mutant, never a subset, or a
    mutant would be called a survivor because the test that holds it was not run.
    """
    source = (tree / "scripts" / "tests" / "test_scripts.py").read_text(encoding="utf-8")
    parsed = ast.parse(source)
    wanted = IMPORTED_BY.get(module, tuple(m[:-3] for m in MODULES))
    out = []
    for node in parsed.body:
        if not isinstance(node, ast.ClassDef):
            continue
        body = ast.get_source_segment(source, node) or ""
        if any(name in body for name in wanted):
            out.append(f"test_scripts.{node.name}")
    return out


def run_suite(tree: Path, only: list[str] | None = None) -> tuple[int, list[str]]:
    args = ["-m", "unittest"] + (only if only else ["discover", "."])
    done = subprocess.run([sys.executable, *args],
                          cwd=tree / "scripts" / "tests", capture_output=True, text=True,
                          env=env(), timeout=2400)
    failed = sorted({f"{c}.{t}" for t, c in
                     re.findall(r"^(?:FAIL|ERROR): (\w+) \(\w+\.(\w+)", done.stderr, re.M)})
    return done.returncode, failed


def one_test_dominates(results: list[dict]) -> tuple[str, int] | None:
    """The test that failed for most mutants, where that share is too high to be coverage.

    A test that notices nearly every change is not a thorough test: it is failing for a reason the
    mutant did not cause. The campaign that ran `check.sh` under a python too old for it had two
    such tests, and read as 99% coverage.
    """
    if not results:
        return None
    counts = collections.Counter(t for r in results for t in set(r["killed"]))
    if not counts:
        return None
    test, n = counts.most_common(1)[0]
    return (test, n) if n > ONE_TEST_CEILING * len(results) else None


def moves_output(frozen: Path, module: str, kind: str, n: int, baseline: str) -> int:
    """How many of behavior.py's cases a mutant moves, over the records.

    A surviving mutant that moves nothing changes no record's problems, no claims.md and no page
    for any paper recorded, so it is a decision the records never reach rather than one the
    suite fails to hold. A survivor that moves cases is a gap: the output differs and nothing
    said so.
    """
    source = (frozen / "scripts" / module).read_text(encoding="utf-8")
    mutated, _ = mutate(source, kind, n)
    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "t"
        shutil.copytree(frozen, tree)
        (tree / "scripts" / module).write_text(mutated, encoding="utf-8")
        try:
            done = subprocess.run([sys.executable, str(tree / "scripts" / "behavior.py"),
                                   "--diff", baseline], capture_output=True, text=True,
                                  env=env(), timeout=2400, cwd=tree)
        except subprocess.TimeoutExpired:
            # One survivor that will not finish used to end the whole campaign and lose every
            # verdict already measured. It is reported as unmeasured instead.
            return -1
    found = re.search(r"CEA_BEHAVIOR: (\d+) of \d+ case\(s\) moved", done.stdout)
    return int(found.group(1)) if found else -1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--module", action="append", choices=MODULES,
                    help="a module to mutate; repeatable, default all but pdf_text.py")
    ap.add_argument("--sample", type=int, default=30, help="mutants per module")
    ap.add_argument("--seed", type=int, default=41)
    ap.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) * 3 // 4),
                    help="default: three quarters of the cores")
    ap.add_argument("--out", help="write the result here as JSON")
    ap.add_argument("--triage", metavar="BASELINE",
                    help="for each survivor, how many behavior.py cases it moves, against this "
                         "baseline written by `behavior.py --out`")
    args = ap.parse_args(argv)
    chosen = args.module or [m for m in MODULES if m != "pdf_text.py"]

    frozen = Path(tempfile.mkdtemp()) / "frozen"
    shutil.copytree(REPO, frozen, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    print(f"CEA_MUTANTS: frozen at {frozen}", flush=True)

    code, failed = run_suite(frozen)
    if code != 0:
        print(f"CEA_FAILED: the control run of the unmutated tree does not pass, so nothing a "
              f"mutant does can be read. {len(failed)} test(s) failed, first: "
              f"{failed[0] if failed else '(none parsed)'}")
        return 1
    print("CEA_MUTANTS: control run passes", flush=True)

    random.seed(args.seed)
    source = {m: (frozen / "scripts" / m).read_text(encoding="utf-8") for m in chosen}
    jobs = []
    for m in chosen:
        pool = [(k, i) for k in ("cmp", "bool", "boolop") for i in range(sites(source[m], k))]
        jobs += [(m, k, i) for k, i in random.sample(pool, min(args.sample, len(pool)))]
    reaching = {m: tests_reaching(frozen, m) for m in chosen}
    for m in chosen:
        print(f"CEA_MUTANTS: {m} is put to {len(reaching[m])} test class(es)", flush=True)
    print(f"CEA_MUTANTS: {len(jobs)} mutant(s) on {args.jobs} worker(s)", flush=True)

    def one(job):
        m, kind, i = job
        mutated, did = mutate(source[m], kind, i)
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "t"
            shutil.copytree(frozen, tree)
            (tree / "scripts" / m).write_text(mutated, encoding="utf-8")
            try:
                _, killed = run_suite(tree, reaching[m])
            except subprocess.TimeoutExpired:
                killed = ["<timeout>"]
        return {"module": m, "kind": kind, "n": i, "where": did, "killed": killed}

    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for k, r in enumerate(pool.map(one, jobs), 1):
            results.append(r)
            if k % 15 == 0:
                print(f"  {k}/{len(jobs)}", flush=True)

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")

    dominant = one_test_dominates(results)
    if dominant:
        test, n = dominant
        print(f"CEA_FAILED: {test} failed for {n} of {len(results)} mutants. One test that "
              f"notices nearly every change is a fault in how the suite was run, not coverage.")
        return 1

    print()
    total = survived = 0
    for m in chosen:
        mine = [r for r in results if r["module"] == m]
        out = [r for r in mine if not r["killed"]]
        total += len(mine); survived += len(out)
        print(f"  {m:16s} {len(mine):3d} mutant(s), {len(out):3d} survived "
              f"-> {100 * (len(mine) - len(out)) // max(len(mine), 1)}% killed")
    print(f"  {'TOTAL':16s} {total:3d} mutant(s), {survived:3d} survived "
          f"-> {100 * (total - survived) // max(total, 1)}% killed")
    survivors = [r for r in results if not r["killed"]]
    if args.triage:
        print(f"\n  triage: how many of behavior.py's cases each of the {len(survivors)} survivor(s)"
              f" moves. Every case runs validate, render, the page and the site, so no module can be"
              f" left out of one.", flush=True)
        # as_completed, not map: map yields in order and prints nothing until the slowest of a batch
        # finishes, so a twenty-minute run showed no progress at all.
        done_n = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(moves_output, frozen, r["module"], r["kind"], r["n"],
                                   args.triage): r for r in survivors}
            for future in as_completed(futures):
                r = futures[future]
                r["moved"] = future.result()
                done_n += 1
                verdict = ("GAP" if r["moved"] > 0 else
                           "reaches nothing in the records" if r["moved"] == 0 else
                           "could not be measured")
                print(f"  [{done_n}/{len(survivors)}] {r['module']:16s} {r['kind']:7s} "
                      f"{r['where']:22s} {r['moved']:5d} case(s)  {verdict}", flush=True)
        gaps = [r for r in survivors if r.get("moved", 0) > 0]
        print(f"\n  {len(gaps)} of {len(survivors)} survivor(s) change what the records produce")
        if args.out:
            Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    else:
        for r in survivors:
            print(f"  SURVIVOR {r['module']:16s} {r['kind']:7s} {r['where']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
