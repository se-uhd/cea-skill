"""Tests for pdf_text.py and cea_claims.py.

Run from the repository root:

    python3 -m unittest discover skills/cea-extract-claims/scripts/tests

The tests on real papers read the PDFs in evals/papers/ and are skipped when the PDFs are missing.
"""

import contextlib
import io
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
        out, regions, _ = pdf_text._layout(lines)
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
        out, regions, _ = pdf_text._layout([caption, ""] + rows + [""] + body)
        text = [l.strip() for l in out if l.strip()]
        self.assertEqual(regions, 1)
        self.assertEqual(text[1:5], [r.strip() for r in rows])
        self.assertEqual(text[5:17], left)
        self.assertEqual(text[17:], right)

    def test_caption_across_the_gutter_stays_whole(self):
        caption = "Figure 3. A caption that runs across both columns and is long enough to cross the gutter entirely."
        rows = lambda k: [f"{'left column line %02d words' % (k + i):<45}right column line {k + i:02d} words" for i in range(10)]
        out, regions, _ = pdf_text._layout(rows(0) + [caption] + rows(10))
        self.assertIn(caption, [l.strip() for l in out])
        self.assertEqual(regions, 2)

    def test_table_cell_inside_the_gutter_stays_left(self):
        body = [f"{'left text of line %02d here' % i:<46}right column text {i:02d}" for i in range(10)]
        cells = [f"{'cells of row %d' % i:<37}19{'':<7}right column text x{i}" for i in range(3)]
        out, regions, _ = pdf_text._layout(body + cells)
        self.assertEqual(regions, 1)
        left_part = out[:len(body) + len(cells)]
        self.assertTrue(all("19" in l for l in left_part[-3:]), left_part[-3:])

    def test_caption_line_with_two_table_captions_is_not_full_width(self):
        caption = f"{'':<20}TABLE VI{'':<64}TABLE IX"
        rows = [f"{'':<20}{'x   y   z':<72}u   v   w" for _ in range(4)]
        self.assertEqual(pdf_text._full_width_tables([caption] + rows, 60, 120), [])

    def test_cell_continued_on_the_next_line_stays_in_the_table(self):
        caption = f"{'':<56}TABLE II"
        rows = [f"{'a1   a2   a3':<90}r1" for _ in range(3)]
        continued = f"{'more words in a cell':<90}end of r1"
        body = f"{'Body text of the left column here':<66}Right column body text"
        lines = [caption] + rows + [continued, "", body]
        self.assertEqual(pdf_text._full_width_tables(lines, 60, 120), [(0, 5)])

    def test_full_width_table_ends_before_body_text(self):
        caption = f"{'':<56}TABLE II"
        rows = [f"{'cell a   cell b   cell c':<70}cell d   cell e" for _ in range(3)]
        body = [f"{'Our coding process allowed coders to assign':<62}3.2   Results",
                f"{'codes per mention during the refinement':<62}Our analysis revealed patterns"]
        self.assertEqual(pdf_text._full_width_tables([caption] + rows + body, 60, 120), [(0, 4)])

    def test_single_column_page_is_unchanged(self):
        lines = [("This is a line of ordinary single-column prose that fills most of the "
                  f"width, number {i}.") for i in range(20)]
        out, regions, _ = pdf_text._layout(lines)
        self.assertEqual(regions, 0)
        self.assertEqual(out, lines)

    def test_region_reaches_past_lines_that_cross_the_gutter(self):
        row = lambda i: f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
        wide = [f"[{i}] A. Author, Title of a paper set in a smaller font that reaches past the gutter, 2020."
                for i in range(3)]
        out, regions, _ = pdf_text._layout([row(i) for i in range(20)] + wide + [row(i) for i in range(20, 24)], 44)
        text = [l.strip() for l in out if l.strip()]
        self.assertEqual(regions, 1)
        self.assertLess(text.index("left column sentence part 23 of text"),
                        text.index("right column sentence part 23 words"))

    def test_line_across_the_cut_stays_whole(self):
        row = lambda i: f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
        wide = "  Task Triggers and Modes        Defines when and how the action runs, with an example per row"
        out, regions, _ = pdf_text._layout([row(i) for i in range(12)] + [wide] + [row(i) for i in range(12, 24)])
        self.assertTrue(regions)
        self.assertIn(wide.strip(), [l.strip() for l in out])
        self.assertFalse([l for l in out if l.strip().endswith("Defines when and how the action runs, with an")])

    def test_short_right_column_is_split(self):
        lines = [f"{'left column sentence part %02d of text' % i:<50}"
                 + (f"right column sentence part {i:02d} words" if i >= 32 else "") for i in range(40)]
        out, regions, _ = pdf_text._layout(lines)
        self.assertEqual(regions, 1)
        self.assertFalse(any("left" in l and "right" in l for l in out))

    def test_short_block_is_split_only_at_a_known_gutter(self):
        lines = [f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
                 for i in range(5)]
        self.assertEqual(pdf_text._layout(lines)[1], 0)
        out, regions, _ = pdf_text._layout(lines, 44, allow_short=True)
        text = [l.strip() for l in out if l.strip()]
        self.assertEqual(regions, 1)
        self.assertEqual(text[5], "right column sentence part 00 words")

    def test_short_table_is_not_split_at_a_known_gutter(self):
        rows = [f"{'row %d   cell a   cell b' % i:<50}cell c   cell d" for i in range(5)]
        self.assertEqual(pdf_text._layout(rows, 44, allow_short=True), (rows, 0, None))

    def test_author_line_above_the_columns_stays_whole(self):
        body = [f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
                for i in range(40)]
        out, regions, _ = pdf_text._layout([" " * 10 + "Generated", " " * 30 + "A. Author and B. Author"] + body)
        self.assertEqual(regions, 1)
        self.assertIn("A. Author and B. Author", [l.strip() for l in out])

    def test_right_column_is_aligned_with_the_left(self):
        left = ["   Text of the left column.", "More text on the left."]
        right = ["      Text of the right column.", "   More text on the right."]
        self.assertEqual(pdf_text._align(left, right), ["   Text of the right column.", "More text on the right."])

    def test_page_number_between_line_numbers_is_blanked(self):
        lines = [f"{i}   text of line {i} in the left column" for i in range(51, 58)] + [f"58{'':<40}1{'':<40}116"]
        pdf_text._blank_edges(lines, set())
        self.assertEqual(lines[-1], "")
        self.assertTrue(lines[-2].endswith("in the left column"))

    def test_table_in_one_column_does_not_stop_the_split(self):
        lines = [f"{'row %02d   12   34   56' % i:<50}right column sentence part {i:02d} words" for i in range(40)]
        out, regions, _ = pdf_text._layout(lines)
        self.assertEqual(regions, 1)
        self.assertFalse(any("row" in l and "right" in l for l in out))

    def test_short_page_is_split_at_the_gutter_of_other_pages(self):
        row = lambda i: f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
        pages, _ = pdf_text._read_pages([[row(i) for i in range(20)], [row(i) for i in range(4)]])
        self.assertEqual([p.regions for p in pages], [1, 1])

    def test_table_row_at_the_page_edge_stays(self):
        lines = ["2010    0     0", "2011    3     5"] + [f"text of line {i} in the left column" for i in range(8)]
        pdf_text._blank_edges(lines, set())
        self.assertEqual(lines[0], "2010    0     0")

    def test_short_running_header_on_three_pages_is_removed(self):
        pages = ["A. Author\n\n" + "\n".join(f"text line {i} on page {n} of the body" for i in range(6))
                 for n in range(3)]
        self.assertEqual(pdf_text._running_headers(pages), {"A. Author"})
        self.assertEqual(pdf_text._running_headers(pages[:2]), set())

    def test_one_column_paper_is_left_alone(self):
        rows = [f"   proj-{i:02d}   1296   35   3.66" for i in range(20)]
        prose = [f"This is a line of single-column prose that fills most of the width, number {i}."
                 for i in range(20)]
        pages, _ = pdf_text._read_pages([prose, rows])
        self.assertEqual([p.regions for p in pages], [0, 0])
        self.assertEqual(pages[1].lines, rows)

    def test_two_column_paper_keeps_a_table_page_whole(self):
        row = lambda i: f"{'left column sentence part %02d of text' % i:<50}right column sentence part {i:02d} words"
        rows = [f"   proj-{i:02d}   1296   35   3.66" for i in range(20)]
        pages, _ = pdf_text._read_pages([[row(i) for i in range(20)], rows])
        self.assertEqual([p.regions for p in pages], [1, 0])
        self.assertEqual(pages[1].lines, rows)

    def test_region_needs_text_on_both_sides(self):
        lines = [f"{'left column sentence part %02d of text' % i:<50}"
                 + (f"right column part {i:02d}" if i >= 37 else "") for i in range(40)]
        self.assertEqual(pdf_text._layout(lines)[1], 0)

    def test_short_definition_table_is_not_split_at_a_known_gutter(self):
        rows = [f"{'Continuous integration %d' % i:<44}CI, the automated build and test pipeline"
                for i in range(4)]
        self.assertEqual(pdf_text._layout(rows, 44, allow_short=True)[1], 0)

    def test_numbers_only_row_next_to_text_stays(self):
        lines = [f"   12{'':<30}45{'':<30}78"] + [f"text of line {i} in the column" for i in range(8)]
        pdf_text._blank_edges(lines, set())
        self.assertTrue(lines[0].strip())

    def test_repeated_line_next_to_the_body_is_not_a_header(self):
        pages = ["A. Author\n" + "\n".join(f"text line {i} on page {n} of the body" for i in range(6))
                 for n in range(3)]
        self.assertEqual(pdf_text._running_headers(pages), set())

    def test_running_header_is_blanked_on_the_page(self):
        lines = ["A. Author", ""] + [f"text line {i} of the body" for i in range(6)]
        pdf_text._blank_edges(lines, {"A. Author"})
        self.assertEqual(lines[0], "")

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
        # A single letter before a space is not a roman numeral without its period.
        self.assertTrue(pdf_text._is_references_heading("L ITERATURE C ITED"))

    def test_references_word_in_body_text_is_not_a_heading(self):
        pages = [pdf_text.Page(1, ["We compared the tools against the", "references.", "II. RESULTS",
                                   "Across the 48 projects, build time fell."])]
        self.assertIsNone(pdf_text._drop_references(pages))
        pages = [pdf_text.Page(1, ["Bibliography", "This paragraph is about the bibliography of the field.",
                                   "More text."])]
        self.assertIsNone(pdf_text._drop_references(pages))

    def test_lettered_appendix_after_references_is_kept(self):
        pages = [pdf_text.Page(1, ["References", "[1] A. Author. 2020. A title. In Proc. X.",
                                   "[2] B. Author. 2021. Another title.", "A     Additional Results",
                                   "In the appendix study, caching reduced failure rates by 17%."])]
        self.assertEqual(pdf_text._drop_references(pages), (1, 1))
        self.assertIn("In the appendix study, caching reduced failure rates by 17%.", pages[0].lines)
        self.assertNotIn("[2] B. Author. 2021. Another title.", pages[0].lines)

    def test_references_heading_with_a_period_is_not_a_heading(self):
        pages = [pdf_text.Page(1, ["as listed in the", "References.", "[1] A. Author. 2020. A title."])]
        self.assertIsNone(pdf_text._drop_references(pages))

    def test_appendix_headings(self):
        for heading in ("Supplemental Material", "Online Appendix", "A RQ1 Details", "B Precision, Recall, and Accuracy",
                        "A. Additional Results", "A.1 Details for RQ1", "B) Prompts Used"):
            with self.subTest(heading=heading):
                self.assertTrue(pdf_text._is_appendix_heading(heading, ["The appendix gives 12 more results."]))
        self.assertFalse(pdf_text._is_appendix_heading("A Survey of Program Repair,", []))
        self.assertFalse(pdf_text._is_appendix_heading("A Study of Builds (2020)", []))

    def test_appendix_before_a_sentence_that_starts_with_two_capitalized_words(self):
        pages = [pdf_text.Page(1, ["References", "[1] A. Author. 2020. A title. In Proc. X.",
                                   "A     Additional Results",
                                   "Overall, Table 3 shows the per-project results for all 48 projects."])]
        self.assertEqual(pdf_text._drop_references(pages), (1, 1))
        self.assertIn("Overall, Table 3 shows the per-project results for all 48 projects.", pages[0].lines)

    def test_wrapped_title_in_the_bibliography_is_not_an_appendix(self):
        pages = [pdf_text.Page(1, ["References", "[1] J. Smith. 2020.", "A Survey of Program Repair Techniques",
                                   "for Java Programs, with a long subtitle that wraps", "again onto a third line",
                                   "and a fourth line of the same title", "[2] K. Lee. 2021. Another title.",
                                   "[3] L. Park. 2022. A third title."])]
        self.assertEqual(pdf_text._drop_references(pages), (1, None))

    def test_appendix_that_does_not_say_appendix(self):
        for heading in ("Supplementary Results", "Additional Analyses", "VIII. A DDITIONAL R ESULTS"):
            with self.subTest(heading=heading):
                pages = [pdf_text.Page(1, ["References", "[1] A. Author. 2020. A title. In Proc. X.",
                                           heading, "The appendix reports 12 further results of the study."])]
                self.assertEqual(pdf_text._drop_references(pages), (1, 1))
                self.assertIn("The appendix reports 12 further results of the study.", pages[0].lines)


class LineNumbers(unittest.TestCase):
    def test_left_margin_numbers_are_removed(self):
        lines = [f"{i:>3}   Sentence {i} of the running text here." for i in range(1, 21)]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertTrue(all(l.strip().startswith("Sentence") for l in out))

    def test_numbers_glued_to_the_end_of_lines_are_removed(self):
        words = ["observed", "of 12", "projects.", "that", "in 2023", "builds", "the", "of 17"]
        lines = [f"text line {w}{50 + i}" for i, w in enumerate(words * 2)]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertEqual(out[:8], [f"text line {w}" for w in words])

    def test_numbers_on_every_other_line_are_removed(self):
        lines = []
        for i in range(10):
            lines += [f"left column text of line {i} ends here", f"{'':<40}{276 + i}"]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertFalse(any(l.strip().isdigit() for l in out))

    def test_numbers_on_every_fifth_line_are_removed(self):
        lines = [f"{i:>3}   Sentence {i} of the running text." if i % 5 == 0 else f"      Sentence {i} of the running text."
                 for i in range(1, 41)]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertTrue(all(l.strip().startswith("Sentence") for l in out))

    def test_numbers_at_the_right_margin_are_removed(self):
        lines = [f"{'text of line %d' % i:<50}{i:>4}" for i in range(1, 21)]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertEqual(out[0], "text of line 1")

    def test_last_cell_of_a_table_row_stays(self):
        lines = [f"{i:>3}   Sentence {i} of the running text." for i in range(1, 21)]
        lines[10:10] = [f"      {label:<14}{text:<48}{v}" for label, text, v in
                        (("Flaky test", "Passes on a re-run of the same commit", 37),
                         ("Real defect", "Fails again on every re-run of the commit", 21),
                         ("Infra error", "Fails before the test suite starts at all", 44),
                         ("Timeout", "Exceeds the time limit set for the job", 12))]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertTrue([l for l in out if l.strip().endswith("commit                        37")]
                        or [l for l in out if l.rstrip().endswith("37")], out[10:14])

    def test_table_in_a_numbered_paper_keeps_its_first_cell(self):
        lines = [f"{i:>3}   Sentence {i} of the running text." for i in range(1, 21)]
        lines[10:10] = ["      2010    0    0", "      2011    3    5"]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertIn("      2010    0    0", out)

    def test_number_that_ends_a_sentence_is_not_a_line_number(self):
        lines = []
        for n in range(10, 26):
            lines += [f"{n}", "     We found that caching helped in many projects."]
        lines[11] = "     We found that 9 of the projects used caching, a total of 16"
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertIn("     We found that 9 of the projects used caching, a total of 16", out)

    def test_a_short_run_of_numbers_is_not_line_numbering(self):
        lines = [f"Sentence {i} of the text ends with a count of   {i}" for i in range(1, 4)] + ["Plain text."] * 20
        self.assertEqual(pdf_text._strip_line_numbers(lines), (lines, False))

    def test_glued_numbers_of_the_other_column_are_cut(self):
        lines = [f"{i:>3}   text of line {i} in the left column observed{155 + i}" for i in range(1, 9)]
        lines += ["  9   Sentence S46 states that 46 of 53 builds failed in the observed projects.",
                  " 10   We found that 9 of the projects used caching, a total161",
                  " 11   and the median build time fell in the observed170",
                  " 12   projects in that sample, as the table shows and172"]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertIn("Sentence S46", out[8])
        for i, ending in ((9, "a total"), (10, "the observed"), (11, "the table shows and")):
            with self.subTest(line=i):
                self.assertTrue(out[i].rstrip().endswith(ending), out[i])

    def test_word_that_ends_in_a_digit_keeps_it(self):
        lines = [f"{i:>3}   text of line {i} in the left column observed{155 + i}" for i in range(1, 9)]
        lines += ["  9   we report the results of the first experiment in Study1",
                  " 10   and the weakest model on that benchmark was Qwen2",
                  " 11   and the median build time fell in the observed170",
                  " 12   projects in that sample, as the table shows and172"]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        self.assertTrue(out[8].rstrip().endswith("Study1"), out[8])
        self.assertTrue(out[9].rstrip().endswith("Qwen2"), out[9])
        self.assertTrue(out[10].rstrip().endswith("the observed"), out[10])

    def test_a_counting_column_under_a_caption_is_not_line_numbering(self):
        lines = ["Table 1: Participants and what they reported", ""]
        lines += [f"  {i}   Reported improved comprehension of the code base" for i in range(1, 15)]
        lines += [f"Sentence {i} of the running text stands outside the table." for i in range(1, 7)]
        self.assertEqual(pdf_text._strip_line_numbers(lines), (lines, False))

    def test_numbers_in_a_table_stay(self):
        lines = [f"{i:>3}   Sentence {i} of the running text here." for i in range(1, 21)]
        lines[10:10] = [f"   {2010 + i}   cell a   {12 + i}" for i in range(8)]
        out, found = pdf_text._strip_line_numbers(lines)
        self.assertTrue(found)
        for i in range(8):
            with self.subTest(row=i):
                self.assertIn(f"   {2010 + i}   cell a   {12 + i}", out)


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

    def test_table_cell_and_captions_on_genai_pages(self):
        pages = self.pages("tse26-genai-usage")
        row = next(l for l in pages[11].splitlines() if "negligible" in l and "5 | 10" in l)
        self.assertIn("19", row)
        row = next(l for l in pages[7].splitlines() if "Blame Copilot" in l)
        self.assertIn("Specifically attributing errors", row)
        self.assertOnlyOnPage("tse26-genai-usage",
                              "Figure 1. Overview of the data collection process used to answer our three "
                              "research questions, from the selection (1) and filtering (2) of GitHub "
                              "repositories to the extraction of GenAI mentions (3) and the identification of "
                              "self-admitted GenAI usage (4) in these repositories.", 3)
        self.assertOnlyOnPage("tse26-genai-usage",
                              "Figure 2. One representative repository for each of the four RDD patterns of "
                              "file-based churn, showing average file-based churn per week (gray), pre-adoption "
                              "fit (blue), post-adoption fit (green), and the first GenAI mention (red dashed line).", 12)

    def test_column_tables_and_body_text_stay_apart(self):
        self.assertOnlyOnPage("tse26-genai-usage",
                              "Of the 1,009 instances of code PR description in Table 3, 1,000 originated "
                              "from a single repository", 6)
        heading = next(l for l in self.pages("tse26-genai-usage")[5].splitlines()
                       if "3.2" in l and "Results" in l)
        self.assertNotIn("Our coding process", heading)
        row = next(l for l in self.pages("tse26-ai-code-review")[9].splitlines() if "Is Human" in l)
        self.assertNotIn("Is Action 3", row)
        page3 = self.pages("tse26-ai-code-review")[3]
        self.assertLess(page3.index("change summary"), page3.index("Action Selection"))

    def test_sentence_across_lines(self):
        self.assertOnlyOnPage("ieeesw26-ai-slop",
                              "This left 15 documents (1,154 posts) in the final corpus.", 2)

    def test_author_line_stays_whole(self):
        self.assertOnlyOnPage("tse26-ai-code-review",
                              "Xiaoxing Ma, Guoping Rong, Dong Shao, and Christoph Treude", 1)

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
    def check(self, data, text=TEXT):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(text, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, _ = cea_claims.validate(d)
            return "\n".join(problems)

    def warnings(self, data, text=TEXT):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(text, encoding="utf-8")
            return "\n".join(cea_claims.advisories(d, data))

    def test_valid_record_passes(self):
        self.assertEqual(self.check(valid_claims()), "")

    def test_claim_serving_many_statements_warns(self):
        data = valid_claims()
        data["broad_statements"] += [
            {"id": "B2", "quote": "Caching halves median build time.", "page": 1,
             "section": "1 Introduction", "source": "contributions"},
            {"id": "B3", "quote": "Caching halves median build time.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"}]
        data["claims"][0]["serves"] = ["B1", "B2", "B3"]
        self.assertIn("names 3 broad statements", self.warnings(data))

    def test_repetition_naming_no_broad_statement_warns(self):
        data = valid_claims()
        data["rejected"].append(
            {"id": "R2", "quote": "As Section 5.1 showed, caching cut median build time.",
             "page": 3, "section": "6 Discussion", "duplicate_of": ["C1"], "reason": "Repeats C1."})
        self.assertIn("names no broad statement", self.warnings(data))
        data["rejected"][-1]["duplicate_of"] = ["B1"]
        self.assertNotIn("names no broad statement", self.warnings(data))

    def test_boxed_answer_takes_the_rq_answer_source(self):
        data = valid_claims()
        data["broad_statements"][0]["section"] = "5 Results, Summary RQ1"
        self.assertIn("the source is rq_answer", self.warnings(data))
        data["broad_statements"][0]["source"] = "rq_answer"
        self.assertNotIn("the source is rq_answer", self.warnings(data))

    def test_source_other_says_why(self):
        data = valid_claims()
        data["broad_statements"][0]["source"] = "other"
        self.assertIn("says why no summary sentence states it", self.warnings(data))
        data["broad_statements"][0]["note"] = "No summary sentence gives this number."
        self.assertNotIn("says why no summary sentence states it", self.warnings(data))

    def test_reason_naming_a_ground_needs_no_broad_statement(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = "Describes the study, not a result."
        self.assertNotIn("names neither a main result nor a ground", self.warnings(data))

    def test_reason_naming_neither_a_result_nor_a_ground_warns(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = "Not an important number."
        self.assertIn("names neither a main result nor a ground", self.warnings(data))

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

    def test_split_from_uses_the_prescribed_letter(self):
        data = valid_claims()
        for claim in data["claims"]:
            claim["split_from"] = "group-1"
        self.assertIn("split_from: must be S and a number", self.check(data))

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

    def test_render_puts_the_most_stated_result_first(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        data["claims"][1]["serves"] = ["B2"]
        data["rejected"].append(
            {"id": "R2", "quote": "As Section 5.1 showed, caching cut median build time.",
             "page": 3, "section": "6 Discussion", "duplicate_of": ["B1"], "reason": "Repeats B1."})
        md = cea_claims.render(data)
        self.assertIn("### B1 (abstract, stated in 2 places)", md)
        self.assertLess(md.index("### B1 (abstract"), md.index("### B2 (conclusion"))

    def test_render_lists_claims_in_page_order(self):
        data = valid_claims()
        data["claims"][1]["page"] = 1
        md = cea_claims.render(data)
        self.assertLess(md.index("### C2: page 1"), md.index("### C1: page 2"))

    def test_duplicate_of_accepts_a_list_with_a_rejected_id(self):
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": "Build failures are rare in general.",
                                 "page": 3, "section": "6 Discussion", "duplicate_of": ["R1", "C1"],
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

    def test_two_claims_with_the_same_quote(self):
        data = valid_claims()
        quote = "We collected 1,203 builds from 48 projects."
        data["rejected"] = []
        data["claims"] += [{"id": f"C{n}", "quote": quote, "text": quote, "page": 2, "section": "4 Data",
                            "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."} for n in (3, 4)]
        self.assertIn("(C4).quote: claims[2] (C3) already quotes the same sentence", self.check(data))

    def test_byte_order_mark_and_malformed_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(valid_claims()).encode())
            self.assertEqual(cea_claims.validate(d)[0], [])
        data = valid_claims()
        data["claims"][0]["serves"] = [["B1"]]
        data["claims"][1]["quote"] = 123
        data["claims"][1]["text"] = ["median"]
        out = self.check(data)
        self.assertIn("must list ids as strings", out)
        self.assertIn("(C2).quote: must be a non-empty string", out)

    def test_not_only_is_not_a_negation(self):
        quote = "Remote caching not only cut median build time by 55% but also reduced flaky failures by 12%."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "text": "Remote caching cut median build time by 55%.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "text": "Remote caching also reduced flaky failures by 12%.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        self.assertEqual(self.check(data, TEXT + quote + "\n"), "")

    def test_boundary_warnings(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "We saw 13% more cache hits, and builds got faster.\nIn total, 48 projects were studied, as listed, "
                "respectively.\nThe rate was non-\nsignificant for 12 of the 48 projects.\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["claims"] = [
            {"id": "C1", "quote": "3% more cache hits", "text": "3% more cache hits", "page": 2, "section": "5",
             "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C2", "quote": "In total, 48 projects were stud", "text": "In total, 48 projects were stud", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C3", "quote": "We saw 13% more cache hits", "text": "We saw 13% more cache hits", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": "significant for 12 of the 48 projects.", "text": "significant for 12 of the 48 projects.",
             "page": 2, "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."}]
        data["rejected"] = [
            {"id": "R1", "quote": "In total, 48 projects were studied, as listed, respectively.", "text": "48 projects were studied",
             "page": 2, "section": "5", "split_from": "S1", "reason": "Describes the study."},
            {"id": "R2", "quote": "In total, 48 projects were studied, as listed, respectively.", "text": "as listed",
             "page": 2, "section": "5", "split_from": "S1", "reason": "Describes the study."}]
        out = self.warnings(data, text)
        self.assertIn("(C1).quote: starts in the middle of a word or number", out)
        self.assertIn("(C2).quote: ends in the middle of a word or number", out)
        self.assertIn("(C3).quote: ends in the middle of a sentence", out)
        self.assertIn("(C4).quote: starts in the middle of a word or number", out)
        self.assertIn("split_from 'S1': the quote pairs items and numbers with \"respectively\"", out)

    def test_no_gap_warning_for_a_caption_of_several_lines(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "The failure rate increased for 12 of the 48\n"
                "Fig. 3. Distribution of review comments across the four\n"
                "categories, with the share of each category per project\n"
                "projects that used remote caching.\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "The failure rate increased for 12 of the 48 [...] projects that used remote caching.",
                             "page": 2, "section": "5 Results", "reason": "No main result depends on it."}]
        self.assertEqual([w for w in self.warnings(data, text).splitlines() if "skips" in w], [])

    def test_gap_that_skips_five_lines_of_prose_is_flagged(self):
        body = "\n".join(f"line {i} of running prose that the quote leaves out of its middle" for i in range(5))
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "The failure rate increased for 12 of the 48\n" + body + "\ndecreased for the other 36 projects.\n"
                "=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "The failure rate increased for 12 of the 48 [...] decreased for the other 36 projects.",
                             "page": 2, "section": "5 Results", "reason": "No main result depends on it."}]
        self.assertIn("which reads like part of the sentence", self.warnings(data, text))

    def test_gap_that_skips_part_of_the_sentence_is_flagged(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "The failure rate increased for 12 of the 48\nprojects that did not use remote caching, while it\n"
                "decreased for the other 36 projects.\nTable 2: Failure rates per project group\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["rejected"] = [{"id": "R1", "quote": "The failure rate increased for 12 of the 48 [...] decreased for the other 36 projects.",
                             "page": 2, "section": "5", "reason": "No main result depends on it."}]
        self.assertIn('the [...] skips "projects that did not use remote caching, while it"', self.warnings(data, text))

    def test_same_sentence_recorded_on_two_pages(self):
        text = TEXT.replace("Build failures are rare in general.", "Caching halves median build time.")
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": "Caching halves median build time.", "page": 3,
                                 "section": "7 Conclusion", "duplicate_of": ["B1"], "reason": "Repeats B1."})
        self.assertEqual(self.check(data, text), "")

    def test_no_gap_warning_for_a_footnote_glued_to_a_word(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "The failure rate stayed at 3% across\n"
                "1We use the term build for one CI run of the project pipeline.\n"
                "the 48 projects.\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "The failure rate stayed at 3% across [...] the 48 projects.",
                             "page": 2, "section": "5 Results", "reason": "No main result depends on it."}]
        self.assertEqual([w for w in self.warnings(data, text).splitlines() if "skips" in w], [])

    def test_page_that_text_does_not_have(self):
        text = "=== page 1 ===\nCaching halves median build time.\n=== page 5 ===\nEnd of the paper.\n"
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "Caching halves median build time.", "page": "1-2",
                             "section": "Abstract", "reason": "Repeats B1."}]
        self.assertIn("names page 2, which text.txt does not have", self.check(data, text))

    def test_text_without_page_markers(self):
        self.assertIn("holds no '=== page N ===' line", self.check(valid_claims(), "Caching halves build time.\n"))

    def test_page_range_must_name_two_consecutive_pages(self):
        data = valid_claims()
        data["rejected"][0]["page"] = "1-3"
        self.assertIn("must name two consecutive pages", self.check(data))

    def test_page_range_for_a_quote_on_one_page(self):
        data = valid_claims()
        data["rejected"][0]["page"] = "2-3"
        self.assertIn("the quote is on page 2 alone", self.check(data))

    def test_duplicate_id(self):
        data = valid_claims()
        data["rejected"][0]["id"] = "C1"
        self.assertIn("'C1' is also used by claims[0]", self.check(data))

    def test_unknown_source_and_field(self):
        data = valid_claims()
        data["broad_statements"][0]["source"] = "summary"
        data["claims"][0]["kind"] = "causal"
        out = self.check(data)
        self.assertIn("source: must be one of", out)
        self.assertIn("unknown field 'kind'", out)

    def test_rq_answer_source_is_accepted(self):
        data = valid_claims()
        data["broad_statements"][0]["source"] = "rq_answer"
        self.assertEqual(self.check(data), "")

    def test_source_other_needs_a_note(self):
        data = valid_claims()
        data["broad_statements"][0]["source"] = "other"
        self.assertIn("with source other needs a note", self.check(data))

    def test_unserved_broad_statement_needs_a_note(self):
        data = valid_claims()
        data["broad_statements"].append({"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
                                         "section": "6 Discussion", "source": "conclusion"})
        self.assertIn("no claim serves this broad statement", self.check(data))
        data["broad_statements"][1]["note"] = "The paper gives no result for it."
        self.assertEqual(self.check(data), "")

    def test_claim_and_rejected_candidate_with_the_same_quote(self):
        data = valid_claims()
        quote = "We collected 1,203 builds from 48 projects."
        data["claims"].append({"id": "C3", "quote": quote, "text": quote, "page": 2, "section": "4 Data",
                               "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."})
        self.assertIn("(R1).quote: claims[2] (C3) already quotes the same sentence", self.check(data))

    def test_broad_statements_with_the_same_quote(self):
        data = valid_claims()
        data["broad_statements"].append(dict(data["broad_statements"][0], id="B2"))
        self.assertIn("(B2).quote: broad_statements[0] (B1) already quotes the same sentence", self.check(data))

    def test_broad_statement_and_rejected_candidate_with_the_same_quote(self):
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": "Caching halves median build time.", "page": 1,
                                 "section": "Abstract", "reason": "Repeats B1."})
        self.assertIn("a broad statement is not also a rejected candidate", self.check(data))

    def test_split_statement_and_unsplit_entry_with_the_same_quote(self):
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": SPLIT_QUOTE, "page": 2, "section": "5 Results",
                                 "reason": "Not selected."})
        self.assertIn("the parts of split_from 'S1' quote the same sentence", self.check(data))

    def test_two_split_groups_with_the_same_quote(self):
        data = valid_claims()
        data["rejected"] += [
            {"id": "R2", "quote": SPLIT_QUOTE, "text": "median build time fell from 9.2 to 4.1 minutes",
             "page": 2, "section": "5 Results", "split_from": "S2", "reason": "Not selected."},
            {"id": "R3", "quote": SPLIT_QUOTE, "text": "the failure rate stayed at 3%",
             "page": 2, "section": "5 Results", "split_from": "S2", "reason": "Not selected."}]
        self.assertIn("split_from 'S1' and 'S2' quote the same sentence", self.check(data))

    def test_warnings_for_a_split_that_moves_a_negation_or_drops_a_comparison(self):
        quote = "No project got slower, and the failure rate stayed lower than 3%."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "text": "project got slower", "page": 3, "section": "6",
             "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "text": "No failure rate stayed at 3%", "page": 3, "section": "6",
             "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        out = self.warnings(data, TEXT + quote + "\n")
        self.assertIn('no part keeps "no project" from the quote', out)
        self.assertIn("no part keeps lower, than from the quote", out)

    def test_claim_quoting_a_broad_statement_must_serve_it(self):
        data = valid_claims()
        quote = "Build failures are rare in general."
        data["broad_statements"].append({"id": "B2", "quote": quote, "page": 3, "section": "6", "source": "conclusion"})
        data["claims"].append({"id": "C3", "quote": quote, "text": quote, "page": 3, "section": "6",
                               "serves": ["B1"], "split_from": None, "selection_reason": "B2 rests on it."})
        self.assertIn("so it must serve B2", self.check(data))

    def test_split_parts_serve_their_own_broad_statements(self):
        data = valid_claims()
        data["broad_statements"] = [
            dict(data["broad_statements"][0], quote=SPLIT_QUOTE, page=2, section="5 Results", source="other",
                 note="The results state this main result, and no summary sentence does."),
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3, "section": "6 Discussion",
             "source": "conclusion"}]
        data["claims"][0]["serves"] = ["B1"]
        data["claims"][1]["serves"] = ["B2"]
        self.assertEqual(self.check(data), "")
        data["claims"][0]["serves"] = ["B2"]
        self.assertIn("split_from 'S1': the parts quote the sentence of B1, so one part must serve B1",
                      self.check(data))

    def test_ids_use_the_prescribed_letters(self):
        data = valid_claims()
        data["claims"][0]["id"] = "B7"
        data["claims"][1]["serves"] = ["B1"]
        self.assertIn("(B7).id: must be C and a number", self.check(data))

    def test_warning_when_the_reason_names_a_longer_id(self):
        data = valid_claims()
        data["broad_statements"].append({"id": "B12", "quote": "Build failures are rare in general.",
                                         "page": 3, "section": "6 Discussion", "source": "conclusion",
                                         "note": "No result of the paper states it."})
        data["claims"][0]["selection_reason"] = "B12 would need revision."
        self.assertIn("(C1).selection_reason: names none of the broad statements", self.warnings(data))

    def test_serves_lists_an_id_twice(self):
        data = valid_claims()
        data["claims"][0]["serves"] = ["B1", "B1"]
        self.assertIn("'B1' is listed twice", self.check(data))

    def test_text_on_a_rejected_candidate_without_split(self):
        data = valid_claims()
        data["rejected"][0]["text"] = "Something else."
        self.assertIn("only a rejected part of a split statement has text", self.check(data))

    def test_split_part_repeats_the_whole_quote(self):
        data = valid_claims()
        data["claims"][1]["text"] = SPLIT_QUOTE
        self.assertIn("repeats the whole quote", self.check(data))

    def test_split_parts_drop_the_negation(self):
        quote = "Caching did not change the failure rate, and it cut build time by half."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "text": "Caching did change the failure rate.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "text": "Caching cut build time by half.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        self.assertIn("contains a negation (not) that no part keeps", self.warnings(data, TEXT + quote + "\n"))

    def test_split_part_of_a_quote_with_a_gap_inside_a_word(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\nWe found ten dis-\n"
                "Table 5 row with cells 12 34 56\ntinct codes and 4 themes in the data.\n")
        quote = "We found ten dis[...]tinct codes and 4 themes in the data."
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["rejected"] = []
        data["claims"] = [
            {"id": "C1", "quote": quote, "text": "We found ten distinct codes in the data.", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on it."},
            {"id": "C2", "quote": quote, "text": "We found 4 themes in the data.", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on it."}]
        self.assertEqual(self.check(data, text), "")

    def test_warnings(self):
        data = valid_claims()
        data["rejected"][0]["quote"] = "1,203 builds from 48 projects."
        data["claims"][0]["text"] = "Across the 48 projects, median build time fell from 4.1 to 9.2 minutes."
        out = self.warnings(data)
        self.assertIn("(R1).quote: starts in the middle of a sentence", out)
        self.assertIn("(C1).text: gives its numbers (48, 4.1, 9.2) in a different order", out)

    def test_warning_for_a_reason_that_names_no_broad_statement(self):
        data = valid_claims()
        data["claims"][0]["selection_reason"] = "Important result."
        self.assertIn("(C1).selection_reason: names none of the broad statements in serves", self.warnings(data))

    def test_warning_for_the_same_sentence_on_two_pages(self):
        text = TEXT.replace("Build failures are rare in general.", "Caching halves median build time.")
        data = valid_claims()
        data["rejected"].append({"id": "R2", "quote": "Caching halves median build time.", "page": 3,
                                 "section": "7 Conclusion", "duplicate_of": ["B1"], "reason": "Repeats B1."})
        self.assertIn("quotes the same sentence on another page", self.warnings(data, text))

    def test_warning_for_a_quote_inside_another_quote(self):
        data = valid_claims()
        data["rejected"][0]["quote"] = "median build time fell from 9.2 to 4.1 minutes"
        data["rejected"][0]["page"] = 2
        self.assertIn("quote: contains the quote of rejected[0] (R1)", self.warnings(data))

    def test_render_keeps_a_quote_on_one_line(self):
        data = valid_claims()
        data["rejected"][0]["quote"] = "We collected 1,203 builds\nfrom 48 projects."
        self.assertIn("> We collected 1,203 builds from 48 projects.", cea_claims.render(data))


class QuoteMatching(unittest.TestCase):
    def test_hyphen_before_a_capital_is_not_a_word_break(self):
        page = "Abstract-Caching halves median build time in 48 projects."
        self.assertEqual(cea_claims._sentence_warnings("q", "Caching halves median build time in 48 projects.", page), [])

    def test_em_dash_before_a_quote_is_not_a_word_break(self):
        page = "Abstract\u2014Caching halves median build time in 48 projects."
        self.assertEqual(cea_claims._sentence_warnings("q", "Caching halves median build time in 48 projects.", page), [])

    def test_footnote_number_inside_a_quote_can_be_left_out(self):
        self.assertTrue(cea_claims.quote_on("in revisions, assisted by tools", "in revisions,13 assisted by tools"))
        self.assertTrue(cea_claims.quote_on("with high effort ) and", "with high effort14 ) and"))

    def test_digits_of_the_text_are_not_footnotes(self):
        self.assertFalse(cea_claims.quote_on("For RQ, 12% of PRs were merged.", "For RQ3, 12% of PRs were merged."))
        self.assertFalse(cea_claims.quote_on("precision of 0. and recall", "precision of 0.93 and recall"))
        self.assertFalse(cea_claims.quote_on("we found 1234 defects", "we found 12 34 defects"))

    def test_signs_and_ranges_are_kept(self):
        self.assertFalse(cea_claims.quote_on("The correlation was 0.42 for large projects.",
                                             "The correlation was \u22120.42 for large projects."))
        self.assertTrue(cea_claims.quote_on("in 5-10 cases", "in 5\u201310 cases"))
        self.assertTrue(cea_claims.quote_on("fell to 4.1 minutes", "fell to 4.1 min-\nutes"))

    def test_single_digit_footnote_after_a_word(self):
        self.assertTrue(cea_claims.quote_on("on social media and search engine pollution. However",
                                            "on social media5 and search engine pollution.6 However"))

    def test_each_end_of_a_gap_must_be_a_line_break(self):
        page = "We found ten codes\nTable 5 row with cells 12 34 56\nin the data."
        self.assertFalse(cea_claims.quote_on("We found ten [...] in the data.", page))
        self.assertFalse(cea_claims.quote_on("We found ten codes [...] the data.", page))

    def test_printed_brackets_and_a_real_gap_in_one_quote(self):
        page = ('a developer wrote in the\nprompt:[...].\u201d The team then measured\n'
                'Table 2: Failure rates per project group\nthe failure rate of every build.')
        self.assertTrue(cea_claims.quote_on(
            'prompt:[...].\u201d The team then measured [...] the failure rate of every build.', page))

    def test_brackets_that_the_paper_prints_itself(self):
        page = "a developer wrote in the\nprompt:[...].\u201d In another example, a developer added"
        self.assertTrue(cea_claims.quote_on("prompt:[...].\u201d In another example, a developer added", page))

    def test_gap_must_fall_between_lines(self):
        self.assertFalse(cea_claims.quote_on("generating 40 [...] codes.", "started with\ngenerating 40 initial codes.\n"))
        self.assertTrue(cea_claims.quote_on("We found ten codes [...] in the data.",
                                            "We found ten codes\nTable 5 row with cells 12 34 56\nin the data."))


class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cea_claims.main(list(args))
        return code, out.getvalue()

    def test_validate_and_render_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            self.assertEqual(self.run_cli("validate", tmp)[0], 0)
            self.assertEqual(self.run_cli("render", tmp)[0], 0)
            (d / "claims.json").write_bytes(b'{"paper": "\xff"}')
            code, out = self.run_cli("validate", tmp)
            self.assertEqual(code, 1)
            self.assertIn("must be UTF-8", out)
            self.assertEqual(self.run_cli("render", tmp)[0], 1)

    def test_render_into_a_directory_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            (d / "claims.md").mkdir()
            code, out = self.run_cli("render", tmp)
            self.assertEqual(code, 2)
            self.assertIn("cannot write", out)

    def test_extract_rejects_a_missing_file_and_an_unsafe_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli("extract", str(Path(tmp) / "missing.pdf"), "--out", tmp)
            self.assertEqual(code, 2)
            self.assertIn("no such file", out)
            code, out = self.run_cli("extract", str(Path(tmp) / "missing.pdf"), "--out", tmp, "--id", "../x")
            self.assertEqual(code, 2)
            self.assertIn("invalid paper id", out)

    @unittest.skipUnless((PAPERS / "ieeesw26-ai-slop.pdf").is_file(), "ieeesw26-ai-slop.pdf is not in evals/papers/")
    def test_extract_reports_an_output_path_that_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "afile"
            target.write_text("x")
            code, out = self.run_cli("extract", str(PAPERS / "ieeesw26-ai-slop.pdf"), "--out", str(target))
            self.assertEqual(code, 2)
            self.assertIn("cannot write", out)


if __name__ == "__main__":
    unittest.main()
