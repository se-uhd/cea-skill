#!/bin/sh
# Every gate, in the environments that actually differ. `check.sh` alone passes on a machine that
# happens to hold the papers and poppler, which is how a test that needed a paper and did not say
# so reached a green run.
#
#     sh scripts/gates.sh
#
# 1. a fresh clone: only the files a commit would carry, so anything gitignored is absent
# 2. this tree, release mode: the papers present and no skipped test tolerated
#
# Both have to pass. Nothing is "green" until this says so.
set -e
cd "$(dirname "$0")/.."
root="$(pwd)"
failed=0

echo "=============================================================="
echo "GATE 1/2  a fresh clone (tracked and committable files only)"
echo "=============================================================="
clone="$(mktemp -d)"
trap 'rm -rf "$clone"' EXIT
# --cached and --others --exclude-standard: what a commit would carry, which is what someone
# cloning gets. Gitignored paths -- the papers, the workspace -- are left out on purpose.
git ls-files --cached --others --exclude-standard -z \
  | while IFS= read -r -d '' f; do
        mkdir -p "$clone/$(dirname "$f")"
        cp "$root/$f" "$clone/$f"
    done
if sh "$clone/scripts/check.sh"; then
    echo "GATE 1: pass"
else
    echo "GATE 1: FAIL -- the suite does not survive a fresh clone"
    failed=1
fi

echo
echo "=============================================================="
echo "GATE 2/2  this tree, release mode (CEA_REQUIRE_PAPERS=1)"
echo "=============================================================="
# The papers are third-party and not committed, so this gate cannot run where they are absent,
# which includes CI. Saying so beats failing there: a gate that cannot pass in CI is a gate nobody
# runs in CI, and then the script itself is only ever exercised by hand.
if [ -z "$(find "$root/evals/papers" -name '*.pdf' -print -quit 2>/dev/null)" ]; then
    echo "GATE 2: not run -- no PDFs in evals/papers/, which are third-party and not committed."
    echo "  A release has to run this gate on a tree that holds them."
    gate2="not run"
elif CEA_REQUIRE_PAPERS=1 sh "$root/scripts/check.sh"; then
    echo "GATE 2: pass"
    gate2="pass"
else
    echo "GATE 2: FAIL -- a release run skipped a test or something broke"
    failed=1
fi

echo
if [ "$failed" -ne 0 ]; then
    echo "CEA_GATES: FAILED"
    exit 1
elif [ "${gate2:-}" = "not run" ]; then
    echo "CEA_GATES: gate 1 passes; gate 2 needs the papers and was not run here"
else
    echo "CEA_GATES: both gates pass"
fi
