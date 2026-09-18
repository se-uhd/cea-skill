# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). CEA reconstructs the chain from a paper's narrow quantitative claims to the evidence in the paper and its research artifact, and compares what each claim asserts with what the chain shows. Claude Code drafts each step, and a person checks it.

| Skill | What it does |
|---|---|
| `cea-extract-claims` | Extracts the narrow claims, the broad statements that they serve, and the rejected candidates from a paper PDF, with every quote checked against the page text. Renders the record as Markdown and as a page, and assembles the pages of several papers into a site |

## Installation

```text
/plugin marketplace add se-uhd/cea-skill
/plugin install cea-skill@cea-skill
```

The scripts of `cea-extract-claims` need Python 3.10 or newer (standard library only) and `pdftotext` from poppler (`brew install poppler` or `apt-get install poppler-utils`).

## The site

`cea_claims.py site <paper directory> ... --out _site` builds a static site from a set of claim records: one page per paper and an index listing them. [se-uhd/cea-website](https://github.com/se-uhd/cea-website) holds the published records and runs this in CI. A record whose reason still says "Unsure" stops the build, because a reader cannot tell an open question from a decision.

## Tests

```sh
python3 -m unittest discover skills/cea-extract-claims/scripts/tests
```

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed. These tests are skipped when the PDFs are missing.
