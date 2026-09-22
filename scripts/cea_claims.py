#!/usr/bin/env python3
"""Command-line scripts for the CEA skills: extract-claims and site.

  check-env                          check for Python 3.10 or newer and pdftotext
  extract PDF [--out DIR] [--id ID]  write DIR/<paper_id>/text.txt, the paper's text with page markers
  validate PAPER_DIR                 check PAPER_DIR/claims.json against PAPER_DIR/text.txt
  render PAPER_DIR                   check claims.json, then write claims.md and claims.html
  site PAPER_DIR... [--out DIR]
                                     build a static site from several papers' records
  schema [--out FILE]                the JSON Schema of claims.json, printed or written to FILE

Marked lines start with CEA_OK, CEA_EXTRACTED, CEA_VALID, CEA_RENDERED, CEA_SITE, or CEA_SCHEMA on
success, with CEA_FAILED, CEA_INVALID, or CEA_UNRESOLVED on failure, and with CEA_WARNING for a line
that neither succeeds nor fails, such as a paper whose PDF is missing. The exit code decides, not
the first marker: render writes claims.md and prints CEA_RENDERED before failing on an open
decision. Other lines carry no marker and are meant to be read, such as validate's warning: lines
and the page count extract prints. schema without --out prints its marker to stderr, so that
stdout holds nothing but the JSON and can be piped.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Running the skill must not write __pycache__ into the skill directory.
sys.dont_write_bytecode = True

PAGE_MARKER = re.compile(r"^=== page (\d{1,6}) ===$")
# The first line `extract` writes, naming the PDF this text came out of and the digest of
# its bytes. Without it nothing tied text.txt to a paper: a record could keep one paper's
# text, name another paper's title and PDF, and publish a page whose every quote came from
# a document the footer told the reader to check against.
# The framework this plugin builds records against, and the definitions page the site
# publishes. One document, so the rules and the terms the pages show cannot drift apart.
FRAMEWORK = Path(__file__).resolve().parent.parent / "skills" / "extract-claims" \
    / "references" / "framework.md"

SOURCE_MARKER = re.compile(r"^=== paper (.+) sha256:([0-9a-f]{64}) ===$")
# The files the site writes into a paper's directory, and claims.html, which render writes beside
# a record. A paper.pdf named after one of them would be copied over it. Compared case-folded,
# because the checker's filesystem usually is.
PUBLISHED_NAMES = frozenset({"claims.json", "claims.md", "claims.html", "text.txt", "index.html"})
SOURCES = ("abstract", "contributions", "rq_answer", "conclusion", "other")
# Marks where a figure, table, footnote, or page break interrupts a quoted sentence in text.txt.
GAP = re.compile(r"\s*\[(?:\.\.\.|\u2026)\]\s*")
# cea_page mines a reason for these, so validate holds them to the same rule as `serves`.
RESULT_REF = re.compile(r"\bR\d+\b")
# How far apart, in normalized characters, the parts of a quote on either side of a gap may be. A
# gap must also start and end at a line break (see `find_quote`).
MAX_GAP = 4000

# Required and optional fields per entry type.

FIELDS = {
    "paper": ({"id", "title", "pdf", "pages"}, set()),
    "main_results": ({"id", "quote", "page", "section", "source"}, {"note", "states"}),
    "claims": ({"id", "quote", "states", "page", "section", "serves", "split_from",
                "selection_reason"}, {"note"}),
    "excluded": ({"id", "quote", "page", "section", "reason"},
                 {"states", "split_from", "duplicate_of", "breaks_down", "note"}),
}

# The type of every field in FIELDS, for the JSON Schema that `schema` writes. A field added to
# FIELDS with no entry here, or with one that constrains nothing, fails the tests, so the two
# cannot drift apart. An entry constrains with type, enum, or anyOf. Page, source and split_from
# use the latter two.
_TEXT = {"type": "string", "minLength": 1}
# validate rejects an empty list, and breaks_down may name only main results.
_REPEATS = {"type": "array", "items": {"type": "string", "pattern": r"^[RCE]\d+$"},
            "minItems": 1, "uniqueItems": True}
_BREAKS_DOWN = {"type": "array", "items": {"type": "string", "pattern": r"^R\d+$"},
                "minItems": 1, "uniqueItems": True}
PROPERTIES = {
    # paper.id names a directory in the site. Every entry id is overridden in entry() below.
    "id": {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]*$", "maxLength": 200},
    "title": _TEXT, "pdf": _TEXT,
    "pages": {"type": "integer", "minimum": 1},
    "quote": _TEXT, "states": _TEXT, "section": _TEXT,
    "reason": _TEXT, "selection_reason": _TEXT, "note": _TEXT,
    "page": {"anyOf": [{"type": "integer", "minimum": 1},
                       {"type": "string", "pattern": r"^\d+-\d+$"}]},
    "source": {"enum": list(SOURCES)},
    "serves": {"type": "array", "items": {"type": "string", "pattern": r"^R\d+$"},
               "minItems": 1, "uniqueItems": True},
    "split_from": {"anyOf": [{"type": "null"}, {"type": "string", "pattern": r"^S\d+$"}]},
    "duplicate_of": _REPEATS, "breaks_down": _BREAKS_DOWN,
}
# The id each list's entries carry, which FIELDS cannot say because it only names the field.
_ID_PREFIX = {"main_results": "R", "claims": "C", "excluded": "E"}


def schema() -> dict:
    """The shape of claims.json as JSON Schema, generated from FIELDS.

    It holds what a checker's editor can check while they edit a record by hand: which fields each
    entry has, the id patterns, and the values of `source`. The checks that need the paper, such as
    whether a quote stands on its page or whether `states` uses only words of its quote, stay in
    `validate`, because no schema can express them.
    """
    def entry(key: str) -> dict:
        required, optional = FIELDS[key]
        props = {f: dict(PROPERTIES[f]) for f in sorted(required | optional)}
        if key in _ID_PREFIX:
            props["id"] = {"type": "string", "pattern": rf"^{_ID_PREFIX[key]}\d+$"}
        return {"type": "object", "additionalProperties": False,
                "required": sorted(required), "properties": props}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "CEA claim record",
        "description": "The record that the extract-claims skill writes for one paper. "
                       "cea_claims.py validate checks everything this schema cannot.",
        "type": "object",
        "additionalProperties": False,
        "required": sorted(FIELDS),
        "properties": {"$schema": {"type": "string"},
                       "paper": entry("paper"),
                       **{k: {"type": "array", "items": entry(k)}
                          for k in ("main_results", "claims", "excluded")}},
    }

# --- Quote matching ---

_FOLD = str.maketrans({
    "∆": "Δ", "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "`": "'", "´": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "­": None,
    "\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None,
})
# A footnote number that pdftotext puts directly after a word or a punctuation mark, as in
# "revisions,13 assisted" or "media5 and". A quote may leave it out. A single digit counts only
# after a lowercase letter or punctuation, so that "GPT4" or "RQ3" keeps its digit.
_FOOTNOTE = re.compile(r"(?:(?<=[^\W\d_])\d{2}|(?<=[a-z])\d|(?<=[.,;:!?)\]\"'])\d{1,2})(?=[\s.,;:!?)\]]|$)")


def _names_a_version(text: str, start: int) -> bool:
    """Whether the digits at `start` belong to a name rather than marking a footnote.

    A quote may leave a footnote marker off, and a digit that ends a name looks like one. Read as
    a marker it could be dropped, and the quote then named "deepseek-v", "@v", "v.0" or "[R]" --
    a model, a release, a licence or a source the paper never mentions, with no way for a reader
    to tell which one. A real marker is still quoted, simply by copying the digit as `text.txt`
    prints it.
    """
    # Digits that run on into a decimal belong to the number: "v1.0", "Section 2.1".
    after = start
    while after < len(text) and text[after].isdigit():
        after += 1
    if after + 1 < len(text) and text[after] == "." and text[after + 1].isdigit():
        return True
    # Walk back over the token. The extraction breaks a hyphenated name across two lines, as in
    # "deepseek-\nr1", and the token is still one name, so the walk crosses a line break that a
    # hyphen introduced.
    i = start
    while i > 0:
        c = text[i - 1]
        if c.isalnum() or c in "-.":
            i -= 1
            continue
        if c.isspace():
            j = i
            while j > 0 and text[j - 1].isspace():
                j -= 1
            if j > 0 and text[j - 1] == "-":
                i = j
                continue
        break
    token = text[i:start]
    if "-" in token:
        return True
    # A label rather than a word: "[R07]", "(P13)", "@v1". One or two letters introduced by a
    # bracket or an at sign name a source or a release, and the digits are part of the label.
    # A label such as "[R07]", "(P13)" or "@v1". In a list only the first sat against its
    # bracket, so "(R01, R02, R03)" protected R01 alone and the rest could lose their digits.
    return (bool(re.fullmatch(r"[A-Za-z]{1,2}", token)) and i > 0
            and (text[i - 1] in "[(@/" or (text[i - 1] in ",; " and _inside_a_label(text, i))))


def _inside_a_label(text: str, i: int) -> bool:
    """Whether position `i` stands inside a bracketed run that already opened with a label.

    "(R01, R02)" is one list of sources. Only the first sat against the bracket, so only the
    first was read as a label and the others could drop their digits and still be certified.
    """
    opened = text.rfind("(", max(0, i - 120), i)
    square = text.rfind("[", max(0, i - 120), i)
    start = max(opened, square)
    if start < 0:
        return False
    run = text[start + 1:i]
    return ")" not in run and "]" not in run and bool(
        re.match(r"[A-Za-z]{1,2}\d", run))
# Stands for whitespace between two digits, so that "12 34" does not match "1234".
# Stands where whitespace separated two word characters, so a boundary in text.txt is a boundary
# in the quote. Not a character either can contain.
_WORD_BREAK = "‖"
_NEGATION = re.compile(r"\b(?:not(?!\s+only\b)|no|never|neither|nor|none|without|cannot)\b|n't\b")
# A negation with the word that follows it, such as "no project" or "didn't change".
_NEGATION_PAIR = re.compile(r"\b(?:not(?!\s+only\b)|no|never|neither|nor|none|without|cannot)\s+\w+|\w+n't\s+\w+")
# Words that state a comparison, which a part of a split statement should not lose.
_COMPARISON = frozenset({"more", "less", "fewer", "higher", "lower", "greater", "larger", "smaller",
                         "better", "worse", "faster", "slower", "than", "most", "least", "only"})


# The same folding, but with the dashes left as they are, so that the dash of an IEEE abstract
# heading can be told from a hyphen that joins a word across a line break. Both tables delete the
# same characters, so positions in the two results match.
_FOLD_KEEP_DASHES = {k: v for k, v in _FOLD.items() if v != "-"}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_FOLD)


def _fold_keeping_dashes(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_FOLD_KEEP_DASHES)


def _normalized(text: str, footnotes: bool = False) -> tuple[str, list[bool], list[bool], list[int]]:
    """`text` folded for matching, with three lists that hold, for each character, whether it
    belongs to a footnote number that a quote may skip, whether a line break comes right before it,
    and its position in the folded text.

    Whitespace is dropped, because line breaks and spacing differ between text.txt and a quote.
    Whitespace separating two word characters becomes one marker instead, so that a word boundary
    is a boundary on both sides: without it "was not able" matches a page reading "was notable",
    and the quote would be certified as the paper's own words with its meaning reversed.

    A hyphen is dropped between two letters, also across a line break, because a word broken
    across lines keeps its hyphen in text.txt, and the break it spans carries no marker: the quote
    writes such a word whole. A hyphen next to a digit stays, so that a minus sign or a range such
    as 10-20 is kept."""
    text = _fold(text)
    skip = [False] * len(text)
    if footnotes:
        for m in _FOOTNOTE.finditer(text):
            # Digits after "0." or "1," are the decimals of a number, not a footnote.
            if m.start() >= 2 and text[m.start() - 1] in ".," and text[m.start() - 2].isdigit():
                continue
            if _names_a_version(text, m.start()):
                continue
            skip[m.start():m.end()] = [True] * (m.end() - m.start())
    chars: list[str] = []
    skips: list[bool] = []
    breaks: list[bool] = []
    origins: list[int] = []
    newline = False
    joined = False   # a hyphen between two letters was dropped, so the break it spans is not one
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                newline = newline or text[j] == "\n"
                j += 1
            if joined:
                joined = False
            elif chars and chars[-1].isalnum() and j < n and text[j].isalnum():
                chars.append(_WORD_BREAK)
                skips.append(False)
                breaks.append(newline)
                origins.append(j)
            i = j
            continue
        if ch == "-":
            k, j = i - 1, i + 1
            while k >= 0 and text[k].isspace():
                k -= 1
            while j < n and text[j].isspace():
                j += 1
            if k >= 0 and text[k].isalpha() and j < n and text[j].isalpha():
                # Only when the hyphen actually spans whitespace: "Abstract-Caching" joins two
                # words with nothing between them, and the next space is still a boundary.
                joined = j > i + 1
                i += 1
                continue
        for folded in ch.casefold():
            chars.append(folded)
            skips.append(skip[i])
            breaks.append(newline)
            origins.append(i)
        newline = False
        joined = False
        i += 1
    return "".join(chars), skips, breaks, origins


def normalize(text: str, footnotes: bool = False) -> tuple[str, tuple[bool, ...]]:
    """`text` folded for matching (see `_normalized`), with the footnote flags."""
    chars, skips, _, _ = _normalized(text, footnotes)
    return chars, tuple(skips)


@lru_cache(maxsize=512)
def _normalized_page(text: str) -> tuple[str, tuple[bool, ...], tuple[bool, ...], tuple[int, ...]]:
    """The page folded for matching, with the characters a quote may pass over.

    Besides footnote numbers, those are the word boundaries that the extraction invented. A paper
    setting a caption in small capitals comes out as "R EFINED D ATASET", one letter split off each
    word, and a checker quoting that caption writes it as the paper prints it.

    That shape only: a single character on one side, and capitals on both, which is what small
    capitals leave behind. Extraction splits words, it does not run two of them together, so a
    boundary that does not look like a split stands. Skipping any boundary would let a quote
    saying "notable" match a page saying "not able", and skipping every one-character boundary
    would let "Iran the experiment" match a page saying "I ran the experiment".
    """
    chars, skips, breaks, origins = _normalized(text, footnotes=True)
    # "pollution.6 However" has to read as "pollution. However" for a quote that drops the number
    # and as itself for one that keeps it, so the boundary beside the number goes with it.
    for i, ch in enumerate(chars):
        if ch == _WORD_BREAK and ((i and skips[i - 1]) or (i + 1 < len(chars) and skips[i + 1])):
            skips[i] = True
    folded = _fold(text)
    for i, ch in enumerate(chars):
        if ch != _WORD_BREAK or i == 0:
            continue
        before, after = folded[origins[i - 1]], folded[origins[i]]
        if not (before.isupper() and after.isupper()):
            continue
        left = right = 0
        while i - 1 - left >= 0 and chars[i - 1 - left].isalnum():
            left += 1
        while i + 1 + right < len(chars) and chars[i + 1 + right].isalnum():
            right += 1
        if min(left, right) <= 1:
            skips[i] = True
    return chars, tuple(skips), tuple(breaks), tuple(origins)


def _match_end(q: str, p: str, skippable: tuple[bool, ...], start: int) -> int:
    """End index in `p` of a match of `q` that begins at `p[start]`, or -1. A footnote number in
    `p` is either matched like any other text or skipped as a whole."""
    stack = [(0, start)]
    tried: set[tuple[int, int]] = set()
    while stack:
        j, k = stack.pop()
        while True:
            if j == len(q):
                return k
            if k >= len(p):
                break
            if skippable[k] and (k == 0 or not skippable[k - 1]):
                # Every landing place in the run, not only the far side of it: a quote that keeps
                # a footnote number still has to find the boundary that follows the number.
                after = k
                while after < len(p) and skippable[after]:
                    after += 1
                    # Never between two digits of one number. Leaving a footnote number out whole
                    # is what the reference allows. Dropping a digit of "[R13]" to read "[R3]"
                    # points the quote at another source and reads as the paper's own text.
                    if after < len(p) and p[after - 1].isdigit() and p[after].isdigit():
                        continue
                    if (j, after) not in tried:
                        tried.add((j, after))
                        stack.append((j, after))
            if p[k] != q[j]:
                break
            j += 1
            k += 1
    return -1


def find_quote(quote: str, page_text: str, max_gap: int = MAX_GAP) -> tuple[int, int, list[tuple[int, int]]] | None:
    """Where `quote` occurs in `page_text` (see `_find_quote`). Each `[...]` is read as a gap first
    and, where that fails, as text that the paper itself prints, one occurrence at a time."""
    markers = len(GAP.findall(quote.strip()))
    if not markers:
        return _find_quote(quote, page_text, [], max_gap)
    # Four, not six. Every reading of six markers is 64 searches, which cost more per unmatched
    # quote than the linear path below it: the exhaustive branch was dearer than the fallback it
    # gave way to. Two or more markers the paper prints itself, in one sentence, is not a case
    # these papers show.
    if markers > 4:
        # Every marker a gap, then every marker literal, then each one literal on its own. A
        # paper that prints "[...]" itself prints it once, in a quoted excerpt, so one literal
        # marker among real interruptions is the case that actually occurs. Trying only the two
        # uniform readings left such a quote unfindable past six markers, and the message then
        # told the checker to do what they had already done. This is markers + 2 searches, not
        # the 2**markers the loop below would run.
        readings = [[False] * markers, [True] * markers]
        readings += [[i == k for i in range(markers)] for k in range(markers)]
        for literal in readings:
            span = _find_quote(quote, page_text, literal, max_gap)
            if span:
                return span
        return None
    for literal in sorted(itertools.product((False, True), repeat=markers), key=sum):
        span = _find_quote(quote, page_text, list(literal), max_gap)
        if span:
            return span
    return None


def _find_quote(quote: str, page_text: str, literal: list[bool], max_gap: int = MAX_GAP) -> tuple[int, int, list[tuple[int, int]]] | None:
    """Where `quote` occurs in `page_text`, or None: its start and end, and the start and end of the
    text that each `[...]` gap skips, as positions in the folded page text. Layout differences and
    footnote numbers are allowed for. Each gap may skip at most MAX_GAP characters and must start
    and end at a line break, because a figure, table, footnote, or page break interrupts a
    sentence only between lines."""
    text = quote.strip()
    pieces, start = [], 0
    for (a, b), as_text in zip([m.span() for m in GAP.finditer(text)], literal):
        if not as_text:
            pieces.append(text[start:a])
            start = b
    pieces.append(text[start:])
    # A piece ending at a gap can end in the hyphen of a word the paper broke there. The page text
    # joins such a word, so the hyphen would never be found.
    parts = [_normalized(re.sub(r"-$", "", piece.rstrip()))[0] for piece in pieces]
    if not parts or any(not part for part in parts):
        return None
    p, skippable, breaks, origins = _normalized_page(page_text)
    folded = _fold(page_text)

    def gap_start(end: int) -> int:
        """Where a gap after a part ending at `end` starts, after any footnote number, or -1 if
        no line break follows there."""
        while end < len(p) and skippable[end]:
            end += 1
        return end if end < len(p) and breaks[end] else -1

    seen: dict[tuple[int, int], tuple[int, list[tuple[int, int]]] | None] = {}

    def rest(i: int, lo: int) -> tuple[int, list[tuple[int, int]]] | None:
        if (i, lo) in seen:
            return seen[(i, lo)]
        seen[(i, lo)] = answer = _rest(i, lo)
        return answer

    def joined(k: int) -> bool:
        """Whether a word of the page runs on across position `k` of its folded text.

        Read from the page's own characters, not from the matched form: folding drops the marks
        between words, so "Abstract\u2014Caching" reads there as one word and the sentence after the
        dash would look as though it began inside one.
        """
        if k <= 0 or k >= len(folded):
            return False
        if folded[k - 1].isalnum() and folded[k].isalnum():
            return True
        # A number runs on across its own decimal point, whichever side of it the quote stops on:
        # "0.17 to 0" and "0.17 to 0." are both the start of "0.06", and the shortened figure is a
        # different measurement.
        if folded[k - 1].isdigit() and folded[k] in ".," and folded[k + 1:k + 2].isdigit():
            return True
        if (folded[k - 1] in ".," and folded[k - 2:k - 1].isdigit()
                and folded[k].isdigit()):
            return True
        # A word the paper broke at a line end keeps its hyphen, and `_normalized` drops that
        # hyphen, so the two halves are one word of the page. A quote that stops at the hyphen
        # stops inside that word.
        rest = folded[k:]
        return folded[k - 1].isalnum() and rest[:1] == "-" and rest[1:].lstrip()[:1].isalnum()

    def skipped_after(k: int) -> bool:
        """Whether what follows position `k` of the folded page is a footnote number.

        The reference lets a quote leave out a marker attached to a word, so a quote ending at
        such a word ends at the paper's word even though a digit follows it.
        """
        # Digits after "0." or "1," are the decimals of a number, not a footnote, which is the
        # same rule `_normalized` applies when it decides what a quote may leave out. Unless a
        # sentence ends there: "released in 2023.4 The next section" is a marker on a full stop,
        # and a marker is what the reference lets a quote leave off.
        if k >= 2 and folded[k - 1] in ".," and folded[k - 2].isdigit():
            # "gpt-4.1", "v1.0", "Attribution 4.0" have this shape too, and a quote stopping at
            # "gpt-4." names a different model. A marker follows a number long enough to be a
            # count or a year, never the one or two digits a version is written with.
            run = re.search(r"(\d+)$", folded[:k - 1])
            if not run or len(run.group(1)) < 3:
                return False
            m = _FOOTNOTE.match(folded, k)
            return bool(m) and bool(re.match(r"\s+[A-Z\u201c\"(]", folded[m.end():]))
        return bool(_FOOTNOTE.match(folded, k)) and not _names_a_version(folded, k)

    def outer(start: int | None = None, end: int | None = None) -> bool:
        """The boundaries of the whole quote, which are the only ones that have to be words.

        The search is a substring search, so without this a quote may begin or end inside a page
        word: "we can" is found in a page that reads "we cannot", and the quote is certified with
        its meaning reversed.

        A `[...]` may stand inside a word, as the reference allows where a page break falls there,
        so the parts either side of a gap begin and end mid-word by design. Only the quote as a
        whole has to start and end where the paper's words do.
        """
        if start is not None and joined(origins[start]):
            return False
        if end is None:
            return True
        k = origins[end - 1] + 1
        return not joined(k) or skipped_after(k)

    def _rest(i: int, lo: int) -> tuple[int, list[tuple[int, int]]] | None:
        part = parts[i]
        pos = p.find(part[0], lo)
        while pos != -1 and pos <= lo + max_gap:
            if breaks[pos]:
                end = _match_end(part, p, skippable, pos)
                if end >= 0:
                    if i + 1 == len(parts):
                        if outer(end=end):
                            return end, [(lo, pos)]
                    # `elif`, as in the loop below: with the last part matched but its outer edge
                    # refused, this fell through and recursed one past the end of `parts`, so the
                    # next call indexed out of range. A quote whose tail was copied from a line
                    # ending at a hyphenated word break reaches it, and validate, render and site
                    # then died with a traceback and no CEA_ marker of any kind.
                    elif gap_start(end) >= 0:
                        tail = rest(i + 1, gap_start(end))
                        if tail:
                            return tail[0], [(lo, pos)] + tail[1]
            pos = p.find(part[0], pos + 1)
        return None

    first = parts[0]
    pos = p.find(first[0])
    while pos != -1:
        end = _match_end(first, p, skippable, pos)
        if end >= 0 and outer(start=pos):
            if len(parts) == 1:
                if outer(end=end):
                    return origins[pos], origins[end - 1] + 1, []
            elif gap_start(end) >= 0:
                tail = rest(1, gap_start(end))
                if tail:
                    return (origins[pos], origins[tail[0] - 1] + 1,
                            [(origins[a], origins[b]) for a, b in tail[1]])
        pos = p.find(first[0], pos + 1)
    return None


def quote_on(quote: str, page_text: str) -> bool:
    """Whether `quote` occurs in `page_text` (see `find_quote`)."""
    return find_quote(quote, page_text) is not None


_WORD = re.compile(r"[^\W_]+(?:[.,][0-9]+)*")
_GAP_IN_WORD = re.compile(r"(?<=\w)\s*\[(?:\.\.\.|…)\]\s*(?=\w)")
# The same marker with no room around it. Only such a marker stands inside a single word: with a
# space on either side it stands between two words, and closing those up would make one word of
# two.
_GAP_JOINING_A_WORD = re.compile(r"(?<=\w)\[(?:\.\.\.|…)\](?=\w)")


def _without_gaps(text: str) -> str:
    """`text` folded for comparison with its `[...]` markers dropped. A marker written inside a
    word, as in "dis[...]tinct", closes the word up. A marker standing between two words leaves
    the space, so that the two words stay two."""
    return normalize(GAP.sub(" ", _GAP_JOINING_A_WORD.sub("", text)))[0]


def _words(text: str) -> set[str]:
    """The words and numbers in `text`, casefolded, with hyphens treated as spaces."""
    return {w.casefold() for w in _WORD.findall(_fold(text).replace("-", " "))}


# A word the checker supplies to fill a place the quote elides, written in square brackets as
# scholarship writes it: "15 [projects] had ...". A bracket holding a digit is not a borrow --
# bracketing a number would be the way to launder one past the order rule.
_BORROWED = re.compile(r"\[([^\[\]\d]+?)\]")


def _states_sequence(text: str, quote: str = "") -> list[str]:
    """The words of a `states` in order, with a bracketed borrow taken out.

    The borrow is checked as one of the quote's words by the set rule that runs first; it is
    skipped here because it stands where the quote says nothing, so it has no place in the
    quote's order.

    A `[...]` marker is not a borrow, and neither is a bracket the quote itself prints: the
    reference tells the checker to copy the paper's own brackets as they stand. Counting those
    as the borrow both refused a part that spans a gap and let the paper's own "[auto/manual]"
    be moved onto another item -- the very swap this rule exists to refuse.
    """
    text = GAP.sub(" ", text)
    found = [m for m in _BORROWED.finditer(text) if m.group(0) not in quote]
    # One borrow, not several. A part fills at most one place the quote elides; bracketing a
    # second word is how the order check would be stepped around, by taking out the very word
    # that tells one reading from another.
    if len(found) == 1:
        text = text[:found[0].start()] + " " + text[found[0].end():]
    return _word_run(text)


def _word_run(text: str) -> list[str]:
    """The words of `text` in order, folded as `_words` folds them."""
    return [w.casefold() for w in _WORD.findall(_fold(text).replace("-", " "))]


def _out_of_quote_order(states: str, quote: str) -> bool:
    """Whether `states` uses the quote's words in an order the quote does not.

    `states` is a deletion from the quote, so its words run in the quote's order. Checking only
    that the words are the quote's let a record re-deal them: two measures swapped between the
    parts of one sentence, or "effectiveness varies widely" written as "effectiveness is growing
    widely", both passed and were published as the claim.

    Read against the same four readings of the quote that `_quote_words` unions, so this refuses
    nothing that the word check already accepts: the quote as written, its gaps closed, its
    line-broken words joined, and its footnote digits dropped.
    """
    joined = GAP.sub(" ", _GAP_IN_WORD.sub("", quote))
    whole = re.sub(r"-\s+", "", joined)
    readings = [_word_run(GAP.sub(" ", quote)), _word_run(joined), _word_run(whole),
                _word_run(re.sub(r"(?<=[^\W\d_])\d{1,2}\b", "", joined))]
    part = _states_sequence(states, quote)
    before, after = _around_the_borrow(states, quote)
    return not any(_in_order([w for w in part if w in set(reading)], reading)
                   and _borrow_stands_in_a_gap(before, after, reading)
                   for reading in readings)


def _around_the_borrow(states: str, quote: str) -> tuple[list[str], list[str]]:
    """The words on either side of a bracketed borrow, or two empty lists where there is none."""
    text = GAP.sub(" ", states)
    found = [m for m in _BORROWED.finditer(text) if m.group(0) not in quote]
    if len(found) != 1:
        return [], []
    # Only a borrow glued into a word, which is the shape it was added for: "[file]-level",
    # "[line]-based". A borrow standing as its own word fills a place between two words, and
    # demanding those two be immediate neighbours refused "26 [repositories] showed" over a
    # sentence reading "26 (file-based) and 30 (line-based) repositories showed" -- a real
    # sentence, and the refusal told the checker to write the bracket it had just written.
    if not (re.search(r"[\w-]$", text[:found[0].start()])
            or re.match(r"[\w-]", text[found[0].end():])):
        return [], []
    return _word_run(text[:found[0].start()]), _word_run(text[found[0].end():])


def _borrow_stands_in_a_gap(before: list[str], after: list[str], reading: list[str]) -> bool:
    """Whether the borrow fills a place the quote leaves empty.

    The reference says a borrow "stands where the quote says nothing", and the check took that on
    trust: the bracketed word was deleted before the order was read, so bracketing the one word
    that proves a `states` is out of order hid it. "Overall, [file]-level actions exhibit a higher
    addressing rate (6.5%-19.2%)" passed, over a quote saying that of hunk-level actions.

    So the borrow has to earn its place: the words it stands between must be neighbours in the
    quote, which is what "the quote says nothing there" means. A borrow at either end of `states`
    has no pair to check and is allowed, as an elided subject at the front is.
    """
    if not before or not after:
        return True
    held = set(reading)
    left = [w for w in before if w in held]
    right = [w for w in after if w in held]
    if not left or not right:
        return True
    # Every place the word before the borrow stands, followed by the word after it.
    return any(reading[i + 1] == right[0]
               for i, w in enumerate(reading[:-1]) if w == left[-1])


def _quote_words(quote: str) -> set[str]:
    """The words of a quote, also reading a gap inside a word, as in "dis[...]tinct", as no gap,
    and a word with a footnote number, as in "media5", without the number."""
    joined = GAP.sub(" ", _GAP_IN_WORD.sub("", quote))
    # The matcher joins a word broken across two lines, so "preregis-" and "tered" are one word to
    # it. Reading the quote the same way keeps the check from rejecting a word the quote holds.
    whole = re.sub(r"-\s+", "", joined)
    return (_words(GAP.sub(" ", quote)) | _words(joined) | _words(whole)
            | _words(re.sub(r"(?<=[^\W\d_])\d{1,2}\b", "", joined)))


def _negations(text: str) -> set[str]:
    return set(_NEGATION.findall(_fold(text).casefold()))


# --- Validation ---

def repeated_pages(path: Path) -> list[int]:
    """Page numbers that `text.txt` opens more than once."""
    seen, twice = set(), []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = PAGE_MARKER.match(line)
        if m:
            n = int(m.group(1))
            if n in seen and n not in twice:
                twice.append(n)
            seen.add(n)
    return twice


def _source_header(pdf: Path) -> str:
    """The line that says which PDF this text came out of, and what its bytes were."""
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    return f"=== paper {pdf.name} sha256:{digest} ===\n"


def source_of(path: Path) -> tuple[str, str] | None:
    """The PDF name and digest `text.txt` records, or None where it records neither."""
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            found = SOURCE_MARKER.match(line.rstrip("\n"))
            return (found.group(1), found.group(2)) if found else None
    return None


def load_pages(path: Path) -> dict[int, str]:
    """The text of each page of `text.txt`, by page number.

    A second marker for a page already seen adds to it. Emptying it instead would drop text that
    is plainly there, and the validator would then tell the checker to copy a quote exactly from a
    file where it already stands exactly. `repeated_pages` reports the file so that a quote cannot
    be stitched across the join without anyone noticing.
    """
    pages: dict[int, list[str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        m = PAGE_MARKER.match(line)
        if m:
            current = int(m.group(1))
            pages.setdefault(current, [])
        elif current is not None:
            pages[current].append(line)
    return {n: "\n".join(lines) for n, lines in pages.items()}


# The characters that make text display in an order the record does not hold: the bidirectional
# marks, embeddings, overrides and isolates. A zero-width joiner or non-joiner is NOT one of them
# -- Devanagari, Persian and Arabic need them to render at all, and refusing them would refuse a
# paper's own title. A control character is refused separately, tab and newline excepted.
_REORDERS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                      "\u2066\u2067\u2068\u2069\ufeff\u200b")


def _unshowable(text: str) -> str:
    """A character that makes the page show something other than what the record holds.

    A right-to-left override reverses the digits after it, so a note reading "19.2%" in the
    record displays as "2.91%" on the page, and a diff of the two records shows no difference. A
    zero-width space or joiner hides inside a word. Neither survives `_fold`, so no quote can
    hold one and still match, and no real field holds one: 0 of the 19,995 strings in the records
    to hand. They have no business in a field that is published as the paper's own words.
    """
    for c in text:
        if c in _REORDERS or (unicodedata.category(c) == "Cc" and c not in "\t\n\r"):
            return f"U+{ord(c):04X}"
    return ""


def _has_surrogate(text: str) -> bool:
    """A lone surrogate is legal JSON and survives json.loads, but cannot be written as UTF-8.

    text.txt is read as UTF-8, so no quote can hold one legitimately; left in place it crashes
    whichever command writes a file next, after that file has been truncated.
    """
    return any("\ud800" <= c <= "\udfff" for c in text)


def _printable(text: str) -> str:
    """`text` with anything unwritable escaped, so that reporting a problem cannot itself raise."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _label(key: str, i: int, entry: dict) -> str:
    return f"{key}[{i}] ({entry['id']})" if _nonempty(entry.get("id")) else f"{key}[{i}]"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _page_span(value, pages) -> tuple[int, int] | str:
    if isinstance(value, int) and not isinstance(value, bool):
        lo = hi = value
    elif isinstance(value, str) and re.fullmatch(r"\d{1,6}-\d{1,6}", value):
        lo, hi = map(int, value.split("-"))
        if hi != lo + 1:
            return f'"{value}" must name two consecutive pages, such as "8-9"'
    else:
        return 'must be a page number, or two consecutive pages such as "8-9"'
    missing = [n for n in range(lo, hi + 1) if n not in pages]
    if missing:
        return f"{value!r} names page {missing[0]}, which text.txt does not have"
    return lo, hi


def _page_text(pages: dict[int, str], span: tuple[int, int]) -> str:
    return "\n".join(pages[n] for n in range(span[0], span[1] + 1))


def _key(quote: str) -> str:
    """A quote folded for comparison with another quote."""
    return normalize(GAP.sub(" ", quote))[0]


def _name_missing_its_digit(quote: str, page_text: str) -> str | None:
    """A hyphenated name in the quote that the page writes with a digit after it.

    `_names_a_version` refuses such a quote, and the general message tells the checker to copy the
    wording exactly from text.txt, which is what they did. Read textually rather than by running
    the matcher again, because the folded page is cached and a matcher rerun would not see a
    changed rule.
    """
    page = _fold(page_text)
    for token in set(re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)+", _fold(quote))):
        if re.search(re.escape(token) + r"\d", page) and not re.search(
                re.escape(token) + r"(?![\w-])", page):
            return token
    return None


def _check_quote(problems: list[str], label: str, entry: dict, span: tuple[int, int],
                 pages: dict[int, str]) -> None:
    quote = entry["quote"]
    lo, hi = span
    if quote_on(quote, _page_text(pages, span)):
        # Refused, not merely questioned. A warning does not stop `render` or `site`, which do not
        # print advisories at all, so a quote welded across a section boundary reached the page.
        crossed = _skipped_heading(quote, _page_text(pages, span))
        if crossed:
            problems.append(f"{label}.quote: the [...] skips the heading \"{crossed[:60]}\", so "
                            "the quote runs across two sections and is not one sentence of the "
                            "paper; use [...] only where a figure, table, footnote, or page break "
                            "interrupts one sentence")
        else:
            # Refused rather than questioned, for the same reason: `render` and `site` print no
            # advisories and stop for none, so a quote welding two sentences together reached the
            # page with nothing said.
            if _joins_two_rows(quote, _page_text(pages, span)):
                problems.append(f"{label}.quote: starts in one row of a table and ends in "
                                "another, so it is not one sentence of the paper; a [...] stands "
                                "for a table interrupting a sentence, not for the step from one "
                                "row to the next. Quote one row, or the sentence that states it")
                return
            skipped = _skipped_prose(quote, _page_text(pages, span))
            if skipped:
                problems.append(f"{label}.quote: the [...] skips \"{skipped}\", which reads as the "
                                "paper's own running text, so the quote joins text that the paper "
                                "does not join; use [...] only where a figure, table, footnote, or "
                                "page break interrupts one sentence")
        if hi > lo:
            alone = [n for n in (lo, hi) if quote_on(quote, pages[n])]
            if alone:
                problems.append(f"{label}.page: the quote is on page {alone[0]} alone; give that page "
                                "instead of a range")
        return
    numbers = sorted(pages)
    found = [str(n) for n in numbers if quote_on(quote, pages[n])]
    if not found:
        found = [f"{n}-{n + 1}" for n in numbers
                 if n + 1 in pages and quote_on(quote, pages[n] + "\n" + pages[n + 1])]
    if found:
        problems.append(f"{label}.quote: not on page {entry['page']}, but found on page {found[0]}")
        return
    # A `[...]` may skip at most MAX_GAP characters, and a whole page can be longer than that. The
    # quote was then reported as not found, and the remedy the message gave was what the agent had
    # already done, so the only way left to reach CEA_VALID was to cut the sentence short. Say
    # which it is.
    if GAP.search(quote):
        wide = [n for n in numbers if find_quote(quote, _page_text(pages, (n, n)), 10 ** 9)]
        if not wide:
            wide = [n for n in numbers if n + 1 in pages
                    and find_quote(quote, _page_text(pages, (n, n + 1)), 10 ** 9)]
        if wide:
            problems.append(f"{label}.quote: found on page {wide[0]}, but one of its [...] skips "
                            f"more than the {MAX_GAP} characters the validator allows, counted "
                            "with spacing ignored, so text.txt shows more than that. Quote the "
                            "part of the sentence that stands unbroken, and say in `note` what "
                            "interrupts it and that the quote is the part that stands unbroken")
            return
    # "copy the wording exactly" is what the checker did, if the only difference is a digit that
    # ends a name. Retry with that guard off and say which it is.
    for n in numbers:
        cut = _name_missing_its_digit(quote, _page_text(pages, (n, n)))
        if cut:
            problems.append(f"{label}.quote: page {n} writes \"{cut}\" with a digit after it, and "
                            "the quote leaves the digit off. A digit that ends a hyphenated name "
                            "is part of the name, not a footnote marker, so copy it as text.txt "
                            "prints it")
            return
    problems.append(f"{label}.quote: not found in text.txt; copy the wording exactly from "
                    "text.txt, and use [...] only where a figure, table, footnote, or page "
                    "break interrupts it between two lines")


def validate(paper_dir: Path) -> tuple[list[str], dict | None]:
    """Problems found in paper_dir/claims.json, and the parsed data."""
    text_path, claims_path = paper_dir / "text.txt", paper_dir / "claims.json"
    if not paper_dir.is_dir():
        return [f"{paper_dir} is not a directory; name the directory `extract` wrote"], None
    if not text_path.is_file():
        return [f"{text_path} not found; run extract first"], None
    if not claims_path.is_file():
        return [f"{claims_path} not found"], None
    try:
        data = json.loads(claims_path.read_text(encoding="utf-8-sig"))
        pages = load_pages(text_path)
    except UnicodeDecodeError as e:  # a ValueError too, so it has to come first
        return [f"claims.json and text.txt must be UTF-8: {e}"], None
    except ValueError as e:  # JSONDecodeError, and the 4300-digit int limit, are both this
        return [f"claims.json is not valid JSON: {e}"], None
    except RecursionError:  # thousands of nested arrays exhaust the stack before json gives up
        return ["claims.json is nested too deeply to read"], None
    except OSError as e:
        return [f"cannot read {paper_dir}: {e}"], None
    if not isinstance(data, dict):
        return ["claims.json must hold a JSON object"], None
    problems: list[str] = []
    if not pages:
        problems.append("text.txt holds no '=== page N ===' line; run extract again")
    # `extract` writes each page once. A file that opens one twice has been edited, and a quote
    # could then run straight from one part of it into the other as though the page read that way.
    # text.txt says which PDF it came out of. A record that keeps one paper's text while naming
    # another paper's file publishes a page whose every quote comes from a document its own
    # footer tells the reader to check against. Records written before the header carry none, and
    # are left alone with a warning rather than refused.
    came_from = source_of(text_path)
    if came_from:
        named = str(data.get("paper", {}).get("pdf", "")) if isinstance(data.get("paper"), dict) else ""
        if named and Path(named).name != came_from[0]:
            problems.append(f"paper.pdf names {named!r}, but text.txt was extracted from "
                            f"{came_from[0]!r}. The page would tell a reader to check its quotes "
                            "against a paper they did not come from; run extract on the paper "
                            "this record is about")
        beside = text_path.parent / came_from[0]
        if beside.is_file():
            if hashlib.sha256(beside.read_bytes()).hexdigest() != came_from[1]:
                problems.append(f"{came_from[0]} beside the record is not the file text.txt was "
                                "extracted from; run extract again so the text and the paper agree")
    for n in repeated_pages(text_path):
        problems.append(f"text.txt opens page {n} more than once; extract writes each page once, "
                        "so run extract again rather than editing text.txt")

    for key in sorted(set(data) - set(FIELDS) - {"$schema"}):
        problems.append(f"unknown top-level field '{_printable(key)}'")
    for key in FIELDS:
        if key not in data:
            problems.append(f"missing top-level field '{key}'")

    def check_fields(key: str, entry: dict, label: str) -> None:
        required, optional = FIELDS[key]
        for f in sorted(required - set(entry)):
            problems.append(f"{label}: missing field '{f}'")
        for f in sorted(set(entry) - required - optional):
            problems.append(f"{label}: unknown field '{_printable(f)}'")

    paper = data.get("paper")
    if isinstance(paper, dict):
        check_fields("paper", paper, "paper")
        for f in ("id", "title", "pdf"):
            if f in paper and not _nonempty(paper[f]):
                problems.append(f"paper.{f}: must be a non-empty string")
        # site writes the paper's page in a directory named by this id, so it has to be one
        # plain path segment. extract folds a PDF's file name to the same rule.
        given = str(paper.get("pdf", ""))
        # A name, not a path. `extract` copies the PDF into the record and prints the bare name to
        # write here. A path was accepted as long as it was relative and held no `..`, and
        # `claims.md` then printed it to the checker, naming a file that exists on one machine.
        if given and Path(given).name != given:
            problems.append(f"paper.pdf: must be the file's name, not the path {given!r}; "
                            "`extract` copies the PDF into the record and prints the name to "
                            "write, because the site publishes that file beside the page")
        elif given and (Path(given).is_absolute() or ".." in Path(given).parts):
            problems.append("paper.pdf: must be a relative path without '..', because the site "
                            "copies the file it names into the published pages")
        if any(ord(c) < 32 for c in given):
            problems.append("paper.pdf: must not hold a control character")
        for f in ("id", "title", "pdf"):
            if isinstance(paper.get(f), str) and _has_surrogate(paper[f]):
                problems.append(f"paper.{f}: must not hold an unpaired surrogate")
            found = _unshowable(str(paper.get(f, "")))
            if found:
                problems.append(f"paper.{f}: holds {found}, which makes the page show something "
                                "other than what the record says")
        if given and not Path(given).name.strip():
            problems.append("paper.pdf: must name a file, not a directory")
        if Path(given).name.casefold() in PUBLISHED_NAMES:
            problems.append("paper.pdf: must not be named after a file the site publishes, "
                            "because the copy would overwrite it")
        # The id names a directory, and a name the filesystem refuses fails inside `site`'s
        # staging copy, where the remedy it prints is to run validate, which is what passed it.
        if _nonempty(paper.get("id")) and len(paper["id"]) > 200:
            problems.append(f"paper.id: is {len(paper['id'])} characters, and the site writes the "
                            "paper's page in a directory of that name, which the filesystem "
                            "refuses past 255; give a shorter id")
        elif _nonempty(paper.get("id")) and not PAPER_ID.fullmatch(paper["id"]):
            problems.append("paper.id: must be a plain name of letters, digits, dot, dash or "
                            "underscore, because the site writes the paper's page in a directory "
                            "named by it")
        if "pages" in paper:
            if isinstance(paper["pages"], bool) or not isinstance(paper["pages"], int):
                problems.append("paper.pages: must be a whole number")
            elif paper["pages"] != len(pages):
                problems.append(f"paper.pages: is {paper['pages']}, but text.txt has {len(pages)} pages")
    elif "paper" in data:
        problems.append("'paper' must be an object")

    lists: dict[str, list[tuple[int, dict]]] = {}
    for key in ("main_results", "claims", "excluded"):
        value = data.get(key, [])
        if not isinstance(value, list):
            problems.append(f"'{key}' must be a list")
            value = []
        lists[key] = []
        for i, e in enumerate(value):
            if isinstance(e, dict):
                lists[key].append((i, e))
            else:
                problems.append(f"{key}[{i}]: must be an object")

    owners: dict[str, str] = {}
    for key, entries in lists.items():
        for i, e in entries:
            label = _label(key, i, e)
            check_fields(key, e, label)
            if not _nonempty(e.get("id")):
                problems.append(f"{label}.id: must be a non-empty string")
            elif e["id"] in owners:
                problems.append(f"{label}.id: '{e['id']}' is also used by {owners[e['id']]}")
            else:
                owners[e["id"]] = label
                prefix = {"main_results": "R", "claims": "C", "excluded": "E"}[key]
                if not re.fullmatch(rf"{prefix}\d{{1,6}}", e["id"]):
                    problems.append(f"{label}.id: must be {prefix} and a number, as in {prefix}1; ids of "
                                    "main results start with B, claims with C, excluded claim candidates with R")
    result_ids = {e["id"] for _, e in lists["main_results"] if _nonempty(e.get("id"))}
    broad_quotes = {_key(e["quote"]): e["id"] for _, e in lists["main_results"]
                    if _nonempty(e.get("quote")) and _nonempty(e.get("id"))}
    served: set[str] = set()
    broad_pages: dict[str, list[str]] = {}
    # A main result's quote with every page that its record covers, so that an excluded claim candidate
    # on one of those pages is caught even when the main result gives a page range.
    broad_quoted: dict[str, set[int]] = {}
    excluded_quotes: dict[tuple[str, int], str] = {}
    splits: dict[str, list[tuple[str, dict]]] = {}
    # For each quote, the entries that use it: (kind, label, split_from), where kind is "results",
    # "split", "claims", or "excluded".
    uses: dict[str, list[tuple[str, str, str | None]]] = {}

    for key, entries in lists.items():
        for i, e in entries:
            label = _label(key, i, e)
            texts = ["quote", "section"] + {"claims": ["states", "selection_reason"],
                                            "main_results": ["states"],
                                            "excluded": ["reason"]}.get(key, [])
            # A lone surrogate is legal JSON, survives json.loads, and only fails when the page
            # is written. text.txt is read as UTF-8, so no quote can hold one legitimately.
            for f in list(e):
                if _has_surrogate(f) or (isinstance(e[f], str) and _has_surrogate(e[f])):
                    problems.append(f"{label}.{_printable(f)}: must not hold an unpaired surrogate")
                found = _unshowable(f) or (_unshowable(e[f]) if isinstance(e[f], str) else "")
                if found:
                    problems.append(f"{label}.{_printable(f)}: holds {found}, which makes the "
                                    "page show something other than what the record says")
            for f in texts:
                if f in e and not _nonempty(e[f]):
                    problems.append(f"{label}.{f}: must be a non-empty string")
            if "note" in e and not _nonempty(e["note"]):
                problems.append(f"{label}.note: must be a non-empty string")
            quote = e["quote"] if _nonempty(e.get("quote")) else None
            split = e.get("split_from")
            if split is not None and not _nonempty(split):
                split = None
            if quote:
                kind = "results" if key == "main_results" else "split" if split else key
                uses.setdefault((_key(quote), str(e.get("page"))), []).append((kind, label, split))
                if kind == "results":
                    broad_pages.setdefault(_key(quote), []).append(label)
                    span = _page_span(e.get("page"), pages)
                    broad_quoted.setdefault(_key(quote), set()).update(
                        range(span[0], span[1] + 1) if not isinstance(span, str) else [])
                elif kind == "excluded" and not split:
                    excluded_quotes.setdefault((_key(quote), _first_page(e)), label)
            if "page" in e:
                span = _page_span(e["page"], pages)
                if isinstance(span, str):
                    problems.append(f"{label}.page: {span}")
                elif quote:
                    _check_quote(problems, label, e, span, pages)

            if key == "main_results":
                if _nonempty(e.get("states")) and quote:
                    extra = sorted(_words(e["states"]) - _quote_words(quote))
                    if extra:
                        problems.append(f"{label}.states: uses words that are not in the quote: "
                                        f"{', '.join(extra[:5])}")
                    elif _out_of_quote_order(e["states"], quote):
                        problems.append(f"{label}.states: uses the quote's words in an order the "
                                        "quote does not. It is what the page prints as the main "
                                        "result, so its words run as the quote runs them; where "
                                        "it needs a word the quote elides, write that word in "
                                        "square brackets")
                if "source" in e and e["source"] not in SOURCES:
                    problems.append(f"{label}.source: must be one of {', '.join(SOURCES)}")
                if e.get("source") == "other" and not _nonempty(e.get("note")):
                    problems.append(f"{label}.note: a main result with source other needs a note "
                                    "that says why no summary sentence states it")

            split = e.get("split_from")
            if split is not None and not _nonempty(split):
                problems.append(f"{label}.split_from: must be null or a non-empty string")
                split = None
            if split:
                if not re.fullmatch(r"S\d+", split):
                    problems.append(f"{label}.split_from: must be S and a number, as in S1")
                if split in owners:
                    problems.append(f"{label}.split_from: '{split}' must not be the id of an entry")
                splits.setdefault(split, []).append((label, e))
                if quote and _nonempty(e.get("states")):
                    extra = sorted(_words(e["states"]) - _quote_words(quote))
                    if extra:
                        problems.append(f"{label}.states: uses words that are not in the quote: "
                                        f"{', '.join(extra[:8])}")
                    elif _out_of_quote_order(e["states"], quote):
                        problems.append(f"{label}.states: uses the quote's words in an order the "
                                        "quote does not. A part states its own part of the "
                                        "sentence, so its words run as the quote runs them; where "
                                        "it needs a word the quote elides, write that word in "
                                        "square brackets")


            if key == "claims":
                serves = e.get("serves")
                if not isinstance(serves, list) or not serves:
                    problems.append(f"{label}.serves: must list at least one main result id")
                    serves = []
                listed: set[str] = set()
                for ref in serves:
                    if not isinstance(ref, str):
                        problems.append(f"{label}.serves: must list ids as strings")
                        continue
                    if ref in listed:
                        problems.append(f"{label}.serves: '{ref}' is listed twice")
                    listed.add(ref)
                    if ref in result_ids:
                        served.add(ref)
                    else:
                        problems.append(f"{label}.serves: '{ref}' is not a main result id")
                if quote and not split:
                    same = broad_quotes.get(_key(quote))
                    if same and same not in listed:
                        problems.append(f"{label}.serves: the claim quotes the same sentence as {same}, "
                                        f"so it must serve {same}")
                # The marker drops out of both sides. A quote carrying a `[...]` inside a word
                # had no `states` the check would take: the word whole was refused, the word with
                # the marker was refused, and the word broken in two was the only thing accepted.
                if (not split and quote and _nonempty(e.get("states"))
                        and _without_gaps(quote) != _without_gaps(e["states"])):
                    problems.append(f"{label}.states: differs from the quote, but split_from is "
                                    "null; copy the quote, with or without its [...], or set "
                                    "split_from if this claim is one part of a split statement")

            if key == "excluded":
                if "duplicate_of" in e:
                    refs = e["duplicate_of"]
                    if not isinstance(refs, list) or not refs or not all(_nonempty(r) for r in refs):
                        problems.append(f"{label}.duplicate_of: must be a list of the ids of the "
                                        "claims, excluded claim candidates, or main results that the "
                                        "statement repeats")
                        refs = [r for r in refs if _nonempty(r)] if isinstance(refs, list) else []
                    for n, ref in enumerate(refs):
                        if ref in refs[:n]:
                            problems.append(f"{label}.duplicate_of: '{ref}' is listed twice")
                        if ref == e.get("id"):
                            problems.append(f"{label}.duplicate_of: an entry cannot repeat itself")
                        elif ref not in owners:
                            problems.append(f"{label}.duplicate_of: '{ref}' is not the id of a claim, "
                                            "excluded claim candidate, or main result")
                if "breaks_down" in e:
                    refs = e["breaks_down"]
                    if not isinstance(refs, list) or not refs or not all(_nonempty(r) for r in refs):
                        problems.append(f"{label}.breaks_down: must be a list of the ids of the main "
                                        "results whose result the statement breaks down")
                        refs = [r for r in refs if _nonempty(r)] if isinstance(refs, list) else []
                    for n, ref in enumerate(refs):
                        if ref in refs[:n]:
                            problems.append(f"{label}.breaks_down: '{ref}' is listed twice")
                        if ref not in result_ids:
                            problems.append(f"{label}.breaks_down: '{ref}' is not a main result id")
                    dups = e.get("duplicate_of")
                    both = {r for r in refs if r in (dups if isinstance(dups, list) else [])}
                    if both:
                        problems.append(f"{label}.breaks_down: '{sorted(both)[0]}' is named by "
                                        "duplicate_of as well; a sentence either repeats a result or "
                                        "breaks it down")
                if split and not _nonempty(e.get("states")):
                    problems.append(f"{label}.states: an excluded part of a split statement needs "
                                    "the words of that part")
                if not split and "states" in e:
                    problems.append(f"{label}.states: only an excluded part of a split statement has "
                                    "words of its own; remove them or set split_from")

    for (quote_key, page), label in excluded_quotes.items():
        same = [b for b, e in broad_quoted.items() if b == quote_key and page in e]
        if same:
            problems.append(f"{label}.quote: {broad_pages[quote_key][0]} quotes the same sentence on the "
                            "same page; a main result is not also an excluded claim candidate")
    for labels in broad_pages.values():
        if len(labels) > 1:
            problems.append(f"{labels[1]}.quote: {labels[0]} already quotes the same sentence; one "
                            "sentence is one main result, even when it states several main results")
    for users in uses.values():
        broads = [label for kind, label, _ in users if kind == "results"]
        whole = [(kind, label) for kind, label, _ in users if kind in ("claims", "excluded")]
        groups = sorted({split for kind, _, split in users if kind == "split"})
        if len(whole) > 1:
            problems.append(f"{whole[1][1]}.quote: {whole[0][1]} already quotes the same sentence; record "
                            "a sentence once, or split it into parts that share one split_from")
        if whole and groups:
            problems.append(f"{whole[0][1]}.quote: the parts of split_from '{groups[0]}' quote the same "
                            "sentence; record a sentence once, or make this entry a part of that split")
        if len(groups) > 1:
            problems.append(f"split_from '{groups[0]}' and '{groups[1]}' quote the same sentence; give "
                            "all parts of one statement the same split_from")
    for i, b in lists["main_results"]:
        if _nonempty(b.get("id")) and b["id"] not in served and not _nonempty(b.get("note")):
            problems.append(f"{_label('main_results', i, b)}.note: no claim serves this main "
                            "result; add a note that says why")

    for split, members in splits.items():
        quoted = broad_quotes.get(_key(_str(members[0][1].get("quote"))))
        serving = {ref for _, e in members
                   for ref in (e.get("serves") if isinstance(e.get("serves"), list) else [])
                   if isinstance(ref, str)}
        if quoted and quoted not in serving:
            problems.append(f"split_from '{split}': the parts quote the sentence of {quoted}, so one part "
                            f"must serve {quoted}")
        if len(members) < 2:
            problems.append(f"{members[0][0]}.split_from: '{split}' has no other part")
        if len({_key(_str(e.get("quote"))) for _, e in members}) > 1:
            problems.append(f"split_from '{split}': the parts ({', '.join(l for l, _ in members)}) "
                            "must share the same quote")
        texts = [normalize(_str(e.get("states")))[0] for _, e in members]
        if len(set(texts)) < len(texts):
            problems.append(f"split_from '{split}': two parts have the same text")
        for label, e in members:
            if _nonempty(e.get("states")) and normalize(e["states"])[0] == _key(_str(e.get("quote"))):
                problems.append(f"{label}.states: repeats the whole quote; each part of a split "
                                "statement states one part of it")

    return problems, data


_SENTENCE_END = re.compile(r"[.!?:;)\]\"']$")
# The number is followed by a stop, or by the capitalised words of a title, or by nothing.
_FIGURE_HEAD = re.compile(r"(Fig\.|Figure|FIGURE|Table|TABLE|Listing|Algorithm)\s*([\dIVXLC]+)")


def figure_labels(lines) -> list[str]:
    """The captions `extract` lists, one entry per figure or table.

    A number in the text is looked up against this list, so the two spellings of one figure have
    to fold together: "Figure 2" and "Fig. 2" are one entry, and listing both costs a reader a
    hunt for a third figure that is not there."""
    found: dict[str, None] = {}
    for line in lines:
        head = _FIGURE_HEAD.match(line.strip())
        if head:
            kind = "Fig." if head.group(1).lower().startswith("fig") else head.group(1).title()
            found.setdefault(f"{kind} {head.group(2)}", None)
    return list(found)


# Without that, running prose opening "Figure 2 shows the frequency distribution" was read as a
# caption, and one such line passed over a whole gap: two unrelated sentences could be welded
# into one certified quote with nothing said about it.
_CAPTION_START = re.compile(r"^(?:Fig\.|Figure|FIGURE|Table|TABLE|Listing|Algorithm)\s*[\dIVXLC]+(?:[.:]|\s+[A-Z]|\s*$)")


# A section heading, which a `[...]` may not stand for: that marker is for a figure, a table, a
# footnote, or a page break, and a heading means the quote runs across two sections.
#
# A heading is a label before a title: a roman numeral, a decimal number, or a single letter, then
# capitalised words. The title may be set in small capitals, which the extraction splits ("A
# NALYSES"), and may hold an ampersand. A line of capitals alone is not enough: "GPT" and "LLM"
# are words of the paper, and "TABLE V" names the very thing a [...] is for, so a caption is
# tested first.
# A numbered heading whose number already holds a dot often omits the trailing one ("6.2 RQ2:
# Existing Guidelines"), and every such subsection heading was invisible to this.
# A footnote opens with its number and then text: a capital, or a link. "15 repositories with
# a downward trend" opens with a quantity and a lowercase noun, and reading it as a marker
# waved a page of the paper's own prose through behind it.
# How many lines a caption or a footnote may run on for. Longer than this and what follows
# is the paper again, whatever punctuation the block ended with.
# Five: the longest caption or footnote in these papers runs to seven lines, and a sweep over
# the real material puts the first cap with no legitimate gap refused at five.
_CAPTION_LINES = 5
_FOOTNOTE_LINE = re.compile(
    r"^\d{1,2}\s+(?=[A-Z]|https?://|(?:[\w-]+\.)+[a-z]{2,}(?:[/\s]|$))")
_HEADING = re.compile(r"^(?:[IVXLC]+(?:-[A-Z])?\.|\d+(?:\.\d+)*\.|\d+(?:\.\d+)+|[A-Z]\.)"
                      r"\s+[A-Z][\w &,:'\u2019-]*$")
# A heading names a thing. These words mean the line is a sentence about something, which is
# what an author-affiliation footnote and a sentence opening "III. In our first round" are.
# A heading that crosses this test is refused, so a false one refuses a sound record.
# Verbs and pronouns, not articles: "Analysis of Results" and "An Overview of the Corpus" are
# ordinary headings, and listing "of", "the" or "a" made the guard refuse them, which puts the
# heading back out of sight and lets a quote weld across it.
_READS_AS_A_SENTENCE = re.compile(r"\b(?:is|are|was|were|be|been|has|have|had|with|we|our|us|their|it)\b", re.I)
# The same heading without the dot, which is how IEEE small caps come out of pdftotext:
# "9     C ONCLUSION", "8 T HREATS TO VALIDITY". `_HEADING` misses these, and the shape then
# matched the footnote test below instead, which passed over the whole block. The one thing the
# check exists to catch was the thing that switched it off.
_HEADING_NO_DOT = re.compile(r"^(?:[IVXLC]+|\d+(?:\.\d+)*)\s+(?=(?:[A-Z]\s?){4})[A-Z][A-Z \t]*$")


def _titles_a_section(line: str, indented: bool = True) -> str:
    """Whether a line that has a heading's shape reads as a heading rather than as a sentence."""
    title = line.split(None, 1)[1] if " " in line or "\t" in line else ""
    if _READS_AS_A_SENTENCE.search(title) or len(title.split()) > 9:
        return False
    # A wide run of space after the number is layout, not a heading: "1.0        Data" is a
    # figure's axis tick beside its legend title, and refusing a quote over a figure body for it
    # is refusing exactly what the marker is for. Small capitals are spaced out, so a shouting
    # line keeps its gap.
    # Only for a line that was indented on the page. A real heading stands at the left margin,
    # however wide the space after its number: "6.1               RQ1: Reasons for Mentioning
    # GenAI Tools" is a real section of a real paper, and hiding it left that gap guarded only by
    # the prose fallback, which steps aside for any gap holding a caption. The axis tick this
    # rule is for sits inside the figure, indented.
    gap = re.match(r"\S+(\s+)", line)
    return not (indented and gap and len(gap.group(1)) >= 8 and not _shouts(line))


def _skipped_prose(quote: str, page_text: str) -> str | None:
    """The running text that one of the quote's `[...]` skips, if it skips running text.

    `record-format.md` says never to use `[...]` to shorten a sentence, and nothing enforced it.
    A gap standing over the paper's own prose welds two unrelated sentences into one certified
    quote, which is the whole of what the published record promises cannot happen.
    """
    span = find_quote(quote, page_text)
    if span is None:
        return None
    text = _fold(page_text)
    for a, b in span[2]:
        rest = _without_the_figures(text[a:b])
        prose = [l for l in rest if _reads_like_prose(l)]
        # A footnote block is all marker lines and their continuations. One prose line left over
        # is that continuation; a page of prose behind a line that merely opens with a number is
        # not, and treating two lines as the cutoff waved those welds through.
        if not prose:
            continue
        # The paper's own text runs; a table's cells do not. Two lines of prose standing next to
        # each other are a weld however much table stands beside them, and asking only that prose
        # be nearly all of what is left let a gap carrying eight table rows and two sentences
        # pass. Asking merely for two prose lines anywhere refused a real 36-line codebook table
        # whose widest three cells read as prose, so what is asked for is a run.
        run = longest = 0
        for line in rest:
            run = run + 1 if _reads_like_prose(line) else 0
            longest = max(longest, run)
        if longest >= 2 or len(prose) >= len(rest) - 1:
            words = " ".join(rest)
            return words if len(words) <= 90 else words[:90] + "..."
    return None


def _without_the_figures(skipped_text: str) -> list[str]:
    """The lines a `[...]` skips, with each caption and each footnote block taken out.

    A caption used to exempt the whole gap, so a quote could skip a caption and the paper's own
    running text beside it and say nothing. A caption's continuation lines read as ordinary prose
    by every test available here, so the block runs from the caption's first line to the next
    blank line rather than being matched line by line.
    """
    out: list[str] = []
    left, shouting = 0, False
    for raw in skipped_text.splitlines():
        line = raw.strip()
        if not line:
            left = 0
            continue
        if _CAPTION_START.match(line) or _FOOTNOTE_LINE.match(line):
            # A caption set in capitals carries full stops inside it, because the extraction
            # splits its small caps ("CODE . T HE FULL"), so the stop cannot end the block there.
            # The case change and the count end it instead.
            shouting = _shouts(line)
            left = 0 if (not shouting and _stands_alone(line)) else _CAPTION_LINES
            continue
        if left and _shouts(line) == shouting:
            # A caption or a footnote is one sentence, so the block ends where that sentence
            # ends. Waiting for a blank line swallowed the paragraph after a caption that had
            # none, which is most of them in extracted text; waiting only for the full stop
            # swallowed it after a caption set in capitals that never printed one; and a line
            # count either cut a long caption short or ate the prose after a short one. A
            # caption set in capitals runs on in capitals, and the paper resumes in mixed case,
            # so the case is what says where the block ends. The count only bounds the damage.
            left = 0 if (not shouting and _stands_alone(line)) else left - 1
            continue
        left = 0
        out.append(line)
    return out


def _stands_alone(line: str) -> bool:
    """Whether a caption or footnote line is finished, so nothing after it belongs to the block.

    A full stop ends it, and so does a link: a footnote that gives a URL is complete on its own,
    and a budget to run on let it swallow the paper's own sentence behind it.

    This was measured, put back, and taken again. The first measurement said it refused fifteen
    legitimate gaps to catch twenty-three welds, but all fifteen came from generated cases whose
    blocks had the paper's prose swept into them, so they were welds scored as legitimate. With
    the cases built honestly the trade reverses.
    """
    if line[-1] in ".?!":
        return True
    # The last word is a link: "1 merriam-webster.com/slang/slop" ends at a path, not at the
    # domain, so the domain has to be looked for inside the word rather than at the line's end.
    last = line.split()[-1] if line.split() else ""
    return bool(re.match(r"(?:https?://|(?:[\w-]+\.)+[a-z]{2,}[/,.]?)", last)) and "/" in last


def _shouts(line: str) -> bool:
    """Whether a line is set in capitals, as small-caps captions come out of the extraction."""
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= len(letters) * 0.7


def _joins_two_rows(quote: str, page_text: str) -> bool:
    """Whether a gapped quote starts in one row of a table and ends in another.

    A `[...]` stands for a table interrupting one sentence. It cannot also carry a quote from one
    row of that table to another: two rows are two records, and welding them reads as one. The
    case is real -- one project's policy joined to another's, published under the table's own
    section, attributing to the first what the second wrote.

    Read from the ends rather than from what is skipped, because a row is not the paper's running
    text and never will be: `_reads_like_prose` rules out any line laid out in cells, and making
    it do otherwise refuses every legitimate quote that steps over a table.
    """
    span = find_quote(quote, page_text)
    if not span or not span[2]:
        return False
    text = _fold(page_text)

    def row_at(pos: int) -> str:
        start = text.rfind("\n", 0, pos) + 1
        end = text.find("\n", pos)
        return text[start:end if end != -1 else len(text)]

    return (len(_CELL_RUN.findall(row_at(span[0]))) >= 2
            and len(_CELL_RUN.findall(row_at(span[1] - 1))) >= 2)


def _skipped_heading(quote: str, page_text: str) -> str | None:
    """The first section heading that one of the quote's `[...]` skips, if any.

    A sentence does not cross a section heading, so a quote that skips one is not one sentence of
    the paper. It reads as though the paper says in one breath what it says in two places, which
    is what the published record promises it never does.
    """
    span = find_quote(quote, page_text)
    if span is None:
        return None
    text = _fold(page_text)
    for a, b in span[2]:
        raw = [l for l in text[a:b].splitlines() if l.strip()]
        lines = [l.strip() for l in raw]
        # A figure's y-axis tick can land on the same extracted line as its legend title, and
        # "1.0                Data" then has a numbered heading's exact shape. A gap holding a
        # caption is a figure or a table, which is what the marker is for, so it is passed over
        # here just as `_skipped_prose` passes it over.
        if any(_CAPTION_START.match(l) for l in lines):
            continue
        for source, line in zip(raw, lines):
            if _HEADING.match(line) and _titles_a_section(line, source[:1].isspace()):
                return line
            # "1     TOTAL     COUNT     SHARE" has the shape of a numbered small-caps heading and
            # is a row of a table. A heading's only wide gap is the one after its number, and the
            # letter spacing of small caps is single spaces, so a run of gaps after the number is
            # what tells the two apart.
            if _HEADING_NO_DOT.match(line):
                rest = line.split(None, 1)[1] if " " in line or "\t" in line else ""
                if len(re.findall(r"\S\s{3,}\S", rest)) < 2:
                    return line
    return None


# A figure's panel label: "(a) Upward Trend with Positive Slope Change", "(Days Relative to
# Intro Commit)". Not "(1) how these tools are adopted and configured, (2) whether", which is
# running prose, so a digit in the bracket is left out on purpose.
# A run of wide gaps is how a row of cells is laid out.
_CELL_RUN = re.compile(r"\S\s{3,}\S")
_FIGURE_LABEL = re.compile(r"^\((?:[a-z]|[ivx]{1,4})\)\s|^\([^()]*\)$")


def _reads_like_prose(line: str) -> bool:
    """Whether a line skipped by a `[...]` gap looks like running text rather than a caption, a
    table row, a footnote, or a figure label."""
    # A footnote opens with its number and a space. A sentence can open with a number too ("38
    # distinct licenses we found"), so the number alone does not settle it: a footnote's number is
    # followed by a capital or by the marker's own spacing, and running prose carries on in lower
    # case. Justified text can hold a wide gap between words, so only a run of gaps reads as cells.
    return (len(line.split()) >= 4 and not _CAPTION_START.match(line)
            and not _FIGURE_LABEL.match(line)
            and len(re.findall(r"\S\s{3,}\S", line)) < 2
            and "http" not in line and not line.startswith("[references removed"))
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# A reason that gives a verdict instead of a ground: a word for how much the statement matters,
# which a checker cannot accept or overturn, or too few words to say anything. What follows the word
# decides: "Key numbers here come from cited work" names a ground, "A key number of the paper" names
# none. The grounds are those the reference lists, so the words that name them are few.
_A_GROUND = re.compile(
    r"\b(?:describ\w+|repeat\w+|cited|agreement|corpus|codebook|sample\s+size|no\s+results\s+"
    r"statement|states?\s+the\s+main\s+result|out\s+of\s+scope|comes?\s+from|came\s+from"
    # the shape the reference asks a standing reason to take, which read as a verdict without it
    r"|(?:would|does|still)\s+(?:\w+\s+){0,3}?stands?|breaks?\s+\w+\s+down|part\s+of\s+a\s+split)\b",
    re.I)
# A reason that runs on has said something, whatever word it opens with, so only a short one is read
# for a verdict.
_VERDICT_WORDS = 8
_EMPTY_REASON = re.compile(
    r"^\W*(?:this\s+is\s+|it\s+is\s+)?(?:not\s+)?(?:an?\s+|the\s+)?"
    r"(?:important|significant|relevant|interesting|major|minor|key|main|central|notable)\b"
    # Not anchored at the end: "Not a claim." was caught and "Not a claim, R1." was not, though
    # neither says why. What follows a verdict has to be a ground, which `_A_GROUND` looks for.
    r"|^\W*(?:not\s+a\s+claim|no|n/?a|none)\b"
    r"|^\W*(?:it\s+)?(?:does\s+not\s+qualify|fails\s+the\s+test|nothing\s+rests\s+on\s+it"
    r"|judged\s+not\s+to\s+be\s+one|excluded\s+from\s+the\s+set|see\s+the\s+note)\b", re.I)
_NO_MAIN_RESULT = re.compile(r"\bno main result\b", re.I)
# A note saying the table prints the parts of the sentence's value rather than the value itself.
# A reason saying the sentence repeats a number without restating the result behind it. The
# advisory above offers this as a way to settle the question, so it has to be read.
_ONLY_THE_NUMBER = re.compile(r"repeats?\s+(?:only\s+)?the\s+number|only\s+the\s+number"
                              r"|states?\s+no\s+(?:main\s+)?result|not\s+the\s+main\s+result", re.I)
# A note that says its values are the parts of the number the sentence gives. Only then is the sum
# of those values something to check. "No total" alone is not such a note: a table that prints no
# total for the quantity the sentence gives is a table whose rows do not add up to it, which is
# what the note is there to say.
# A note that names the number its values are the parts of: "the parts of the sentence's 1,203",
# "the parts of its number, 48". Only then is there a total to check the rows against.
# How many words a selection_reason needs before it can be saying how a result would fail.
# A note saying its rows do not make up the whole: they are some of them, or shares of another
# number. Then the sum is not meant to match and there is nothing to warn about.
_SAYS_SOME_ARE_MISSING = re.compile(
    r"\b(?:only\s+(?:some|part|the\s+parts?)|some\s+of\s+(?:them|these|the)|not\s+all"
    r"|do(?:es)?\s+not\s+add|shares?\s+of)\b", re.I)
_A_REASON = 5
_PARTS_OF = re.compile(r"parts? of (?:the |its )?(?:sentence's )?(?:value |number )?"
                       r"[^\d\n]{0,20}?(?<![BCRS])(\d[\d,]*(?:\.\d+)?)", re.I)
# A section heading that names the paper's answer to a research question.
_BOXED_SECTION = re.compile(r"(?:summary|answer)\s*(?:to|for)?\s*rq", re.I)
# The order the reference gives for the sentence that states a main result. Where the paper states
# two results in as many places, the kind of sentence breaks the tie.
_SOURCE_ORDER = ("rq_answer", "abstract", "contributions", "conclusion", "other")
# A heading copied whole, such as a research question spelled out in full. A question mark gives one
# away once the heading also runs long; without a question it has to run longer still, because a
# section title can be a sentence.
_A_QUESTION = 60
_LONG_SECTION = 100
# How much of their support two main results share before the checker is asked whether they
# state one main result. Below this, a paper's separate results draw the question; above it, its
# restatements of one result stop drawing it.
_SAME_RESULT = 0.5


def _sentence_warnings(label: str, quote: str, page_text: str, is_claim: bool = True) -> list[str]:
    span = find_quote(quote, page_text)
    if span is None:
        return []
    text = _fold(page_text)
    start, end, gaps = span
    out = []
    # The reference says a number that appears only inside a table or a figure is evidence for a
    # claim, not a claim, and nothing checked it: a row copied verbatim out of a table is in
    # text.txt, so the quote is found, and the lines around it end and start cleanly, so the
    # sentence-boundary checks below say nothing either. The quote's own text on the page still
    # carries the run of wide gaps that a row is laid out with, even though the quote field itself
    # is written with single spaces.
    # Only for a claim. An excluded claim candidate that quotes a table row or a figure caption is the
    # rule being followed, not broken: the row was considered and set aside, and its entry says so.
    # `text`, not `page_text`: `start` and `end` are positions in the folded text, and folding
    # expands a ligature and drops a soft hyphen, so the raw string is a different length. Sliced
    # raw, the advisory read some other part of the page and called an ordinary sentence a table.
    lines = [l for l in text[start:end].splitlines() if l.strip()]
    if is_claim and lines and not any(_reads_like_prose(l) for l in lines):
        shown = " ".join(lines[0].split())
        out.append(f"{label}.quote: \"{shown[:60]}\" reads like a row of a table rather than a "
                   "sentence; a number that stands only in a table is evidence for a claim, not a "
                   "claim, so quote the sentence that states it")
    head = text[:start]
    before = head.rstrip()
    gap = head[len(before):]
    first = quote.lstrip()[:1]
    paragraph = "\n\n" in re.sub(r"[ \t]+", "", gap)
    if before:
        # A hyphen that joins a word across a line break, as in "non-" and "significant", also
        # leaves the quote starting inside a word.
        joined = _fold_keeping_dashes(page_text)[:start].rstrip()
        if ((not gap and before[-1].isalnum())
                or (joined.endswith("-") and joined[-2:-1].isalpha() and not paragraph
                    and not first.isupper())):
            out.append(f"{label}.quote: starts in the middle of a word or number")
        # A blank line before the quote, or a line break before a capital letter, usually follows
        # a heading, so neither counts.
        elif ((before[-1].islower() or before[-1] in ",;")
              and not paragraph
              and ("\n" not in gap or not first.isupper())):
            out.append(f"{label}.quote: starts in the middle of a sentence; quote the whole sentence")
    tail = text[end:]
    after = tail.lstrip()
    if after and not _SENTENCE_END.search(quote.rstrip()):
        footnote = quote.rstrip()[-1:].isalpha() and re.match(r"\d{1,2}(?:\s|$)", tail)
        if tail == after and re.match(r"\w|[.,]\d", after) and not footnote:
            out.append(f"{label}.quote: ends in the middle of a word or number")
        elif after[0].islower() or after[0] in ",;":
            out.append(f"{label}.quote: ends in the middle of a sentence; quote the whole sentence")
    # What a `[...]` skips is judged in `_skipped_prose` and `_skipped_heading`, which
    # refuse the quote outright. A second copy of that judgement here could only drift
    # from it, and anything it would say has already stopped the record before advisories
    # run at all.
    return out


# Words that tell one title from another: the short and the very common ones do not.
_TITLE_STOP = frozenset("the a an of and in on for to with is are how do does not yet it its"
                        " their from by at as that this we our".split())


def _title_words(text: str) -> set[str]:
    """The words of a title that would show it is the wrong paper."""
    return {w for w in re.findall(r"[^\W\d_]{3,}", _fold(text).casefold())
            if w not in _TITLE_STOP}


# `(?![A-Za-z])`: without it the first letter of an unnumbered heading reads as a roman
# numeral, so "Limitations" was section "L" and warned while "Discussion" stayed silent.
_SECTION_NUMBER = re.compile(r"\s*([IVXLC]+(?:-[A-Z])?|\d+(?:\.\d+)*)(?![A-Za-z])")


def _section_number(title: str) -> list[str]:
    """The number a section heading opens with, as its parts."""
    found = _SECTION_NUMBER.match(title.strip())
    return found.group(1).split(".") if found else []


def _top_number(title: str) -> str:
    """The number of the top-level section a heading or a `section` field names.

    "IV-B Coding Procedure" and "5.2 Results" belong to sections IV and 5.
    """
    number = _section_number(title)
    return re.split(r"[-.]", number[0])[0] if number else ""


def _nested(a: list[str], b: list[str]) -> bool:
    """Whether one section number stands inside the other: 6.1 is part of 6."""
    if not a or not b:
        return False
    depth = min(len(a), len(b))
    return a[:depth] == b[:depth]


def _letters(text: str) -> str:
    """A heading reduced to its letters, so that small caps split by the extraction fold away."""
    return re.sub(r"[^a-z]", "", _fold(text).casefold())


def _headings_of(pages: dict[int, str]) -> list[tuple[int, int, str]]:
    """Every heading of the paper, with the page and the offset on it where it starts."""
    out = []
    for n in sorted(pages):
        offset = 0
        for raw in pages[n].splitlines():
            line = raw.strip()
            if line and ((_HEADING.match(line) and _titles_a_section(line, raw[:1].isspace()))
                         or _HEADING_NO_DOT.match(line)):
                out.append((n, offset, line))
            offset += len(raw) + 1
    return out


def _sentence_around(note: str, found: re.Match) -> str:
    """The sentence of `note` that holds `found`.

    The loop that reads a note's wholes is scoped to one whole each; the hedge that lets a note
    say its rows are incomplete was not, and searched the whole note. A note that hedged one
    quantity honestly then silenced a second quantity whose arithmetic really was wrong.
    """
    start = note.rfind(".", 0, found.start()) + 1
    stop = note.find(".", found.end())
    return note[start:stop + 1 if stop != -1 else len(note)]


def _in_order(part: list[str], whole: list[str]) -> bool:
    rest = iter(whole)
    return all(any(x == y for y in rest) for x in part)


def advisories(paper_dir: Path, data: dict) -> list[str]:
    """Likely mistakes in a valid record that the checker should look at."""
    pages = load_pages(paper_dir / "text.txt")
    out: list[str] = []
    # The title is what the page, its tab, claims.md and the index all name the paper by, and
    # nothing compared it with the paper. A fabricated title publishes an invented conclusion over
    # a correct paper's correct quotes. Measured across the 30 records to hand, every real title
    # shares every one of its content words with page 1; a fabricated one shared 62%, so this
    # fires with a wide margin either way. A warning, not a refusal: a title can legitimately sit
    # in a logo or a figure the extraction does not keep.
    paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    title = str(paper.get("title", ""))
    first = load_pages(paper_dir / "text.txt").get(1, "")
    # The matcher, not a share of the title's words. A paper prints its title on page 1, and all
    # 30 records to hand hold theirs verbatim there. The word count could never see a negation:
    # "not" and "no" are stop words, so "Does AI Code Review Lead to Code Changes?" and "AI Code
    # Review Does Not Lead to Code Changes" have the same content words, and the page's largest
    # line could be made to say the opposite of the paper with nothing said about it.
    # The matcher reads a sentence; a title block is laid out differently, and a title set in
    # small capitals comes out letter-spaced ("S ELF-A DMITTED"), which the matcher joins only
    # where no hyphen or word merge is in the way. Letters alone settle those: a title whose
    # letters run through page 1 is this paper's however it is set. This can only quieten a false
    # warning -- a negated or fabricated title has different letters.
    shown = _letters(title) and _letters(title) in _letters(first)
    if title and first and not shown and not quote_on(title, first):
        out.append("paper.title: page 1 of text.txt does not print this title. The page, its tab, "
                   "claims.md and the index all name the paper by it, so copy it from text.txt "
                   "as the paper prints it")
    # `section` and `source` are the page's statement of where in the paper a result stands, and
    # `source` also orders the Claim Map. Nothing compared either with the paper. A sentence from
    # Threats to Validity, labelled as the conclusion, published a self-defence with the authority
    # of the paper's own conclusion -- and a reader checking the quote finds it verbatim and is
    # reassured. This says so only when the record names some OTHER heading of the paper and not
    # the one the quote stands under, and passes over a section nested in it, which is how a
    # caption or a subsection is properly named. Measured: 1 of 1,800 real entries, a figure
    # caption the reference expressly allows.
    pages = load_pages(paper_dir / "text.txt")
    heads = _headings_of(pages)
    if heads:
        titles = [(_letters(h[2]), h) for h in heads]
        # The number a section opens with has to be one the paper has. Writing a roman-numbered
        # paper's section as "8 Conclusion" walked past the check below, which only looks at the
        # words: no heading of that paper is a substring of "conclusion", so nothing was said.
        numbered = {_top_number(h[2]) for h in heads} - {""}
        for key in ("main_results", "claims", "excluded"):
            for i, e in enumerate(data[key]):
                said = _top_number(str(e.get("section", "")))
                if said and numbered and said not in numbered:
                    out.append(f"{_label(key, i, e)}.section: the paper has no section {said}; "
                               f"its sections are numbered {', '.join(sorted(numbered)[:6])}")
        for key in ("main_results", "claims", "excluded"):
            for i, e in enumerate(data[key]):
                said = _letters(str(e.get("section", "")))
                quote = str(e.get("quote", ""))
                if not said or not quote:
                    continue
                stands = None
                for n in sorted(pages):
                    at = find_quote(quote, pages[n])
                    if at:
                        before = [h for h in heads if h[0] < n or (h[0] == n and h[1] <= at[0])]
                        stands = max(before, key=lambda h: (h[0], h[1])) if before else None
                        break
                if stands is None or _letters(stands[2]) in said:
                    continue
                if _nested(_section_number(str(e["section"])), _section_number(stands[2])):
                    continue
                if any(t and t in said for t, _h in titles):
                    out.append(f"{_label(key, i, e)}.section: the quote stands under "
                               f"\"{stands[2][:46]}\", and this names another heading of the "
                               "paper. The page prints it as where the result stands")

    # The check that `paper.pdf` names the paper the text came from is gated on this header, so
    # without it the check does not run and nothing said so. Every record written before the
    # header has none, which is every record the published site holds.
    if source_of(paper_dir / "text.txt") is None:
        out.append("text.txt records no source paper, so paper.pdf could not be checked against "
                   "it and the page's footer names a document nothing verified. Run extract "
                   "again to record it")
    seen: set[tuple[str, str]] = set()
    # The skill asks a reason to name the statement that would still stand, and the page shows
    # the ones it recognises. A name the record does not hold is ordinary prose as far as the
    # page is concerned, which is right for a token that only looks like an id and wrong for a typo, so it is asked
    # about rather than refused.
    results = {b["id"] for b in data["main_results"] if isinstance(b.get("id"), str)}
    named = _str(data.get("paper", {}).get("pdf")) if isinstance(data.get("paper"), dict) else ""
    # Compared case-folded, and found case-folded, because the checker's filesystem usually is.
    strays = sorted(p.name for p in paper_dir.iterdir()
                    if p.suffix.casefold() == ".pdf"
                    and p.name.casefold() != Path(named).name.casefold())
    if strays and named:
        out.append(f"paper.pdf names {Path(named).name!r}, and {', '.join(strays)} "
                   "also stands here. The page says every quote was checked against the paper "
                   "this names, so make sure it is the one text.txt was extracted from.")

    for key in ("claims", "excluded"):
        for i, e in enumerate(data[key]):
            for f in ("reason", "selection_reason"):
                for ref in sorted(set(RESULT_REF.findall(_str(e.get(f)))) - results):
                    out.append(f"{_label(key, i, e)}.{f}: names '{ref}', which is not a main-result "
                               "statement in this record. The page passes over it. Correct it "
                               "if it was meant as an id.")
    for key in ("main_results", "claims", "excluded"):
        for i, e in enumerate(data[key]):
            span = _page_span(e["page"], pages)
            if isinstance(span, str) or (e["quote"], str(e["page"])) in seen:
                continue
            seen.add((e["quote"], str(e["page"])))
            out += _sentence_warnings(_label(key, i, e), e["quote"], _page_text(pages, span),
                                      is_claim=key == "claims")
    for i, e in enumerate(data["claims"]):
        if not any(re.search(rf"\b{re.escape(ref)}\b", e["selection_reason"]) for ref in e["serves"]):
            out.append(f"{_label('claims', i, e)}.selection_reason: names none of the main results in "
                       "serves; say which main result would fail and how")
        # Naming the statement is half of it. The reference asks the reason to say how the result
        # would fail without this claim, and that is the ground for putting it in the chain the
        # pages publish, so a reason of an id and nothing else says nothing a checker can weigh.
        elif len(e["selection_reason"].split()) < _A_REASON:
            out.append(f"{_label('claims', i, e)}.selection_reason: names the main result and "
                       "nothing else; say how that main result would fail without this claim")
    for i, e in enumerate(data["excluded"]):
        reason = e["reason"]
        named = e.get("duplicate_of") or e.get("split_from") or re.search(r"\bR\d+\b", reason)
        if _NO_MAIN_RESULT.search(reason) and data["main_results"] and not named:
            out.append(f"{_label('excluded', i, e)}.reason: saying that no main result depends on the "
                       "candidate is the selection question answered no; name the main result that "
                       "still stands, the result that this one breaks down, or the ground that puts "
                       "the statement out of scope")
        verdict = _EMPTY_REASON.match(reason.strip())
        # The branch above has already said this reason names no statement. Falling through would
        # say it again in other words, so it is passed over here rather than warned about twice.
        if _NO_MAIN_RESULT.search(reason) and data["main_results"] and not named:
            pass
        # `breaks_down` no longer excuses the reason. Naming a statement the candidate divides
        # says what the candidate is, not on what ground it is not a claim, and the exemption
        # meant a two-word verdict published as "Breaks a main result into parts" with nothing a
        # checker could assess.
        elif (
                len(reason.split()) < 3
                or (verdict and len(reason.split()) <= _VERDICT_WORDS
                    and not _A_GROUND.search(reason[verdict.end():]))):
            out.append(f"{_label('excluded', i, e)}.reason: gives no ground a checker can assess; say "
                       "which main result still stands, by the id of its main result, or what puts "
                       "the statement out of scope")
    # A claim serves a main result, so a sentence repeating a claim repeats that statement's
    # main result. Naming the claim alone leaves the repetition out of the count of the places where
    # the paper states the result. A sentence repeating an excluded claim candidate names no statement,
    # which is why only a claim counts here.
    statements = {b["id"] for b in data["main_results"]}
    claims = {c["id"] for c in data["claims"]}
    served = {c["id"]: set(c["serves"]) & statements for c in data["claims"]}
    for i, e in enumerate(data["excluded"]):
        refs = e.get("duplicate_of") or []
        refs = refs if isinstance(refs, list) else [refs]
        wanted = set().union(*(served[r] for r in refs if r in claims)) if set(refs) & claims else set()
        if (statements and not (set(refs) & statements)
                and not wanted <= set(e.get("breaks_down") or [])
                and not _ONLY_THE_NUMBER.search(_str(e.get("reason")))):
            out.append(f"{_label('excluded', i, e)}.duplicate_of: names a claim, and every claim "
                       "serves a main result. Where the sentence restates that main result, "
                       "name the main result as well, or the record counts one place too few. "
                       "Where it repeats only the number, say so in the reason.")
    supporting = {b["id"]: {c["id"] for c in data["claims"] if b["id"] in c["serves"]}
                  for b in data["main_results"]}
    # A statement whose claims all serve another statement that more claims serve divides that
    # result instead of stating one of its own.
    breakdowns = set()
    for i, b in enumerate(data["main_results"]):
        mine = supporting[b["id"]]
        covers = [o["id"] for o in data["main_results"]
                  if o["id"] != b["id"] and mine and mine < supporting[o["id"]]]
        if covers:
            breakdowns.add(b["id"])
            # The statement that the most claims serve, so that a chain of nested statements points
            # at the one that survives rather than at each other.
            widest = max(covers, key=lambda o: (len(supporting[o]), o))
            out.append(f"{_label('main_results', i, b)}: every claim that serves it also serves "
                       f"{widest}, which more claims serve, so it may break that result down rather "
                       "than state one of its own; it keeps its record where its sentence reports "
                       "something no other main result mentions, and where its only addition is "
                       "a subgroup, an exception, an example, or a subset, record it as an excluded "
                       f"candidate with breaks_down naming {widest}")
    # The record has to say which main results the paper has, and say each one once, or a reader
    # cannot tell one result stated four ways from four results.
    for group in _one_result(data):
        if len(group) > 1 and not set(group) & breakdowns:
            out.append(f"main_results {', '.join(group)}: the same claims serve all of them, so "
                       "they read as one main result stated several times; record it once and keep "
                       "the others as excluded claim candidates with duplicate_of naming it")
    # Statements that share much of their support without sharing all of it are a question for the
    # checker, not a verdict. Each pair is asked about once, whichever order the record lists it in,
    # and a pair that the breakdown warning covers is left to that warning.
    results = _one_result(data)
    for n, one in enumerate(results):
        for two in results[n + 1:]:
            if set(one + two) & breakdowns:
                continue
            first = set().union(*(supporting[b] for b in one))
            second = set().union(*(supporting[b] for b in two))
            if not (first and second) or first <= second or second <= first:
                continue
            if len(first & second) / len(first | second) >= _SAME_RESULT:
                out.append(f"main_results {one[0]} and {two[0]}: most of the claims that "
                           "serve one serve the other as well; where they state one main result, "
                           "record it once and keep the other sentence as an excluded claim candidate with "
                           "duplicate_of naming it")
    for i, b in enumerate(data["main_results"]):
        if _BOXED_SECTION.search(b.get("section", "")) and b.get("source") != "rq_answer":
            out.append(f"{_label('main_results', i, b)}.source: the section names a boxed answer, "
                       f"so the source is rq_answer, not {b.get('source')!r}")
    # Where a note says the table prints only the parts of a value, the model is forbidden to add them
    # up, so the script does it: the sum is either the sentence's number or a discrepancy to look at.
    for key in ("main_results", "claims", "excluded"):
        for i, e in enumerate(data[key]):
            note = e.get("note") or ""
            # Only where the note itself names the number its values are the parts of, as
            # "the parts of the sentence's 1,203: 700 and 503" does. Adding up every number a
            # note holds says nothing: a note routinely names the parts of two quantities the
            # sentence pairs, and their grand total is a number no one asserted. Checked against
            # the real records, that reading was wrong every time it fired, and silent when the
            # arithmetic really was wrong, because two wrong halves can still total correctly.
            # Each whole the note names owns the values up to the next one it names. A note
            # covering two quantities gives the parts of each in turn, and reading to the end of
            # the note would add the second quantity's rows to the first quantity's total.
            wholes = list(_PARTS_OF.finditer(note))
            for n, named in enumerate(wholes):
                rest = note[named.end():wholes[n + 1].start() if n + 1 < len(wholes) else len(note)]
                # the rows that follow, with the marks of a place rather than a value taken out
                rest = re.sub(r"\([^)]*%\)|\b\d[\d,]*(?:\.\d+)?\s*%", " ", rest)
                rest = re.sub(r"\b(?:Table|Fig\.|Figure)\s*[\dIVXLC]+|\bpages?\s+[\d-]+", " ", rest)
                rest = re.sub(r"\bpp?\.?\s*\d+(?:\s*[-\u2013]\s*\d+)?\b", " ", rest, flags=re.I)
                # A row is a value the table prints. A year, a number attached to a name, and a
                # number after an equals sign are none of them, and a correct note that ends by
                # saying when the snapshot was taken was being told its rows do not add up.
                rest = re.sub(r"\b(?:19|20)\d{2}\b", " ", rest)
                rest = re.sub(r"[A-Za-z]+-?\d[\d.]*", " ", rest)
                rest = re.sub(r"=\s*\d[\d,]*(?:\.\d+)?", " ", rest)
                total = float(named.group(1).replace(",", ""))
                parts = [float(v.replace(",", ""))
                         for v in re.findall(r"\b\d[\d,]*(?:\.\d+)?\b(?!\s*%)", rest)
                         if float(v.replace(",", "")) != total]
                # A note that says the rows are only some of the whole, or are shares of another
                # number, is the note the skill asks for where a table prints the parts of a
                # value. Warning about it paid the writer to be vaguer: the way to clear the
                # warning was to stop naming what the rows were parts of.
                if (len(parts) >= 2 and abs(sum(parts) - total) > 1e-9
                        and not _SAYS_SOME_ARE_MISSING.search(_sentence_around(note, named))):
                    out.append(f"{_label(key, i, e)}.note: it names {named.group(1)} and then "
                               f"values that add up to {sum(parts):.10g}. Check the rows against "
                               "the table, and name them as printed.")

    whole = [(_label(key, i, e), e.get("section", "")) for key in ("main_results", "claims", "excluded")
             for i, e in enumerate(data[key])
             if ("?" in e.get("section", "") and len(e.get("section", "")) > _A_QUESTION)
             or len(e.get("section", "")) > _LONG_SECTION]
    if whole:
        headings = sorted({s for _, s in whole}, key=len, reverse=True)
        first = [next(label for label, s in whole if s == h) for h in headings[:3]]
        out.append(f"{len(headings)} heading{'s' if len(headings) > 1 else ''} "
                   f"{'are' if len(headings) > 1 else 'is'} copied whole into {len(whole)} "
                   f"section{'s' if len(whole) > 1 else ''}, the longest at {len(headings[0])} "
                   f"characters ({', '.join(first)}); keep the heading's number and the words that "
                   "identify it, because the section only has to lead the checker to the page")
    entries = [(_label(key, i, e), _key(e["quote"])) for key in ("main_results", "claims", "excluded")
               for i, e in enumerate(data[key])]
    for label, key in entries:
        inside = [other for other, k in entries if other != label and k != key and k in key]
        if inside:
            out.append(f"{label}.quote: contains the quote of {inside[0]}; record a sentence once, unless "
                       "this quote needs both sentences because the second one refers to the first")
    serving = {(ref, e["id"]) for e in data["claims"] for ref in e["serves"]}
    seen: dict[str, tuple[str, str, str]] = {}
    for key in ("main_results", "claims", "excluded"):
        for i, e in enumerate(data[key]):
            if e.get("split_from"):
                continue
            label, k = _label(key, i, e), _key(e["quote"])
            if k in seen:
                other_key, other_label, other_id = seen[k]
                pair = {(other_id, e["id"]), (e["id"], other_id)}
                if not pair & serving:
                    out.append(f"{label}.quote: {other_label} quotes the same sentence on another page; "
                               "record a sentence once, unless the paper prints it twice")
            else:
                seen[k] = (key, label, e["id"])
    groups: dict[str, list[dict]] = {}
    for key in ("claims", "excluded"):
        for i, e in enumerate(data[key]):
            if not e.get("split_from"):
                continue
            groups.setdefault(e["split_from"], []).append(e)
            numbers = _NUMBER.findall(_fold(e["states"]))
            if not _in_order(numbers, _NUMBER.findall(_fold(e["quote"]))):
                out.append(f"{_label(key, i, e)}.states: gives its numbers ({', '.join(numbers)}) in a "
                           "different order than the quote; check that each number stays with its item")
    for split, members in groups.items():
        quote = members[0]["quote"]
        texts = [" ".join(_fold(e["states"]).casefold().split()) for e in members]
        if re.search(r"\brespectively\b", quote, re.I) and not all(
                _note_pairs(e) for e in members):
            # The warning asks a person to check the pairing. Once every part's note records how
            # the pairing goes, the check has been made and saying it again is noise.
            out.append(f"split_from '{split}': the quote pairs items and numbers with "
                       "\"respectively\". Check that each part keeps the right pair, and write "
                       "the pairing into each part's note, naming the number that part states")
        dropped = sorted((_words(quote) & _COMPARISON) - set().union(*(_words(e["states"]) for e in members)))
        if dropped:
            out.append(f"split_from '{split}': no part keeps {', '.join(dropped)} from the quote; check "
                       "that each comparison stays whole")
        if _negations(quote) and not any(_negations(e["states"]) for e in members):
            out.append(f"split_from '{split}': the quote contains a negation "
                       f"({', '.join(sorted(_negations(quote)))}) that no part keeps; keep it unless it "
                       "belongs to a clause that is out of scope, such as a qualitative finding")
        for pair in sorted({" ".join(m.group(0).split())
                            for m in _NEGATION_PAIR.finditer(_fold(quote).casefold())}):
            if not any(pair in t for t in texts):
                out.append(f"split_from '{split}': no part keeps \"{pair}\" from the quote; check that "
                           "each negation stays with what it negates")

    # A main result's `states` is the same kind of thing as a split part's: the clause the
    # statement is recorded for, in the quote's own words. It carries the main result, so it is
    # held to the same rules. Checked only as a subset of the quote's words, "did not reduce"
    # could be recorded as "did reduce" and nothing would say so.
    for i, b in enumerate(data["main_results"]):
        said = _str(b.get("states"))
        if not said or _key(said) == _key(_str(b.get("quote"))):
            continue
        quote = _str(b.get("quote"))
        label = _label("main_results", i, b)
        dropped = sorted((_words(quote) & _COMPARISON) - _words(said))
        if dropped:
            out.append(f"{label}.states: does not keep {', '.join(dropped)} from the quote; check "
                       "that the comparison stays whole")
        # The same check the split parts get. `states` is what the page prints as the main result,
        # so a `states` that swaps two ranges between the items it compares reverses the finding
        # on the published page, and nothing said so.
        numbers = _NUMBER.findall(_fold(said))
        if not _in_order(numbers, _NUMBER.findall(_fold(quote))):
            out.append(f"{label}.states: gives its numbers ({', '.join(numbers)}) in a different "
                       "order than the quote; check that each number stays with its item")
        # Only the inversion: `states` keeps the word the quote negates and leaves the negation
        # behind, so "did not reduce" is recorded as "did reduce". A `states` that drops the
        # negated clause altogether is the ordinary case and says nothing wrong.
        text = " ".join(_fold(said).casefold().split())
        words = set(text.split())
        for pair in sorted({" ".join(m.group(0).split())
                            for m in _NEGATION_PAIR.finditer(_fold(quote).casefold())}):
            negated = pair.split()[-1]
            if negated in words and pair not in text:
                out.append(f"{label}.states: keeps \"{negated}\" from the quote without the "
                           f"\"{pair.split()[0]}\" before it, which turns the statement round; "
                           "keep the negation with what it negates")
    return out


# --- Rendering ---

def _flat(text) -> str:
    """`text` on one line, so that a line break in a field cannot break the Markdown.

    A word the paper broke across two lines is joined and its hyphen dropped, because the hyphen
    is the break and not the paper's: "man-\nagement" is one word. A hyphen the paper prints is
    kept, and only the break closes up. Dropping that one too deleted a minus sign from a
    published number -- a quote rendered as "0.96" where the paper prints "-0.96" -- and turned
    "14-day" into "14day" and "ID-1" into "ID1", in the blockquote labelled as the paper's own
    words, with nothing to show for it. `_normalized` draws the line in the same place, so what
    is published now says what the matcher matched.
    """
    joined = re.sub(r"(?<=[^\W\d_])-\s*\n\s*(?=[^\W\d_])", "", str(text))
    return " ".join(re.sub(r"-\s*\n\s*", "-", joined).split())


def _quote_block(text: str) -> list[str]:
    return [f"> {_flat(text)}"]


def _first_page(entry: dict) -> int:
    try:
        return int(str(entry.get("page")).split("-")[0])
    except ValueError:
        return 0


def _one_result(data: dict) -> list[list[str]]:
    """The main results grouped by the main result they state, each group in the order recorded.

    The reference records a main result once and keeps the sentences that repeat it as excluded
    candidates. Where that has not happened, the same result stands as several main results, and
    the claims give it away: statements that state one result are served by the same claims. Equal
    sets of claims group, and nothing else: two statements sharing part of their support may state
    one result or two, which the checker settles, and a warning asks."""
    supports = {b["id"]: {c["id"] for c in data["claims"] if b["id"] in c["serves"]}
                for b in data["main_results"]}
    groups: dict[frozenset, list[str]] = {}
    for b, claims in supports.items():
        # A statement that no claim serves shares nothing with another such statement, and the
        # reference lets it stand where its note says why, so it groups with nothing.
        groups.setdefault(frozenset(claims) if claims else b, []).append(b)
    return list(groups.values())


def _note_pairs(entry: dict) -> bool:
    """Whether an entry's note records how a "respectively" pairing goes for this part.

    A note does that by naming the number this part states, beside the item it belongs to. Asking
    for the word "respectively" instead would mean the warning clears only on one spelling of the
    answer, which nothing tells the checker, and a note saying the pairing could not be worked out
    carries that word as readily as one that gives it.

    The number has to come from `states`, the words this part is recorded for. The quote holds
    every number of the pairing, including the ones that belong to the other parts, so a note
    naming another part's number would clear the warning for this one.
    """
    note = _str(entry.get("note"))
    # A table, a figure or a page carries a number of its own, and naming where something stands
    # is not saying how the pairing goes.
    body = re.sub(r"\b(?:Table|Fig\.|Figure)\s*[\dIVXLC]+|\bpp?\.?\s*\d+|\bpages?\s+[\d-]+",
                  " ", note, flags=re.I)
    mine = set(_NUMBER.findall(_str(entry.get("states"))))
    if not mine:
        # This part carries the wordy half of the pairing ("and the majority of pull requests"),
        # so it states no number and cannot name one. A note of its own is all it can give.
        return True
    return bool(note and mine & set(_NUMBER.findall(body)))


def _stated_in(data: dict, group: list[str] | None = None) -> int:
    """How many sentences of the paper state one main result: the main results that hold it, and
    the recorded sentences that repeat any of them.

    A breakdown of a result is not a sentence stating it, and names `breaks_down` rather than
    `duplicate_of`, so it is not counted here.

    The sentences are counted, not the links to them. Where one result stands as four main results, a sentence repeating it names several of the four, and counting each statement's
    repetitions on its own would count that sentence several times over.

    A split group is one sentence too, for the same reason: its parts are pieces of one sentence
    of the paper. Counting them separately let a record say a result is stated in eight sentences
    where the paper states it in six, by recording one sentence as three parts of itself -- and
    this number sorts the Claim Map, so it also chose which result the page led with."""
    ids = set(group) if group is not None else {b["id"] for b in data["main_results"]}
    repeats = set()
    for i, r in enumerate(data["excluded"]):
        refs = r.get("duplicate_of") or []
        if ids & set(refs if isinstance(refs, list) else [refs]):
            repeats.add(r.get("split_from") or r.get("id", i))
    return len(ids) + len(repeats)


def _shared_with_other_results(data: dict, group: list[str]) -> int:
    """How many of the sentences `_stated_in` counts for this result also state another one.

    One sentence repeating two results is one sentence of the paper, so the page's headline counts
    it once while each of the two rows counts it as stating that result. This is what each row
    says about itself. It is not the size of the gap between the rows and the headline: a sentence
    stating k results adds k to the rows and 1 to the headline, so the gap is the sum of k - 1
    over those sentences, which no single row can know.
    """
    mine, others = set(group), {b["id"] for b in data["main_results"]} - set(group)
    # A split group is one sentence, exactly as `_stated_in` counts it. Counting its parts
    # separately here while counting them once there printed "stated in 2 sentences (3 shared)"
    # -- more sentences shared than the row says exist -- beside a main result, on a record
    # doing nothing wrong.
    shared = set()
    for i, r in enumerate(data["excluded"]):
        refs = r.get("duplicate_of") or []
        refs = set(refs if isinstance(refs, list) else [refs])
        if mine & refs and others & refs:
            shared.add(r.get("split_from") or r.get("id", i))
    return len(shared)


def _id_order(entry_id: str) -> tuple[str, int, str]:
    """Sort R2 before R10, as the page does, rather than as strings.

    The digit count is bounded because Python refuses to convert an integer past 4300 digits.
    """
    m = re.fullmatch(r"([A-Z]+)(\d{1,6})", entry_id or "")
    return (m.group(1), int(m.group(2)), "") if m else ("", 0, entry_id or "")


def _by_weight(data: dict) -> list[dict]:
    """The main results, the ones the paper states in most places first. The paper orders its
    own results this way. It puts no order on the claims that support one result, so neither does
    this.

    Counted by the result, not by the statement. Where one result stands as several statements,
    every sentence stating it counts towards all of them, which is the number the page and
    claims.md print beside each. Counting per statement would order the list by one number while
    printing another next to it.
    """
    group_of = {b: g for g in _one_result(data) for b in g}

    def key(b):
        source = _SOURCE_ORDER.index(b["source"]) if b["source"] in _SOURCE_ORDER else len(_SOURCE_ORDER)
        return (-_stated_in(data, group_of.get(b["id"], [b["id"]])), source,
                _first_page(b), _id_order(b["id"]))
    return sorted(data["main_results"], key=key)


def render(data: dict) -> str:
    paper, results = data["paper"], data["main_results"]
    claims, excluded = data["claims"], data["excluded"]
    out = [
        f"# Claims: {_flat(paper['title'])}",
        "",
        f"Paper `{paper['id']}` ({paper['pages']} pages, `{paper['pdf']}`): "
        f"{len(results)} main results, {len(claims)} claims, "
        f"{len(excluded)} excluded claim candidates.",
        "",
        "Generated from `claims.json` by `cea_claims.py render`. To change the record, edit "
        "`claims.json` and render again.",
        "",
        "## Main results",
        "",
    ]
    if not results:
        out += ["None recorded.", ""]
    for b in results:
        serving = [c["id"] for c in claims if b["id"] in c["serves"]]
        out += [f"### {b['id']}: {b['source']}, page {b['page']}", "", *_quote_block(b["quote"]), "",
                f"- Section: {_flat(b['section'])}",
                f"- Claims: {', '.join(serving) if serving else 'none selected'}"]
        if b.get("states") and _key(b["states"]) != _key(b["quote"]):
            out.append(f"- States: {_flat(b['states'])}")
        if b.get("note"):
            out.append(f"- Note: {_flat(b['note'])}")
        out.append("")

    out += ["## Claims", ""]
    if not claims:
        out += ["None selected.", ""]
    else:
        out += ["Grouped under each main result they serve, the result the paper states in the most "
                "places first. Within a result the claims stand in page order, because the paper puts "
                "no order on them.", ""]
    by_id = {b["id"]: b for b in data["main_results"]}
    order = {b["id"]: n for n, b in enumerate(_by_weight(data))}
    results = sorted(_one_result(data), key=lambda g: min(order[b] for b in g))
    for group in results:
        lead = min(group, key=lambda b: order[b])
        serving = sorted((c for c in claims if set(c["serves"]) & set(group)), key=_first_page)
        if not serving:
            continue
        stated = _stated_in(data, group)
        # A sentence that states two results counts under both, so these numbers add up to more
        # than the paper has sentences. The page says so beside each row, and this is the document
        # the checker reads, and the one written even when the page is withheld.
        n_shared = _shared_with_other_results(data, group)
        shared = f", {n_shared} shared with another result" if n_shared else ""
        also = f", also stated as {', '.join(b for b in group if b != lead)}" if len(group) > 1 else ""
        out += [f"### {lead} ({by_id[lead]['source']}, stated in {stated} "
                f"place{'s' if stated > 1 else ''}{shared}{also}): {_flat(by_id[lead]['quote'])}", ""]
        for c in serving:
            # What this claim carries of this result, and whether it carries another result as well.
            carries = "the only claim for this result" if len(serving) == 1 else f"1 of {len(serving)} for this result"
            elsewhere = sum(1 for g in results if g is not group and set(c["serves"]) & set(g))
            also = f", and of {elsewhere} other result{'s' if elsewhere > 1 else ''}" if elsewhere else ""
            out.append(f"- {c['id']}, page {c['page']}, {carries}{also}")
        out.append("")
    if claims:
        out += ["### Every claim", ""]
    for c in sorted(claims, key=_first_page):
        out += [f"#### {c['id']}: page {c['page']}, {_flat(c['section'])}", "", *_quote_block(c["quote"]), ""]
        if c["split_from"]:
            others = [x["id"] for x in claims + excluded
                      if x.get("split_from") == c["split_from"] and x is not c]
            out += [f"- Part: {_flat(c['states'])}",
                    f"- Split from {c['split_from']}, with {', '.join(others)}"]
        out += [f"- Serves: {', '.join(c['serves'])}",
                f"- Selection reason: {_flat(c['selection_reason'])}"]
        if c.get("note"):
            out.append(f"- Note: {_flat(c['note'])}")
        out.append("")

    out += ["## Excluded claim candidates", ""]
    if not excluded:
        out += ["None recorded.", ""]
    for r in sorted(excluded, key=_first_page):
        out += [f"### {r['id']}: page {r['page']}, {_flat(r['section'])}", "", *_quote_block(r["quote"]), ""]
        if r.get("split_from"):
            out.append(f"- Rejected part: {_flat(r.get('states') or '')} (split from {r['split_from']})")
        if r.get("duplicate_of"):
            refs = r["duplicate_of"] if isinstance(r["duplicate_of"], list) else [r["duplicate_of"]]
            out.append(f"- Repeats: {', '.join(refs)}")
        if r.get("breaks_down"):
            refs = r["breaks_down"] if isinstance(r["breaks_down"], list) else [r["breaks_down"]]
            out.append(f"- Breaks down: {', '.join(refs)}")
        out.append(f"- Reason: {_flat(r['reason'])}")
        if r.get("note"):
            out.append(f"- Note: {_flat(r['note'])}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"

# --- Commands ---

def cmd_check_env(_args) -> int:
    problems = []
    if sys.version_info < (3, 10):
        problems.append(f"Python 3.10 or newer is required, found {sys.version.split()[0]}")
    exe = shutil.which("pdftotext")
    if not exe:
        problems.append("pdftotext not found; install poppler (macOS: brew install poppler; "
                        "Debian or Ubuntu: apt-get install poppler-utils)")
    if problems:
        for p in problems:
            print(f"CEA_FAILED: {p}")
        return 3
    try:
        probe = subprocess.run([exe, "-v"], capture_output=True, text=True, errors="replace")
    except OSError as e:
        print(f"CEA_FAILED: {exe} cannot be run: {e}")
        return 3
    if probe.returncode != 0:
        print(f"CEA_FAILED: {exe} does not answer -v (exit {probe.returncode}); "
              "the poppler install is broken")
        return 3
    version = ((probe.stderr or probe.stdout).strip().splitlines() or ["pdftotext"])[0]
    print(f"CEA_OK: Python {sys.version.split()[0]}, {version}")
    return 0


def _named_path(value: str) -> str:
    """A path the caller named.

    An empty one is `.` to pathlib and falsy to an `if`, so `--out "$SITE_DIR"` with the variable
    unset would scatter the site through whatever directory the command ran in, and
    Both reported success.
    """
    if not value.strip():
        raise argparse.ArgumentTypeError("name a path; an empty one is not the current directory "
                                         "and not 'leave it out'")
    return value


def _out_dir(value: str) -> str:
    """An output directory named by the caller (see `_named_path`)."""
    return _named_path(value)


def cmd_extract(args) -> int:
    import pdf_text

    pdf = Path(args.pdf)
    if args.id is None:
        # A publisher's file name carries spaces and parentheses. The id names a directory in the
        # site, so fold it to what PAPER_ID allows rather than failing after the paper is read.
        folded = unicodedata.normalize("NFKD", pdf.stem)
        paper_id = re.sub(r"[^A-Za-z0-9._-]+", "-",
                          "".join(c for c in folded if not unicodedata.combining(c))).strip("-._")
    else:
        paper_id = args.id
    if not PAPER_ID.fullmatch(paper_id or ""):
        how = "use --id to name it" if args.id is None else "use letters, digits, dot, dash or underscore"
        print(f"CEA_FAILED: invalid paper id {paper_id!r}; {how}, because the site names the "
              "paper's directory after it")
        return 2
    if not pdf.is_file():
        print(f"CEA_FAILED: no such file: {pdf}")
        return 2
    try:
        extraction = pdf_text.extract(str(pdf))
    except RuntimeError as e:
        print(f"CEA_FAILED: {e}")
        return 3
    chars = sum(len(l.strip()) for p in extraction.pages for l in p.lines)
    if not extraction.pages or chars < 500:
        print(f"CEA_FAILED: {pdf} has no usable text ({len(extraction.pages)} pages, {chars} "
              "characters); a scanned PDF needs OCR first")
        return 3
    out_dir = Path(args.out) / paper_id
    text_path = out_dir / "text.txt"
    # A record already here belongs to whatever paper produced the text beside it. Two papers can
    # reach one id, by folding or by --id, and overwriting the text would leave the record quoting
    # a paper it was never checked against.
    if (out_dir / "claims.json").is_file() and not text_path.is_file():
        print(f"CEA_FAILED: {out_dir} holds a record but no text.txt, so the paper it was "
              "checked against is unknown. Pass a different --id, or remove that record first.")
        return 2
    if (out_dir / "claims.json").is_file():
        try:
            same = (text_path.read_text(encoding="utf-8-sig")
                    == _source_header(pdf) + pdf_text.to_text(extraction))
        except (OSError, UnicodeDecodeError) as e:
            print(f"CEA_FAILED: {out_dir} holds a record, but its text.txt cannot be read: {e}")
            return 2
        if not same:
            print(f"CEA_FAILED: {out_dir} holds a record whose text.txt is not what this PDF "
                  "extracts now. Either the record belongs to another paper, in which case pass a "
                  "different --id, or the extractor has changed, in which case re-extract there "
                  "and run validate again.")
            return 2
    if args.id is None and paper_id != pdf.stem:
        print(f"CEA_WARNING: the paper id {paper_id!r} was derived from {pdf.stem!r}")
    # The checks above catch a second paper landing on a record. Before there is a record they do
    # not run, and the text of the first paper is replaced without a word, leaving two PDFs in the
    # directory for whoever writes claims.json to choose between.
    if text_path.is_file():
        try:
            same_text = (text_path.read_text(encoding="utf-8-sig")
                         == _source_header(pdf) + pdf_text.to_text(extraction))
        except (OSError, UnicodeDecodeError):
            same_text = False
        if not same_text:
            others = sorted(p.name for p in out_dir.iterdir()
                            if p.suffix.casefold() == ".pdf" and p.name != pdf.name)
            was = f", beside {', '.join(others)}" if others else ""
            print(f"CEA_WARNING: {text_path} already held another paper's text and is being "
                  f"replaced by {pdf.name}{was}. Pass a different --id to keep both.")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        # Writing through a link replaces whatever it points at, anywhere on the machine, and
        # leaves the record holding a link rather than its own file.
        if text_path.is_symlink():
            print(f"CEA_FAILED: {text_path} is a symbolic link; move it out of the way, because "
                  "writing through it would replace the file it points at")
            return 2
        text_path.write_text(_source_header(pdf) + pdf_text.to_text(extraction),
                             encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {text_path}: {e}")
        return 2
    # The record is meant to be complete on its own: the site publishes the paper beside the page
    # only from here, and a zip of this directory is what a reader is given. The PDF stays where
    # the user keeps it, and this is a copy.
    copied = pdf.name
    dest = out_dir / pdf.name
    if pdf.name.casefold() in PUBLISHED_NAMES:
        copied = ""
        print(f"CEA_WARNING: {pdf.name} is the name of a file the site publishes, so the paper "
              "was not copied here; rename it and extract again to have the page link it")
    elif dest.is_symlink() or dest.is_dir():
        copied = ""
        kind = "a symbolic link" if dest.is_symlink() else "a directory"
        print(f"CEA_WARNING: {dest} is {kind}, so the paper was not copied here; move it out of "
              "the way and extract again to have the page link the paper")
    else:
        try:
            if pdf.resolve() != dest.resolve():
                shutil.copy2(pdf, dest)
        except OSError as e:
            copied = ""
            print(f"CEA_WARNING: cannot copy {pdf} to {out_dir}: {e}; the page will name the "
                  "paper without linking it")
    # Only pages that still hold text: counting the layout before the references were removed
    # printed "7 of them with two-column text" beside "empty pages: 7, 8".
    blank = {p.number for p in extraction.pages if not any(l.strip() for l in p.lines)}
    columns = sum(1 for p in extraction.pages if p.regions and p.number not in blank)
    print(f"CEA_EXTRACTED: {text_path}")
    print(f"paper_id: {paper_id}")
    if copied:
        print(f"pdf: {copied}, copied here; write that name as paper.pdf")
    print(f"pages: {len(extraction.pages)}, {columns} of them with two-column text put in reading order")
    if extraction.unreadable:
        first = extraction.unreadable.splitlines()[0][:120]
        print(f"CEA_WARNING: pdftotext could not read all of {pdf} ({first}). Pages it could not "
              "read are missing from text.txt, so every later page carries the number of another "
              "page. Check the page count against the PDF before recording anything.")
    labels: dict[str, int] = {}
    for page in extraction.pages:
        for name in figure_labels(page.lines):
            labels.setdefault(name, page.number)
    if labels:
        print("tables and figures: " + ", ".join(f"{n} p{page}" for n, page in labels.items()))
        print("  a number in the text is looked up against this list; a caption missing from it did not "
              "survive the extraction, so nothing can be compared with it")
    single = [str(p.number) for p in extraction.pages
              if extraction.two_column and not p.regions and sum(1 for l in p.lines if l.strip()) >= 8]
    if single:
        print(f"one-column pages: {', '.join(single)} (the paper has two columns elsewhere; lines of "
              "both columns can be mixed on these pages)")
    if extraction.references is None:
        print("references: no References heading found, so any bibliography is still in text.txt")
    else:
        first, appendix = extraction.references
        if appendix:
            rest = f"up to the appendix heading on page {appendix}"
        elif extraction.resumed:
            where = "" if extraction.resumed == first else f" on page {extraction.resumed}"
            rest = (f"only to where the paper's text starts again{where}, "
                    "so a bibliography entry may be left")
        else:
            rest = "to the end"
        print(f"references: removed from page {first} {rest}")
    empty = [str(n) for n in sorted(blank)]
    if empty:
        print(f"empty pages: {', '.join(empty)} (no text left, for example after removing references)")
    if extraction.lineno:
        print("line numbers: LaTeX margin line numbers removed")
    return 0


# site writes the paper's page in a directory named by paper.id, so it is one path segment.
PAPER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

_UNSURE = re.compile(r"\bunsure\b", re.I)


def unsettled(data: dict) -> list[str]:
    """What stops a record from being published, each named by its id.

    A candidate recorded as unsure leaves the claim-or-not decision to the checker, and a reader
    cannot tell an open question from a decision. The other unfinished record, a main result
    that no claim serves and no note explains, is already a problem for the validator, so a record
    that reaches here has a note on every such statement and the page shows it as a finding.
    """
    # A claim states its reason in selection_reason. Only an excluded claim candidate has reason, so
    # reading one field alone leaves an unsettled claim on the page. The word counts wherever it
    # stands, quoted or not: every attempt to read the quoting has been wrong in the direction
    # that publishes an open question, and a reason that merely quotes the word can be reworded.
    return [f"{x['id']}: the reason does not say whether the statement is a claim"
            for x in data["claims"] + data["excluded"]
            if _UNSURE.search(" ".join(str(x.get(f) or "")
                                       for f in ("reason", "selection_reason")))]


def cmd_validate(args) -> int:
    paper_dir = Path(args.paper_dir)
    problems, data = validate(paper_dir)
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s) in {paper_dir / 'claims.json'}")
        for p in problems:
            print(f"- {p}")
        return 1
    print(f"CEA_VALID: {len(data['main_results'])} main results, {len(data['claims'])} "
          f"claims, {len(data['excluded'])} excluded claim candidates; every quote found on its page")
    for warning in advisories(paper_dir, data):
        print(f"warning: {warning}")
    return 0


def cmd_render(args) -> int:
    paper_dir = Path(args.paper_dir)
    problems, data = validate(paper_dir)
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s); run validate and fix them before rendering")
        return 1
    # Printed here too, not only by `validate`. Every check that is a warning rather than a
    # refusal was invisible on the path that actually publishes: a page could go out carrying a
    # section that names the wrong part of the paper, or a title the paper does not print, and
    # the command that wrote it said nothing.
    # CEA_WARNING, not "warning:": SKILL.md says the first output line carries a marker, and a
    # CEA_WARNING line may come first. `validate` prints the same text after its CEA_VALID line.
    for warning in advisories(paper_dir, data):
        print(f"CEA_WARNING: {warning}")
    md_path = paper_dir / "claims.md"
    html_path = paper_dir / "claims.html"
    # Writing through a link replaces whatever it points at, anywhere on the machine. `extract`
    # guards text.txt and the copied PDF this way and `site` guards everything it publishes, and
    # this was the hole in that perimeter, in the command the skill runs most often. A dangling
    # link counts: `is_file` is false for one, so the unresolved branch below would report the
    # page as never written and leave the link for the next render to write through.
    for path in (md_path, html_path):
        if path.is_symlink():
            print(f"CEA_FAILED: {path} is a symbolic link; move it out of the way, because "
                  "writing through it would replace the file it points at")
            return 2
    try:
        md_path.write_text(render(data), encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {md_path}: {e}")
        return 2
    print(f"CEA_RENDERED: {md_path}")
    problems = unsettled(data)
    if problems:
        print(f"CEA_UNRESOLVED: {len(problems)} open decision(s) to settle before a page can be written:")
        for problem in problems:
            print(f"- {problem}")
        if html_path.is_file():
            try:
                html_path.unlink()
                print(f"CEA_FAILED: {html_path} was removed, because the page it held no longer "
                      "matches the record.")
            except OSError as e:
                print(f"CEA_FAILED: {html_path} is out of date and could not be removed: {e}")
                return 2
        else:
            print(f"CEA_FAILED: {html_path} was not written.")
        return 1
    import cea_page  # imported here, because cea_page reads the ordering functions above
    try:
        html_path.write_text(cea_page.build(data, paper_dir / "claims.json", html_path),
                             encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {html_path}: {e}")
        return 2
    print(f"CEA_RENDERED: {html_path}")
    return 0


def cmd_schema(args) -> int:
    text = json.dumps(schema(), indent=2) + "\n"
    if not args.out:
        print("CEA_SCHEMA: stdout", file=sys.stderr)
        sys.stdout.write(text)
        return 0
    try:
        Path(args.out).write_text(text, encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {args.out}: {e}")
        return 2
    print(f"CEA_SCHEMA: {args.out}")
    return 0


def cmd_site(args) -> int:
    import cea_site
    records = [Path(r) for r in args.paper_dirs]
    missing = [str(r) for r in records if not (r / "claims.json").is_file()]
    if missing:
        print(f"CEA_FAILED: no claims.json in {', '.join(missing)}")
        return 2
    # The framework is this plugin's own document, not a path the caller hands in. Every claim
    # card stamps a mapping level and every footer links the page that defines it, so a site
    # without that page shows a level that nothing anywhere explains. It was optional before,
    # which built exactly that site and reported success.
    framework = FRAMEWORK
    if not framework.is_file():
        print(f"CEA_FAILED: the framework document is missing from the plugin at {framework}")
        return 2
    try:
        text = framework.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        print(f"CEA_FAILED: cannot read the framework document {framework}: {e}")
        return 2
    # What matters is what a reader sees, so the rendered page is what is measured: a document of
    # nothing but HTML comments has text in it and renders to nothing, because the comments are
    # dropped.
    if not re.sub(r"<[^>]+>", "", cea_site.md_to_html(text)).strip():
        print(f"CEA_FAILED: the framework document {framework} renders to an empty page, and "
              "every paper would link it as the page that defines the terms they use")
        return 2
    written, messages = cea_site.build_site(records, Path(args.out), framework)
    for message in messages:
        print(message)
    if not written:
        return 1
    print(f"CEA_SITE: {args.out} with {written} paper{'s' if written != 1 else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cea_claims.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-env").set_defaults(func=cmd_check_env)
    p = sub.add_parser("extract")
    p.add_argument("pdf")
    p.add_argument("--out", default="cea-out", type=_out_dir,
                   help="output directory (default: cea-out)")
    p.add_argument("--id", help="paper id (default: the PDF file name without .pdf, folded to letters, digits, dot, dash and underscore)")
    p.set_defaults(func=cmd_extract)
    for name, func in (("validate", cmd_validate), ("render", cmd_render)):
        p = sub.add_parser(name)
        p.add_argument("paper_dir")
        p.set_defaults(func=func)
    p = sub.add_parser("schema")
    p.add_argument("--out", type=_named_path,
                   help="write the JSON Schema to this file (default: print it)")
    p.set_defaults(func=cmd_schema)
    p = sub.add_parser("site")
    p.add_argument("paper_dirs", nargs="+")
    p.add_argument("--out", default="_site", type=_out_dir,
                   help="output directory (default: _site)")
    p.set_defaults(func=cmd_site)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
