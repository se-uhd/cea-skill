"""Cases for the `[...]` rules, built by recombining real pages.

Every fix to those rules so far has been a pattern added after a reader found a quote it got
wrong, and half of them broke something else. The rules read messy extracted layout, where each
tightening opens a new edge, and neither the 30 records nor the test suite could see any of it:
both were green through every one of those defects.

So the cases are generated instead of written. Real material is harvested from the papers --
sentences carried across two lines, and the caption, table, footnote and heading blocks a marker
stands for -- and recombined so that the answer is known by construction rather than by judgement:

  a sentence, a block, the rest of the sentence   -> the gap stands where the marker belongs
  a sentence, a block, then the paper's own prose -> a weld, and the same for prose alone
  anything with a section heading in the gap      -> a weld, since no sentence crosses one

`scripts/tests/test_scripts.py` runs this over a fixed seed and holds the two error rates to what
they were measured at. Run it directly to sweep more seeds:

    python3 scripts/gap_cases.py <seed> <rounds>
"""
import random
import re
import sys
from pathlib import Path

# Beside this file, not beside the caller: run from a directory holding another
# scripts/cea_claims.py and the harness measured that one against this repo's pages.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cea_claims as C

WORKSPACE = Path(__file__).resolve().parent.parent / "skills" / "cea-extract-claims-workspace"


# The material is classified WITHOUT the predicates under test. Reading a page with
# `_CAPTION_START` and friends meant a block shape a regex did not recognise was never drawn, so
# it was never a case -- and that is the class every defect here has belonged to. Deleting
# `_FOOTNOTE_LINE` outright left the old harness green. These patterns are deliberately separate
# and deliberately plain: they say what a caption, a footnote, a heading and a table row look
# like on the page, and if they and the validator's own patterns disagree, that disagreement is
# the finding.
# The number is followed by a stop, a capitalised title, or nothing. "Table XII presents the
# interpretability results" is a sentence about a table, not a caption.
CAPTION = re.compile(r"^(?:Fig\.?|Figure|FIGURE|Table|TABLE|Listing|Algorithm)"
                     r"\s*[\dIVXLC]+(?:[.:]|\s+[A-Z]|\s*$)")
# A marker and then a note: a capital, a link, or a bare domain. "6 out of the 16 actions"
# opens with a quantity and carries on in lower case, and is the paper, not a footnote.
FOOTNOTE = re.compile(r"^\d{1,2}\s+(?=[A-Z]|https?://|(?:[\w-]+\.)+[a-z]{2,}(?:[/\s]|$))")
HEADING = re.compile(r"^(?:[IVXLC]+(?:-[A-Z])?|\d+(?:\.\d+)*|[A-Z])\.?\s+\S")
CELLS = re.compile(r"\S\s{3,}\S")
PANEL = re.compile(r"^\((?:[a-z]|[ivx]{1,4})\)\s|^\([^()]*\)$")
WORDS = 8


def reads_as_the_paper(line: str) -> bool:
    """Whether a line is the paper's own running text, judged without the validator's rules."""
    line = line.strip()
    return (len(line.split()) >= WORDS and len(CELLS.findall(line)) < 2
            and not CAPTION.match(line) and not FOOTNOTE.match(line)
            and not HEADING.match(line) and not PANEL.match(line)
            and line[:1].isalpha() or False)


WORKSPACE = Path(__file__).resolve().parent.parent / "skills" / "cea-extract-claims-workspace"


def material(root=None):
    """Blocks a `[...]` stands for, and sentences carried across two lines, from the real pages."""
    root = WORKSPACE if root is None else Path(root)
    seen, pages = set(), []
    for d in sorted(Path(root).rglob("text.txt")):
        t = d.read_text(encoding="utf-8", errors="replace")
        if len(t) < 20000:
            continue
        name = str(d.parent)
        if name in seen:
            continue
        seen.add(name)
        pages += re.split(r"=== page \d+ ===", t)[1:]

    blocks, pairs = {"caption": [], "table": [], "footnote": [], "heading": [],
                 "panel": []}, []
    for pg in pages:
        lines = pg.splitlines()
        i = 0
        while i < len(lines):
            s = lines[i].strip()
            if not s:
                i += 1
                continue
            kind = None
            shouts = [c for c in s if c.isalpha()]
            shouts = bool(shouts) and sum(c.isupper() for c in shouts) >= len(shouts) * 0.7
            if CAPTION.match(s):
                kind = "caption"
            # Small capitals come out split ("E XISTING G UIDELINES"), so a shouting heading
            # holds far more tokens than words and needs the looser cap.
            elif (HEADING.match(s) and len(s.split()) <= (20 if shouts else 10)
                  and (shouts or not FOOTNOTE.match(s))):
                # "6 D ISCUSSION" opens like a footnote and is a heading. Capitals settle it.
                kind = "heading"
            elif FOOTNOTE.match(s):
                kind = "footnote"
            elif PANEL.match(s):
                # A multi-panel figure's own labels. The harness drew none, so the
                # rule keeping them out of the prose count was covered by nothing.
                kind = "panel"
            elif len(CELLS.findall(lines[i])) >= 2:
                kind = "table"
            if kind in ("caption", "footnote"):
                # The block runs while the lines are still block material. It used to run until a
                # full stop, with no cap and no case test, which swept five lines of the paper's
                # own prose into a "footnote" and then scored a weld built from it as legitimate.
                blk, j = [lines[i]], i + 1
                while (j < len(lines) and lines[j].strip()
                       and not reads_as_the_paper(lines[j])
                       and not CAPTION.match(lines[j].strip())
                       and not FOOTNOTE.match(lines[j].strip())
                       and len(blk) < 8):
                    blk.append(lines[j])
                    j += 1
                blocks[kind].append(blk)
                i = j
                continue
            if kind == "panel":
                blk, k = [lines[i]], i + 1
                while (k < len(lines) and lines[k].strip()
                       and not reads_as_the_paper(lines[k])):
                    blk.append(lines[k])
                    k += 1
                blocks["panel"].append(blk)
                i = k
                continue
            if kind == "heading":
                blocks["heading"].append([lines[i]])
                i += 1
                continue
            if kind == "table":
                blk, j = [lines[i]], i + 1
                while j < len(lines) and len(CELLS.findall(lines[j])) >= 2:
                    blk.append(lines[j])
                    j += 1
                blocks["table"].append(blk)
                i = j
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if (reads_as_the_paper(lines[i]) and reads_as_the_paper(nxt)
                    and s[-1] not in ".?!:;" and not s.endswith("-")):
                pairs.append((lines[i], lines[i + 1]))
            i += 1
    # A block holding a line of the paper is not a block, whatever it opened with.
    blocks = {k: [b for b in v if not any(reads_as_the_paper(l) for l in b)]
              for k, v in blocks.items()}
    return blocks, pairs


def verdict(quote, page):
    """What the validator's gap rules say: None when the quote stands."""
    return C._skipped_heading(quote, page) or C._skipped_prose(quote, page)


def run(seed, rounds):
    blocks, pairs = material()
    # A kind the pages held none of cannot be drawn from, and a run needs sentences to build with.
    blocks = {k: v for k, v in blocks.items() if v}
    if not blocks or len(pairs) < 2:
        return {"legitimate": 0, "weld": 0}, {"legitimate refused": [], "weld accepted": []}
    rng = random.Random(seed)
    fails = {"legitimate refused": [], "weld accepted": []}
    built = {"legitimate": 0, "weld": 0}

    for _ in range(rounds):
        kind = rng.choice(list(blocks))
        blk = rng.choice(blocks[kind])
        head, tail = rng.choice(pairs)
        # A sentence carried across two lines, interrupted by the block. A heading never stands
        # inside one sentence, so a gap over one is a weld by construction, not a legitimate gap.
        page = "\n".join([head, *blk, tail]) + "\n"
        quote = " ".join(head.split()) + " [...] " + " ".join(tail.split())
        if C.find_quote(quote, page):
            if kind == "heading":
                built["weld"] += 1
                if not verdict(quote, page):
                    fails["weld accepted"].append(("heading in the gap", quote[:70]))
            else:
                built["legitimate"] += 1
                said = verdict(quote, page)
                if said:
                    fails["legitimate refused"].append((kind, str(said)[:70]))

        # A block AND the paper's own prose behind it. Every defect the reviews found in this
        # area had this shape: one caption or one numbered line excused the whole gap.
        others = [k for k in blocks if k != "heading"]
        if not others:
            continue
        blk2 = rng.choice(blocks[rng.choice(others)])
        (h3, t3), (h4, t4) = rng.sample(pairs, 2)
        page = "\n".join([h3, *blk2, t3, h4, t4]) + "\n"
        quote = " ".join(h3.split()) + " [...] " + " ".join(t4.split())
        if C.find_quote(quote, page):
            built["weld"] += 1
            if not verdict(quote, page):
                fails["weld accepted"].append(("block then prose", quote[:70]))

        # Two different sentences with the paper's own prose between them.
        (h1, t1), (h2, t2) = rng.sample(pairs, 2)
        middle = [t1] + [rng.choice(pairs)[0] for _ in range(rng.randint(1, 3))] + [h2]
        page = "\n".join([h1, *middle, t2]) + "\n"
        quote = " ".join(h1.split()) + " [...] " + " ".join(t2.split())
        span = C.find_quote(quote, page)
        # A gap that skips nothing is not a weld: the two halves landed adjacent, which happens
        # when the sampled material repeats. Only a gap that really stands over text is a case.
        if span and any(l.strip() for a, b in span[2] for l in C._fold(page)[a:b].splitlines()):
            built["weld"] += 1
            if not verdict(quote, page):
                fails["weld accepted"].append(("prose in the gap", quote[:70]))
    return built, fails


if __name__ == "__main__":
    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    built, fails = run(seed, rounds)
    print(f"built: {built['legitimate']} legitimate gaps, {built['weld']} welds")
    for name, rows in fails.items():
        print(f"{name}: {len(rows)}")
        for kind, detail in rows[:4]:
            print(f"    [{kind}] {detail}")
