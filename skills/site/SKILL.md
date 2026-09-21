---
name: site
description: >-
  Build a static site from a set of Claim-Evidence Alignment (CEA) records, one directory per paper:
  a page per paper, an index listing every paper with its counts, and the framework text that the
  pages' terms come from. Use this skill when someone wants to publish, deploy, or preview the claim
  records of several papers together, add a paper to such a site, or rebuild it after a record
  changed. It renders records that the extract-claims skill has already produced and checked; it does not
  read a PDF or select any claim. A record that still leaves a decision open stops the build.
license: MIT
compatibility: Requires Python 3.10 or newer (standard library only).
---

# Build a site from claim records

This skill publishes the records that `extract-claims` writes. One directory per paper holds
`claims.json`, `claims.md`, `text.txt` and the PDF, and the build turns each one into a page and
lists them on an index. The pages are generated, never hand-edited: the record is the source, and
changing what a page says means changing the record or the skill that renders it.

## Stop conditions

- An entry whose `reason`, or whose `selection_reason` on a claim, uses the word "unsure" leaves
  open whether the statement is a claim, wherever the word stands. Every
  record is checked before anything is written, so one such record stops the whole site. Report it
  and let the checker settle it in `claims.json`. Do not edit the reason yourself to get the build
  through.
- A record that fails `cea_claims.py validate` is not published. Run the validator first.
- Do not write into the output directory by hand. Each build writes the records it is given over what is there. A paper dropped from the list keeps its old directory, which the new index no longer lists, so remove the output directory to start clean.

## The command

The scripts are in `scripts/` at the root of the plugin, beside the `skills/` directory that holds this skill, shared with
`extract-claims`.

```sh
python3 <plugin root>/scripts/cea_claims.py validate <paper directory>
python3 <plugin root>/scripts/cea_claims.py render <paper directory>
python3 <plugin root>/scripts/cea_claims.py site <paper directory> [<paper directory> ...] \
    --out <site directory>
```

Run `validate` for each paper first. The build runs it too and refuses the record, but it prints
only the first three problems per paper, while `validate` prints them all with their warnings.

`site` writes:

| Path | What it holds |
|---|---|
| `<site>/index.html` | every paper, with its counts and a link to its page |
| `<site>/papers/<paper_id>/index.html` | the paper's page |
| `<site>/papers/<paper_id>/claims.json`, `claims.md`, `text.txt`, the PDF | the record the page was built from, beside it |
| `<site>/framework/index.html` | the framework text, always |

The record sits beside its page so that the page's links to it work wherever the folder is served,
and so that a zip of one paper's folder is complete on its own.

A successful build prints `CEA_SITE`, preceded by a `CEA_WARNING` line for each paper whose PDF is
missing, for each main result that no claim serves, and for each paper directory left in the
output by an earlier build that this one no longer lists. A record that fails the checks prints `CEA_INVALID` or `CEA_UNRESOLVED`, then `CEA_FAILED`.
Nothing is written unless every record holds the fields the pages need, passes `validate`, and
leaves no decision open. The build runs `validate` itself, so a record whose quote is not on its
page stops it. Each record therefore needs its `text.txt` beside its `claims.json`.

## The framework page

The pages use the framework's terms: main result, claim, excluded claim candidate, checker, and
mapping level. Every claim card carries an `L1` badge over six dots, so
the document has to define the mapping levels and name the six links of the chain the badge shows:
interpretation, operationalization, measurement, unit bridge, analysis, and reasoning. `site` always publishes the framework as a page and links it from every footer, so a reader has
the definitions at hand. It publishes the plugin's own
`skills/extract-claims/references/framework.md`, which is the framework the records are built
against and holds every term the pages use, and there is nothing to pass: a document handed in
could be the wrong one or a stale copy, and a site built without one showed every claim's mapping
level with no page anywhere defining it. The framework and the selection rules are one document
for the same reason: a second, shorter account of the rules is the thing most likely to
contradict them.

## Publishing

A site directory is static, so any host serves it. Where the records live in a repository that
deploys to GitHub Pages, keep the skill pinned: the repository names a tag of this plugin, its
workflow checks that tag out and runs the commands above, and a change to the page design reaches
the site only when that tag moves. `se-uhd/cea-website` is that arrangement, and its `CLAUDE.md`
states the contract.

Report to the user: where the site is, how many papers it holds, which records stopped the build,
and any paper whose page says that no claim serves one of its main results, because that is
a finding a reader should not have to search for.
