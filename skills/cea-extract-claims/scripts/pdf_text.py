"""Extract a paper PDF's text page by page, in reading order.

Adapted from hallucite's `skills/hallucite/scripts/pdf_references.py` (MIT License, Copyright (c)
2026 Software Engineering Group @ Heidelberg University). hallucite reads a paper to segment its
bibliography. This module reads it to find claims, which changes the following:

- Pages stay separate, because a claim's location is its page.
- A two-column page can carry full-width blocks: a title block, a table, or a caption that spans
  both columns. hallucite looks for one gutter per page, and a full-width block hides it, so the
  whole page would be read line by line across both columns. Here each page is first cut into
  two-column regions and full-width blocks.
- The references section is removed and an appendix after it is kept, because an appendix can
  report results and a bibliography cannot.

Pipeline:
  1. `pdftotext -layout`, split into pages on form feeds.
  2. Blank running headers, footers, and bare page numbers at the top and bottom of each page.
  3. Cut each page into two-column regions and full-width blocks, and read each region's left
     column before its right.
  4. Blank LaTeX `lineno` margin numbers.
  5. Remove the references section.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass

# A `lineno` margin number, in the two shapes `pdftotext -layout` produces: alone on its line, or
# in the gutter before the line's text.
_MARGIN_BARE = re.compile(r"^\s*\d{1,4}\s*$")
_MARGIN_GUTTER = re.compile(r"^\s*\d{1,4}\s{2,}(?=\S)")

# How many blank character columns make a gutter. hallucite measured this over 37 arXiv papers:
# a threshold of four missed the narrow gutter of ACM bibliographies, and a threshold of two
# found no gutter that three did not.
_MIN_GUTTER = 3
# Share of a two-column region's lines that may run across the gutter (hallucite's 97% rule).
_CROSSING_SHARE = 0.03
# A region needs this many non-blank lines, so that a few table rows with a gap in the middle are
# not read as two columns.
_MIN_REGION_LINES = 8
# Share of a region's lines that must carry text on each side of the gutter. Without it, a block
# of short single-column lines, which is blank right of the middle, would count as two columns.
_MIN_SIDE_SHARE = 0.25

_PAGE_NUMBER = re.compile(r"^\s*\d{1,4}\s*$")
# A table caption at the start of a line ("TABLE V", "Table 3") and a run of 3 or more spaces between
# two cells. Text lines in a two-column region have at most one such run, the gap between columns.
_TABLE_CAPTION = re.compile(r"^(?:TABLE|Table)\s+(?:[IVXLC]+|\d+)\b")
_WIDE_GAP = re.compile(r"\S\s{3,}(?=\S)")
# The page number in a running header is at its start or end.
_EDGE_NUMBER = re.compile(r"^[\d:.\s]+|[\d:.\s]+$")

# "7 ", "7. ", "VII. ". A roman numeral needs its period, or the "C" of a small-caps "C ONCLUSION"
# would count as a numeral.
_HEADER_NUM = re.compile(r"^(?:\d+[.)]?|[ivxlc]+[.)])\s+", re.I)
# Headings are compared without spaces, because pdftotext writes a small-caps "REFERENCES" as
# "R EFERENCES".
_REFERENCE_HEADINGS = frozenset(h.replace(" ", "") for h in (
    "references", "bibliography", "references and notes", "literature cited", "works cited"))
_APPENDIX_HEADINGS = ("appendix", "appendices", "supplementarymaterial")

REFERENCES_REMOVED = "[references removed by cea_claims.py extract]"


@dataclass
class Page:
    number: int        # 1-based PDF page number
    lines: list[str]   # in reading order
    regions: int = 0   # two-column regions put in reading order


@dataclass
class Extraction:
    pages: list[Page]
    lineno: bool
    # (page of the References heading, page of the appendix heading after it or None), or None
    # when no References heading was found.
    references: tuple[int, int | None] | None


# ── Text layer ───────────────────────────────────────────────────────────────

def _pages(pdf_path: str) -> list[str]:
    try:
        out = subprocess.run(["pdftotext", "-layout", pdf_path, "-"], capture_output=True,
                             encoding="utf-8", errors="replace", check=True).stdout
    except FileNotFoundError:
        raise RuntimeError("pdftotext not found; install poppler (e.g. `brew install poppler`)")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pdftotext failed: {(e.stderr or '').strip() or e}")
    pages = out.split("\x0c")
    # pdftotext ends every page with a form feed, so the last chunk is empty. A blank page inside
    # the document stays, because dropping it would shift every later page number.
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


# ── Running headers, footers, and page numbers ──────────────────────────────

def _without_page_number(s: str) -> str:
    return re.sub(r"\s+", " ", _EDGE_NUMBER.sub("", s)).strip()


def _running_headers(pages: list[str]) -> set[str]:
    """Running headers and footers: a page's first or last non-blank line that says enough to be
    recognized and repeats on at least two pages once its page number is removed (hallucite)."""
    counts: Counter[str] = Counter()
    for page in pages:
        lines = [l for l in page.split("\n") if l.strip()]
        if len(lines) < 5:
            continue
        for line in (lines[0], lines[-1]):
            norm = _without_page_number(line)
            if len(norm) >= 20 and len(re.findall(r"[^\W\d_]{2,}", norm)) >= 3:
                counts[norm] += 1
    return {n for n, c in counts.items() if c >= 2}


def _blank_edges(lines: list[str], headers: set[str]) -> None:
    """Blank the running header, footer, and bare page number at either end of a page, in place.
    Only the ends are touched, because the same words inside a page are text."""
    for edge in (0, -1):
        for _ in range(2):  # a page number and a running header can be stacked
            body = [i for i, l in enumerate(lines) if l.strip()]
            if len(body) < 5:
                return
            i = body[edge]
            if _PAGE_NUMBER.match(lines[i]) or _without_page_number(lines[i]) in headers:
                lines[i] = ""
            else:
                break


# ── Columns ──────────────────────────────────────────────────────────────────

def _gutter(page_lines: list[str]) -> int | None:
    """Column position of a two-column gutter: a band of >=_MIN_GUTTER columns, in the middle
    30-72% of the width, that is whitespace on >97% of non-blank lines (hallucite)."""
    nb = [l for l in page_lines if l.strip()]
    if len(nb) < 5:
        return None
    width = max(len(l) for l in nb)
    lo, hi = int(width * 0.30), int(width * 0.72)
    if hi <= lo:
        return None
    space_cols = [c for c in range(lo, hi)
                  if sum(c >= len(l) or l[c] == " " for l in nb) / len(nb) > 1 - _CROSSING_SHARE]
    if not space_cols:
        return None
    band = [space_cols[0]]
    for c in space_cols[1:]:
        if c == band[-1] + 1:
            band.append(c)
        elif len(band) >= _MIN_GUTTER:
            break
        else:
            band = [c]
    if len(band) < _MIN_GUTTER:
        return None
    # Cut where the band is blank on every line, so a line reaching into the band is not split
    # inside a word.
    strict = _longest_blank_run(band, nb)
    if len(strict) >= _MIN_GUTTER:
        return strict[len(strict) // 2]
    return band[len(band) // 2]


def _longest_blank_run(band: list[int], lines: list[str]) -> list[int]:
    """The longest stretch of `band` that is whitespace on every one of `lines`."""
    best: list[int] = []
    run: list[int] = []
    for c in band:
        if all(c >= len(l) or l[c] == " " for l in lines):
            run.append(c)
            if len(run) > len(best):
                best = list(run)
        else:
            run = []
    return best


def _text_edge(lines: list[str]) -> int | None:
    """The character position where a column's text starts, ignoring bare numbers and `lineno` margins."""
    edges = []
    for l in lines:
        if not l.strip() or _MARGIN_BARE.match(l):
            continue
        m = _MARGIN_GUTTER.match(l)
        edges.append(m.end() if m else len(l) - len(l.lstrip()))
    return min(edges) if edges else None


def _align(left: list[str], right: list[str]) -> list[str]:
    """The right column shifted so that its text edge lines up with the left column's (hallucite). The cut
    falls mid-gutter, so without the shift every right-column line starts with extra indentation."""
    le, re_ = _text_edge(left), _text_edge(right)
    if le is None or re_ is None or le == re_:
        return right
    shift = re_ - le
    if shift < 0:
        return [" " * -shift + l if l.strip() else l for l in right]
    out = []
    for l in right:
        if not l[:shift].strip():
            out.append(l[shift:])
        elif _MARGIN_BARE.match(l):
            out.append("")
        else:
            out.append(l.lstrip())
    return out


def _blank_at(line: str, c: int) -> bool:
    return all(k >= len(line) or line[k] == " " for k in range(c - 1, c + 2))


def _widest_window(blank: list[bool], floor: int) -> tuple[int, int] | None:
    """The longest run [s, e) longer than `floor` that starts and ends on a line blank at the
    candidate gutter and has at most _CROSSING_SHARE of its lines crossing it."""
    miss = [0]
    for b in blank:
        miss.append(miss[-1] + (not b))
    best = None
    n = len(blank)
    for s in range(n):
        if not blank[s]:
            continue
        for e in range(n, s, -1):
            if e - s <= (best[1] - best[0] if best else floor):
                break
            if blank[e - 1] and miss[e] - miss[s] <= _CROSSING_SHARE * (e - s):
                best = (s, e)
                break
    return best


def _full_width_tables(lines: list[str], c: int, width: int) -> list[tuple[int, int]]:
    """Line spans [start, end) of tables that run across both columns.

    Such a table starts with a caption centered on the page and continues with rows that have
    wide gaps between cells or that cross the gutter. A table whose cell gap lines up with the
    gutter would otherwise be cut in half, with its right half placed at the top of the right
    column. A table inside one column has its caption centered on that column and is split with
    the column as usual."""
    spans = []
    i = 0
    while i < len(lines):
        text = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if text and _TABLE_CAPTION.match(text) and abs(indent + len(text) / 2 - width / 2) <= 0.1 * width:
            rows, end, j = 0, i + 1, i + 1
            while j < len(lines):
                line = lines[j]
                if line.strip():
                    if len(_WIDE_GAP.findall(line.strip())) >= 2:
                        rows += 1
                    elif _blank_at(line, c):
                        break
                    end = j + 1
                j += 1
            if rows >= 3:
                spans.append((i, end))
                i = end
                continue
        i += 1
    return spans


def _layout(lines: list[str]) -> tuple[list[str], int]:
    """`lines` in reading order, and the number of two-column regions found.

    The longest run of lines that shares a gutter becomes a two-column region, read left column
    first. The lines above and below it are laid out the same way, so a full-width table or title
    block stays whole and in place, and a page with no region passes through unchanged."""
    idx = [i for i, l in enumerate(lines) if l.strip()]
    if len(idx) < _MIN_REGION_LINES:
        return lines, 0
    nb = [lines[i] for i in idx]
    width = max(len(l) for l in nb)
    lo, hi = max(int(width * 0.30), 1), int(width * 0.72)
    best: tuple[int, int, int] | None = None  # (s, e, c) over non-blank line indices
    for c in range(lo, hi):
        floor = best[1] - best[0] if best else _MIN_REGION_LINES - 1
        win = _widest_window([_blank_at(l, c) for l in nb], floor)
        if win is None:
            continue
        region = nb[win[0]:win[1]]
        left = sum(bool(l[:c - 1].strip()) for l in region)
        right = sum(bool(l[c + 2:].strip()) for l in region)
        if min(left, right) >= _MIN_SIDE_SHARE * len(region):
            best = (win[0], win[1], c)
    if best is None:
        return lines, 0
    s, e, c = best
    start, end = idx[s], idx[e - 1] + 1
    # Keep full-width tables at the top or bottom of the region whole and out of the split.
    top_end, bottom_start = start, end
    for ts, te in _full_width_tables(lines, c, width):
        if ts <= start < te < end:
            top_end, start = ts, te
        elif start < ts < end <= te:
            end, bottom_start = ts, te
    region = lines[start:end]
    if sum(bool(l.strip()) for l in region) < _MIN_REGION_LINES:
        top, n_top = _layout(lines[:top_end])
        bottom, n_bottom = _layout(lines[bottom_start:])
        return top + lines[top_end:bottom_start] + bottom, n_top + n_bottom
    g = _gutter(region)
    if g is None:
        g = c
    left = [l[:g].rstrip() for l in region]
    right = [l[g:].rstrip() for l in region]
    top, n_top = _layout(lines[:top_end])
    bottom, n_bottom = _layout(lines[bottom_start:])
    return (top + lines[top_end:start] + left + _align(left, right) + lines[end:bottom_start]
            + bottom, 1 + n_top + n_bottom)


# ── Line numbers ─────────────────────────────────────────────────────────────

def _blank_margin(line: str, margin_col: int) -> str:
    """Replace a `lineno` margin number with spaces, keeping the text in its columns. A number
    alone on its line is blanked only at the margin column, so a wrapped number stays (hallucite)."""
    if _MARGIN_BARE.match(line):
        indent = len(line) - len(line.lstrip())
        return line if indent > margin_col + 2 else ""
    m = _MARGIN_GUTTER.match(line)
    return " " * m.end() + line[m.end():] if m else line


def _strip_line_numbers(lines: list[str]) -> tuple[list[str], bool]:
    nb = [l for l in lines if l.strip()]
    if not nb:
        return lines, False
    matches = [l for l in nb if _MARGIN_BARE.match(l) or _MARGIN_GUTTER.match(l)]
    if len(matches) / len(nb) <= 0.5:
        return lines, False
    margin_col = Counter(len(l) - len(l.lstrip()) for l in matches).most_common(1)[0][0]
    return [_blank_margin(l, margin_col) for l in lines], True


# ── References ───────────────────────────────────────────────────────────────

def _heading_key(line: str) -> str:
    return _HEADER_NUM.sub("", line.strip().rstrip(" .:").lower()).replace(" ", "")


def _is_references_heading(line: str) -> bool:
    return 0 < len(line.strip()) <= 40 and _heading_key(line) in _REFERENCE_HEADINGS


def _is_appendix_heading(line: str) -> bool:
    return 0 < len(line.strip()) <= 80 and _heading_key(line).startswith(_APPENDIX_HEADINGS)


def _drop_references(pages: list[Page]) -> tuple[int, int | None] | None:
    """Blank everything from the first References heading to the next appendix heading, or to
    the end. Author biographies after an IEEE bibliography are removed with it."""
    start = next(((pi, li) for pi, p in enumerate(pages)
                  for li, l in enumerate(p.lines) if _is_references_heading(l)), None)
    if start is None:
        return None
    pi, li = start
    end = next(((pj, lj) for pj in range(pi, len(pages))
                for lj in range(li + 1 if pj == pi else 0, len(pages[pj].lines))
                if _is_appendix_heading(pages[pj].lines[lj])), None)
    stop = end if end else (len(pages) - 1, len(pages[-1].lines))
    for pj in range(pi, stop[0] + 1):
        lo = li if pj == pi else 0
        hi = stop[1] if pj == stop[0] else len(pages[pj].lines)
        for k in range(lo, hi):
            pages[pj].lines[k] = ""
    pages[pi].lines[li] = REFERENCES_REMOVED
    return pages[pi].number, (pages[end[0]].number if end else None)


# ── Entry points ─────────────────────────────────────────────────────────────

def extract(pdf_path: str) -> Extraction:
    raw = _pages(pdf_path)
    headers = _running_headers(raw)
    pages = []
    for number, text in enumerate(raw, 1):
        lines = [l.rstrip() for l in text.split("\n")]
        _blank_edges(lines, headers)
        ordered, regions = _layout(lines)
        pages.append(Page(number, ordered, regions))
    # Line numbers are detected over the whole paper, because a single page can have too few lines to decide.
    flat, lineno = _strip_line_numbers([l for p in pages for l in p.lines])
    k = 0
    for p in pages:
        p.lines, k = flat[k:k + len(p.lines)], k + len(p.lines)
    references = _drop_references(pages)
    return Extraction(pages, lineno, references)


def to_text(extraction: Extraction) -> str:
    """The pages as text, each after a `=== page N ===` line, with runs of blank lines collapsed."""
    out: list[str] = []
    for p in extraction.pages:
        out.append(f"=== page {p.number} ===")
        previous_blank = True
        for line in p.lines:
            line = line.rstrip()
            if not line.strip():
                if not previous_blank:
                    out.append("")
                previous_blank = True
                continue
            out.append(line)
            previous_blank = False
        if out and out[-1] == "":
            out.pop()
    return "\n".join(out) + "\n"
