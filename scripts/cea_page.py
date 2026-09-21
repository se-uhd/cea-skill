#!/usr/bin/env python3
"""Render a claim record as one HTML page.

A third output of the extract-claims skill, beside claims.json and claims.md. It follows the page
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
import os
import re
import sys
from datetime import date
from pathlib import Path

# cea_claims.py sits beside this file, and the page reuses its ordering so that the page and
# claims.md cannot disagree about which claims stand under which main result.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cea_claims import (_by_weight, _first_page, _flat, _id_order, _key,  # noqa: E402
                        _one_result, _shared_with_other_results, _stated_in)

E = html.escape
RID = re.compile(r"\bR\d+\b")

ANCHOR = ('<a class="section-anchor" href="#{id}" onclick="navigator.clipboard.writeText(this.href)" '
          'title="Copy link to section"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" '
          'stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
          '<path d="M6.75 9.25a3.25 3.25 0 0 0 4.596.148l1.904-1.904a3.25 3.25 0 0 0-4.596-4.596L7.5 4.052"/>'
          '<path d="M9.25 6.75a3.25 3.25 0 0 0-4.596-.148L2.75 8.506a3.25 3.25 0 0 0 4.596 4.596L8.5 11.948"/>'
          '</svg></a>')

LINKS = ["interpretation", "operationalization", "measurement", "unit bridge", "analysis", "reasoning"]

# What a reason says when it weighs a candidate against a main result and the result survives.
# The reference asks for "R2 would still stand, because ...", so the words are the reason's own.
# A clause that asks whether a result stands is not saying that it does, and one that says it
# stands only under some reading is stating a condition, not a verdict. The page prints this
# as "Leaves its main result standing", over a reason that declines to say so.
_ASKS = re.compile(r"\b(?:whether|unclear|uncertain|depends|debatable|arguable)\b", re.I)
_ONLY_IF = re.compile(r"\bstands?\s+only\b", re.I)
_STANDS = re.compile(r"\b(?:would|does|still)\b([^.]{0,40}?)\bstands?\b", re.I)
# "without" is not one of these: "R1 still stands without this sentence" is the affirmative case,
# and it is how the reference's own template reads.
# "nothing" is its own word: `\bno\b` does not reach inside it, so "Nothing in R1 would still
# stand" read as the affirmative and the page said the result stands.
_NOT = re.compile(r"\b(?:not|never|no|none|nothing|neither|nor|cannot|hardly|barely|n't)\b",
                  re.I)


def still_stands(reason: str) -> bool:
    """Whether a reason says a main result survives the candidate.

    Each clause is read on its own. A negation can stand before the words as readily as between
    them ("No main result would still stand"), and a clause that negates one thing can be followed
    by one that affirms another ("does not need this sentence and would still stand").
    """
    return any(_affirms(clause) for clause in _clauses(reason))


# What a fragment may hold besides the ids it carries into the next clause. Anything else is a
# clause of its own: "Repeats R4" and "R3 names no property" say something about a statement, and
# welding them to the verdict that follows published "leaves standing R4" over a reason saying the
# candidate repeats R4, and lost R3 the credit its own reason gives it.
_CARRIES = frozenset(("", "so", "and", "or", "both", "also", "then", "thus",
                      "together", "along", "with", "plus", "as", "well"))


def _clauses(reason: str) -> list[str]:
    """A reason split into the clauses that are read one at a time.

    The colon splits too. "R3 would still stand: this changed nothing" is two clauses, and
    without the split the second clause's "nothing" would deny the first, which is a real reason
    in a real record.
    """
    parts = re.split(r"[,;.:]| and | but ", reason)
    # A list of results shares one verb: "R3 and R5 would both still stand" is cut at the "and",
    # which left R3 in a clause of its own with nothing to affirm it, and two real records lost a
    # result they are credited with. A short piece naming a result and holding no verb of its own
    # belongs to the clause that follows it.
    out: list[str] = []
    carried = ""
    for part in parts:
        # A part with no words at all is what "R3, R5, and R7" leaves between the comma and the
        # " and ". Keeping it flushed the carried ids into a clause of their own, so an Oxford
        # comma cost a reason every result but the last.
        if not part.strip():
            continue
        bare = [w.strip(".,;:()").casefold() for w in RID.sub(" ", part).split()]
        if (RID.search(part) and not _STANDS.search(part) and len(part.split()) <= 4
                and all(w in _CARRIES for w in bare)):
            carried += part + " and "
            continue
        out.append(carried + part)
        carried = ""
    if carried:
        out.append(carried)
    return out


def _affirms(clause: str) -> bool:
    """Whether one clause says a main result survives the candidate."""
    return bool(_STANDS.search(clause) and not _NOT.search(clause)
                and not _ASKS.search(clause) and not _ONLY_IF.search(clause))

# The heading over each group of excluded claim candidates. The first four are read off the record: a
# candidate names what it repeats, what it breaks down, the split it is part of, or the statement
# it leaves standing. The last is everything else, so its heading says only that, and does not
# assert a ground: a candidate held open for the checker, or excluded for a reason the record
# states in words rather than by id, falls here too, and the card beneath gives its own reason.
REJ_GROUPS = [
    ("repeat", "Repeats a recorded statement"),
    ("breakdown", "Breaks a main result into parts"),
    ("part", "Another part of a split sentence"),
    ("standing", "Leaves its main result standing"),
    ("other", "Rejected on the ground its reason gives"),
]


SECTION_MAP = """    <section id="map">
      <h2>Claim Map@@ANCHOR_MAP@@</h2>
      <p class="section-desc">Hover a node for its sentence, click it for its card. Results stand in the order of
      how many sentences state each one, and claims stand in page order.</p>
      <div class="map" id="claim-map">
        <svg class="map-edges" id="map-edges" aria-hidden="true"></svg>
        <div class="map-head">Main results</div>
        <div class="map-head">Claims</div>
        @@MAP@@
      </div>
    </section>
"""

SECTION_RESULTS = """    <section id="results">
      <h2>Main Results@@ANCHOR_RESULTS@@</h2>
      <div class="cards-grid">@@RESULTS@@</div>
    </section>
"""

SECTION_CLAIMS = """    <section id="claims">
      <h2>Claims@@ANCHOR_CLAIMS@@</h2>
      <div class="filter-bar">
        <input id="claim-search" type="search" placeholder="Filter by quote, section or reason"
               aria-label="Filter claims">
        <button class="chip" data-flag="note">Has a note</button>
        <button class="chip" data-flag="table">Checked against a table or figure</button>
        <span class="filter-count" id="claim-count"></span>
      </div>
      <div class="cards-grid">@@CLAIMS@@</div>
    </section>
"""


def refs(entry: dict, field: str) -> list[str]:
    value = entry.get(field) or []
    return value if isinstance(value, list) else [value]


def weighed_against(r: dict, known: set[str] | None = None) -> set[str]:
    """The main results that an excluded claim candidate refers to.

    `duplicate_of` and `breaks_down` name them as data. A reason names one in its text, because the
    skill requires "R2 would still stand, because ...". Prose is not data, though: a paper about
    vitamin R12 puts that in a reason too, so a name the record does not hold is not a reference.

    Nor is every mention a weighing. A reason may name a statement to say what it is about, or to
    say that no statement states the result the candidate would serve, and calling that "leaves
    R3 standing" tells the reader the opposite of what the record says.
    """
    # Per clause, not over the whole reason. `still_stands` was made to read a reason clause by
    # clause, and this then took every id in the string as soon as any clause affirmed -- so a
    # reason weighing two results, one surviving and one not, tagged the candidate with both and
    # the page printed "leaves standing R5" directly above a reason saying R5 does not stand.
    named = {b for clause in _clauses(r.get("reason", ""))
             if _affirms(clause) for b in RID.findall(clause)}
    if known is not None:
        named &= known
    return set(refs(r, "duplicate_of")) | set(refs(r, "breaks_down")) | named


def kind_of(r: dict, known: set[str] | None = None) -> str:
    if refs(r, "duplicate_of"):
        return "repeat"
    if refs(r, "breaks_down"):
        return "breakdown"
    if r.get("split_from"):
        return "part"
    if weighed_against(r, known) - set(refs(r, "duplicate_of")) - set(refs(r, "breaks_down")):
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


def plugin_version() -> str:
    """The version of the plugin that built the page, read from its manifest.

    Nothing published says which build wrote it. A record written against an older format can
    still pass every check and put a wrong number on a page, and there is then no way to tell
    from the page which build produced it. Empty when the manifest is not beside the scripts,
    which is not worth refusing to build over.
    """
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return ""
    return version if isinstance(version, str) and re.fullmatch(r"[\w.+-]{1,32}", version) else ""


def chain_track() -> str:
    """The mapping level of every claim on the page, which is L1 for all of them.

    L1 means the chain from the paper's words down to what was measured has not been reconstructed,
    which is what this skill does and does not do: it records the claim and its evidence, and
    leaves the chain to a later step. The gloss says so of each of the six links, because reading
    it as a list ending in "not reconstructed" made it sound like a remark about the last one.
    """
    dots = "".join(f'<i class="dot" title="link {i} of 6, {t}: not reconstructed"></i>'
                   for i, t in enumerate(LINKS, 1))
    # The gloss is written out, not left in the title attribute. A title shows on hover and
    # nowhere else: not in print, not on a touch screen, not to a reader using a screen reader,
    # and not in the text of a saved page. Six empty circles and the letters L1 mean nothing
    # without it.
    return ('<div class="chain" title="mapping level L1: none of the six links of the chain ('
            + ", ".join(LINKS) + ') is reconstructed here, which is what L1 means">'
            '<span class="lvl">L1</span>' + dots
            + '<span class="chain-note">none of the six links is reconstructed here, '
              'which is what L1 means</span></div>')


def near(name: str, source: Path, out: Path) -> str:
    """The relative path from the page to a file of the record, when it is close enough to stay valid."""
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

    The PDF published beside the page comes first, because that is the copy a reader of the page can
    open. Then the copy in the record's own directory, which is where `extract` puts one. With no
    file to link, the recorded path stands as text.
    """
    name = Path(str(paper["pdf"])).name
    # Beside the page, or in the record. Not the directory the command happened to run in: a file
    # that merely shares the name is not this paper, the link dies the moment the record is moved
    # or zipped, and the footer says every quote was checked against whatever it points at.
    tried = [out.resolve().parent / name, source.parent / name, source.parent / str(paper["pdf"])]
    # Only what the record carries, as `cea_site.copy_paper` requires of what it publishes. A path
    # that climbs out through a link points at a file the record does not hold, and the footer
    # says every quote was checked against the file this links.
    inside = [out.resolve().parent, source.parent.resolve()]
    for candidate in tried:
        try:
            candidate = candidate.resolve()
            if not candidate.is_file() or not any(candidate.is_relative_to(d) for d in inside):
                continue
        except (OSError, ValueError):  # a NUL or an over-long name is not a file, it is a typo
            continue
        rel = os.path.relpath(candidate, out.resolve().parent)
        if rel.count("..") > 2:  # a path that climbs that far will not survive being moved
            continue
        return f'<a class="nav-ext" href="{E(rel)}">{E(name)}</a>'
    # No web address here. `paper` holds id, title, pdf and pages and nothing else, so a doi or a
    # url field cannot reach a record that validate accepts, and a branch for one would be a
    # defence against nothing that a reader would take for a live one.
    return f'<span class="nav-ext plain" title="{E(str(paper["pdf"]))}">{E(name)}</span>'


_MARKER = re.compile(r"@@([A-Z_]+)@@")


def _fill(template: str, fields: dict) -> str:
    """Replace every @@MARKER@@ in one pass.

    Substituting key by key would re-scan each value already inserted, so a record that happens to
    contain a marker would have it expanded into markup that html.escape never saw. The sections
    are composed into the template before this runs, for the same reason: every value passed here
    holds record text, and none of it is scanned.
    """
    return _MARKER.sub(lambda m: fields.get(m.group(1), m.group(0)), template)


def build(data: dict, source: Path, out: Path, index_href: str | None = None,
          framework_href: str | None = None, built_by: str = "extract-claims skill") -> str:
    """`index_href` is the path back to the index, which only a site has, and a page rendered on its
    own has no index to return to, so it carries no link.

    `built_by` names the skill whose command produced the page, which the footer states. A site
    build calls this directly and never runs extract-claims, so it says so itself.

    `$schema` is dropped: a checker may point their editor at the schema, and that path is theirs
    rather than the reader's, while the page embeds the whole record."""
    data = {k: v for k, v in data.items() if k != "$schema"}
    paper = data["paper"]
    results, claims, excluded = data["main_results"], data["claims"], data["excluded"]
    by_id = {b["id"]: b for b in results}
    order = {b["id"]: n for n, b in enumerate(_by_weight(data))}
    groups = sorted(_one_result(data), key=lambda g: min(order[b] for b in g))
    serving = {b["id"]: [c["id"] for c in claims if b["id"] in c["serves"]] for b in results}
    known = {b["id"] for b in results}
    considered = {b["id"]: [r["id"] for r in excluded if b["id"] in weighed_against(r, known)]
                  for b in results}
    splits: dict[str, list[str]] = {}
    for x in claims + excluded:
        if x.get("split_from"):
            splits.setdefault(x["split_from"], []).append(x["id"])
    repeats: dict[str, list[str]] = {}
    breakdowns: dict[str, list[str]] = {}
    for r in excluded:
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
        # page order, the order the claim cards below stand in and the order the caption promises
        mine = sorted((c for c in claims if home.get(c["id"]) == n), key=_first_page)
        rej = sorted({x for g in group for x in considered[g]}, key=lambda i: int(i[1:]))
        nodes = "".join(
            f'<button class="node claim" data-id="{E(c["id"])}" data-serves="{E(" ".join(c["serves"]))}" '
            f'title="{snippet(c["states"], 300)}"><span class="node-id">{E(c["id"])}</span>'
            f'<span class="node-text">{snippet(c["states"], 170)}</span>'
            f'<span class="node-meta">p. {E(str(c["page"]))}'
            f'{" &middot; note" if c.get("note") else ""}</span></button>' for c in mine)
        # A claim that serves two results is filed under the first of them, so the second row
        # would show nothing of it and contradict its own card, which lists that claim.
        elsewhere = sorted({c["id"] for c in claims
                            if set(c["serves"]) & set(group) and home.get(c["id"]) != n},
                           key=lambda i: int(i[1:]))
        if not mine:
            nodes = ('<p class="node-empty">'
                     + (f'Served by {", ".join(E(i) for i in elsewhere)}, shown under the result '
                        'each claim was filed with above. ' if elsewhere else
                        'No claim serves this result. ')
                     + f'{E(_flat(b.get("note") or ""))}</p>')
        elif elsewhere:
            nodes += ('<p class="node-empty">Also served by '
                      f'{", ".join(E(i) for i in elsewhere)}, shown under the result each claim '
                      'was filed with.</p>')
        if rej:
            # It names them all, so it takes the reader to all of them: the first card, with
            # the section open and the others listed here, rather than one card of the ten.
            nodes += (f'<button class="node candidates" data-reveal="{E(rej[0])}" '
                      f'title="{E(", ".join(rej))}">'
                      f'{len(rej)} excluded claim candidate{"s" if len(rej) != 1 else ""}: '
                      f'{E(", ".join(rej[:6]))}{" and more" if len(rej) > 6 else ""} '
                      f'&rarr;</button>')
        # A sentence that repeats this result and another one is counted under both, so these
        # rows add up to more than the Overview's count of sentences. Say which are which.
        n_shared = _shared_with_other_results(data, group)
        shared = f' ({n_shared} shared)' if n_shared else ""
        also = f' <span class="node-also">also stated as {", ".join(also_id for also_id in group if also_id != lead)}</span>' if len(group) > 1 else ""
        rows.append(
            f'<div class="map-row">'
            f'<button class="node result{" empty" if not serving[lead] else ""}" data-id="{E(lead)}" '
            f'title="{snippet(b["quote"], 300)}"><span class="node-id">{E(lead)}</span>'
            f'<span class="node-text">{snippet(b["quote"], 190)}</span>'
            f'<span class="node-meta">{E(b["source"])} &middot; p. {E(str(b["page"]))} &middot; '
            f'stated in {_stated_in(data, group)} sentence{"s" if _stated_in(data, group) > 1 else ""}'
            f'{shared}{also}</span></button>'
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
            field("excluded claim candidates that repeat it", badges(repeats.get(lead, []))),
            field("excluded claim candidates that break it into parts", badges(breakdowns.get(lead, []))),
            field("excluded claim candidates that leave it standing",
                  badges([r for r in considered[lead]
                          if r not in repeats.get(lead, []) and r not in breakdowns.get(lead, [])])),
            field("note", para(b["note"]) if b.get("note") else ""),
        ])
        result_cards.append(entry_card(
            "result", lead, f'{E(b["source"])} &middot; p. {E(str(b["page"]))} &middot; '
            f'{E(_flat(b["section"]))}', E(_flat(b["quote"])), tags, body))

        # Every statement of the group gets a card of its own, not only the one the group is led
        # by. A claim's `serves` badge links each id it names, and an id the page does not anchor
        # is a link that goes nowhere. The reader also never sees that statement's own sentence,
        # page or note, though the record holds them and claims.md prints them.
        for other in sorted(set(group) - {lead}, key=lambda i: order[i]):
            o = by_id[other]
            with_lead = (f'<span class="taglabel">states the same result as</span>{badge(lead)}')
            if o.get("note"):
                with_lead += ' <span class="minitag">note</span>'
            result_cards.append(entry_card(
                "result", other, f'{E(o["source"])} &middot; p. {E(str(o["page"]))} &middot; '
                f'{E(_flat(o["section"]))}', E(_flat(o["quote"])), with_lead, "".join([
                    field("quote", quote_of(o)),
                    field("states", para(o["states"]))
                    if o.get("states") and _key(o["states"]) != _key(o["quote"]) else "",
                    field("states the same main result as", badges([lead])),
                    field("claims that serve it", badges(serving[other]) or '<p class="dim">none</p>'),
                    field("note", para(o["note"]) if o.get("note") else ""),
                ])))
            ticks.append((_first_page(o), "result", other, _flat(o["section"])))

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
            E(_flat(c["states"])), tags, body))
        ticks.append((_first_page(c), "claim", c["id"], _flat(c["section"])))

    # the excluded claim candidates, folded
    buckets: dict[str, list[dict]] = {k: [] for k, _ in REJ_GROUPS}
    for r in sorted(excluded, key=_first_page):
        buckets[kind_of(r, known)].append(r)
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
                "standing": ("leaves standing", sorted(weighed_against(r, known))),
                "part": ("split from", [r.get("split_from") or ""]),
            }.get(kind_of(r, known), ("", []))
            ids = [i for i in ids if i]
            tags = (f'<span class="taglabel">{label}</span>' +
                    (badges(ids) if kind_of(r, known) != "part" else E(ids[0]))) if ids else ""
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
                                    E(_flat(r["reason"])), tags, body))
        cand_html.append(f'<div class="cand-group">'
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
                        # by id as the rest of the page does, so R7 does not follow R15
                        for pg, k, i in sorted(items,
                                               key=lambda m: (m[0], rank[m[1]], _id_order(m[2]))))
        counts = {k: sum(1 for _, kk, _ in items if kk == k) for k in rank}
        tally = ", ".join(f"{n} {name}{'s' if n > 1 else ''}" for name, n in
                          (("main result", counts["result"]), ("claim", counts["claim"]),
                           ("excluded claim candidate", counts["candidate"])) if n)
        rows_sec.append(f'<tr><td class="sec">{E(sec)}</td><td class="pp">{span}</td>'
                        f'<td class="mm" title="{E(tally)}"><div class="marks">{marks}</div></td></tr>')
    strip = ('<table class="sectable"><thead><tr><th>Section, as the record gives it</th><th>Page</th>'
             '<th>What is recorded there</th></tr></thead><tbody>'
             + "".join(rows_sec) + "</tbody></table>")

    # the overview
    places = _stated_in(data) if results else 0
    # "main results", not "main results": several statements can state one result, which the
    # map merges into one row, and one statement can state two. claims.md has always said this.
    stats = [(len(results), "main results"), (len(claims), "claims"),
             (len(excluded), "excluded claim candidates"), (places, "sentences the record marks as stating the main results")]
    stat_html = "".join(f'<div class="stat-card"><div class="stat-value">{v}</div>'
                        f'<div class="stat-label">{E(l)}</div></div>' for v, l in stats)

    unserved = [b for b in results if not serving[b["id"]]]
    watch_html = ""
    if unserved:
        items = "".join(
            f'<li>{badge(b["id"])} <span class="question">{E(_flat(b["states"]))}</span>'
            f'<p class="side">{E(_flat(b.get("note") or ""))}</p>'
            f'<p class="side dim">The sentence it is quoted from: {E(_flat(b["quote"]))}</p></li>'
            if b.get("states") and _key(b["states"]) != _key(b["quote"]) else
            f'<li>{badge(b["id"])} <span class="question">{E(_flat(b["quote"]))}</span>'
            f'<p class="side">{E(_flat(b.get("note") or ""))}</p></li>' for b in unserved)
        watch_html = (f'<div class="note-card"><h3>Main result{"s" if len(unserved) != 1 else ""} that no '
                      f'claim serves</h3><ul>{items}</ul></div>')
    if not results:
        # What the record says, not what the paper does. The record marks no sentence as a main
        # result; it does not say the paper has none, and the page cannot tell the difference. A
        # record whose main results were all demoted to candidates made the page assert, in
        # its own voice, that a paper with a quantitative abstract states no quantitative result,
        # above cards quoting that paper's numbers.
        watch_html = ('<div class="note-card scope"><h3>Scope</h3><p>This record marks no sentence '
                      'of this paper as stating a quantitative main result, so it records no main '
                      'result and no claim. Claim-Evidence Alignment covers quantitative '
                      'empirical claims, and a qualitative finding is out of its scope, not absent '
                      'from the paper. The candidates considered are below, each with the reason '
                      'it is not a claim.</p></div>') + watch_html

    links = [("overview", "Overview")]
    if results:
        links += [("map", "Claim Map"), ("results", "Main Results")]
    if claims:
        links.append(("claims", "Claims"))
    links.append(("candidates", "Excluded Claim Candidates"))
    active = ' class="active"'
    nav_links = "\n    ".join(
        f'<a href="#{i}"{active if n == 0 else ""}>{E(label)}</a>'
        for n, (i, label) in enumerate(links))
    template = (TEMPLATE
                .replace("@@SEC_MAP@@", SECTION_MAP if results else "")
                .replace("@@SEC_RESULTS@@", SECTION_RESULTS if results else "")
                .replace("@@SEC_CLAIMS@@", SECTION_CLAIMS if claims else ""))

    fields = {
        "TITLE": E(_flat(paper["title"])),
        "PAPER_ID": E(paper["id"]),
        "PAGES": str(pages),
        "PDF": E(Path(str(paper["pdf"])).name),
        "PDF_LINK": pdf_link(paper, source, out),
        "NAV_LINKS": nav_links,
        "MAP": "".join(rows),
        "RESULTS": "".join(result_cards),
        "CLAIMS": "".join(claim_cards),
        "FRAMEWORK": (f'The terms on this page are defined in <a href="{E(framework_href)}">the framework</a>.'
                      if framework_href else ""),
        "HOME": (f'<a class="nav-home" href="{E(index_href)}">&larr; All papers</a>'
                 if index_href else ""),
        "BUILT_BY": E(built_by),
        "VERSION": f" (version {E(v)})" if (v := plugin_version()) else "",
        "SOURCE_LINKS": (f'<p class="files">{links}</p>' if (links := source_links(paper, source, out)) else ""),
        "STATS": stat_html,
        "WATCH": watch_html,
        "STRIP": strip,
        "CANDIDATES": "".join(cand_html) or '<p class="node-empty">No candidate was recorded.</p>',
        "N_CANDIDATES": str(len(excluded)),
        "SOURCE": E(source.name),
        "BUILT": date.today().isoformat(),
        "DATA": json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"),
        "ANCHOR_OVERVIEW": ANCHOR.format(id="overview"),
        "ANCHOR_MAP": ANCHOR.format(id="map"),
        "ANCHOR_RESULTS": ANCHOR.format(id="results"),
        "ANCHOR_CLAIMS": ANCHOR.format(id="claims"),
        "ANCHOR_CANDIDATES": ANCHOR.format(id="candidates"),
    }
    return _fill(template, fields)


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
    nav .nav-home { color: #78716c; }
    nav .nav-home:hover { color: #1c1917; }
    nav .nav-ext:hover { color: #1e40af; }
    nav .nav-ext.plain { color: #a8a29e; font-size: 0.8rem; font-family: 'SFMono-Regular', Consolas, Menlo, monospace; }
    nav .nav-ext { overflow-wrap: anywhere; max-width: 100%; }

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
    .note-card .side.dim { color: #a8a29e; font-family: 'Source Serif 4', Georgia, serif; }
    .note-card li + li { border-top: 1px solid #f5f5f4; padding-top: 0.5rem; margin-top: 0.5rem; }
    .note-card.scope { border-left-color: #004488; }
    .note-card li + li { border-top: 1px solid #f5f5f4; padding-top: 0.5rem; }
    .note-card li + li { margin-top: 0.35rem; }

    .panel { background: #fff; border: 1px solid #e7e5e4; padding: 1.25rem; }
    .panel h3 { font-size: 1.0rem; color: #1c1917; margin-bottom: 0.25rem; }
    .panel .panel-desc { font-size: 0.8rem; color: #78716c; margin-bottom: 1rem; }
    .strip { overflow-x: auto; }
    .sectable { border-collapse: collapse; font-size: 0.8rem; width: 100%; table-layout: fixed; }
    .sectable td.sec { overflow-wrap: anywhere; }
    .sectable th:nth-child(2), .sectable td.pp { width: 4.5rem; }
    .sectable th:nth-child(3) { width: 34%; }
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
    .node.candidates { background: #fff; border-left-color: #78716c; border-left-style: dashed; font-size: 0.75rem; color: #57534e; }
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
    .expand-icon { font-size: 0.85rem; color: #78716c; flex-shrink: 0; transition: transform 0.2s; }
    .entry-header:hover .expand-icon { color: #1c1917; }
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
    footer a { color: #1d4ed8; }
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
      nav .nav-ext { margin-left: 0; }
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
  <noscript>
    <style>
      /* Only the page's own script opens a body, so without scripting the page holds back four
         fifths of the record: every selection reason, every note, and every excluded claim candidate
         with the reason it was excluded. None of that is decoration. It is shown open instead,
         and the controls that would now do nothing are taken away. */
      .entry-body { display: block; }
      #candidates-body { display: block; }
      .filter-bar, .reveal { display: none; }
    </style>
  </noscript>
</head>
<body>
  <nav id="main-nav">
    <span class="mark">CEA</span>
    @@HOME@@
    @@NAV_LINKS@@
    @@PDF_LINK@@
  </nav>

  <header>
    <h1>@@TITLE@@</h1>
    <p class="subtitle">This page records the claims the paper's main results rest on, and the
    candidates that were considered and excluded. <code>@@PAPER_ID@@</code> &middot; @@PAGES@@ pages.</p>
  </header>

  <main>
    <section id="overview">
      <h2>Overview@@ANCHOR_OVERVIEW@@</h2>
      <div class="stats-grid">@@STATS@@</div>
      @@WATCH@@
      <div class="panel">
        <h3>By section of the paper</h3>
        <p class="panel-desc">One row per section heading, in page order. Each mark is one main result, claim, or excluded claim candidate recorded in that section. Click a mark to open its card.</p>
        <div class="legend in-panel">
          <span class="legend-item"><i class="legend-dot result"></i>main result (B)</span>
          <span class="legend-item"><i class="legend-dot claim"></i>claim (C)</span>
          <span class="legend-item"><i class="legend-dot candidate"></i>excluded claim candidate (R)</span>
        </div>
        <div class="strip">@@STRIP@@</div>
      </div>
    </section>

@@SEC_MAP@@@@SEC_RESULTS@@@@SEC_CLAIMS@@
    <section id="candidates">
      <h2>Excluded Claim Candidates@@ANCHOR_CANDIDATES@@</h2>
      <button class="reveal" id="reveal-candidates" aria-expanded="false">Show @@N_CANDIDATES@@ excluded claim candidates</button>
      <div id="candidates-body">
        <div class="filter-bar">
          <input id="cand-search" type="search" placeholder="Filter by quote, section or reason"
                 aria-label="Filter excluded claim candidates">
          <button class="chip" data-flag="note">Has a note</button>
          <button class="chip" data-flag="table">Checked against a table or figure</button>
          <span class="filter-count" id="cand-count"></span>
        </div>
        @@CANDIDATES@@
      </div>
    </section>

  </main>

  <footer>
    The <a href="https://github.com/se-uhd/cea-skill">cea-skill</a> @@BUILT_BY@@@@VERSION@@ built this page from
    <code>@@SOURCE@@</code> on @@BUILT@@, and checked every quote against the page text of <code>@@PDF@@</code>. An agent drafted this page's contents and a person, the checker, reviews them and can
    overturn any main result, claim or candidate. Claim-Evidence Alignment covers quantitative
    empirical claims, so a main result that the paper states as a qualitative finding is out of its scope and
    is not recorded here. @@FRAMEWORK@@
    @@SOURCE_LINKS@@
  </footer>

  <div id="tooltip" class="tooltip"></div>

  <script type="application/json" id="cea-data">@@DATA@@</script>
  <script>
(function () {
  var DATA = JSON.parse(document.getElementById('cea-data').textContent);
  var BY_ID = {};
  DATA.main_results.forEach(function (b) { BY_ID[b.id] = { kind: 'result', e: b }; });
  DATA.claims.forEach(function (c) { BY_ID[c.id] = { kind: 'claim', e: c }; });
  DATA.excluded.forEach(function (r) { BY_ID[r.id] = { kind: 'candidate', e: r }; });

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
      revealBtn.textContent = 'Show ' + DATA.excluded.length + ' excluded claim candidates';
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
    function esc(v) {
      return String(v === undefined || v === null ? '' : v)
        .replace(/[<>&"]/g, function (c) { return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' }[c]; });
    }
    var extra = rec.kind === 'result'
      ? 'Main result, ' + esc(String(e.source).replace('_', ' ')) + ', page ' + esc(e.page)
      : 'Claim, page ' + esc(e.page) + ', serves ' + esc([].concat(e.serves || []).join(', '));
    tooltip.innerHTML = '<span class="abbr">' + esc(e.id) + ' &middot; ' + extra + '</span><p>' +
      esc(e.quote) + '</p>';
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
    if (!mapEl || !edges) return;   // there is no map on a page with no main result
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
    if (!input || !count) return;   // a page with no claims has no claim search box
    var entries = Array.prototype.slice.call(document.querySelectorAll(scope + ' .entry'));
    entries.forEach(function (e) { e.dataset.text = (e.textContent || '').toLowerCase(); });
    var chips = flags ? Array.prototype.slice.call(document.querySelectorAll(scope + ' .chip')) : [];
    function flagOf(entry, flag) {
      var rec = BY_ID[entry.id];
      if (!rec) return false;
      var note = rec.e.note || '';
      if (flag === 'note') return !!note;
      // Case-insensitive, so a note recording that the paper prints no table is counted:
      // that note is the check, made and written down.
      if (flag === 'table') return /\b(table|fig\.?|figure)\b/i.test(note);  // bounded: "configuration" and "notable" held both words
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
  var navLinks = document.querySelectorAll('#main-nav a:not(.nav-ext):not(.nav-home)');
  navLinks.forEach(function (link) {
    link.addEventListener('click', function (e) {
      e.preventDefault();
      var target = document.querySelector(link.getAttribute('href'));
      if (!target) return;
      navLinks.forEach(function (l) { l.classList.toggle('active', l === link); });
      target.scrollIntoView({ behavior: 'smooth' });
    });
  });
  // The section the reader is in is the last one whose top has passed under the nav. Asking an
  // observer which sections are on screen marks the earliest of them, so the underline lags behind
  // the reader by the height of the band.
  var sections = Array.prototype.slice.call(document.querySelectorAll('main > section'));
  function markSection() {
    var line = 100;
    var current = sections[0];
    sections.forEach(function (s) {
      if (s.getBoundingClientRect().top <= line) current = s;
    });
    var scrollable = document.documentElement.scrollHeight > window.innerHeight + 4;
    if (scrollable && window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) {
      current = sections[sections.length - 1];  // the last section never reaches the line
    }
    if (!current) return;
    navLinks.forEach(function (l) {
      l.classList.toggle('active', l.getAttribute('href') === '#' + current.id);
    });
  }
  markSection();
  window.addEventListener('scroll', markSection, { passive: true });
  window.addEventListener('resize', markSection);
  window.addEventListener('resize', drawEdges);
  window.addEventListener('hashchange', function () {
    if (location.hash.length > 1) navigateTo(location.hash.slice(1));
  });
  if (window.ResizeObserver && mapEl) new ResizeObserver(drawEdges).observe(mapEl);
  drawEdges();
  if (location.hash.length > 1 && BY_ID[location.hash.slice(1)]) {
    setTimeout(function () { navigateTo(location.hash.slice(1)); }, 200);
  }
})();
  </script>
</body>
</html>
"""
