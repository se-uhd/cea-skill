# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). CEA reconstructs the chain from a paper's narrow quantitative claims to the evidence in the paper and its research artifact, and compares what each claim asserts with what the chain shows. An agent drafts each step, and a person checks it.

| Skill | What it does |
|---|---|
| `cea-extract-claims` | Extracts the narrow claims, the broad statements they serve, and the rejected candidates from a paper PDF, with every quote checked against the page text |

## Installation

```text
/plugin marketplace add se-uhd/cea-skill
/plugin install cea-skill@cea-skill
```

The scripts need Python 3.10 or newer (standard library only) and `pdftotext` from poppler (`brew install poppler` or `apt-get install poppler-utils`).

## Tests

```sh
python3 -m unittest discover skills/cea-extract-claims/scripts/tests
```

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed. They are skipped when the PDFs are missing.
