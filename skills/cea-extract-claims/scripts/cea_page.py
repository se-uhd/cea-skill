#!/usr/bin/env python3
"""Render a claim record as one HTML page.

A third output for cea-extract-claims, beside claims.json and claims.md. It follows the page
skeleton of codebook_visualizer.html and annotation_explorer.html: a sticky section nav, a serif
masthead, stat cards, an overview visual with nodes that jump to the detail cards below, and
accordion cards for the records.

It reuses the ordering of `cea_claims.render`, so the page and the Markdown cannot disagree about
which claims stand under which main result.

`cea_claims.py render` calls `build`. This module holds the page template and nothing else.
"""
from __future__ import annotations

import html
import json
import re
import sys
from datetime import date
from pathlib import Path

# cea_claims.py sits beside this file, and the page reuses its ordering so that the page and
# claims.md cannot disagree about which claims stand under which main result.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cea_claims import _by_weight, _first_page, _flat, _key, _one_result, _stated_in  # noqa: E402

E = html.escape
BID = re.compile(r"\bB\d+\b")

ANCHOR = ('<a class="section-anchor" href="#{id}" onclick="navigator.clipboard.writeText(this.href)" '
          'title="Copy link to section"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" '
          'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
          '<path d="M6.75 9.25a3.25 3.25 0 0 0 4.596.148l1.904-1.904a3.25 3.25 0 0 0-4.596-4.596L7.5 4.052"/>'
          '<path d="M9.25 6.75a3.25 3.25 0 0 0-4.596-.148L2.75 8.506a3.25 3.25 0 0 0 4.596 4.596L8.5 11.948"/>'
          '</svg></a>')

LINKS = ["interpretation", "operationalization", "measurement", "unit bridge", "analysis", "reasoning"]

REJ_GROUPS = [
    ("repeat", "Repeats a recorded statement"),
    ("breakdown", "Breaks a main result into parts"),
    ("part", "Another part of a split sentence"),
    ("standing", "Leaves its main result standing"),
    ("other", "Describes the study, or is out of scope"),
]


def refs(entry: dict, field: str) -> list[str]:
    value = entry.get(field) or []
    return value if isinstance(value, list) else [value]


def weighed_against(r: dict) -> set[str]:
    """The broad statements that a rejected candidate refers to.

    `duplicate_of` and `breaks_down` name them as data. A reason names one in its text, because the
    skill requires "B2 would still stand, because ...".
    """
    return set(refs(r, "duplicate_of")) | set(refs(r, "breaks_down")) | set(BID.findall(r.get("reason", "")))


def kind_of(r: dict) -> str:
    if refs(r, "duplicate_of"):
        return "repeat"
    if refs(r, "breaks_down"):
        return "breakdown"
    if r.get("split_from"):
        return "part"
    if BID.findall(r.get("reason", "")):
        return "standing"
    return "other"


def badge(i: str) -> str:
    return f'<a class="idbadge" href="#{E(i)}" data-jump="{E(i)}">{E(i)}</a>'


def badges(ids) -> str:
    return " ".join(badge(i) for i in ids)


def field(title: str, body: str) -> str:
    return f'<div class="field"><div class="field-title">{E(title)}</div>{body}</div>' if body else ""


def para(text) -> str:
    return f"<p>{E(_flat(text))}</p>" if text else ""


def quote_of(entry: dict) -> str:
    return f'<blockquote class="paper-quote">{E(_flat(entry["quote"]))}</blockquote>'


def snippet(text: str, n: int = 130) -> str:
    flat = _flat(text)
    return E(flat if len(flat) <= n else flat[: n - 1].rsplit(" ", 1)[0] + "…")


def entry_card(kind: str, eid: str, loc: str, preview: str, tags: str, body: str) -> str:
    return (f'<article class="entry {kind}" id="{E(eid)}">'
            f'<div class="entry-header" role="button" tabindex="0" aria-expanded="false">'
            f'<div class="entry-info"><h3><span class="idtag">{E(eid)}</span>'
            f'<span class="loc">{loc}</span></h3><p class="preview">{preview}</p>'
            f'<div class="entry-tags">{tags}</div></div>'
            f'<span class="expand-icon">▾</span></div>'
            f'<div class="entry-body">{body}</div></article>')


def chain_track() -> str:
    dots = "".join(f'<i class="dot" title="link {i}: {t}"></i>' for i, t in enumerate(LINKS, 1))
    return ('<div class="chain" title="mapping level M1: ' + ", ".join(LINKS) +
            ' not reconstructed"><span class="lvl">M1</span>' + dots + '</div>')


def near(name: str, source: Path, out: Path) -> str:
    """The relative path from the page to a file of the record, when it is close enough to stay valid."""
    import os
    for candidate in (out.resolve().parent / name, source.parent / name):
        if candidate.resolve().is_file():
            rel = os.path.relpath(candidate.resolve(), out.resolve().parent)
            if rel.count("..") <= 2:
                return rel
    return ""


def source_links(paper: dict, source: Path, out: Path) -> str:
    """The files this page was built from, linked where they sit beside it."""
    items = []
    for name, label in ((source.name, "what this page is built from"),
                        ("claims.md", "the same content as Markdown"),
                        ("text.txt", "the page text that every quote was checked against")):
        rel = near(name, source, out)
        if rel:
            items.append(f'<a href="{E(rel)}">{E(name)}</a> <span class="dim">{E(label)}</span>')
    return " &middot; ".join(items)


def pdf_link(paper: dict, source: Path, out: Path) -> str:
    """The header link to the paper.

    The PDF published beside the page comes first, because that is the copy a reader of the page can open.
    Then the copy beside the record, then `paper.pdf` as the user typed it, which is relative to
    wherever the command ran. With no file to link, `paper.doi` or `paper.url` names the published
    paper, and failing both the recorded path stands as text.
    """
    import os
    name = str(paper["pdf"]).rsplit("/", 1)[-1]
    tried = [out.resolve().parent / name, source.parent / name]
    tried += [(base / str(paper["pdf"])) for base in (Path.cwd(), source.parent, source.parent.parent)]
    for candidate in tried:
        candidate = candidate.resolve()
        if not candidate.is_file():
            continue
        rel = os.path.relpath(candidate, out.resolve().parent)
        if rel.count("..") > 2:  # a path that climbs that far will not survive being moved
            continue
        return f'<a class="nav-ext" href="{E(rel)}">{E(name)} &rarr;</a>'
    if paper.get("doi"):
        return f'<a class="nav-ext" href="https://doi.org/{E(paper["doi"])}">{E(paper["doi"])} &rarr;</a>'
    if paper.get("url"):
        return f'<a class="nav-ext" href="{E(paper["url"])}">{E(name)} &rarr;</a>'
    return f'<span class="nav-ext plain" title="{E(str(paper["pdf"]))}">{E(name)}</span>'


def build(data: dict, source: Path, out: Path) -> str:
    paper = data["paper"]
    broad, claims, rejected = data["broad_statements"], data["claims"], data["rejected"]
    by_id = {b["id"]: b for b in broad}
    order = {b["id"]: n for n, b in enumerate(_by_weight(data))}
    groups = sorted(_one_result(data), key=lambda g: min(order[b] for b in g))
    serving = {b["id"]: [c["id"] for c in claims if b["id"] in c["serves"]] for b in broad}
    considered = {b["id"]: [r["id"] for r in rejected if b["id"] in weighed_against(r)] for b in broad}
    splits: dict[str, list[str]] = {}
    for x in claims + rejected:
        if x.get("split_from"):
            splits.setdefault(x["split_from"], []).append(x["id"])
    repeats: dict[str, list[str]] = {}
    breakdowns: dict[str, list[str]] = {}
    for r in rejected:
        for t in refs(r, "duplicate_of"):
            repeats.setdefault(t, []).append(r["id"])
        for t in refs(r, "breaks_down"):
            breakdowns.setdefault(t, []).append(r["id"])

    home: dict[str, int] = {}
    for n, group in enumerate(groups):
        for c in sorted((c for c in claims if set(c["serves"]) & set(group)), key=_first_page):
            home.setdefault(c["id"], n)

    ticks: list[tuple[int, str, str, str]] = []
    result_cards = []

    # the map: compact nodes, one row per main result
    rows = []
    for n, group in enumerate(groups):
        lead = min(group, key=lambda b: order[b])
        b = by_id[lead]
        mine = [c for c in claims if home.get(c["id"]) == n]
        rej = sorted({x for g in group for x in considered[g]}, key=lambda i: int(i[1:]))
        nodes = "".join(
            f'<button class="node claim" data-id="{E(c["id"])}" data-serves="{E(" ".join(c["serves"]))}" '
            f'title="{snippet(c["states"], 300)}"><span class="node-id">{E(c["id"])}</span>'
            f'<span class="node-text">{snippet(c["states"], 110)}</span>'
            f'<span class="node-meta">p. {E(str(c["page"]))}'
            f'{" &middot; note" if c.get("note") else ""}</span></button>' for c in mine)
        if not mine:
            nodes = ('<p class="node-empty">No narrow claim serves this result. '
                     f'{E(_flat(b.get("note") or ""))}</p>')
        if rej:
            nodes += (f'<button class="node candidates" data-reveal="{E(rej[0])}">'
                      f'{len(rej)} rejected candidate{"s" if len(rej) != 1 else ""} &rarr;</button>')
        also = f' <span class="node-also">also stated as {", ".join(also_id for also_id in group if also_id != lead)}</span>' if len(group) > 1 else ""
        rows.append(
            f'<div class="map-row">'
            f'<button class="node result{" empty" if not mine else ""}" data-id="{E(lead)}" '
            f'title="{snippet(b["quote"], 300)}"><span class="node-id">{E(lead)}</span>'
            f'<span class="node-text">{snippet(b["quote"], 150)}</span>'
            f'<span class="node-meta">{E(b["source"])} &middot; p. {E(str(b["page"]))} &middot; '
            f'stated in {_stated_in(data, group)} sentence{"s" if _stated_in(data, group) > 1 else ""}'
            f'{also}</span></button>'
            f'<div class="node-stack">{nodes}</div></div>')
        ticks.append((_first_page(b), "result", lead, _flat(b["section"])))

        tags = ('<span class="taglabel">served by</span>' + badges(serving[lead])
                if serving[lead] else '<span class="minitag">no claim selected</span>')
        if b.get("note"):
            tags += ' <span class="minitag">note</span>'
        body = "".join([
            field("quote", quote_of(b)),
            field("states", para(b["states"]))
            if b.get("states") and _key(b["states"]) != _key(b["quote"]) else "",
            field("claims that serve it", badges(serving[lead]) or '<p class="dim">none</p>'),
            field("rejected candidates that repeat it", badges(repeats.get(lead, []))),
            field("rejected candidates that break it into parts", badges(breakdowns.get(lead, []))),
            field("rejected candidates that leave it standing",
                  badges([r for r in considered[lead]
                          if r not in repeats.get(lead, []) and r not in breakdowns.get(lead, [])])),
            field("note", para(b["note"]) if b.get("note") else ""),
        ])
        result_cards.append(entry_card(
            "result", lead, f'{E(b["source"])} &middot; p. {E(str(b["page"]))} &middot; '
            f'{E(_flat(b["section"]))}', snippet(b["quote"], 220), tags, body))

    # the claim cards
    claim_cards = []
    for c in sorted(claims, key=_first_page):
        sibs = [s for s in splits.get(c.get("split_from") or "", []) if s != c["id"]]
        note = _flat(c["note"]) if c.get("note") else ""
        tags = '<span class="taglabel">serves</span>' + badges(c["serves"])
        if note:
            tags += ' <span class="minitag">note</span>'
        if sibs:
            tags += ' <span class="minitag">split</span>'
        body = "".join([
            field("quote", quote_of(c)),
            field("states", para(c["states"])) if _key(c["states"]) != _key(c["quote"]) else "",
            field("serves", badges(c["serves"])),
            field("selection_reason", para(c["selection_reason"])),
            field("split_from", badges(sibs) + f' <span class="dim">split {E(c["split_from"])}</span>') if sibs else "",
            field("note", para(note)),
            chain_track(),
        ])
        claim_cards.append(entry_card(
            "claim", c["id"], f'p. {E(str(c["page"]))} &middot; {E(_flat(c["section"]))}',
            snippet(c["states"], 220), tags, body))
        ticks.append((_first_page(c), "claim", c["id"], _flat(c["section"])))

    # the rejected candidates, folded
    buckets: dict[str, list[dict]] = {k: [] for k, _ in REJ_GROUPS}
    for r in sorted(rejected, key=_first_page):
        buckets[kind_of(r)].append(r)
        ticks.append((_first_page(r), "candidate", r["id"], _flat(r["section"])))
    cand_html = []
    for key, heading in REJ_GROUPS:
        items = buckets[key]
        if not items:
            continue
        cards = []
        for r in items:
            note = _flat(r["note"]) if r.get("note") else ""
            label, ids = {
                "repeat": ("repeats", refs(r, "duplicate_of")),
                "breakdown": ("breaks down", refs(r, "breaks_down")),
                "standing": ("leaves standing", sorted(weighed_against(r))),
                "part": ("split from", [r.get("split_from") or ""]),
            }.get(kind_of(r), ("", []))
            ids = [i for i in ids if i]
            tags = (f'<span class="taglabel">{label}</span>' +
                    (badges(ids) if kind_of(r) != "part" else E(ids[0]))) if ids else ""
            if note:
                tags += ' <span class="minitag">note</span>'
            body = "".join([
                field("quote", quote_of(r)),
                field("reason", para(r["reason"])),
                field("duplicate_of", badges(refs(r, "duplicate_of"))),
                field("breaks_down", badges(refs(r, "breaks_down"))),
                field("states",
                      para(r["states"]) + f'<p class="dim">split {E(r["split_from"])}</p>')
                if r.get("split_from") and r.get("states") else "",
                field("note", para(note)),
            ])
            cards.append(entry_card("candidate", r["id"],
                                    f'p. {E(str(r["page"]))} &middot; {E(_flat(r["section"]))}',
                                    snippet(r["reason"], 200), tags, body))
        cand_html.append(f'<div class="cand-group" data-group="{key}">'
                         f'<h3 class="group-head">{E(heading)} <span class="count">{len(items)}</span></h3>'
                         f'<div class="cards-grid">{"".join(cards)}</div></div>')

    # where the record sits: the sections of the paper it comes from
    pages = int(paper["pages"])
    rank = {"result": 0, "claim": 1, "candidate": 2}
    sections: dict[str, list[tuple[int, str, str]]] = {}
    for page, k, i, sec in ticks:
        sections.setdefault(sec, []).append((page, k, i))
    rows_sec = []
    for sec, items in sorted(sections.items(), key=lambda kv: (min(p for p, _, _ in kv[1]), kv[0])):
        lo, hi = min(p for p, _, _ in items), max(p for p, _, _ in items)
        span = f"{lo}" if lo == hi else f"{lo}\u2013{hi}"
        marks = "".join(f'<button class="mk {k}" data-jump="{E(i)}" aria-label="{E(i)}, page {pg}" '
                        f'title="{E(i)}, page {pg}"></button>'
                        for pg, k, i in sorted(items, key=lambda m: (m[0], rank[m[1]], m[2])))
        counts = {k: sum(1 for _, kk, _ in items if kk == k) for k in rank}
        tally = ", ".join(f"{n} {name}" for name, n in
                          (("main result", counts["result"]), ("claim", counts["claim"]),
                           ("candidate", counts["candidate"])) if n)
        rows_sec.append(f'<tr><td class="sec">{E(sec)}</td><td class="pp">{span}</td>'
                        f'<td class="mm" title="{E(tally)}"><div class="marks">{marks}</div></td></tr>')
    strip = ('<table class="sectable"><thead><tr><th>Section as printed</th><th>Page</th>'
             '<th>What is recorded there</th></tr></thead><tbody>'
             + "".join(rows_sec) + "</tbody></table>")

    # the overview
    places = _stated_in(data) if broad else 0
    stats = [(len(broad), "main results"), (len(claims), "narrow claims"),
             (len(rejected), "rejected candidates"), (places, "sentences stating the main results")]
    stat_html = "".join(f'<div class="stat-card"><div class="stat-value">{v}</div>'
                        f'<div class="stat-label">{E(l)}</div></div>' for v, l in stats)

    unserved = [b for b in broad if not serving[b["id"]]]
    watch_html = ""
    if unserved:
        items = "".join(
            f'<li>{badge(b["id"])} <span class="question">{E(snippet(b["quote"], 200))}</span>'
            f'<p class="side">{E(_flat(b.get("note") or ""))}</p></li>' for b in unserved)
        watch_html = (f'<div class="note-card"><h3>Main result{"s" if len(unserved) != 1 else ""} that no '
                      f'narrow claim serves</h3><ul>{items}</ul></div>')
    if not broad:
        watch_html = ('<div class="note-card scope"><h3>Scope</h3><p>This paper states no quantitative '
                      'main result, so no main result and no narrow claim is recorded for it. '
                      'Claim&ndash;Evidence Alignment covers quantitative empirical claims, and a '
                      'qualitative finding is out of its scope, not absent from the paper. The '
                      'candidates considered, and the reason each one is not a narrow claim, are '
                      'below.</p></div>') + watch_html

    fields = {
        "TITLE": E(_flat(paper["title"])),
        "PAPER_ID": E(paper["id"]),
        "PAGES": str(pages),
        "PDF": E(str(paper["pdf"])),
        "PDF_LINK": pdf_link(paper, source, out),
        "SOURCE_LINKS": (f'<p class="files">{links}</p>' if (links := source_links(paper, source, out)) else ""),
        "STATS": stat_html,
        "WATCH": watch_html,
        "STRIP": strip,
        "RESULTS": "".join(result_cards) or
                   '<p class="node-empty">The paper states no main result that a narrow claim could serve.</p>',
        "MAP": "".join(rows) or '<p class="node-empty">The record has no main result and no narrow claim.</p>',
        "CLAIMS": "".join(claim_cards) or
                  '<p class="node-empty">No narrow claim was selected. The candidates are below.</p>',
        "CANDIDATES": "".join(cand_html) or '<p class="node-empty">No candidate was recorded.</p>',
        "N_CANDIDATES": str(len(rejected)),
        "SOURCE": E(source.name),
        "BUILT": date.today().isoformat(),
        "DATA": json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"),
        "ANCHOR_OVERVIEW": ANCHOR.format(id="overview"),
        "ANCHOR_MAP": ANCHOR.format(id="map"),
        "ANCHOR_RESULTS": ANCHOR.format(id="results"),
        "ANCHOR_CLAIMS": ANCHOR.format(id="claims"),
        "ANCHOR_CANDIDATES": ANCHOR.format(id="candidates"),
    }
    out = TEMPLATE
    for key, value in fields.items():
        out = out.replace("@@" + key + "@@", value)
    return out


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Claims: @@TITLE@@</title>
  <link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600;8..60,700&display=swap" rel="stylesheet">
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; background: #fafaf9; color: #292524; line-height: 1.6; }

    nav { position: sticky; top: 0; z-index: 100; background: #fff; border-bottom: 1px solid #e7e5e4; padding: 0.75rem 2rem; display: flex; gap: 1.5rem; align-items: center; flex-wrap: wrap; }
    nav .mark { font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace; font-size: 0.8rem; color: #1c1917; font-weight: 600; margin-right: 1.5rem; }
    nav a { text-decoration: none; color: #78716c; font-size: 0.875rem; font-weight: 500; padding: 0.25rem 0; border-bottom: 2px solid transparent; transition: all 0.15s; }
    nav a.active, nav a:hover { color: #1c1917; border-bottom-color: currentColor; }
    nav .nav-ext { margin-left: auto; color: #1d4ed8; border-bottom: none; }
    nav .nav-ext:hover { color: #1e40af; }
    nav .nav-ext.plain { color: #a8a29e; font-size: 0.8rem; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }

    header { text-align: left; padding: 2.5rem 2rem 1.5rem; max-width: 1260px; margin: 0 auto; }
    header h1 { font-family: 'Source Serif 4', Georgia, serif; font-size: 2.0rem; color: #1c1917; margin-bottom: 0.25rem; line-height: 1.25; }
    header .subtitle { color: #78716c; font-size: 0.875rem; }
    header .subtitle code { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.8rem; background: #f5f5f4; padding: 1px 5px; border-radius: 3px; }

    section { max-width: 1260px; margin: 0 auto; padding: 0 2rem 2rem; scroll-margin-top: 4rem; }
    section + section { padding-top: 1.5rem; }
    section h2 { font-family: 'Source Serif 4', Georgia, serif; font-size: 1.2rem; color: #1c1917; margin-bottom: 0.5rem; padding-bottom: 0.5rem; border-bottom: 2px solid #e7e5e4; }
    .section-anchor { color: #a8a29e; opacity: 0; transition: opacity 0.2s; margin-left: 0.4rem; display: inline-flex; align-items: center; vertical-align: middle; }
    h2:hover .section-anchor { opacity: 1; }
    .section-anchor:hover { color: #57534e; }
    .section-desc { font-size: 0.8rem; color: #78716c; margin-bottom: 1.25rem; }

    /* -- Overview -- */
    .stats-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 1rem; margin-bottom: 1.5rem; }
    .stat-card { background: #fff; padding: 1rem; border: 1px solid #e7e5e4; text-align: center; }
    .stat-value { font-size: 1.75rem; font-weight: 700; color: #1c1917; }
    .stat-label { font-size: 0.8rem; color: #78716c; margin-top: 0.15rem; }
    .note-card { background: #fff; padding: 1.25rem; border: 1px solid #e7e5e4; border-left: 2px solid #BB5566; margin-bottom: 1.5rem; }
    .note-card h3 { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #78716c; margin-bottom: 0.5rem; font-weight: 600; }
    .note-card ul { list-style: none; font-size: 0.875rem; }
    .note-card p { font-size: 0.875rem; }
    .note-card .question { font-weight: 600; }
    .note-card .side { font-size: 0.85rem; color: #57534e; margin: 0.3rem 0 0 2.2rem; }
    .note-card li + li { border-top: 1px solid #f5f5f4; padding-top: 0.5rem; margin-top: 0.5rem; }
    .note-card.scope { border-left-color: #004488; }
    .note-card li + li { border-top: 1px solid #f5f5f4; padding-top: 0.5rem; }
    .note-card li + li { margin-top: 0.35rem; }

    .panel { background: #fff; border: 1px solid #e7e5e4; padding: 1.25rem; }
    .panel h3 { font-size: 1.0rem; color: #1c1917; margin-bottom: 0.25rem; }
    .panel .panel-desc { font-size: 0.8rem; color: #78716c; margin-bottom: 1rem; }
    .strip { overflow-x: auto; }
    .sectable { border-collapse: collapse; font-size: 0.8rem; width: 100%; }
    .sectable th, .sectable td { padding: 4px 8px; border: 1px solid #e7e5e4; text-align: left; vertical-align: middle; }
    .sectable th { background: #fafaf9; color: #57534e; font-weight: 600; font-size: 0.75rem; }
    .sectable td.sec { color: #292524; }
    .sectable td.pp { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; color: #78716c; white-space: nowrap; width: 1%; }
    .sectable td.mm .marks { display: flex; flex-wrap: wrap; gap: 3px; align-items: center; }
    .mk { border: 0; padding: 0; background: none; cursor: pointer; display: block; width: 9px; height: 9px; }
    .mk.result { background: #1c1917; }
    .mk.claim { background: #004488; border-radius: 50%; }
    .mk.candidate { width: 7px; height: 7px; border: 1.5px solid #78716c; border-radius: 50%; }
    .mk:hover { outline: 2px solid #1d4ed8; outline-offset: 1px; }

    .legend.in-panel { margin: 0 0 0.75rem; padding: 0.5rem 0.75rem; background: #fafaf9; }
    .legend { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 1.25rem; padding: 0.75rem 1rem; background: #fff; border: 1px solid #e7e5e4; }
    .legend-item { display: flex; align-items: center; gap: 0.4rem; font-size: 0.8rem; color: #57534e; }
    .legend-dot { width: 12px; height: 12px; display: inline-block; flex-shrink: 0; }
    .legend-dot.result { background: #1c1917; }
    .legend-dot.claim { background: #004488; border-radius: 50%; }
    .legend-dot.candidate { width: 10px; height: 10px; border: 2px solid #78716c; border-radius: 50%; }

    /* -- Claim map -- */
    .map { position: relative; background: #fff; border: 1px solid #e7e5e4; padding: 1.25rem; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.05fr); gap: 0.75rem 3rem; align-items: start; }
    .map-head { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: #a8a29e; font-weight: 600; }
    .map-row { display: contents; }
    .map-edges { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; overflow: visible; }
    .node-stack { display: flex; flex-direction: column; gap: 0.4rem; min-width: 0; }
    .node { position: relative; z-index: 1; display: block; width: 100%; text-align: left; font-family: inherit; background: #fafaf9; border: 1px solid #e7e5e4; border-left: 2px solid #a8a29e; padding: 0.55rem 0.7rem; cursor: pointer; transition: background 0.15s; }
    .node:hover, .node:focus-visible { background: #f5f5f4; }
    .node.result { border-left-color: #1c1917; }
    .node.claim { border-left-color: #004488; }
    .node.result.empty { border-left-color: #BB5566; }
    .node-id { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.7rem; color: #57534e; background: #e7e5e4; padding: 1px 5px; border-radius: 3px; margin-right: 0.4rem; }
    .node-text { font-size: 0.8rem; color: #292524; }
    .node-meta { display: block; font-size: 0.7rem; color: #a8a29e; margin-top: 0.2rem; }
    .node-also { color: #78716c; }
    .node.candidates { background: #fff; border-left-color: #78716c; border-style: dashed; font-size: 0.75rem; color: #57534e; }
    .node-empty { font-size: 0.8rem; color: #a8a29e; font-style: italic; padding: 0.4rem 0; }
    .node.dim { opacity: 0.35; }

    .tooltip { position: fixed; background: #fff; border: 1px solid #e7e5e4; padding: 0.75rem 1rem; box-shadow: 0 4px 16px rgba(0,0,0,0.1); pointer-events: none; z-index: 200; max-width: 380px; display: none; font-size: 0.85rem; }
    .tooltip .abbr { color: #78716c; font-size: 0.7rem; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }
    .tooltip p { margin-top: 0.4rem; color: #57534e; font-family: 'Source Serif 4', Georgia, serif; }

    /* -- Filter bar -- */
    .filter-bar { display: flex; gap: 0.75rem; align-items: center; flex-wrap: wrap; margin-bottom: 1.25rem; padding: 0.75rem 0; border-bottom: 1px solid #e7e5e4; }
    .filter-bar input[type="search"] { flex: 1; min-width: 200px; padding: 0.35rem 0; border: none; border-bottom: 1px solid #e7e5e4; border-radius: 0; font-size: 0.8rem; font-family: inherit; background: none; outline: none; }
    .filter-bar input[type="search"]:focus { border-color: #78716c; box-shadow: 0 1px 0 0 #78716c; }
    .chip { padding: 2px 8px; border-radius: 3px; font-size: 0.7rem; font-family: inherit; cursor: pointer; border: 1px solid #d6d3d1; background: #fff; color: #57534e; }
    .chip.active { background: #292524; color: #fafaf9; border-color: #292524; }
    .filter-count { font-size: 0.7rem; color: #a8a29e; margin-left: auto; }

    /* -- Entry cards -- */
    .cards-grid { display: grid; grid-template-columns: 1fr; gap: 0.5rem; }
    .entry { background: #fff; border: 1px solid #e7e5e4; border-left: 2px solid #a8a29e; overflow: hidden; scroll-margin-top: 4rem; }
    .entry.result { border-left-color: #1c1917; }
    .entry.claim { border-left-color: #004488; }
    .entry.candidate { border-left-color: #78716c; }
    .entry.hide { display: none; }
    .entry-header { padding: 0.75rem 1.25rem; cursor: pointer; display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
    .entry-header:hover { background: #fafaf9; }
    .entry-info { min-width: 0; flex: 1 1 auto; }
    .entry-info h3 { font-size: 0.9rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; font-weight: 600; }
    .idtag { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.7rem; color: #57534e; background: #f5f5f4; padding: 1px 6px; border-radius: 4px; }
    .loc { font-size: 0.75rem; color: #78716c; font-weight: 400; }
    .preview { font-size: 0.8rem; color: #57534e; margin-top: 0.2rem; }
    .entry-tags { display: flex; gap: 0.3rem; align-items: center; flex-wrap: wrap; margin-top: 0.45rem; }
    .taglabel { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; color: #a8a29e; font-weight: 600; margin-right: 0.15rem; }
    .idbadge { display: inline-block; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.7rem; background: #e7e5e4; color: #57534e; padding: 1px 5px; border-radius: 3px; text-decoration: none; transition: background 0.15s; }
    .idbadge:hover { background: #d6d3d1; }
    .minitag { font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.05em; color: #a8a29e; border: 1px solid #e7e5e4; padding: 0 4px; border-radius: 3px; }
    .expand-icon { font-size: 0.7rem; color: #a8a29e; flex-shrink: 0; transition: transform 0.2s; }
    .entry.expanded .expand-icon { transform: rotate(180deg); }
    .entry-body { display: none; padding: 0 1.25rem 1.25rem; border-top: 1px solid #e7e5e4; }
    .entry.expanded .entry-body { display: block; }
    .field { margin-top: 1rem; }
    .field-title { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; color: #a8a29e; margin-bottom: 0.4rem; font-weight: 600; }
    .field p { font-size: 0.875rem; color: #44403c; text-align: justify; }
    .field p.dim { color: #a8a29e; font-size: 0.75rem; }
    .paper-quote { font-family: 'Source Serif 4', Georgia, serif; font-size: 0.95rem; line-height: 1.55; margin: 0; padding: 0.6rem 0.9rem; background: #fafaf9; border-left: 3px solid #a8a29e; }
    .chain { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px dashed #e7e5e4; display: flex; align-items: center; gap: 0.35rem; }
    .lvl { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.65rem; color: #57534e; background: #f5f5f4; padding: 2px 5px; border-radius: 3px; }
    .dot { width: 8px; height: 8px; border: 1.5px solid #d6d3d1; border-radius: 50%; display: inline-block; }
    .chain-note { font-size: 0.7rem; color: #a8a29e; margin-left: 0.4rem; }

    .group-head { font-size: 0.95rem; color: #1c1917; margin: 1.5rem 0 0.15rem; font-weight: 600; }
    .group-head .count { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.75rem; color: #a8a29e; }
    .group-desc { font-size: 0.78rem; color: #78716c; margin-bottom: 0.6rem; }
    .cand-group.hide { display: none; }
    .reveal { padding: 0.45rem 0.9rem; font-size: 0.8rem; font-family: inherit; border: 1px solid #d6d3d1; background: #fff; color: #292524; cursor: pointer; }
    .reveal:hover { background: #f5f5f4; }
    #candidates-body { display: none; }
    #candidates.shown #candidates-body { display: block; }
    #candidates.shown .reveal { margin-bottom: 1rem; }

    .procedure { font-size: 0.85rem; line-height: 1.7; color: #44403c; text-align: justify; max-width: 62rem; }
    .procedure h3 { font-size: 0.95rem; color: #1c1917; margin: 1.75rem 0 0.5rem; font-weight: 600; }
    .procedure h3:first-child { margin-top: 0; }
    .procedure p { margin-bottom: 0.75rem; }
    .procedure code { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; font-size: 0.8rem; background: #f5f5f4; padding: 0.1em 0.35em; border-radius: 3px; }

    footer { max-width: 1200px; margin: 2rem auto 0; padding: 1.5rem 2rem; border-top: 1px solid #e7e5e4; font-size: 0.7rem; color: #a8a29e; }
    footer code { font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }
    footer .files { margin-top: 0.5rem; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }
    footer .files a { color: #1d4ed8; }
    footer .files .dim { font-family: var(--sans, inherit); color: #a8a29e; }

    .highlight-flash { animation: flash 1.2s ease; }
    @keyframes flash { 0%, 100% { box-shadow: 0 0 0 0 rgba(0,68,136,0); } 30% { box-shadow: 0 0 0 4px rgba(0,68,136,0.35); } }

    @media (max-width: 900px) {
      .stats-grid { grid-template-columns: repeat(2, 1fr); }
      .map { grid-template-columns: 1fr; gap: 0.5rem; }
      .map-edges, .map-head { display: none; }
      .node-stack { margin: 0 0 1rem 1rem; }
      nav { padding: 0.5rem 1rem; gap: 0.75rem; }
      header, section { padding-left: 1rem; padding-right: 1rem; }
      header h1 { font-size: 1.5rem; }
    }
    @media print {
      nav, .filter-bar, .map-edges, .reveal { display: none; }
      #candidates-body { display: block; }
      .entry-body { display: block !important; }
      .entry, .stat-card, .panel { break-inside: avoid; }
    }
  </style>
</head>
<body>
  <nav id="main-nav">
    <span class="mark">CEA</span>
    <a href="#overview" class="active">Overview</a>
    <a href="#map">Claim Map</a>
    <a href="#results">Main Results</a>
    <a href="#claims">Narrow Claims</a>
    <a href="#candidates">Rejected Candidates</a>
    @@PDF_LINK@@
  </nav>

  <header>
    <h1>@@TITLE@@</h1>
    <p class="subtitle">This page records the narrow claims the paper's main results rest on, and the
    candidates that were considered and rejected. <code>@@PAPER_ID@@</code> &middot; @@PAGES@@ pages.</p>
  </header>

  <main>
    <section id="overview">
      <h2>Overview@@ANCHOR_OVERVIEW@@</h2>
      <div class="stats-grid">@@STATS@@</div>
      @@WATCH@@
      <div class="panel">
        <h3>By section of the paper</h3>
        <p class="panel-desc">One row per section heading, in page order. Each mark is one main result, narrow
        claim, or rejected candidate recorded in that section. Click a mark to open its card.</p>
        <div class="legend in-panel">
          <span class="legend-item"><i class="legend-dot result"></i>main result (B)</span>
          <span class="legend-item"><i class="legend-dot claim"></i>narrow claim (C)</span>
          <span class="legend-item"><i class="legend-dot candidate"></i>rejected candidate (R)</span>
        </div>
        <div class="strip">@@STRIP@@</div>
      </div>
    </section>

    <section id="map">
      <h2>Claim Map@@ANCHOR_MAP@@</h2>
      <p class="section-desc">Hover a node for its sentence, click it for its card. Results stand in the order of
      how many sentences state each one, and claims stand in page order.</p>
      <div class="map" id="claim-map">
        <svg class="map-edges" id="map-edges" aria-hidden="true"></svg>
        <div class="map-head">Main results</div>
        <div class="map-head">Narrow claims</div>
        @@MAP@@
      </div>
    </section>

    <section id="results">
      <h2>Main Results@@ANCHOR_RESULTS@@</h2>
      <div class="cards-grid" id="result-cards">@@RESULTS@@</div>
    </section>

    <section id="claims">
      <h2>Narrow Claims@@ANCHOR_CLAIMS@@</h2>
      <div class="filter-bar">
        <input id="claim-search" type="search" placeholder="Filter by quote, section or reason"
               aria-label="Filter narrow claims">
        <button class="chip" data-flag="note">Has a note</button>
        <button class="chip" data-flag="table">Checked against a table or figure</button>
        <span class="filter-count" id="claim-count"></span>
      </div>
      <div class="cards-grid" id="claim-cards">@@CLAIMS@@</div>
    </section>

    <section id="candidates">
      <h2>Rejected Candidates@@ANCHOR_CANDIDATES@@</h2>
      <button class="reveal" id="reveal-candidates" aria-expanded="false">Show @@N_CANDIDATES@@ rejected candidates</button>
      <div id="candidates-body">
        <div class="filter-bar">
          <input id="cand-search" type="search" placeholder="Filter by quote, section or reason"
                 aria-label="Filter rejected candidates">
          <button class="chip" data-flag="note">Has a note</button>
          <button class="chip" data-flag="table">Checked against a table or figure</button>
          <span class="filter-count" id="cand-count"></span>
        </div>
        @@CANDIDATES@@
      </div>
    </section>

  </main>

  <footer>
    <code>cea_claims.py render</code> wrote this page from <code>@@SOURCE@@</code> on @@BUILT@@.
    Every quote is the paper's wording, and <code>cea_claims.py validate</code> checked each one against the
    page text of <code>@@PDF@@</code>. An agent drafted this page's contents and a person, the checker, reviews them and can
    overturn any main result, claim or candidate. Claim&ndash;Evidence Alignment covers quantitative
    empirical claims, so a main result that the paper states as a qualitative finding is out of its scope and
    is not recorded here.
    @@SOURCE_LINKS@@
  </footer>

  <div id="tooltip" class="tooltip"></div>

  <script type="application/json" id="cea-data">@@DATA@@</script>
  <script>
(function () {
  var DATA = JSON.parse(document.getElementById('cea-data').textContent);
  var BY_ID = {};
  DATA.broad_statements.forEach(function (b) { BY_ID[b.id] = { kind: 'result', e: b }; });
  DATA.claims.forEach(function (c) { BY_ID[c.id] = { kind: 'claim', e: c }; });
  DATA.rejected.forEach(function (r) { BY_ID[r.id] = { kind: 'candidate', e: r }; });

  var candidates = document.getElementById('candidates');
  var revealBtn = document.getElementById('reveal-candidates');
  var tooltip = document.getElementById('tooltip');
  var mapEl = document.getElementById('claim-map');
  var edges = document.getElementById('map-edges');

  function revealCandidates() {
    candidates.classList.add('shown');
    revealBtn.setAttribute('aria-expanded', 'true');
    revealBtn.textContent = 'Hide candidates';
  }
  revealBtn.addEventListener('click', function () {
    if (candidates.classList.contains('shown')) {
      candidates.classList.remove('shown');
      revealBtn.setAttribute('aria-expanded', 'false');
      revealBtn.textContent = 'Show ' + DATA.rejected.length + ' rejected candidates';
    } else {
      revealCandidates();
    }
  });

  function toggleEntry(entry, force) {
    var open = force === undefined ? !entry.classList.contains('expanded') : force;
    entry.classList.toggle('expanded', open);
    entry.querySelector('.entry-header').setAttribute('aria-expanded', String(open));
  }

  document.addEventListener('click', function (ev) {
    var jump = ev.target.closest('[data-jump]');
    if (jump) { ev.preventDefault(); navigateTo(jump.dataset.jump); return; }
    var node = ev.target.closest('.node[data-id]');
    if (node) { navigateTo(node.dataset.id); return; }
    var open = ev.target.closest('.node[data-reveal]');
    if (open) { navigateTo(open.dataset.reveal); return; }
    var header = ev.target.closest('.entry-header');
    if (header) { toggleEntry(header.parentElement); return; }
  });

  document.addEventListener('keydown', function (ev) {
    if (ev.key !== 'Enter' && ev.key !== ' ') return;
    var header = ev.target.closest && ev.target.closest('.entry-header');
    if (header) { ev.preventDefault(); toggleEntry(header.parentElement); }
  });

  function navigateTo(id) {
    var el = document.getElementById(id);
    if (!el) return;
    if (el.closest('#candidates')) revealCandidates();
    if (el.classList.contains('entry')) toggleEntry(el, true);
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    el.classList.add('highlight-flash');
    setTimeout(function () { el.classList.remove('highlight-flash'); }, 1500);
    history.replaceState(null, '', '#' + id);
  }

  /* -- tooltip and edge highlighting on the map -- */
  function showTip(node, ev) {
    var rec = BY_ID[node.dataset.id];
    if (!rec) return;
    var e = rec.e;
    var extra = rec.kind === 'result'
      ? 'Main result, ' + e.source.replace('_', ' ') + ', page ' + e.page
      : 'Narrow claim, page ' + e.page + ', serves ' + e.serves.join(', ');
    tooltip.innerHTML = '<span class="abbr">' + e.id + ' &middot; ' + extra + '</span><p>' +
      (e.quote || '').replace(/[<>&]/g, function (c) { return { '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]; }) +
      '</p>';
    tooltip.style.display = 'block';
    moveTip(ev);
  }
  function moveTip(ev) {
    var pad = 14, w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    var x = Math.min(ev.clientX + pad, window.innerWidth - w - pad);
    var y = ev.clientY + pad + h > window.innerHeight ? ev.clientY - h - pad : ev.clientY + pad;
    tooltip.style.left = x + 'px';
    tooltip.style.top = Math.max(pad, y) + 'px';
  }
  var nodes = Array.prototype.slice.call(document.querySelectorAll('.node[data-id]'));
  nodes.forEach(function (node) {
    node.addEventListener('mouseenter', function (ev) { showTip(node, ev); highlight(node.dataset.id); });
    node.addEventListener('mousemove', moveTip);
    node.addEventListener('mouseleave', function () { tooltip.style.display = 'none'; highlight(null); });
    node.addEventListener('focus', function () { highlight(node.dataset.id); });
    node.addEventListener('blur', function () { highlight(null); });
  });

  function related(id) {
    var set = {};
    set[id] = true;
    var rec = BY_ID[id];
    if (!rec) return set;
    if (rec.kind === 'claim') rec.e.serves.forEach(function (b) { set[b] = true; });
    if (rec.kind === 'result') DATA.claims.forEach(function (c) {
      if (c.serves.indexOf(id) > -1) set[c.id] = true;
    });
    return set;
  }

  function highlight(id) {
    var set = id ? related(id) : null;
    nodes.forEach(function (n) { n.classList.toggle('dim', !!set && !set[n.dataset.id]); });
    Array.prototype.forEach.call(edges.children, function (p) {
      var pair = p.dataset.pair.split(' ');
      var on = !set || (set[pair[0]] && set[pair[1]]);
      p.setAttribute('stroke', on && set ? '#004488' : '#a8a29e');
      p.setAttribute('opacity', on ? '1' : '0.2');
    });
  }

  var NS = 'http://www.w3.org/2000/svg';
  function drawEdges() {
    while (edges.firstChild) edges.removeChild(edges.firstChild);
    if (window.innerWidth < 900) return;
    var box = mapEl.getBoundingClientRect();
    edges.setAttribute('viewBox', '0 0 ' + box.width + ' ' + box.height);
    document.querySelectorAll('.node.claim[data-serves]').forEach(function (claim) {
      claim.dataset.serves.split(' ').filter(Boolean).forEach(function (rid) {
        var res = document.querySelector('.node.result[data-id="' + rid + '"]');
        if (!res) return;
        var a = res.getBoundingClientRect(), b = claim.getBoundingClientRect();
        var x1 = a.right - box.left, y1 = a.top - box.top + Math.min(20, a.height / 2);
        var x2 = b.left - box.left, y2 = b.top - box.top + Math.min(20, b.height / 2);
        var dx = Math.max(16, (x2 - x1) / 2);
        var path = document.createElementNS(NS, 'path');
        path.setAttribute('d', 'M' + x1 + ' ' + y1 + ' C' + (x1 + dx) + ' ' + y1 + ' ' +
          (x2 - dx) + ' ' + y2 + ' ' + x2 + ' ' + y2);
        path.setAttribute('fill', 'none');
        path.setAttribute('stroke', '#a8a29e');
        path.setAttribute('stroke-width', '1.5');
        path.dataset.pair = rid + ' ' + claim.dataset.id;
        edges.appendChild(path);
      });
    });
  }

  /* -- filters -- */
  function setupFilter(inputId, countId, scope, flags) {
    var input = document.getElementById(inputId);
    var count = document.getElementById(countId);
    var entries = Array.prototype.slice.call(document.querySelectorAll(scope + ' .entry'));
    entries.forEach(function (e) { e.dataset.text = (e.textContent || '').toLowerCase(); });
    var chips = flags ? Array.prototype.slice.call(document.querySelectorAll(scope + ' .chip')) : [];
    function flagOf(entry, flag) {
      var rec = BY_ID[entry.id];
      if (!rec) return false;
      var note = rec.e.note || '';
      if (flag === 'note') return !!note;
      if (flag === 'table') return /Table|Fig/.test(note);
      return false;
    }
    function apply() {
      var term = input.value.trim().toLowerCase();
      var on = chips.filter(function (c) { return c.classList.contains('active'); })
        .map(function (c) { return c.dataset.flag; });
      var hits = 0;
      entries.forEach(function (e) {
        var ok = (!term || e.dataset.text.indexOf(term) > -1) &&
          on.every(function (f) { return flagOf(e, f); });
        e.classList.toggle('hide', !ok);
        if (ok) hits++;
      });
      document.querySelectorAll(scope + ' .cand-group').forEach(function (g) {
        g.classList.toggle('hide', !g.querySelector('.entry:not(.hide)'));
      });
      count.textContent = term || on.length ? hits + ' of ' + entries.length + ' shown' : '';
    }
    input.addEventListener('input', apply);
    chips.forEach(function (c) {
      c.addEventListener('click', function () { c.classList.toggle('active'); apply(); });
    });
  }
  setupFilter('claim-search', 'claim-count', '#claims', true);
  setupFilter('cand-search', 'cand-count', '#candidates', true);

  /* -- section nav -- */
  var navLinks = document.querySelectorAll('#main-nav a:not(.nav-ext)');
  navLinks.forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      var target = document.querySelector(link.getAttribute('href'));
      if (target) target.scrollIntoView({ behavior: 'smooth' });
    });
  });
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      navLinks.forEach(function (l) { l.classList.remove('active'); });
      var active = document.querySelector('#main-nav a[href="#' + entry.target.id + '"]');
      if (active) active.classList.add('active');
    });
  }, { threshold: 0.2, rootMargin: '-80px 0px 0px 0px' });
  document.querySelectorAll('main > section').forEach(function (s) { observer.observe(s); });

  window.addEventListener('resize', drawEdges);
  window.addEventListener('hashchange', function () {
    if (location.hash.length > 1) navigateTo(location.hash.slice(1));
  });
  if (window.ResizeObserver) new ResizeObserver(drawEdges).observe(mapEl);
  drawEdges();
  if (location.hash.length > 1 && BY_ID[location.hash.slice(1)]) {
    setTimeout(function () { navigateTo(location.hash.slice(1)); }, 200);
  }
})();
  </script>
</body>
</html>
"""
