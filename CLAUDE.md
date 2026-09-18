# cea-skill

Claude Code skills for Claim-Evidence Alignment (CEA). `README.md` says what CEA is and how to install the
plugin. `skills/cea-extract-claims/SKILL.md` is the instruction the agent follows; the rules it applies are in
`skills/cea-extract-claims/references/narrow-claims.md`.

## Where the outputs go

`skills/cea-extract-claims/scripts/cea_claims.py` has the commands. `extract` writes `text.txt`, the agent
writes `claims.json`, `validate` checks every quote against the page text, and `render` writes `claims.md` and
`claims.html`. `site` assembles several papers into a static site with an index.

The page template is `cea_page.py` and the site assembly is `cea_site.py`. Both are modules of the skill and
have no command line of their own; `cea_claims.py` is the only entry point. `cea_page.py` reuses the ordering
functions of `cea_claims.py`, so the page and `claims.md` cannot disagree about which claims stand under which
main result. Changing the page design means editing `cea_page.py`; changing what a record holds means editing
`SKILL.md`, the reference, the validator, and the existing records together.

## The published site

[se-uhd/cea-website](https://github.com/se-uhd/cea-website) holds the published claim records and builds them
with this skill, pinned to a tag in its `.skill-version`. A change here reaches the site only when that file
names a tag that contains the change, so release a tag and bump it there.

## Rules that the code enforces

- A record whose `reason` starts with "Unsure" leaves the claim-or-not decision open. `render` writes
  `claims.md` but no page, and `site` writes nothing at all, both exiting 1. The checker settles it in
  `claims.json`.
- Ids (`B1`, `C3`, `R40`) are the anchors of the published pages. Do not renumber them when a record changes.

## Tests

```sh
python3 -m unittest discover skills/cea-extract-claims/scripts/tests
```

The tests that read real papers use the PDFs in `evals/papers/`, which are not committed, and are skipped when
those are missing.
