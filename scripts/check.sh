#!/bin/sh
# Every check that guards this plugin, in one command. CI runs it, and so should a release tag.
#
#     sh scripts/check.sh
#
# The unit tests carry the checks that matter, including the Agent Skills spec limits. They need
# only Python, except for the tests that read the papers in evals/papers/, which also need
# pdftotext and skip without it. The two external validators run when they are on PATH and are
# reported as skipped when they are not.
set -e
cd "$(dirname "$0")/.."

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || {
    echo "CEA_FAILED: python3 is $(python3 -V 2>&1), and the skills declare 3.10 or newer"
    exit 1
}

echo "== unit tests =="
# The count of skipped tests is printed rather than left in the noise: the papers in evals/papers/
# are not committed, so on a machine without them every test that reads a real paper skips, the
# run still says OK, and a change to the reading order would go unnoticed. Set
# CEA_REQUIRE_PAPERS=1 to make that a failure, which a release should.
tests="$(mktemp)"
count="$(mktemp)"
trap 'rm -f "$tests" "$count"' EXIT
# The runner reports how many tests it skipped through its own exit status, not through its
# printed output: a test that prints a line resembling a verdict was able to stand in for the
# real one and carry a run with skipped tests past the release gate. It also writes the count on
# a marked line of its own, which is read first so that anything appended later cannot displace
# it, including by a handler running at interpreter shutdown.
#
# Not a pipeline either: a pipeline's status is its last command's, so `set -e` would never see
# the suite fail. And not a bare call: `set -e` would end the script before the output could be
# printed, and the trap would delete the evidence.
if python3 - "$count" <<'PYTHON' > "$tests" 2>&1
import sys
import unittest

suite = unittest.TestLoader().discover("scripts/tests")
found = suite.countTestCases()
result = unittest.TextTestRunner(verbosity=1).run(suite)
# `found` as well as `ran`: a class that skips in setUpClass is one skip however many tests it
# holds, and testsRun does not count them, so a whole class could vanish behind a single line.
with open(sys.argv[1], "w", encoding="utf-8") as f:
    f.write(f"CEA_TESTS ran={result.testsRun} found={found} "
            f"skipped={len(result.skipped)} ok={int(result.wasSuccessful())}\n")
if not result.wasSuccessful():
    sys.exit(1)
# A run of nothing is not a run. A rename, a changed discovery pattern or a lost package file
# turns the gate into a no-op, and `wasSuccessful` is true for zero tests.
if result.testsRun < 100:
    print(f"CEA_FAILED: only {result.testsRun} test(s) were discovered under scripts/tests",
          file=sys.stderr)
    sys.exit(1)
sys.exit(3 if result.skipped or found > result.testsRun else 0)
PYTHON
then
    rc=0
else
    rc=$?
fi
cat "$tests"
# Three things have to agree: the status, the count file, and what the runner printed. A test can
# take over any one of them. os._exit supplies a clean status, and a handler at interpreter
# shutdown can rewrite the file, so none of them is trusted alone. The printed verdict is its own
# last word and a run that failed says so there.
ran="$(sed -n 's/^CEA_TESTS ran=\([0-9]*\).*/\1/p' "$count" | head -1)"
ok="$(sed -n 's/^CEA_TESTS .*ok=\([01]\)$/\1/p' "$count" | head -1)"
if [ -z "$ran" ] || [ -z "$ok" ]; then
    echo "CEA_FAILED: the test run did not report a result. It was ended before it finished."
    exit 1
fi
if [ "$ok" != "1" ]; then
    echo "CEA_FAILED: the test run reported failures"
    exit 1
fi
if grep -q '^FAILED' "$tests" || ! grep -q '^OK' "$tests"; then
    echo "CEA_FAILED: the test run printed no clean verdict of its own"
    exit 1
fi
# The floor is read here as well as inside the driver, because the driver reports it through the
# exit status and that is the channel a test can seize.
if [ "$ran" -lt 100 ]; then
    echo "CEA_FAILED: only $ran test(s) ran, which is too few to be the suite"
    exit 1
fi
# `rc`, not `status`: zsh reserves `status` and refuses to assign to it, which ended the run here
# on a tree with nothing wrong with it.
if [ "$rc" -ge 128 ]; then
    echo "CEA_FAILED: the test suite was killed by signal $((rc - 128))"
    exit "$rc"
fi
[ "$rc" -eq 0 ] || [ "$rc" -eq 3 ] || exit "$rc"
if [ "$rc" -eq 3 ]; then
    # `.*$`, not `$`: the count line ends with `ok=1`, so anchoring at the skipped
    # number captured nothing and every run said "some test(s)".
    skipped="$(sed -n 's/^CEA_TESTS .*skipped=\([0-9]*\).*$/\1/p' "$count" | head -1)"
    found="$(sed -n 's/^CEA_TESTS .*found=\([0-9]*\).*$/\1/p' "$count" | head -1)"
    if [ -n "$found" ] && [ -n "$ran" ] && [ "$found" -gt "$ran" ]; then
        # Stated as the fact, not as a comparison with the skip count: a class that skips in
        # setUpClass is one skip entry hiding several tests, so found-minus-ran is normally a
        # part of the reported total, and claiming it is "more than" that total read as nonsense
        # beside its own numbers ("7 ... which is more than the 23 skip(s)").
        echo "CEA_SKIPPED: $((found - ran)) of the $found discovered test(s) never ran at all."
        echo "  A class that skips in setUpClass reports one skip however many tests it holds."
    fi
    echo "CEA_SKIPPED: ${skipped:-some} test(s) did not run. The tests that read a real paper need"
    echo "  the PDFs in evals/papers/, which are third-party and not committed, and the"
    echo "  page-script tests need node. Check which before trusting the run."
    if [ -n "${CEA_REQUIRE_PAPERS:-}" ]; then
        echo "CEA_FAILED: CEA_REQUIRE_PAPERS is set and ${skipped:-some} test(s) skipped"
        exit 1
    fi
fi

echo
echo "== the committed schema is what the code generates =="
generated="$(mktemp)"
trap 'rm -f "$tests" "$count" "$generated"' EXIT
python3 scripts/cea_claims.py schema > "$generated"
diff -u claims.schema.json "$generated" \
  || { echo "claims.schema.json is stale: python3 scripts/cea_claims.py schema --out claims.schema.json"; exit 1; }
echo "claims.schema.json is current"

echo
echo "== Agent Skills spec (skills-ref) =="
if command -v skills-ref >/dev/null 2>&1; then
    for md in skills/*/SKILL.md; do skills-ref validate "$(dirname "$md")"; done
else
    echo "skipped: skills-ref is not installed (pip install 'skills-ref @ \
git+https://github.com/agentskills/agentskills#subdirectory=skills-ref')"
fi

echo
echo "== Claude Code plugin and marketplace manifests =="
if command -v claude >/dev/null 2>&1; then
    claude plugin validate ./
    claude plugin validate ./skills --strict
else
    echo "skipped: the claude CLI is not installed"
fi
