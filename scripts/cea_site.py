#!/usr/bin/env python3
"""Assemble a site from a set of claim records, one directory per paper.

`cea_claims.py site` calls `build_site`. Each paper gets `<out>/papers/<paper_id>/` holding the
record, both renderings and the PDF, so the page's relative links work the same locally, on a web
server, and inside a zip of that one folder.
"""
from __future__ import annotations

import html
import json
import re
import shutil
from datetime import date
from pathlib import Path

import cea_page
from cea_claims import _flat, _stated_in, unsettled
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
  <nav id="main-nav">{nav}</nav>
  <main>{body}</main>
  <footer>{footer}</footer>
</body>
</html>
"""



def md_to_html(text: str) -> str:
    """A small Markdown renderer for the framework page.

    It covers what the framework document uses: headings, paragraphs, bullet and numbered lists,
    tables, fenced code, block quotes, and inline code, bold, italic and links. A fenced block is
    printed as written, which is what a diagram in a fence needs here, since the page loads no
    script to draw one.
    """
    def inline(s: str) -> str:
        s = E(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![*\w])\*([^*]+)\*(?![*\w])", r"<em>\1</em>", s)
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
        return s

    out, lines, i = [], text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block, i = [], i + 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i]); i += 1
            out.append("<pre><code>" + E("\n".join(block)) + "</code></pre>")
            i += 1  # step past the closing fence, or the rest reads as code
        elif line.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").strip()) <= set("-: "):
            head = [c.strip() for c in line.strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")]); i += 1
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) +
                       "</tr></thead><tbody>" +
                       "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows) +
                       "</tbody></table>")
            continue
        elif re.match(r"^#{1,6} ", line):
            level = len(line) - len(line.lstrip("#"))
            title = line[level:].strip()
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            out.append(f'<h{level} id="{slug}">{inline(title)}</h{level}>')
            i += 1
        elif re.match(r"^\s*[-*] ", line) or re.match(r"^\s*\d+\. ", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            items = []
            while i < len(lines) and (re.match(r"^\s*[-*] ", lines[i]) or re.match(r"^\s*\d+\. ", lines[i])):
                items.append(re.sub(r"^\s*(?:[-*]|\d+\.) ", "", lines[i])); i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{inline(x)}</li>" for x in items) + f"</{tag}>")
            continue
        elif line.startswith("> "):
            quote = []
            while i < len(lines) and lines[i].startswith("> "):
                quote.append(lines[i][2:]); i += 1
            out.append("<blockquote>" + inline(" ".join(quote)) + "</blockquote>")
            continue
        elif line.strip():
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "```", "> ")) \
                    and not re.match(r"^\s*(?:[-*]|\d+\.) ", lines[i]):
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
    body = md_to_html(source.read_text(encoding="utf-8"))
    page = PAGE.format(
        title="Claim-Evidence Alignment", css=CSS, prose=PROSE,
        nav='<span class="mark">CEA</span><a class="nav-home" href="../">&larr; All papers</a>',
        body=f'<section id="framework" class="prose">{body}</section>',
        footer=f"Published from <code>{E(source.name)}</code> on {date.today().isoformat()}.")
    (dest / "index.html").write_text(page, encoding="utf-8")
    return "../../framework/"


def copy_paper(record: Path, site: Path, framework_href: str = "") -> dict:
    data = json.loads((record / "claims.json").read_text(encoding="utf-8"))
    paper_id = data["paper"]["id"]
    dest = site / "papers" / paper_id
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("claims.json", "claims.md", "text.txt"):
        if (record / name).is_file():
            shutil.copy2(record / name, dest / name)
    pdf_name = str(data["paper"]["pdf"]).rsplit("/", 1)[-1]
    for base in (Path.cwd(), record, record.parent, record.parent.parent):
        candidate = base / str(data["paper"]["pdf"])
        if candidate.is_file():
            shutil.copy2(candidate, dest / pdf_name)
            break
    out = dest / "index.html"
    out.write_text(cea_page.build(data, dest / "claims.json", out, index_href="../../",
                                  framework_href=framework_href), encoding="utf-8")
    return data


def write_index(site: Path, papers: list[dict], framework: bool = False) -> None:
    rows = []
    for d in papers:
        p = d["paper"]
        rows.append(
            f'<tr><td class="t"><a href="papers/{E(p["id"])}/">{E(_flat(p["title"]))}</a>'
            f'<div class="pid">{E(p["id"])}'
            f'{"" if d["broad_statements"] else " &middot; no quantitative main result"}</div></td>'
            f'<td class="n">{len(d["broad_statements"])}</td><td class="n">{len(d["claims"])}</td>'
            f'<td class="n">{len(d["rejected"])}</td><td class="n">{_stated_in(d) if d["broad_statements"] else 0}</td>'
            f'<td class="n">{p["pages"]}</td></tr>')
    framework_link = ('<a class="nav-home" href="framework/">The framework</a>' if framework else "")
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claim-Evidence Alignment</title>
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
  .idx .pid {{ font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.7rem; color: #a8a29e; }}
</style>
</head>
<body>
  <nav id="main-nav"><span class="mark">CEA</span><a href="#papers" class="active">Papers</a>{framework_link}</nav>
  <header>
    <h1>Claim-Evidence Alignment</h1>
    <p class="subtitle">Each paper's page records the narrow claims its main results rest on, and the
    candidates that were considered and rejected. Claim-Evidence Alignment covers quantitative empirical
    claims, so where a paper states its main results as qualitative findings, only the candidates are recorded.</p>
  </header>
  <main>
    <section id="papers">
      <h2>Papers</h2>
      <table class="idx"><thead><tr><th>Paper</th><th>Main results</th><th>Narrow claims</th>
      <th>Rejected candidates</th><th>Sentences stating the main results</th><th>Pages</th></tr></thead>
      <tbody>{"".join(rows)}</tbody></table>
    </section>
  </main>
  <footer><a href="https://github.com/se-uhd/cea-skill"><code>cea_claims.py site</code></a> wrote this page on
  {date.today().isoformat()} from the {len(papers)} paper{"s" if len(papers) != 1 else ""} listed above.</footer>
</body>
</html>
"""
    (site / "index.html").write_text(page, encoding="utf-8")


def build_site(records: list[Path], site: Path, framework: Path | None = None) -> tuple[int, list[str]]:
    """Write the site, unless a record leaves a decision open.

    Every record is checked before anything is written, so a half-built site never reaches a
    server. Returns the number of papers written and the messages to print.
    """
    messages, blocked = [], False
    for record in records:
        data = json.loads((record / "claims.json").read_text(encoding="utf-8"))
        problems = unsettled(data)
        if problems:
            blocked = True
            for problem in problems:
                messages.append(f"CEA_UNRESOLVED: {record}: {problem}")
    if blocked:
        messages.append("CEA_FAILED: no site was written.")
        return 0, messages
    framework_href = write_framework(site, framework) if framework else ""
    papers = sorted((copy_paper(record, site, framework_href) for record in records),
                    key=lambda d: re.sub(r"^\W+", "", _flat(d["paper"]["title"])).lower())
    write_index(site, papers, bool(framework))
    return len(papers), messages
