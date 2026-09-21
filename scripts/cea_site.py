#!/usr/bin/env python3
"""Assemble a site from a set of claim records, one directory per paper.

`cea_claims.py site` calls `build_site`. Each paper gets `<out>/papers/<paper_id>/` holding the
record, both renderings and the PDF, so the page's relative links work the same locally, on a web
server, and inside a zip of that one folder.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import date
from itertools import count
from pathlib import Path

import cea_page
import cea_claims
from cea_claims import PAPER_ID, _flat, _stated_in, unsettled
from cea_page import E, TEMPLATE

CSS = TEMPLATE[TEMPLATE.index("<style>"):TEMPLATE.index("</style>") + len("</style>")]

PROSE = """<style>
  .prose { max-width: 48rem; }
  .prose h1 { font-family: 'Source Serif 4', Georgia, serif; font-size: 2rem; color: #1c1917; margin-bottom: 0.75rem; }
  .prose h2 { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.2rem; margin: 2rem 0 0.5rem; padding-bottom: 0.5rem; border-bottom: 2px solid #e7e5e4; }
  .prose h3 { font-size: 1rem; margin: 1.5rem 0 0.4rem; }
  .prose p { font-size: 0.9rem; line-height: 1.7; color: #44403c; margin-bottom: 0.75rem; }
  .prose ul, .prose ol { margin: 0 0 0.75rem 1.25rem; font-size: 0.9rem; color: #44403c; }
  .prose li { margin-bottom: 0.25rem; }
  .prose table { border-collapse: collapse; width: 100%; font-size: 0.85rem; background: #fff; margin-bottom: 1rem; }
  .prose th, .prose td { border: 1px solid #e7e5e4; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
  .prose th { background: #fafaf9; color: #57534e; font-weight: 600; }
  .prose pre { background: #fff; border: 1px solid #e7e5e4; padding: 0.75rem 1rem; overflow-x: auto; font-size: 0.8rem; }
  .prose blockquote { margin: 0 0 0.75rem; padding: 0.6rem 0.9rem; background: #fff; border-left: 3px solid #a8a29e; font-size: 0.9rem; }
  .prose code { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.85em; background: #f5f5f4; padding: 1px 5px; border-radius: 3px; }
  .prose a { color: #1d4ed8; }
</style>"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&display=swap" rel="stylesheet">
{css}
{prose}
</head>
<body>
  <nav>{nav}</nav>
  <main>{body}</main>
  <footer>{footer}</footer>
</body>
</html>
"""



def _is_separator(line: str) -> bool:
    """A table's rule row: dashes and colons only, and at least one dash.

    An empty set is a subset of every set, so without the dash a blank line read as a rule and
    turned the line above it into a one-row table.
    """
    rule = line.replace("|", "").strip()
    return bool(rule) and "-" in rule and set(rule) <= set("-: ")


def _cells(line: str) -> list[str]:
    """One table row's cells.

    A pipe written `\\|` belongs to the cell, not between two of them, and a row that ends in a
    space after its last pipe has no cell after that pipe.
    """
    inner = re.sub(r"^\|+|\|+$", "", line.strip())
    return [c.replace("\\|", "|").strip() for c in re.split(r"(?<!\\)\|", inner)]


def _list_html(rows: list[tuple[int, bool, str, str]], inline) -> str:
    """The rows of a list, as `(indent, ordered, text, raw)`, nested by their indentation.

    A nested list is a list inside an item of the one above it, so it is written inside that
    item's `<li>`. Reading every row into one flat list would change what the document says.
    """
    if not rows:
        return ""
    tag = "ol" if rows[0][1] else "ul"
    # An ordered list that begins at 3 has to render 3, or the item the document calls the third
    # is printed as the first, which is the same failure as flattening it.
    start = ""
    if tag == "ol":
        first = re.match(r"\s*(\d+)\.", rows[0][3] if len(rows[0]) > 3 else "")
        start = f' start="{first.group(1)}"' if first and first.group(1) != "1" else ""
    out, k = [], 0
    while k < len(rows):
        indent, _, text = rows[k][0], rows[k][1], rows[k][2]
        k += 1
        deeper = k
        while deeper < len(rows) and rows[deeper][0] > indent:
            deeper += 1
        inside = _list_html(rows[k:deeper], inline) if deeper > k else ""
        out.append(f"<li>{inline(text)}{inside}</li>")
        k = deeper
    return f"<{tag}{start}>" + "".join(out) + f"</{tag}>"


# What a link in the framework document may point at. The document is the operator's own, so
# this is not a trust boundary, but a scheme the page cannot follow is a dead link at best.
_SCHEME = re.compile(r"^(?:https?:|mailto:|[^:]*$|[.#/?])", re.I)


def _link(href: str) -> str:
    """A link target, or "#" where the scheme is not one a page can follow."""
    href = href.strip()
    return E(href) if _SCHEME.match(href) else "#"


def md_to_html(text: str) -> str:
    """A small Markdown renderer for the framework page.

    It covers what the framework document uses: headings, paragraphs, bullet and numbered lists,
    tables, fenced code, block quotes, and inline code, bold, italic and links. It does not cover
    images, reference links, setext headings or horizontal rules: each of those is
    printed as the plain text it is written as, so a document that needs them needs a real
    renderer. A fenced block is
    printed as written, which is what a diagram in a fence needs here, since the page loads no
    script to draw one.
    """
    def inline(s: str) -> str:
        s = E(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", s)
        # `(?<!!)`: an image is written the same way with a `!` in front, and without this the
        # rule turned it into a link with a stray "!" beside it, which is not what the docstring
        # above promises and not a link a reader can use.
        s = re.sub(r"(?<!!)\[([^\]]+)\]\(([^)]+)\)",
                   lambda m: f'<a href="{_link(m.group(2))}">{m.group(1)}</a>', s)
        return s

    # An HTML comment is not content: the framework document carries its grounding quotes in one,
    # and escaping them printed the lot on the page.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out, lines, i = [], text.splitlines(), 0
    slugs: set[str] = set()
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block, i = [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            out.append("<pre><code>" + E("\n".join(block)) + "</code></pre>")
            i += 1  # step past the closing fence, or the rest reads as code
        elif line.startswith("|") and i + 1 < len(lines) and _is_separator(lines[i + 1]):
            head = _cells(line)
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_cells(lines[i])); i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) +
                       "</tr></thead><tbody>" +
                       "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows) +
                       "</tbody></table>")
            continue
        elif re.match(r"^#{1,6} ", line):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            # Two headings can slug to one name ("The chain" and "The Chain"), and a
            # heading with no letters or digits slugs to nothing. Both leave a link pointing at
            # whichever of them the browser happens to find first, so each slug is made unique.
            # The fallback is derived from the title, not from the heading's position: a counter
            # would move every later anchor as soon as a heading was inserted above it.
            folded = unicodedata.normalize("NFKD", title).lower()
            plain = "".join(c for c in folded if not unicodedata.combining(c))
            slug = re.sub(r"[^a-z0-9]+", "-", plain).strip("-")
            if not slug:
                slug = "h-" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
            if slug in slugs:
                slug = next(f"{slug}-{n}" for n in count(2) if f"{slug}-{n}" not in slugs)
            slugs.add(slug)
            out.append(f'<h{level} id="{slug}">{inline(title)}</h{level}>')
            i += 1
        elif re.match(r"^\s*[-*] ", line) or re.match(r"^\s*\d+\. ", line):
            # Indentation and kind both matter. Flattening a nested list changes what the document
            # says: three grounds with two examples under one of them becomes five grounds, and an
            # ordered list renumbers, so the item the reader is told is the second is the fourth.
            rows = []
            while i < len(lines) and (re.match(r"^\s*[-*] ", lines[i])
                                      or re.match(r"^\s*\d+\. ", lines[i])):
                raw = lines[i]
                rows.append((len(raw) - len(raw.lstrip()),
                             bool(re.match(r"^\s*\d+\. ", raw)),
                             re.sub(r"^\s*(?:[-*]|\d+\.) ", "", raw),
                             raw))
                i += 1
            out.append(_list_html(rows, inline))
            continue
        elif line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith("> "):
                quote.append(lines[i][2:]); i += 1
            out.append("<blockquote>" + inline(" ".join(quote)) + "</blockquote>")
            continue
        elif line.strip():
            para, start = [], i
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "```", "> ")) \
                    and not re.match(r"^\s*(?:[-*]|\d+\.) ", lines[i]):
                para.append(lines[i]); i += 1
            if i == start:
                # A line starting with '#' or '|' that no branch above claimed is prose: a hashtag,
                # more than six hashes, or a stray pipe. Taking it here is what advances `i`.
                para.append(lines[i]); i += 1
            out.append("<p>" + inline(" ".join(para)) + "</p>")
            continue
        else:
            i += 1
    return "\n".join(out)


def write_framework(site: Path, source: Path) -> str:
    """Publish the framework document as a page of the site, and return the link to it."""
    dest = site / "framework"
    dest.mkdir(parents=True, exist_ok=True)
    body = md_to_html(source.read_text(encoding="utf-8-sig"))
    page = PAGE.format(
        title="The framework: Claim-Evidence Alignment", css=CSS, prose=PROSE,
        nav='<span class="mark">CEA</span><a class="nav-home" href="../">&larr; All papers</a>',
        # No id here: the headings carry the anchors, and a document whose first heading is
        # "Framework" slugs to `framework` and would collide with a wrapper of that name.
        body=f'<section class="prose">{body}</section>',
        footer=f"Published from <code>{E(source.name)}</code> on {date.today().isoformat()}.")
    (dest / "index.html").write_text(page, encoding="utf-8")
    return "../../framework/"


def copy_paper(record: Path, site: Path, framework_href: str = "") -> dict:
    data = json.loads((record / "claims.json").read_text(encoding="utf-8-sig"))
    # A checker may point their editor at the schema. That path is theirs, not the reader's, so
    # it is dropped rather than published in the record and inside every page.
    data.pop("$schema", None)
    # The recorded path is the checker's. The reader wants the copy published beside the page.
    # Keep the path as recorded for the search below, and publish only its last segment.
    given = str(data["paper"]["pdf"])
    pdf_name = Path(given).name
    data["paper"]["pdf"] = pdf_name
    paper_id = data["paper"]["id"]
    dest = site / "papers" / paper_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "claims.json").write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
    # claims.md prints paper.pdf in its header, so it is re-rendered from the sanitised record
    # rather than copied. Otherwise the path the other two renderings drop reaches the site here.
    (dest / "claims.md").write_text(cea_claims.render(data), encoding="utf-8")
    # validate has already refused a record without it, so it is here. Resolved and checked all
    # the same: a link where text.txt should be publishes whatever it points at, and the page says
    # every quote was checked against that file.
    home = record.resolve()
    said = (record / "text.txt").resolve()
    if not said.is_relative_to(home):
        raise IsADirectoryError(f"{record / 'text.txt'} leads outside {record}; move the paper's "
                                "text into the record rather than linking to it")
    shutil.copy2(said, dest / "text.txt")
    candidates = [record / pdf_name, record / f'{data["paper"]["id"]}.pdf']
    # Only a relative path is followed: an absolute one would publish a file from anywhere on the
    # machine, and the reader needs the copy beside the page in any case.
    if not Path(given).is_absolute() and ".." not in Path(given).parts:
        candidates.append(record / given)
    for candidate in candidates:
        # Resolved, not as written: a symlink in the record directory pointing at a file outside
        # it would otherwise publish that file's contents, and so would a path reached through
        # one. Only a path that lands inside the record may be published beside the page. A hard
        # link is not a path, so it is not covered, and nothing path-based could cover it.
        try:
            target = candidate.resolve()
            # is_file() is what raises on an over-long name, which resolve() accepts
            if not target.is_file():
                continue
        except (OSError, ValueError):  # a NUL or an over-long name is not a file, it is a typo
            continue
        if target.is_relative_to(home):
            # The page links this file under the name the record gives. When those differ, the
            # reader opens something the record does not name, so say which file it was: two
            # extracts under one folded id leave two PDFs in a record directory.
            if candidate.name != pdf_name:
                print(f"CEA_WARNING: {record}: no {pdf_name} here, so {candidate.name} is "
                      f"published under that name; rename it or correct paper.pdf")
            shutil.copy2(target, dest / pdf_name)
            break
    else:
        print(f"CEA_WARNING: {record}: no PDF found for {pdf_name}, the page will name it without a link")
    # A main result that no claim serves is the finding a reader should not have to search
    # for, and the site skill says to report it. Nothing printed it: the record is valid, the page
    # carries the heading, and the fact stood only inside the generated files.
    served = {b for c in data["claims"] for b in c.get("serves", [])}
    for statement in data["main_results"]:
        if statement["id"] not in served:
            print(f"CEA_WARNING: {record}: no claim serves {statement['id']}, "
                  "so its page says the paper's own evidence does not reach that main result")
    out = dest / "index.html"
    out.write_text(cea_page.build(data, dest / "claims.json", out, index_href="../../",
                                  framework_href=framework_href, built_by="site skill"),
                   encoding="utf-8")
    return data


def write_index(site: Path, papers: list[dict], framework: bool = False) -> None:
    rows = []
    for d in papers:
        p = d["paper"]
        rows.append(
            f'<tr><td class="t"><a href="papers/{E(p["id"])}/">{E(_flat(p["title"]))}</a>'
            f'<div class="pid">{E(p["id"])}'
            f'{"" if d["main_results"] else " &middot; no main result recorded"}</div></td>'
            f'<td class="n">{len(d["main_results"])}</td><td class="n">{len(d["claims"])}</td>'
            f'<td class="n">{len(d["excluded"])}</td><td class="n">{_stated_in(d) if d["main_results"] else 0}</td>'
            f'<td class="n">{p["pages"]}</td></tr>')
    framework_link = ('<a class="nav-home" href="framework/">Framework</a>' if framework else "")
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claim-Evidence Alignment: the papers</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&display=swap" rel="stylesheet">
{CSS}
<style>
  .idx {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; background: #fff; }}
  .idx th, .idx td {{ padding: 0.6rem 0.8rem; border: 1px solid #e7e5e4; text-align: left; }}
  .idx th {{ background: #fafaf9; color: #57534e; font-weight: 600; font-size: 0.75rem; }}
  .idx td.n {{ text-align: right; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; white-space: nowrap; }}
  .idx td.t a {{ color: #1c1917; text-decoration: none; font-weight: 600; }}
  .idx td.t a:hover {{ text-decoration: underline; }}
  footer a {{ color: #1d4ed8; }}
  nav .nav-repo {{ margin-left: auto; color: #1d4ed8; border-bottom: none; white-space: nowrap; }}
  nav .nav-repo:hover {{ color: #1e40af; }}
  .idx .pid {{ font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.7rem; color: #a8a29e; }}
</style>
</head>
<body>
  <nav><span class="mark">CEA</span><a href="#papers" class="active">Papers</a>{framework_link}<a class="nav-repo" href="https://github.com/se-uhd/cea-skill">cea-skill on GitHub &nearr;</a></nav>
  <header>
    <h1>Claim-Evidence Alignment</h1>
    <p class="subtitle">Each paper's page records the claims its main results rest on, and the
    candidates that were considered and excluded. Claim-Evidence Alignment covers quantitative empirical
    claims, so where a paper states its main results as qualitative findings, only the candidates are recorded.</p>
  </header>
  <main>
    <section id="papers">
      <h2>Papers</h2>
      <table class="idx"><thead><tr><th>Paper</th><th>Main results</th><th>Claims</th>
      <th>Excluded claim candidates</th><th>Sentences the record marks as stating the main results</th><th>Pages</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table>
    </section>
  </main>
  <footer>The <a href="https://github.com/se-uhd/cea-skill">cea-skill</a> site skill built this page on
  {date.today().isoformat()} from the {len(papers)} paper{"s" if len(papers) != 1 else ""} listed above.</footer>
</body>
</html>
"""
    (site / "index.html").write_text(page, encoding="utf-8")


def _publish(item: Path, target: Path) -> None:
    """Copy one staged file or directory into the site, refusing to write outside it.

    Every destination is checked before it is written. A link anywhere under `site`, not only at
    its root, would otherwise take the page it stands for to wherever it points: `copytree` and
    `copy2` both follow one. A dangling link answers False to `exists()` and to `is_dir()`, so the
    kind is read with `is_symlink()` first.

    The copy merges rather than replaces, so a paper dropped from the list keeps its old directory.
    """
    if target.is_symlink():
        raise IsADirectoryError(f"{target} is a symbolic link; move it out of the way, because "
                                "publishing through it writes outside the site")
    if target.exists() and item.is_dir() != target.is_dir():
        kind = "a directory" if target.is_dir() else "a file"
        raise IsADirectoryError(f"{target} is already {kind}; move it out of the way")
    if not item.is_dir():
        shutil.copy2(item, target)
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(item.iterdir()):
        _publish(child, target / child.name)


def build_site(records: list[Path], site: Path, framework: Path | None = None) -> tuple[int, list[str]]:
    """Write the site, unless a record leaves a decision open.

    Every record is read and checked for the shape the pages need before anything is written, and
    the whole site is built in a staging directory before any of it is copied into `site`, so no
    record can leave a part-written site. The copy merges rather than replaces: a paper dropped
    from the list keeps its old directory, which the new index no longer lists. A failure during
    the copy can leave `site` partly updated and says so, and the staging copy is always removed.

    Every record is validated before anything is written, so a quote that is not on its page stops
    the build rather than reaching a reader. The shape checks here run first and cover what
    `validate` does not: they hold the record to what the pages need, field by field.

    Returns the number of papers written and the messages to print.
    """
    messages, blocked = [], False
    still_published: list[str] = []
    seen_ids: dict[str, tuple[Path, str]] = {}
    for record in records:
        try:
            data = json.loads((record / "claims.json").read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:  # ValueError covers UnicodeDecodeError, a bad
            # JSON document, and the 4300-digit integer-conversion limit
            messages.append(f"CEA_INVALID: {record}: claims.json cannot be read: {e}")
            blocked = True
            continue
        except RecursionError:  # nested arrays exhaust the stack before json gives up
            messages.append(f"CEA_INVALID: {record}: claims.json is nested too deeply to read")
            blocked = True
            continue
        if not isinstance(data, dict):
            messages.append(f"CEA_INVALID: {record}: claims.json must hold a JSON object; "
                            "run validate")
            blocked = True
            continue
        missing = [k for k in cea_claims.FIELDS if k not in data]
        paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
        # Every refusal from here on can name the page it leaves standing. Only the last two
        # branches did, so a build refused by any shape check left a stale page published and
        # said nothing at all about it. Named only where this record is the one refused: a page
        # whose own record is sound is still standing too, and rightly.
        named = paper.get("id")
        named = named if isinstance(named, str) and PAPER_ID.fullmatch(named) else None
        missing += [f"paper.{k}" for k in ("id", "title", "pdf", "pages") if k not in paper]
        if missing:
            messages.append(f"CEA_INVALID: {record}: claims.json is missing "
                            f"{', '.join(missing)}; run validate")
            blocked = True
            still_published.append(named)
            continue
        if isinstance(paper["pages"], bool) or not isinstance(paper["pages"], int):
            messages.append(f"CEA_INVALID: {record}: paper.pages must be a whole number; run validate")
            blocked = True
            still_published.append(named)
            continue
        wrong = [f for f in ("title", "pdf") if not isinstance(paper[f], str) or not paper[f].strip()]
        if wrong:
            messages.append(f"CEA_INVALID: {record}: paper.{', paper.'.join(wrong)} must be text "
                            "that is not empty; run validate")
            blocked = True
            still_published.append(named)
            continue
        # validate refuses a field the record format does not have. It runs below, after the
        # shape checks, so this one is caught here first and reported in the shape's words.
        extra = sorted(set(paper) - set().union(*cea_claims.FIELDS["paper"]))
        if extra:
            messages.append(f"CEA_INVALID: {record}: paper has unknown field(s) "
                            f"{', '.join(repr(f) for f in extra)}; run validate")
            blocked = True
            still_published.append(named)
            continue
        shapes = [f"{key}[{n}] is missing {f}"
                  for key in ("main_results", "claims", "excluded")
                  for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                  for f in sorted(cea_claims.FIELDS[key][0])
                  if not isinstance(e, dict) or f not in e]
        shapes += [f"{key} must be a list" for key in ("main_results", "claims", "excluded")
                   if not isinstance(data[key], list)]
        # Not just a string: the page drops a field whose text is empty, so an empty reason
        # publishes a card with no reason on it at all, and claims.md prints a bare heading.
        shapes += [f"{key}[{n}].{f} must be text that is not empty"
                   for key in ("main_results", "claims", "excluded")
                   for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                   if isinstance(e, dict)
                   for f in ("quote", "section", "source", "states", "selection_reason",
                             "reason", "note")
                   if f in e and not (isinstance(e[f], str) and e[f].strip())]
        prefix = {"main_results": "R", "claims": "C", "excluded": "E"}
        shapes += [f"{key}[{n}].id must be {prefix[key]} and a number"
                   for key in ("main_results", "claims", "excluded")
                   for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                   if not isinstance(e, dict) or not isinstance(e.get("id"), str)
                   or not re.fullmatch(rf"{prefix[key]}\d{{1,6}}", e["id"])]
        shapes += [f"excluded[{n}].states is missing, which an excluded part of a split needs"
                   for n, e in enumerate(data["excluded"] if isinstance(data["excluded"], list) else [])
                   if isinstance(e, dict) and e.get("split_from") and "states" not in e]
        shapes += [f"{key}[{n}].split_from must be a name or null"
                   for key in ("claims", "excluded")
                   for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                   if isinstance(e, dict) and "split_from" in e
                   and e["split_from"] is not None and not isinstance(e["split_from"], str)]
        entries = {key: [e for e in (data[key] if isinstance(data[key], list) else [])
                         if isinstance(e, dict)]
                   for key in ("main_results", "claims", "excluded")}
        known = {e["id"] for group in entries.values() for e in group
                 if isinstance(e.get("id"), str)}
        result_ids = {e["id"] for e in entries["main_results"] if isinstance(e.get("id"), str)}
        shapes += [f"{key}[{n}].{f} must be a list of ids"
                   for key in ("claims", "excluded")
                   for n, e in enumerate(entries[key])
                   for f in ("serves", "duplicate_of", "breaks_down")
                   if f in e and not (isinstance(e[f], list)
                                      and all(isinstance(r, str)
                                              and re.fullmatch(r"[RCE]\d{1,6}", r) for r in e[f]))]
        # The page turns these into links and map edges, so an id that names nothing, or names
        # the wrong kind of entry, publishes a page that contradicts its own record.
        shapes += [f"{key}[{n}].{f} names {r!r}, which is not a main result"
                   for key in ("claims", "excluded")
                   for n, e in enumerate(entries[key])
                   for f in ("serves", "breaks_down")
                   if isinstance(e.get(f), list)
                   for r in e[f] if isinstance(r, str) and r not in result_ids]
        shapes += [f"{key}[{n}].duplicate_of names {r!r}, which is not in this record"
                   for key in ("claims", "excluded")
                   for n, e in enumerate(entries[key])
                   if isinstance(e.get("duplicate_of"), list)
                   for r in e["duplicate_of"] if isinstance(r, str) and r not in known]
        seen_entry_ids: dict[str, str] = {}
        for key in ("main_results", "claims", "excluded"):
            for n, e in enumerate(entries[key]):
                eid = e.get("id")
                if isinstance(eid, str):
                    if eid in seen_entry_ids:
                        shapes.append(f"{key}[{n}].id {eid!r} is also "
                                      f"{seen_entry_ids[eid]}; the page would drop one")
                    seen_entry_ids[eid] = f"{key}[{n}].id"
        shapes += [f"{key}[{n}].split_from must be a split name such as S1"
                   for key in ("claims", "excluded")
                   for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                   if isinstance(e, dict) and e.get("split_from") is not None
                   and not (isinstance(e["split_from"], str)
                            # the same rule validate applies, so it cannot call clean what this
                            # refuses and then send the user to it
                            and re.fullmatch(r"S\d+", e["split_from"]))]
        shapes += [f"{key}[{n}].page must be a number or a range"
                   for key in ("main_results", "claims", "excluded")
                   for n, e in enumerate(data[key] if isinstance(data[key], list) else [])
                   if isinstance(e, dict) and "page" in e
                   and not (isinstance(e["page"], str)
                            and re.fullmatch(r"\d{1,6}(?:-\d{1,6})?", e["page"])
                            or (isinstance(e["page"], int) and not isinstance(e["page"], bool)))]
        if shapes:
            messages.append(f"CEA_INVALID: {record}: {'; '.join(shapes[:3])}; run validate")
            blocked = True
            still_published.append(named)
            continue
        given = str(paper.get("pdf", ""))
        if Path(given).is_absolute() or ".." in Path(given).parts:
            messages.append(f"CEA_INVALID: {record}: paper.pdf {given!r} must be a relative path "
                            "without '..'; run validate")
            blocked = True
            still_published.append(named)
            continue
        if any(ord(c) < 32 for c in given):
            messages.append(f"CEA_INVALID: {record}: paper.pdf must not hold a control character; "
                            "run validate")
            blocked = True
            still_published.append(named)
            continue
        if not Path(given).name.strip() or Path(given).name.casefold() in cea_claims.PUBLISHED_NAMES:
            messages.append(f"CEA_INVALID: {record}: paper.pdf {given!r} must name a file, and not "
                            "one the site publishes, or the copy would overwrite it; run validate")
            blocked = True
            still_published.append(named)
            continue
        paper_id = paper.get("id")
        if not isinstance(paper_id, str) or not PAPER_ID.fullmatch(paper_id):
            messages.append(f"CEA_INVALID: {record}: paper.id {paper_id!r} is not a plain name, "
                            "so it cannot name a directory in the site; run validate")
            blocked = True
            still_published.append(named)
            continue
        # Two ids differing only in case share one directory on a case-insensitive filesystem,
        # where the second record would overwrite the first and the build still report success.
        key = paper_id.casefold()
        if key in seen_ids:
            messages.append(f"CEA_INVALID: {record}: paper.id {paper_id!r} collides with "
                            f"{seen_ids[key][1]!r} in {seen_ids[key][0]}, and on a case-insensitive "
                            "filesystem one page would overwrite the other")
            blocked = True
            still_published.append(named)
            continue
        seen_ids[key] = (record, paper_id)
        # The site is the only thing a reader sees, and CEA rests on every quote standing on its
        # page. The shape checks above cannot tell: they never open text.txt. Without this a
        # record whose quotes are nowhere in the paper is published beside the text that refutes
        # it, which is what skills/site/SKILL.md promises does not happen.
        problems, checked = cea_claims.validate(record)
        if not problems:
            # Every check that is a warning rather than a refusal was invisible on the path that
            # publishes: a page could go out carrying a section naming the wrong part of the
            # paper, or a title the paper does not print, and the build said nothing.
            for warning in cea_claims.advisories(record, checked):
                messages.append(f"CEA_WARNING: {record}: {warning}")
        if problems:
            blocked = True
            still_published.append(named)
            for problem in problems[:3]:
                messages.append(f"CEA_INVALID: {record}: {problem}")
            if len(problems) > 3:
                messages.append(f"CEA_INVALID: {record}: and {len(problems) - 3} more; run "
                                "validate on that record to see them all")
            continue
        problems = unsettled(data)
        if problems:
            blocked = True
            still_published.append(named)
            for problem in problems:
                messages.append(f"CEA_UNRESOLVED: {record}: {problem}")
    if blocked:
        # Refusing to build leaves whatever was published last time in place. Where that is a page
        # for the record just refused, the site still shows a reader the claims the record no
        # longer supports, and "no site was written" reads as though nothing is wrong. `render`
        # deletes its own stale page. This cannot, because the index would then link to a page
        # that is not there, so it names the file instead.
        for paper_id in dict.fromkeys(p for p in still_published if p):
            page = site / "papers" / paper_id / "index.html"
            if page.is_file():
                messages.append(f"CEA_WARNING: {page} is still published and still shows the "
                                "claims of the record above, which this build refused. Settle the "
                                "record and build again, or take the page down by hand.")
        messages.append("CEA_FAILED: no site was written.")
        return 0, messages
    if site.exists() and not site.is_dir():
        messages.append(f"CEA_INVALID: {site} is not a directory, so the site cannot be written there")
        return 0, messages
    staging, publishing = None, False
    try:
        # Staged inside `site`, not beside it: only `site` itself has to be writable, and a deploy
        # path such as /var/www/html/cea sits under a directory that usually is not.
        site.mkdir(parents=True, exist_ok=True)
        # A build killed before its finally could run leaves staging inside the deployed tree.
        # Only sweep what is plainly abandoned: a concurrent build's staging is minutes old at
        # most, and deleting it would destroy a live build.
        stale = time.time() - 3600
        for old in site.glob(".cea-staging-*"):
            try:
                if old.stat().st_mtime < stale:
                    shutil.rmtree(old, ignore_errors=True)
            except OSError:
                pass
        staging = Path(tempfile.mkdtemp(prefix=".cea-staging-", dir=site))
        framework_href = write_framework(staging, framework) if framework else ""
        built = [copy_paper(record, staging, framework_href) for record in records]
        papers = sorted(built,
                        key=lambda d: re.sub(r"^\W+", "", _flat(d["paper"]["title"])).lower())
        write_index(staging, papers, bool(framework))
        publishing = True
        # The index goes last, so an interrupted publish leaves the old index over old pages
        # rather than a new index pointing at pages that are not there yet.
        for item in sorted(staging.iterdir(), key=lambda p: p.name == "index.html"):
            _publish(item, site / item.name)
        index = site / "index.html"
        if index.is_symlink() or not index.is_file():
            raise FileNotFoundError(f"{index} was not written as a file of the site")
        # A paper dropped from the list keeps its directory, and its page stays reachable at the
        # address a reader may have saved, while the new index no longer lists it. That is the
        # merge working as intended, and it is worth saying: a withdrawn or renamed paper stays
        # published until someone removes it.
        built = {d.name for d in (staging / "papers").iterdir()} if (staging / "papers").is_dir() else set()
        kept = sorted(d.name for d in (site / "papers").iterdir()
                      if d.is_dir() and d.name not in built) if (site / "papers").is_dir() else []
        if kept:
            messages.append(f"CEA_WARNING: {site}/papers still holds {', '.join(kept)}, which this "
                            "build did not write. The index no longer lists them and their pages "
                            "are still reachable; remove the directories to withdraw them.")
    except BaseException as e:
        if publishing:
            where, remedy = f"{site} may be partly updated", f"check that {site} is writable"
        elif staging is None:
            where, remedy = "nothing was written", f"check that {site} can be created and written"
        else:
            where = "nothing was written"
            remedy = "run validate on every record and build again"
        messages.append(f"CEA_FAILED: {where}: {type(e).__name__}: {e}; {remedy}")
        if not isinstance(e, Exception):  # KeyboardInterrupt and friends still stop the run
            # The caller never returns from here, so say it now: this is the one case where the
            # site really can be left half-updated. Cleanup is the finally's job, not this one's.
            print(messages[-1], file=sys.stderr)
            raise
        return 0, messages
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    return len(papers), messages
