"""Tests for pdf_text.py and cea_claims.py.

Run from the repository root:

    python3 -m unittest discover skills/cea-extract-claims/scripts/tests

The tests on real papers read the PDFs in evals/papers/ and are skipped when those are missing.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cea_claims  # noqa: E402
import pdf_text  # noqa: E402

PAPERS = SCRIPTS.parents[2] / "evals" / "papers"


class Layout(unittest.TestCase):
    def test_full_width_caption_stays_whole_above_the_columns(self):
        caption = ("Fig. 1. A caption that runs across both columns of the page and is long "
                   "enough to cross the gutter.")
        left = [f"left column line {i:02d} with some words" for i in range(12)]
        right = [f"right column line {i:02d} with more words" for i in range(12)]
        lines = [caption, ""] + [f"{l:<45}{r}" for l, r in zip(left, right)]
        out, regions = pdf_text._layout(lines)
        text = [l.strip() for l in out if l.strip()]
        self.assertEqual(regions, 1)
        self.assertEqual(text[0], caption)
        self.assertEqual(text[1:13], left)
        self.assertEqual(text[13:], right)

    def test_full_width_table_with_gap_at_gutter_stays_whole(self):
        caption = " " * 38 + "TABLE II"
        rows = [f"  row {i}   left cell {i}   more   {'':<16}   right cell {i}   value {i}" for i in range(4)]
        left = [f"left column line {i:02d} with some words" for i in range(12)]
        right = [f"right column line {i:02d} with more words" for i in range(12)]
        body = [f"{l:<45}{r}" for l, r in zip(left, right)]
        out, regions = pdf_text._layout([caption, ""] + rows + [""] + body)
        text = [l.strip() for l in out if l.strip()]
        self.assertEqual(regions, 1)
        self.assertEqual(text[1:5], [r.strip() for r in rows])
        self.assertEqual(text[5:17], left)
        self.assertEqual(text[17:], right)

    def test_single_column_page_is_unchanged(self):
        lines = [("This is a line of ordinary single-column prose that fills most of the "
                  f"width, number {i}.") for i in range(20)]
        out, regions = pdf_text._layout(lines)
        self.assertEqual(regions, 0)
        self.assertEqual(out, lines)

    def test_references_removed_up_to_appendix(self):
        pages = [
            pdf_text.Page(1, ["5 Results", "We found X.", "", "R EFERENCES",
                              "[1] A. Author, Title, 2020."]),
            pdf_text.Page(2, ["[2] B. Author, Title, 2021.", "A PPENDIX A",
                              "Table 9 shows 12% more."]),
        ]
        self.assertEqual(pdf_text._drop_references(pages), (1, 2))
        text = "\n".join(l for p in pages for l in p.lines)
        for kept in ("We found X.", "A PPENDIX A", "Table 9 shows 12% more.",
                     pdf_text.REFERENCES_REMOVED):
            self.assertIn(kept, text)
        self.assertNotIn("Author", text)

    def test_small_caps_conclusion_is_not_a_references_heading(self):
        self.assertFalse(pdf_text._is_references_heading("C ONCLUSION"))
        self.assertTrue(pdf_text._is_references_heading("VIII. R EFERENCES"))


@unittest.skipUnless(any(PAPERS.glob("*.pdf")), "no test papers in evals/papers/")
class RealPapers(unittest.TestCase):
    NAMES = ("tse26-ai-code-review", "tse26-genai-usage", "ieeesw26-ai-slop")

    @classmethod
    def setUpClass(cls):
        cls.papers = {}
        for name in cls.NAMES:
            path = PAPERS / f"{name}.pdf"
            if path.is_file():
                extraction = pdf_text.extract(str(path))
                cls.papers[name] = {p.number: "\n".join(p.lines) for p in extraction.pages}

    def pages(self, name):
        if name not in self.papers:
            self.skipTest(f"{name}.pdf is not in evals/papers/")
        return self.papers[name]

    def assertOnlyOnPage(self, name, quote, page):
        pages = self.pages(name)
        found = [n for n, text in pages.items() if cea_claims.quote_on(quote, text)]
        self.assertEqual(found, [page], f"{quote!r} in {name}")

    def test_page_counts(self):
        for name, count in zip(self.NAMES, (17, 18, 8)):
            with self.subTest(name=name):
                self.assertEqual(len(self.pages(name)), count)

    def test_full_width_caption_and_columns(self):
        self.assertOnlyOnPage(
            "tse26-ai-code-review",
            "Fig. 1. Example comments from a PR-level review action "
            "(Integral-Healthcare/robin-ai-reviewer), a file-level review action "
            "(anc95/ChatGPT-CodeReview), and a hunk-Level review action "
            "(coderabbitai/ai-pr-reviewer).", 2)
        self.assertOnlyOnPage(
            "tse26-ai-code-review",
            "We introduce an LLM-assisted framework for assessing whether code review comments "
            "are actionable and whether they have been addressed, achieving high accuracy "
            "compared to human annotations.", 2)

    def test_text_below_a_full_width_table(self):
        self.assertOnlyOnPage("tse26-ai-code-review",
                              "This process yielded a final set of 16 relevant actions.", 3)
        self.assertOnlyOnPage("tse26-genai-usage",
                              "GenAI-assisted tasks (RQ1): Definition and frequency of categories "
                              "and codes", 5)

    def test_full_width_table_row_stays_on_one_line(self):
        pages = self.pages("tse26-ai-code-review")
        row = next(l for l in pages[5].splitlines() if "anc95/ChatGPT-CodeReview" in l and "2,831" in l)
        self.assertIn("2,384 (84.21%)", row)
        self.assertOnlyOnPage("tse26-ai-code-review",
                              "RQ2: REFINED DATASET FOR COMMENT ADDRESSING ANALYSIS (N=5,652): "
                              "POST-REVIEW FILE CHANGE DISTRIBUTION BY COMMENT SOURCES.", 5)

    def test_sentence_across_lines(self):
        self.assertOnlyOnPage("ieeesw26-ai-slop",
                              "This left 15 documents (1,154 posts) in the final corpus.", 2)

    def test_references_and_biographies_removed(self):
        for name, gone in (("tse26-ai-code-review", "D. M. Blei, A. Y. Ng, and M. I. Jordan"),
                           ("ieeesw26-ai-slop", "is a Professor of Software Engineering at "
                                                "Heidelberg University")):
            with self.subTest(name=name):
                pages = self.pages(name)
                self.assertFalse(any(cea_claims.quote_on(gone, t) for t in pages.values()))
                self.assertTrue(any(pdf_text.REFERENCES_REMOVED in t for t in pages.values()))


TEXT = """=== page 1 ===
Abstract—Caching halves median build time.
=== page 2 ===
Across the 48 projects, median build time fell from 9.2 to 4.1 min-
utes, and the failure rate stayed at 3%.
We collected 1,203 builds from 48 projects.2
=== page 3 ===
Build failures are rare in general.
"""

SPLIT_QUOTE = ("Across the 48 projects, median build time fell from 9.2 to 4.1 minutes, and the "
               "failure rate stayed at 3%.")


def valid_claims():
    return {
        "paper": {"id": "fixture", "title": "Fixture", "pdf": "fixture.pdf", "pages": 3},
        "broad_statements": [
            {"id": "B1", "quote": "Caching halves median build time.", "page": 1,
             "section": "Abstract", "source": "abstract"},
        ],
        "claims": [
            {"id": "C1", "quote": SPLIT_QUOTE,
             "text": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes.",
             "page": 2, "section": "5 Results", "serves": ["B1"], "split_from": "S1",
             "selection_reason": "B1 rests on this comparison."},
            {"id": "C2", "quote": SPLIT_QUOTE,
             "text": "Across the 48 projects, the failure rate stayed at 3%.",
             "page": 2, "section": "5 Results", "serves": ["B1"], "split_from": "S1",
             "selection_reason": "Needs different evidence than C1."},
        ],
        "rejected": [
            {"id": "R1", "quote": "We collected 1,203 builds from 48 projects.", "page": 2,
             "section": "4 Data", "reason": "Describes the data, not a result."},
        ],
    }


class Validate(unittest.TestCase):
    def check(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, _ = cea_claims.validate(d)
            return "\n".join(problems)

    def test_valid_record_passes(self):
        self.assertEqual(self.check(valid_claims()), "")

    def test_quote_on_wrong_page(self):
        data = valid_claims()
        data["rejected"][0]["page"] = 3
        self.assertIn("found on page 2", self.check(data))

    def test_invented_quote(self):
        data = valid_claims()
        data["broad_statements"][0]["quote"] = "Caching triples median build time."
        self.assertIn("not found in text.txt", self.check(data))

    def test_serves_unknown_broad_statement(self):
        data = valid_claims()
        data["claims"][0]["serves"] = ["B9"]
        self.assertIn("'B9' is not a broad statement id", self.check(data))

    def test_split_with_one_part(self):
        data = valid_claims()
        del data["claims"][1]
        self.assertIn("'S1' has no other part", self.check(data))

    def test_reworded_text_without_split(self):
        data = valid_claims()
        data["claims"][0]["split_from"] = None
        data["claims"].pop(1)
        self.assertIn("differs from the quote", self.check(data))

    def test_page_count_mismatch(self):
        data = valid_claims()
        data["paper"]["pages"] = 4
        self.assertIn("text.txt has 3 pages", self.check(data))

    def test_render(self):
        md = cea_claims.render(valid_claims())
        for expected in ("## Broad statements", "### C1: page 2", "- Split from S1, with C2",
                         "### R1: page 2, 4 Data"):
            self.assertIn(expected, md)

    def test_render_lists_claims_in_page_order(self):
        data = valid_claims()
        data["claims"][1]["page"] = 1
        md = cea_claims.render(data)
        self.assertLess(md.index("### C2: page 1"), md.index("### C1: page 2"))

    def test_duplicate_of_accepts_a_list_with_a_rejected_id(self):
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": "We collected 1,203 builds from 48 projects.",
                                 "page": 2, "section": "4 Data", "duplicate_of": ["R1", "C1"],
                                 "reason": "Repeats R1."})
        self.assertEqual(self.check(data), "")

    def test_duplicate_of_unknown_id(self):
        data = valid_claims()
        data["rejected"][0]["duplicate_of"] = ["C9"]
        self.assertIn("'C9' is not the id of a claim, rejected candidate, or broad statement", self.check(data))

    def test_duplicate_of_accepts_a_broad_statement_id(self):
        data = valid_claims()
        data["rejected"][0]["duplicate_of"] = ["B1"]
        self.assertEqual(self.check(data), "")

    def test_quote_with_a_gap_inside_a_word(self):
        table = "Table 5 row with cells 12 34 56\n"
        page = "We found ten dis-\n" + table * 100 + "tinct codes in the data."
        self.assertTrue(cea_claims.quote_on("We found ten dis[...]tinct codes in the data.", page))
        far = "We found ten dis-\n" + table * 200 + "tinct codes in the data."
        self.assertFalse(cea_claims.quote_on("We found ten dis[...]tinct codes in the data.", far))

    def test_split_part_adds_words(self):
        data = valid_claims()
        data["claims"][1]["text"] = "Across the 48 projects, the failure rate stayed low at 3%."
        self.assertIn("uses words that are not in the quote: low", self.check(data))


if __name__ == "__main__":
    unittest.main()
