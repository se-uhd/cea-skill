# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). `README.md` says what CEA is and how to install the
plugin. Two skills share one set of scripts:

| Skill | What it is for |
|---|---|
| `skills/extract-claims` | one paper: extract its text, select its claims, validate, and render `claims.md` and `claims.html`. The rules it applies are Section 1 of its `references/framework.md`. |
| `skills/site` | several papers: assemble their pages, an index, and the framework page into a site |

## Where the outputs go

`skills/extract-claims/references/framework.md` is the framework itself and the source of truth
for every term the skills and the published pages use. Section 1 holds the claim-selection rules
that `extract-claims` applies, and Sections 2 to 8 the later steps, so that the rules and the
framework they serve cannot drift apart. It sits in that skill's `references/` because the Agent
Skills spec has a skill name its files by a relative path one level down, and that is the skill
that reads it; `site` resolves it through `FRAMEWORK` in `cea_claims.py` and always publishes it as
the definitions page. Keep it the only account of the terms: a shorter second copy is what
contradicts it.

`scripts/cea_claims.py`, at the root of the plugin, has the commands for both skills. `extract` writes `text.txt`, the agent
writes `claims.json`, `validate` checks every quote against the page text, and `render` writes `claims.md` and
`claims.html`. `site` assembles several papers into a static site with an index, and always publishes the
framework as the page that defines the terms the pages use.

The page template is `cea_page.py` and the site assembly is `cea_site.py`. Both are modules with no command
line of their own; `cea_claims.py` is the only entry point. `cea_page.py` reuses the ordering
functions of `cea_claims.py`, so the page and `claims.md` cannot disagree about which claims stand under which
main result. Changing the page design means editing `cea_page.py`; changing what a record holds means editing
`FIELDS` and `PROPERTIES` in `cea_claims.py`, `references/record-format.md`, `claims.schema.json` (regenerate it with
`cea_claims.py schema --out claims.schema.json`), and the existing records together. The tests fail when the
committed schema is stale, when a field in `FIELDS` has no constraint in `PROPERTIES`, and when the
reference documents a field that the record does not hold. They do not check that the reference
documents every field, and some fields carry no bullet of their own there.

## The published site

[se-uhd/cea-website](https://github.com/se-uhd/cea-website) holds the published claim records and builds them
with this skill, pinned to a tag in its `.skill-version`. A change here reaches the site only when that file
names a tag that contains the change, so release a tag and bump it there.

Before moving that tag, run the gates the way a release runs them:

```sh
sh scripts/gates.sh
```

and check that the site's records still validate under the new build. There is no record format to
read and no backwards compatibility to keep: a record written against older rules is refused by its
shape, which names what is wrong with it. Where a rule moves, run `extract` on those papers again
and select their claims again, rather than editing the old records' fields to match: what needs
checking is the selection, not the field names.

## Rules that the code enforces

- An entry whose `reason`, or whose `selection_reason` on a claim, uses the word "unsure" leaves
  the claim-or-not decision open. The word counts wherever it stands, quoted or not. `render` writes
  `claims.md` but no page, and `site` writes nothing at all, both exiting 1. The checker settles it in
  `claims.json`.
- The page mines a `reason` for the `R\d+` it names and turns each into a tag and a link, but only for the main results the record holds. A paper can print a token of that shape without meaning an id, such as a respondent or a round numbered R1, and there is no other way to write the sentence. A name the record does not hold is passed over, and `validate` warns so that a typo is still noticed.
- `site` publishes the paper file only from inside the record's own directory, and resolves it first, so a symlink cannot publish what it points at.
- `paper.pdf` is the file's name, not a path. `extract` copies the PDF into the record and prints
  the name to write.
- A paper that may not be republished goes up without its PDF and with a `paper.doi`, which the page
  links. The page links `text.txt` only beside the PDF, because it is the paper's whole text.
- Ids (`R1`, `C3`, `E40`) are the anchors of the published pages. Do not renumber them when a record changes.

## Tests

```sh
sh scripts/gates.sh
```

Nothing is green until that says so. It runs `check.sh` twice, in the two environments that differ:
a fresh clone holding only what a commit carries, where everything gitignored is absent, and this
tree in release mode (`CEA_REQUIRE_PAPERS=1`), where the papers are present and no skipped test is
tolerated. `check.sh` on its own passes on a machine that happens to hold the papers and poppler,
which is how a test that needed a paper and did not say so reached a green run.

For a single quick pass while working:

```sh
python3 -m unittest discover scripts/tests
```

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed, and are skipped when
those are missing. `pdftotext` has to be on `PATH` for them to run at all: without it they skip and the suite
still says OK, which is what `CEA_REQUIRE_PAPERS=1` turns into a failure.

`scripts/tests/text_digests.json` pins what `extract` produces for each paper in `evals/papers/`, page by page.
`text.txt` is what every quote is checked against, so a layout change moves what the validator accepts;
without the pin, twelve mutations of `pdf_text.py` changed a real paper's text with the whole suite green.
Regenerate it only when a layout change is intended, and say in the commit what moved:

```sh
python3 scripts/text_digests.py --out scripts/tests/text_digests.json
```

The file records the `pdftotext` version it was made with, and the test skips rather than fails under another
one, because `pdftotext -layout` lays out to its own version.
