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
     column before its right. A page too short for this is cut at the gutter of the other pages.
  4. Blank LaTeX `lineno` line numbers, at either margin or glued to the end of a line.
  5. Remove the references section.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass

# A `lineno` margin number, in the shapes `pdftotext -layout` produces: alone on its line, in the
# gutter before the line's text, or after the line's text at the right margin.
_MARGIN_BARE = re.compile(r"^\s*\d{1,4}\s*$")
_MARGIN_GUTTER = re.compile(r"^\s*\d{1,4}\s{2,}(?=\S)")
_TRAILING_DIGITS = re.compile(r"(\d+)\s*$")
# Digits glued to the end of a word, as pdftotext writes a line number that touches the text. The
# word must be at least three letters long and start in lower case, so that a label such as "S46",
# "RQ3", "GPT4", "Study1", or "Qwen2" keeps its digits.
_GLUED_END = re.compile(r"\b([^\W\d_]+)(\d{1,4})\s*$")


def _glued_number(line: str) -> re.Match | None:
    m = _GLUED_END.search(line)
    return m if m and m.group(1)[:1].islower() else None

# How many blank character columns make a gutter. hallucite measured this over 37 arXiv papers:
# a threshold of four missed the narrow gutter of ACM bibliographies, and a threshold of two
# found no gutter that three did not.
_MIN_GUTTER = 3
# Share of a two-column region's lines that may run across the gutter (hallucite's 97% rule).
_CROSSING_SHARE = 0.03
# A region needs this many non-blank lines, so that a few table rows with a gap in the middle are
# not read as two columns.
_MIN_REGION_LINES = 8
# Lines of a region that must carry text on each side of the gutter. Without this minimum, a block of short
# single-column lines, which is blank right of the middle, would count as two columns. It is a count
# rather than a share, so that a page with a full left column and a short right column still splits.
_MIN_SIDE_LINES = 4
# How far a region's cut may sit from the gutter of the paper. A gutter is a band several columns
# wide, and the cut can fall anywhere inside it, so the distance is measured generously.
_GUTTER_NEAR = 6

# A line that holds only one to three numbers.
_NUMBERS_ONLY = re.compile(r"^\s*(?:\d{1,4}\s{2,}){0,2}\d{1,4}\s*$")
# A table caption at the start of a line (e.g., "TABLE V", "Table 3") and a run of 3 or more spaces between
# two cells. Text lines in a two-column region have at most one such run, the gap between columns.
_TABLE_CAPTION = re.compile(r"^(?:TABLE|Table)\s+(?:[IVXLC]+|\d+)\b")
_WIDE_GAP = re.compile(r"\S\s{3,}(?=\S)")
# The first line of a figure or table caption.
_CAPTION = re.compile(r"^(?:Fig\.|Figure|FIGURE|Table|TABLE)\s*(?:[IVXLC]+|\d+)\b")
# The page number in a running header is at its start or end.
_EDGE_NUMBER = re.compile(r"^[\d:.\s]+|[\d:.\s]+$")

# "7 ", "7. ", "VII. ". A roman numeral needs its period, or the "C" of a small-caps "C ONCLUSION"
# would count as a numeral.
_HEADER_NUM = re.compile(r"^(?:\d+[.)]?|[ivxlc]+[.)])\s+", re.I)
# Headings are compared without spaces, because pdftotext writes a small-caps "REFERENCES" as
# "R EFERENCES".
_REFERENCE_HEADINGS = frozenset(h.replace(" ", "") for h in (
    "references", "bibliography", "references and notes", "literature cited", "works cited"))
_APPENDIX_HEADINGS = ("appendix", "appendices", "supplementarymaterial", "supplementalmaterial",
                      "onlineappendix")
# Words that start an appendix heading in papers that number their appendices with their sections,
# such as "VIII. Additional Results". A bibliography entry must not follow such a heading.
_APPENDIX_WORDS = ("supplementary", "supplemental", "additional", "online")
# The start of a bibliography entry: "[12] ...", "12. Author ...", "Surname, I.", "Surname, John.",
# initials and a surname, or names followed by a year. A sentence that opens "Overall, Table 3 ..."
# must not match, or an appendix heading before it is missed.
_BIB_ENTRY = re.compile(r"^\s*(?:\[\d{1,3}\]"
                        r"|\d{1,3}\.\s+[A-Z]"
                        r"|[A-Z][\w'\u2019-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+\.)"
                        r"|(?:[A-Z]\.\s?){1,3}[A-Z][\w'\u2019-]+"
                        r"|[A-Z][\w'\u2019-]+(?:\s+(?:and\s+)?[A-Z][\w'\u2019-]+)+\.\s+(?:19|20)\d{2}\.)")
# A top-level heading labeled with a letter, as acmart, IEEEtran, and LNCS label appendices:
# "A Additional Results", "A. Additional Results", "A.1 Details for RQ1".
_LETTER_LABEL = re.compile(r"^[A-Z](?:\.\d+)?[.)]?\s+(?=[A-Z])")
# The quotation marks that a bibliography entry puts around a title.
_QUOTE_MARKS = "\"'\u201c\u201d"

REFERENCES_REMOVED = "[references removed by cea_claims.py extract]"


@dataclass
class Page:
    number: int        # 1-based PDF page number
    lines: list[str]   # in reading order
    regions: int = 0   # two-column regions put in reading order
    # Set where the removal of the bibliography stopped because the paper's text starts again here.
    resumed: bool = False


@dataclass
class Extraction:
    pages: list[Page]
    lineno: bool
    # (page of the References heading, page of the appendix heading after it or None), or None
    # when no References heading was found.
    references: tuple[int, int | None] | None
    # Whether the paper sets its body in two columns anywhere.
    two_column: bool = False
    # What pdftotext said while reading the file, where it said anything.
    unreadable: str = ""
    # The page where the text starts again, when the removal stopped there instead of running on.
    resumed: int | None = None


# --- Text layer ---

def _pages(pdf_path: str) -> tuple[list[str], str]:
    """The pages of the PDF as text, and what pdftotext said about reading it.

    A damaged PDF makes poppler report on stderr and exit 0 all the same, with the pages it could
    not read left out. Every later page then carries the number of a page it is not, so the second
    value is given to the caller rather than dropped.
    """
    try:
        done = subprocess.run(["pdftotext", "-layout", pdf_path, "-"], capture_output=True,
                              encoding="utf-8", errors="replace", check=True, timeout=300)
        out, said = done.stdout, (done.stderr or "").strip()
    except FileNotFoundError:
        raise RuntimeError("pdftotext not found; install poppler (e.g. `brew install poppler`)")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"pdftotext did not finish reading {pdf_path} within five minutes")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"pdftotext failed: {(e.stderr or '').strip() or e}")
    pages = out.split("\x0c")
    # pdftotext ends every page with a form feed, so the last chunk is empty. A blank page inside
    # the document stays, because dropping it would shift every later page number.
    if pages and not pages[-1].strip():
        pages.pop()
    return pages, said


# --- Running headers, footers, and page numbers ---

def _without_page_number(s: str) -> str:
    return re.sub(r"\s+", " ", _EDGE_NUMBER.sub("", s)).strip()


def _running_headers(pages: list[str]) -> set[str]:
    """Running headers and footers: a page's first or last non-blank line that stands apart from
    the body and repeats on other pages once its page number is removed. A line that says enough to
    be recognized must repeat on two pages (hallucite). A short line, such as an author name, must
    repeat on three. Blank space above or below the line keeps the last line of a paragraph, which
    can read the same on several pages, out of the count."""
    counts: Counter[str] = Counter()
    for page in pages:
        all_lines = page.split("\n")
        lines = [l for l in all_lines if l.strip()]
        if len(lines) < 5:
            continue
        first, last = all_lines.index(lines[0]), len(all_lines) - 1 - all_lines[::-1].index(lines[-1])
        apart = [l for l, i, step in ((lines[0], first, 1), (lines[-1], last, -1))
                 if not (0 <= i + step < len(all_lines)) or not all_lines[i + step].strip()]
        for line in apart:
            norm = _without_page_number(line)
            # A table that runs over a page break repeats its heading row at the top of each page.
            # That row is the page's first line and stands apart, but it is a row, not a header.
            if _table_line(line):
                continue
            if re.search(r"[^\W\d_]{2,}", norm):
                counts[norm] += 1
    return {n for n, c in counts.items()
            if c >= 3 or (c >= 2 and len(n) >= 20 and len(re.findall(r"[^\W\d_]{2,}", n)) >= 3)}


def _numbering_continues(line: str, neighbor: str) -> bool:
    """Whether a number of `line` continues, at the same column, the numbering of `neighbor`. The
    line numbers beside a page number do. The cells of a table row do not."""
    near = {m.start(): int(m.group()) for m in re.finditer(r"\d+", neighbor)}
    return any(abs(near[col] - int(m.group())) == 1
               for m in re.finditer(r"\d+", line)
               for col in (m.start() - 1, m.start(), m.start() + 1) if col in near)


def _edge_numbers(page_lines: list[list[str]]) -> list[tuple[int, int, int, list[tuple[int, int]]]]:
    """Every page's first and last non-blank line that holds nothing but numbers, as
    (page, line, which edge, [(column, value)])."""
    found = []
    for pi, lines in enumerate(page_lines):
        body = [i for i, l in enumerate(lines) if l.strip()]
        if len(body) < 5:
            continue
        for edge in (0, -1):
            i = body[edge]
            if _NUMBERS_ONLY.match(lines[i]):
                found.append((pi, i, edge,
                              [(m.start(), int(m.group())) for m in re.finditer(r"\d+", lines[i])]))
    return found


def _page_number_lines(page_lines: list[list[str]]) -> dict[int, set[int]]:
    """The lines that hold a page number, as {page: {line indices}}.

    A page number counts up from one page to the next at the same edge. The last row of a table, or
    the first row of a table carried onto the next page, holds numbers just as a page number does,
    and the page it stands on is the only one that has it. Without counting up across the pages,
    such a row is blanked and the paper loses it.

    The column is not what ties the run together. pdftotext pads a line out to the width of the
    widest line on its page, so a page number in the outer margin starts wherever that page happens
    to end, and it shifts again when the count reaches two digits."""
    found = _edge_numbers(page_lines)
    holds: dict[int, set[int]] = {}
    for pi, i, edge, numbers in found:
        for _, value in numbers:
            for pj, j, other_edge, others in found:
                if pj == pi + 1 and other_edge == edge and any(v == value + 1 for _, v in others):
                    holds.setdefault(pi, set()).add(i)
                    holds.setdefault(pj, set()).add(j)
    return holds


def _page_number_line(line: str, width: int, neighbor: str) -> bool:
    """Whether `line` holds only a page number: one number, or a number near the middle of the page
    between the line numbers of the two columns. A row of a table holds its numbers closer together,
    and its cells do not continue the numbers of the line beside it."""
    if not _NUMBERS_ONLY.match(line):
        return False
    spans = [m.span() for m in re.finditer(r"\d+", line)]
    if len(spans) == 1:
        # Counting up across the pages decides this one; see `_page_number_lines`.
        return False
    apart = all(b[0] - a[1] >= 10 for a, b in zip(spans, spans[1:]))
    centered = any(abs((a + b) / 2 - width / 2) <= 0.1 * width for a, b in spans)
    return apart and centered and _numbering_continues(line, neighbor)


def _blank_edges(lines: list[str], headers: set[str], numbers: set[int] = frozenset()) -> None:
    """Blank the running header, footer, and page number at either end of a page, in place. Only the
    ends are touched, because the same words inside a page are text. `numbers` holds the lines that
    `_page_number_lines` found to carry a page number."""
    width = max((len(l) for l in lines), default=0)
    for edge in (0, -1):
        for _ in range(2):  # a page number and a running header can be stacked
            body = [i for i, l in enumerate(lines) if l.strip()]
            if len(body) < 5:
                return
            i = body[edge]
            inner = i + 1 if edge == 0 else i - 1
            neighbor = lines[inner] if 0 <= inner < len(lines) else ""
            if (i in numbers or _page_number_line(lines[i], width, neighbor)
                    or _without_page_number(lines[i]) in headers):
                lines[i] = ""
            else:
                break


# --- Columns ---

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
    runs, run = [], [space_cols[0]]
    for c in space_cols[1:]:
        if c == run[-1] + 1:
            run.append(c)
        else:
            runs.append(run)
            run = [c]
    runs.append(run)
    runs = [r for r in runs if len(r) >= _MIN_GUTTER]
    if not runs:
        return None
    # A table cell inside the gap between columns cuts the gap into two runs. The gutter is the run
    # that the right column's text starts after. On ties, the leftmost run wins.
    def starts_after(r: list[int]) -> int:
        edge = r[-1] + 1
        return sum(1 for l in nb if len(l) > edge and l[edge] != " " and l[edge - 1] == " ")
    band = max(runs, key=starts_after)
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


def _centred_across(line: str, width: int) -> bool:
    """Whether `line` is one run of text centred on the page, with a margin on both sides.

    A title, an author list, an affiliation or a centred heading looks like this. Two columns of
    prose do not: their text reaches both edges of the page. Such a line has to stay whole, because
    every character between its two margins is text, so a cut anywhere inside it breaks a word.
    """
    text = line.rstrip()
    start = len(text) - len(text.lstrip())
    if not text.strip() or width <= 0:
        return False
    margin = 0.1 * width
    return (start > margin and len(text) < width - margin
            and abs(start + (len(text) - start) / 2 - width / 2) <= margin)


def _captions_across(lines: list[str], blank: list[bool]) -> list[bool]:
    """Lines of a caption that runs across the candidate gutter: a crossing line that starts with
    "Figure 2." or "TABLE V", and the crossing lines right after it. A two-column region must not
    contain them, or the caption is cut in half."""
    marks, in_caption = [], False
    for line, b in zip(lines, blank):
        in_caption = not b and (bool(_CAPTION.match(line.strip())) or in_caption)
        marks.append(in_caption)
    return marks


def _widest_window(blank: list[bool], floor: int, walls: list[bool] | None = None,
                   share: float = _CROSSING_SHARE) -> tuple[int, int] | None:
    """The longest run [s, e) longer than `floor` that starts and ends on a line blank at the
    candidate gutter, has at most _CROSSING_SHARE of its lines crossing it, and contains no line
    marked in `walls`."""
    miss, wall = [0], [0]
    for i, b in enumerate(blank):
        miss.append(miss[-1] + (not b))
        wall.append(wall[-1] + bool(walls and walls[i]))
    best = None
    n = len(blank)
    for s in range(n):
        if not blank[s]:
            continue
        for e in range(n, s, -1):
            if e - s <= (best[1] - best[0] if best else floor):
                break
            if (blank[e - 1] and miss[e] - miss[s] <= share * (e - s)
                    and wall[e] == wall[s]):
                best = (s, e)
                break
    return best


def _cell_gaps(line: str, c: int) -> int:
    """Runs of 3 or more spaces between two cells of `line`, not counting a run across column `c`,
    which on a two-column page is the gap between the columns."""
    return sum(1 for m in _WIDE_GAP.finditer(line) if not m.start() + 1 <= c < m.end())


def _centred_over(indent: int, length: int, width: int, below: list[str], c: int) -> bool:
    """Whether a caption at `indent` is centred on the page, or on the table below it.

    A full-width table is announced by a caption centred above it. The page may hold a line wider
    than the table, so the page's own width is not always what the caption is centred on: the rows
    of the table are. Only the lines that reach across the gutter are measured, because a line of
    body text beside another column is not part of the table and is wider than all of it.

    Either measure will do, because a caption inside one column is centred on neither.
    """
    middle = indent + length / 2
    if abs(middle - width / 2) <= 0.1 * width:
        return True
    # A caption of two lines, "TABLE III" over its title, is centred on one axis with the title.
    # The page can hold a line wider than the table, so its own middle is not always that axis.
    title = next((l for l in below if l.strip()), "")
    if title:
        start = len(title) - len(title.lstrip())
        end = len(title.rstrip())
        # Wide, so that a caption centred over one column of a two-column page is not taken for
        # one centred over the whole of it: the title of a full-width table reaches across most
        # of the page.
        if (end - start > 0.5 * width
                and abs(middle - (start + end) / 2) <= 0.1 * width):
            return True
    across = [l for l in below if l.strip() and not _blank_at(l, c)]
    if len(across) < 2:
        return False
    left = min(len(l) - len(l.lstrip()) for l in across)
    right = max(len(l.rstrip()) for l in across)
    return right - left > 0.3 * width and abs(middle - (left + right) / 2) <= 0.1 * (right - left)


def _spans_with_cells(line: str, c: int) -> bool:
    """Whether `line` is a row of a full-width table with one cell boundary.

    A table of two columns, such as a name beside its description, has one gap per row. Two columns
    of body text also have one gap, at the gutter, and that is the difference: their gap is where
    this line has text. A row that reaches across the gutter and keeps a cell apart somewhere else
    is full-width content, and cutting the page through it would break a word.

    The gap may fall on either side. A table with a long label and a short value at the right
    margin keeps its only gap right of the gutter, and requiring the left side cut those rows in
    half. Justified prose can stretch a gap on either side too, so the side settles nothing.
    """
    return not _blank_at(line, c) and _cell_gaps(line, c) >= 1


def _keeps_the_cells(line: str, c: int, cells: set[int]) -> bool:
    """Whether `line` is another row of a table whose cells are already known.

    A row of a full-width table can have a cell boundary that falls on the gutter, and that gap is
    the one `_cell_gaps` leaves out, so such a row counts one gap and reads as body text. It is
    still a row when it keeps a boundary the table has already used: a line of prose beside another
    column has no reason to break where the table's cells break.
    """
    gaps = list(_WIDE_GAP.finditer(line))
    return (len(gaps) >= 2 and any(m.start() + 1 <= c < m.end() for m in gaps)
            and any(abs(m.end() - x) <= 1 for m in gaps for x in cells))


# How long the last cell of a row may be where it stands alone right of the gutter.
_LAST_CELL = 15
# What the last column of a table holds where nothing lines up with it: a count or a measurement.
_LAST_CELL_VALUE = re.compile(r"^[\d(<>=~+-][\d.,%()<>=~+-]*$")
# How long a cell of a row is. Where a page sets two columns, pdftotext prints them on one line with
# a gap between, and each side carries a run of prose far longer than a cell.
_CELL_TEXT = 30


def _spanning_row(line: str, c: int) -> bool:
    """Whether `line` has gaps between table cells on both sides of column `c`, as a row of a table
    that runs across the gutter has. A row of a table inside one column has them on one side only.
    The line numbers are removed before the columns are split, so a number here is a cell of a row
    and not a number in the margin."""
    return _cell_gaps(line[:c], c) > 0 and _cell_gaps(" " * c + line[c:], c) > 0


def _last_cell_row(region: list[str], i: int, c: int) -> bool:
    """Whether line `i` of `region` is a row of a table with its last cell alone right of column
    `c`, which is where a table with a narrow last column meets the gutter. The gap before that cell falls on the
    gutter, so the row would be cut in half and its last cell read as text of the right column.

    Two tables set side by side both carry cells right of the gutter, so more than one cell stands
    there. A line that ends a paragraph of the right column leaves a single short word there too, so
    the cell has to read as a count or a measurement, which is what such a last column holds.

    One case stays wrong: A last column of words, such as a column of Yes and No values, is not held
    together this way, and its cells are read as text of the right column. Comparing the column
    against the rows nearby does not tell a cell from the end of a paragraph either, because the word
    that ends a paragraph falls in the column of a cell as a cell does. The cells are kept, each on a line of its own."""
    line = region[i]
    last = line[c:].strip()
    if _cell_gaps(line[:c], c) == 0 or not 0 < len(last) <= _LAST_CELL or " " in last:
        return False
    return bool(_LAST_CELL_VALUE.match(last))


def _row_follows(lines: list[str], j: int, c: int, lookahead: int = 3,
                 cells: set[int] | None = None) -> bool:
    """Whether one of the next `lookahead` non-blank lines after line `j` is a table row. A short
    label inside a table, such as a category name on a line of its own, then does not end it.

    A row whose cell boundary falls on the gutter counts too, where the table's own boundaries are
    known: those rows are the ones `_cell_gaps` cannot see, and a run of them would otherwise end
    the table at the first line that is not a row of its own.
    """
    seen = 0
    for line in lines[j + 1:]:
        if not line.strip():
            continue
        if (_cell_gaps(line, c) >= 2 or _spans_with_cells(line, c)
                or (cells and _keeps_the_cells(line, c, cells))):
            return True
        seen += 1
        if seen >= lookahead:
            return False
    return False


def _full_width_tables(lines: list[str], c: int, width: int) -> list[tuple[int, int]]:
    """Line spans [start, end) of tables that run across both columns.

    Such a table starts with a caption centered on the page and continues with rows that have
    wide gaps between cells or that cross the gutter. A table with a cell gap that lines up with the
    gutter would otherwise be cut in half, with its right half placed at the top of the right
    column. A table inside one column has its caption centered on that column and is split with
    the column as usual."""
    spans = []
    i = 0
    while i < len(lines):
        text = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        # Judge the caption by its own text, because the same line can also hold text of the other
        # column or the caption of a second table beside it.
        caption = re.split(r"\s{3,}", text)[0]
        if text and _TABLE_CAPTION.match(text) and _centred_over(indent, len(caption), width,
                                                                lines[i + 1:i + 9], c):
            rows, end, j, cells = 0, i + 1, i + 1, set()
            while j < len(lines):
                line = lines[j]
                if line.strip():
                    right = re.search(r"\S", line[c:]) if len(line) > c else None
                    right_start = c + right.start() if right else None
                    if (_cell_gaps(line, c) >= 2 or _keeps_the_cells(line, c, cells)
                            or _spans_with_cells(line, c)):
                        rows += 1
                        cells.update(m.end() for m in _WIDE_GAP.finditer(line))
                    elif _blank_at(line, c) and not (
                            # A line inside the table that is not a row of its own: a category
                            # label, or the rest of a cell that ran over. Another row right after
                            # it says the table has not ended. Body text below the table has no
                            # row after it, so the table still ends where it ends.
                            _row_follows(lines, j, c, cells=cells)
                            # The next line of a row with a right half that continues a cell. It follows
                            # the row directly, and its right text starts where a cell starts, well
                            # right of the gutter, where a right column of body text would not start.
                            or (j == end and right_start is not None and right_start > c + 0.1 * width
                                and any(abs(right_start - x) <= 1 for x in cells))):
                        break
                    end = j + 1
                j += 1
            if rows >= 3:
                spans.append((i, end))
                i = end
                continue
        i += 1
    return spans


def _trim_edges(blank: list[bool], s: int, e: int, k: int = 3) -> tuple[int, int]:
    """[s, e) without its first lines up to the last of the first `k` lines that crosses the
    gutter, and likewise at its end. Such a line belongs to a full-width block above or below the
    columns, such as a title or an author line, and a region that kept it would cut it in half."""
    head = [i for i in range(s, min(s + k, e)) if not blank[i]]
    if head:
        s = head[-1] + 1
    tail = [i for i in range(max(e - k, s), e) if not blank[i]]
    if tail:
        e = tail[0]
    return s, e


def _layout(lines: list[str], known: int | None = None, allowed: set[int] | None = None,
            allow_short: bool = False) -> tuple[list[str], int, int | None]:
    """`lines` in reading order, the number of two-column regions found, and the column where the
    largest region was cut, or None.

    The longest run of lines that shares a gutter becomes a two-column region, read left column
    first. The lines above and below it are laid out the same way, so a full-width table or title
    block stays whole and in place, and a page with no region passes through unchanged. A region
    needs _MIN_REGION_LINES lines. With `allow_short`, a shorter region is accepted at `known`, a gutter
    found above, below, or on other pages, if no line of it is a row of a table that crosses the
    gutter and its left column reaches that gutter. With `allowed`, the cut must lie near one of the
    gutters that the paper uses, so that a gap inside a wide table is never taken for a gutter. The
    returned gutter is None for a region where most lines are rows of a table, so that a gap inside
    a table cannot become the gutter of the paper."""
    idx = [i for i, l in enumerate(lines) if l.strip()]
    if len(idx) < (2 if known is not None else _MIN_REGION_LINES):
        return lines, 0, None
    nb = [lines[i] for i in idx]
    width = max(len(l) for l in nb)
    lo, hi = max(int(width * 0.30), 1), int(width * 0.72)
    best: tuple[int, int, int] | None = None  # (s, e, c) over non-blank line indices
    for c in range(lo, hi):
        near = known is not None and abs(c - known) <= 3
        if allowed is not None and not any(abs(c - g) <= _GUTTER_NEAR for g in allowed):
            continue
        shortest = 2 if near and allow_short else _MIN_REGION_LINES
        floor = max(best[1] - best[0] if best else 0, shortest - 1)
        blank = [_blank_at(l, c) for l in nb]
        # At a gutter that the paper already uses, more lines may cross it. A bibliography set in a
        # smaller font reaches past the gutter, and a region that stopped there would leave the rest of
        # the left column behind the right column's text.
        win = _widest_window(blank, floor, _captions_across(nb, blank),
                             0.15 if near else _CROSSING_SHARE)
        if win is None:
            continue
        s, e = _trim_edges(blank, *win)
        if e - s < shortest or (best and e - s <= best[1] - best[0]):
            continue
        region = nb[s:e]
        left = sum(bool(l[:c - 1].strip()) for l in region)
        right = sum(bool(l[c + 2:].strip()) for l in region)
        if len(region) >= _MIN_REGION_LINES:
            if min(left, right) >= _MIN_SIDE_LINES:
                best = (s, e, c)
        elif (min(left, right) >= 2 and not any(_spanning_row(l, c) for l in region)
              # In two columns of text the left column reaches the gutter. A table of labels and
              # definitions leaves a wide space before it.
              and 2 * sum(1 for l in region if l[:c].rstrip() and c - len(l[:c].rstrip()) <= 12) >= len(region)):
            best = (s, e, c)
    if best is None:
        return lines, 0, None
    s, e, c = best
    start, end = idx[s], idx[e - 1] + 1
    # Keep full-width tables at the top or bottom of the region whole and out of the split, whether
    # they overlap the region or end right before it, with only blank lines in between.
    top_end, bottom_start = start, end
    for ts, te in _full_width_tables(lines, c, width):
        if ts <= start and te < end and not any(l.strip() for l in lines[te:start]):
            top_end, start = ts, max(start, te)
        elif start < ts and end <= te and not any(l.strip() for l in lines[end:ts]):
            end, bottom_start = min(end, ts), te
    region = lines[start:end]
    near = known is not None and abs(c - known) <= 3
    if sum(bool(l.strip()) for l in region) < (2 if near else _MIN_REGION_LINES):
        top, n_top, _ = _layout(lines[:top_end], known, allow_short=allow_short)
        bottom, n_bottom, _ = _layout(lines[bottom_start:], known, allow_short=allow_short)
        return top + lines[top_end:bottom_start] + bottom, n_top + n_bottom, None
    g = _gutter(region)
    if g is None:
        g = c
    # A line that reaches across the cut stays whole in the left column: a row of a table wherever
    # it stands, because the gap before its last cell can fall on the gutter, and a line of the
    # title or author block at either end of the region. Cutting such a line would break a word and
    # move its end far from its start. A line of prose is cut at the gutter as usual, or the two
    # columns would run into each other.
    page_width = max((len(l.rstrip()) for l in lines if l.strip()), default=0)
    whole = [_last_cell_row(region, i, g)
             or (not _blank_at(l, g) and (_spanning_row(l, g) or _spans_with_cells(l, g)
                                          or i < 3 or i >= len(region) - 3
                                          or _centred_across(l, page_width)))
             for i, l in enumerate(region)]
    left = [l.rstrip() if w else l[:g].rstrip() for l, w in zip(region, whole)]
    right = ["" if w else l[g:].rstrip() for l, w in zip(region, whole)]
    top, n_top, _ = _layout(lines[:top_end], g, allow_short=True)
    bottom, n_bottom, _ = _layout(lines[bottom_start:], g, allow_short=True)
    # Two columns of text carry comparable amounts on either side of the gutter, and few of their
    # lines are rows of a table. A gap inside a table leaves a column of short cells beside a wide
    # one, so a table never decides where the paper's gutter is.
    filled = sum(len(l[g:].rstrip()) for l in region)
    # Every wide gap counts here, the one that falls on the cut as well. A row of a table keeps its
    # cells apart wherever the cut lands, and leaving that gap out is what lets a table read as prose
    # at the very column where cutting it would scramble its rows.
    prose = (2 * sum(len(_WIDE_GAP.findall(l)) >= 2 for l in region) <= len(region)
             and filled >= 0.3 * sum(len(l[:g].rstrip()) for l in region))
    return (top + lines[top_end:start] + left + _align(left, right) + lines[end:bottom_start]
            + bottom, 1 + n_top + n_bottom, g if prose else None)


# --- Line numbers ---

# A numbering is the run of numbers that one column prints down its margin, counting up line after
# line. A paper sets one for each column of a page.

# A number set apart from the text by two or more spaces. A paper prints its line numbers in a
# margin of their own, so nothing stands right beside them.
_LONE_NUMBER = re.compile(r"(?<=\s{2})(\d{1,4})(?=\s|$)")
# A number in the margin at the start of a line, where only the indent stands before it. Two spaces
# must follow it, because the marker of a footnote also starts its line but its text follows at once.
_MARGIN_NUMBER = re.compile(r"^\s*(\d{1,4})(?=\s{2}|\s*$)")
# A number that ends a line. One space is enough to set it apart there, because a paper prints its
# line numbers at the right margin as well, where nothing follows them.
_END_NUMBER = re.compile(r"(?<=\s)(\d{1,4})\s*$")
# How many numbers in one column make a numbering.
_MIN_NUMBERING = 6
# How much of the paper a numbering runs through, as a share of its lines. A numbering that pdftotext
# renders in pieces reaches about half of a page, so the share is set below that.
# `_near_a_caption` and `_reads_as_prose` together keep a column of a table from passing.
_NUMBERING_SPAN = 0.4
# How many lines may stand between two numbered lines. A paper numbers every line, or every fifth
# or tenth line, and a figure or a table can stand between two of them.
_NUMBERING_GAP = 40
# A run of digits set off from what follows it. pdftotext prints the line number of the right column
# inside the text of the left one, sometimes glued to its last word, as in "of97" or "S2299".
_GUTTER_NUMBER = re.compile(r"(\d{1,4})(?=\s{2,}|$)")
# The smallest distance between the numbering of one column and the numbering of the next. A column
# holds many lines, so the two numberings are far apart.
_MIN_COLUMN_OFFSET = 5
# How far past the last number of a page's own numbering the numbering of its next column may
# start. The next column takes up the count where this one stopped.
_NUMBERING_RESUMES = 10
# How far a number may stand from the column of its numbering. pdftotext sets a right-aligned number
# a character to either side of where the numbers above and below it stand.
_COLUMN_NEAR = 2


def _table_line(line: str) -> bool:
    """Whether `line` has two or more gaps between cells, as a row of a table has. Numbers in such
    a line are cells of the table, not line numbers."""
    return len(_WIDE_GAP.findall(line)) >= 2


def _last_cell_of_a_row(line: str, at: int) -> bool:
    """Whether the number at `at` is the last cell of a table row rather than a line number.

    A table of two columns, such as a study beside its year, counts up down its last column just as
    line numbers do, and has one gap per row where `_table_line` looks for two. What stands before
    the gap tells them apart: a cell holds a few words, and the line of prose that a line number
    follows holds a sentence.
    """
    before = line[:at].rstrip()
    # The gap is the spaces right before the number, so it has to be measured there: searching
    # `line[:at]` for a gap between two characters cannot see one whose right side is the number.
    return bool(re.search(r"\S\s{3,}$", line[:at])) and len(before.split()) <= _CELL_WORDS


# How many words stand before the gap in a table row, rather than in a line of prose.
_CELL_WORDS = 5


def _counted_numbers(lines: list[str]) -> dict[int, int]:
    """Line numbers at the end of lines, with or without a space before them, as {line index:
    number}. pdftotext can put the line numbers of the right column at the end of the left
    column's lines and glue them to the last word, as in "observed53" or "of 1257" for "of 12".
    A number counts when it belongs to a run of at least six numbered lines that count up by the
    same step of one, five, or ten, with at most that many lines in between. A row of a table is
    left out, because the last column of a table can count up as well. The part of a decimal number
    after its point is left out too."""
    nb = [i for i, l in enumerate(lines) if l.strip()]
    digits = []
    for i in nb:
        line = lines[i].rstrip()
        m = _TRAILING_DIGITS.search(line)
        before = line[:m.start(1)] if m else ""
        decimal = before[-1:] in (".", ",") and before[-2:-1].isdigit()
        # A word that ends in a digit, such as "Qwen2", must not seed a run of line numbers.
        label = (w := _GLUED_END.search(line)) and w.group(1)[:1].isupper()
        digits.append("" if not m or decimal or label or _table_line(line)
                      or _last_cell_of_a_row(line, m.start(1)) else m.group(1))
    found: dict[int, int] = {}
    k = 0
    while k < len(nb):
        best: list[tuple[int, int]] = []
        for size in range(min(4, len(digits[k])), 0, -1):
            for step in (1, 5, 10):
                run = [(k, int(digits[k][-size:]))]
                while True:
                    j, n = run[-1]
                    nxt = next((x for x in range(j + 1, min(j + step + 2, len(nb)))
                                if digits[x].endswith(str(n + step))), None)
                    if nxt is None:
                        break
                    run.append((nxt, n + step))
                if len(run) > len(best):
                    best = run
        if len(best) >= 6:
            # A line number ends where the paper prints its line numbers, or is glued to the text.
            # A number that ends a sentence stands elsewhere and stays.
            column = Counter(len(lines[nb[j]].rstrip()) for j, _ in best).most_common(1)[0][0]
            for j, n in best:
                body = lines[nb[j]].rstrip()
                glued = not body[:-len(str(n))].endswith(" ")
                if glued or abs(len(body) - column) <= 1:
                    found[nb[j]] = n
            k = best[-1][0] + 1
        else:
            k += 1
    return found


def _cut_number(line: str, n: int) -> str:
    body = line.rstrip()
    return body[:-len(str(n))].rstrip() if body.endswith(str(n)) else line


def _cut_stray_numbers(lines: list[str], counted: dict[int, int]) -> list[str]:
    """In a numbered paper, a number glued to the end of a line that continues the numbering of a
    line nearby is a line number as well, even where it belongs to no run of its own. Where the two
    columns of a page interleave, pdftotext leaves such numbers behind."""
    if not counted:
        return lines
    nb = [i for i, l in enumerate(lines) if l.strip()]
    glued = {pos: m for pos, i in enumerate(nb) if i not in counted and (m := _glued_number(lines[i]))}
    out = list(lines)
    for pos, m in glued.items():
        value = int(m.group(2))
        near = [int(other.group(2)) for far, other in glued.items() if 0 < abs(far - pos) <= 12]
        # The numbers of one column advance with its lines, so a neighbor of the same numbering is
        # close in value as well.
        if any(0 < abs(value - n) <= 15 for n in near):
            i = nb[pos]
            out[i] = lines[i][:m.start(2)].rstrip()
    return out


def _number_chain(hits: list, step: int) -> list:
    """The longest chain in `hits`, triples of (line index, number, whatever the caller needs),
    whose numbers count up by `step` down the lines.

    Whatever stands between two numbers of the chain is stepped over, so that a table in the middle
    of a numbered page does not end the numbering, and so that a column of a table that counts up on
    its own few lines forms a chain of its own rather than joining the one around it."""
    best = None
    nearest: dict[int, tuple[int, tuple]] = {}
    below: dict[tuple, tuple | None] = {}
    for hit in sorted(hits, key=lambda h: -h[0]):
        following = nearest.get(hit[1] + step)
        if following and following[1][0] - hit[0] <= _NUMBERING_GAP:
            length, follows = following[0] + 1, following[1]
        else:
            length, follows = 1, None
        below[hit] = follows
        nearest[hit[1]] = (length, hit)
        if best is None or length > best[0]:
            best = (length, hit)
    chain: list = []
    hit = best[1] if best else None
    while hit is not None:
        chain.append(hit)
        hit = below[hit]
    return chain


def _runs_down_the_page(hits: list, bounds: list[tuple[int, int]], reach: int | None = None) -> list:
    """The chains of `hits` that run down the paper as a numbering does, longest first. A chain is
    kept only where it reaches across the pages it stands on. A column of a table counts up on its
    own few lines and no further."""
    kept, left = [], list(hits)
    while len(left) >= _MIN_NUMBERING:
        chain = max((_number_chain(left, step) for step in (1, 5, 10)), key=len)
        across = reach if reach is not None else sum(
            e - b for b, e in bounds if any(b <= i < e for i, _, _ in chain))
        if len(chain) < _MIN_NUMBERING or chain[-1][0] - chain[0][0] < _NUMBERING_SPAN * across:
            break
        kept.append(chain)
        taken = set(chain)
        left = [h for h in left if h not in taken]
    return kept


# How far above or below a block its caption may stand. A caption of several printed lines, or one
# set below its table, has to be seen from the block.
_CAPTION_NEAR = 12
# The share of a block's lines that must carry on a sentence for the block to read as prose.
_PROSE_SHARE = 0.10
# A line that ends a sentence.
_SENTENCE_TAIL = re.compile(r"[.!?]$")


def _near_a_caption(lines: list[str], first: int, last: int) -> bool:
    """Whether the caption of a table or a figure stands above or below the block that runs from
    line `first` to line `last`. Journals set a table's caption on either side of it, and a caption
    of several printed lines reaches further, so both sides are read."""
    above = [lines[j].strip() for j in range(max(0, first - _CAPTION_NEAR), first) if lines[j].strip()]
    below = [lines[j].strip() for j in range(last + 1, min(len(lines), last + 1 + _CAPTION_NEAR))
             if lines[j].strip()]
    return any(_TABLE_CAPTION.match(x) or _CAPTION.match(x) for x in above + below)


def _reads_as_prose(lines: list[str], chain: list) -> bool:
    """Whether the block reads as the running text of a paper. Enough of its lines carry on the
    sentence of the line before, which they show by starting in lower case, and at least one line
    ends a sentence. A table of rows that begin in lower case, such as rows named after tools or
    files, starts lines in lower case but never ends a sentence, because a row is a phrase that stands on its own."""
    carries = ends = rest = 0
    for i, number, start in chain:
        line = lines[i]
        body = (line[:start] + " " * len(str(number)) + line[start + len(str(number)):]).strip()
        if body:
            rest += 1
            carries += body[0].islower()
            ends += bool(_SENTENCE_TAIL.search(body))
    return bool(rest) and carries / rest >= _PROSE_SHARE and ends > 0


def _in_the_margin(line: str, start: int, over: int) -> bool:
    """Whether the number that fills the columns `start` to `over` stands in a margin of `line`,
    with nothing but space to its left or nothing but space to its right. A paper prints its line
    numbers in a margin. This is not enough on its own: the first and the last column of a table
    have the edge of the line beside them as well, which is what `_row_of_cells` is for."""
    return not line[:start].strip() or not line[over:].strip()


def _row_of_cells(line: str) -> bool:
    """Whether `line` is a row of a table, once a number at either end of it is set aside.

    A row holds several cells kept apart by runs of spaces, while a numbered line of text holds one
    run of words after its number. Without this, the first or the last column of a table counts up as
    readily as a numbering does, with nothing but the edge of the line beside it, and a whole column
    of the table would be read as line numbers and blanked.

    Two cells are a row only where both are short. A page that sets two columns has pdftotext print
    them on one line with a gap between, and that line would otherwise read as a row of two cells."""
    m = _MARGIN_NUMBER.match(line)
    body = line[m.end(1):] if m else line
    t = _END_NUMBER.search(body)
    cells = [c for c in re.split(r" {3,}", (body[:t.start(1)] if t else body).strip()) if c]
    return len(cells) > 2 or (len(cells) == 2 and max(len(c) for c in cells) <= _CELL_TEXT)


def _numbering_columns(lines: list[str],
                       bounds: list[tuple[int, int]]) -> dict[int, list[tuple[int, int]]]:
    """The line numbers that the paper prints in a column of its own, as
    {line index: [(number, the column it starts in)]}.

    They count up through the paper, which is what tells them apart from the numbers in a table: a
    column of a table counts up as well, but it does so on the lines of that table alone. Each
    column of a two-column page carries a numbering of its own, and a page can hold the end of one
    and the start of the next, so every chain in a column is taken, not only the longest."""
    seen: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
    for i, line in enumerate(lines):
        if _row_of_cells(line):
            continue
        starts = {m.start(1): m for m in _LONE_NUMBER.finditer(line)}
        starts.update({m.start(1): m for m in _MARGIN_NUMBER.finditer(line)})
        starts.update({m.start(1): m for m in _END_NUMBER.finditer(line)})
        for m in starts.values():
            for key in (("start", m.start(1)), ("end", m.end(1))):
                seen.setdefault(key, []).append((i, int(m.group(1)), m.start(1)))
    found: dict[int, list[tuple[int, int]]] = {}
    for hits in _merge_near_columns(seen).values():
        if len(hits) < _MIN_NUMBERING:
            continue
        for chain in _runs_down_the_page(hits, bounds):
            # Most of a chain must stand in a margin, not all of it. pdftotext pushes a number
            # out of the margin where the line beside it runs long, and one such line must not
            # cost the paper its numbering.
            if 2 * sum(_in_the_margin(lines[i], start, start + len(str(n)))
                       for i, n, start in chain) < len(chain):
                continue
            # The first and the last column of a table stand in a margin as well, and a table of
            # enough rows counts up as far as a numbering does. Such a column has a caption above
            # or below its block and does not read as the running text of a paper. Both have to
            # hold, because a paper prints figures among its text as well, and a table can hold a
            # sentence.
            if _near_a_caption(lines, chain[0][0], chain[-1][0]) and not _reads_as_prose(lines, chain):
                continue
            for i, n, start in chain:
                # The same number is found by the column it starts in and by the column it ends in.
                if (n, start) not in found.setdefault(i, []):
                    found[i].append((n, start))
    return found


def _merge_near_columns(seen: dict[tuple[str, int], list[tuple[int, int, int]]]
                        ) -> dict[tuple[str, int], list[tuple[int, int, int]]]:
    """`seen` with the columns that stand within _COLUMN_NEAR of each other read as one column,
    because pdftotext sets a right-aligned number a character to either side of its neighbors."""
    merged: dict[tuple[str, int], list[tuple[int, int, int]]] = {}
    for edge in ("start", "end"):
        columns = sorted(col for side, col in seen if side == edge)
        group: list[int] = []
        for col in columns + [None]:
            if group and (col is None or col - group[0] > _COLUMN_NEAR):
                merged[(edge, group[0])] = sorted({hit for c in group for hit in seen[(edge, c)]})
                group = []
            if col is not None:
                group.append(col)
    return merged


def _numbering_spans(lines: list[str],
                     bounds: list[tuple[int, int]] | None = None) -> dict[int, list[tuple[int, int]]]:
    """Where the paper prints a line number, as {line index: [(start column, end column)]}."""
    bounds = bounds if bounds is not None else [(0, len(lines))]
    found = _numbering_columns(lines, bounds)
    spans: dict[int, list[tuple[int, int]]] = {}
    for i, numbers in found.items():
        for value, start in numbers:
            spans.setdefault(i, []).append((start, start + len(str(value))))
    for start, end in bounds:
        page = {i: numbers for i, numbers in found.items() if start <= i < end}
        _cut_other_column(lines, range(start, end), page, spans)
    return spans


def _cut_other_column(lines: list[str], page: range, found: dict[int, list[tuple[int, int]]],
                      spans: dict[int, list[tuple[int, int]]]) -> None:
    """Add the line numbers of the other column of `page` to `spans`.

    On a two-column page pdftotext prints both columns on one line, so the line numbers of the right
    column land inside the text of the left one, where no column holds them and where they are
    sometimes glued to a word, as in "of97" or "S2299". They are still a numbering: they count up
    line after line down the page, and that is how they are found here. Following them rather than
    measuring one distance from the left column holds where a heading in one column shifts the
    other, and a number is taken only where it continues the count, so a number in the text stays."""
    numbers = sorted({value for i in page if i in found for value, _ in found[i]})
    if len(numbers) < _MIN_NUMBERING:
        return
    step = min((b - a for a, b in zip(numbers, numbers[1:]) if b > a), default=1)
    hits: list = []
    for i in page:
        mine = spans.get(i, [])
        ours = {value for value, _ in found.get(i, ())}
        for m in _GUTTER_NUMBER.finditer(lines[i]):
            if any(begin <= m.start(1) < over for begin, over in mine):
                continue
            digits = m.group(1)
            for size in range(1, len(digits) + 1):
                value = int(digits[-size:])
                # The numbering of a column of its own is found already, and the two are far apart.
                if all(value - n >= _MIN_COLUMN_OFFSET for n in ours):
                    hits.append((i, value, (m.end(1) - size, m.end(1))))
    # The next column takes up the count where this one stopped, which a column of a table does not.
    left = list(hits)
    while len(left) >= _MIN_NUMBERING:
        chain = max((_number_chain(left, step) for step in (1, 5, 10)), key=len)
        if len(chain) < _MIN_NUMBERING:
            return
        if numbers[-1] < chain[0][1] <= numbers[-1] + _NUMBERING_RESUMES * step:
            for i, _, (begin, over) in chain:
                spans.setdefault(i, []).append((begin, over))
            return
        taken = set(chain)
        left = [h for h in left if h not in taken]


def _strip_line_numbers(lines: list[str], bounds: list[tuple[int, int]] | None = None) -> tuple[list[str], bool]:
    """`lines` without the line numbers, and whether the paper is numbered. A number is replaced by
    spaces rather than cut out, so that the text keeps its columns and the page can still be split
    between them afterwards. A number that pdftotext glued to the last word of a line is cut,
    because no column holds it."""
    spans = _numbering_spans(lines, bounds)
    counted = _counted_numbers(lines)
    if not spans and not counted:
        return lines, False
    out = []
    for i, line in enumerate(lines):
        for start, end in sorted(spans.get(i, []), reverse=True):
            line = line[:start] + " " * (end - start) + line[end:]
        # A line with its number in a column is cut there already. Cutting again would take the
        # last digit of the text with it.
        if i in counted and i not in spans:
            line = _cut_number(line, counted[i])
        out.append(line.rstrip())
    return _cut_stray_numbers(out, counted), True


def _clean_line_numbers(page_lines: list[list[str]]) -> tuple[list[list[str]], bool]:
    """Every page without its line numbers. The numbering is found over the whole paper, because a
    single page can have too few numbered lines to tell one from a column of a table."""
    bounds, k = [], 0
    for lines in page_lines:
        bounds.append((k, k + len(lines)))
        k += len(lines)
    flat, found = _strip_line_numbers([l for lines in page_lines for l in lines], bounds)
    pages, k = [], 0
    for lines in page_lines:
        pages.append(flat[k:k + len(lines)])
        k += len(lines)
    return pages, found


# --- References ---

def _heading_key(line: str) -> str:
    return _HEADER_NUM.sub("", line.strip().rstrip(" .:").lower()).replace(" ", "")


def _is_references_heading(line: str) -> bool:
    """Whether `line` is the heading of a bibliography.

    Not one written with a colon: a section heading does not carry one, while a prompt that a paper
    quotes says "References:" before showing an entry, and taking that for the paper's own would
    remove every claim printed after it.
    """
    text = line.strip()
    return (0 < len(text) <= 40 and not text.endswith((".", ":"))
            and _heading_key(line) in _REFERENCE_HEADINGS)


def _next_lines(pages: list[Page], pi: int, li: int, n: int) -> list[str]:
    """The next `n` non-blank lines after line `li` of page `pi`."""
    out: list[str] = []
    for pj in range(pi, len(pages)):
        for line in pages[pj].lines[li + 1 if pj == pi else 0:]:
            if line.strip():
                out.append(line)
                if len(out) == n:
                    return out
    return out


def _entries_follow(following: list[str]) -> bool:
    """Whether the lines below read as bibliography entries rather than an ordinary numbered list.

    "1. How many years have you reviewed code?" opens a survey instrument in an appendix and looks
    like "1. A. Smith, ..." to `_BIB_ENTRY`. An entry also carries what an entry carries: a link, a
    page range, a volume, a venue, or a year.
    """
    return any(_BIB_ENTRY.match(l) and (_BIB_LOOK.search(l) or not re.match(r"^\s*\d{1,3}\.", l))
               for l in following)


def _bibliography_follows(pages: list[Page], pi: int, li: int) -> bool:
    """Whether a bibliography follows this heading."""
    return any(_BIB_ENTRY.match(x) for x in _next_lines(pages, pi, li, 12))


def _is_appendix_heading(line: str, following: list[str]) -> bool:
    """Whether `line`, after the bibliography, starts an appendix. A heading labeled with a letter,
    as in "A Additional Results" or "B Details for RQ1", counts only if it does not end or read like
    part of a bibliography entry and no entry follows it in `following`, because a wrapped title in
    the bibliography can also start with "A"."""
    text = line.strip()
    if not 0 < len(text) <= 80:
        return False
    label = _LETTER_LABEL.match(text)
    rest = _LETTER_LABEL.sub("", text)
    if _heading_key(text).startswith(_APPENDIX_HEADINGS) or _heading_key(rest).startswith(_APPENDIX_HEADINGS):
        return True
    if (len(text) <= 60 and not text.endswith((".", ",")) and not _entries_follow(following)
            and not re.search(r"https?:|doi|\bpp\.|\bvol\.|\bProc\b|\(\d{4}\)", text, re.I)
            and (_heading_key(text).startswith(_APPENDIX_WORDS)
                 or _heading_key(rest).startswith(_APPENDIX_WORDS))):
        return True
    if label and label.group().rstrip()[-1] in ".)" and (
            "," in rest or any(q in rest for q in _QUOTE_MARKS) or len(rest.split()) > 8):
        return False
    return (rest != text and len(text) <= 60 and not text.endswith((".", ","))
            and not re.search(r"https?:|doi|\bpp\.|\bvol\.|\bProc\b|\(\d{4}\)", text, re.I)
            and not _entries_follow(following))


# What a line of a bibliography carries: a link, a page range, a volume, a venue, or a year.
_BIB_LOOK = re.compile(r"https?:|doi|\bpp\.|\bvol\.|\bno\.|\bProc\b|\bConf\b|\bJ\.|\bIEEE\b|\bACM\b"
                       r"|\(\d{4}\)|\b(?:19|20)\d{2}[.,)]?$|\b(?:19|20)\d{2}[.,]")
# How many lines of running text in a row end a bibliography.
_BODY_RUN = 6


def _stands_early(pages: list[Page], pi: int, li: int, share: float = 2 / 3) -> bool:
    """Whether line `li` of page `pi` stands in the first `share` of the paper's lines. A
    bibliography that starts there cannot be the tail of the paper."""
    before = sum(1 for pj in range(pi) for l in pages[pj].lines if l.strip())
    before += sum(1 for l in pages[pi].lines[:li] if l.strip())
    total = sum(1 for p in pages for l in p.lines if l.strip())
    return bool(total) and before < share * total


def _text_resumes(pages: list[Page], pi: int, li: int) -> tuple[int, int] | None:
    """Where the bibliography that starts at line `li` of page `pi` gives way to running text again.

    Removal would otherwise run to the end of the paper, because a bibliography is the last thing
    a paper prints. It is not always: the columns of its page can interleave, an appendix heading
    can go unrecognized, and a paper about prompting prints "References:" inside a prompt it
    quotes, anywhere at all. A run of lines carrying none of the marks of a bibliography entry ends
    the removal there. Keeping a few entries leaves a little noise in `text.txt`, while removing the
    body would lose text the paper needs."""
    run: tuple[int, int] | None = None
    seen = 0
    for pj in range(pi, len(pages)):
        for lj in range(li + 1 if pj == pi else 0, len(pages[pj].lines)):
            line = pages[pj].lines[lj]
            if not line.strip():
                continue
            # The same rule `_is_appendix_heading` uses: a numbered line is a bibliography entry
            # only where it carries what an entry carries. An appendix opening with a numbered
            # questionnaire would otherwise reset this run at every item, and the removal would
            # never stop, taking the appendix's own results with it.
            if _entries_follow([line]) or _BIB_LOOK.search(line):
                run, seen = None, 0
                continue
            run = run or (pj, lj)
            seen += 1
            if seen >= _BODY_RUN:
                return run
    return None


def _drop_references(pages: list[Page]) -> tuple[int, int | None] | None:
    """Blank everything from the first References heading that a bibliography entry follows, to
    the next appendix heading or the end. Author biographies after an IEEE bibliography are removed
    with it.

    A heading written with a colon is not one: a paper about prompting quotes a prompt that says
    "References:", and taking that for the paper's own would remove everything after it."""
    # The first heading a bibliography follows. A later one belongs to an appendix, and removing
    # from there would take the appendix's own results with it while leaving the paper's real
    # bibliography in the text, where its entries read as sentences of this paper.
    start = next(((pi, li) for pi, p in enumerate(pages) for li, l in enumerate(p.lines)
                  if _is_references_heading(l) and _bibliography_follows(pages, pi, li)), None)
    if start is None:
        return None
    pi, li = start
    end = next(((pj, lj) for pj in range(pi, len(pages))
                for lj in range(li + 1 if pj == pi else 0, len(pages[pj].lines))
                if pages[pj].lines[lj].strip()
                and _is_appendix_heading(pages[pj].lines[lj], _next_lines(pages, pj, lj, 12))), None)
    # Only where the heading stands early: see `_stands_early`. A heading in the last third is the
    # paper's own, and the biographies after it go with it.
    resumes = _text_resumes(pages, pi, li) if _stands_early(pages, pi, li) else None
    stop = min([x for x in (end, resumes) if x], default=(len(pages) - 1, len(pages[-1].lines)))
    # The page reported is the one the removal reached, which is the appendix heading only where the
    # removal ran that far.
    end = end if end == stop else None
    for pj in range(pi, stop[0] + 1):
        lo = li if pj == pi else 0
        hi = stop[1] if pj == stop[0] else len(pages[pj].lines)
        for k in range(lo, hi):
            pages[pj].lines[k] = ""
    pages[pi].lines[li] = REFERENCES_REMOVED
    if end is None and resumes:
        # Where the text starts again, so that `extract` can say the removal stopped there rather
        # than running to the end of the paper.
        pages[resumes[0]].resumed = True
    return pages[pi].number, (pages[end[0]].number if end else None)


# --- Entry points ---

# How many of a page's lines must be blank at a column for it to be read as the gutter where no
# gutter runs the whole height of the page.
_PAGE_GUTTER_BLANK = 0.8


def _page_gutter(lines: list[str]) -> int | None:
    """The gutter of `lines`, whether or not the page reads as two columns of prose.

    A page with its right column filled for part of its height only, as a page that ends in a
    bibliography is, has no gutter that runs its whole height. The column that is blank on most of
    its lines and carries text on both sides is taken for it, because leaving such a page unsplit
    puts the text of the two columns on one line, where a sentence of one runs into the other."""
    nb = [l for l in lines if l.strip()]
    if len(nb) < _MIN_REGION_LINES:
        return None
    found = _gutter(nb)
    if found is None:
        # The gutter is the column with the most text on the side that carries less of it. Ranking
        # by the blank lines alone would take the right edge of the text instead, where the page is
        # blank on almost every line and a few long lines reach past it.
        best = None
        for c in range(_MIN_GUTTER, max(len(l) for l in nb)):
            blank = sum(1 for l in nb if _blank_at(l, c))
            if blank < _PAGE_GUTTER_BLANK * len(nb):
                continue
            left = sum(1 for l in nb if l[:c - 1].strip())
            right = sum(1 for l in nb if len(l) > c + 2 and l[c + 2:].strip())
            if min(left, right) >= _MIN_SIDE_LINES and (best is None or (min(left, right), blank) > best[0]):
                best = ((min(left, right), blank), c)
        found = best[1] if best else None
    # Only a page of prose is cut this way. A page filled with a wide table has a gap between two of
    # its columns that is blank just as often, and cutting there scrambles the rows of the table.
    if found is None or 2 * sum(_cell_gaps(l, found) >= 2 for l in nb) > len(nb):
        return None
    return found


def _read_pages(page_lines: list[list[str]]) -> tuple[list[Page], bool]:
    """Each page with its lines in reading order, and whether the paper is set in two columns.

    The pages are read twice. The first pass collects the gutters at which the paper's two-column
    prose is set, and the second pass cuts pages only near one of them. A gap inside a wide table is
    then never taken for a gutter. A table that continues on a page without its caption stays whole,
    and a paper printed in one column keeps every page as it is.

    One case stays wrong: In a paper printed in one column, a table of three or more columns that
    continues on a page without its caption can still be cut at a gap between two of its columns,
    which puts its cells in the wrong order. The prose of the page is not affected, and a number
    that stands only in a table is evidence for a claim rather than a claim."""
    laid = [_layout(lines) for lines in page_lines]
    votes = Counter(g for _, _, g in laid if g is not None)
    allowed = set(votes)
    if not allowed:
        # No page reads as two columns of prose, but a page may still hold a region, for example
        # where one column is much shorter than the other. Its gutter is better than none.
        allowed = {c for (_, regions, _), page in zip(laid, page_lines) if regions
                   for c in [_page_gutter(page)] if c}
    known = votes.most_common(1)[0][0] if votes else None
    pages = []
    for number, lines in enumerate(page_lines, 1):
        ordered, regions = lines, 0
        if allowed:
            ordered, regions, _ = _layout(lines, known, allowed)
            if not regions:
                # A page with too few lines for a region, such as a short last page.
                ordered, regions, _ = _layout(lines, known, allowed, allow_short=True)
        pages.append(Page(number, ordered, regions))
    return pages, bool(allowed)


def extract(pdf_path: str) -> Extraction:
    raw, said = _pages(pdf_path)
    headers = _running_headers(raw)
    page_lines = [[l.rstrip() for l in text.split("\n")] for text in raw]
    numbers = _page_number_lines(page_lines)
    for pi, lines in enumerate(page_lines):
        _blank_edges(lines, headers, numbers.get(pi, frozenset()))
    # The line numbers are removed before the columns are split, because pdftotext prints them in a
    # column of their own, where they stand apart from the text. Once the columns of a page are put
    # in reading order, the numbers of one column sit inside the sentences of the other, where a
    # number in a sentence and a cell of a table can no longer be told apart.
    page_lines, lineno = _clean_line_numbers(page_lines)
    pages, two_column = _read_pages(page_lines)
    references = _drop_references(pages)
    resumed = next((p.number for p in pages if p.resumed), None)
    return Extraction(pages, lineno, references, two_column, said, resumed)


PAGE_MARKER = re.compile(r"^=== page (\d{1,6}) ===$")


def to_text(extraction: Extraction) -> str:
    """The pages as text, each after a `=== page N ===` line, with runs of blank lines collapsed.

    A body line that reads as a page marker is indented by one space, so that the paper's own text
    cannot open a page of its own.
    """
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
            # A paper that prints this exact shape in its body would otherwise start a new page
            # in text.txt, and everything recorded for the real page would be read as that one's.
            # One space keeps the words and stops the marker.
            out.append(" " + line if PAGE_MARKER.match(line) else line)
            previous_blank = False
        if out and out[-1] == "":
            out.pop()
    return "\n".join(out) + "\n"
