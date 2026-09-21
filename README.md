# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). CEA reconstructs the chain from a paper's narrow quantitative claims to the evidence in the paper and its research artifact, and compares what each claim asserts with what the chain shows. Claude Code drafts each step, and a person checks it.

| Skill | What it does |
|---|---|
| `extract-claims` | Extracts the claims, the main results that they serve, and the excluded claim candidates from a paper PDF, with every quote checked against the page text, and renders the record as Markdown and as a page |
| `site` | Builds a site from the records of several papers: a page each, an index, and the framework text the pages' terms come from |

## Installation

```text
/plugin marketplace add se-uhd/cea-skill
/plugin install cea-skill@cea-skill
```

The scripts, in `scripts/` at the root of the plugin and shared by both skills, need Python 3.10 or newer (standard library only) and `pdftotext` from poppler (`brew install poppler` or `apt-get install poppler-utils`). Both skills call those shared scripts, so a `skills/<name>/` directory copied on its own does not run. Install the plugin.

## The record

`claims.json` holds the record for one paper. `cea_claims.py schema` prints its JSON Schema, generated from the field definitions in the script itself. `claims.schema.json` at the repository root is that file. A checker editing a record by hand can point their editor at it, with `"$schema"` set to the file's absolute path or URL. The path is local either way: `site` republishes `claims.json` without the `$schema` key, so whatever a checker sets never reaches a reader. `cea_claims.py validate` checks everything a schema cannot, such as whether each quote stands on its page.

## The site

`cea_claims.py site <paper directory> ... --out _site` builds a static site from a set of claim records: one page per paper, an index listing them, and the framework text as a page that the footers link. The framework is the plugin's own `skills/extract-claims/references/framework.md`, so there is nothing to pass and no way to publish a site whose pages define none of the terms they use. [se-uhd/cea-website](https://github.com/se-uhd/cea-website) holds the published records and runs this in CI, pinned to a tag of this plugin. An entry whose reason still says it is unsure stops the build, because a reader cannot tell an open question from a decision.

## Tests

```sh
sh scripts/gates.sh
```

That runs every check twice: once over a fresh clone, holding only what a commit carries, and once over the working tree in release mode, where the papers have to be present and no skipped test is tolerated. `python3 -m unittest discover scripts/tests` is the quick pass while working.

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed, and need `pdftotext` on `PATH`. They skip without either.
