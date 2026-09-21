#!/bin/sh
# Copy the paper this case reads into the run's workspace. The papers are not committed, so the
# operator names their directory. A run inherits only an allowlist plus EVAL_* from the shell, so
# the variable has to carry that prefix. No absolute path is written here: this file is committed.
set -e
papers="${EVAL_CEA_PAPERS:-}"
if [ -z "$papers" ]; then
    echo "set EVAL_CEA_PAPERS to the directory holding the eval papers" >&2
    exit 1
fi
if [ ! -f "$papers/tse26-ai-code-review.pdf" ]; then
    echo "missing $papers/tse26-ai-code-review.pdf" >&2
    exit 1
fi
mkdir -p evals/papers
cp "$papers/tse26-ai-code-review.pdf" evals/papers/tse26-ai-code-review.pdf
