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

- A record whose `reason` starts with "Unsure" leaves open whether the statement is a claim. Every
  record is checked before anything is written, so one such record stops the whole site. Report it
  and let the checker settle it in `claims.json`. Do not edit the reason yourself to get the build
  through.
- A record that fails `cea_claims.py validate` is not published. Run the validator first.
- Do not write into the output directory by hand. It is rebuilt from the records every time.

## The command

The scripts are in `scripts/` at the root of the plugin, beside this skill's directory, shared with
`extract-claims`.

```sh
python3 <plugin root>/scripts/cea_claims.py validate <paper directory>
python3 <plugin root>/scripts/cea_claims.py render <paper directory>
python3 <plugin root>/scripts/cea_claims.py site <paper directory> [<paper directory> ...] \
    --out <site directory> [--framework <framework.md>]
```

Run `validate` and `render` for each paper first: `render` rewrites `claims.md`, and a site built
from a record whose Markdown is stale publishes two versions of the same record.

`site` writes:

| Path | What it holds |
|---|---|
| `<site>/index.html` | every paper, with its counts and a link to its page |
| `<site>/papers/<paper_id>/index.html` | the paper's page |
| `<site>/papers/<paper_id>/claims.json`, `claims.md`, `text.txt`, the PDF | the record the page was built from, beside it |
| `<site>/framework/index.html` | the framework text, when `--framework` names a Markdown file |

The record sits beside its page so that the page's links to it work wherever the folder is served,
and so that a zip of one paper's folder is complete on its own.

The first output line starts with `CEA_SITE` on success, and with `CEA_UNRESOLVED` or `CEA_FAILED`
otherwise. Nothing is written unless every record passes.

## The framework page

The pages use the framework's terms: main result, narrow claim, rejected candidate, checker,
mapping level. `--framework <file.md>` publishes the framework text as a page of the site and links
it from every footer, so a reader has the definitions at hand. Point it at the project's own
framework document. Do not write a summary of the framework instead: a second, shorter account of
the rules is the thing most likely to contradict them.

## Publishing

A site directory is static, so any host serves it. Where the records live in a repository that
deploys to GitHub Pages, keep the skill pinned: the repository names a tag of this plugin, its
workflow checks that tag out and runs the commands above, and a change to the page design reaches
the site only when that tag moves. `se-uhd/cea-website` is that arrangement, and its `CLAUDE.md`
states the contract.

Report to the user: where the site is, how many papers it holds, which records stopped the build,
and any paper whose page says that no narrow claim serves one of its main results, because that is
a finding a reader should not have to search for.
