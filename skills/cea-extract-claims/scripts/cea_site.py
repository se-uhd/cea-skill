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


def copy_paper(record: Path, site: Path) -> dict:
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
    out.write_text(cea_page.build(data, dest / "claims.json", out, index_href="../../"), encoding="utf-8")
    return data


def write_index(site: Path, papers: list[dict]) -> None:
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
  <nav id="main-nav"><span class="mark">CEA</span><a href="#papers" class="active">Papers</a></nav>
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
  {date.today().isoformat()} from the {len(papers)} paper{"s" if len(papers) != 1 else ""} listed above.
  The command is part of the <a href="https://github.com/se-uhd/cea-skill">cea-extract-claims</a> skill, which also writes each
  paper's page.</footer>
</body>
</html>
"""
    (site / "index.html").write_text(page, encoding="utf-8")


def build_site(records: list[Path], site: Path) -> tuple[int, list[str]]:
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
    papers = sorted((copy_paper(record, site) for record in records),
                    key=lambda d: re.sub(r"^\W+", "", _flat(d["paper"]["title"])).lower())
    write_index(site, papers)
    return len(papers), messages
