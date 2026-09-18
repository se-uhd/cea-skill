# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). CEA reconstructs the chain from a paper's narrow quantitative claims to the evidence in the paper and its research artifact, and compares what each claim asserts with what the chain shows. Claude Code drafts each step, and a person checks it.

| Skill | What it does |
|---|---|
| `cea-extract-claims` | Extracts the narrow claims, the broad statements that they serve, and the rejected candidates from a paper PDF, with every quote checked against the page text, and renders the record as Markdown and as a page |
| `cea-site` | Builds a site from the records of several papers: a page each, an index, and the framework text the pages' terms come from |

## Installation

```text
/plugin marketplace add se-uhd/cea-skill
/plugin install cea-skill@cea-skill
```

The scripts, in `scripts/` at the root of the plugin and shared by both skills, need Python 3.10 or newer (standard library only) and `pdftotext` from poppler (`brew install poppler` or `apt-get install poppler-utils`).

## The site

`cea_claims.py site <paper directory> ... --out _site --framework <framework.md>` builds a static site from a set of claim records: one page per paper, an index listing them, and the framework text as a page that the footers link. [se-uhd/cea-website](https://github.com/se-uhd/cea-website) holds the published records and runs this in CI, pinned to a tag of this plugin. A record whose reason still says "Unsure" stops the build, because a reader cannot tell an open question from a decision.

## Tests

```sh
python3 -m unittest discover scripts/tests
```

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed. These tests are skipped when the PDFs are missing.
