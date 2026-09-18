#!/usr/bin/env python3
"""Command-line scripts for the cea-extract-claims skill.

  check-env                          check for Python 3.10 or newer and pdftotext
  extract PDF [--out DIR] [--id ID]  write DIR/<paper_id>/text.txt, the paper's text with page markers
  validate PAPER_DIR                 check PAPER_DIR/claims.json against PAPER_DIR/text.txt
  render PAPER_DIR                   check claims.json, then write PAPER_DIR/claims.md from it

The first output line starts with CEA_OK, CEA_EXTRACTED, CEA_VALID, or CEA_RENDERED on success, and
with CEA_FAILED or CEA_INVALID otherwise. Failures exit non-zero.
"""

from __future__ import annotations

import argparse
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

PAGE_MARKER = re.compile(r"^=== page (\d+) ===$")
SOURCES = ("abstract", "contributions", "rq_answer", "conclusion", "other")
# Marks where a figure, table, footnote, or page break interrupts a quoted sentence in text.txt.
GAP = re.compile(r"\s*\[(?:\.\.\.|\u2026)\]\s*")
# How far apart, in normalized characters, the parts of a quote on either side of a gap may be. A
# gap must also start and end at a line break (see `find_quote`).
MAX_GAP = 4000

# Required and optional fields per entry type.
FIELDS = {
    "paper": ({"id", "title", "pdf", "pages"}, set()),
    "broad_statements": ({"id", "quote", "page", "section", "source"}, {"note", "states"}),
    "claims": ({"id", "quote", "states", "page", "section", "serves", "split_from",
                "selection_reason"}, {"note"}),
    "rejected": ({"id", "quote", "page", "section", "reason"},
                 {"states", "split_from", "duplicate_of", "breaks_down", "note"}),
}

# --- Quote matching ---

_FOLD = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "`": "'", "´": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "­": None,
    "\u200b": None, "\u200c": None, "\u200d": None, "\ufeff": None,
})
# A footnote number that pdftotext puts directly after a word or a punctuation mark, as in
# "revisions,13 assisted" or "media5 and". A quote may leave it out. A single digit counts only
# after a lowercase letter or punctuation, so that "GPT4" or "RQ3" keeps its digit.
_FOOTNOTE = re.compile(r"(?:(?<=[^\W\d_])\d{2}|(?<=[a-z])\d|(?<=[.,;:!?)\]\"'])\d{1,2})(?=[\s.,;:!?)\]]|$)")
# Stands for whitespace between two digits, so that "12 34" does not match "1234".
_DIGIT_BREAK = "‖"
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
    Whitespace between two digits becomes one marker instead. A hyphen is dropped between two
    letters, also across a line break, because a word broken across lines keeps its hyphen in
    text.txt. A hyphen next to a digit stays, so that a minus sign or a range such as 10-20 is kept."""
    text = _fold(text)
    skip = [False] * len(text)
    if footnotes:
        for m in _FOOTNOTE.finditer(text):
            # Digits after "0." or "1," are the decimals of a number, not a footnote.
            if m.start() >= 2 and text[m.start() - 1] in ".," and text[m.start() - 2].isdigit():
                continue
            skip[m.start():m.end()] = [True] * (m.end() - m.start())
    chars: list[str] = []
    skips: list[bool] = []
    breaks: list[bool] = []
    origins: list[int] = []
    newline = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            j = i
            while j < n and text[j].isspace():
                newline = newline or text[j] == "\n"
                j += 1
            if chars and chars[-1].isdigit() and j < n and text[j].isdigit():
                chars.append(_DIGIT_BREAK)
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
                i += 1
                continue
        for folded in ch.casefold():
            chars.append(folded)
            skips.append(skip[i])
            breaks.append(newline)
            origins.append(i)
        newline = False
        i += 1
    return "".join(chars), skips, breaks, origins


def normalize(text: str, footnotes: bool = False) -> tuple[str, tuple[bool, ...]]:
    """`text` folded for matching (see `_normalized`), with the footnote flags."""
    chars, skips, _, _ = _normalized(text, footnotes)
    return chars, tuple(skips)


@lru_cache(maxsize=512)
def _normalized_page(text: str) -> tuple[str, tuple[bool, ...], tuple[bool, ...], tuple[int, ...]]:
    chars, skips, breaks, origins = _normalized(text, footnotes=True)
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
                after = k
                while after < len(p) and skippable[after]:
                    after += 1
                if (j, after) not in tried:
                    tried.add((j, after))
                    stack.append((j, after))
            if p[k] != q[j]:
                break
            j += 1
            k += 1
    return -1


def find_quote(quote: str, page_text: str) -> tuple[int, int, list[tuple[int, int]]] | None:
    """Where `quote` occurs in `page_text` (see `_find_quote`). Each `[...]` is read as a gap first
    and, where that fails, as text that the paper itself prints, one occurrence at a time."""
    markers = len(GAP.findall(quote.strip()))
    if not markers:
        return _find_quote(quote, page_text, [])
    if markers > 6:
        return (_find_quote(quote, page_text, [True] * markers)
                or _find_quote(quote, page_text, [False] * markers))
    for literal in sorted(itertools.product((False, True), repeat=markers), key=sum):
        span = _find_quote(quote, page_text, list(literal))
        if span:
            return span
    return None


def _find_quote(quote: str, page_text: str, literal: list[bool]) -> tuple[int, int, list[tuple[int, int]]] | None:
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

    def gap_start(end: int) -> int:
        """Where a gap after a part ending at `end` starts, after any footnote number, or -1 if
        no line break follows there."""
        while end < len(p) and skippable[end]:
            end += 1
        return end if end < len(p) and breaks[end] else -1

    def rest(i: int, lo: int) -> tuple[int, list[tuple[int, int]]] | None:
        part = parts[i]
        pos = p.find(part[0], lo)
        while pos != -1 and pos <= lo + MAX_GAP:
            if breaks[pos]:
                end = _match_end(part, p, skippable, pos)
                if end >= 0:
                    if i + 1 == len(parts):
                        return end, [(lo, pos)]
                    if gap_start(end) >= 0:
                        tail = rest(i + 1, gap_start(end))
                        if tail:
                            return tail[0], [(lo, pos)] + tail[1]
            pos = p.find(part[0], pos + 1)
        return None

    first = parts[0]
    pos = p.find(first[0])
    while pos != -1:
        end = _match_end(first, p, skippable, pos)
        if end >= 0:
            if len(parts) == 1:
                return origins[pos], origins[end - 1] + 1, []
            if gap_start(end) >= 0:
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


def _words(text: str) -> set[str]:
    """The words and numbers in `text`, casefolded, with hyphens treated as spaces."""
    return {w.casefold() for w in _WORD.findall(_fold(text).replace("-", " "))}


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

def load_pages(path: Path) -> dict[int, str]:
    pages: dict[int, list[str]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PAGE_MARKER.match(line)
        if m:
            current = int(m.group(1))
            pages[current] = []
        elif current is not None:
            pages[current].append(line)
    return {n: "\n".join(lines) for n, lines in pages.items()}


def _nonempty(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _label(key: str, i: int, entry: dict) -> str:
    return f"{key}[{i}] ({entry['id']})" if _nonempty(entry.get("id")) else f"{key}[{i}]"


def _str(value) -> str:
    return value if isinstance(value, str) else ""


def _page_span(value, pages) -> tuple[int, int] | str:
    if isinstance(value, int) and not isinstance(value, bool):
        lo = hi = value
    elif isinstance(value, str) and re.fullmatch(r"\d+-\d+", value):
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


def _check_quote(problems: list[str], label: str, entry: dict, span: tuple[int, int],
                 pages: dict[int, str]) -> None:
    quote = entry["quote"]
    lo, hi = span
    if quote_on(quote, _page_text(pages, span)):
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
    else:
        problems.append(f"{label}.quote: not found in text.txt; copy the wording exactly from "
                        "text.txt, and use [...] only where a figure, table, footnote, or page "
                        "break interrupts it between two lines")


def validate(paper_dir: Path) -> tuple[list[str], dict | None]:
    """Problems found in paper_dir/claims.json, and the parsed data."""
    text_path, claims_path = paper_dir / "text.txt", paper_dir / "claims.json"
    if not text_path.is_file():
        return [f"{text_path} not found; run extract first"], None
    if not claims_path.is_file():
        return [f"{claims_path} not found"], None
    try:
        data = json.loads(claims_path.read_text(encoding="utf-8-sig"))
        pages = load_pages(text_path)
    except json.JSONDecodeError as e:
        return [f"claims.json is not valid JSON: {e}"], None
    except UnicodeDecodeError as e:
        return [f"claims.json and text.txt must be UTF-8: {e}"], None
    except OSError as e:
        return [f"cannot read {paper_dir}: {e}"], None
    if not isinstance(data, dict):
        return ["claims.json must hold a JSON object"], None
    problems: list[str] = []
    if not pages:
        problems.append("text.txt holds no '=== page N ===' line; run extract again")

    for key in sorted(set(data) - set(FIELDS)):
        problems.append(f"unknown top-level field '{key}'")
    for key in FIELDS:
        if key not in data:
            problems.append(f"missing top-level field '{key}'")

    def check_fields(key: str, entry: dict, label: str) -> None:
        required, optional = FIELDS[key]
        for f in sorted(required - set(entry)):
            problems.append(f"{label}: missing field '{f}'")
        for f in sorted(set(entry) - required - optional):
            problems.append(f"{label}: unknown field '{f}'")

    paper = data.get("paper")
    if isinstance(paper, dict):
        check_fields("paper", paper, "paper")
        for f in ("id", "title", "pdf"):
            if f in paper and not _nonempty(paper[f]):
                problems.append(f"paper.{f}: must be a non-empty string")
        if "pages" in paper:
            if isinstance(paper["pages"], bool) or not isinstance(paper["pages"], int):
                problems.append("paper.pages: must be a whole number")
            elif paper["pages"] != len(pages):
                problems.append(f"paper.pages: is {paper['pages']}, but text.txt has {len(pages)} pages")
    elif "paper" in data:
        problems.append("'paper' must be an object")

    lists: dict[str, list[tuple[int, dict]]] = {}
    for key in ("broad_statements", "claims", "rejected"):
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
                prefix = {"broad_statements": "B", "claims": "C", "rejected": "R"}[key]
                if not re.fullmatch(rf"{prefix}\d+", e["id"]):
                    problems.append(f"{label}.id: must be {prefix} and a number, as in {prefix}1; ids of "
                                    "broad statements start with B, claims with C, rejected candidates with R")
    broad_ids = {e["id"] for _, e in lists["broad_statements"] if _nonempty(e.get("id"))}
    broad_quotes = {_key(e["quote"]): e["id"] for _, e in lists["broad_statements"]
                    if _nonempty(e.get("quote")) and _nonempty(e.get("id"))}
    served: set[str] = set()
    broad_pages: dict[str, list[str]] = {}
    # A broad statement's quote with every page that its record covers, so that a rejected candidate
    # on one of those pages is caught even when the broad statement gives a page range.
    broad_quoted: dict[str, set[int]] = {}
    rejected_quotes: dict[tuple[str, int], str] = {}
    splits: dict[str, list[tuple[str, dict]]] = {}
    # For each quote, the entries that use it: (kind, label, split_from), where kind is "broad",
    # "split", "claims", or "rejected".
    uses: dict[str, list[tuple[str, str, str | None]]] = {}

    for key, entries in lists.items():
        for i, e in entries:
            label = _label(key, i, e)
            texts = ["quote", "section"] + {"claims": ["states", "selection_reason"],
                                            "rejected": ["reason"]}.get(key, [])
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
                kind = "broad" if key == "broad_statements" else "split" if split else key
                uses.setdefault((_key(quote), str(e.get("page"))), []).append((kind, label, split))
                if kind == "broad":
                    broad_pages.setdefault(_key(quote), []).append(label)
                    span = _page_span(e.get("page"), pages)
                    broad_quoted.setdefault(_key(quote), set()).update(
                        range(span[0], span[1] + 1) if not isinstance(span, str) else [])
                elif kind == "rejected" and not split:
                    rejected_quotes.setdefault((_key(quote), _first_page(e)), label)
            if "page" in e:
                span = _page_span(e["page"], pages)
                if isinstance(span, str):
                    problems.append(f"{label}.page: {span}")
                elif quote:
                    _check_quote(problems, label, e, span, pages)

            if key == "broad_statements":
                if _nonempty(e.get("states")) and quote:
                    extra = sorted(_words(e["states"]) - _quote_words(quote))
                    if extra:
                        problems.append(f"{label}.states: uses words that are not in the quote: "
                                        f"{', '.join(extra[:5])}")
                if "source" in e and e["source"] not in SOURCES:
                    problems.append(f"{label}.source: must be one of {', '.join(SOURCES)}")
                if e.get("source") == "other" and not _nonempty(e.get("note")):
                    problems.append(f"{label}.note: a broad statement with source other needs a note "
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

            if key == "claims":
                serves = e.get("serves")
                if not isinstance(serves, list) or not serves:
                    problems.append(f"{label}.serves: must list at least one broad statement id")
                    serves = []
                listed: set[str] = set()
                for ref in serves:
                    if not isinstance(ref, str):
                        problems.append(f"{label}.serves: must list ids as strings")
                        continue
                    if ref in listed:
                        problems.append(f"{label}.serves: '{ref}' is listed twice")
                    listed.add(ref)
                    if ref in broad_ids:
                        served.add(ref)
                    else:
                        problems.append(f"{label}.serves: '{ref}' is not a broad statement id")
                if quote and not split:
                    same = broad_quotes.get(_key(quote))
                    if same and same not in listed:
                        problems.append(f"{label}.serves: the claim quotes the same sentence as {same}, "
                                        f"so it must serve {same}")
                if not split and quote and _nonempty(e.get("states")) and _key(quote) != normalize(e["states"])[0]:
                    problems.append(f"{label}.states: differs from the quote, but split_from is null; "
                                    "copy the quote, or set split_from if this claim is one part "
                                    "of a split statement")

            if key == "rejected":
                if "duplicate_of" in e:
                    refs = e["duplicate_of"]
                    if not isinstance(refs, list) or not refs or not all(_nonempty(r) for r in refs):
                        problems.append(f"{label}.duplicate_of: must be a list of the ids of the "
                                        "claims, rejected candidates, or broad statements that the "
                                        "statement repeats")
                        refs = [r for r in refs if _nonempty(r)] if isinstance(refs, list) else []
                    for ref in refs:
                        if ref == e.get("id"):
                            problems.append(f"{label}.duplicate_of: an entry cannot repeat itself")
                        elif ref not in owners:
                            problems.append(f"{label}.duplicate_of: '{ref}' is not the id of a claim, "
                                            "rejected candidate, or broad statement")
                if "breaks_down" in e:
                    refs = e["breaks_down"]
                    if not isinstance(refs, list) or not refs or not all(_nonempty(r) for r in refs):
                        problems.append(f"{label}.breaks_down: must be a list of the ids of the broad "
                                        "statements whose result the statement breaks down")
                        refs = [r for r in refs if _nonempty(r)] if isinstance(refs, list) else []
                    for ref in refs:
                        if ref not in broad_ids:
                            problems.append(f"{label}.breaks_down: '{ref}' is not a broad statement id")
                    dups = e.get("duplicate_of")
                    both = {r for r in refs if r in (dups if isinstance(dups, list) else [])}
                    if both:
                        problems.append(f"{label}.breaks_down: '{sorted(both)[0]}' is named by "
                                        "duplicate_of as well; a sentence either repeats a result or "
                                        "breaks it down")
                if split and not _nonempty(e.get("states")):
                    problems.append(f"{label}.states: a rejected part of a split statement needs "
                                    "the words of that part")
                if not split and "states" in e:
                    problems.append(f"{label}.states: only a rejected part of a split statement has "
                                    "words of its own; remove them or set split_from")

    for (quote_key, page), label in rejected_quotes.items():
        same = [b for b, e in broad_quoted.items() if b == quote_key and page in e]
        if same:
            problems.append(f"{label}.quote: {broad_pages[quote_key][0]} quotes the same sentence on the "
                            "same page; a broad statement is not also a rejected candidate")
    for labels in broad_pages.values():
        if len(labels) > 1:
            problems.append(f"{labels[1]}.quote: {labels[0]} already quotes the same sentence; one "
                            "sentence is one broad statement, even when it states several main results")
    for users in uses.values():
        broads = [label for kind, label, _ in users if kind == "broad"]
        whole = [(kind, label) for kind, label, _ in users if kind in ("claims", "rejected")]
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
    for i, b in lists["broad_statements"]:
        if _nonempty(b.get("id")) and b["id"] not in served and not _nonempty(b.get("note")):
            problems.append(f"{_label('broad_statements', i, b)}.note: no claim serves this broad "
                            "statement; add a note that says why")

    for split, members in splits.items():
        quoted = broad_quotes.get(_key(_str(members[0][1].get("quote"))))
        serving = {ref for _, e in members for ref in (e.get("serves") or []) if isinstance(ref, str)}
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
_CAPTION_START = re.compile(r"^(?:Fig\.|Figure|FIGURE|Table|TABLE|Listing|Algorithm)\s*[\dIVXLC]")


def _reads_like_prose(line: str) -> bool:
    """Whether a line skipped by a `[...]` gap looks like running text rather than a caption, a
    table row, a footnote, or a figure label."""
    return (len(line.split()) >= 4 and not _CAPTION_START.match(line)
            and not re.search(r"\S\s{3,}\S", line) and not re.match(r"\d{1,2}[\s A-Z]", line)
            and "http" not in line and not line.startswith("[references removed"))
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# A reason that gives a verdict instead of a ground: a word for how much the statement matters,
# which a checker cannot accept or overturn, or too few words to say anything. What follows the word
# decides: "Key numbers here come from cited work" names a ground, "A key number of the paper" names
# none. The grounds are those the reference lists, so the words that name them are few.
_A_GROUND = re.compile(
    r"\b(?:describ\w+|repeat\w+|cited|agreement|corpus|codebook|sample\s+size|no\s+broad\s+"
    r"statement|states?\s+the\s+main\s+result|out\s+of\s+scope|comes?\s+from|came\s+from)\b", re.I)
# A reason that runs on has said something, whatever word it opens with, so only a short one is read
# for a verdict.
_VERDICT_WORDS = 8
_EMPTY_REASON = re.compile(
    r"^\W*(?:this\s+is\s+|it\s+is\s+)?(?:not\s+)?(?:an?\s+|the\s+)?"
    r"(?:important|significant|relevant|interesting|major|minor|key|main|central|notable)\b"
    r"|^\W*(?:not\s+a\s+claim|no|n/?a|none)\W*$", re.I)
_NO_MAIN_RESULT = re.compile(r"\bno main result\b", re.I)
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
# How much of their support two broad statements share before the checker is asked whether they
# state one main result. Below this, a paper's separate results draw the question; above it, its
# restatements of one result stop drawing it.
_SAME_RESULT = 0.5


def _sentence_warnings(label: str, quote: str, page_text: str) -> list[str]:
    span = find_quote(quote, page_text)
    if span is None:
        return []
    text = _fold(page_text)
    start, end, gaps = span
    out = []
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
    for a, b in gaps:
        skipped = [l.strip() for l in text[a:b].splitlines() if l.strip()]
        # A caption or a footnote runs over several lines, and only its first line names itself, so
        # one such line makes the whole block a caption or a footnote.
        if any(_CAPTION_START.match(l) or re.match(r"\d{1,2}[\s A-Z]", l) for l in skipped):
            continue
        prose = [l for l in skipped if _reads_like_prose(l)]
        # One short line among running text, such as the end of a paragraph, does not make the
        # skipped text a figure or a table.
        if prose and len(prose) >= len(skipped) - 1:
            words = " ".join(skipped)
            words = words if len(words) <= 90 else words[:90] + "..."
            out.append(f"{label}.quote: the [...] skips \"{words}\", which reads like part of the "
                       "sentence; use [...] only where a figure, table, footnote, or page break "
                       "interrupts it")
    return out


def _in_order(part: list[str], whole: list[str]) -> bool:
    rest = iter(whole)
    return all(any(x == y for y in rest) for x in part)


def advisories(paper_dir: Path, data: dict) -> list[str]:
    """Likely mistakes in a valid record that the checker should look at."""
    pages = load_pages(paper_dir / "text.txt")
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for key in ("broad_statements", "claims", "rejected"):
        for i, e in enumerate(data[key]):
            span = _page_span(e["page"], pages)
            if isinstance(span, str) or (e["quote"], str(e["page"])) in seen:
                continue
            seen.add((e["quote"], str(e["page"])))
            out += _sentence_warnings(_label(key, i, e), e["quote"], _page_text(pages, span))
    for i, e in enumerate(data["claims"]):
        if not any(re.search(rf"\b{re.escape(ref)}\b", e["selection_reason"]) for ref in e["serves"]):
            out.append(f"{_label('claims', i, e)}.selection_reason: names none of the broad statements in "
                       "serves; say which main result would fail and how")
    for i, e in enumerate(data["rejected"]):
        reason = e["reason"]
        named = e.get("duplicate_of") or e.get("split_from") or re.search(r"\bB\d+\b", reason)
        if _NO_MAIN_RESULT.search(reason) and data["broad_statements"] and not named:
            out.append(f"{_label('rejected', i, e)}.reason: saying that no main result depends on the "
                       "candidate is the selection question answered no; name the broad statement that "
                       "still stands, the result that this one breaks down, or the ground that puts "
                       "the statement out of scope")
        verdict = _EMPTY_REASON.match(reason.strip())
        if _NO_MAIN_RESULT.search(reason) and data["broad_statements"] and not named:
            pass
        elif not (named or e.get("breaks_down")) and (
                len(reason.split()) < 3
                or (verdict and len(reason.split()) <= _VERDICT_WORDS
                    and not _A_GROUND.search(reason[verdict.end():]))):
            out.append(f"{_label('rejected', i, e)}.reason: gives no ground a checker can assess; say "
                       "which main result still stands, by the id of its broad statement, or what puts "
                       "the statement out of scope")
    # A claim serves a broad statement, so a sentence repeating a claim repeats that statement's
    # main result. Naming the claim alone leaves the repetition out of the count of the places where
    # the paper states the result. A sentence repeating a rejected candidate names no statement,
    # which is why only a claim counts here.
    statements = {b["id"] for b in data["broad_statements"]}
    claims = {c["id"] for c in data["claims"]}
    served = {c["id"]: set(c["serves"]) & statements for c in data["claims"]}
    for i, e in enumerate(data["rejected"]):
        refs = e.get("duplicate_of") or []
        refs = refs if isinstance(refs, list) else [refs]
        wanted = set().union(*(served[r] for r in refs if r in claims)) if set(refs) & claims else set()
        if statements and not (set(refs) & statements) and not wanted <= set(e.get("breaks_down") or []):
            out.append(f"{_label('rejected', i, e)}.duplicate_of: names a claim, and every claim "
                       "serves a broad statement; where the sentence restates that main result, name "
                       "the broad statement as well, or the record counts one place too few, and "
                       "where it repeats only the number, say so in the reason")
    supporting = {b["id"]: {c["id"] for c in data["claims"] if b["id"] in c["serves"]}
                  for b in data["broad_statements"]}
    # A statement whose claims all serve another statement that more claims serve divides that
    # result instead of stating one of its own.
    breakdowns = set()
    for i, b in enumerate(data["broad_statements"]):
        mine = supporting[b["id"]]
        covers = [o["id"] for o in data["broad_statements"]
                  if o["id"] != b["id"] and mine and mine < supporting[o["id"]]]
        if covers:
            breakdowns.add(b["id"])
            # The statement that the most claims serve, so that a chain of nested statements points
            # at the one that survives rather than at each other.
            widest = max(covers, key=lambda o: (len(supporting[o]), o))
            out.append(f"{_label('broad_statements', i, b)}: every claim that serves it also serves "
                       f"{widest}, which more claims serve, so it may break that result down rather "
                       "than state one of its own; it keeps its record where its sentence reports "
                       "something no other broad statement mentions, and where its only addition is "
                       "a subgroup, an exception, an example, or a subset, record it as a rejected "
                       f"candidate with breaks_down naming {widest}")
    # The record has to say which main results the paper has, and say each one once, or a reader
    # cannot tell one result stated four ways from four results.
    for group in _one_result(data):
        if len(group) > 1 and not set(group) & breakdowns:
            out.append(f"broad_statements {', '.join(group)}: the same claims serve all of them, so "
                       "they read as one main result stated several times; record it once and keep "
                       "the others as rejected candidates with duplicate_of naming it")
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
                out.append(f"broad_statements {one[0]} and {two[0]}: most of the claims that "
                           "serve one serve the other as well; where they state one main result, "
                           "record it once and keep the other sentence as a rejected candidate with "
                           "duplicate_of naming it")
    for i, b in enumerate(data["broad_statements"]):
        if _BOXED_SECTION.search(b.get("section", "")) and b.get("source") != "rq_answer":
            out.append(f"{_label('broad_statements', i, b)}.source: the section names a boxed answer, "
                       f"so the source is rq_answer, not {b.get('source')!r}")
    whole = [(_label(key, i, e), e.get("section", "")) for key in ("broad_statements", "claims", "rejected")
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
    entries = [(_label(key, i, e), _key(e["quote"])) for key in ("broad_statements", "claims", "rejected")
               for i, e in enumerate(data[key])]
    for label, key in entries:
        inside = [other for other, k in entries if other != label and k != key and k in key]
        if inside:
            out.append(f"{label}.quote: contains the quote of {inside[0]}; record a sentence once, unless "
                       "this quote needs both sentences because the second one refers to the first")
    serving = {(ref, e["id"]) for e in data["claims"] for ref in e["serves"]}
    seen: dict[str, tuple[str, str, str]] = {}
    for key in ("broad_statements", "claims", "rejected"):
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
    for key in ("claims", "rejected"):
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
        if re.search(r"\brespectively\b", quote, re.I):
            out.append(f"split_from '{split}': the quote pairs items and numbers with "
                       "\"respectively\"; check that each part keeps the right pair")
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
    return out


# --- Rendering ---

def _flat(text) -> str:
    """`text` on one line, so that a line break in a field cannot break the Markdown. A word that
    the paper broke across two lines is joined, because the quote holds it as the paper printed it
    and the checker reads one word."""
    return " ".join(re.sub(r"-\s*\n\s*", "", str(text)).split())


def _quote_block(text: str) -> list[str]:
    return [f"> {_flat(text)}"]


def _first_page(entry: dict) -> int:
    try:
        return int(str(entry.get("page")).split("-")[0])
    except ValueError:
        return 0


def _one_result(data: dict) -> list[list[str]]:
    """The broad statements grouped by the main result they state, each group in the order recorded.

    The reference records a main result once and keeps the sentences that repeat it as rejected
    candidates. Where that has not happened, the same result stands as several broad statements, and
    the claims give it away: statements that state one result are served by the same claims. Equal
    sets of claims group, and nothing else: two statements sharing part of their support may state
    one result or two, which the checker settles, and a warning asks."""
    supports = {b["id"]: {c["id"] for c in data["claims"] if b["id"] in c["serves"]}
                for b in data["broad_statements"]}
    groups: dict[frozenset, list[str]] = {}
    for b, claims in supports.items():
        # A statement that no claim serves shares nothing with another such statement, and the
        # reference lets it stand where its note says why, so it groups with nothing.
        groups.setdefault(frozenset(claims) if claims else b, []).append(b)
    return list(groups.values())


def _stated_in(data: dict, group: list[str] | None = None) -> int:
    """How many sentences of the paper state one main result: the broad statements that hold it, and
    the recorded sentences that repeat any of them.

    A breakdown of a result is not a sentence stating it, and names `breaks_down` rather than
    `duplicate_of`, so it is not counted here.

    The sentences are counted, not the links to them. Where one result stands as four broad
    statements, a sentence repeating it names several of the four, and counting each statement's
    repetitions on its own would count that sentence several times over."""
    ids = set(group) if group is not None else {b["id"] for b in data["broad_statements"]}
    repeats = set()
    for i, r in enumerate(data["rejected"]):
        refs = r.get("duplicate_of") or []
        if ids & set(refs if isinstance(refs, list) else [refs]):
            repeats.add(r.get("id", i))
    return len(ids) + len(repeats)


def _by_weight(data: dict) -> list[dict]:
    """The broad statements, the ones the paper states in most places first. The paper orders its
    own results this way; it puts no order on the claims that support one result, so neither does
    this."""
    def key(b):
        source = _SOURCE_ORDER.index(b["source"]) if b["source"] in _SOURCE_ORDER else len(_SOURCE_ORDER)
        return (-_stated_in(data, [b["id"]]), source, _first_page(b), b["id"])
    return sorted(data["broad_statements"], key=key)


def render(data: dict) -> str:
    paper, broad = data["paper"], data["broad_statements"]
    claims, rejected = data["claims"], data["rejected"]
    out = [
        f"# Claims: {_flat(paper['title'])}",
        "",
        f"Paper `{paper['id']}` ({paper['pages']} pages, `{paper['pdf']}`): "
        f"{len(broad)} broad statements, {len(claims)} narrow claims, "
        f"{len(rejected)} rejected candidates.",
        "",
        "Generated from `claims.json` by `cea_claims.py render`. To change the record, edit "
        "`claims.json` and render again.",
        "",
        "## Broad statements",
        "",
    ]
    if not broad:
        out += ["None recorded.", ""]
    for b in broad:
        serving = [c["id"] for c in claims if b["id"] in c["serves"]]
        out += [f"### {b['id']}: {b['source']}, page {b['page']}", "", *_quote_block(b["quote"]), "",
                f"- Section: {_flat(b['section'])}",
                f"- Narrow claims: {', '.join(serving) if serving else 'none selected'}"]
        if b.get("states") and _key(b["states"]) != _key(b["quote"]):
            out.append(f"- States: {_flat(b['states'])}")
        if b.get("note"):
            out.append(f"- Note: {_flat(b['note'])}")
        out.append("")

    out += ["## Narrow claims", ""]
    if not claims:
        out += ["None selected.", ""]
    else:
        out += ["Grouped under each main result they serve, the result the paper states in the most "
                "places first. Within a result the claims stand in page order, because the paper puts "
                "no order on them.", ""]
    by_id = {b["id"]: b for b in data["broad_statements"]}
    order = {b["id"]: n for n, b in enumerate(_by_weight(data))}
    results = sorted(_one_result(data), key=lambda g: min(order[b] for b in g))
    for group in results:
        lead = min(group, key=lambda b: order[b])
        serving = sorted((c for c in claims if set(c["serves"]) & set(group)), key=_first_page)
        if not serving:
            continue
        stated = _stated_in(data, group)
        also = f", also stated as {', '.join(b for b in group if b != lead)}" if len(group) > 1 else ""
        out += [f"### {lead} ({by_id[lead]['source']}, stated in {stated} "
                f"place{'s' if stated > 1 else ''}{also}): {_flat(by_id[lead]['quote'])}", ""]
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
            others = [x["id"] for x in claims + rejected
                      if x.get("split_from") == c["split_from"] and x is not c]
            out += [f"- Part: {_flat(c['states'])}",
                    f"- Split from {c['split_from']}, with {', '.join(others)}"]
        out += [f"- Serves: {', '.join(c['serves'])}",
                f"- Selection reason: {_flat(c['selection_reason'])}"]
        if c.get("note"):
            out.append(f"- Note: {_flat(c['note'])}")
        out.append("")

    out += ["## Rejected candidates", ""]
    if not rejected:
        out += ["None recorded.", ""]
    for r in sorted(rejected, key=_first_page):
        out += [f"### {r['id']}: page {r['page']}, {_flat(r['section'])}", "", *_quote_block(r["quote"]), ""]
        if r.get("split_from"):
            out.append(f"- Rejected part: {_flat(r['states'])} (split from {r['split_from']})")
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
    probe = subprocess.run([exe, "-v"], capture_output=True, text=True)
    version = ((probe.stderr or probe.stdout).strip().splitlines() or ["pdftotext"])[0]
    print(f"CEA_OK: Python {sys.version.split()[0]}, {version}")
    return 0


def cmd_extract(args) -> int:
    import pdf_text

    pdf = Path(args.pdf)
    paper_id = args.id if args.id is not None else pdf.stem
    if not paper_id or paper_id != paper_id.strip() or "/" in paper_id or "\\" in paper_id or paper_id in (".", ".."):
        print(f"CEA_FAILED: invalid paper id {paper_id!r}; use a plain name without slashes or '..'")
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
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        text_path.write_text(pdf_text.to_text(extraction), encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {text_path}: {e}")
        return 2
    columns = sum(1 for p in extraction.pages if p.regions)
    print(f"CEA_EXTRACTED: {text_path}")
    print(f"paper_id: {paper_id}")
    print(f"pages: {len(extraction.pages)}, {columns} of them with two-column text put in reading order")
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
    empty = [str(p.number) for p in extraction.pages if not any(l.strip() for l in p.lines)]
    if empty:
        print(f"empty pages: {', '.join(empty)} (no text left, for example after removing references)")
    if extraction.lineno:
        print("line numbers: LaTeX margin line numbers removed")
    return 0


def cmd_validate(args) -> int:
    paper_dir = Path(args.paper_dir)
    problems, data = validate(paper_dir)
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s) in {paper_dir / 'claims.json'}")
        for p in problems:
            print(f"- {p}")
        return 1
    print(f"CEA_VALID: {len(data['broad_statements'])} broad statements, {len(data['claims'])} "
          f"claims, {len(data['rejected'])} rejected candidates; every quote found on its page")
    for warning in advisories(paper_dir, data):
        print(f"warning: {warning}")
    return 0


def cmd_render(args) -> int:
    paper_dir = Path(args.paper_dir)
    problems, data = validate(paper_dir)
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s); run validate and fix them before rendering")
        return 1
    md_path = paper_dir / "claims.md"
    try:
        md_path.write_text(render(data), encoding="utf-8")
    except OSError as e:
        print(f"CEA_FAILED: cannot write {md_path}: {e}")
        return 2
    print(f"CEA_RENDERED: {md_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cea_claims.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-env").set_defaults(func=cmd_check_env)
    p = sub.add_parser("extract")
    p.add_argument("pdf")
    p.add_argument("--out", default="cea-out", help="output directory (default: cea-out)")
    p.add_argument("--id", help="paper id (default: the PDF file name without .pdf)")
    p.set_defaults(func=cmd_extract)
    for name, func in (("validate", cmd_validate), ("render", cmd_render)):
        p = sub.add_parser(name)
        p.add_argument("paper_dir")
        p.set_defaults(func=func)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
