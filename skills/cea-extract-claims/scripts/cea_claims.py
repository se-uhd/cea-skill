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
SOURCES = ("abstract", "contributions", "conclusion", "other")
# Marks where a figure, table, footnote, or page break interrupts a quoted sentence in text.txt.
GAP = re.compile(r"\s*\[(?:\.\.\.|…)\]\s*")
# How far apart, in normalized characters, the parts of a quote on either side of a gap may be.
MAX_GAP = 4000

# Required and optional fields per entry type.
FIELDS = {
    "paper": ({"id", "title", "pdf", "pages"}, set()),
    "broad_statements": ({"id", "quote", "page", "section", "source"}, {"note"}),
    "claims": ({"id", "quote", "text", "page", "section", "serves", "split_from",
                "selection_reason"}, {"note"}),
    "rejected": ({"id", "quote", "page", "section", "reason"},
                 {"text", "split_from", "duplicate_of", "note"}),
}

# ── Quote matching ───────────────────────────────────────────────────────────

_FOLD = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "―": "-",
    "−": "-", "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "`": "'", "´": "'", "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "­": None,
})
# A footnote number that pdftotext puts directly after a word or punctuation mark, as in
# "revisions,13 assisted". A quote may leave it out.
_FOOTNOTE = re.compile(r"(?:(?<=[^\W\d_])|(?<=[.,;:!?)\]\"']))\d{1,2}(?=[\s.,;:!?)\]]|$)")


def normalize(text: str, footnotes: bool = False) -> tuple[str, tuple[bool, ...]]:
    """`text` casefolded and without whitespace or hyphens, with one flag per character that is
    True for a footnote number a quote may skip.

    Whitespace is dropped because line breaks and spacing differ between text.txt and a quote.
    Hyphens are dropped because a word broken across two lines keeps its hyphen in text.txt."""
    text = unicodedata.normalize("NFKC", text).translate(_FOLD)
    marked = [False] * len(text)
    if footnotes:
        for m in _FOOTNOTE.finditer(text):
            marked[m.start():m.end()] = [True] * (m.end() - m.start())
    chars: list[str] = []
    flags: list[bool] = []
    for ch, mark in zip(text, marked):
        if ch.isspace() or ch == "-":
            continue
        for folded in ch.casefold():
            chars.append(folded)
            flags.append(mark)
    return "".join(chars), tuple(flags)


@lru_cache(maxsize=512)
def _normalized_page(text: str) -> tuple[str, tuple[bool, ...]]:
    return normalize(text, footnotes=True)


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


def quote_on(quote: str, page_text: str) -> bool:
    """Whether `quote` occurs in `page_text`, allowing for layout differences, footnote numbers
    and `[...]` gaps of at most MAX_GAP characters."""
    parts = [normalize(part)[0] for part in GAP.split(quote.strip())]
    if not parts or any(not part for part in parts):
        return False
    p, skippable = _normalized_page(page_text)

    def search(i: int, lo: int, hi: int) -> bool:
        part = parts[i]
        pos = p.find(part[0], lo)
        while pos != -1 and pos < hi:
            end = _match_end(part, p, skippable, pos)
            if end >= 0 and (i + 1 == len(parts) or search(i + 1, end, end + MAX_GAP + 1)):
                return True
            pos = p.find(part[0], pos + 1)
        return False

    return search(0, 0, len(p))


_WORD = re.compile(r"[^\W_]+(?:[.,][0-9]+)*")


def _words(text: str) -> set[str]:
    """The words and numbers in `text`, casefolded, with hyphens treated as spaces."""
    text = unicodedata.normalize("NFKC", text).translate(_FOLD).replace("-", " ")
    return {w.casefold() for w in _WORD.findall(text)}


# ── Validation ───────────────────────────────────────────────────────────────

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


def _page_span(value, n_pages: int) -> tuple[int, int] | str:
    if isinstance(value, int) and not isinstance(value, bool):
        lo = hi = value
    elif isinstance(value, str) and re.fullmatch(r"\d+-\d+", value):
        lo, hi = map(int, value.split("-"))
        if hi <= lo:
            return f'"{value}" must name a later page after the dash'
    else:
        return 'must be a page number, or a range such as "5-6"'
    if not 1 <= lo <= hi <= n_pages:
        return f"{value!r} is outside pages 1-{n_pages} of text.txt"
    return lo, hi


def _check_quote(problems: list[str], label: str, entry: dict, span: tuple[int, int],
                 pages: dict[int, str]) -> None:
    quote = entry["quote"]
    lo, hi = span
    if quote_on(quote, "\n".join(pages[n] for n in range(lo, hi + 1))):
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
                        "break interrupts it")


def validate(paper_dir: Path) -> tuple[list[str], dict | None]:
    """Problems found in paper_dir/claims.json, and the parsed data."""
    text_path, claims_path = paper_dir / "text.txt", paper_dir / "claims.json"
    if not text_path.is_file():
        return [f"{text_path} not found; run extract first"], None
    if not claims_path.is_file():
        return [f"{claims_path} not found"], None
    try:
        data = json.loads(claims_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [f"claims.json is not valid JSON: {e}"], None
    if not isinstance(data, dict):
        return ["claims.json must hold a JSON object"], None
    pages = load_pages(text_path)
    problems: list[str] = []

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
        if "pages" in paper and paper["pages"] != len(pages):
            problems.append(f"paper.pages: is {paper['pages']!r}, but text.txt has {len(pages)} pages")
    elif "paper" in data:
        problems.append("'paper' must be an object")

    lists: dict[str, list[dict]] = {}
    for key in ("broad_statements", "claims", "rejected"):
        value = data.get(key, [])
        if not isinstance(value, list):
            problems.append(f"'{key}' must be a list")
            value = []
        lists[key] = [e for e in value if isinstance(e, dict)]
        if len(lists[key]) != len(value):
            problems.append(f"'{key}' must hold only objects")

    owners: dict[str, str] = {}
    for key, entries in lists.items():
        for i, e in enumerate(entries):
            label = _label(key, i, e)
            check_fields(key, e, label)
            if not _nonempty(e.get("id")):
                problems.append(f"{label}.id: must be a non-empty string")
            elif e["id"] in owners:
                problems.append(f"{label}.id: '{e['id']}' is also used by {owners[e['id']]}")
            else:
                owners[e["id"]] = label
    broad_ids = {e.get("id") for e in lists["broad_statements"]}
    claim_ids = {e.get("id") for e in lists["claims"]}
    rejected_ids = {e.get("id") for e in lists["rejected"]}
    splits: dict[str, list[tuple[str, dict]]] = {}

    for key, entries in lists.items():
        for i, e in enumerate(entries):
            label = _label(key, i, e)
            texts = ["quote", "section"] + {"claims": ["text", "selection_reason"],
                                            "rejected": ["reason"]}.get(key, [])
            for f in texts:
                if f in e and not _nonempty(e[f]):
                    problems.append(f"{label}.{f}: must be a non-empty string")
            if "page" in e:
                span = _page_span(e["page"], len(pages))
                if isinstance(span, str):
                    problems.append(f"{label}.page: {span}")
                elif _nonempty(e.get("quote")):
                    _check_quote(problems, label, e, span, pages)

            if key == "broad_statements" and "source" in e and e["source"] not in SOURCES:
                problems.append(f"{label}.source: must be one of {', '.join(SOURCES)}")

            split = e.get("split_from")
            if split is not None and not _nonempty(split):
                problems.append(f"{label}.split_from: must be null or a non-empty string")
                split = None
            if split:
                splits.setdefault(split, []).append((label, e))
                if _nonempty(e.get("quote")) and _nonempty(e.get("text")):
                    extra = sorted(_words(e["text"]) - _words(GAP.sub(" ", e["quote"])))
                    if extra:
                        problems.append(f"{label}.text: uses words that are not in the quote: "
                                        f"{', '.join(extra[:8])}")

            if key == "claims":
                serves = e.get("serves")
                if not isinstance(serves, list) or not serves:
                    problems.append(f"{label}.serves: must list at least one broad statement id")
                else:
                    for ref in serves:
                        if ref not in broad_ids:
                            problems.append(f"{label}.serves: '{ref}' is not a broad statement id")
                if (not split and _nonempty(e.get("quote")) and _nonempty(e.get("text"))
                        and normalize(GAP.sub(" ", e["quote"]))[0] != normalize(e["text"])[0]):
                    problems.append(f"{label}.text: differs from the quote, but split_from is null; "
                                    "copy the quote, or set split_from if this claim is one part "
                                    "of a split statement")

            if key == "rejected":
                if "duplicate_of" in e:
                    refs = e["duplicate_of"] if isinstance(e["duplicate_of"], list) else [e["duplicate_of"]]
                    if not refs or not all(_nonempty(r) for r in refs):
                        problems.append(f"{label}.duplicate_of: must list the ids of the claims, "
                                        "rejected candidates, or broad statements that the statement repeats")
                    for ref in refs:
                        if not _nonempty(ref):
                            continue
                        if ref == e.get("id"):
                            problems.append(f"{label}.duplicate_of: an entry cannot repeat itself")
                        elif ref not in claim_ids | rejected_ids | broad_ids:
                            problems.append(f"{label}.duplicate_of: '{ref}' is not the id of a claim, "
                                            "rejected candidate, or broad statement")
                if split and not _nonempty(e.get("text")):
                    problems.append(f"{label}.text: a rejected part of a split statement needs "
                                    "the text of that part")

    for split, members in splits.items():
        if len(members) < 2:
            problems.append(f"{members[0][0]}.split_from: '{split}' has no other part")
        quotes = {normalize(GAP.sub(" ", e.get("quote") or ""))[0] for _, e in members}
        if len(quotes) > 1:
            problems.append(f"split_from '{split}': the parts ({', '.join(l for l, _ in members)}) "
                            "must share the same quote")

    return problems, data


# ── Rendering ────────────────────────────────────────────────────────────────

def _quote_block(text: str) -> list[str]:
    return [f"> {text.strip()}"]


def _first_page(entry: dict) -> int:
    try:
        return int(str(entry.get("page")).split("-")[0])
    except ValueError:
        return 0


def render(data: dict) -> str:
    paper, broad = data["paper"], data["broad_statements"]
    claims, rejected = data["claims"], data["rejected"]
    out = [
        f"# Claims: {paper['title']}",
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
                f"- Section: {b['section']}",
                f"- Narrow claims: {', '.join(serving) if serving else 'none selected'}"]
        if b.get("note"):
            out.append(f"- Note: {b['note']}")
        out.append("")

    out += ["## Narrow claims", ""]
    if not claims:
        out += ["None selected.", ""]
    for c in sorted(claims, key=_first_page):
        out += [f"### {c['id']}: page {c['page']}, {c['section']}", "", *_quote_block(c["quote"]), ""]
        if c["split_from"]:
            others = [x["id"] for x in claims + rejected
                      if x.get("split_from") == c["split_from"] and x is not c]
            out += [f"- Part: {c['text']}",
                    f"- Split from {c['split_from']}, with {', '.join(others)}"]
        out += [f"- Serves: {', '.join(c['serves'])}",
                f"- Selection reason: {c['selection_reason']}"]
        if c.get("note"):
            out.append(f"- Note: {c['note']}")
        out.append("")

    out += ["## Rejected candidates", ""]
    if not rejected:
        out += ["None recorded.", ""]
    for r in sorted(rejected, key=_first_page):
        out += [f"### {r['id']}: page {r['page']}, {r['section']}", "", *_quote_block(r["quote"]), ""]
        if r.get("split_from"):
            out.append(f"- Rejected part: {r['text']} (split from {r['split_from']})")
        if r.get("duplicate_of"):
            refs = r["duplicate_of"] if isinstance(r["duplicate_of"], list) else [r["duplicate_of"]]
            out.append(f"- Repeats: {', '.join(refs)}")
        out.append(f"- Reason: {r['reason']}")
        if r.get("note"):
            out.append(f"- Note: {r['note']}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"

# ── Commands ─────────────────────────────────────────────────────────────────

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
    paper_id = args.id or pdf.stem
    out_dir = Path(args.out) / paper_id
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path = out_dir / "text.txt"
    text_path.write_text(pdf_text.to_text(extraction), encoding="utf-8")
    columns = sum(1 for p in extraction.pages if p.regions)
    print(f"CEA_EXTRACTED: {text_path}")
    print(f"paper_id: {paper_id}")
    print(f"pages: {len(extraction.pages)}, {columns} of them with two-column text put in reading order")
    if extraction.references is None:
        print("references: no References heading found, so any bibliography is still in text.txt")
    else:
        first, appendix = extraction.references
        rest = f"up to the appendix heading on page {appendix}" if appendix else "to the end"
        print(f"references: removed from page {first} {rest}")
    empty = [str(p.number) for p in extraction.pages if not any(l.strip() for l in p.lines)]
    if empty:
        print(f"empty pages: {', '.join(empty)} (no text left, for example after removing references)")
    if extraction.lineno:
        print("line numbers: LaTeX margin line numbers removed")
    return 0


def cmd_validate(args) -> int:
    problems, data = validate(Path(args.paper_dir))
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s) in {Path(args.paper_dir) / 'claims.json'}")
        for p in problems:
            print(f"- {p}")
        return 1
    print(f"CEA_VALID: {len(data['broad_statements'])} broad statements, {len(data['claims'])} "
          f"claims, {len(data['rejected'])} rejected candidates; every quote found on its page")
    return 0


def cmd_render(args) -> int:
    paper_dir = Path(args.paper_dir)
    problems, data = validate(paper_dir)
    if problems:
        print(f"CEA_INVALID: {len(problems)} problem(s); run validate and fix them before rendering")
        return 1
    md_path = paper_dir / "claims.md"
    md_path.write_text(render(data), encoding="utf-8")
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
