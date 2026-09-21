"""Tests for pdf_text.py and cea_claims.py.

Run from the repository root:

    python3 -m unittest discover scripts/tests

The tests on real papers read the PDFs in evals/papers/ and are skipped when the PDFs are missing.
"""

import contextlib
import faulthandler
from html import unescape
import io
import itertools
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import unittest.mock


# Two bugs in this codebase were infinite loops, and an unbounded call wedges the whole run before
# any guarded test is reached. This cannot be swallowed: it runs on a watchdog thread and prints
# the traceback of whatever is stuck, which is what a timed-out CI job otherwise never gets.
faulthandler.dump_traceback_later(180, exit=True)


@contextlib.contextmanager
def bounded(seconds=10):
    """Fail rather than hang.

    Two bugs in this codebase were infinite loops, and an in-process call to one of them wedges
    the whole run before any subprocess-guarded test is reached. CI then burns to its timeout with
    nothing to report.
    """
    import signal

    def ring(signum, frame):
        raise AssertionError(f"call did not finish within {seconds}s")

    previous = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, seconds, seconds)  # re-arm: one ring can be swallowed
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import cea_claims  # noqa: E402
import cea_page  # noqa: E402
import pdf_text
import text_digests  # noqa: E402

PAPERS = SCRIPTS.parent / "evals" / "papers"


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


class PublishDestinations(unittest.TestCase):
    """Every destination under the site, not only the ones at its root."""

    def record(self, tmp):
        d = Path(tmp) / "rec"
        d.mkdir()
        (d / "text.txt").write_text(TEXT, encoding="utf-8")
        (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
        return d

    def build(self, tmp, prepare):
        import cea_site
        rec = self.record(tmp)
        site = Path(tmp) / "_site"
        prepare(Path(tmp), site)
        with contextlib.redirect_stdout(io.StringIO()):
            written, messages = cea_site.build_site([rec], site)
        return written, "\n".join(messages)

    def test_a_link_below_the_site_root_does_not_take_a_paper_outside_it(self):
        """The guard checked the root's own entries, and copytree then followed a link inside them."""
        def prepare(tmp, site):
            (site / "papers").mkdir(parents=True)
            (tmp / "outside").mkdir()
            (site / "papers" / "fixture").symlink_to(tmp / "outside")

        with tempfile.TemporaryDirectory() as tmp:
            written, messages = self.build(tmp, prepare)
            self.assertEqual(written, 0, "a paper was published through a link")
            self.assertIn("symbolic link", messages)
            self.assertEqual(sorted((Path(tmp) / "outside").iterdir()), [],
                             "the paper was written outside the site")

    def test_a_link_standing_in_for_a_page_does_not_overwrite_what_it_points_at(self):
        def prepare(tmp, site):
            (site / "papers" / "fixture").mkdir(parents=True)
            (tmp / "elsewhere.html").write_text("MINE", encoding="utf-8")
            (site / "papers" / "fixture" / "index.html").symlink_to(tmp / "elsewhere.html")

        with tempfile.TemporaryDirectory() as tmp:
            written, messages = self.build(tmp, prepare)
            self.assertEqual(written, 0)
            self.assertEqual((Path(tmp) / "elsewhere.html").read_text(encoding="utf-8"), "MINE")

    def test_a_rebuild_over_a_real_site_still_merges(self):
        """The guard must not cost the ordinary rebuild, which keeps a dropped paper's folder."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                cea_site.build_site([rec], site)
            (site / "papers" / "older").mkdir(parents=True)
            (site / "papers" / "older" / "index.html").write_text("old", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
        self.assertEqual(written, 1, messages)

    def test_a_paper_published_under_another_name_says_so(self):
        """The `<id>.pdf` fallback put a different document behind the recorded name."""
        def prepare(tmp, site):
            (Path(tmp) / "rec" / "fixture.pdf").write_bytes(b"%PDF-ANOTHER-PAPER\n")

        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            data = valid_claims()
            data["paper"]["pdf"] = "the-real-paper.pdf"
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            (rec / "fixture.pdf").write_bytes(b"%PDF-ANOTHER-PAPER\n")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()) as said:
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, messages)
            self.assertIn("published under that name", said.getvalue())


class ShapeGateAgreesWithValidate(unittest.TestCase):
    """The gate must not refuse what validate calls clean and then send the user to validate."""

    def test_a_long_split_name_is_accepted_by_both(self):
        """The gate allowed six digits and validate any number, so S1234567 was a dead end."""
        import cea_site
        data = valid_claims()
        for entry in data["claims"]:
            entry["split_from"] = "S1234567"
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, _ = cea_claims.validate(rec)
            self.assertEqual(problems, [], "validate refuses it, so the gate may too")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
        self.assertEqual(written, 1, f"the gate refused what validate calls clean: {messages}")


class NamedPaths(unittest.TestCase):
    """An empty path from the shell is an unset variable, not a request to use the default."""

    def test_an_empty_out_is_refused(self):
        for command in (["site", "rec", "--out", ""], ["extract", "p.pdf", "--out", ""]):
            with self.subTest(command=command[0]):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as caught:
                        cea_claims.main(command)
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("name a path", err.getvalue())

    def test_an_empty_framework_is_refused(self):
        """It was falsy, so the site built with no framework page and no link to one, at exit 0."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as caught:
                cea_claims.main(["site", "rec", "--out", "s", "--framework", ""])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("name a path", err.getvalue())


class DeepJson(unittest.TestCase):
    """Thousands of nested arrays exhaust the stack before json.loads gives up."""

    def record(self, tmp):
        d = Path(tmp) / "rec"
        d.mkdir()
        (d / "text.txt").write_text(TEXT, encoding="utf-8")
        (d / "claims.json").write_text("[" * 60000 + "]" * 60000, encoding="utf-8")
        return d

    def test_validate_reports_it_rather_than_tracing_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            problems, data = cea_claims.validate(self.record(tmp))
        self.assertIsNone(data)
        self.assertIn("nested too deeply", "\n".join(problems))

    def test_site_reports_it_too(self):
        """The catch was added to validate only, and `site` reads claims.json itself first."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
        self.assertEqual(written, 0)
        joined = "\n".join(messages)
        self.assertIn("nested too deeply", joined)
        self.assertIn("CEA_FAILED", joined)


class SymbolicLinksInTheRecord(unittest.TestCase):
    """Writing through a link replaces whatever it points at, anywhere on the machine."""

    def test_extract_does_not_write_through_a_link(self):
        import pdf_text
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        precious = tmp / "precious.txt"
        precious.write_text("PRECIOUS", encoding="utf-8")
        pdf = tmp / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        out = tmp / "out" / "paper"
        out.mkdir(parents=True)
        (out / "text.txt").symlink_to(precious)
        extraction = pdf_text.Extraction(
            pages=[pdf_text.Page(number=1, lines=["word " * 200], regions=[])],
            lineno=False, references=None)
        with mock.patch.object(pdf_text, "extract", return_value=extraction):
            with contextlib.redirect_stdout(io.StringIO()) as said:
                code = cea_claims.main(["extract", str(pdf), "--out", str(tmp / "out")])
        self.assertEqual(code, 2, said.getvalue())
        self.assertEqual(precious.read_text(encoding="utf-8"), "PRECIOUS",
                         "extract wrote through the link and replaced the file it points at")

    def test_extract_does_not_copy_the_paper_through_a_link(self):
        import pdf_text
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        precious = tmp / "precious.pdf"
        precious.write_bytes(b"PRECIOUS")
        pdf = tmp / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        out = tmp / "out" / "paper"
        out.mkdir(parents=True)
        (out / "paper.pdf").symlink_to(precious)
        extraction = pdf_text.Extraction(
            pages=[pdf_text.Page(number=1, lines=["word " * 200], regions=[])],
            lineno=False, references=None)
        with mock.patch.object(pdf_text, "extract", return_value=extraction):
            with contextlib.redirect_stdout(io.StringIO()) as said:
                code = cea_claims.main(["extract", str(pdf), "--out", str(tmp / "out")])
        self.assertEqual(code, 0, said.getvalue())
        self.assertIn("CEA_WARNING", said.getvalue())
        self.assertEqual(precious.read_bytes(), b"PRECIOUS",
                         "the paper was copied through the link, over the file it points at")


class ExtractCopiesThePaper(unittest.TestCase):
    """The record has to be complete on its own: the site publishes the paper only from inside it."""

    def run_extract(self, name="paper.pdf"):
        import pdf_text
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pdf = tmp / name
        pdf.write_bytes(b"%PDF-1.4 fixture\n")
        pages = [pdf_text.Page(number=1, lines=["word " * 200], regions=[])]
        extraction = pdf_text.Extraction(pages=pages, lineno=False, references=None)
        out = tmp / "out"
        with mock.patch.object(pdf_text, "extract", return_value=extraction):
            with contextlib.redirect_stdout(io.StringIO()) as said:
                code = cea_claims.main(["extract", str(pdf), "--out", str(out)])
        return code, said.getvalue(), out, pdf

    def test_the_paper_is_copied_into_the_record(self):
        code, said, out, pdf = self.run_extract()
        self.assertEqual(code, 0, said)
        copied = out / "paper" / "paper.pdf"
        self.assertTrue(copied.is_file(), f"the paper was not copied in: {said}")
        self.assertEqual(copied.read_bytes(), pdf.read_bytes())
        self.assertIn("write that name as paper.pdf", said)

    def test_a_paper_named_after_a_file_the_site_publishes_is_not_copied_over_it(self):
        """A PDF called text.txt would overwrite the page text every quote is checked against."""
        code, said, out, pdf = self.run_extract("text.txt")
        self.assertEqual(code, 0, said)
        self.assertIn("CEA_WARNING", said)
        self.assertIn("=== page 1 ===", (out / "text.txt").read_text(encoding="utf-8")
                      if (out / "text.txt").is_file()
                      else (out / "text" / "text.txt").read_text(encoding="utf-8"))


class UncoveredGuards(unittest.TestCase):
    """Guards that were added without a test, so reverting them cost nothing."""

    def record(self, tmp, mutate=None):
        data = valid_claims()
        if mutate:
            mutate(data)
        rec = Path(tmp) / "rec"
        rec.mkdir()
        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
        (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        return rec

    def test_a_paper_title_of_only_spaces_is_refused(self):
        """The gate checked the type and not the text, so the masthead came out empty."""
        import cea_site
        for title in ("", "   "):
            with self.subTest(title=repr(title)), tempfile.TemporaryDirectory() as tmp:
                rec = self.record(tmp, lambda d: d["paper"].update(title=title))
                with contextlib.redirect_stdout(io.StringIO()):
                    written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                self.assertEqual(written, 0)
                self.assertIn("paper.title", "\n".join(messages))

    def test_a_site_with_no_index_page_is_not_reported_as_published(self):
        """The post-condition had no test, so removing it left the suite green."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            site.mkdir()
            (site / "index.html").symlink_to(Path(tmp) / "escaped.html")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 0, "the index was published through a symbolic link")
            self.assertFalse((Path(tmp) / "escaped.html").exists(),
                             "a file was written outside the site")

    def test_a_paper_name_too_long_for_the_filesystem_is_not_a_build_failure(self):
        """`resolve()` accepts it and `is_file()` raises, so the whole build died on one record."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].update(pdf="x" * 252 + ".pdf"))
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()) as said:
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, f"an over-long paper name stopped the build: {messages}")
            self.assertIn("no PDF found", said.getvalue())


class TruncatedFigures(unittest.TestCase):
    """A number runs on across its decimal point, and a shortened figure is a different value."""

    def test_a_quote_cannot_stop_inside_a_figure(self):
        for quote, page in (
                ("churn decreased from 0.17 to 0.",
                 "The average churn decreased from 0.17 to 0.06 here."),
                ("these posts received 1.", "On average, these posts received 1.6 codes"),
                ("Of all 1,154 posts, 978 (84.", "Of all 1,154 posts, 978 (84.7%) were coded.")):
            with self.subTest(quote=quote[-16:]):
                self.assertFalse(cea_claims.quote_on(quote, page))

    def test_a_figure_the_quote_carries_whole_still_matches(self):
        for quote, page in (
                ("The average churn decreased from 0.17 to 0.06 here.",
                 "The average churn decreased from 0.17 to 0.06 here."),
                ("The rate was 3.2.", "The rate was 3.2. Then it fell."),
                ("We saw 1,203 builds.", "We saw 1,203 builds. Then more.")):
            with self.subTest(quote=quote[-16:]):
                self.assertTrue(cea_claims.quote_on(quote, page))

    def test_a_footnote_marker_may_still_be_left_off_the_last_word(self):
        """`record-format.md` allows it, and the anchor was rejecting it at the quote's end."""
        page = "we surveyed social media5 and found nothing"
        self.assertTrue(cea_claims.quote_on("we surveyed social media", page))
        self.assertTrue(cea_claims.quote_on("we surveyed social media and found", page))


class BroadStatementStates(unittest.TestCase):
    """`states` on a broad statement carries the main result, so it is held to the split rules."""

    QUOTE = "Caching did not reduce build failures, and it halved median build time."
    TEXT = ("=== page 1 ===\nCaching did not reduce build failures, and it halved median build "
            "time.\n=== page 2 ===\nAcross the 48 projects, median build time fell from 9.2 to "
            "4.1 minutes.\n")

    def warnings(self, states):
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["broad_statements"][0].update(quote=self.QUOTE, states=states, page=1)
        data["claims"] = [{"id": "C1", "quote": "Across the 48 projects, median build time fell "
                                                "from 9.2 to 4.1 minutes.",
                           "states": "Across the 48 projects, median build time fell from 9.2 to "
                                     "4.1 minutes.",
                           "page": 2, "section": "5 Results", "serves": ["B1"],
                           "split_from": None,
                           "selection_reason": "B1 rests on this comparison of build times."}]
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(self.TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            return "\n".join(problems or cea_claims.advisories(d, loaded))

    def test_dropping_the_negation_but_keeping_what_it_negates_is_questioned(self):
        """The same inversion written as a split part was caught. This side was not."""
        self.assertIn("without the \"not\" before it", self.warnings(
            "Caching did reduce build failures"))

    def test_leaving_the_negated_clause_out_altogether_is_not(self):
        self.assertNotIn(".states:", self.warnings("it halved median build time"))


class TheSumAdvisory(unittest.TestCase):
    """It is the only place the tool does arithmetic on the paper, so it has to be right."""

    def note(self, text, states=None):
        entry = {"note": text, "states": states or "Of the 1,203 builds, 700 succeeded."}
        return entry

    def check(self, note, states=None):
        import tempfile
        data = valid_claims()
        data["rejected"][0]["note"] = note
        if states:
            data["rejected"][0]["states"] = states
            data["rejected"][0]["split_from"] = "S9"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            if problems:
                return "\n".join(problems)
            return "\n".join(cea_claims.advisories(d, loaded))

    def test_a_note_naming_the_whole_and_wrong_parts_is_questioned(self):
        out = self.check("Table 2 (page 2) prints the parts of the sentence's 1,203 and no "
                         "total: 700 and 403.")
        self.assertIn("add up to 1103", out)

    def test_the_same_note_with_the_right_parts_is_not(self):
        out = self.check("Table 2 (page 2) prints the parts of the sentence's 1,203 and no "
                         "total: 700 and 503.")
        self.assertNotIn("add up to", out)

    def test_a_note_naming_the_parts_of_two_quantities_is_not_added_together(self):
        """Adding every number a note holds gives a total no one asserted.

        A sentence that pairs two counts has a note naming the rows behind each. Their grand
        total is meaningless, and it was reported as though the note had claimed it.
        """
        out = self.check("Table 3 prints no total; for translation it prints 9 and 2, and for "
                         "optimization it prints 7 and 8.")
        self.assertNotIn("add up to", out)


class TheGate(unittest.TestCase):
    """`scripts/check.sh` is the only step CI runs, and nothing exercised it.

    A test runs in the same interpreter as the runner, so it can take the exit status, rewrite the
    file the runner writes, or print whatever it likes. No one of those channels is trusted alone.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("sh"):
            raise unittest.SkipTest("sh is not installed")
        # The gate runs this suite, and this suite runs the gate. The inner run skips.
        if os.environ.get("CEA_INNER_RUN"):
            raise unittest.SkipTest("this is the run the gate started")
        cls.tree = Path(tempfile.mkdtemp())
        root = Path(__file__).resolve().parent.parent.parent
        for name in ("scripts", "skills", "evals", "claims.schema.json", ".claude-plugin"):
            src = root / name
            if not src.exists():
                continue
            if src.is_dir():
                shutil.copytree(src, cls.tree / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*-workspace",
                                                              "papers"))
            else:
                shutil.copy2(src, cls.tree / name)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tree, ignore_errors=True)

    def run_gate(self, module=None, name="test_zprobe.py", env=None):
        probe = self.tree / "scripts" / "tests" / name
        if module is not None:
            probe.write_text(module, encoding="utf-8")
        try:
            # CEA_REQUIRE_PAPERS is stripped: the copied tree never holds the papers, so
            # letting it through made the documented release command impossible to satisfy on
            # any tree, papers present or not.
            outer = {k: v for k, v in os.environ.items() if k != "CEA_REQUIRE_PAPERS"}
            done = subprocess.run(["sh", "scripts/check.sh"], cwd=self.tree,
                                  capture_output=True, text=True, timeout=600,
                                  env={**outer, "CEA_INNER_RUN": "1", **(env or {})})
        finally:
            probe.unlink(missing_ok=True)
        return done.returncode, done.stdout + done.stderr

    def test_a_clean_tree_passes(self):
        code, out = self.run_gate()
        self.assertEqual(code, 0, out[-600:])

    def test_a_failing_test_fails_the_gate_and_says_why(self):
        code, out = self.run_gate("import unittest\n\n\n"
                                  "class T(unittest.TestCase):\n"
                                  "    def test_x(self):\n"
                                  "        self.fail('INJECTED')\n")
        self.assertNotEqual(code, 0)
        self.assertIn("INJECTED", out, "the cause was not printed")

    def test_a_test_that_seizes_the_exit_status_does_not_pass_the_gate(self):
        code, out = self.run_gate("import atexit\nimport os\nimport unittest\n\n"
                                  "atexit.register(lambda: os._exit(0))\n\n\n"
                                  "class T(unittest.TestCase):\n"
                                  "    def test_x(self):\n"
                                  "        self.fail('INJECTED')\n")
        self.assertNotEqual(code, 0, out[-600:])

    def test_a_test_that_rewrites_the_count_file_does_not_pass_the_gate(self):
        code, out = self.run_gate("import atexit\nimport os\nimport sys\nimport unittest\n\n\n"
                                  "def rewrite():\n"
                                  "    with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
                                  "        f.write('CEA_TESTS ran=9999 skipped=0 ok=1\\n')\n"
                                  "    os._exit(0)\n\n\n"
                                  "atexit.register(rewrite)\n\n\n"
                                  "class T(unittest.TestCase):\n"
                                  "    def test_x(self):\n"
                                  "        self.fail('INJECTED')\n")
        self.assertNotEqual(code, 0, out[-600:])


class TheSumAdvisoryResidue(unittest.TestCase):
    def check(self, note):
        data = valid_claims()
        data["rejected"][0]["note"] = note
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            return "\n".join(problems or cea_claims.advisories(d, loaded))

    def test_each_named_whole_owns_only_its_own_values(self):
        """Reading to the end of the note added the second quantity's rows to the first's total."""
        self.assertNotIn("add up to", self.check(
            "Table 3 prints the parts of the sentence's 26 and no total: Label revision 8, README "
            "revision 7, Comment revision 11. Table 5 prints the parts of the sentence's 12 and "
            "no total: Whole methods 7 and One block 5."))

    def test_an_entry_id_is_not_read_as_a_total(self):
        """"part of B12" captured 12, and four committed notes are written that way."""
        self.assertNotIn("add up to", self.check(
            "The remaining part of B12 is recorded separately. Table 4 prints 9 and 8 for the "
            "two groups."))

    def test_one_quantity_with_a_wrong_sum_is_still_questioned(self):
        self.assertIn("add up to 1103", self.check(
            "Table 2 prints the parts of the sentence's 1,203 and no total: 700 and 403."))


class TheDocumentedReasonShapes(unittest.TestCase):
    """Every ground the reference prescribes has to pass the check that reads a reason."""

    def warns(self, reason):
        data = valid_claims()
        data["rejected"][0]["reason"] = reason
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            return any("no ground" in w for w in (problems or cea_claims.advisories(d, loaded)))

    def test_the_standing_template_passes_however_it_opens(self):
        """"B1 would still stand." passed and "Main result B1 would still stand." did not."""
        for reason in ("B1 would still stand.", "Main result B1 would still stand.",
                       "Main result B1 still stands without it.",
                       "Describes the data, not a result."):
            with self.subTest(reason=reason):
                self.assertFalse(self.warns(reason))


class AVerdictIsNotAGround(unittest.TestCase):
    """A reason has to say why, and a word after the verdict used to be enough to escape."""

    def warns(self, reason):
        data = valid_claims()
        data["rejected"][0]["reason"] = reason
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            return any("no ground" in w for w in (problems or cea_claims.advisories(d, loaded)))

    def test_every_shape_of_verdict_is_questioned(self):
        for reason in ("Not a claim.", "Not a claim here.", "Not a claim, B1.",
                       "Does not qualify.", "It does not qualify.", "Nothing rests on it.",
                       "See the note.", "Judged not to be one.", "Excluded from the set.",
                       "Fails the test."):
            with self.subTest(reason=reason):
                self.assertTrue(self.warns(reason))

    def test_a_reason_that_gives_a_ground_is_not(self):
        for reason in ("Describes the data, not a result of the study.",
                       "B1 would still stand, because this only repeats the sample size."):
            with self.subTest(reason=reason[:40]):
                self.assertFalse(self.warns(reason))


class GroundsAreChecked(unittest.TestCase):
    """A reason and a selection_reason carry the ground for a judgement the pages publish."""

    def warnings(self, mutate):
        import tempfile
        data = valid_claims()
        mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            return "\n".join(problems or cea_claims.advisories(d, loaded))

    def test_naming_an_id_does_not_excuse_a_verdict_for_a_reason(self):
        """"Important." was questioned and "Important. B1." was not."""
        for reason in ("Important.", "Important. B1."):
            with self.subTest(reason=reason):
                self.assertIn("no ground a checker can assess",
                              self.warnings(lambda d: d["rejected"][0].update(reason=reason)))

    def test_a_selection_reason_of_an_id_alone_is_questioned(self):
        """The reference asks it to say how the main result would fail without the claim."""
        self.assertIn("names the broad statement and nothing else",
                      self.warnings(lambda d: d["claims"][0].update(selection_reason="B1")))

    def test_a_real_selection_reason_is_not(self):
        self.assertNotIn("nothing else", self.warnings(lambda d: None))


class MessagesThatCanBeActedOn(unittest.TestCase):
    """A message an agent cannot act on sends it round a loop it never leaves."""

    def record(self, tmp, mutate=None):
        data = valid_claims()
        if mutate:
            mutate(data)
        d = Path(tmp)
        (d / "text.txt").write_text(TEXT, encoding="utf-8")
        (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        return d

    GAPPED = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
              "We found 38 dis\nTABLE I  A caption here\ntinct configurations here.\n")

    def gapped(self, states):
        """A record whose one claim quotes a word the page breaks around a table."""
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["claims"] = [{"id": "C1", "quote": "We found 38 dis[...]tinct configurations here.",
                           "states": states, "page": 2, "section": "5 Results", "serves": ["B1"],
                           "split_from": None,
                           "selection_reason": "B1 rests on this count of configurations."}]
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(self.GAPPED, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return cea_claims.validate(d)[0]

    def test_a_claim_whose_quote_holds_a_gap_inside_a_word_has_a_states_it_can_write(self):
        """The word whole was refused, the word with the marker was refused, and the word broken
        in two was the only thing accepted, while `states` is what the checker is shown."""
        for states in ("We found 38 distinct configurations here.",
                       "We found 38 dis[...]tinct configurations here."):
            with self.subTest(states=states[:34]):
                self.assertEqual(self.gapped(states), [])

    def test_a_states_that_is_not_the_quote_is_still_refused(self):
        self.assertIn(".states: differs from the quote",
                      "\n".join(self.gapped("We found 39 distinct configurations here.")))

    BETWEEN = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
               "The median was 4.1\nTABLE II  Review latency by project\n"
               "across every project we measured.\n")

    def between(self, states):
        """A record whose one claim quotes across a gap standing between two whole words."""
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["claims"] = [{"id": "C1",
                           "quote": "The median was 4.1 [...] across every project we measured.",
                           "states": states, "page": 2, "section": "5 Results", "serves": ["B1"],
                           "split_from": None,
                           "selection_reason": "B1 rests on this median."}]
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(self.BETWEEN, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return cea_claims.validate(d)[0]

    def test_a_gap_between_two_words_leaves_a_states_that_can_be_written(self):
        """The marker was dropped with the space around it, so "4.1 [...] across" folded to
        "4.1across" and no `states` matched it: the one spelling the message asks for was
        refused, and the record could not be written at all."""
        for states in ("The median was 4.1 across every project we measured.",
                       "The median was 4.1 [...] across every project we measured."):
            with self.subTest(states=states[:34]):
                self.assertEqual(self.between(states), [])

    def test_a_gap_between_two_words_does_not_join_them(self):
        """The two words stay two: a `states` that runs them together is not the quote."""
        self.assertIn(".states: differs from the quote",
                      "\n".join(self.between(
                          "The median was 4.1across every project we measured.")))

    def test_a_note_that_says_how_the_pairing_goes_clears_the_warning(self):
        """It cleared only on the literal word "respectively", which nothing told the checker."""
        note = ("In the quote the counts pair with the categories in order: optimization with 34 "
                "instances and maintenance with 26 instances.")
        for states in ("optimization tasks accounted for 34 instances",
                       "maintenance tasks accounted for 26 instances"):
            with self.subTest(states=states):
                self.assertTrue(cea_claims._note_pairs({"note": note, "states": states}))
        self.assertFalse(cea_claims._note_pairs(
            {"note": "The extraction garbled a sign here.", "states": "x 34 y"}),
            "a note that says nothing about the pairing must not clear it")

    def test_validate_names_the_directory_when_there_is_none(self):
        with contextlib.redirect_stdout(io.StringIO()) as said:
            code = cea_claims.main(["validate", "/nonexistent/paper/dir"])
        self.assertEqual(code, 1)
        self.assertIn("is not a directory", said.getvalue())

    def test_an_empty_framework_document_is_refused(self):
        """Every paper's footer links it as the page that defines the terms they use."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            self.record(rec)
            empty = Path(tmp) / "framework.md"
            # nothing but an HTML comment: it has text, and md_to_html drops comments, so the
            # page a reader gets is blank
            for content in ("   \n", "<!--\nan internal note the reader must not see\n-->\n"):
                empty.write_text(content, encoding="utf-8")
                with contextlib.redirect_stdout(io.StringIO()) as said:
                    code = cea_claims.main(["site", str(rec), "--out", str(Path(tmp) / "_site"),
                                            "--framework", str(empty)])
                with self.subTest(document=content.split("\n")[0]):
                    self.assertEqual(code, 2)
                    self.assertIn("renders to an empty page", said.getvalue())

    def test_a_second_paper_in_a_record_directory_is_pointed_out(self):
        """The page's footer says every quote was checked against the paper paper.pdf names."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            (d / "fixture.pdf").write_bytes(b"%PDF")
            (d / "another-paper.pdf").write_bytes(b"%PDF")
            problems, loaded = cea_claims.validate(d)
            self.assertEqual(problems, [])
            warnings = "\n".join(cea_claims.advisories(d, loaded))
        self.assertIn("another-paper.pdf", warnings)
        self.assertIn("also stands here", warnings)


class AppendixWithAQuestionnaire(unittest.TestCase):
    """Removal stops where the paper's text resumes, and a numbered question is not an entry."""

    def test_an_appendix_of_numbered_questions_is_not_removed_with_the_bibliography(self):
        body = [f"Body sentence {i} of the paper, which runs on a while." for i in range(40)]
        refs = ["R EFERENCES"] + [f"[{i}] A. Author{i}. 20{10 + i}. A title. In Proc. X."
                                  for i in range(1, 31)]
        appendix = ["Study 2 Materials"]
        appendix += [f"{i}. Question number {i} of the survey we ran?" for i in range(1, 9)]
        appendix += ["The extra analysis shows a 0.29 effect size for the second study here.",
                     "We also report the median completion time for each condition below.",
                     "Nine of the fourteen participants said they would keep using the tool."]
        pages = [pdf_text.Page(1, body[:20]), pdf_text.Page(2, body[20:]),
                 pdf_text.Page(3, refs), pdf_text.Page(4, appendix)]
        pdf_text._drop_references(pages)
        kept = [l for page in pages for l in page.lines if l.strip()]
        self.assertTrue(any("0.29 effect size" in l for l in kept),
                        "the appendix's own result was removed with the bibliography")
        self.assertEqual(sum(1 for l in kept if l.strip()[:2].rstrip(".").isdigit()), 8,
                         "the questionnaire was removed")
        self.assertFalse(any("In Proc. X" in l for l in kept),
                         "the bibliography was not removed")


class ReferencesRemoval(unittest.TestCase):
    """What the removal must not take with it. A deleted claim cannot be quoted at all."""

    def test_a_references_line_quoted_inside_a_prompt_does_not_delete_the_paper(self):
        """A paper about prompting prints "References:" inside a prompt, with an entry under it.

        The first such heading used to start the removal, so everything after the prompt was
        blanked: the Discussion, the Conclusion, and every number in them.
        """
        body = ["I. INTRODUCTION"] + [f"We studied {i} repositories and their reviews."
                                      for i in range(1, 12)]
        prompt = ["Our prompt template was:", "    Summarize the paper.", "    References:",
                  "    [1] A. Smith and B. Jones, A study of things, 2021."]
        after = ["Reviewers accepted 41% of the generated summaries.",
                 "Our tool cut review time by 22 minutes per pull request.",
                 "V. CONCLUSION",
                 "Grounding failed for 15% of the sentences we checked."]
        refs = ["R EFERENCES", "[1] A. Author. 2020. A title. In Proc. X.",
                "[2] B. Author. 2021. Another title."]
        pages = [pdf_text.Page(1, body + prompt + after), pdf_text.Page(2, refs)]
        pdf_text._drop_references(pages)
        kept = [l for page in pages for l in page.lines]
        for claim in ("41%", "22 minutes", "15% of the sentences"):
            self.assertTrue(any(claim in l for l in kept), f"{claim} was deleted with the prompt")
        self.assertFalse(any("A title. In Proc" in l for l in kept),
                         "the paper's own bibliography was not removed")

    def test_an_appendix_that_opens_with_a_numbered_list_is_kept(self):
        """A survey instrument opens "1. How many years ...", which reads as "1. A. Smith, ..."."""
        following = ["1. How many years have you reviewed code?",
                     "2. How often do you review AI-written code?",
                     "Of the 120 respondents, 62% said they review AI-written code daily."]
        for heading in ("B. Survey Instrument", "C. Additional Results", "APPENDIX"):
            with self.subTest(heading=heading):
                self.assertTrue(pdf_text._is_appendix_heading(heading, following),
                                "a numbered list made the appendix look like a bibliography")

    def test_a_wrapped_title_before_real_entries_is_still_not_an_appendix(self):
        entries = ["[1] A. Author. 2020. A title. In Proc. X.",
                   "[2] B. Author. 2021. Another title."]
        self.assertFalse(pdf_text._is_appendix_heading("A Wrapped Title Of Some Paper", entries))


class PageMarkersPerPage(unittest.TestCase):
    """`to_text` is what writes the markers, and nothing asserted it writes one per page."""

    def pages(self, n):
        return pdf_text.Extraction(
            pages=[pdf_text.Page(number=i, lines=[f"Text of page {i}."]) for i in range(1, n + 1)],
            lineno=False, references=None)

    def test_one_marker_per_page_in_order(self):
        text = pdf_text.to_text(self.pages(5))
        self.assertEqual(re.findall(r"^=== page (\d+) ===$", text, re.M),
                         ["1", "2", "3", "4", "5"])

    def test_an_empty_page_still_opens(self):
        """A page whose text layer is empty keeps its number, or every later page shifts."""
        extraction = self.pages(3)
        extraction.pages[1].lines = []
        self.assertEqual(re.findall(r"^=== page (\d+) ===$", pdf_text.to_text(extraction), re.M),
                         ["1", "2", "3"])


class DamagedPdf(unittest.TestCase):
    """poppler reports what it could not read on stderr and exits 0 all the same."""

    def test_what_pdftotext_said_reaches_the_user(self):
        import subprocess
        done = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="=== nothing ===\n\x0c",
            stderr="Syntax Error: Kid object (page 7) is wrong type (null)\n")
        with mock.patch.object(subprocess, "run", return_value=done):
            pages, said = pdf_text._pages("paper.pdf")
        self.assertIn("page 7", said, "what pdftotext said was dropped")

    def test_the_warning_names_the_risk(self):
        """A missing page shifts every later page number, and a record names pages."""
        extraction = pdf_text.Extraction(
            pages=[pdf_text.Page(number=1, lines=["word " * 200], regions=[])],
            lineno=False, references=None, two_column=False,
            unreadable="Syntax Error: Kid object (page 7) is wrong type (null)")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pdf = tmp / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with mock.patch.object(pdf_text, "extract", return_value=extraction):
            with contextlib.redirect_stdout(io.StringIO()) as said:
                cea_claims.main(["extract", str(pdf), "--out", str(tmp / "out")])
        printed = said.getvalue()
        self.assertIn("CEA_WARNING", printed)
        self.assertIn("carries the number of another", printed)


class TableColumnsThatCountUp(unittest.TestCase):
    """A table's last column counts up exactly as a line numbering does."""

    def test_a_year_column_on_a_page_of_text_is_kept(self):
        prose = [f"Body sentence number {i} of this page, which runs on and wraps."
                 for i in range(1, 26)]
        table = ["Study            Year"] + [f"Smith et al.      {2015 + i}" for i in range(1, 8)]
        out, found = pdf_text._strip_line_numbers(prose[:12] + [""] + table + [""] + prose[12:])
        self.assertFalse(found, "a year column was read as the paper's line numbering")
        self.assertTrue(any("2016" in l for l in out), "the years were blanked")

    def test_a_number_glued_to_a_short_cell_is_not_a_line_number(self):
        rows = [f"Smith et al.      {2015 + i}" for i in range(1, 9)]
        self.assertEqual(pdf_text._counted_numbers(rows), {},
                         "the last cell of each row was counted as a line number")


class LayoutKeepsEveryWord(unittest.TestCase):
    """Putting two columns in reading order may move a line. It may not change the words.

    The same property is checked against the real papers in `RealPapers`, which skip wherever the
    PDFs are absent, and they are third-party and not committed. These pages are built here, so the
    property is checked wherever the suite runs, CI included.
    """

    GUTTER = 56

    def page(self, kind):
        left = ["Caching halves the median build time across",
                "the projects we studied, and the failure",
                "rate does not rise with it. We measured",
                "both over the whole period rather than at",
                "a single point in time, because the load",
                "on the build farm varies through the week.",
                "The medians are reported for that reason.",
                "The appendix gives the daily figures."]
        right = ["The tools we compared differ in how they",
                 "invalidate an entry, which matters more",
                 "than the size of the cache in our sample.",
                 "We report the medians because the",
                 "distributions are skewed. The mean would",
                 "be pulled by a handful of very long builds",
                 "that the cache never helps, and those are",
                 "the builds a reader asks about first."]
        # sixteen distinct pairs: a block has to be longer than _MIN_REGION_LINES to be read as
        # two columns at all, and a fixture whose lines repeat cannot show an order
        left = left + [f"A further sentence of the left column, number {i}." for i in range(8)]
        right = right + [f"A further sentence of the right column, number {i}." for i in range(8)]
        rows = [f"{l:<{self.GUTTER}}{r}" for l, r in zip(left, right)]
        if kind == "plain":
            return rows
        if kind == "with a full-width table":
            width = max(len(r) for r in rows)
            caption = f"{'':<{(width - 8) // 2}}TABLE II"
            table = [f"{'    Task Triggers':<30}Defines when and how the action runs each time"
                     for _ in range(4)]
            return rows[:1] + [""] + [caption] + table + [""] + rows[1:]
        if kind == "with a centred title block":
            width = max(len(r) for r in rows)
            title = [f"{'':<{(width - 28) // 2}}A Title Of The Paper Goes Here",
                     f"{'':<{(width - 44) // 2}}Sebastian Baltes, Marc Cheong, Christoph Treude"]
            return title + [""] + rows
        if kind == "with a justified gap":
            # a stretched space inside a justified line, which must not read as a table cell
            return [r.replace("median build", "median    build") for r in rows]
        raise AssertionError(kind)

    def test_a_row_that_keeps_the_table_s_own_cells_is_one(self):
        """Its only gap falls on the gutter, so it counts one cell where two are asked for.

        It is a row all the same, because it breaks where the table already breaks. Without that,
        the table ends at this line and the page is cut through the rows below it.
        """
        header = f"{'Code':<30}{'Description':<40}Source"
        width = len(header)
        caption = f"{'':<{(width - 7) // 2}}TABLE I"
        rows = [f"{'reviewer-burden':<30}{'Workload asymmetry where AI saves':<40}R05"
                for _ in range(3)]
        # Blank where the gutter falls, so it counts one cell where two are asked for and does not
        # read as a row that crosses the gutter either. It is a row because its gap ends where the
        # table's last column begins.
        continued = f"{'':<30}{'time and the reviewer pays':<40}R13"
        gutter = 60
        self.assertTrue(pdf_text._blank_at(continued, gutter), "the fixture crosses the gutter")
        self.assertFalse(pdf_text._spans_with_cells(continued, gutter),
                         "the fixture is a row by another rule, so this one is not under test")
        lines = [caption, header] + rows[:1] + [continued] + rows[1:]
        self.assertEqual(pdf_text._full_width_tables(lines, gutter, width), [(0, len(lines))],
                         "a row that keeps the table's own cells did not count as a row")

    def test_no_word_is_added_dropped_or_broken(self):
        import collections
        for kind in ("plain", "with a full-width table", "with a centred title block",
                     "with a justified gap"):
            with self.subTest(page=kind):
                lines = self.page(kind)
                out, _, _ = pdf_text._layout(lines)
                self.assertEqual(collections.Counter(" ".join(lines).split()),
                                 collections.Counter(" ".join(out).split()),
                                 "the layout changed the words of the page")

    def test_no_line_crosses_from_one_column_into_the_other(self):
        """Conserving the words is not enough. Every rule that keeps a line whole could be removed
        and the words would still all be there, in the wrong column.

        A line pulled whole into the left column leaves a hole in the right one, and the two lines
        that close over that hole read as one sentence the page never printed.
        """
        for kind in ("plain", "with a justified gap"):
            with self.subTest(page=kind):
                lines = self.page(kind)
                out, regions, g = pdf_text._layout(lines)
                self.assertTrue(regions, "the page was not read as two columns")
                right_words = {w for l in lines for w in l[g:].split()}
                left_words = {w for l in lines for w in l[:g].split()}
                only_right = right_words - left_words
                self.assertTrue(only_right, "the fixture cannot tell the columns apart")
                for line in out:
                    got = set(line.split())
                    if got & only_right:
                        self.assertFalse(got - right_words,
                                         f"a line mixes the two columns: {line.strip()!r}")

    def test_each_column_keeps_its_own_order(self):
        """Conserving the words is not enough: they have to stay in the order they were read in."""
        lines = self.page("plain")
        out, regions, _ = pdf_text._layout(lines)
        self.assertEqual(regions, 1, "the page was not read as two columns")
        text = "\n".join(out)
        for column in (0, self.GUTTER):
            fragments = [l[column:column + self.GUTTER].strip() for l in lines]
            places = [text.find(f) for f in fragments]
            self.assertNotIn(-1, places, f"a line of the column at {column} is missing")
            self.assertEqual(places, sorted(places), f"the column at {column} was reordered")


class FullWidthContent(unittest.TestCase):
    """A line that runs the width of a two-column page must not be cut at the gutter.

    Cutting one breaks a word and moves its end far from its start, which puts two lines beside
    each other that the paper never printed together. A quote can then be stitched across that
    seam with a `[...]`, which is what the reference permits between two lines, and the validator
    certifies a sentence the paper does not contain.
    """

    def page(self, middle):
        head = [f"{'':<56}TABLE II",
                f"{'':<40}C ATEGORIES OF T HINGS"]
        body = [f"{'Left column text that runs on and on here':<66}Right column text here"
                for _ in range(8)]
        return head + middle + [""] + body

    def test_a_two_column_table_is_found_as_one_block(self):
        """`_full_width_tables` has to return the span, or nothing above knows the table is there.

        Its rows have one cell gap, like body text, so the row test needs `_spans_with_cells`.
        """
        rows = [f"{'    Task Triggers':<30}Defines when and how the action runs for each request"
                for _ in range(5)]
        width = max(len(r) for r in rows)
        caption = f"{'':<{(width - 8) // 2}}TABLE II"
        lines = [caption] + rows
        self.assertEqual(pdf_text._full_width_tables(lines, 60, width), [(0, 6)],
                         "a two-column full-width table was not found")

    def test_a_two_column_table_row_is_not_cut(self):
        """Its rows have one cell gap, like body text, but they cross the gutter and body text does not."""
        rows = [f"{'    Task Triggers':<30}Defines when and how the action runs for each pull request"
                for _ in range(5)]
        out, regions, _ = pdf_text._layout(self.page(rows))
        self.assertTrue(regions, "the page was not read as two columns at all")
        whole = [l.strip() for l in out]
        for row in rows:
            self.assertIn(row.strip(), whole,
                          "a row of a full-width table was cut at the gutter")

    def test_a_centred_title_block_is_not_cut(self):
        centred = [f"{'':<20}A Title Of The Paper Goes Here",
                   f"{'':<41}Sebastian Baltes, Marc Cheong, Christoph Treude",
                   f"{'':<44}Heidelberg University, Germany"]
        out, regions, _ = pdf_text._layout(self.page(centred))
        whole = [l.strip() for l in out]
        for line in centred:
            self.assertIn(line.strip(), whole,
                          "a centred full-width line was cut at the gutter")

    def test_two_columns_of_body_text_are_still_split(self):
        """The guards must not stop the page being read in reading order."""
        out, regions, _ = pdf_text._layout(self.page([]))
        self.assertEqual(regions, 1)
        text = "\n".join(out)
        self.assertIn("Left column text that runs on and on here", text)
        self.assertLess(text.index("Right column text here"),
                        len(text), "the right column was dropped")
        self.assertGreater(text.count("Right column text here"), 0)


class GapWarnings(unittest.TestCase):
    """A `[...]` stands for a figure, a table, a footnote, or a page break, and nothing else."""

    PAGE = ("Caching reduced the median build time in our sample.\n{skipped}\n"
            "We now turn to the threats that this design cannot rule out.\n")
    QUOTE = ("Caching reduced the median build time in our sample. [...] "
             "We now turn to the threats that this design cannot rule out.")

    def warnings(self, skipped):
        page = self.PAGE.format(skipped=skipped)
        self.assertTrue(cea_claims.quote_on(self.QUOTE, page), "the fixture does not match")
        return "\n".join(cea_claims._sentence_warnings("X", self.QUOTE, page))

    def crossed(self, skipped):
        """The heading a `[...]` skips, which is refused rather than questioned."""
        page = self.PAGE.format(skipped=skipped)
        self.assertTrue(cea_claims.quote_on(self.QUOTE, page), "the fixture does not match")
        return cea_claims._skipped_heading(self.QUOTE, page)

    def test_a_gap_over_a_section_heading_is_refused(self):
        """It was a warning, and `render` and `site` do not print warnings and never stop for
        one, so a quote welded across a section boundary reached the published page."""
        for heading in ("V. DISCUSSION", "A. Data Collection", "IV. A NALYSES & R ESULTS",
                        "5.2. Results by project", "III-B. Coding Procedure"):
            with self.subTest(heading=heading):
                self.assertIsNotNone(self.crossed(heading),
                                     "a quote running across two sections went through")

    def test_a_heading_without_its_dot_is_still_a_heading(self):
        """How IEEE small caps come out of pdftotext. These matched the footnote test instead,
        which passed over the whole block, so the one shape the check exists for switched it off."""
        for heading in ("9     C ONCLUSION", "8 T HREATS TO VALIDITY", "7     R ELATED W ORK",
                        "2     M ETHODOLOGY", "3       R EASONS FOR M ENTIONING G EN AI TOOLS"):
            with self.subTest(heading=heading):
                self.assertEqual(self.crossed(heading), heading.strip())

    def test_a_footnote_line_does_not_silence_the_prose_behind_it(self):
        """One line opening with a digit passed over the whole block, so a footnote standing in
        front of a page of the paper's own prose hid all of it."""
        body = "\n".join(f"line {i} of running prose that the quote leaves out of its middle"
                         for i in range(4))
        page = self.PAGE.format(skipped="3 See the replication package for the full list.\n" + body)
        self.assertIsNotNone(cea_claims._skipped_prose(self.QUOTE, page))

    def test_a_footnote_on_its_own_is_still_what_the_marker_is_for(self):
        page = self.PAGE.format(skipped="3 See the replication package for the full list.")
        self.assertIsNone(cea_claims._skipped_prose(self.QUOTE, page))

    def test_a_caption_of_several_lines_is_still_passed_over(self):
        """A caption's continuation lines read as ordinary prose by every test available here,
        so judging what is left of a captioned block questions every multi-line caption."""
        page = self.PAGE.format(skipped="Fig. 3. Distribution of review comments across the four\n"
                                        "categories, with the share of each category per project")
        self.assertIsNone(cea_claims._skipped_prose(self.QUOTE, page))

    def test_a_gap_over_a_table_caption_is_not_called_a_heading(self):
        """"TABLE V" is a heading by shape and a table by meaning, and a table is what [...] is for."""
        for caption in ("TABLE V", "TABLE XVI", "Fig. 3", "Figure 12", "Algorithm 2"):
            with self.subTest(caption=caption):
                self.assertIsNone(self.crossed(caption))
                self.assertNotIn("skips the heading", self.warnings(caption))

    def test_the_paper_s_own_words_are_not_headings(self):
        for line in ("GPT", "LLM", "YES", "AI AI AI AI", "RQ1 RQ2 RQ3 RQ4"):
            with self.subTest(line=line):
                self.assertIsNone(self.crossed(line))

    def test_a_row_of_a_table_in_capitals_is_not_a_heading(self):
        """The no-dot pattern must not turn an all-capitals table row into a section."""
        for row in ("1     TOTAL     COUNT     SHARE", "2   GPT    CLAUDE    GEMINI    LLAMA"):
            with self.subTest(row=row):
                self.assertIsNone(self.crossed(row))


class WordBoundaries(unittest.TestCase):
    """Whitespace used to vanish entirely, so a quote could move a word boundary and still pass."""

    def test_a_quote_cannot_manufacture_a_negation(self):
        """"was not able" against a page saying "was notable" was certified as the paper's words."""
        self.assertFalse(cea_claims.quote_on("The difference was not able to be measured.",
                                             "The difference was notable to be measured."))

    def test_a_quote_cannot_close_a_gap_the_paper_leaves_open(self):
        """The same in reverse: "notable" is not what a paper saying "not able" said."""
        self.assertFalse(cea_claims.quote_on("The difference was notable.",
                                             "The difference was not able."))
        self.assertFalse(cea_claims.quote_on("an inadequate design", "an in adequate design"))

    def test_a_quote_cannot_split_a_word_the_paper_writes_whole(self):
        self.assertFalse(cea_claims.quote_on("Build fail ures are rare",
                                             "Build failures are rare in general."))

    def test_small_capitals_split_by_the_extraction_are_still_matched(self):
        """A caption set in small capitals extracts as "R EFINED D ATASET", one letter split off.

        The checker quotes the caption as the paper prints it, so the page's invented boundary is
        passed over. Only that shape: one character on a side, capitals on both.
        """
        self.assertTrue(cea_claims.quote_on(
            "REFINED DATASET FOR COMMENT ADDRESSING ANALYSIS",
            "RQ2: R EFINED DATASET FOR C OMMENT A DDRESSING A NALYSIS (N=5,652)"))

    def test_a_one_letter_word_is_not_a_split_word(self):
        """"I ran" is two words. Passing over every one-character boundary made it "Iran"."""
        self.assertFalse(cea_claims.quote_on("Iran the experiment twice.",
                                             "I ran the experiment twice."))
        self.assertFalse(cea_claims.quote_on("the nvalue was small", "the n value was small"))
        self.assertFalse(cea_claims.quote_on("aresult we did not expect",
                                             "a result we did not expect"))

    def test_a_word_broken_across_lines_keeps_matching(self):
        self.assertTrue(cea_claims.quote_on("a distinct result", "a dis-\ntinct result"))
        self.assertTrue(cea_claims.quote_on("joined across lines here", "joined across\nlines here"))

    def test_a_footnote_number_does_not_move_a_boundary(self):
        """"pollution.6 However" has to read as "pollution. However", number and all."""
        self.assertTrue(cea_claims.quote_on(
            "on social media and search engine pollution. However",
            "on social media5 and search engine pollution.6 However"))

    def test_a_quote_that_keeps_a_footnote_number_still_matches(self):
        """The reference lets a quote leave the number out. It does not require that.

        The boundary beside the number was being read off the characters that would remain once
        the number was gone, so the page carried no boundary there while a quote keeping the
        number carried one, and the sentence did not match itself.
        """
        self.assertTrue(cea_claims.quote_on("pollution.6 However", "pollution.6 However"))
        self.assertTrue(cea_claims.quote_on("pollution. However", "pollution.6 However"))
        self.assertTrue(cea_claims.quote_on(
            "on social media5 and search engine pollution.6 However",
            "on social media5 and search engine pollution.6 However"))
        self.assertTrue(cea_claims.quote_on(
            "checked every label across all 15 documents over four review rounds,16 changing 234",
            "checked every label across all 15 documents over four review rounds,16\nchanging 234"))

    def test_a_quote_cannot_run_two_numbers_together(self):
        """A merged table cell is a number the paper never printed, and nothing else reads wrong.

        Passing over a one-character boundary has to stop at digits: "2" and "30" in a table row
        would otherwise certify a minimum of "230".
        """
        self.assertFalse(cea_claims.quote_on("125", "12 5"))
        self.assertFalse(cea_claims.quote_on("we found 1234 defects", "we found 1 234 defects"))
        self.assertFalse(cea_claims.quote_on("0.53", "the score was 0.5 3 in the table"))
        self.assertFalse(cea_claims.quote_on(
            "# Contributors 230 15 475 49",
            "  # Contributors      2        30        15        475        49"))


class QuotesStartAndEndAtWords(unittest.TestCase):
    """A quote is the paper's own sentence, so it begins and ends where the paper's words do.

    The search is a substring search. Without a boundary at each end a quote can stop inside a
    page word, and the shortened word means something else: "we can" against a page that reads
    "we cannot" reverses the sentence and was certified.
    """

    def test_a_quote_cannot_stop_inside_a_word(self):
        for quote, page in (
                ("with our data and methodology, we can",
                 "Therefore, with our data and methodology, we cannot rule out the alternative."),
                ("the tools were used with", "the tools were used without any supervision."),
                ("we found that the tool is use",
                 "we found that the tool is useless for this task."),
                ("accuracy rose to 9", "accuracy rose to 91 percent overall.")):
            with self.subTest(quote=quote[-20:]):
                self.assertFalse(cea_claims.quote_on(quote, page))

    def test_a_quote_cannot_stop_at_a_word_the_paper_broke_across_lines(self):
        """The page keeps the hyphen and `_normalized` drops it, so the two halves are one word."""
        page = "We report that the difference was non-\nsignificant across all three tasks,"
        self.assertFalse(cea_claims.quote_on("We report that the difference was non", page))
        self.assertTrue(cea_claims.quote_on(
            "We report that the difference was nonsignificant across all three tasks,", page))

    def test_a_sentence_after_a_dash_still_begins_a_quote(self):
        """Folding drops the dash, so the two words read as one and the boundary is not there."""
        self.assertTrue(cea_claims.quote_on("Caching halves median build time.",
                                            "Abstract\u2014Caching halves median build time."))

    def test_a_gap_may_still_stand_inside_a_word(self):
        """The reference allows `dis[...]tinct` where a table interrupts the word."""
        page = "We found ten dis\nTABLE I  A caption here\ntinct codes in the data."
        self.assertTrue(cea_claims.quote_on("We found ten dis[...]tinct codes in the data.", page))


class PageMarkers(unittest.TestCase):
    """`=== page N ===` is the only structure text.txt has, so the paper must not write one."""

    def test_a_second_marker_for_one_page_adds_to_it(self):
        """It used to empty the page, losing text that is plainly in the file.

        The validator then told the checker to copy a quote exactly from a file where it already
        stood exactly, and the skill forbids editing text.txt, so there was no way out.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "text.txt"
            t.write_text("=== page 1 ===\nFirst.\n=== page 2 ===\nEarly on two.\n"
                         "=== page 2 ===\nLater on two.\n", encoding="utf-8")
            pages = cea_claims.load_pages(t)
        self.assertEqual(sorted(pages), [1, 2])
        self.assertIn("Early on two.", pages[2])
        self.assertIn("Later on two.", pages[2])

    def test_a_text_file_that_opens_one_page_twice_is_reported(self):
        """Adding to the page keeps the text, and a quote could then be stitched across the join.

        `extract` writes each page once, so a file that opens one twice was edited by hand, which
        the skill forbids. The checker is told rather than left with a page that reads wrongly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            (d / "text.txt").write_text(TEXT + "=== page 1 ===\nstitched on\n", encoding="utf-8")
            problems, _ = cea_claims.validate(d)
        self.assertIn("opens page 1 more than once", "\n".join(problems))

    def test_a_paper_that_prints_a_page_marker_cannot_open_a_page(self):
        """A body line of that exact shape used to start a page and swallow the real one."""
        import pdf_text

        class Page:
            def __init__(self, number, lines):
                self.number, self.lines, self.regions = number, lines, []

        class Extraction:
            def __init__(self, pages):
                self.pages = pages

        text = pdf_text.to_text(Extraction([Page(1, ["Intro."]),
                                            Page(2, ["=== page 7 ===", "Real page two."])]))
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp) / "text.txt"
            t.write_text(text, encoding="utf-8")
            pages = cea_claims.load_pages(t)
        self.assertEqual(sorted(pages), [1, 2], "the paper's own text opened a page")
        self.assertIn("Real page two.", pages[2])


class RealPapers(unittest.TestCase):
    NAMES = ("tse26-ai-code-review", "tse26-genai-usage", "ieeesw26-ai-slop")

    # Words the layout used to cut in half at the gutter, and the whole word each belongs to. The
    # transcript is what every published quote is checked against, so a word broken here puts two
    # lines beside each other that the paper never printed together, and a quote can be stitched
    # across the join with a [...] and certified.
    WHOLE_WORDS = {
        "ieeesw26-ai-slop": [("Marc Cheong", "Marc C"),
                             ("absurdist framing", "absurdist framin"),
                             ("sebastian.baltes@uni-heidelberg.de", "sebastian.baltes")],
        "tse26-ai-code-review": [("connecting to the LLM service", "connecting to"),
                                 ("Configuration Category", "Configuration Category")],
    }

    @classmethod
    def setUpClass(cls):
        cls.papers = {}
        if not shutil.which("pdftotext"):
            raise unittest.SkipTest("pdftotext is not installed")
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

    def test_no_word_is_cut_at_the_gutter(self):
        """Each of these was split across the page by the two-column layout."""
        for name, pairs in self.WHOLE_WORDS.items():
            pages = self.pages(name)
            lines = [l.rstrip() for n in sorted(pages) for l in pages[n].splitlines()]
            text = "\n".join(lines)
            for whole, broken in pairs:
                with self.subTest(paper=name, word=whole):
                    self.assertIn(whole, text, f"{whole!r} is not in the transcript whole")
                    # a cut shows as a line that stops in the middle of the word
                    self.assertEqual([l for l in lines if l.endswith(broken)], [],
                                     f"{broken!r} ends a line, so the word was cut at the gutter")

    def test_putting_the_columns_in_reading_order_changes_no_word(self):
        """The reordering may move a line. It may not break a word or drop one.

        `_layout` is the step that cuts a page at its gutter, so its output has to hold the same
        words as its input. A fixed list of quotes cannot see a word broken somewhere else on the
        page, and every repair to the layout was for exactly that.
        """
        import collections
        import subprocess
        for name in self.NAMES:
            if name not in self.papers:
                self.skipTest(f"{name}.pdf is not in evals/papers/")
            path = PAPERS / f"{name}.pdf"
            raw = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                 capture_output=True, text=True, check=True).stdout
            for number, chunk in enumerate(c for c in raw.split("\f") if c.strip()):
                lines = chunk.split("\n")
                with self.subTest(paper=name, page=number + 1):
                    out, _, _ = pdf_text._layout(lines)
                    self.assertEqual(collections.Counter(" ".join(lines).split()),
                                     collections.Counter(" ".join(out).split()),
                                     "the layout changed the words of the page")

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
        "format": cea_claims.FORMAT,
        "paper": {"id": "fixture", "title": "Fixture", "pdf": "fixture.pdf", "pages": 3},
        "broad_statements": [
            {"id": "B1", "quote": "Caching halves median build time.", "page": 1,
             "section": "Abstract", "source": "abstract"},
        ],
        "claims": [
            {"id": "C1", "quote": SPLIT_QUOTE,
             "states": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes.",
             "page": 2, "section": "5 Results", "serves": ["B1"], "split_from": "S1",
             "selection_reason": "B1 rests on this comparison."},
            {"id": "C2", "quote": SPLIT_QUOTE,
             "states": "Across the 48 projects, the failure rate stayed at 3%.",
             "page": 2, "section": "5 Results", "serves": ["B1"], "split_from": "S1",
             "selection_reason": "Needs different evidence than C1."},
        ],
        "rejected": [
            {"id": "R1", "quote": "We collected 1,203 builds from 48 projects.", "page": 2,
             "section": "4 Data", "reason": "Describes the data, not a result."},
        ],
    }


def unused_hooks(html, anchors=frozenset()):
    """The classes and ids a page emits that its own CSS, script and links never name.

    Returns the two sorted lists and how many hooks were looked at, so a caller can tell an
    empty result from a page it failed to scan.
    """
    style = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
    script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
    body = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
    # What counts as a use: a name inside one of the script's own string literals. A bare
    # literal is an argument like setupFilter('cand-search'), `#x`/`.x` come from selectors,
    # and class="x" from the markup the script writes itself. Every word in the file would
    # let a hook renamed to an ordinary word like `quote` or `section` pass unnoticed.
    used = set()
    for a, b in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", script):
        value = a or b
        if re.fullmatch(r"[\w-]+", value):
            used.add(value)
        used |= set(re.findall(r"[#.]([\w-]+)", value))
        used |= {c for m in re.findall(r'class="([^"]+)"', value) for c in m.split()}
    linked = set(re.findall(r'href="[^"]*#([\w-]+)"', body))
    classes = {c for m in re.findall(r'class="([^"]*)"', body) for c in m.split()}
    ids = set(re.findall(r'\sid="([^"]+)"', body))
    return (sorted(classes - set(re.findall(r"\.([A-Za-z][\w-]*)", style)) - used),
            sorted(ids - set(re.findall(r"#([\w-]+)", style)) - used - linked - set(anchors)),
            len(classes) + len(ids))


def _a_candidate_weighed_against_a_result(d):
    """A rejected candidate that names a broad statement hangs off that result in the map."""
    d["rejected"][0]["duplicate_of"] = "B1"
    d["rejected"][0]["reason"] = "B1 would still stand, because it repeats B1."


def _a_result_with_a_note(d):
    """A result no claim serves stands only where its note says why, and the page prints both."""
    d["claims"] = []
    d["broad_statements"][0]["states"] = "Whether caching helps."
    d["broad_statements"][0]["note"] = "The paper reports no comparison."


def _two_statements_of_one_result(d):
    """Two broad statements that the same claims serve state one result, and the map groups them."""
    d["broad_statements"].append({"id": "B2", "quote": "Build time is cut in half by caching.",
                                  "page": 1, "section": "1 Introduction", "source": "contributions"})
    for c in d["claims"]:
        c["serves"] = ["B1", "B2"]


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

    def refusals(self, data, text=TEXT):
        """What `validate` refuses. A `[...]` over the paper's own prose is a problem now, not a
        warning, because render and site print no warnings and stop for none."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(text, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return "\n".join(cea_claims.validate(d)[0])

    def test_a_quote_with_many_gaps_does_not_blow_up(self):
        """The gap search re-explored the same position once per path: exponential in the gaps.

        Unattended, a hang in `validate` produces no output at all, which is the worst failure
        this tool has. The bound is generous; before memoisation this took over twenty seconds.
        """
        import time
        page = "\n".join(["the same repeated phrase here"] * 50)
        quote = " [...] ".join(["the same repeated phrase here"] * 7) + " never matches"
        started = time.monotonic()
        with bounded():
            self.assertIsNone(cea_claims.find_quote(quote, page))
        self.assertLess(time.monotonic() - started, 5.0, "the gap search must not blow up")

    def test_ten_or_more_results_order_numerically_not_as_strings(self):
        """B10 sorted before B2 as a string, while the page sorts them numerically."""
        data = valid_claims()
        base = data["broad_statements"][0]
        data["broad_statements"] = [dict(base, id=f"B{n}") for n in range(1, 13)]
        for claim in data["claims"]:
            claim["serves"] = ["B1"]
        order = [b["id"] for b in cea_claims._by_weight(data)]
        tail = [i for i in order if i != "B1"]
        self.assertEqual(tail, [f"B{n}" for n in range(2, 13)],
                         f"results must order numerically, got {order}")

    def test_a_rejected_part_without_its_words_is_refused_not_a_crash(self):
        """`states` is optional on a rejected candidate, so the gate passed it to a KeyError."""
        import cea_site
        data = valid_claims()
        data["rejected"][0]["split_from"] = "S1"
        cea_claims.render(data)  # must not raise
        self.assertIn("states", self.check(data))
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
        self.assertEqual(written, 0)
        self.assertIn("rejected[0].states", "\n".join(messages))

    def test_an_entry_page_must_be_a_number_or_a_range(self):
        """The gate took any string, so markup in `page` reached the page's attributes."""
        import cea_site
        for bad in ('2"><b>INJ</b><x y="', "abc", "1_0", ""):
            with self.subTest(page=repr(bad)):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = Path(tmp) / "rec"
                    rec.mkdir()
                    data = valid_claims()
                    data["claims"][0]["page"] = bad
                    (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                    (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)
                    self.assertIn("page must be", "\n".join(messages))
        for good in (3, "8-9", "12"):
            with self.subTest(page=repr(good)):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = Path(tmp) / "rec"
                    rec.mkdir()
                    data = valid_claims()
                    data["claims"][0]["page"] = good
                    (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                    (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        _written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertNotIn("page must be", "\n".join(messages),
                                     "the gate must not refuse what validate allows")

    def test_a_non_list_serves_is_reported_not_raised(self):
        """validate raised a TypeError where the skill tells the agent to read a marker."""
        for bad in (1, True, 1.5, "B1"):
            with self.subTest(serves=repr(bad)):
                data = valid_claims()
                data["claims"][0]["serves"] = bad
                self.assertIn("serves", self.check(data))

    def test_duplicate_of_rejects_a_repeated_id(self):
        data = valid_claims()
        data["rejected"][0]["duplicate_of"] = ["C1", "C1"]
        self.assertIn("is listed twice", self.check(data))

    def test_breaks_down_rejects_a_repeated_id(self):
        data = valid_claims()
        data["rejected"][0]["breaks_down"] = ["B1", "B1"]
        self.assertIn("is listed twice", self.check(data))

    def test_paper_id_must_be_one_path_segment(self):
        """site names a directory after it, so `..` would write outside --out."""
        for bad in ("../../escaped", "a/b", ".", " spaced"):
            with self.subTest(id=bad):
                data = valid_claims()
                data["paper"]["id"] = bad
                self.assertIn("paper.id", self.check(data))

    def test_a_broad_statement_states_must_not_be_empty(self):
        data = valid_claims()
        data["broad_statements"][0]["states"] = ""
        self.assertIn("states", self.check(data))

    def test_valid_record_passes(self):
        self.assertEqual(self.check(valid_claims()), "")

    def test_statements_serving_one_result_are_named(self):
        data = valid_claims()
        data["broad_statements"] += [
            {"id": "B2", "quote": "Caching halves median build time.", "page": 1,
             "section": "1 Introduction", "source": "contributions"},
            {"id": "B3", "quote": "Caching halves median build time.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"}]
        for c in data["claims"]:
            c["serves"] = ["B1", "B2", "B3"]
        self.assertIn("broad_statements B1, B2, B3", self.warnings(data))

    def test_one_result_is_rendered_once(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Caching halves median build time.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        for c in data["claims"]:
            c["serves"] = ["B1", "B2"]
        md = cea_claims.render(data)
        self.assertIn("also stated as B2", md)
        self.assertEqual(md.count("1 of 2 for this result"), 2)

    def test_a_claim_serving_two_results_says_so(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        # Two claims of their own keep B2 a result apart from B1, rather than a restatement of it.
        data["claims"] += [
            {"id": "C3", "quote": "Only 3 of the 48 builds failed.", "states": "Only 3 of the 48 builds failed.",
             "page": 3, "section": "5 Results", "serves": ["B2"], "split_from": None,
             "selection_reason": "B2 says failures are rare."},
            {"id": "C4", "quote": "No project saw its failure rate rise.", "states": "No project saw its failure rate rise.",
             "page": 3, "section": "5 Results", "serves": ["B2"], "split_from": None,
             "selection_reason": "B2 rests on this as well."}]
        data["claims"][0]["serves"] = ["B1", "B2"]
        md = cea_claims.render(data).split("### Every claim")[0]
        self.assertIn("and of 1 other result", md)
        self.assertIn("1 of 3 for this result", md)

    def test_a_statement_whose_claims_all_serve_another_is_a_breakdown(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Caching helps most on the largest projects.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        data["claims"][0]["serves"] = ["B1", "B2"]
        self.assertIn("may break that result down", self.warnings(data))
        # The record puts no order on the statements, so neither does the warning.
        data["broad_statements"].reverse()
        self.assertIn("may break that result down", self.warnings(data))
        data["broad_statements"].reverse()
        # A finding of its own brings a claim of its own.
        data["claims"].append(
            {"id": "C3", "quote": "The largest projects saved 6.1 minutes.", "states": "The largest projects saved 6.1 minutes.",
             "page": 3, "section": "5 Results", "serves": ["B2"], "split_from": None,
             "selection_reason": "B2 rests on this."})
        self.assertNotIn("may break that result down", self.warnings(data))

    def test_a_chain_of_breakdowns_names_the_statement_that_survives(self):
        data = valid_claims()
        data["broad_statements"] += [
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"},
            {"id": "B3", "quote": "We collected 1,203 builds from 48 projects.", "page": 2,
             "section": "4 Data", "source": "other", "note": "No summary sentence states it."}]
        # B3 is served by C1 alone, B2 by C1 and C2, B1 by C1, C2 and C3.
        data["claims"].append(
            {"id": "C3", "quote": "Build failures are rare in general.",
             "states": "Build failures are rare in general.", "page": 3, "section": "5 Results",
             "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on this."})
        data["claims"][0]["serves"] = ["B1", "B2", "B3"]
        data["claims"][1]["serves"] = ["B1", "B2"]
        warned = [w for w in self.warnings(data).splitlines() if "may break that result down" in w]
        self.assertEqual(len(warned), 2)
        # Both point at B1, which survives, not at each other.
        self.assertTrue(all("naming B1" in w for w in warned), warned)

    def test_a_reason_keeps_its_opening_word_when_it_names_a_ground(self):
        data = valid_claims()
        for reason in ("Key numbers here come from cited work, not from this study.",
                       "Significant only as a sample size, so it describes the study.",
                       "Minor rounding differences in the table describe the study, not a result.",
                       "Key numbers here come from cited work.",
                       "Central to the coding procedure, an agreement score.",
                       "Main sizes of the corpus, not a result.",
                       # A reason that runs on has said something, whatever word it opens with.
                       "An important result that the reader should certainly bear in mind here."):
            data["rejected"][0]["reason"] = reason
            self.assertNotIn("gives no ground", self.warnings(data), reason)
        for reason in ("Important result.", "Not an important number.", "Minor.", "No",
                       "Important result for the study.", "A key number of the paper.",
                       "Important result that stands out in the discussion.",
                       "A notable figure about the participants."):
            data["rejected"][0]["reason"] = reason
            self.assertIn("gives no ground", self.warnings(data), reason)

    def test_a_reason_that_names_a_statement_keeps_the_phrase(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = "B1 would still stand, because no main result depends on this subgroup."
        self.assertNotIn("is the selection question answered no", self.warnings(data))
        data["rejected"][0]["reason"] = "No main result depends on this subgroup."
        self.assertIn("is the selection question answered no", self.warnings(data))

    def test_a_chain_of_overlaps_does_not_become_one_result(self):
        # Neighbours share two claims of three, but B1 and B4 share none, so nothing groups.
        support = {"B1": ["C1", "C2", "C3"], "B2": ["C2", "C3", "C4"],
                   "B3": ["C3", "C4", "C5"], "B4": ["C4", "C5", "C6"]}
        every = sorted({c for cs in support.values() for c in cs})
        for order in itertools.permutations(support):
            data = {"broad_statements": [{"id": b} for b in order],
                    "claims": [{"id": c, "serves": [b for b in support if c in support[b]]}
                               for c in every]}
            groups = sorted(sorted(g) for g in cea_claims._one_result(data))
            self.assertEqual(groups, [["B1"], ["B2"], ["B3"], ["B4"]], order)

    def test_two_statements_with_no_claims_are_not_one_result(self):
        data = valid_claims()
        data["broad_statements"] += [
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion",
             "note": "The paper gives no result for it."},
            {"id": "B3", "quote": "We collected 1,203 builds from 48 projects.", "page": 2,
             "section": "4 Data", "source": "other", "note": "No summary sentence states it."}]
        self.assertNotIn("the same claims serve all of them", self.warnings(data))

    def test_statements_sharing_most_of_their_claims_are_asked_about(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        for c in data["claims"]:
            c["serves"] = ["B1", "B2"]
        data["claims"] += [
            {"id": "C3", "quote": "We collected 1,203 builds from 48 projects.",
             "states": "We collected 1,203 builds from 48 projects.", "page": 2, "section": "5 Results",
             "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on this."},
            {"id": "C4", "quote": "Build failures are rare in general.",
             "states": "Build failures are rare in general.", "page": 3, "section": "5 Results",
             "serves": ["B2"], "split_from": None, "selection_reason": "B2 rests on this."}]
        # B1 has C1, C2, C3 and B2 has C1, C2, C4: half of four shared, so a question, not a verdict.
        self.assertIn("broad_statements B1 and B2: most of the claims", self.warnings(data))
        self.assertNotIn("the same claims serve all of them", self.warnings(data))
        data["broad_statements"].reverse()
        self.assertIn("most of the claims", self.warnings(data))

    def test_breaks_down_on_a_broken_record_reports_rather_than_raises(self):
        data = valid_claims()
        del data["broad_statements"][0]["id"]
        data["rejected"][0]["breaks_down"] = ["B1"]
        self.assertIn("is not a broad statement id", self.check(data))

    def test_breaks_down_and_duplicate_of_name_different_things(self):
        data = valid_claims()
        data["rejected"][0]["duplicate_of"] = ["B1"]
        data["rejected"][0]["breaks_down"] = ["B1"]
        self.assertIn("a sentence either repeats a result or breaks it down", self.check(data))

    def test_a_breakdown_still_needs_a_ground(self):
        """Naming the statement a candidate divides says what the candidate is, not on what
        ground it is not a claim. The exemption meant a two-word verdict was published under the
        page's own heading "Breaks a main result into parts" with nothing a checker could read."""
        data = valid_claims()
        data["rejected"][0]["breaks_down"] = ["B1"]
        data["rejected"][0]["reason"] = "A breakdown."
        self.assertIn("gives no ground", self.warnings(data))

    def test_a_breakdown_with_a_ground_is_accepted(self):
        data = valid_claims()
        data["rejected"][0]["breaks_down"] = ["B1"]
        data["rejected"][0]["reason"] = ("It divides B1's median into the two project groups, so "
                                         "B1 still stands and this is one part of it.")
        self.assertNotIn("gives no ground", self.warnings(data))

    def test_a_repetition_names_the_statement_behind_the_claim(self):
        data = valid_claims()
        data["rejected"].append(
            {"id": "R2", "quote": "As Section 5.1 showed, caching cut median build time.",
             "page": 3, "section": "6 Discussion", "duplicate_of": ["C1"], "reason": "Repeats C1."})
        self.assertIn("name the broad statement as well", self.warnings(data))
        data["rejected"][-1]["duplicate_of"] = ["C1", "B1"]
        self.assertNotIn("name the broad statement as well", self.warnings(data))
        # A sentence repeating a number no main result rests on names no statement.
        data["rejected"][-1]["duplicate_of"] = ["R1"]
        self.assertNotIn("name the broad statement as well", self.warnings(data))

    def test_the_script_adds_up_the_parts_a_note_names(self):
        data = valid_claims()
        data["rejected"][0]["note"] = ("Table 2 (page 2) prints the parts of the sentence's 1,203 and no "
                                       "total: 700 and 503.")
        self.assertNotIn("add up to", self.warnings(data))
        # The same note with a row that does not belong to the total.
        data["rejected"][0]["note"] = ("Table 2 (page 2) prints the parts of the sentence's 1,203 and no "
                                       "total: 700 and 403.")
        self.assertIn("add up to 1103", self.warnings(data))
        # Percentages beside the counts are not parts.
        data["rejected"][0]["note"] = ("Table 2 (page 2) prints the parts of the sentence's 1,203 and no "
                                       "total: 700 (58.2%) and 503 (41.8%).")
        self.assertNotIn("add up to", self.warnings(data))

    def test_a_heading_copied_whole_is_reported_once(self):
        data = valid_claims()
        # A long heading is what the rule asks for as long as it is a heading, not a question.
        data["claims"][0]["section"] = ("V-B Beyond Code Changes: Impact on Closed PRs and Feedback "
                                        "on Unaddressed Comments")
        self.assertNotIn("copied whole", self.warnings(data))
        whole = ("IV-A RQ1: How are LLM-based code review actions adopted in GitHub repositories?, "
                 "Results")
        data["claims"][0]["section"] = data["claims"][1]["section"] = whole
        warned = [w for w in self.warnings(data).splitlines() if "copied whole" in w]
        self.assertEqual(len(warned), 1)
        self.assertIn("1 heading is copied whole into 2 sections", warned[0])

    def test_breaks_down_names_a_broad_statement(self):
        data = valid_claims()
        data["rejected"][0]["breaks_down"] = ["C1"]
        self.assertIn("is not a broad statement id", self.check(data))
        data["rejected"][0]["breaks_down"] = ["B1"]
        self.assertEqual(self.check(data), "")
        self.assertIn("- Breaks down: B1", cea_claims.render(data))

    def test_a_breakdown_is_not_a_place_that_states_the_result(self):
        data = valid_claims()
        data["rejected"][0]["duplicate_of"] = ["B1"]
        self.assertEqual(cea_claims._stated_in(data), 2)
        del data["rejected"][0]["duplicate_of"]
        data["rejected"][0]["breaks_down"] = ["B1"]
        self.assertEqual(cea_claims._stated_in(data), 1)

    def test_grouping_depends_on_neither_the_order_nor_the_ids(self):
        # Two statements the same claims serve, and a third on its own.
        shapes = [["C1", "C2"], ["C1", "C2"], ["C3"]]
        seen = set()
        for names in itertools.permutations(["B1", "B2", "B3"]):
            serves: dict[str, list[str]] = {}
            for name, shape in zip(names, shapes):
                for c in shape:
                    serves.setdefault(c, []).append(name)
            data = {"broad_statements": [{"id": b} for b in names],
                    "claims": [{"id": c, "serves": s} for c, s in sorted(serves.items())]}
            groups = cea_claims._one_result(data)
            self.assertEqual(sorted(len(g) for g in groups), [1, 2], names)
            # The same partition, however the statements are named and listed.
            shape_of = dict(zip(names, (frozenset(s) for s in shapes)))
            seen.add(frozenset(frozenset(shape_of[b] for b in g) for g in groups))
        self.assertEqual(len(seen), 1)

    def test_boxed_answer_takes_the_rq_answer_source(self):
        data = valid_claims()
        data["broad_statements"][0]["section"] = "5 Results, Summary RQ1"
        self.assertIn("the source is rq_answer", self.warnings(data))
        data["broad_statements"][0]["source"] = "rq_answer"
        self.assertNotIn("the source is rq_answer", self.warnings(data))

    def test_reason_naming_a_ground_needs_no_broad_statement(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = "Describes the study, not a result."
        self.assertNotIn("gives no ground", self.warnings(data))
        # A ground the script has no word for is a ground all the same.
        data["rejected"][0]["reason"] = "It defines the metric rather than measuring anything."
        self.assertNotIn("gives no ground", self.warnings(data))

    def test_reason_naming_neither_a_result_nor_a_ground_warns(self):
        data = valid_claims()
        for verdict in ("Not an important number.", "Important result.", "Minor.", "No"):
            with self.subTest(reason=verdict):
                data["rejected"][0]["reason"] = verdict
                self.assertIn("gives no ground", self.warnings(data))

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
        table = "Table 5.  Row with cells   12   34   56\n"
        page = "We found ten dis-\n" + table * 100 + "tinct codes in the data."
        self.assertTrue(cea_claims.quote_on("We found ten dis[...]tinct codes in the data.", page))
        far = "We found ten dis-\n" + table * 200 + "tinct codes in the data."
        self.assertFalse(cea_claims.quote_on("We found ten dis[...]tinct codes in the data.", far))

    def test_split_part_adds_words(self):
        data = valid_claims()
        data["claims"][1]["states"] = "Across the 48 projects, the failure rate stayed low at 3%."
        self.assertIn("uses words that are not in the quote: low", self.check(data))

    def test_two_claims_with_the_same_quote(self):
        data = valid_claims()
        quote = "We collected 1,203 builds from 48 projects."
        data["rejected"] = []
        data["claims"] += [{"id": f"C{n}", "quote": quote, "states": quote, "page": 2, "section": "4 Data",
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
        data["claims"][1]["states"] = ["median"]
        out = self.check(data)
        self.assertIn("must list ids as strings", out)
        self.assertIn("(C2).quote: must be a non-empty string", out)

    def test_not_only_is_not_a_negation(self):
        quote = "Remote caching not only cut median build time by 55% but also reduced flaky failures by 12%."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "states": "Remote caching cut median build time by 55%.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "states": "Remote caching also reduced flaky failures by 12%.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        self.assertEqual(self.check(data, TEXT + quote + "\n"), "")

    def test_boundary_warnings(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "We saw 13% more cache hits, and builds got faster.\nIn total, 48 projects were studied, as listed, "
                "respectively.\nThe rate was non-\nsignificant for 12 of the 48 projects.\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["claims"] = [
            {"id": "C1", "quote": "3% more cache hits", "states": "3% more cache hits", "page": 2, "section": "5",
             "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C2", "quote": "In total, 48 projects were stud", "states": "In total, 48 projects were stud", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C3", "quote": "We saw 13% more cache hits", "states": "We saw 13% more cache hits", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": "significant for 12 of the 48 projects.", "states": "significant for 12 of the 48 projects.",
             "page": 2, "section": "5", "serves": ["B1"], "split_from": None, "selection_reason": "B1 rests on it."}]
        data["rejected"] = [
            {"id": "R1", "quote": "In total, 48 projects were studied, as listed, respectively.", "states": "48 projects were studied",
             "page": 2, "section": "5", "split_from": "S1", "reason": "Describes the study."},
            {"id": "R2", "quote": "In total, 48 projects were studied, as listed, respectively.", "states": "as listed",
             "page": 2, "section": "5", "split_from": "S1", "reason": "Describes the study."}]
        # A quote that begins or ends inside a word of the page is no longer found there at all:
        # "3% more cache hits" against a page reading "13% more cache hits" is a different number,
        # and a warning on a published record is too late. C4 still warns, because the page breaks
        # its word at a line end and the quote takes the second half.
        problems = self.check(data, text)
        for entry in ("C1", "C2"):
            with self.subTest(claim=entry):
                self.assertIn(f"({entry}).quote: not found in text.txt", problems)
        out = self.warnings(data, text)
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
        self.assertIn("reads as the paper's own running text", self.refusals(data, text))

    def test_gap_that_skips_part_of_the_sentence_is_flagged(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\n"
                "The failure rate increased for 12 of the 48\nprojects that did not use remote caching, while it\n"
                "decreased for the other 36 projects.\nTable 2: Failure rates per project group\n=== page 3 ===\nEnd.\n")
        data = valid_claims()
        data["rejected"] = [{"id": "R1", "quote": "The failure rate increased for 12 of the 48 [...] decreased for the other 36 projects.",
                             "page": 2, "section": "5", "reason": "No main result depends on it."}]
        self.assertIn('the [...] skips "projects that did not use remote caching, while it"',
                      self.refusals(data, text))

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
        data["claims"].append({"id": "C3", "quote": quote, "states": quote, "page": 2, "section": "4 Data",
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
            {"id": "R2", "quote": SPLIT_QUOTE, "states": "median build time fell from 9.2 to 4.1 minutes",
             "page": 2, "section": "5 Results", "split_from": "S2", "reason": "Not selected."},
            {"id": "R3", "quote": SPLIT_QUOTE, "states": "the failure rate stayed at 3%",
             "page": 2, "section": "5 Results", "split_from": "S2", "reason": "Not selected."}]
        self.assertIn("split_from 'S1' and 'S2' quote the same sentence", self.check(data))

    def test_warnings_for_a_split_that_moves_a_negation_or_drops_a_comparison(self):
        quote = "No project got slower, and the failure rate stayed lower than 3%."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "states": "project got slower", "page": 3, "section": "6",
             "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "states": "No failure rate stayed at 3%", "page": 3, "section": "6",
             "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        out = self.warnings(data, TEXT + quote + "\n")
        self.assertIn('no part keeps "no project" from the quote', out)
        self.assertIn("no part keeps lower, than from the quote", out)

    def test_claim_quoting_a_broad_statement_must_serve_it(self):
        data = valid_claims()
        quote = "Build failures are rare in general."
        data["broad_statements"].append({"id": "B2", "quote": quote, "page": 3, "section": "6", "source": "conclusion"})
        data["claims"].append({"id": "C3", "quote": quote, "states": quote, "page": 3, "section": "6",
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

    def test_states_on_a_rejected_candidate_without_split(self):
        data = valid_claims()
        data["rejected"][0]["states"] = "Something else."
        self.assertIn("only a rejected part of a split statement has words of its own", self.check(data))

    def test_split_part_repeats_the_whole_quote(self):
        data = valid_claims()
        data["claims"][1]["states"] = SPLIT_QUOTE
        self.assertIn("repeats the whole quote", self.check(data))

    def test_split_parts_drop_the_negation(self):
        quote = "Caching did not change the failure rate, and it cut build time by half."
        data = valid_claims()
        data["claims"] += [
            {"id": "C3", "quote": quote, "states": "Caching did change the failure rate.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."},
            {"id": "C4", "quote": quote, "states": "Caching cut build time by half.", "page": 3,
             "section": "6", "serves": ["B1"], "split_from": "S2", "selection_reason": "B1 rests on it."}]
        self.assertIn("contains a negation (not) that no part keeps", self.warnings(data, TEXT + quote + "\n"))

    def test_split_part_of_a_quote_with_a_gap_inside_a_word(self):
        text = ("=== page 1 ===\nCaching halves median build time.\n=== page 2 ===\nWe found ten dis-\n"
                "Table 5.  Row with cells   12   34   56\ntinct codes and 4 themes in the data.\n")
        quote = "We found ten dis[...]tinct codes and 4 themes in the data."
        data = valid_claims()
        data["paper"]["pages"] = 2
        data["rejected"] = []
        data["claims"] = [
            {"id": "C1", "quote": quote, "states": "We found ten distinct codes in the data.", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on it."},
            {"id": "C2", "quote": quote, "states": "We found 4 themes in the data.", "page": 2,
             "section": "5", "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on it."}]
        self.assertEqual(self.check(data, text), "")

    def test_warnings(self):
        data = valid_claims()
        data["rejected"][0]["quote"] = "1,203 builds from 48 projects."
        data["claims"][0]["states"] = "Across the 48 projects, median build time fell from 4.1 to 9.2 minutes."
        out = self.warnings(data)
        self.assertIn("(R1).quote: starts in the middle of a sentence", out)
        self.assertIn("(C1).states: gives its numbers (48, 4.1, 9.2) in a different order", out)

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
        page = "We found ten codes\nTable 5.  Row with cells   12   34   56\nin the data."
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
                                            "We found ten codes\nTable 5.  Row with cells   12   34   56\nin the data."))


class PageScript(unittest.TestCase):
    """Run the published page's own JavaScript.

    Nothing else in the suite executes it: the other tests read `cea_page.TEMPLATE` as text. That
    is how a page with no narrow claims came to throw before it installed the candidate filter and
    the navigation, on the one kind of paper where the rejected candidates are the whole content.
    """

    SHIM = r"""// A DOM small enough to read, big enough that the page script's loops actually run.
// Elements come from the emitted markup, so a selector that matches nothing returns nothing,
// exactly as it would in a browser, and every forEach body is executed at least once.
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
// keep the opening tags: the record travels in a <script id="cea-data"> the page reads back
const body = html.replace(/(<(style|script)\b[^>]*>)[\s\S]*?<\/\2>/g, '$1');

function parseAttrs(raw) {
  const at = {};
  for (const m of raw.matchAll(/([\w:.-]+)\s*=\s*"([^"]*)"/g)) at[m[1]] = m[2];
  return at;
}
const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
const ELEMENTS = [];
// A stack, so an element knows its parent: the page opens a card by its header's parentElement,
// and `closest` walks upwards. A flat list answers both wrongly.
{
  const stack = [];
  for (const m of body.matchAll(/<(\/?)([a-zA-Z][\w-]*)\b([^>]*?)(\/?)>/g)) {
    const [, close, tag, attrs, selfClose] = m;
    if (close) {
      for (let i = stack.length - 1; i >= 0; i--) {
        if (stack[i].nodeName === tag.toUpperCase()) { stack.length = i; break; }
      }
      continue;
    }
    const el = make(tag, parseAttrs(attrs));
    const parent = stack[stack.length - 1] || null;
    if (parent) { parent.children.push(el); el.parentNode = parent; }
    ELEMENTS.push(el);
    if (!selfClose && !VOID.has(tag.toLowerCase())) stack.push(el);
  }
}
const ids = new Set(ELEMENTS.filter(e => e.id).map(e => e.id));
const did = { listeners: 0, classed: 0, created: 0, fired: 0, badSelectors: [] };
const wired = new Set();
const bound = [];

const PSEUDO = new Set(['not', 'hover', 'focus', 'focus-visible', 'active', 'visited', 'target',
  'root', 'empty', 'checked', 'disabled', 'enabled', 'first-child', 'last-child', 'only-child',
  'nth-child', 'nth-of-type', 'first-of-type', 'last-of-type', 'before', 'after', 'placeholder']);
function matchOne(el, part) {
  for (const m of part.matchAll(/::?([\w-]+)/g)) {
    if (!PSEUDO.has(m[1])) throw new SyntaxError("'" + part + "' is not a valid selector");
  }
  const nots = [...part.matchAll(/:not\(([^)]*)\)/g)].map(x => x[1]);
  let rest = part.replace(/:not\([^)]*\)/g, '').replace(/::?[a-z-]+(\([^)]*\))?/g, '');
  for (const n of nots) if (matchOne(el, n)) return false;
  for (const a of rest.matchAll(/\[([\w:.-]+)(?:([~^$*|]?=)"([^"]*)")?\]/g)) {
    const v = el.getAttribute(a[1]);
    if (v === null) return false;
    if (a[2] === '=' && v !== a[3]) return false;
  }
  rest = rest.replace(/\[[^\]]*\]/g, '');
  if (!/^\s*(?:\*|[a-zA-Z][\w-]*)?(?:[#.][\w-]+)*\s*$/.test(rest) || !/[\w*]/.test(rest + part)) {
    throw new SyntaxError("'" + part + "' is not a valid selector");
  }
  const tag = /^[a-zA-Z][\w-]*/.exec(rest);
  if (tag && el.nodeName !== tag[0].toUpperCase()) return false;
  for (const c of rest.matchAll(/\.([\w-]+)/g)) if (!el.classList.contains(c[1])) return false;
  for (const i of rest.matchAll(/#([\w-]+)/g)) if (el.id !== i[1]) return false;
  return true;
}
// A descendant selector is matched right to left, each step climbing the parent chain.
function matches(el, sel) {
  const parts = sel.trim().split(/\s+/).filter(Boolean);
  if (!parts.length || parts[parts.length - 1] === '>') throw new SyntaxError(sel);
  if (!matchOne(el, parts.pop())) return false;
  let node = el.parentNode;
  while (parts.length) {
    const want = parts.pop();
    if (want === '>') {                  // the parent itself has to match, not some ancestor
      const next = parts.pop();
      if (!next || next === '>') throw new SyntaxError(sel);
      if (!node || !matchOne(node, next)) return false;
    } else {
      while (node && !matchOne(node, want)) node = node.parentNode;
      if (!node) return false;
    }
    node = node.parentNode;
  }
  return true;
}
// An index by class and id, so a query looks at a handful of elements rather than all of them.
// Class changes make it stale, so every classList change bumps a counter that rebuilds it.
let indexAt = -1;
let byClass = new Map();
function index() {
  if (indexAt === did.classed) return byClass;
  byClass = new Map();
  for (const e of ELEMENTS) {
    for (const c of e.classes()) {
      if (!byClass.has(c)) byClass.set(c, []);
      byClass.get(c).push(e);
    }
  }
  indexAt = did.classed;
  return byClass;
}
function candidates(part) {
  const id = /#([\w-]+)/.exec(part);
  if (id) { const e = ELEMENTS.find(x => x.id === id[1]); return e ? [e] : []; }
  const cls = /\.([\w-]+)/.exec(part.replace(/:not\([^)]*\)/g, ''));
  if (cls) return index().get(cls[1]) || [];
  return ELEMENTS;
}
const asked = new Map();
const matchAll = (sel) => {
  if (typeof sel !== 'string' || !sel.trim()) { did.badSelectors.push(String(sel)); return []; }
  const hits = sel.split(',').flatMap(s => {
    if (!s.trim()) return [];
    const last = s.trim().split(/\s+/).filter(x => x && x !== '>').pop();
    return candidates(last).filter(e => matches(e, s));
  });
  asked.set(sel, Math.max(asked.get(sel) || 0, hits.length));
  return hits;
};

function make(tag, attrs) {
  const cls = new Set((attrs.class || '').split(/\s+/).filter(Boolean));
  const data = {};
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('data-')) data[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  }
  const el = {
    nodeName: tag.toUpperCase(), tagName: tag.toUpperCase(), id: attrs.id || '', dataset: data, attrs,
    value: attrs.value || '', checked: false, type: attrs.type || '', href: attrs.href || '',
    offsetWidth: 100, offsetHeight: 20, offsetTop: 0, offsetLeft: 0, scrollTop: 0,
    style: {}, children: [], parentNode: null, textContent: '', innerHTML: '',
    get parentElement() { return this.parentNode; },
    classList: {
      add: (...c) => { did.classed++; c.forEach(x => cls.add(x)); },
      remove: (...c) => c.forEach(x => cls.delete(x)),
      toggle: (c, f) => did.classed++ || (f === undefined ? (cls.has(c) ? cls.delete(c) : cls.add(c)) : (f ? cls.add(c) : cls.delete(c))),
      contains: (c) => cls.has(c),
    },
    classes: () => cls,
    getAttribute: (k) => (k in attrs ? attrs[k] : null),
    setAttribute: (k, v) => { attrs[k] = String(v); if (k === 'class') { cls.clear(); String(v).split(/\s+/).filter(Boolean).forEach(x => cls.add(x)); } },
    removeAttribute: (k) => { delete attrs[k]; },
    addEventListener(t, fn){ did.listeners++; wired.add(this); bound.push({ el: this, type: t, fn: fn }); },
    removeEventListener(){},
    appendChild(c){ did.created++; this.children.push(c); c.parentNode = this; return c; },
    removeChild(c){
      const at = this.children.indexOf(c);
      if (at < 0) throw new Error('removeChild: node is not a child of this node');
      this.children.splice(at, 1); c.parentNode = null; return c;
    },
    get firstChild(){ return this.children[0] || null; },
    insertBefore(c){ return c; }, remove(){}, cloneNode(){ return make(tag, {...attrs}); },
    getBoundingClientRect: () => ({ width: 100, height: 20, top: 0, left: 0, right: 100, bottom: 20 }),
    scrollIntoView(){}, focus(){}, blur(){}, click(){},
    querySelector: (s) => matchAll(s).find(e => e !== el && el.contains(e)) || null,
    querySelectorAll: (s) => matchAll(s).filter(e => e !== el && el.contains(e)),
    closest: (s) => { const all = matchAll(s); let n = el; while (n) { if (all.includes(n)) return n; n = n.parentNode; } return null; },
    contains: (other) => { let n = other; while (n) { if (n === el) return true; n = n.parentNode; } return false; },
    getContext: () => ({ clearRect(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, save(){}, restore(){}, setTransform(){}, scale(){} }),
  };
  return el;
}

global.window = {
  innerWidth: 1200, innerHeight: 900, location: { hash: '' }, devicePixelRatio: 1, scrollY: 0,
  addEventListener(t, fn){ bound.push({ el: null, type: t, fn: fn }); }, removeEventListener(){}, scrollTo(){},
  matchMedia: () => ({ matches: false, addListener(){}, addEventListener(){} }),
  requestAnimationFrame(cb){ if (cb) cb(0); },
  getComputedStyle: () => ({ getPropertyValue: () => '', width: '100px', height: '20px' }),
  ResizeObserver: function (cb) { this.observe = (el) => { if (!el) throw new TypeError('observe: parameter 1 is not an Element'); }; this.disconnect = () => {}; },
};
global.ResizeObserver = window.ResizeObserver;
// The page's own timers: node would otherwise wait out the real 1500ms flash, and the callback
// would never run. Collected here and called below, so that code is tested too.
const timers = [];
global.setTimeout = (fn) => timers.push(fn);
global.setInterval = () => 0;
global.clearTimeout = () => {};
global.clearInterval = () => {};
window.setTimeout = global.setTimeout;
global.navigator = { clipboard: { writeText(){} } };
global.location = window.location;
global.history = { replaceState(){}, pushState(){} };
for (const k of ['getComputedStyle', 'requestAnimationFrame', 'addEventListener', 'matchMedia', 'innerWidth', 'scrollTo']) global[k] = window[k];

const docEl = make('document', {});
global.document = {
  getElementById: (id) => (ids.has(id) ? ELEMENTS.find(e => e.id === id) : null),
  querySelector: (s) => matchAll(s)[0] || null,
  querySelectorAll: (s) => matchAll(s),
  addEventListener(t, fn){ bound.push({ el: docEl, type: t, fn: fn, delegated: true }); },
  createElement: (t) => make(t, {}), createElementNS: (ns, t) => make(t, {}),
  documentElement: Object.assign(make('html', {}), { scrollHeight: 4000, clientHeight: 900 }),
  body: Object.assign(make('body', {}), { scrollHeight: 4000, clientHeight: 900 }), title: '',
};
const cea = ELEMENTS.find(e => e.id === 'cea-data');
if (cea) {
  const m = /id="cea-data"[^>]*>([\s\S]*?)<\/script>/.exec(html);
  if (m) cea.textContent = m[1];
}
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');
try { new Function(script)(); }
catch (e) { console.log('SCRIPT_THREW: ' + e.constructor.name + ': ' + e.message); process.exit(1); }

// Handlers are where the page does its work, and a handler that is never called is never tested.
// Each one is called once with a plausible event; a delegated one is called once per kind of
// element it could be reached through, which is how a reader reaches it.
// Every element a reader could click, not one per selector: the click handler returns at its
// first matching branch, so a target carrying data-id would hide the data-reveal branch, and a
// button clicked once would never reach the branch that closes what the first click opened.
const reachable = ['.entry-header', '.node', '.idbadge', '.chip', '.mk', '#main-nav a', 'input'];
const evFor = (type, target) => ({
  type: type, target: target, currentTarget: target, key: 'Enter', keyCode: 13,
  clientX: 10, clientY: 10, pageX: 10, pageY: 10, detail: 1, button: 0,
  preventDefault(){}, stopPropagation(){}, stopImmediatePropagation(){},
});
for (const b of bound) {
  const seenTargets = new Set();
  const targets = b.delegated
    ? reachable.flatMap(s => matchAll(s)).filter(t => !seenTargets.has(t) && seenTargets.add(t))
    : [b.el || docEl];
  for (const t of targets) {
    did.fired++;
    try { b.fn.call(t, evFor(b.type, t)); b.fn.call(t, evFor(b.type, t)); }
    catch (e) {
      console.log('HANDLER_THREW: ' + b.type + ' on ' + (t.nodeName || '?') + '.' +
                  (t.id || t.attrs && t.attrs.class || '') + ': ' + e.constructor.name + ': ' + e.message);
      process.exit(1);
    }
  }
}
// Once more with the reader at the foot of the page: the last section never rises past the
// nav, so the branch that marks it is reached no other way.
window.scrollY = document.documentElement.scrollHeight - window.innerHeight;
global.scrollY = window.scrollY;
for (const b of bound) {
  if (b.type !== 'scroll' && b.type !== 'resize') continue;
  did.fired++;
  try { b.fn.call(b.el || docEl, evFor(b.type, b.el || docEl)); }
  catch (e) {
    console.log('HANDLER_THREW: ' + b.type + ' at the foot of the page: ' +
                e.constructor.name + ': ' + e.message);
    process.exit(1);
  }
}
for (let i = 0; i < timers.length && i < 200; i++) {
  did.fired++;
  try { timers[i](); }
  catch (e) { console.log('TIMER_THREW: ' + e.constructor.name + ': ' + e.message); process.exit(1); }
}
const edgeBox = ELEMENTS.find(e => e.id === 'map-edges');
did.edges = edgeBox ? edgeBox.children.length : 0;
did.wired = wired.size;
did.matched = Object.fromEntries(asked);
console.log('SCRIPT_OK ' + JSON.stringify(did));
"""

    def run_script(self, data, nav=False):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / "page.html"
            # Every page in a real site is built with both links (cea_site.copy_paper), and the
            # nav the script queries is not the same one without them.
            extra = {"index_href": "../../", "framework_href": "../../framework/"} if nav else {}
            page.write_text(cea_page.build(data, Path("claims.json"), page, **extra),
                            encoding="utf-8")
            shim = Path(tmp) / "shim.js"
            shim.write_text(self.SHIM, encoding="utf-8")
            done = subprocess.run([shutil.which("node"), str(shim), str(page)],
                                  capture_output=True, text=True, timeout=30)
        out = done.stdout.strip() or done.stderr.strip()
        if not out.startswith("SCRIPT_OK "):
            return out, {}
        return "SCRIPT_OK", json.loads(out[len("SCRIPT_OK "):])

    def test_every_selector_the_script_uses_matches_something_it_emits(self):
        """A renamed class or id silently disables behaviour: nothing errors, it just stops.

        Renaming `entry-header`, for instance, leaves every card on every published page
        permanently collapsed, because the CSS hides the body until the script adds `expanded`.
        """
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"),
                              index_href="../../", framework_href="../../framework/")
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        ids = set(re.findall(r'\sid="([^"]+)"', html))
        classes = {c for m in re.findall(r'class="([^"]+)"', html) for c in m.split()}
        # classes the script puts on elements itself do not have to be in the markup
        classes |= set(re.findall(r"classList\.(?:add|toggle|remove)\(['\"]([\w-]+)['\"]", script))
        selectors = re.findall(r"(?:querySelector(?:All)?|closest)\(['\"]([^'\"]+)['\"]\)", script)
        wanted_ids = set(re.findall(r"getElementById\(['\"]([\w-]+)['\"]\)", script))
        wanted_ids |= {i for s in selectors for i in re.findall(r"#([\w-]+)", s)}
        wanted_classes = {c for s in selectors for c in re.findall(r"\.([\w-]+)", s)}
        # classList.contains('entry') decides whether a jump opens the card it lands on, and a
        # name that matches nothing is as silent as a selector that matches nothing
        wanted_classes |= set(re.findall(r"classList\.contains\(['\"]([\w-]+)['\"]\)", script))
        self.assertGreater(len(wanted_ids) + len(wanted_classes), 8, "the scan found nothing")
        self.assertEqual(sorted(wanted_ids - ids), [], "the script asks for ids the page never emits")
        self.assertEqual(sorted(wanted_classes - classes), [],
                         "the script asks for classes the page never emits")

    def test_the_markup_and_the_script_agree_on_every_data_attribute(self):
        """`data-jump` in the markup and `dataset.jump` in the script are one name in two spellings.

        Change either alone and the badge still renders, the click still fires, and the handler
        reads undefined: the page stops jumping to the record and says nothing about it.
        """
        def camel(name):
            return re.sub(r"-(\w)", lambda m: m.group(1).upper(), name)

        emitted, read = set(), set()
        for mutate in (lambda d: None, _a_candidate_weighed_against_a_result, _a_result_with_a_note):
            data = valid_claims()
            mutate(data)
            html = cea_page.build(data, Path("claims.json"), Path("out.html"))
            script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
            body = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
            emitted |= {camel(n) for n in re.findall(r'\sdata-([\w-]+)="', body)}
            read |= set(re.findall(r"dataset\.(\w+)", script))
            read |= {camel(n) for n in re.findall(r"\[data-([\w-]+)[\]=]", script)}
            read |= {camel(n) for n in re.findall(r"getAttribute\(['\"]data-([\w-]+)", script)}
        # the script gives itself a couple of keys that no markup carries
        written = set(re.findall(r"dataset\.(\w+)\s*=", script))
        self.assertGreater(len(emitted), 3, "the scan found no data attributes")
        self.assertEqual(sorted(emitted - read), [],
                         "the markup carries data attributes the script never reads")
        self.assertEqual(sorted(read - emitted - written), [],
                         "the script reads data attributes the markup never carries")

    def test_the_script_sets_only_attributes_a_browser_knows(self):
        """The map edges are SVG built by hand, and a misspelt attribute draws nothing.

        `setAttribute('stroke-width', ...)` is not checked by anything: a browser ignores a name
        it does not know, so the edge is still appended, still sized, and still invisible.
        """
        known = {"d", "fill", "opacity", "stroke", "stroke-width", "stroke-dasharray",
                 "viewBox", "transform", "class", "role", "tabindex", "title",
                 "aria-expanded", "aria-hidden", "aria-label", "hidden"}
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        used = set(re.findall(r"setAttribute\(['\"]([\w:-]+)['\"]", script))
        self.assertGreater(len(used), 3, "the scan found no attributes")
        self.assertEqual(sorted(used - known), [],
                         "the script sets an attribute no browser acts on; add it to `known` "
                         "only if it is a real SVG or ARIA attribute")

    def test_every_link_into_the_page_lands_on_something_it_emits(self):
        """A section anchor is built from a literal, and the section id from another.

        The copy-link button beside each heading is `ANCHOR.format(id=...)`. Change that id and
        the button copies a link to `#overview` on a page whose section is called something else:
        the link is written, the browser follows it, and it lands nowhere.
        """
        seen = 0
        for nav in ({}, {"index_href": "../../", "framework_href": "../../framework/"}):
            html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"), **nav)
            body = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
            ids = set(re.findall(r'\sid="([^"]+)"', body))
            # only links that stay on this page: `../../framework/#x` is another page's anchor
            targets = set(re.findall(r'href="#([\w-]+)"', body))
            seen += len(targets)
            with self.subTest(nav=bool(nav)):
                self.assertEqual(sorted(targets - ids), [],
                                 "the page links to anchors it does not emit")
        self.assertGreater(seen, 4, "the scan found no links")

    def test_every_class_the_script_toggles_is_one_the_stylesheet_styles(self):
        """A class the script adds does nothing unless a rule acts on it.

        `expanded` is what opens a card: the CSS hides `.entry-body` until `.entry.expanded`
        shows it. Rename it in the script and every card stays shut, with no error to see.
        """
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        style = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        toggled = set(re.findall(r"classList\.(?:add|remove|toggle)\(['\"]([\w-]+)", script))
        self.assertGreater(len(toggled), 3, "the scan found no class changes")
        self.assertEqual(sorted(toggled - set(re.findall(r"\.([A-Za-z][\w-]*)", style))), [],
                         "the script toggles classes the stylesheet never acts on")

    def test_every_class_the_script_writes_is_one_the_stylesheet_knows(self):
        """The tooltip's markup is written by the script, so the CSS scan never sees it.

        `tooltip.innerHTML` names `abbr`, and `.abbr` is styled in the stylesheet. Rename either
        and the tooltip still opens, unstyled, with no error anywhere.
        """
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        style = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        written = {c for m in re.findall(r'class=\\?"([^"\\]+)', script) for c in m.split()}
        self.assertTrue(written, "the scan found no markup in the script")
        self.assertEqual(sorted(written - set(re.findall(r"\.([A-Za-z][\w-]*)", style))), [],
                         "the script writes markup with classes nothing styles")

    def test_every_class_and_id_the_page_emits_is_used_by_its_css_or_script(self):
        """The other direction: a hook renamed where it is emitted leaves its rule behind.

        Renaming the emitted `cand-group` or `idbadge` keeps the stylesheet rule that names the
        old spelling, so the markup still renders but loses its styling, and nothing errors.
        """
        shapes = {
            "a full record": lambda d: None,
            "no results and no claims": lambda d: d.update(broad_statements=[], claims=[]),
            "a result no claim serves": lambda d: d.update(claims=[]),
            "no rejected candidates": lambda d: d.update(rejected=[]),
            "two statements of one result": _two_statements_of_one_result,
            "a candidate weighed against a result": _a_candidate_weighed_against_a_result,
            "a result with a note": _a_result_with_a_note,
        }
        seen = 10 ** 6
        for label, mutate in shapes.items():
            data = valid_claims()
            mutate(data)
            # record ids are the published anchors, and stand whether or not the page links them
            anchors = {x["id"] for k in ("broad_statements", "claims", "rejected")
                       for x in data[k]}
            for nav in ({}, {"index_href": "../../", "framework_href": "../../framework/"}):
                html = cea_page.build(data, Path("claims.json"), Path("out.html"), **nav)
                stray_c, stray_i, n = unused_hooks(html, anchors)
                seen = min(seen, n)
                with self.subTest(record=label, nav=bool(nav)):
                    self.assertEqual(stray_c, [],
                                     "the page emits classes nothing styles or scripts")
                    self.assertEqual(stray_i, [],
                                     "the page emits ids nothing styles, scripts or links to")
        # the leanest shape, so one page rendering nothing cannot hide behind the others
        self.assertGreater(seen, 20, "some shape rendered almost no markup")
        # The paper link has a branch per way of reaching the paper, and the one that links a
        # copy sitting beside the page needs that copy to be a real file.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "fixture.pdf").write_bytes(b"%PDF-1.4\n")
            # source_links only lists the files it can find beside the page
            for name in ("claims.json", "claims.md", "text.txt"):
                (d / name).write_text("x", encoding="utf-8")
            data = valid_claims()
            html = cea_page.build(data, d / "claims.json", d / "out.html")
            self.assertIn('href="fixture.pdf"', html, "the local paper is not linked")
            self.assertIn('class="files"', html, "the files this page was built from are not listed")
            anchors = {x["id"] for k, v in data.items() if isinstance(v, list) for x in v}
            self.assertEqual(unused_hooks(html, anchors)[:2], ([], []),
                             "the paper link emits a hook nothing styles or scripts")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_page_script_runs_for_every_record_shape(self):
        import cea_page as _  # noqa: F401  (imported at module scope below)
        shapes = {
            "a full record": lambda d: None,
            "no results and no claims": lambda d: d.update(broad_statements=[], claims=[]),
            "a result no claim serves": lambda d: (d.update(claims=[]),
                                                   d["broad_statements"][0].update(note="why")),
            "no rejected candidates": lambda d: d.update(rejected=[]),
            "one claim, one result": lambda d: d.update(claims=d["claims"][:1], rejected=[]),
            "a candidate weighed against a result": _a_candidate_weighed_against_a_result,
            "a result with a note": _a_result_with_a_note,
            "two statements of one result": _two_statements_of_one_result,
        }
        ran = {}
        for label, mutate in shapes.items():
            for nav in (False, True):
                with self.subTest(record=label, nav=nav):
                    data = valid_claims()
                    mutate(data)
                    key = label if not nav else f"{label}, in a site"
                    verdict, ran[key] = self.run_script(data, nav=nav)
                    self.assertEqual(verdict, "SCRIPT_OK",
                                     f"the page script must run for {key}")
                    # a query built from an attribute the script failed to read: the browser
                    # finds nothing, the handler returns, and the page quietly stops responding
                    self.assertEqual(ran[key]["badSelectors"], [],
                                     f"the script queries with a value it never read, for {key}")
        # Running without throwing is not enough: a renamed hook makes every loop iterate zero
        # times, which throws nothing. A page with results and claims has to wire more than one
        # with neither, and has to move at least one class, or the script took no hold at all.
        full, bare = ran["a full record"], ran["no results and no claims"]
        self.assertGreater(full["wired"], bare["wired"],
                           "the script wires no more on a full record than on an empty one")
        self.assertGreater(full["classed"], 0, "the script changes no class on a full record")
        self.assertGreater(full["created"], 0, "the script draws no map edge on a full record")
        # The map is redrawn on resize and after a jump. Each redraw has to clear what the last
        # one left, or the edges pile up and the faded ones stay behind the lit ones. The count
        # comes from the markup, not from the script: one edge per claim-to-result pair whose
        # result has a node of its own.
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        results = set(re.findall(r'class="node result[^"]*" data-id="([^"]+)"', html))
        pairs = sum(len(set(m.split()) & results)
                    for m in re.findall(r'class="node claim"[^>]*data-serves="([^"]*)"', html))
        self.assertGreater(pairs, 0, "the fixture draws no edges")
        self.assertEqual(full["edges"], pairs,
                         "the map holds a different number of edges than the record has pairs, "
                         "so a redraw is adding to what the last one drew")
        # A selector written out in the script has to reach something on some page, or the code
        # behind it never runs. Selectors the script composes are left out: `scope + ' .cand-group'`
        # is legitimately empty under `#claims`, and a grouped result has no node of its own.
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        best = {}
        for did in ran.values():
            for sel, n in did["matched"].items():
                best[sel] = max(best.get(sel, 0), n)
        literal = {sel: n for sel, n in best.items()
                   if f"'{sel}'" in script or f'"{sel}"' in script}
        self.assertGreater(len(literal), 5, "the scan found no selectors")
        self.assertEqual(sorted(sel for sel, n in literal.items() if n == 0), [],
                         "the script queries selectors that match nothing on any page")


class WhatThePageAsserts(unittest.TestCase):
    """The page applies labels the record does not contain, so each has to be earned."""

    def page(self, mutate=None):
        data = valid_claims()
        if mutate:
            mutate(data)
        return cea_page.build(data, Path("claims.json"), Path("out.html"))

    def test_a_reason_that_names_a_result_without_weighing_it_is_not_called_standing(self):
        """"B3 names Source Features" is what a candidate is about, not a result it leaves."""
        for reason, kind in (
                ("B1 would still stand, because this only divides the rate by language.", "standing"),
                ("No broad statement states the main result this would serve: B1 names the "
                 "features, and no summary sentence states a result for it.", "other"),
                ("The accuracy of a model the paper only reads other findings from, fitted so "
                 "that SHAP can give the importances behind B1.", "other")):
            with self.subTest(reason=reason[:40]):
                self.assertEqual(cea_page.kind_of({"id": "R1", "reason": reason}, {"B1"}), kind)

    def test_the_overview_counts_broad_statements_by_that_name(self):
        """Several statements can state one result and one can state two, so the page said a
        number its own Claim Map contradicted."""
        html = self.page()
        self.assertIn("broad statements", html)
        self.assertNotIn('stat-label">main results', html)

    def test_the_footer_names_the_skill_that_built_the_page(self):
        data = valid_claims()
        alone = cea_page.build(data, Path("claims.json"), Path("out.html"))
        in_site = cea_page.build(data, Path("claims.json"), Path("out.html"),
                                 index_href="../../", built_by="site skill")
        # The version of the build stands between the two, so the name is read up to it.
        for built_by, html in (("extract-claims skill", alone), ("site skill", in_site)):
            with self.subTest(built_by=built_by):
                self.assertRegex(html, re.escape(built_by) + r"[^<]*? built this page")

    def test_the_table_chip_counts_a_note_that_says_there_is_no_table(self):
        """That note is the check, made and written down, and it was the one being excluded.

        Read as the pattern's own behaviour rather than its spelling: pinning the source text
        meant the test had to be edited to say the same thing whenever the pattern changed.
        """
        html = self.page()
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        found = re.search(r"flag === 'table'\) return (/.+?/i)\.test\(note\)", script)
        self.assertIsNotNone(found, "the page no longer tests a note for a table or a figure")
        body, _, flags = found.group(1)[1:].rpartition("/")
        chip = re.compile(body.replace("\\b", "\\b"), re.I if "i" in flags else 0)
        for note, wanted in (("No table gives this number; it is in the text.", True),
                             ("Checked against Table V on page 7.", True),
                             ("The value is read from Fig. 2.", True),
                             ("Read from Figure 12.", True),
                             # "configuration" and "notable" hold the words without meaning them
                             ("The tool's configuration is given in Section 3; no notable "
                              "difference was reported.", False)):
            with self.subTest(note=note[:40]):
                self.assertEqual(bool(chip.search(note)), wanted)


class StandingIsWeighed(unittest.TestCase):
    """The heading says the result survives, so the reason has to say it does."""

    def test_a_negated_standing_claim_is_not_filed_under_standing(self):
        for reason, kind in (
                ("B1 would still stand, because this only divides the rate.", "standing"),
                ("B1 would not stand without this sentence, but the number is in a table.", "other"),
                ("B1 does not still stand here.", "other"),
                ("The main result B1 still stands without this sentence.", "standing")):
            with self.subTest(reason=reason[:44]):
                self.assertEqual(cea_page.kind_of({"id": "R1", "reason": reason}, {"B1"}), kind)


class FootnoteOnAFinalNumeral(unittest.TestCase):
    def test_a_marker_after_a_sentence_ending_in_a_year_may_be_dropped(self):
        """The decimal rule took the allowance away from any quote ending in a numeral."""
        self.assertTrue(cea_claims.quote_on(
            "The dataset was released in 2023.",
            "The dataset was released in 2023.4 The next section describes it."))

    def test_a_decimal_still_holds_the_figure_together(self):
        self.assertFalse(cea_claims.quote_on(
            "churn decreased from 0.17 to 0.",
            "The average churn decreased from 0.17 to 0.06 here."))


class NestedLists(unittest.TestCase):
    """The framework page is where a reader is sent for the definitions."""

    def test_a_nested_list_stays_nested(self):
        import cea_site
        html = cea_site.md_to_html("- a ground\n  - an example\n  - another\n- a second ground\n")
        self.assertEqual(html.count("<ul>"), 2, "the nesting was flattened")
        self.assertIn("<li>a ground<ul>", html)
        top = html[: html.index("<ul>", 4)]
        self.assertEqual(top.count("<li>"), 1, "a sub-item was promoted to a ground of its own")

    def test_an_ordered_list_keeps_its_numbering(self):
        """Flattening renumbered the items, so the reader's "item 2" was printed as item 4."""
        import cea_site
        html = cea_site.md_to_html("1. M1 nothing reconstructed\n   - a note\n   - another\n"
                                   "2. M2 the interpretation is drawn\n")
        outer = re.findall(r"<ol>(.*)</ol>", html, re.S)[0]
        items = re.findall(r"<li>(?:(?!<li>).)*?(M\d)", outer)
        self.assertEqual(items, ["M1", "M2"], "the sub-items were counted as items of the list")


class FrameworkMarkdown(unittest.TestCase):
    """The framework page is Markdown someone writes, rendered by `md_to_html`."""

    def test_two_headings_that_slug_the_same_get_different_anchors(self):
        import cea_site
        html = cea_site.md_to_html("## Narrow claim\n\nOne.\n\n### Narrow Claim\n\nTwo.\n")
        ids = re.findall(r'<h\d id="([^"]*)"', html)
        self.assertEqual(ids, ["narrow-claim", "narrow-claim-2"])

    def test_a_heading_with_a_diacritic_keeps_a_readable_anchor(self):
        """Without folding, "Méthode" slugs to `m-thode`, and every accented heading is mangled."""
        import cea_site
        html = cea_site.md_to_html("## Méthode\n\nOne.\n\n## Überblick\n\nTwo.\n")
        self.assertEqual(re.findall(r'<h\d id="([^"]*)"', html), ["methode", "uberblick"])

    def test_an_anchor_does_not_move_when_a_heading_is_added_above_it(self):
        """A positional fallback renumbered every later anchor, which published links depend on."""
        import cea_site
        def ids(doc):
            return re.findall(r'<h\d id="([^"]*)"', cea_site.md_to_html(doc))
        before = ids("## A\n\nOne.\n\n## \u4e2d\u6587\n\nTwo.\n")
        after = ids("## New\n\nZero.\n\n## A\n\nOne.\n\n## \u4e2d\u6587\n\nTwo.\n")
        self.assertEqual(before[-1], after[-1], "the anchor moved when a heading was added above")

    def test_a_heading_with_nothing_to_slug_still_gets_an_anchor(self):
        import cea_site
        html = cea_site.md_to_html("## !!!\n\nOne.\n\n## ???\n\nTwo.\n")
        ids = re.findall(r'<h\d id="([^"]*)"', html)
        self.assertEqual(len(set(ids)), 2, f"two headings share one anchor: {ids}")
        self.assertNotIn("", ids, "a heading was given an empty anchor")

    def test_a_pipe_written_into_a_table_cell_stays_in_that_cell(self):
        import cea_site
        html = cea_site.md_to_html("| a | b |\n| - | - |\n| `x\\|y` | one cell |\n")
        rows = [re.findall(r"<td>(.*?)</td>", r) for r in re.findall(r"<tr>(.*?)</tr>", html)]
        self.assertEqual(rows[-1], ["<code>x|y</code>", "one cell"])

    def test_a_row_that_ends_in_a_space_has_no_extra_column(self):
        import cea_site
        html = cea_site.md_to_html("| a | b |\n| - | - |\n| one | two | \n")
        rows = [re.findall(r"<td>(.*?)</td>", r) for r in re.findall(r"<tr>(.*?)</tr>", html)]
        self.assertEqual(rows[-1], ["one", "two"])


class PaperLinkStaysInTheRecord(unittest.TestCase):
    """The header link is how a reader opens the paper the footer says the quotes were checked
    against, so it may only point at a file the record carries."""

    def test_a_file_in_the_working_directory_is_not_linked(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = tmp / "cea-out" / "mypaper"
        rec.mkdir(parents=True)
        # a file of that name in the directory the command runs in, but not in the record
        (tmp / "mypaper.pdf").write_bytes(b"%PDF-SOMEONE-ELSES\n")
        data = valid_claims()
        data["paper"]["pdf"] = "mypaper.pdf"
        here = Path.cwd()
        os.chdir(tmp)
        try:
            html = cea_page.build(data, rec / "claims.json", rec / "index.html")
        finally:
            os.chdir(here)
        self.assertNotIn("..", "".join(re.findall(r'class="nav-ext" href="([^"]*)"', html)),
                         "the page links a file outside the record")

    def test_the_copy_in_the_record_is_linked(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "mypaper.pdf").write_bytes(b"%PDF-1.4\n")
        data = valid_claims()
        data["paper"]["pdf"] = "mypaper.pdf"
        html = cea_page.build(data, tmp / "claims.json", tmp / "index.html")
        self.assertIn('href="mypaper.pdf"', html)


class WithdrawnPapers(unittest.TestCase):
    def test_a_build_says_which_pages_it_did_not_write(self):
        """The merge keeps a dropped paper's directory, so its page stays reachable."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            recs = []
            for pid in ("alpha", "beta"):
                d = Path(tmp) / pid
                d.mkdir()
                (d / "text.txt").write_text(TEXT, encoding="utf-8")
                data = valid_claims()
                data["paper"]["id"] = pid
                (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                recs.append(d)
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                cea_site.build_site(recs, site)
                written, messages = cea_site.build_site(recs[:1], site)
        self.assertEqual(written, 1)
        joined = "\n".join(messages)
        self.assertIn("still holds beta", joined)
        self.assertIn("remove the directories", joined)


class EveryStatementReachesThePage(unittest.TestCase):
    """The page groups the statements that state one result. It may not drop any of them.

    A claim's `serves` badge links every id it names, so an id the page does not anchor is a link
    that goes nowhere, and the statement's own sentence, page and note never reach the reader
    although the record holds them and claims.md prints them.
    """

    def record(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "6 Discussion", "source": "conclusion"})
        for claim in data["claims"]:
            claim["serves"] = ["B1", "B2"]
        return data

    def test_every_broad_statement_has_a_card(self):
        data = self.record()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        self.assertEqual(re.findall(r'<article class="entry result" id="(B\d+)"', html),
                         [b["id"] for b in data["broad_statements"]])

    def test_no_link_on_the_page_points_at_a_statement_it_does_not_anchor(self):
        html = cea_page.build(self.record(), Path("claims.json"), Path("out.html"))
        ids = set(re.findall(r'\sid="([^"]+)"', html))
        self.assertEqual(sorted(set(re.findall(r'href="#(B\d+)"', html)) - ids), [])

    def test_the_overview_count_matches_the_cards(self):
        html = cea_page.build(self.record(), Path("claims.json"), Path("out.html"))
        said = int(re.search(r'<div class="stat-value">(\d+)</div>'
                             r'<div class="stat-label">broad statements', html).group(1))
        self.assertEqual(said, len(re.findall(r'<article class="entry result"', html)))

    def test_the_page_and_claims_md_hold_the_same_statements(self):
        data = self.record()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        self.assertEqual(sorted(set(re.findall(r'<article class="entry result" id="(B\d+)"', html))),
                         sorted(set(re.findall(r"^### (B\d+)", cea_claims.render(data), re.M))))


class ResultsAreOrderedByWhatIsPrintedBesideThem(unittest.TestCase):
    """The caption says results stand by how many sentences state each one, and that number is
    printed on every row. It was the per-statement count that decided the order and the per-group
    count that was printed, so a grouped result sorted below one stated fewer times."""

    def record(self):
        data = valid_claims()
        data["broad_statements"] = [
            {"id": f"B{i}", "quote": f"Statement number {i} of the paper here.", "page": 1,
             "section": "Abstract", "source": "abstract"} for i in (1, 2, 3)]
        data["broad_statements"].append(
            {"id": "B4", "quote": "A different result entirely here.", "page": 4,
             "section": "7 Conclusion", "source": "conclusion"})
        data["claims"] = [
            {"id": "C1", "quote": "A number for the first result.",
             "states": "A number for the first result.", "page": 2, "section": "5 Results",
             "serves": ["B1", "B2", "B3"], "split_from": None,
             "selection_reason": "They rest on this."},
            {"id": "C2", "quote": "A number for the other result.",
             "states": "A number for the other result.", "page": 2, "section": "5 Results",
             "serves": ["B4"], "split_from": None, "selection_reason": "B4 rests on this."}]
        data["rejected"] = [
            {"id": "R1", "quote": "It repeats B4 here.", "page": 4, "section": "7 Conclusion",
             "reason": "Repeats B4.", "duplicate_of": ["B4"]}]
        return data

    def test_the_rows_stand_in_the_order_of_the_number_they_print(self):
        html = cea_page.build(self.record(), Path("claims.json"), Path("out.html"))
        counts = [int(n) for n in re.findall(r"stated in (\d+) sentence", html)]
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "a row stands above one that names a larger number")

    def test_claims_md_says_the_same(self):
        md = cea_claims.render(self.record())
        places = [int(n) for n in re.findall(r"stated in (\d+) place", md)]
        self.assertEqual(places, sorted(places, reverse=True))


class MapAgreesWithTheRecord(unittest.TestCase):
    """The Claim Map is a second rendering of the record, and may not say something else."""

    def rows(self, data):
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        out = {}
        for row in re.findall(r'<div class="map-row">.*?</div></div>', html, re.S):
            lead = re.search(r'class="node result[^"]*" data-id="([^"]+)"', row).group(1)
            note = re.search(r'class="node-empty">([^<]*)', row)
            out[lead] = (re.findall(r'class="node claim" data-id="([^"]+)"', row),
                         (note.group(1) if note else ""))
        return html, out

    def test_the_map_lists_claims_in_the_page_order_its_caption_promises(self):
        """`mine` iterated the record, while the caption and the cards below use page order."""
        data = valid_claims()
        data["claims"][0]["page"] = 7          # C1 later in the paper than C2
        data["claims"][1]["page"] = 2
        html, rows = self.rows(data)
        self.assertIn("claims stand in page order", html)
        self.assertEqual(rows["B1"][0], ["C2", "C1"], "the map is not in page order")
        cards = re.findall(r'<article class="entry claim" id="(C\d+)"', html)
        self.assertEqual(rows["B1"][0], cards, "the map and the claim cards disagree")

    def two_results_one_repeating_sentence(self):
        """A record whose single rejected sentence repeats both main results."""
        data = valid_claims()
        data["broad_statements"].append({"id": "B2", "quote": "Failures stayed flat.", "page": 1,
                                         "section": "Abstract", "source": "abstract"})
        data["claims"].append({"id": "C3", "quote": "Across the 48 projects, the failure rate "
                                                    "stayed at 3%.",
                               "states": "The failure rate stayed at 3%.", "page": 2,
                               "section": "5 Results", "serves": ["B2"], "split_from": "S2",
                               "selection_reason": "B2 rests on this."})
        data["rejected"].append({"id": "R2", "quote": "Caching halves build time and failures "
                                                      "hold flat.",
                                 "page": 1, "section": "1 Introduction",
                                 "duplicate_of": ["B1", "B2"], "reason": "Repeats B1 and B2."})
        return data

    def test_a_row_says_how_many_of_its_sentences_state_another_result_too(self):
        """The rows add up to more than the Overview, because one sentence states two results.

        Both numbers are right for what they count. Without the note the reader adds up the rows,
        gets a different number from the stat beside them, and has nothing to explain the gap.
        """
        for label, extra_shared in (("one shared sentence", 0), ("two shared sentences", 1)):
            with self.subTest(record=label):
                data = self.two_results_one_repeating_sentence()
                for n in range(extra_shared):
                    data["rejected"].append(
                        {"id": f"R{10 + n}", "quote": f"Both results hold, restated {n}.",
                         "page": 1, "section": "6 Discussion", "duplicate_of": ["B1", "B2"],
                         "reason": "Repeats both."})
                html = cea_page.build(data, Path("claims.json"), Path("out.html"))
                rows = re.findall(r"stated in (\d+) sentences?(?: \((\d+) shared\))?", html)
                self.assertEqual(len(rows), 2)
                for _, shared in rows:
                    self.assertEqual(int(shared or 0), 1 + extra_shared,
                                     "a row miscounts the sentences it shares")
                headline = int(re.search(r'<div class="stat-value">(\d+)</div>'
                                         r'<div class="stat-label">sentences[^<]*'
                                         r'stating the main results', html).group(1))
                # A sentence stating k results adds k to the rows and 1 to the headline. Here
                # every shared sentence states both, so each contributes an excess of one.
                shared_sentences = 1 + extra_shared
                self.assertEqual(sum(int(n) for n, _ in rows) - shared_sentences, headline,
                                 "the rows and the Overview do not differ by the shared sentences")

    def test_a_record_with_no_shared_sentence_says_nothing_about_sharing(self):
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertNotIn("shared)", html)

    def test_a_claim_serving_two_results_is_named_under_both(self):
        """It is filed under the first, so the second row showed nothing of it and said nothing."""
        data = valid_claims()
        data["broad_statements"].append({"id": "B2", "quote": "Failures stayed flat.", "page": 1,
                                         "section": "Abstract", "source": "abstract"})
        data["claims"][0]["serves"] = ["B1", "B2"]
        data["claims"].append({"id": "C3", "quote": SPLIT_QUOTE, "states": "Failures held at 3%.",
                               "page": 3, "section": "5 Results", "serves": ["B2"],
                               "split_from": "S1", "selection_reason": "B2 rests on this."})
        html, rows = self.rows(data)
        self.assertEqual(rows["B1"][0], ["C1", "C2"])
        self.assertEqual(rows["B2"][0], ["C3"])
        self.assertIn("C1", rows["B2"][1],
                      "B2's row neither shows C1 nor says where it stands, while B2's own card "
                      "lists it")
        card = re.search(r'<article class="entry result" id="B2".*?</article>', html, re.S).group(0)
        self.assertIn(">C1<", card)


class TheTableRowAdvisoryReadsTheRightText(unittest.TestCase):
    """`start` and `end` are positions in the FOLDED page. Slicing the raw page with them reads
    some other part of it, because folding expands a ligature and drops a soft hyphen."""

    def test_a_plain_sentence_on_a_page_with_ligatures_is_not_called_a_table(self):
        page = ("Notes: " + "\ufb01" * 70 + "\n"
                "   1       Tool A       2,831      436\n"
                "Overall, the tool changed 12.4% of the files it reviewed in our sample.\n")
        quote = "Overall, the tool changed 12.4% of the files it reviewed in our sample."
        self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
        self.assertEqual(cea_claims._sentence_warnings("X", quote, page), [])

    def test_a_table_row_on_such_a_page_is_still_called_one(self):
        page = ("We veri\ufb01ed the \ufb01gures in \ufb01ve con\ufb01gurations of the \ufb01le.\n"
                "   1       Tool A       2,831      436\n")
        said = "\n".join(cea_claims._sentence_warnings("X", "1 Tool A 2,831 436", page))
        self.assertIn("reads like a row of a table", said)
        self.assertIn("1 Tool A 2,831 436", said, "it must quote the text it actually read")


class ARefusedBuildNamesEveryPageItLeaves(unittest.TestCase):
    """Only the last two refusal branches recorded the paper id, so a build refused by any shape
    check left a stale page published and said nothing about it."""

    def build(self, mutate):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            data = valid_claims()
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, first = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, "\n".join(first))
            mutate(data)
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 0)
            return "\n".join(messages)

    def test_a_refusal_raised_before_validate_still_names_the_page(self):
        """`serves` naming an unknown id is caught by a shape check, long before validate runs."""
        said = self.build(lambda d: d["claims"][0].update(serves=["B99"]))
        self.assertIn("CEA_INVALID", said)
        self.assertIn("is still published", said)

    def test_a_record_whose_own_page_is_sound_is_not_named(self):
        """Two records, one refused. The sound one's page is standing too, and rightly."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            recs = []
            for name in ("good", "bad"):
                rec = Path(tmp) / name
                rec.mkdir()
                (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                data = valid_claims()
                data["paper"]["id"] = name
                (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                recs.append(rec)
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, first = cea_site.build_site(recs, site)
            self.assertEqual(written, 2, "\n".join(first))
            data = json.loads((recs[1] / "claims.json").read_text(encoding="utf-8"))
            data["claims"][0].update(serves=["B99"])
            (recs[1] / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                _written, messages = cea_site.build_site(recs, site)
        said = "\n".join(messages)
        self.assertIn("papers/bad/index.html is still published", said)
        self.assertNotIn("papers/good/index.html is still published", said)


class RenderMayNotWriteThroughALink(unittest.TestCase):
    """Writing through a link replaces whatever it points at, anywhere on the machine. `extract`
    guards text.txt and the copied PDF, `site` guards everything it publishes, and `render` was
    the hole in that perimeter, in the command the skill runs most often."""

    def render(self, prepare):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rec = tmp / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            prepare(tmp, rec)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["render", str(rec)])
            outside = (tmp / "outside.txt")
            return code, out.getvalue(), (outside.read_text(encoding="utf-8")
                                          if outside.is_file() else None)

    def test_a_link_in_place_of_claims_md_is_refused(self):
        def prepare(tmp, rec):
            (tmp / "outside.txt").write_text("PRECIOUS", encoding="utf-8")
            (rec / "claims.md").symlink_to(tmp / "outside.txt")
        code, said, outside = self.render(prepare)
        self.assertEqual(code, 2)
        self.assertIn("is a symbolic link", said)
        self.assertEqual(outside, "PRECIOUS", "render wrote through the link")

    def test_a_link_in_place_of_claims_html_is_refused(self):
        def prepare(tmp, rec):
            (tmp / "outside.txt").write_text("PRECIOUS", encoding="utf-8")
            (rec / "claims.html").symlink_to(tmp / "outside.txt")
        code, said, outside = self.render(prepare)
        self.assertEqual(code, 2)
        self.assertIn("is a symbolic link", said)
        self.assertEqual(outside, "PRECIOUS")

    def test_a_dangling_link_is_refused_too(self):
        """`is_file` is false for one, so the page would read as never written and the link would
        be left for the next render to write through."""
        code, said, _ = self.render(
            lambda tmp, rec: (rec / "claims.html").symlink_to(tmp / "never-existed.html"))
        self.assertEqual(code, 2)
        self.assertIn("is a symbolic link", said)

    def test_ordinary_files_still_render(self):
        code, said, _ = self.render(lambda tmp, rec: None)
        self.assertEqual(code, 0, said)
        self.assertIn("CEA_RENDERED", said)


class AQuoteWithManyMarkers(unittest.TestCase):
    """Past the exhaustive limit only the two uniform readings were tried, so a quote in which the
    paper itself prints "[...]" once among real interruptions could not be found at all, and the
    message told the checker to do what they had already done."""

    def fixture(self, gaps):
        page = ["One participant said the tool [...] saved them time on review, and we then"]
        quote = "One participant said the tool [...] saved them time on review, and we then"
        for i in range(gaps):
            page.append(f"Fig. {i + 1}. A caption standing between two lines of the sentence")
            page.append(f"and the sentence carries on at line {i} of the paragraph here")
            quote += f" [...] and the sentence carries on at line {i} of the paragraph here"
        return "\n".join(page) + "\n", quote

    def test_one_printed_marker_among_many_gaps_is_found(self):
        for gaps in (3, 5, 6, 9, 12):
            with self.subTest(markers=gaps + 1):
                page, quote = self.fixture(gaps)
                self.assertIsNotNone(cea_claims.find_quote(quote, page),
                                     "a quote the paper prints a marker in was unfindable")

    def test_an_unmatched_quote_stays_cheap_as_the_markers_grow(self):
        """Every reading of six markers is 64 searches, which cost more per unmatched quote than
        the linear path above the limit: the exhaustive branch was dearer than its own fallback."""
        page = "\n".join(f"line {i} of a page the quote does not appear on" for i in range(60))
        worst = 0.0
        for gaps in range(0, 11):
            quote = "nothing here" + " [...] nothing there" * gaps
            began = time.monotonic()
            cea_claims.find_quote(quote, page)
            worst = max(worst, time.monotonic() - began)
        self.assertLess(worst, 1.0, f"an unmatched quote took {worst:.2f}s")


class TheGateFailsWhenAReleaseNeedsThePapers(unittest.TestCase):
    """`CEA_REQUIRE_PAPERS=1` is the documented release mode, and nothing covered the branch that
    makes a skipping run fail. It could never pass at all until the variable stopped leaking into
    the inner gate run, so the branch had never been exercised either way."""

    # Run against a stub suite, not the real one: the branch fires on a run that skipped, and the
    # real suite skips inside the gate's own run. The stub carries the skip instead, so the gate
    # under test is the real script and nothing recurses.
    STUB = '''import unittest


class Stub(unittest.TestCase):
    pass


for i in range(120):
    setattr(Stub, f"test_{i}", lambda self: None)


class Skips(unittest.TestCase):
    def test_this_one_skips(self):
        self.skipTest("stands in for a test that needs a paper")
'''

    def gate(self, require):
        """Run the real check.sh over a tree whose suite skips one test."""
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / "t"
            shutil.copytree(SCRIPTS.parent, tree,
                            ignore=shutil.ignore_patterns("__pycache__", ".git", "*-workspace"))
            for f in (tree / "scripts" / "tests").glob("test_*.py"):
                f.unlink()
            (tree / "scripts" / "tests" / "test_stub.py").write_text(self.STUB, encoding="utf-8")
            # This interpreter's directory, so the gate does not fall back to a system python
            # too old for its own version check; nothing else, so neither skills-ref nor the
            # claude CLI is found and the inner run stays offline and free.
            env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                   "HOME": str(Path.home())}
            if require:
                env["CEA_REQUIRE_PAPERS"] = "1"
            return subprocess.run(["sh", str(tree / "scripts" / "check.sh")],
                                  capture_output=True, text=True, timeout=300, env=env)

    def test_the_branch_fails_a_run_that_skipped(self):
        done = self.gate(require=True)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("CEA_FAILED: CEA_REQUIRE_PAPERS is set", done.stdout)

    def test_the_same_run_passes_without_the_variable(self):
        """Otherwise the branch could fail every run and still look like it works."""
        done = self.gate(require=False)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("CEA_SKIPPED:", done.stdout)
        self.assertNotIn("CEA_FAILED", done.stdout)


class ExtractionThatFailsSaysSoRatherThanPublishingPart(unittest.TestCase):
    """Nothing covered the three ways `pdftotext` can fail. It mattered most for the one that
    exits non-zero: without `check=True` a damaged PDF returns whatever it managed to read, and
    `extract` would write that as `text.txt` -- a page text with pages missing, which every later
    quote is then checked against. The command has to fail instead, and say so with a marker."""

    def run_extract(self, pdf, path):
        with tempfile.TemporaryDirectory() as out:
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "cea_claims.py"), "extract", str(pdf),
                 "--out", out],
                capture_output=True, text=True, timeout=600,
                env={"PATH": path, "HOME": str(Path.home())})
            return done, sorted(Path(out).rglob("text.txt"))

    def test_a_missing_poppler_names_what_to_install(self):
        """The first thing a new user of the skill hits."""
        pdf = SCRIPTS.parent / "evals" / "papers" / "ieeesw26-ai-slop.pdf"
        if not pdf.is_file():
            self.skipTest(f"{pdf.name} is not in evals/papers/")
        with tempfile.TemporaryDirectory() as empty:
            done, written = self.run_extract(pdf, empty)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("CEA_FAILED", done.stdout, "a failure has to carry the marker")
        self.assertIn("poppler", done.stdout, "it has to say what to install")
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(written, [], "nothing may be written when nothing was read")

    def test_a_damaged_pdf_fails_instead_of_writing_what_it_managed_to_read(self):
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "broken.pdf"
            bad.write_bytes(b"%PDF-1.4\nnot really a pdf\n")
            done, written = self.run_extract(bad, os.environ.get("PATH", ""))
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("CEA_FAILED", done.stdout)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(written, [], "a part of a damaged paper is not a page text")

    def test_a_pdftotext_that_never_finishes_is_reported(self):
        pdf = SCRIPTS.parent / "evals" / "papers" / "ieeesw26-ai-slop.pdf"
        if not pdf.is_file():
            self.skipTest(f"{pdf.name} is not in evals/papers/")
        with unittest.mock.patch.object(
                pdf_text.subprocess, "run",
                side_effect=subprocess.TimeoutExpired(cmd="pdftotext", timeout=300)):
            with self.assertRaises(RuntimeError) as caught:
                pdf_text._pages(str(pdf))
        self.assertIn("did not finish", str(caught.exception))

    def test_output_from_a_run_that_exited_non_zero_is_not_used(self):
        """Poppler usually fails wholesale or succeeds with a warning, so no real PDF reaches
        this. It is pinned anyway: what makes partial output dangerous is that it reads as a
        whole paper, and the only thing standing between the two is that the exit status is
        checked at all."""
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "pdftotext"
            fake.write_text("#!/bin/sh\nprintf 'page one text\\n'\nexit 1\n", encoding="utf-8")
            fake.chmod(0o755)
            old = os.environ.get("PATH", "")
            os.environ["PATH"] = f"{tmp}:{old}"
            try:
                with self.assertRaises(RuntimeError) as caught:
                    pdf_text._pages("anything.pdf")
            finally:
                os.environ["PATH"] = old
        self.assertIn("failed", str(caught.exception))
        self.assertNotIn("page one text", str(caught.exception),
                         "what a failed run printed is not the paper")

    def test_the_three_failures_are_told_apart(self):
        """One message for all three would leave a user guessing which had happened."""
        pdf = SCRIPTS.parent / "evals" / "papers" / "ieeesw26-ai-slop.pdf"
        if not pdf.is_file():
            self.skipTest(f"{pdf.name} is not in evals/papers/")
        said = []
        for error in (FileNotFoundError(),
                      subprocess.TimeoutExpired(cmd="pdftotext", timeout=300),
                      subprocess.CalledProcessError(1, "pdftotext", stderr="Syntax Error: no xref")):
            with unittest.mock.patch.object(pdf_text.subprocess, "run", side_effect=error):
                with self.assertRaises(RuntimeError) as caught:
                    pdf_text._pages(str(pdf))
                said.append(str(caught.exception))
        self.assertEqual(len(set(said)), 3, f"two failures read the same: {said}")
        self.assertIn("Syntax Error: no xref", said[2],
                      "the reason poppler gave has to reach the user")


class WhatTheLayoutSaysThePapersHold(unittest.TestCase):
    """`text.txt` is what every quote is checked against, so a change in what the layout produces
    changes what the validator accepts. Twelve mutations of `pdf_text.py` were found that move a
    real paper's text -- two of them stop extraction working at all -- with the whole suite green:
    the tests that read a real paper assert a handful of sentences and the rest use synthetic
    pages. The digests pin the rest of it.

    Regenerate deliberately, and say in the commit what moved:
        python3 scripts/text_digests.py --out scripts/tests/text_digests.json
    """

    PINNED = Path(__file__).resolve().parent / "text_digests.json"

    @classmethod
    def setUpClass(cls):
        if not cls.PINNED.is_file():
            raise unittest.SkipTest(f"{cls.PINNED.name} is not committed")
        cls.want = json.loads(cls.PINNED.read_text(encoding="utf-8"))
        version = text_digests.pdftotext_version()
        if version is None:
            raise unittest.SkipTest("pdftotext is not installed")
        if version != cls.want["pdftotext"]:
            # Skipped, not failed: `pdftotext -layout` lays out to its own version, and a wrong
            # verdict from another toolchain would be worse than no verdict.
            raise unittest.SkipTest(
                f"pdftotext is {version} and the digests were pinned with "
                f"{cls.want['pdftotext']}")

    def test_every_pinned_paper_still_extracts_the_same_text(self):
        for stem, want in sorted(self.want["papers"].items()):
            pdf = text_digests.PAPERS / f"{stem}.pdf"
            with self.subTest(paper=stem):
                if not pdf.is_file():
                    self.skipTest(f"{pdf.name} is not in evals/papers/")
                got = text_digests.digest_of(pdf)
                # What extract told the agent, before the text: the page count, the figure list
                # and where the references were removed are what the next step acts on.
                self.assertEqual(got["said"], want["said"],
                                 f"{stem}: extract says something different now")
                if got["sha256"] == want["sha256"]:
                    continue
                self.assertEqual(got["pages"], want["pages"],
                                 f"{stem}: the layout changed how many pages there are")
                moved = [n for n, page in sorted(want["per_page"].items(), key=lambda kv: int(kv[0]))
                         if got["per_page"].get(n, {}).get("sha256") != page["sha256"]]
                first = moved[0] if moved else "?"
                self.fail(
                    f"{stem}: page(s) {', '.join(moved)} no longer extract the same text. "
                    f"Page {first} was {want['per_page'][first]['words']} word(s) in "
                    f"{want['per_page'][first]['lines']} line(s) and is now "
                    f"{got['per_page'].get(first, {}).get('words')} in "
                    f"{got['per_page'].get(first, {}).get('lines')}. If the change is meant, "
                    f"regenerate with `python3 scripts/text_digests.py --out "
                    f"{self.PINNED.relative_to(SCRIPTS.parent)}` and say in the commit what moved.")

    def test_the_pin_covers_every_paper_that_is_present(self):
        """A paper added to evals/papers/ and left out of the digests is not pinned by anything."""
        here = {p.stem for p in text_digests.PAPERS.glob("*.pdf")}
        if not here:
            self.skipTest("no papers in evals/papers/")
        self.assertEqual(here - set(self.want["papers"]), set(),
                         "regenerate scripts/tests/text_digests.json")


class ExtractNamesEachFigureOnce(unittest.TestCase):
    """The list is what a number in the text is looked up against. "Fig. 2" and "Figure 2" are
    one figure, and listing both costs a reader a hunt for a third. Reverting the fold left every
    test green, so the behaviour had no cover at all."""

    def labels(self, lines):
        return cea_claims.figure_labels(lines)

    def test_the_two_spellings_of_a_figure_are_one_entry(self):
        self.assertEqual(self.labels(["Figure 2 shows the distribution",
                                      "Fig. 2. Distribution of review comments"]), ["Fig. 2"])
        self.assertEqual(self.labels(["FIGURE 3. A caption", "Fig. 3 again"]), ["Fig. 3"])

    def test_different_figures_and_tables_stay_apart(self):
        self.assertEqual(self.labels(["Fig. 1. One", "Figure 2 two", "TABLE I  three",
                                      "Table II four"]),
                         ["Fig. 1", "Fig. 2", "Table I", "Table II"])

    def test_the_real_extract_lists_each_figure_once(self):
        pdf = SCRIPTS.parent / "evals" / "papers" / "ieeesw26-ai-slop.pdf"
        if not pdf.is_file() or not shutil.which("pdftotext"):
            self.skipTest("the paper or pdftotext is not present")
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "cea_claims.py"), "extract", str(pdf),
                 "--out", tmp], capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        listed = [l for l in done.stdout.splitlines() if l.startswith("tables and figures:")]
        self.assertEqual(len(listed), 1)
        names = [n.rsplit(" p", 1)[0] for n in listed[0].split(": ", 1)[1].split(", ")]
        self.assertEqual(sorted(names), sorted(set(names)), f"listed twice: {names}")


class ANoteHedgesOnlyTheQuantityItHedges(unittest.TestCase):
    """The loop reading a note's wholes is scoped to one whole each. The hedge that lets a note
    say its rows are incomplete was not: it searched the whole note, so a note that hedged one
    quantity honestly silenced a second whose arithmetic really was wrong."""

    def warnings(self, note):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": "Caching halves median build time.", "page": 1,
            "section": "Abstract", "source": "abstract",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "We coded 15 codes in the data.", "page": 1,
                             "section": "5 Results", "note": note,
                             "reason": "B1 would still stand: this only counts the codebook."}]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text("=== page 1 ===\nCaching halves median build time.\n"
                                        "We coded 15 codes in the data.\n", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return len([w for w in cea_claims.advisories(d, parsed) if "add up to" in w])

    def test_a_hedge_on_one_quantity_does_not_cover_another(self):
        self.assertEqual(self.warnings(
            "Table I prints only the parts of the sentence's 15 that are topical: 5 and 3. "
            "It also prints the parts of its number, 12: 7 and 9."), 1)

    def test_ordinary_prose_is_not_a_hedge(self):
        """"out of", "rather than" and a bare "only" are how the reference asks notes to name a
        place: "in Table I rather than Fig. 1"."""
        for note in ("Table I prints the parts of the sentence's 15: 5 and 3. Only Table I prints them.",
                     "Table I prints the parts of the sentence's 15: 5 and 3, out of those collected.",
                     "Table I prints the parts of the sentence's 15: 5 and 3, in Table I rather than Fig. 1."):
            with self.subTest(note=note[-40:]):
                self.assertEqual(self.warnings(note), 1)

    def test_a_note_that_says_its_rows_are_incomplete_is_still_quiet(self):
        for note in ("Table I prints only some of the parts of the sentence's 15: 5 and 3.",
                     "Table I gives shares of the 978 posts; the parts of the sentence's 15: 5 and 3."):
            with self.subTest(note=note[:44]):
                self.assertEqual(self.warnings(note), 0)


class TheStampBitesAtALaterFormat(unittest.TestCase):
    """Reading a missing stamp as format 1 was done with an early return, which made the
    comparisons below it unreachable: at a future FORMAT an unstamped record would have passed a
    build that refuses a stamped format-1 one, so the stamp was inert for its own purpose."""

    def refusals(self, stamp, writes):
        data = valid_claims()
        if stamp is None:
            data.pop("format", None)
        else:
            data["format"] = stamp
        with unittest.mock.patch.object(cea_claims, "FORMAT", writes), \
                unittest.mock.patch.object(
                    cea_claims, "_FORMAT_CHANGES",
                    {**cea_claims._FORMAT_CHANGES, 2: "`page` became a range everywhere."}):
            return [p for p in cea_claims._format_problems(data) if p.startswith("format")]

    def test_an_unstamped_record_is_refused_by_a_later_build(self):
        said = "\n".join(self.refusals(None, 2))
        self.assertIn("written in format 1 and this build writes 2", said)
        self.assertIn("became a range", said, "it has to say what changed")

    def test_a_stamped_record_is_refused_the_same_way(self):
        self.assertEqual(self.refusals(None, 2), self.refusals(1, 2),
                         "an unstamped record must be treated as the format-1 record it is")

    def test_this_build_accepts_both(self):
        self.assertEqual(self.refusals(None, cea_claims.FORMAT), [])
        self.assertEqual(self.refusals(cea_claims.FORMAT, cea_claims.FORMAT), [])

    def test_the_advisory_names_the_format_it_was_read_as(self):
        """Naming the build's own FORMAT would tell the checker to relabel a format-1 record as a
        later format in one edit."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            data = valid_claims()
            data.pop("format")
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            said = "\n".join(cea_claims.advisories(d, parsed))
        self.assertIn('"format": 1', said)
        self.assertIn("read as format 1", said)


class AWideGapDoesNotHideARealHeading(unittest.TestCase):
    """A real section heading can carry a wide run of space after its number. Treating that as
    layout hid it, leaving the gap guarded only by the prose fallback -- which steps aside for any
    gap holding a caption, and such a heading can stand on a page with one."""

    def test_a_heading_at_the_margin_keeps_its_wide_gap(self):
        page = ("We found that developers mention tools for several reasons in\n"
                "6.1               RQ1: Reasons for Mentioning GenAI Tools\n"
                "the repositories we studied across every project.\n")
        quote = ("We found that developers mention tools for several reasons in [...] "
                 "the repositories we studied across every project.")
        self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
        self.assertEqual(cea_claims._skipped_heading(quote, page),
                         "6.1               RQ1: Reasons for Mentioning GenAI Tools")

    def test_an_indented_axis_tick_is_still_layout(self):
        page = ("These results are contrary to\n"
                "      1.0                                                            Data\n"
                "Fig. 2. Churn by project, four panels, with the legend at the right\n"
                "our expectations because the report was very bold in claiming that\n")
        quote = ("These results are contrary to [...] our expectations because the report was "
                 "very bold in claiming that")
        self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
        self.assertIsNone(cea_claims._skipped_heading(quote, page))


class AQuoteKeepsTheHyphensThePaperPrints(unittest.TestCase):
    """`_flat` joined every hyphen at a line break, so a hyphen the paper prints was deleted from
    the published quote. A red team used it to render "-0.96" as "0.96" in the blockquote labelled
    as the paper's own words, on a page that printed the correct sign two cards above. It hits an
    honest checker too: copying "within a 14-\nday window" exactly published "14day"."""

    def test_a_word_broken_across_lines_is_still_joined(self):
        self.assertEqual(cea_claims._flat("man-\nagement of the tool"), "management of the tool")

    def test_a_minus_sign_survives_the_break(self):
        self.assertEqual(cea_claims._flat("Trigger auto 2 0.0410 -\n0.96"),
                         "Trigger auto 2 0.0410 -0.96")

    def test_a_hyphen_the_paper_prints_survives(self):
        for raw, want in (("within a 14-\nday window", "within a 14-day window"),
                          ("Actions ID-\n1 to ID-\n4", "Actions ID-1 to ID-4"),
                          ("pages 16-\n18 of the report", "pages 16-18 of the report")):
            with self.subTest(raw=raw[:24]):
                self.assertEqual(cea_claims._flat(raw), want)

    def test_it_draws_the_line_where_the_matcher_does(self):
        """What is published has to say what the matcher matched."""
        for raw in ("man-\nagement", "14-\nday", "-\n0.96", "ID-\n1"):
            with self.subTest(raw=raw):
                flat = cea_claims._flat(raw)
                page = f"The value {raw} appears here.\n"
                self.assertTrue(cea_claims.quote_on(f"The value {raw} appears here.", page))
                self.assertEqual(cea_claims.normalize(flat)[0],
                                 cea_claims.normalize(raw)[0],
                                 "the rendered text folds differently from the quote")


class ThePageReportsTheRecordNotThePaper(unittest.TestCase):
    """With no broad statements the page said "This paper states no quantitative main result".
    The record does not say that: it says these sentences are not broad statements. Demoting a
    quantitative paper's statements to candidates made the page assert it in its own voice, above
    cards quoting that paper's numbers. It is the cheapest attack in the system: delete an array."""

    def test_the_scope_card_speaks_for_the_record(self):
        data = valid_claims()
        data["broad_statements"] = []
        data["claims"] = []
        for e in data["rejected"]:
            e.pop("duplicate_of", None)
            e.pop("breaks_down", None)
            e["reason"] = "No main result depends on this count."
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        self.assertIn("marks no sentence of this paper as stating a quantitative main result", html)
        self.assertNotIn("This paper states no quantitative", html)


class AListOfResultsSharesOneVerb(unittest.TestCase):
    """Reading a reason clause by clause cuts "B3 and B5 would both still stand" at the "and",
    which left B3 in a clause with nothing to affirm it. Two real records lost a result they are
    credited with, and the suite did not see it."""

    def test_a_list_keeps_every_result_it_names(self):
        for reason, want in (
                ("This only restates the rate; B3 and B5 would both still stand without it.",
                 {"B3", "B5"}),
                ("A remark that a robustness check changed nothing, so B3 and B7 would still "
                 "stand.", {"B3", "B7"})):
            with self.subTest(reason=reason[:44]):
                self.assertEqual(
                    cea_page.weighed_against({"reason": reason}, {"B1", "B3", "B5", "B7"}), want)

    def test_a_comitative_connective_carries_the_list(self):
        """"B3, together with B5, would still stand" dropped both, and an Oxford comma in
        "B3, B5, and B7" kept only the last."""
        for reason, want in (
                ("B3, together with B5, would still stand, because the evidence lies elsewhere.",
                 {"B3", "B5"}),
                ("B3, along with B5, would still stand here.", {"B3", "B5"}),
                ("B3, B5, and B7 would still stand, because the abstract states it.",
                 {"B3", "B5", "B7"}),
                ("B3, B5 and B7 would still stand.", {"B3", "B5", "B7"})):
            with self.subTest(reason=reason[:44]):
                self.assertEqual(
                    cea_page.weighed_against({"reason": reason}, {"B3", "B5", "B7"}), want)

    def test_a_repeat_before_a_verdict_still_credits_only_the_verdict(self):
        self.assertEqual(
            cea_page.weighed_against({"reason": "Repeats B4, so B2 would still stand."},
                                     {"B2", "B4"}), {"B2"})

    def test_no_real_record_loses_a_result(self):
        """Measured across the workspace, because the two that broke were real records."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        seen = 0
        for path in sorted(workspace.rglob("claims.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            known = {b["id"] for b in data.get("broad_statements", []) if isinstance(b, dict)}
            for e in data.get("rejected", []):
                if not isinstance(e, dict):
                    continue
                reason = str(e.get("reason", ""))
                named = set(re.findall(r"\bB\d+\b", reason)) & known
                if len(named) > 1 and cea_page.still_stands(reason):
                    seen += 1
                    self.assertEqual(cea_page.weighed_against(e, known), named,
                                     f"{e['id']} lost a result it names: {reason[:90]}")
        if not seen:
            self.skipTest("no record names more than one result in a standing reason")


class TheTwoSiteTitlesNameTheirOwnPage(unittest.TestCase):
    """Giving the framework page a name of its own put that name on the index instead, so the
    paper list called itself the framework and the framework called itself the site."""

    def test_the_index_and_the_framework_differ_and_say_what_they_are(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rec = tmp / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            framework = tmp / "framework.md"
            framework.write_text("# Claim-Evidence Alignment\n\nThe terms.\n", encoding="utf-8")
            site = tmp / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site, framework=framework)
            self.assertEqual(written, 1, "\n".join(messages))
            titles = {}
            for name in ("index.html", "framework/index.html"):
                html = (site / name).read_text(encoding="utf-8")
                titles[name] = re.search(r"<title>([^<]*)", html).group(1)
        self.assertNotEqual(titles["index.html"], titles["framework/index.html"])
        self.assertIn("papers", titles["index.html"])
        self.assertIn("framework", titles["framework/index.html"])


class AReasonWeighingTwoResultsTagsOnlyTheOneItAffirms(unittest.TestCase):
    """`still_stands` reads a reason clause by clause; `weighed_against` then took every id in the
    whole string as soon as any clause affirmed. A reason weighing two results -- one surviving,
    one not, which is what a careful checker writes -- tagged the candidate with both, and the
    page printed "leaves standing B5" directly above a reason saying B5 does not stand."""

    REASON = ("B7 would still stand without this sentence, because the abstract already states "
              "the concentration. B5 is a different matter: nothing else in the paper supports "
              "it, so B5 does not stand without this sentence.")

    def test_only_the_affirmed_result_is_tagged(self):
        self.assertEqual(cea_page.weighed_against({"reason": self.REASON}, {"B5", "B7"}), {"B7"})

    def test_the_candidate_still_counts_as_standing(self):
        self.assertTrue(cea_page.still_stands(self.REASON))

    def test_an_ordinary_reason_is_unchanged(self):
        for reason, want in (
                ("B1 would still stand, because this only divides the rate by language.", {"B1"}),
                ("B3 would still stand: this reports that a check changed nothing.", {"B3"}),
                ("No main result would still stand without this sentence.", set())):
            with self.subTest(reason=reason[:44]):
                self.assertEqual(cea_page.weighed_against({"reason": reason}, {"B1", "B3"}), want)


class ATitleThatIsNotThisPapersIsQuestioned(unittest.TestCase):
    """The title names the paper in the page, its tab, claims.md and the index, and nothing
    compared it with the paper. Measured across the 30 records: every real title shares every one
    of its content words with page 1; a fabricated one shared 62%."""

    def warnings(self, title):
        data = valid_claims()
        data["paper"]["title"] = title
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return [w for w in cea_claims.advisories(d, parsed) if "paper.title" in w]

    def test_a_title_the_page_prints_is_not_questioned(self):
        self.assertEqual(self.warnings("Caching halves median build time"), [])

    def test_a_title_from_another_paper_is_questioned(self):
        said = "\n".join(self.warnings(
            "Large Language Models Are Not Yet Ready for Code Review: A Replication"))
        self.assertIn("does not print this title", said)

    def test_a_title_set_in_small_capitals_is_not_questioned(self):
        """A title block is laid out differently from a sentence: small capitals come out
        letter-spaced, which the matcher joins only where no hyphen or merge is in the way."""
        for page, title in (("S ELF-A DMITTED U SAGE", "SELF-ADMITTED USAGE"),
                            ("G EN AI U SAGE", "GENAI USAGE")):
            with self.subTest(title=title):
                data = valid_claims()
                data["paper"]["title"] = title
                data["paper"]["pages"] = 3
                with tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp)
                    (d / "text.txt").write_text(TEXT.replace("=== page 1 ===\n",
                                                             f"=== page 1 ===\n{page}\n"),
                                                encoding="utf-8")
                    (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                    problems, parsed = cea_claims.validate(d)
                    self.assertEqual(problems, [])
                    said = [w for w in cea_claims.advisories(d, parsed) if "paper.title" in w]
                self.assertEqual(said, [], f"{title!r} was questioned")

    def test_a_negated_title_is_questioned(self):
        """A share of the title's words could never see this: "not" and "no" are stop words, so
        "Caching halves median build time" and "Caching does not halve median build time" have
        the same content words, and the page's largest line could deny the paper."""
        said = "\n".join(self.warnings("Caching does not halve median build time"))
        self.assertIn("does not print this title", said)

    def test_every_real_title_is_printed_on_page_one(self):
        """The rule reads the paper, so it costs nothing only if papers print their titles."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        found = missing = 0
        for path in sorted(workspace.rglob("claims.json")):
            record = path.parent
            if not (record / "text.txt").is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            title = str(data.get("paper", {}).get("title", ""))
            pages = cea_claims.load_pages(record / "text.txt")
            if not title or 1 not in pages:
                continue
            if cea_claims.quote_on(title, pages[1]):
                found += 1
            else:
                missing += 1
        if not (found or missing):
            self.skipTest("no records with a title and a first page")
        self.assertEqual(missing, 0, f"{missing} of {found + missing} real titles not on page 1")

    def test_it_is_a_warning_and_not_a_refusal(self):
        """A title can legitimately sit in a logo or a figure the extraction does not keep."""
        data = valid_claims()
        data["paper"]["title"] = "Something Entirely Unrelated To Any Of This"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(cea_claims.validate(d)[0], [])


class TextTxtSaysWhichPaperItCameFrom(unittest.TestCase):
    """Nothing tied the text to a paper. A record could keep one paper's text, name another
    paper's title and PDF, and publish a page whose every quote came from a document the footer
    told the reader to check against -- with the wrong PDF copied in beside it."""

    def extracted(self, tmp, pdf_name):
        pdf = SCRIPTS.parent / "evals" / "papers" / pdf_name
        if not pdf.is_file():
            self.skipTest(f"{pdf_name} is not present")
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "cea_claims.py"), "extract", str(pdf), "--out", str(tmp)],
            capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return tmp / pdf.stem

    def test_extract_records_the_paper_and_its_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.extracted(Path(tmp), "ieeesw26-ai-slop.pdf")
            came = cea_claims.source_of(record / "text.txt")
        self.assertIsNotNone(came, "text.txt does not say which paper it came from")
        self.assertEqual(came[0], "ieeesw26-ai-slop.pdf")
        self.assertRegex(came[1], r"^[0-9a-f]{64}$")

    def test_a_record_naming_another_paper_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.extracted(Path(tmp), "ieeesw26-ai-slop.pdf")
            data = valid_claims()
            data["paper"]["pdf"] = "some-other-paper.pdf"
            data["paper"]["pages"] = len(cea_claims.load_pages(record / "text.txt"))
            (record / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            said = "\n".join(cea_claims.validate(record)[0])
        self.assertIn("but text.txt was extracted from", said)
        self.assertIn("ieeesw26-ai-slop.pdf", said)

    def test_a_swapped_file_under_the_right_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.extracted(Path(tmp), "ieeesw26-ai-slop.pdf")
            (record / "ieeesw26-ai-slop.pdf").write_bytes(b"%PDF-1.4 not the same paper\n")
            data = valid_claims()
            data["paper"]["pdf"] = "ieeesw26-ai-slop.pdf"
            data["paper"]["pages"] = len(cea_claims.load_pages(record / "text.txt"))
            (record / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            said = "\n".join(cea_claims.validate(record)[0])
        self.assertIn("is not the file text.txt was extracted from", said)

    def test_a_record_written_before_the_header_is_not_refused(self):
        """Every record that exists today has no header, and refusing them would stop the
        published site's build dead, exactly as requiring the format stamp would have."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            self.assertIsNone(cea_claims.source_of(d / "text.txt"))
            self.assertEqual(cea_claims.validate(d)[0], [])

    def test_the_page_markers_still_parse_under_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = self.extracted(Path(tmp), "ieeesw26-ai-slop.pdf")
            pages = cea_claims.load_pages(record / "text.txt")
        self.assertGreater(len(pages), 1, "the header swallowed the pages")
        self.assertEqual(min(pages), 1)


class AVerdictIsNotAQuestion(unittest.TestCase):
    """`still_stands` mines the reason's prose for the page's "Leaves its main result standing"
    heading. A clause that ASKS whether a result stands is not saying that it does, and one that
    says it stands only under some reading is stating a condition."""

    def test_a_reason_that_leaves_the_question_open_is_not_a_verdict(self):
        reason = ("Dropping this sentence would leave B2 with no denominator at all, so whether "
                  "B2 would still stand is the one thing a reader has to decide for themselves.")
        self.assertFalse(cea_page.still_stands(reason))
        self.assertNotEqual(cea_page.kind_of({"id": "R1", "reason": reason}, {"B2"}), "standing")

    def test_a_result_that_stands_only_under_a_reading_is_not_standing(self):
        reason = "B2 would still stand only in a reading that ignores the sample this table rests on."
        self.assertFalse(cea_page.still_stands(reason))

    def test_the_ordinary_verdicts_are_unchanged(self):
        for reason in ("B1 would still stand, because this only divides the rate by language.",
                       "B3 would still stand: this reports that a check changed nothing.",
                       "B1 still stands without this sentence."):
            with self.subTest(reason=reason[:44]):
                self.assertTrue(cea_page.still_stands(reason))


class ACountTheRecordCannotEarnSaysSo(unittest.TestCase):
    """`duplicate_of` is held to referential integrity and nothing else: no check asks whether the
    sentence it names actually repeats the result. The count is the record's own marking, so the
    page and the index say that rather than claiming the paper states a result that often."""

    def test_the_stat_card_names_what_it_counts(self):
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertIn("the record marks as stating the main results", html)

    def test_the_index_column_matches(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, "\n".join(messages))
            index = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("the record marks as stating the main results", index)


class NothingIsANegation(unittest.TestCase):
    """The page applies the label "leaves its main result standing" from the reason's own words,
    and `\\bno\\b` does not reach inside "nothing". A reason saying no result survives was read as
    saying one does, and the page then asserted the opposite of the record in three places."""

    def test_a_reason_that_says_nothing_stands_is_not_called_standing(self):
        reason = ("This is the condition the measurement was taken under. Nothing in B1 would "
                  "still stand if the cache had been cold, so the number is part of the setup.")
        self.assertFalse(cea_page.still_stands(reason))
        self.assertNotEqual(cea_page.kind_of({"id": "R1", "reason": reason}, {"B1"}), "standing")

    def test_a_result_that_stands_although_something_changed_nothing_still_stands(self):
        """A real reason from a real record: the colon separates the verdict from its ground, so
        the "nothing" in the ground must not deny the verdict in front of it."""
        reason = ("B3 would still stand: this reports that a robustness check on the AI-only "
                  "subset changed nothing.")
        self.assertTrue(cea_page.still_stands(reason))

    def test_the_ordinary_shapes_are_unchanged(self):
        for reason, stands in (
                ("B1 would still stand, because this only divides the rate by language.", True),
                ("No main result would still stand without this sentence.", False),
                ("B1 still stands without this sentence.", True),
                ("Neither B1 nor B2 would still stand.", False)):
            with self.subTest(reason=reason[:40]):
                self.assertEqual(cea_page.still_stands(reason), stands)


class TheReferenceExampleIsARecordThatValidates(unittest.TestCase):
    """SKILL.md tells the agent to write claims.json as the reference describes, so the worked
    example is what gets copied. It has to be a record the validator accepts."""

    def test_the_example_holds_every_required_top_level_field(self):
        reference = (SCRIPTS.parent / "skills" / "extract-claims" / "references"
                     / "record-format.md").read_text(encoding="utf-8")
        found = re.search(r"```json\n(\{.*?\n\})\n```", reference, re.S)
        self.assertIsNotNone(found, "the reference has no worked example")
        example = json.loads(found.group(1))
        for field in [*cea_claims.FIELDS, "format"]:  # the example shows it even though it is optional
            with self.subTest(field=field):
                self.assertIn(field, example,
                              f"the example an agent copies has no '{field}'")
        self.assertEqual(example["format"], cea_claims.FORMAT)

    def test_the_example_is_not_refused_for_its_shape(self):
        reference = (SCRIPTS.parent / "skills" / "extract-claims" / "references"
                     / "record-format.md").read_text(encoding="utf-8")
        example = json.loads(re.search(r"```json\n(\{.*?\n\})\n```", reference, re.S).group(1))
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            by_page = {}
            for key in ("broad_statements", "claims", "rejected"):
                for e in example.get(key, []):
                    page = int(str(e["page"]).split("-")[0])
                    by_page.setdefault(page, []).append(e["quote"].replace(" [...] ", "\n"))
            (d / "text.txt").write_text(
                "".join(f"=== page {n} ===\n" + "\n".join(by_page.get(n, [])) + "\n"
                        for n in range(1, example["paper"]["pages"] + 1)), encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(example), encoding="utf-8")
            problems = cea_claims.validate(d)[0]
        shape = [p for p in problems if "missing" in p or "unknown" in p or "format" in p]
        self.assertEqual(shape, [], "the example is refused for its shape")


class APaperIdNamesADirectory(unittest.TestCase):
    """`site` writes the page in a directory of that name, so a name the filesystem refuses fails
    inside the staging copy, where the remedy printed is to run validate, which passed it."""

    def problems(self, paper_id):
        data = valid_claims()
        data["paper"]["id"] = paper_id
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return [p for p in cea_claims.validate(d)[0] if p.startswith("paper.id")]

    def test_an_id_too_long_for_a_directory_is_refused(self):
        said = "\n".join(self.problems("x" * 300))
        self.assertIn("characters", said)
        self.assertIn("shorter id", said)

    def test_an_ordinary_id_passes(self):
        self.assertEqual(self.problems("tse26-genai-usage"), [])

    def test_the_schema_says_so_too(self):
        self.assertEqual(cea_claims.schema()["properties"]["paper"]["properties"]["id"]
                         .get("maxLength"), 200)


class TheFrameworkPageRendersItsLinks(unittest.TestCase):
    """The framework document is the operator's own, so this is not a trust boundary. A scheme
    the page cannot follow is a dead link, and an image came out as a link with a stray "!"."""

    def test_an_image_stays_the_text_it_is_written_as(self):
        import cea_site
        out = cea_site.md_to_html("See ![the diagram](chain.png) above.")
        self.assertIn("![the diagram](chain.png)", out)
        self.assertNotIn("<a href=\"chain.png\"", out)

    def test_the_links_a_page_can_follow_are_kept(self):
        import cea_site
        for href in ("https://example.org/p.pdf", "#levels", "mailto:a@b.org", "claims.md"):
            with self.subTest(href=href):
                self.assertIn(f'href="{href}"', cea_site.md_to_html(f"See [it]({href})."))

    def test_a_scheme_the_page_cannot_follow_is_dropped(self):
        import cea_site
        for href in ("javascript:alert(1)", "data:text/html,<script>x</script>", "vbscript:x"):
            with self.subTest(href=href):
                out = cea_site.md_to_html(f"See [it]({href}).")
                self.assertIn('href="#"', out)
                self.assertNotIn(href.split(":")[0] + ":", out)


class ANoteMaySayItsRowsAreIncomplete(unittest.TestCase):
    """The skill asks for a note naming what a table's rows are parts of where the table prints
    only some of them. That note then trips the sum check, and the way to clear the warning was to
    stop naming the whole -- so the rule paid the writer to be vaguer."""

    def warnings(self, note):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": "Caching halves median build time.", "page": 1,
            "section": "Abstract", "source": "abstract",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = [{"id": "R1", "quote": "We coded 15 codes in the data.", "page": 1,
                             "section": "5 Results", "note": note,
                             "reason": "B1 would still stand: this only counts the codebook."}]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text("=== page 1 ===\nCaching halves median build time.\n"
                                        "We coded 15 codes in the data.\n", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return [w for w in cea_claims.advisories(d, parsed) if "add up to" in w]

    def test_rows_that_should_add_up_and_do_not_are_still_questioned(self):
        self.assertTrue(self.warnings("The table prints the parts of the sentence's 15: 5, 5 and 4."))

    def test_a_note_saying_the_rows_are_only_some_of_them_is_not(self):
        self.assertEqual(
            self.warnings("The table prints only the parts of the sentence's 15 that are "
                          "topical: 5, 5 and 4."), [])

    def test_a_note_naming_another_denominator_is_not(self):
        self.assertEqual(
            self.warnings("Figure 2 gives shares of the 978 coded posts, while the sentence's 15 "
                          "is of all codings: 5, 5 and 4."), [])

    def test_rows_that_add_up_say_nothing(self):
        self.assertEqual(
            self.warnings("The table prints the parts of the sentence's 15: 5, 5 and 5."), [])


class WhatExtractTellsTheAgent(unittest.TestCase):
    """The list of tables and figures is what a number in the text is looked up against, and the
    page counts are what an agent reads to know the paper was read whole."""

    def report(self, name="ieeesw26-ai-slop"):
        pdf = SCRIPTS.parent / "evals" / "papers" / f"{name}.pdf"
        if not pdf.is_file():
            self.skipTest(f"{pdf.name} is not present")
        if not shutil.which("pdftotext"):
            self.skipTest("pdftotext is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "cea_claims.py"), "extract", str(pdf),
                 "--out", tmp], capture_output=True, text=True, timeout=300)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            return done.stdout

    def test_one_figure_is_listed_once(self):
        """The paper writes "Figure 2" in the text and "Fig. 2" in the caption. Listed twice, a
        reader spends a moment looking for a third figure."""
        listed = [l for l in self.report().splitlines() if l.startswith("tables and figures:")]
        self.assertEqual(len(listed), 1, "extract printed no list of tables and figures")
        names = [n.rsplit(" p", 1)[0] for n in listed[0].split(": ", 1)[1].split(", ")]
        self.assertEqual(sorted(names), sorted(set(names)), f"a figure is listed twice: {names}")
        self.assertNotIn("Figure 2", names, "Fig. 2 and Figure 2 are one figure")

    def test_the_page_counts_do_not_contradict_each_other(self):
        """"7 of them with two-column text" stood beside "empty pages: 7, 8"."""
        said = self.report()
        columns = int(re.search(r"pages: \d+, (\d+) of them", said).group(1))
        empty = re.search(r"empty pages: ([\d, ]+)", said)
        total = int(re.search(r"pages: (\d+)", said).group(1))
        blank = len(empty.group(1).split(",")) if empty else 0
        self.assertLessEqual(columns, total - blank,
                             f"{columns} pages said to hold text, but only {total - blank} do")


class AQuoteMayNotStepFromOneRowToTheNext(unittest.TestCase):
    """A `[...]` stands for a table interrupting one sentence. It cannot also carry a quote from
    one row of that table to another: two rows are two records, and welding them reads as one.
    The case is real, from Table 7 of a real paper: one project's policy joined to another's,
    published under the table's own section, attributing to the first what the second wrote."""

    PAGE = ("P3  shoelace-style/shoelace      \u201caccept any code generated in such a manner.\u201d      12\n"
            "P4  turms-im/turms              \u201cCan Responses Generated by a Model be Used?\u201d      9\n"
            "P5  katsutedev/mal4j            \u201cContributions generated by GenAI are refused.\u201d    7\n")

    def test_a_quote_welding_two_rows_is_refused(self):
        weld = ("P3 shoelace-style/shoelace \u201caccept any code generated in such a manner.\u201d 12 "
                "[...] P5 katsutedev/mal4j \u201cContributions generated by GenAI are refused.\u201d 7")
        self.assertTrue(cea_claims.quote_on(weld, self.PAGE), "the fixture does not match")
        self.assertTrue(cea_claims._joins_two_rows(weld, self.PAGE))

    def test_one_row_on_its_own_stands(self):
        one = "P4 turms-im/turms \u201cCan Responses Generated by a Model be Used?\u201d 9"
        self.assertTrue(cea_claims.quote_on(one, self.PAGE))
        self.assertFalse(cea_claims._joins_two_rows(one, self.PAGE))

    def test_a_sentence_stepping_over_a_table_still_stands(self):
        """The ends are the paper's own lines, so the rule does not reach it."""
        page = ("We found 38 dis\nTABLE I   A caption here\nRow A     12     34\n"
                "tinct configurations here.\n")
        quote = "We found 38 dis[...]tinct configurations here."
        self.assertTrue(cea_claims.quote_on(quote, page))
        self.assertFalse(cea_claims._joins_two_rows(quote, page))

    def test_no_real_record_is_refused_by_it(self):
        """Measured across every `[...]` quote in the workspace: the rule costs nothing."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        checked = refused = 0
        for path in sorted(workspace.rglob("claims.json")):
            record = path.parent
            if not (record / "text.txt").is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pages = cea_claims.load_pages(record / "text.txt")
            for key in ("broad_statements", "claims", "rejected"):
                for e in data.get(key, []):
                    if not isinstance(e, dict) or "[" not in str(e.get("quote", "")):
                        continue
                    for n in sorted(pages):
                        if cea_claims.find_quote(e["quote"], pages[n]):
                            checked += 1
                            refused += cea_claims._joins_two_rows(e["quote"], pages[n])
                            break
        if not checked:
            self.skipTest("no records with a [...] quote to measure")
        self.assertEqual(refused, 0, f"{refused} of {checked} real gapped quotes refused")


class AFootnoteMarkerIsNotOnlyACapital(unittest.TestCase):
    """`_FOOTNOTE_LINE` wanted a capital or a literal http after the number, and matched 4 of the
    17 footnote lines in these papers. The rest were left in the gap, so the paper's own sentence
    could hide behind them and be welded into a quote."""

    def test_a_bare_domain_is_a_marker(self):
        for line in ("1 merriam-webster.com/slang/slop",
                     "17 windowscentral.com/. . . /microsoft-ceo-satya-nadella-. . .",
                     "11 https://www.reddit.com", "13 We used the same tool"):
            with self.subTest(line=line[:40]):
                self.assertIsNotNone(cea_claims._FOOTNOTE_LINE.match(line))

    def test_a_section_number_and_a_quantity_are_not(self):
        for line in ("2. Methodology", "15 repositories with a downward trend. Among the",
                     "48 projects that used remote caching in the period"):
            with self.subTest(line=line[:40]):
                self.assertIsNone(cea_claims._FOOTNOTE_LINE.match(line))

    def test_a_link_finishes_the_block_so_the_paper_behind_it_is_seen(self):
        page = ("names a correctness failure of model output-slop can be free of hallucinations "
                "and still waste its recipients'\n"
                "1 merriam-webster.com/slang/slop\n"
                "2 merriam-webster.com/dictionary/hallucination\n"
                "time. Spam, i.e., \u201cunsolicited usually commercial messages\u201d,3\n"
                "serves a commercial or criminal interest.\n")
        quote = ("names a correctness failure of model output-slop can be free of hallucinations "
                 "and still waste its recipients' [...] serves a commercial or criminal interest.")
        self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
        # The paper says the recipients' *time* is wasted, and that spam has the commercial
        # motive. The weld gives slop that motive, in the paper's own voice.
        self.assertIsNotNone(cea_claims._skipped_prose(quote, page))

    def test_a_footnote_that_runs_on_is_still_one_block(self):
        page = ("Later edits are not reflected, and comments created and then deleted before our "
                "data collection were excluded. AI tools\n"
                "  17 windowscentral.com/. . . /microsoft-ceo-satya-nadella-. . .\n"
                "change so quickly that findings can be outdated within months.\n")
        quote = ("Later edits are not reflected, and comments created and then deleted before our "
                 "data collection were excluded. AI tools [...] change so quickly that findings "
                 "can be outdated within months.")
        self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
        self.assertIsNone(cea_claims._skipped_prose(quote, page),
                          "a gap over one footnote is what the marker is for")


class AFigureBodyIsNotThePaperTalking(unittest.TestCase):
    """A four-panel figure between two halves of one sentence was refused, with its own panel
    title quoted back as the paper's running text, and its axis tick read as a section heading."""

    def test_a_panel_label_is_not_prose(self):
        for line in ("(a) Upward Trend with Positive Slope Change",
                     "(Days Relative to Intro Commit)", "(c) Downward Trend with Positive Slope"):
            with self.subTest(line=line[:40]):
                self.assertFalse(cea_claims._reads_like_prose(line))

    def test_a_bracketed_number_opening_a_sentence_is_prose(self):
        """"(1) how these tools are adopted and configured, (2) whether" is the paper."""
        for line in ("(1) how these tools are adopted and configured, (2) whether",
                     "(3) Renamed-Only (filename changed with no content modi-"):
            with self.subTest(line=line[:40]):
                self.assertTrue(cea_claims._reads_like_prose(line))

    def test_an_axis_tick_beside_a_legend_is_not_a_heading(self):
        line = "1.0                                                            Data"
        self.assertTrue(cea_claims._HEADING.match(line), "the fixture must have a heading's shape")
        self.assertFalse(cea_claims._titles_a_section(line),
                         "a wide run of space after the number is layout, not a heading")

    def test_the_headings_that_are_spaced_out_are_kept(self):
        """Small capitals are spaced, so a shouting line keeps its gap."""
        for line in ("9     C ONCLUSION", "7     R ELATED W ORK", "6.2 RQ2: Existing Guidelines"):
            with self.subTest(line=line):
                self.assertTrue(
                    (cea_claims._HEADING.match(line) and cea_claims._titles_a_section(line))
                    or cea_claims._HEADING_NO_DOT.match(line))


class TheGeneratedCasesDoNotUseTheRulesTheyTest(unittest.TestCase):
    """`material()` classified the pages with the very predicates under test, so a shape a regex
    did not recognise was never drawn and never became a case -- which is the class every defect
    here has belonged to. `_CAPTION_START` and `_FOOTNOTE_LINE` could each be deleted outright
    and the harness stayed green."""

    def test_the_harness_does_not_import_the_validator_s_patterns(self):
        harness = SCRIPTS / "gap_cases.py"
        if not harness.is_file():
            self.skipTest("gap_cases.py is not present")
        source = harness.read_text(encoding="utf-8")
        classifier = source.split("def material(", 1)[1].split("\ndef ", 1)[0]
        for borrowed in ("C._CAPTION_START", "C._FOOTNOTE_LINE", "C._HEADING", "C._HEADING_NO_DOT",
                         "C._titles_a_section", "C._reads_like_prose", "C._FIGURE_LABEL"):
            with self.subTest(pattern=borrowed):
                self.assertNotIn(borrowed, classifier,
                                 "the cases are classified with a rule they are meant to test")

    def test_a_block_holding_the_paper_s_prose_is_not_a_block(self):
        """One harvested "footnote" held five lines of running text, so every case drawn from it
        was a weld scored as legitimate."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import gap_cases
        except ImportError as e:  # pragma: no cover
            raise unittest.SkipTest(f"gap_cases is not importable: {e}")
        blocks, _pairs = gap_cases.material()
        for kind, drawn in blocks.items():
            for block in drawn:
                held = [l for l in block if gap_cases.reads_as_the_paper(l)]
                self.assertEqual(held, [], f"a {kind} block holds the paper's own text: {held[:1]}")


class GeneratedGapCases(unittest.TestCase):
    """The `[...]` rules read messy extracted layout, and every fix to them so far was a pattern
    added after a reader found a quote it got wrong. Half of those broke something else, and
    neither the 30 real records nor this suite could see any of it: both stayed green through
    every one. These cases are generated from the real pages instead, with the answer known by
    construction, and the two error rates are pinned so the next change has to face them."""

    ROUNDS = 400
    # Eight seeds, not one: see the note on MOST_WELDS_ACCEPTED below.
    SEEDS = range(8)
    # Measured at the seed and round count above. Tightened only with the measurement that
    # earned it, and never loosened to make a change pass.
    # Re-measured after `material()` stopped classifying with the predicates under test. The old
    # numbers were drawn from six footnote blocks, some of which held the paper's own prose, so a
    # weld built from one was scored as a success. Deleting `_CAPTION_START` or `_FOOTNOTE_LINE`
    # outright left the old harness green; both now fail it.
    # Measured, not chosen. The generated cases are harsher than the papers: on the real pages
    # 0 of 213 legitimate gaps are refused and 609 of 107,012 welds accepted (0.57%). These
    # numbers are the generated ones, which is what this test can re-run anywhere.
    MOST_LEGITIMATE_REFUSED = 0.02
    # Measured over the eight seeds together: 9.6 to 10.0% welds accepted. A single seed's rate
    # ranges from 7.6% to 11.3% across seeds, so a pin one seed passes another fails on the same
    # code -- 4 of 32 seeds breached the old 0.11. `_HEADING_NO_DOT` is NOT held by this number,
    # and is covered instead by the named cases in `EveryNumberedHeadingIsSeen`, `GapWarnings`
    # and `AFigureBodyIsNotThePaperTalking`.
    MOST_WELDS_ACCEPTED = 0.105

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        try:
            import gap_cases
        except ImportError as e:  # pragma: no cover
            raise unittest.SkipTest(f"gap_cases is not importable: {e}")
        cls.gap_cases = gap_cases
        material = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        if not any(material.rglob("text.txt")):
            raise unittest.SkipTest("no extracted papers to build cases from")
        cls.built = {"legitimate": 0, "weld": 0}
        cls.fails = {"legitimate refused": [], "weld accepted": []}
        for seed in cls.SEEDS:
            built, fails = gap_cases.run(seed, cls.ROUNDS)
            for k in cls.built:
                cls.built[k] += built[k]
            for k in cls.fails:
                cls.fails[k] += fails[k]
        if not (cls.built["legitimate"] and cls.built["weld"]):
            raise unittest.SkipTest("the pages held too little to build cases from")

    def test_enough_cases_were_built_to_mean_anything(self):
        self.assertGreater(self.built["legitimate"], 100)
        self.assertGreater(self.built["weld"], 100)

    def test_a_gap_where_the_marker_belongs_is_not_refused(self):
        """A false refusal is the worse error: it stops a record a checker wrote correctly."""
        bad = self.fails["legitimate refused"]
        rate = len(bad) / self.built["legitimate"]
        self.assertLessEqual(
            rate, self.MOST_LEGITIMATE_REFUSED,
            f"{len(bad)} of {self.built['legitimate']} legitimate gaps refused "
            f"({rate:.1%}); first: {bad[:2]}")

    def test_a_weld_is_refused(self):
        bad = self.fails["weld accepted"]
        rate = len(bad) / self.built["weld"]
        self.assertLessEqual(
            rate, self.MOST_WELDS_ACCEPTED,
            f"{len(bad)} of {self.built['weld']} welds accepted "
            f"({rate:.1%}); first: {bad[:2]}")


class WhatAGapMayAndMayNotSkip(unittest.TestCase):
    """The whole suite passed with every one of these wrong, so each is pinned by its own case.
    A gap stands for a figure, a table, a footnote, or a page break, and for nothing else."""

    WELDS = {
        "a caption and the paper's own prose beside it": (
            "Finally, we use interpretable\n"
            "Fig. 1. Overview of the study design, showing the three stages and\n"
            "the data that flows between them.\n"
            "machine learning techniques to model and find out the factors\n"
            "that influence the effectiveness of comments.\n"
            "Our contributions are as follows.\n",
            "Finally, we use interpretable [...] Our contributions are as follows."),
        "prose behind a line that opens with a quantity": (
            "the slope was negative. In addition, we also found\n"
            "15 repositories with a downward trend. Among the\n"
            "GenAI tasks identified in RQ1, generation tasks show\n"
            "a stronger impact on code churn than other tasks.\n",
            "In addition, we also found [...] a stronger impact on code churn than other tasks."),
    }
    ALLOWED = {
        "a caption of several lines": (
            "The failure rate increased for 12 of the 48\n"
            "Fig. 3. Distribution of review comments across the four\n"
            "categories, with the share of each category per project\n\n"
            "projects that used remote caching.\n",
            "The failure rate increased for 12 of the 48 [...] projects that used remote caching."),
        "a footnote of two lines": (
            "We asked whether the tool changed the review outcome in\n\n"
            "  13 We used the same tool and model to proofread and tighten\n"
            "  this manuscript, as the reporting guidelines ask authors to say.\n\n"
            "any measurable way across the sample.\n",
            "We asked whether the tool changed the review outcome in [...] any measurable way "
            "across the sample."),
        "a table with its caption": (
            "We found 38 dis\nTABLE I   A caption here\nRow A     12     34\n"
            "tinct configurations here.\n",
            "We found 38 dis[...]tinct configurations here."),
    }

    def test_a_weld_is_refused(self):
        for name, (page, quote) in self.WELDS.items():
            with self.subTest(case=name):
                self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
                self.assertIsNotNone(cea_claims._skipped_prose(quote, page),
                                     "two sentences welded into one went through")

    def test_what_the_marker_is_for_is_allowed(self):
        for name, (page, quote) in self.ALLOWED.items():
            with self.subTest(case=name):
                self.assertTrue(cea_claims.quote_on(quote, page), "the fixture does not match")
                self.assertIsNone(cea_claims._skipped_prose(quote, page),
                                  "a gap standing where the marker belongs was refused")

    def test_a_caption_block_ends_at_its_sentence(self):
        """Waiting for a blank line swallowed the paragraph after a caption that had none, which
        is most of them in extracted text."""
        block = ("Fig. 1. Overview of the study design, showing the three stages and\n"
                 "the data that flows between them.\n"
                 "machine learning techniques to model and find out the factors\n")
        self.assertEqual(cea_claims._without_the_figures(block),
                         ["machine learning techniques to model and find out the factors"])

    def test_a_quantity_at_the_start_of_a_line_is_not_a_footnote_marker(self):
        for line, marker in (("13 We used the same tool and model to proofread", True),
                             ("3 See the replication package for the full list.", True),
                             ("11 https://www.example.org/replication", True),
                             ("15 repositories with a downward trend. Among the", False),
                             ("48 projects that used remote caching in the period", False)):
            with self.subTest(line=line[:36]):
                self.assertEqual(bool(cea_claims._FOOTNOTE_LINE.match(line)), marker)


class AFiguresAxisIsNotASection(unittest.TestCase):
    """A y-axis tick can land on the same extracted line as a legend title, and "1.0        Data"
    then has a numbered heading's exact shape. A heading is refused, so this refused a quote the
    reference tells the checker to write."""

    PAGE = ("These results are contrary to\n"
            "1.0                                                            Data\n"
            "Fig. 2. Churn by project, four panels, with the legend at the right\n"
            "our expectations because the GitClear report was very bold in claiming that\n")
    QUOTE = ("These results are contrary to [...] our expectations because the GitClear report "
             "was very bold in claiming that")

    def test_a_gap_holding_a_caption_is_not_read_for_headings(self):
        self.assertTrue(cea_claims.quote_on(self.QUOTE, self.PAGE), "the fixture does not match")
        self.assertIsNone(cea_claims._skipped_heading(self.QUOTE, self.PAGE),
                          "a figure's axis label was called a section heading")

    def test_a_real_heading_is_still_refused(self):
        page = ("The failure rate increased for 12 of the 48\n6.2 RQ2: Existing Guidelines\n"
                "projects that used remote caching.\n")
        quote = ("The failure rate increased for 12 of the 48 [...] projects that used remote "
                 "caching.")
        self.assertEqual(cea_claims._skipped_heading(quote, page), "6.2 RQ2: Existing Guidelines")


class EveryLabelInAListKeepsItsDigits(unittest.TestCase):
    """Only the label sitting against its bracket was protected, so "(R01, R02, R03)" guarded R01
    alone and the rest could lose their digits and still be certified verbatim."""

    PAGE = ("This yielded 16 documents: 13 Reddit threads (R01, R02, R03) and three Hacker News "
            "result pages (H01, H02).\n")

    def test_a_later_label_may_not_drop_its_number(self):
        for bad in ("13 Reddit threads (R01, R, R)", "three Hacker News result pages (H01, H)"):
            with self.subTest(quote=bad):
                self.assertIsNone(cea_claims.find_quote(bad, self.PAGE))

    def test_the_list_as_printed_is_accepted(self):
        for good in ("13 Reddit threads (R01, R02, R03)",
                     "three Hacker News result pages (H01, H02)"):
            with self.subTest(quote=good):
                self.assertIsNotNone(cea_claims.find_quote(good, self.PAGE))

    def test_a_footnote_marker_outside_a_label_list_may_still_be_left_off(self):
        page = "We collected 1,203 builds from 48 projects.2 The rest were excluded.\n"
        self.assertIsNotNone(
            cea_claims.find_quote("We collected 1,203 builds from 48 projects.", page))


class AGapMayNotWeldTwoSentences(unittest.TestCase):
    """The record's promise is that a quote is one sentence of the paper. A `[...]` standing over
    the paper's own prose welds two unrelated sentences into one certified quote, and `render` and
    `site` print no advisories and stop for none, so a warning never reached anyone."""

    PAGE = ("On average, these posts received 1.6 codes, showing that developers often address\n"
            "multiple themes in one post.\n\n"
            "Figure 2 shows the frequency distribution. The three most\n"
            "frequent topical codes are structural-drivers (256, or 26.2% of coded posts),\n"
            "ai-limitations (227), and\n"
            "slop-mitigations (226), which together account for 44.2% of all 1,603 codings.\n")
    WELD = ("On average, these posts received 1.6 codes, showing that developers often address "
            "[...] slop-mitigations (226), which together account for 44.2% of all 1,603 codings.")

    def test_the_weld_is_refused(self):
        self.assertTrue(cea_claims.quote_on(self.WELD, self.PAGE), "the fixture does not match")
        self.assertIsNotNone(cea_claims._skipped_prose(self.WELD, self.PAGE),
                             "two sentences welded into one went through with nothing said")

    def test_prose_that_opens_with_a_figure_number_is_not_a_caption(self):
        """One caption-looking line passed over the whole gap, and "Figure 2 shows the frequency
        distribution" is running prose, not a caption."""
        for prose in ("Figure 2 shows the frequency distribution. The three most",
                      "Table 3 lists the models we evaluated in the study."):
            with self.subTest(line=prose[:36]):
                self.assertIsNone(cea_claims._CAPTION_START.match(prose))
        for caption in ("Fig. 3. Distribution of review comments", "TABLE V", "TABLE XVI",
                        "Figure 12", "Algorithm 2", "Table 5 Results by project"):
            with self.subTest(line=caption):
                self.assertIsNotNone(cea_claims._CAPTION_START.match(caption))

    def test_the_commands_stop_for_it(self):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": self.WELD, "page": 1, "section": "4 Results", "source": "abstract",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(f"=== page 1 ===\n{self.PAGE}", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems = cea_claims.validate(d)[0]
        self.assertTrue(any("reads as the paper's own running text" in p for p in problems),
                        f"validate accepted the weld: {problems}")


class EveryNumberedHeadingIsSeen(unittest.TestCase):
    """`_HEADING` required a dot after the number and `_HEADING_NO_DOT` required capitals, so
    every ordinary numbered subsection heading was invisible and a quote could be welded across
    one."""

    def heading(self, line):
        return bool((cea_claims._HEADING.match(line) and cea_claims._titles_a_section(line))
                    or cea_claims._HEADING_NO_DOT.match(line))

    def test_a_subsection_heading_without_a_trailing_dot(self):
        for line in ("6.2 RQ2: Existing Guidelines for GenAI Usage", "2.1 Repository Sampling",
                     "8.3 External Validity", "3.2 Results"):
            with self.subTest(line=line):
                self.assertTrue(self.heading(line), "an ordinary subsection heading was invisible")

    def test_the_shapes_that_already_worked_still_do(self):
        for line in ("V. DISCUSSION", "III-B. Coding Procedure", "5.2. Results by project",
                     "A. Data Collection", "9     C ONCLUSION", "8 T HREATS TO VALIDITY"):
            with self.subTest(line=line):
                self.assertTrue(self.heading(line))

    def test_a_sentence_with_a_heading_s_shape_is_not_a_heading(self):
        """A heading that crosses this test is refused, so a false one refuses a sound record.
        Both of these stand in real papers, the first in an author-affiliation block."""
        for line in ("C. Treude is with School of Computing and Information Systems, Singapore",
                     "III. In our first round of evaluation, we observed that general-"):
            with self.subTest(line=line[:40]):
                self.assertFalse(self.heading(line))


class AVersionDigitSurvivesALineBreak(unittest.TestCase):
    """`_names_a_version` walked back over the token and stopped at the line break that the
    extraction leaves after a hyphen, so half the names it was written for were unprotected."""

    PAGE = ("3-sonnet and claude-3-haiku), and DeepSeek [21] (deepseek-\n"
            "r1 and deepseek-v3). The specific API endpoints and model\n")

    def test_a_name_broken_across_two_lines_keeps_its_digit(self):
        self.assertIsNone(cea_claims.find_quote("(deepseek-r and deepseek-v3)", self.PAGE),
                          "the quote names a model the paper does not")

    def test_the_name_as_printed_is_accepted(self):
        self.assertIsNotNone(cea_claims.find_quote("(deepseek-r1 and deepseek-v3)", self.PAGE))

    def test_a_version_written_without_a_hyphen(self):
        for page, bad, good in (
                ("We tagged the release (e.g., @v1) before the run.\n", "(e.g., @v)", "(e.g., @v1)"),
                ("Released under Creative Commons Zero v1.0 Universal terms.\n",
                 "Creative Commons Zero v.0 Universal", "Creative Commons Zero v1.0 Universal")):
            with self.subTest(bad=bad):
                self.assertIsNone(cea_claims.find_quote(bad, page))
                self.assertIsNotNone(cea_claims.find_quote(good, page))

    def test_a_source_identifier_may_not_lose_its_number(self):
        """A quotation's attribution could be stripped and still certified."""
        page = "One team reported receiving 30 PRs per day across 6 reviewers [R07].\n"
        self.assertIsNone(cea_claims.find_quote(
            "One team reported receiving 30 PRs per day across 6 reviewers [R].", page))
        self.assertIsNotNone(cea_claims.find_quote(
            "One team reported receiving 30 PRs per day across 6 reviewers [R07].", page))

    def test_an_ordinary_footnote_marker_may_still_be_left_off(self):
        for page, quote in (
                ("We collected 1,203 builds from 48 projects.2 The rest were excluded.\n",
                 "We collected 1,203 builds from 48 projects."),
                ("We surveyed the media5 reports in the sample.\n",
                 "We surveyed the media reports in the sample.")):
            with self.subTest(quote=quote[:34]):
                self.assertIsNotNone(cea_claims.find_quote(quote, page))

    def test_the_refusal_says_which_digit_is_missing(self):
        """"copy the wording exactly from text.txt" is what the checker already did."""
        page = ("=== page 1 ===\nWe evaluated several models in the study.\n"
                "The top performers were deepseek-v3 (94.0%) and claude-3-sonnet (93.2%).\n")
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": "We evaluated several models in the study.", "page": 1,
            "section": "1 Introduction", "source": "abstract",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = [{
            "id": "R1", "page": 1, "section": "5 Results",
            "quote": "The top performers were deepseek-v (94.0%) and claude-3-sonnet (93.2%).",
            "reason": "B1 would still stand, because this only names the models."}]
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(page, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            said = "\n".join(cea_claims.validate(d)[0])
        self.assertIn("with a digit after it", said)
        self.assertNotIn("copy the wording exactly", said)


class EveryCommandSaysWhatItFound(unittest.TestCase):
    """`validate` printed the advisories and `render` and `site` printed none, so every check
    that is a warning rather than a refusal was invisible on the path that actually publishes: a
    page could go out carrying a title the paper does not print, and the command that wrote it
    said nothing."""

    def record(self, tmp):
        d = Path(tmp) / "rec"
        d.mkdir()
        (d / "text.txt").write_text(TEXT, encoding="utf-8")
        data = valid_claims()
        data["paper"]["title"] = "A Title This Paper Does Not Print Anywhere"
        (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        return d

    def test_validate_prints_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["validate", str(d)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("paper.title", out.getvalue())

    def test_render_prints_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["render", str(d)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("CEA_WARNING: paper.title", out.getvalue())

    def test_every_line_render_prints_carries_a_marker(self):
        """SKILL.md says the first output line starts with a marker, and a CEA_WARNING line may
        come first. A bare "warning:" line is neither, and fired on 16 of the 30 real records."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cea_claims.main(["render", str(d)])
        printed = [l for l in out.getvalue().splitlines() if l.strip()]
        self.assertTrue(printed)
        for line in printed:
            with self.subTest(line=line[:48]):
                self.assertTrue(line.startswith("CEA_"), f"no marker: {line[:70]}")

    def test_an_advisory_is_one_line_so_site_can_mark_each(self):
        """`site` wraps each advisory in one CEA_WARNING line, so an advisory holding a newline
        left a second line carrying no marker, which a `^CEA_` filter drops."""
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            data = json.loads((d / "claims.json").read_text(encoding="utf-8"))
            data.pop("format", None)
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            said = cea_claims.advisories(d, parsed)
        self.assertTrue(any("format" in w for w in said), "the unstamped advisory did not fire")
        for warning in said:
            with self.subTest(warning=warning[:48]):
                self.assertNotIn("\n", warning)

    def test_site_reports_them(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            d = self.record(tmp)
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([d], Path(tmp) / "_site")
        self.assertEqual(written, 1, "\n".join(messages))
        said = "\n".join(messages)
        self.assertIn("CEA_WARNING", said)
        self.assertIn("paper.title", said)

    def test_a_record_with_nothing_to_say_is_not_made_noisy(self):
        """The shared fixture's title is not printed in its own page text, so the title warning
        is right to fire on it. Given a title the page does hold, nothing is said about it."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "rec"
            d.mkdir()
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            data = valid_claims()
            data["paper"]["title"] = "Caching halves median build time"
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["render", str(d)])
            self.assertEqual(code, 0, out.getvalue())
            self.assertNotIn("warning: paper.title", out.getvalue())


class NoFieldMaySayOneThingAndShowAnother(unittest.TestCase):
    """A right-to-left override reverses the digits after it, so a note reading "19.2%" in the
    record displays as "2.91%" on the page, and a diff of the two records shows no difference.
    `note` is the field a reviewer reads to decide whether a claim was checked."""

    def problems(self, field, value):
        data = valid_claims()
        data["broad_statements"][0][field] = value
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return [p for p in cea_claims.validate(d)[0] if "show something other" in p]

    def test_a_bidi_override_is_refused(self):
        said = "\n".join(self.problems("note", "Table X prints the rate as \u202e19.2\u202c% here."))
        self.assertIn("U+202E", said)

    def test_a_zero_width_character_is_refused(self):
        self.assertTrue(self.problems("note", "The rate is 19\u200b.2% here."))

    def test_a_control_in_the_section_is_refused(self):
        self.assertTrue(self.problems("section", "VI\u200bI\u202e stiderC\u202c"))

    def test_the_paper_block_is_screened_too(self):
        data = valid_claims()
        data["paper"]["title"] = "A Study of \u202egnihtemoS\u202c"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            said = "\n".join(cea_claims.validate(d)[0])
        self.assertIn("paper.title", said)
        self.assertIn("U+202E", said)

    def test_ordinary_text_passes(self):
        self.assertEqual(self.problems("note", "No claim serves this: the paper gives no number."), [])

    def test_a_script_that_needs_a_joiner_is_not_refused(self):
        """Devanagari, Persian and Arabic need the zero-width joiner and non-joiner to render at
        all. Refusing every format character refused a paper's own title."""
        for name, text in (("Devanagari", "\u0915\u094d\u200d\u0937"),
                           ("Persian", "\u0645\u06cc\u200c\u0631\u0648\u062f"),
                           ("a soft hyphen from a PDF", "a soft\u00adhyphen")):
            with self.subTest(script=name):
                self.assertEqual(self.problems("note", f"The paper writes {text} here."), [])

    def test_every_shape_that_reorders_the_display_is_refused(self):
        for name, mark in (("right-to-left mark", "\u200f"), ("isolate", "\u2066"),
                           ("embedding", "\u202a"), ("zero-width space", "\u200b"),
                           ("byte order mark", "\ufeff"), ("a raw control", "\x07")):
            with self.subTest(mark=name):
                self.assertTrue(self.problems("note", f"a value {mark} here"))

    def test_no_real_field_holds_one(self):
        """The rule costs nothing: measured over every string in the workspace."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        held = seen = 0
        def walk(v):
            nonlocal held, seen
            if isinstance(v, str):
                seen += 1
                held += bool(cea_claims._unshowable(v))
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)
        for path in sorted(workspace.rglob("claims.json")):
            try:
                walk(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        if not seen:
            self.skipTest("no records to measure")
        self.assertEqual(held, 0, f"{held} of {seen} real fields hold an invisible control")


class ABorrowStandsWhereTheQuoteSaysNothing(unittest.TestCase):
    """The bracketed borrow was deleted before the order was read, so bracketing the one word
    that proves a `states` is out of order hid it -- and the escape added to make the order rule
    workable became the way past it. It also beat this suite's own swap test."""

    Q = ("In addition, 11 projects had a significant downward trend in file-based churn and "
         "15 had a significant downward trend in line-based churn.")
    Q2 = ("Overall, hunk-level review actions exhibit a higher addressing rate (6.5%-19.2%) "
          "compared to file-level actions (0.9%-4.2%).")

    def test_a_borrow_may_not_walk_a_value_onto_another_item(self):
        for quote, states in (
                (self.Q, "In addition, 11 projects had a significant downward trend in "
                         "[line]-based churn."),
                (self.Q2, "Overall, [file]-level actions exhibit a higher addressing rate "
                          "(6.5%-19.2%)"),
                (self.Q2, "compared to [hunk]-level actions (0.9%-4.2%)")):
            with self.subTest(states=states[:44]):
                self.assertTrue(cea_claims._out_of_quote_order(states, quote))

    def test_the_honest_borrow_still_passes(self):
        for quote, states in (
                (self.Q, "15 [projects] had a significant downward trend in line-based churn."),
                (self.Q2, "[hunk]-level actions exhibit a higher addressing rate (6.5%-19.2%)")):
            with self.subTest(states=states[:44]):
                self.assertFalse(cea_claims._out_of_quote_order(states, quote))

    def test_the_flanking_words_must_be_neighbours_in_the_quote(self):
        reading = cea_claims._word_run(self.Q)
        self.assertTrue(cea_claims._borrow_stands_in_a_gap(["15"], ["had"], reading))
        self.assertFalse(cea_claims._borrow_stands_in_a_gap(["in"], ["churn"], reading))

    def test_a_borrow_at_either_end_has_no_pair_to_check(self):
        reading = cea_claims._word_run(self.Q)
        self.assertTrue(cea_claims._borrow_stands_in_a_gap([], ["had"], reading))
        self.assertTrue(cea_claims._borrow_stands_in_a_gap(["15"], [], reading))

    def test_a_borrow_standing_as_its_own_word_is_not_held_to_adjacency(self):
        """A real sentence writes a qualifier after the number: "26 (file-based) and 30
        (line-based) repositories showed". Demanding the words flanking the borrow be immediate
        neighbours refused the part the reference tells the checker to write, and the refusal's
        own message told them to write the bracket they had written."""
        quote = ("We observed that only 26 (file-based) and 30 (line-based) repositories showed "
                 "significant code churn trends (p < 0.05).")
        for states in ("26 [repositories] showed significant code churn trends",
                       "30 [repositories] showed significant code churn trends"):
            with self.subTest(states=states[:40]):
                self.assertFalse(cea_claims._out_of_quote_order(states, quote))

    def test_what_the_borrow_rule_cannot_reach(self):
        """A borrow dropped where the quote genuinely says nothing can still carry a word from
        elsewhere in the sentence. The flanking words really are neighbours, so no rule over the
        two strings sees it; this is the same limit as the coordinated-sentence case."""
        self.assertFalse(cea_claims._out_of_quote_order(
            "15 [file-based] had a significant downward trend.", self.Q))


class ASharedSentenceIsCountedAsOne(unittest.TestCase):
    """`_stated_in` collapses a split group to one sentence and `_shared_with_other_results` did
    not, so an honest record printed "stated in 2 sentences (3 shared)" beside a main result --
    more sentences shared than the row says exist."""

    def counts(self, split):
        data = {"format": 1, "paper": {"id": "d", "title": "T", "pdf": "d.pdf", "pages": 1},
                "broad_statements": [
                    {"id": "B1", "quote": "Q1", "page": 1, "section": "A", "source": "rq_answer"},
                    {"id": "B2", "quote": "Q2", "page": 1, "section": "A", "source": "rq_answer"}],
                "claims": [],
                "rejected": [dict({"id": f"R{n}", "quote": "S", "page": 1, "section": "A",
                                   "reason": "r", "duplicate_of": ["B1", "B2"]},
                                  **({"split_from": "S1"} if split else {}))
                             for n in (1, 2, 3)]}
        return (cea_claims._stated_in(data, ["B1"]),
                cea_claims._shared_with_other_results(data, ["B1"]))

    def test_the_parts_of_one_sentence_are_one_shared_sentence(self):
        stated, shared = self.counts(True)
        self.assertEqual((stated, shared), (2, 1))
        self.assertLessEqual(shared, stated, "more sentences shared than the row says exist")

    def test_three_separate_sentences_are_three(self):
        self.assertEqual(self.counts(False), (4, 3))

    def test_no_real_row_says_more_shared_than_stated(self):
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        rows = 0
        for path in sorted(workspace.rglob("claims.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "broad_statements" not in data:
                continue
            for b in data["broad_statements"]:
                if not isinstance(b, dict) or "id" not in b:
                    continue
                rows += 1
                self.assertLessEqual(cea_claims._shared_with_other_results(data, [b["id"]]),
                                     cea_claims._stated_in(data, [b["id"]]),
                                     f"{path.parent.name} {b['id']}")
        if not rows:
            self.skipTest("no records to measure")


class OneSentenceCountsOnce(unittest.TestCase):
    """`stated in N sentences` is the page's measure of how firmly the paper commits to a result,
    it sorts the Claim Map, and the site index carries it. Counting each part of a split sentence
    separately let a record record one sentence as three parts of itself and publish eight where
    the paper states six."""

    def data(self, split):
        data = valid_claims()
        quote = data["rejected"][0]["quote"]
        base = {"quote": quote, "page": data["rejected"][0]["page"],
                "section": data["rejected"][0]["section"],
                "reason": "It repeats the result B1 states.", "duplicate_of": ["B1"]}
        data["rejected"] = [dict(base, id=f"R{n}", **({"split_from": "S9"} if split else {}))
                            for n in (90, 91, 92)]
        return data

    def test_three_parts_of_one_sentence_count_once(self):
        self.assertEqual(cea_claims._stated_in(self.data(True), ["B1"]), 2)

    def test_three_separate_sentences_count_three(self):
        self.assertEqual(cea_claims._stated_in(self.data(False), ["B1"]), 4)

    def test_no_real_count_changes(self):
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        checked = 0
        for path in sorted(workspace.rglob("claims.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or "broad_statements" not in data:
                continue
            for b in data["broad_statements"]:
                if not isinstance(b, dict) or "id" not in b:
                    continue
                loose = 1 + len({r.get("id", n) for n, r in enumerate(data.get("rejected", []))
                                 if isinstance(r, dict) and b["id"] in (r.get("duplicate_of") or [])})
                checked += 1
                self.assertEqual(cea_claims._stated_in(data, [b["id"]]), loose,
                                 f"{path.parent.name} {b['id']} changed count")
        if not checked:
            self.skipTest("no records to measure")


class TheSectionIsWhereTheQuoteStands(unittest.TestCase):
    """`section` is the page's statement of where in the paper a result stands. A sentence from
    Threats to Validity labelled as the conclusion published a self-defence with the authority of
    the paper's conclusion, and a reader checking the quote finds it verbatim and is reassured."""

    PAGE = ("=== page 1 ===\n"
            "VI. T HREATS TO VALIDITY\n"
            "Thus, our dataset captures the near-complete population of AI-based review activity.\n"
            "VIII. C ONCLUSION AND F UTURE W ORK\n"
            "We have shown that the tools differ widely in how their comments are received.\n")

    def warnings(self, section):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "page": 1, "section": section, "source": "conclusion",
            "quote": "Thus, our dataset captures the near-complete population of AI-based review "
                     "activity.",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(self.PAGE, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return [w for w in cea_claims.advisories(d, parsed) if ".section:" in w]

    def test_naming_another_section_of_the_paper_is_questioned(self):
        """The message names the heading the quote really stands under. Its small caps come out
        of the extraction split ("T HREATS"), so the letters are what is compared."""
        said = "\n".join(self.warnings("VIII Conclusion and Future Work"))
        self.assertIn("stands under", said)
        self.assertIn("threatstovalidity", re.sub(r"[^a-z]", "", said.casefold()))

    def test_naming_the_section_it_stands_under_is_not(self):
        self.assertEqual(self.warnings("VI Threats to Validity"), [])

    def test_a_number_the_paper_does_not_have_is_questioned(self):
        """Writing a roman-numbered paper's section as "8 Conclusion" walked past the word check,
        because no heading of that paper is a substring of "conclusion"."""
        said = "\n".join(self.warnings("8 Conclusion"))
        self.assertIn("has no section 8", said)

    def test_an_unnumbered_heading_has_no_number(self):
        """`[IVXLC]+` matched the first letter of an ordinary word, so "Limitations" was section
        "L" and warned while "Discussion" stayed silent -- arbitrary, and wrong on the very
        headings the reference says to write as printed."""
        for title in ("Limitations", "Conclusion", "Introduction", "Implications", "Validity",
                      "CCS Concepts", "Case Study", "Abstract", "Acknowledgment"):
            with self.subTest(title=title):
                self.assertEqual(cea_claims._top_number(title), "")

    def test_the_number_of_a_subsection_is_its_top_level_one(self):
        for title, want in (("IV-B Coding Procedure", "IV"), ("5.2 Results", "5"),
                            ("VIII. Conclusion", "VIII"), ("I Introduction", "I"),
                            ("3.2.1 Results by project", "3"), ("Abstract", "")):
            with self.subTest(title=title):
                self.assertEqual(cea_claims._top_number(title), want)

    def test_a_section_nested_in_it_is_not(self):
        """A subsection or a caption is named by its own number, which sits inside the one
        found: "6.1 RQ1" stands under "6 Discussion"."""
        self.assertTrue(cea_claims._nested(["6", "1"], ["6"]))
        self.assertTrue(cea_claims._nested(["6"], ["6", "1"]))
        self.assertFalse(cea_claims._nested(["VIII"], ["VI"]))

    def test_it_costs_at_most_one_warning_on_the_real_records(self):
        """Measured at 1 of 1,800 entries, a figure caption the reference expressly allows."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        fired = records = 0
        for path in sorted(workspace.rglob("claims.json")):
            record = path.parent
            if not (record / "text.txt").is_file():
                continue
            problems, parsed = cea_claims.validate(record)
            if problems:
                continue
            records += 1
            fired += len([w for w in cea_claims.advisories(record, parsed)
                          if ".section: the quote stands under" in w])
        if not records:
            self.skipTest("no valid records to measure")
        self.assertLessEqual(fired, 2, f"{fired} section warnings across {records} records")


class APartStatesItsOwnPartInTheQuotesOrder(unittest.TestCase):
    """`states` may use only the quote's words, and must use them in the quote's order.

    Order was checked once before and removed, because a plain subsequence test refused the honest
    second conjunct of a coordinated sentence -- it borrows the elided subject, which stands only
    in the first conjunct -- while accepting the swap. Measured afterwards: that shape occurs in
    no record, only in the fixture written to describe it, and all 29 `states` fields that are
    free to differ from their quote are already in the quote's order. What the first attempt
    lacked is the escape below: a word the quote elides is written in square brackets, as
    scholarship writes it, and is then not read as part of the order.
    """

    QUOTE = ("In addition, 11 projects had a significant downward trend in file-based churn and "
             "15 had a significant downward trend in line-based churn.")

    def problems(self, first, second):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": "Caching halves median build time.", "page": 1,
            "section": "Abstract", "source": "abstract"}]
        data["claims"] = [
            {"id": "C1", "quote": self.QUOTE, "states": first, "page": 1, "section": "5 Results",
             "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on this."},
            {"id": "C2", "quote": self.QUOTE, "states": second, "page": 1, "section": "5 Results",
             "serves": ["B1"], "split_from": "S1", "selection_reason": "B1 rests on this."}]
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(f"=== page 1 ===\nCaching halves median build time.\n"
                                        f"{self.QUOTE}\n", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return cea_claims.validate(d)[0]

    FIRST = "In addition, 11 projects had a significant downward trend in file-based churn."
    SECOND = "15 [projects] had a significant downward trend in line-based churn."

    def test_the_honest_parts_are_accepted(self):
        """The second borrows "projects" from the first, and says so with the bracket."""
        self.assertEqual(self.problems(self.FIRST, self.SECOND), [])

    def test_a_swap_between_the_conjuncts_is_refused(self):
        said = "\n".join(self.problems(
            "In addition, 11 projects had a significant downward trend in line-based churn.",
            "15 [projects] had a significant downward trend in file-based churn."))
        self.assertIn("in an order the quote does not", said)

    def test_an_unmarked_borrow_is_refused_and_says_what_to_do(self):
        said = "\n".join(self.problems(
            self.FIRST, "15 projects had a significant downward trend in line-based churn."))
        self.assertIn("in an order the quote does not", said)
        self.assertIn("square brackets", said)

    def test_a_second_bracket_does_not_launder_the_word_that_tells_them_apart(self):
        """A part fills at most one elided place. Bracketing a second word would take out the
        very word that tells one reading from another."""
        self.assertTrue(cea_claims._out_of_quote_order(
            "15 [projects] had a significant downward trend in [file]-based churn", self.QUOTE))

    def test_what_the_rule_cannot_reach_is_written_down(self):
        """A sentence's own words, in its own order, can still assert a falsehood: "[11] had a
        significant downward trend in line-based churn" walks the first conjunct's subject into
        the second conjunct's predicate, and both halves run forwards. No rule over the two
        strings refuses this and still accepts the real records -- the strictest conceivable one,
        "states must be a contiguous substring of the quote", refuses 18 of the 29. The pairing
        inside a coordinated sentence needs the paper, not the quote."""
        self.assertFalse(cea_claims._out_of_quote_order(
            "[11] had a significant downward trend in line-based churn", self.QUOTE))

    def test_a_word_the_quote_does_not_hold_is_still_refused(self):
        said = "\n".join(self.problems(
            "In addition, 11 repositories had a significant downward trend in file-based churn.",
            self.SECOND))
        self.assertIn("uses words that are not in the quote", said)

    def test_the_papers_negation_cannot_be_published_as_its_result(self):
        """"effectiveness varies widely" written as "effectiveness is growing widely"."""
        quote = "We found that while adoption is growing, its effectiveness varies widely."
        self.assertTrue(cea_claims._out_of_quote_order("effectiveness is growing widely", quote))
        for honest in ("its effectiveness varies widely", "adoption is growing"):
            with self.subTest(states=honest):
                self.assertFalse(cea_claims._out_of_quote_order(honest, quote))

    def test_every_real_states_is_in_its_quotes_order(self):
        """The rule costs nothing: measured across every record in the workspace."""
        workspace = SCRIPTS.parent / "skills" / "cea-extract-claims-workspace"
        free = refused = 0
        for path in sorted(workspace.rglob("claims.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            for key in ("broad_statements", "claims", "rejected"):
                for e in data.get(key, []):
                    if not isinstance(e, dict):
                        continue
                    states, quote = str(e.get("states", "")), str(e.get("quote", ""))
                    if not states or not quote:
                        continue
                    if key != "broad_statements" and not e.get("split_from"):
                        continue
                    if key == "broad_statements" and cea_claims._key(states) == cea_claims._key(quote):
                        continue
                    free += 1
                    refused += cea_claims._out_of_quote_order(states, quote)
        if not free:
            self.skipTest("no states field is free to differ from its quote")
        self.assertEqual(refused, 0, f"{refused} of {free} real states fields refused")


class AQuoteThatEndsMidWordAfterAGap(unittest.TestCase):
    """`_rest` recursed one past the end of `parts` when the last part matched but its outer edge
    was refused, so `parts[i]` indexed out of range. validate, render and site died with a
    traceback and none of the CEA_ markers the module docstring promises."""

    PAGE = ("These results indicate that more research is required here.\n"
            "TABLE VI  A caption that stands between the two halves\n"
            "Our taxonomy provides a multi-dimensional character-\n"
            "isation of the reasons we found.\n")

    def test_the_matcher_does_not_crash(self):
        quote = ("These results indicate that more research is required here. [...] "
                 "Our taxonomy provides a multi-dimensional character-")
        try:
            found = cea_claims.find_quote(quote, self.PAGE)
        except IndexError as e:
            self.fail(f"the matcher crashed instead of reporting no match: {e}")
        self.assertIsNone(found, "a quote ending inside a word is not a match")

    def test_the_commands_report_rather_than_crash(self):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "page": 1, "section": "5 Results", "source": "abstract",
            "quote": ("These results indicate that more research is required here. [...] "
                      "Our taxonomy provides a multi-dimensional character-"),
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(f"=== page 1 ===\n{self.PAGE}", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems = cea_claims.validate(d)[0]
        self.assertTrue(any(".quote" in p for p in problems),
                        "it has to say which quote it could not find")


class AVersionDigitIsNotAFootnote(unittest.TestCase):
    """A quote may leave off a footnote marker, and a single digit after a lowercase letter looked
    like one. "deepseek-v3" then matched as "deepseek-v": a model the paper never names, with no
    way for a reader to tell which model scored what."""

    PAGE = ("The top performers for Stage-1 were gpt-4.1 (94.5% average overall accuracy), "
            "deepseek-v3 (94.0%), and claude-3-sonnet (93.2%). Meanwhile deepseek-r1 led "
            "with 95.4%. We surveyed open-source3 projects in the sample.\n")

    def test_a_quote_may_not_drop_the_digit_of_a_name(self):
        for dropped in ("deepseek-v (94.0%)", "deepseek-r led with 95.4%"):
            with self.subTest(quote=dropped):
                self.assertIsNone(cea_claims.find_quote(dropped, self.PAGE),
                                  "the quote names a model the paper does not")

    def test_the_name_as_the_paper_prints_it_is_accepted(self):
        for whole in ("deepseek-v3 (94.0%)", "deepseek-r1 led with 95.4%", "gpt-4.1 (94.5%",
                      "claude-3-sonnet (93.2%)", "open-source3 projects in the sample"):
            with self.subTest(quote=whole):
                self.assertIsNotNone(cea_claims.find_quote(whole, self.PAGE))

    def test_an_ordinary_footnote_marker_may_still_be_left_off(self):
        """The exception the reference grants is unchanged where the word holds no hyphen."""
        page = "We collected 1,203 builds from 48 projects.2 The rest were excluded.\n"
        self.assertIsNotNone(
            cea_claims.find_quote("We collected 1,203 builds from 48 projects.", page))

    def test_the_rule_reads_the_whole_token(self):
        for text, at, want in (("deepseek-v3", 10, True), ("deepseek-coder2", 14, True),
                               ("projects2", 8, False), ("gpt-4", 4, True),
                               ("10-20", 4, True), ("media5", 5, False)):
            with self.subTest(text=text):
                self.assertEqual(cea_claims._names_a_version(text, at), want)


class TheRecordSaysWhichFormatItIsIn(unittest.TestCase):
    """A record written against an older format passed every check and published a number that
    meant something else. `duplicate_of` once covered what `breaks_down` covers now, so such a
    record counts too many places a result is stated and puts a headline on the page that is too
    high, with nothing anywhere saying so."""

    def problems(self, mutate=None):
        data = valid_claims()
        if mutate:
            mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return cea_claims.validate(d)[0]

    def test_a_record_without_the_stamp_is_read_as_the_first_format(self):
        """Refusing it stopped the published site's build dead the moment it moved to this
        version. Every record that can exist without the stamp was written before it, and the one
        change format 1 records shipped in v0.4.0, before every tag the site has ever pinned, so
        an unstamped record already has it."""
        self.assertEqual(self.problems(lambda d: d.pop("format")), [])

    def test_a_record_from_an_older_format_is_refused_and_told_what_changed(self):
        """FORMAT is 1 today, so the case is staged: a build at format 2 reading a record at 1."""
        changes = {**cea_claims._FORMAT_CHANGES, 2: "`page` became a range in every entry."}
        with unittest.mock.patch.object(cea_claims, "FORMAT", 2), \
                unittest.mock.patch.object(cea_claims, "_FORMAT_CHANGES", changes):
            said = "\n".join(cea_claims._format_problems({"format": 1}))
        self.assertIn("written in format 1", said)
        self.assertIn("this build writes 2", said)
        self.assertIn("became a range", said, "it has to say what changed since format 1")
        self.assertNotIn("breaks_down", said, "what changed at format 1 is already in the record")

    def test_a_record_from_a_newer_format_is_refused(self):
        said = "\n".join(self.problems(lambda d: d.update(format=cea_claims.FORMAT + 1)))
        self.assertIn("use the build that wrote it", said)

    def test_the_stamp_has_to_be_a_whole_number(self):
        for bad in ("1", 1.5, 0, -1, True, None, [1]):
            with self.subTest(format=bad):
                self.assertTrue(any(p.startswith("format:")
                                    for p in self.problems(lambda d, b=bad: d.update(format=b))),
                                f"{bad!r} must be refused")

    def test_the_current_stamp_passes(self):
        self.assertEqual(self.problems(), [])

    def test_the_schema_pins_the_stamp_without_requiring_it(self):
        s = cea_claims.schema()
        self.assertNotIn("format", s["required"],
                         "requiring it refuses every record written before the stamp")
        self.assertEqual(s["properties"]["format"]["const"], cea_claims.FORMAT)

    def test_every_format_since_the_first_says_what_changed(self):
        """The message is only worth printing if each raise recorded what it changed."""
        self.assertEqual(sorted(cea_claims._FORMAT_CHANGES), list(range(1, cea_claims.FORMAT + 1)))
        for n, what in cea_claims._FORMAT_CHANGES.items():
            self.assertGreater(len(what.split()), 8, f"format {n} says too little")


class WhatTheValidatorChecksOnAStatement(unittest.TestCase):
    """The reference promised these and the code did not do them."""

    QUOTE = ("Hunk-level review actions (6.5%-19.2%) show a higher addressing rate than "
             "file-level actions (0.9%-4.2%).")

    def warnings(self, states):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": self.QUOTE, "states": states, "page": 1, "section": "Abstract",
            "source": "abstract",
            "note": "No claim serves this statement: the paper gives no other sentence for it."}]
        data["claims"] = []
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(f"=== page 1 ===\n{self.QUOTE}\n", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return cea_claims.advisories(d, parsed)

    def test_a_statement_that_swaps_its_numbers_between_the_items_is_refused(self):
        """`states` is what the page prints as the main result, so swapping the two ranges
        reverses the finding on the published page. It was a warning, and warnings do not stop
        `render` or `site`; the order rule refuses it outright."""
        swapped = ("Hunk-level review actions (0.9%-4.2%) show a higher addressing rate than "
                   "file-level actions (6.5%-19.2%).")
        self.assertTrue(cea_claims._out_of_quote_order(swapped, self.QUOTE))

    def test_a_faithful_statement_is_not_questioned(self):
        """It differs from the quote by dropping a clause, and keeps its numbers in order."""
        self.assertEqual([w for w in self.warnings(
            "Hunk-level review actions (6.5%-19.2%) show a higher addressing rate.")
            if "order" in w], [])


class AQuoteThatIsARowOfATable(unittest.TestCase):
    """The reference says a number that stands only in a table is evidence for a claim and not a
    claim. Nothing checked it: a row copied out of a table is in text.txt, so the quote is found,
    and the lines around it end and start cleanly, so the sentence checks say nothing either."""

    PAGE = ("=== page 1 ===\n"
            "Caching halves median build time across every project.\n"
            "TABLE VIII  ADDRESSING RATES\n"
            "Action              Total    Share   Addressed   Rate\n"
            "File-level Action   3,137    100%    467         92.0%\n"
            "We measured 38 projects in total.\n")
    ROW = "File-level Action 3,137 100% 467 92.0%"
    PROSE = "We measured 38 projects in total."

    def warnings(self, key, quote):
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{
            "id": "B1", "quote": "Caching halves median build time across every project.",
            "page": 1, "section": "Abstract", "source": "abstract"}]
        entry = {"id": "C1", "quote": quote, "states": quote, "page": 1, "section": "5 Results",
                 "serves": ["B1"], "split_from": None,
                 "selection_reason": "B1 rests on this count."}
        if key == "rejected":
            entry = {"id": "R1", "quote": quote, "page": 1, "section": "5 Results",
                     "reason": "B1 would still stand, because this is a cell of the table."}
            data["claims"] = [{"id": "C1", "quote": self.PROSE, "states": self.PROSE, "page": 1,
                               "section": "5 Results", "serves": ["B1"], "split_from": None,
                               "selection_reason": "B1 rests on this count of projects."}]
            data["rejected"] = [entry]
        else:
            data["claims"] = [entry]
            data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(self.PAGE, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [])
            return [w for w in cea_claims.advisories(d, parsed) if "row of a table" in w]

    def test_a_claim_that_quotes_a_row_is_questioned(self):
        self.assertTrue(self.warnings("claims", self.ROW),
                        "a table row recorded as a claim went through with nothing said")

    def test_a_claim_that_quotes_a_sentence_is_not(self):
        self.assertEqual(self.warnings("claims", self.PROSE), [])

    def test_a_rejected_candidate_may_quote_a_row(self):
        """Setting a row aside is the rule being followed, not broken."""
        self.assertEqual(self.warnings("rejected", self.ROW), [])


class AGapLongerThanTheValidatorAllows(unittest.TestCase):
    """The message said the quote was not found and told the agent to do what it had already done,
    so the only way left to reach CEA_VALID was to cut the sentence short."""

    def problems(self, filler_chars):
        filler = "\n".join(f"Row {i}    12.5    34.7    a cell here    and another one here"
                            for i in range(filler_chars // 60))
        page = ("=== page 1 ===\nCaching halves median build time.\n"
                "We measured a median of 4.1 minutes\n" + filler +
                "\nacross every project in the corpus.\n")
        data = valid_claims()
        data["paper"]["pages"] = 1
        data["broad_statements"] = [{"id": "B1", "quote": "Caching halves median build time.",
                                     "page": 1, "section": "Abstract", "source": "abstract"}]
        quote = "We measured a median of 4.1 minutes [...] across every project in the corpus."
        data["claims"] = [{"id": "C1", "quote": quote, "page": 1, "section": "5 Results",
                           "states": "We measured a median of 4.1 minutes across every project "
                                     "in the corpus.",
                           "serves": ["B1"], "split_from": None,
                           "selection_reason": "B1 rests on this median."}]
        data["rejected"] = []
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(page, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            return [p for p in cea_claims.validate(d)[0] if ".quote" in p]

    def test_a_gap_inside_the_limit_is_accepted(self):
        self.assertEqual(self.problems(2000), [])

    def test_a_gap_over_the_limit_says_so_rather_than_saying_not_found(self):
        said = "\n".join(self.problems(8000))
        self.assertIn(f"more than the {cea_claims.MAX_GAP} characters", said)
        self.assertIn("stands unbroken", said, "the message has to give a remedy that works")
        self.assertNotIn("not found in text.txt", said)

    def test_the_limit_is_written_down_where_the_marker_is_explained(self):
        reference = (SCRIPTS.parent / "skills" / "extract-claims" / "references"
                     / "record-format.md").read_text(encoding="utf-8")
        self.assertIn(str(cea_claims.MAX_GAP), reference,
                      "a limit the validator enforces has to be in the reference")


class ASiteReportsWhatAReaderWouldHaveToSearchFor(unittest.TestCase):
    """The site skill says to report any paper whose page says that no narrow claim serves one of
    its main results. No command printed it: the record is valid, the page carries the heading,
    and the fact stood only inside the generated files."""

    def build(self, mutate=None):
        import cea_site
        data = valid_claims()
        if mutate:
            mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            self.assertEqual(written, 1, "\n".join(messages))
            return out.getvalue()

    def unserved(self, data):
        """A second main result, on its own sentence, that no claim serves."""
        data["broad_statements"].append(
            {"id": "B9", "quote": "Build failures are rare in general.", "page": 3,
             "section": "6 Discussion", "source": "conclusion",
             "note": "No claim serves this statement: the paper gives no sentence with a number "
                     "for it."})

    def test_the_build_names_the_statement_nothing_serves(self):
        said = self.build(self.unserved)
        self.assertIn("CEA_WARNING", said)
        self.assertIn("no narrow claim serves B9", said)

    def test_a_record_where_every_statement_is_served_says_nothing(self):
        self.assertNotIn("no narrow claim serves", self.build())


class ThePageSaysWhichBuildWroteIt(unittest.TestCase):
    """A record written against an older format can pass every check and still put a wrong number
    on a page. Nothing published said which build produced it, so there was no way to tell."""

    MANIFEST = SCRIPTS.parent / ".claude-plugin" / "plugin.json"

    def declared(self):
        if not self.MANIFEST.is_file():
            self.skipTest("the plugin manifest is not beside the scripts")
        return json.loads(self.MANIFEST.read_text(encoding="utf-8"))["version"]

    def test_the_footer_states_the_plugin_version(self):
        version = self.declared()
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertIn(f"(version {version})", html)

    def test_the_version_is_the_one_the_manifest_declares(self):
        self.assertEqual(cea_page.plugin_version(), self.declared())

    def test_a_manifest_that_cannot_be_read_does_not_stop_the_build(self):
        """A page without the version is worth more than no page at all."""
        with unittest.mock.patch.object(Path, "read_text", side_effect=OSError("gone")):
            self.assertEqual(cea_page.plugin_version(), "")

    def test_a_version_that_is_not_a_plain_version_is_not_printed(self):
        """It goes into the footer, so it cannot be a sentence, or markup, from the manifest."""
        for bad in ('{"version": "<script>x</script>"}', '{"version": 5}', '{"version": null}',
                    "not json at all", '["no object here"]'):
            with self.subTest(bad=bad[:30]):
                with unittest.mock.patch.object(Path, "read_text", return_value=bad):
                    self.assertEqual(cea_page.plugin_version(), "")


class TheMappingLevelSaysWhatItMeans(unittest.TestCase):
    """M1 over six empty circles is the page's own vocabulary. Its only gloss was a title
    attribute, which shows on hover and nowhere else."""

    def test_the_gloss_is_in_the_text_of_the_page(self):
        track = cea_page.chain_track()
        without_attributes = re.sub(r'\s\w+="[^"]*"', "", track)
        self.assertIn("none of the six links is reconstructed here", without_attributes)
        self.assertIn("M1", without_attributes)

    def test_every_claim_card_carries_it(self):
        """Counted against the cards, not the claims: a claim serving two broad statements gets
        a card under each."""
        data = valid_claims()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        cards = html.count('<div class="chain"')
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        self.assertGreaterEqual(cards, len(data["claims"]))
        self.assertEqual(text.count("none of the six links is reconstructed here"), cards)


class ARetractedRecordLeavesAPageStanding(unittest.TestCase):
    """A build that refuses writes nothing, so whatever was published last time stays up. When
    that is the page for the record just refused, the site goes on showing claims the record no
    longer supports, and "no site was written" reads as though nothing is wrong."""

    def build(self, mutate=None):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            data = valid_claims()
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, first = cea_site.build_site([rec], site)
            page = site / "papers" / data["paper"]["id"] / "index.html"
            if not (written and page.is_file()):
                self.fail(f"the first build published nothing: {chr(10).join(first)}")
            if mutate is None:
                return page, "", False
            mutate(data)
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 0)
            # Read inside the temporary directory: outside it nothing is a file any more.
            return page, "\n".join(messages), page.is_file()

    def retract(self, data):
        data["claims"][0]["selection_reason"] = "Unsure whether this is a narrow claim at all."

    def test_the_refusal_names_the_page_that_is_still_published(self):
        page, messages, standing = self.build(self.retract)
        self.assertIn("CEA_UNRESOLVED", messages)
        self.assertIn("CEA_FAILED: no site was written.", messages)
        self.assertIn(f"CEA_WARNING: {page} is still published", messages,
                      "the build refused and said nothing about the page still standing")
        self.assertTrue(standing, "the page is named, not deleted: the index still links it")

    def test_a_record_the_checks_refuse_is_named_the_same_way(self):
        """Not only a retraction. A quote that is no longer on the page leaves one standing too."""
        _page, messages, _standing = self.build(
            lambda d: d["claims"][0].update(quote="A sentence this paper never prints at all."))
        self.assertIn("CEA_INVALID", messages)
        self.assertIn("is still published", messages)

    def test_nothing_is_said_when_no_page_was_published_before(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            data = valid_claims()
            self.retract(data)
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                _written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
        self.assertNotIn("is still published", "\n".join(messages))


class AReaderWithoutScripting(unittest.TestCase):
    """Only the page's own script opens a body. A reader with scripting off, a text browser, a
    reader-mode view, or a crawler saw four fifths of the record hidden, including every reason
    a candidate was rejected. The page is a record, so all of it has to be readable."""

    HIDES = re.compile(r"(?s)<noscript>(.*?)</noscript>")

    def test_the_page_opens_every_body_when_scripting_is_off(self):
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        rules = self.HIDES.search(html)
        self.assertIsNotNone(rules, "the page carries no rules for a reader without scripting")
        for opened in (".entry-body { display: block; }",
                       "#candidates-body { display: block; }"):
            self.assertIn(opened, rules.group(1))

    def test_the_rules_come_after_the_ones_that_close_the_bodies(self):
        """Same specificity, so the later rule wins. Put first, it would do nothing at all."""
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertLess(html.index(".entry-body { display: none;"),
                        self.HIDES.search(html).start())

    def test_the_controls_that_would_do_nothing_are_taken_away(self):
        """A chevron that opens an already-open body, and a filter bar that filters nothing."""
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertIn(".filter-bar, .reveal { display: none; }", self.HIDES.search(html).group(1))

    def test_every_reason_and_every_candidate_is_reachable_with_scripting_off(self):
        """Measured on the page itself: each string the record holds has to survive removing the
        scripts and the subtrees that stay closed."""
        data = valid_claims()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        wanted = [c[k] for c in data["claims"] for k in ("selection_reason", "note") if c.get(k)]
        wanted += [b["note"] for b in data["broad_statements"] if b.get("note")]
        wanted += [r[k] for r in data["rejected"] for k in ("quote", "reason") if r.get(k)]
        self.assertTrue(wanted, "the fixture carries nothing to look for")
        body = re.sub(r"(?s)<script.*?</script>", "", html)
        text = re.sub(r"\s+", " ", unescape(re.sub(r"(?s)<[^>]+>", " ", body)))
        missing = [w for w in wanted if re.sub(r"\s+", " ", unescape(w))[:70] not in text]
        self.assertEqual(missing, [], f"{len(missing)} of {len(wanted)} strings are unreachable")


class PublishedFiles(unittest.TestCase):
    """What `site` copies beside a page, and what it must refuse to copy."""

    def build(self, mutate=None, layout=None):
        import cea_site
        data = valid_claims()
        if mutate:
            mutate(data)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = tmp / "rec"
        rec.mkdir()
        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
        (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        if layout:
            layout(tmp, rec)
        site = tmp / "_site"
        # build_site prints its warnings and returns its refusals, and a caller sees both
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            written, messages = cea_site.build_site([rec], site)
        return tmp, site, written, "\n".join(messages) + said.getvalue()

    def test_a_quote_that_is_not_in_the_paper_is_never_published(self):
        """`site` checked the record's shape and never opened text.txt.

        A quote the paper does not contain reached the page, the republished claims.md, and the
        index, beside the text.txt that refutes it, while `validate` on the same record exits 1.
        The site is the only thing a reader sees, so the check has to hold here too.
        """
        import cea_site
        made_up = "Caching triples median build time and doubles the failure rate."
        tmp, site, written, messages = self.build(
            lambda d: d["broad_statements"][0].update(quote=made_up))
        self.assertEqual(written, 0, "a record whose quote is not in the paper was published")
        self.assertIn("not found in text.txt", messages)
        self.assertFalse(site.exists(), "a refused build still wrote a site")

    def test_a_record_with_no_paper_text_is_refused(self):
        """text.txt is what every quote is checked against, and the site publishes it."""
        def drop(tmp, rec):
            (rec / "text.txt").unlink()
        tmp, site, written, messages = self.build(layout=drop)
        self.assertEqual(written, 0, "a record with no text.txt was published")
        self.assertIn("text.txt", messages)

    def test_a_publish_that_cannot_land_is_reported_rather_than_called_a_success(self):
        """copy2 onto a directory writes inside it, so the index landed one level down.

        The build said it had published a paper and the site root had no index page.
        """
        import cea_site
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = tmp / "rec"
        rec.mkdir()
        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
        (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
        site = tmp / "_site"
        (site / "index.html").mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            written, messages = cea_site.build_site([rec], site)
        self.assertEqual(written, 0, "a site with no index page was reported as published")
        self.assertIn("CEA_FAILED", "\n".join(messages))
        self.assertFalse((site / "index.html" / "index.html").exists())

    def test_a_symlink_in_the_record_does_not_publish_the_file_it_points_at(self):
        """`is_file()` follows a symlink, and copy2 then copies the target's contents.

        A record is contributed as a directory of files. A link in it named after the paper
        published whatever the build machine could read at the other end of it.
        """
        def layout(tmp, rec):
            (tmp / "secret.env").write_text("DEPLOY_KEY=abc123", encoding="utf-8")
            (rec / "fixture.pdf").symlink_to(Path("..") / "secret.env")
        tmp, site, written, messages = self.build(layout=layout)
        self.assertEqual(written, 1, messages)
        published = site / "papers" / "fixture" / "fixture.pdf"
        self.assertFalse(published.exists(),
                         "a file from outside the record was published beside the page")
        self.assertIn("no PDF found", messages)

    def test_the_paper_text_is_not_published_through_a_link(self):
        """The containment check covered the PDF. `text.txt` was copied one line above it.

        The page says every quote was checked against that text, and a link where it should be
        publishes whatever it points at, anywhere on the machine.
        """
        def layout(tmp, rec):
            (tmp / "private").mkdir()
            (tmp / "private" / "text.txt").write_text(TEXT + "EMBARGOED: outside the record.\n",
                                                      encoding="utf-8")
            (rec / "text.txt").unlink()
            (rec / "text.txt").symlink_to(tmp / "private" / "text.txt")

        tmp, site, written, messages = self.build(layout=layout)
        self.assertEqual(written, 0, "the paper text was published through a link")
        self.assertIn("leads outside", messages)
        self.assertFalse(any("EMBARGOED" in p.read_text(encoding="utf-8", errors="ignore")
                             for p in site.rglob("*") if p.is_file()) if site.exists() else False)

    def test_the_paper_may_not_name_a_file_outside_the_record(self):
        """The candidates used to include the working directory and the record's parent."""
        def layout(tmp, rec):
            (tmp / "elsewhere").mkdir()
            (tmp / "elsewhere" / "private.pdf").write_text("TOP SECRET", encoding="utf-8")
        tmp, site, written, messages = self.build(
            lambda d: d["paper"].update(pdf="elsewhere/private.pdf"), layout)
        # It is refused before publication now, rather than published without the file.
        self.assertEqual(written, 0, messages)
        self.assertIn("must be the file's name", messages)
        self.assertFalse(site.exists() and any(site.rglob("*.pdf")),
                         "nothing outside the record may reach the site")

    def test_the_paper_beside_the_record_is_still_published(self):
        """The guard must not cost the ordinary case: the PDF sits in the record directory."""
        tmp, site, written, messages = self.build(
            layout=lambda tmp, rec: (rec / "fixture.pdf").write_bytes(b"%PDF-1.4\n"))
        self.assertEqual(written, 1, messages)
        self.assertEqual((site / "papers" / "fixture" / "fixture.pdf").read_bytes(), b"%PDF-1.4\n")

    def test_a_paper_url_that_is_not_a_web_address_is_not_linked(self):
        """`site` does not call validate, so an unknown field used to reach the page's href."""
        tmp, site, written, messages = self.build(
            lambda d: d["paper"].update(url="javascript:fetch('https://evil.example/')"))
        self.assertEqual(written, 0, "a record with an unknown paper field was published")
        self.assertIn("unknown field", messages)
        # and the page itself refuses the scheme, whatever reaches it
        data = valid_claims()
        data["paper"]["url"] = "javascript:alert(1)"
        data["paper"]["pdf"] = "nowhere.pdf"
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        self.assertEqual([h for h in re.findall(r'href="([^"]*)"', html)
                          if not h.startswith(("#", "http", "claims.", "text.", "fixture."))], [],
                         "the page links a scheme a browser would run")

    def test_site_refuses_an_empty_reason_rather_than_publishing_a_card_without_one(self):
        """validate rejects an empty string. site checked the type and not the text."""
        tmp, site, written, messages = self.build(lambda d: d["rejected"][0].update(reason=""))
        self.assertEqual(written, 0)
        self.assertIn("rejected[0].reason", messages)

    def test_a_name_the_record_does_not_hold_is_not_turned_into_a_link(self):
        """The page mined every `B\\d+` out of a reason and made each one a tag and a link.

        A reason is prose. A paper about vitamin B12 says so in one, and the page asserted a main
        result "B12" behind a link that landed nowhere. Refusing the record instead is no good
        either: there is no other way to write that sentence.
        """
        for reason, label in (("Reports vitamin B12 levels, not a result here.", "real prose"),
                              ("Data only. B7 would still stand.", "a typo")):
            with self.subTest(reason=label):
                tmp, site, written, messages = self.build(
                    lambda d: d["rejected"][0].update(reason=reason))
                self.assertEqual(written, 1, f"the build refused a reason that is {label}: "
                                             f"{messages}")
                page = (site / "papers" / "fixture" / "index.html").read_text(encoding="utf-8")
                self.assertEqual(sorted(set(re.findall(r'class="idbadge" href="#(B\d+)"', page))),
                                 ["B1"], "the page links a result the record does not hold")

    def test_validate_asks_about_a_name_that_looks_like_an_id_but_is_not_one(self):
        """Passing over it is right for prose and wrong for a typo, so the checker is asked."""
        import cea_claims
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            data = valid_claims()
            data["rejected"][0]["reason"] = "Data only. B7 would still stand."
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, loaded = cea_claims.validate(d)
            self.assertEqual(problems, [], "a reason naming B7 is not itself invalid")
            self.assertIn("B7", "\n".join(cea_claims.advisories(d, loaded)))


class RecordEdges(unittest.TestCase):
    """Shapes that `validate` refuses and `site` used to publish, and encodings of the files."""

    def record(self, tmp, mutate=None, name="rec"):
        data = valid_claims()
        if mutate:
            mutate(data)
        rec = Path(tmp) / name
        rec.mkdir()
        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
        (rec / "claims.json").write_text(json.dumps(data, default=str), encoding="utf-8")
        return rec

    def test_a_surrogate_in_the_title_is_refused_before_it_truncates_the_markdown(self):
        """validate accepted it and render then raised mid-write, leaving claims.md empty."""
        data = valid_claims()
        data["paper"]["title"] = "Fix\ud800ture"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            (d / "claims.md").write_text("previous good content", encoding="utf-8")
            problems, _ = cea_claims.validate(d)
            self.assertTrue(any("surrogate" in p for p in problems), problems)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cea_claims.main(["render", str(d)]), 1)
            self.assertNotEqual((d / "claims.md").read_text(encoding="utf-8"), "",
                                "a refused record must not truncate the Markdown")

    def test_a_surrogate_in_a_field_name_is_reported_not_raised(self):
        """The problem list itself could not be printed, so validate crashed on its own guard."""
        for where, data in (("top level", {"extr\ud800a": 1}),
                            ("an entry", None)):
            with self.subTest(where=where):
                record = valid_claims()
                if data is None:
                    record["broad_statements"][0]["zz\ud800"] = "x"
                else:
                    record.update(data)
                with tempfile.TemporaryDirectory() as tmp:
                    d = Path(tmp)
                    (d / "text.txt").write_text(TEXT, encoding="utf-8")
                    (d / "claims.json").write_text(json.dumps(record), encoding="utf-8")
                    problems, _ = cea_claims.validate(d)
                self.assertTrue(problems, "the unknown field must be reported")
                for problem in problems:
                    with self.subTest(problem=problem[:40]):
                        problem.encode("utf-8")   # printing it must not raise
                if where == "an entry":
                    self.assertTrue(any("surrogate" in p for p in problems),
                                    f"say why the name is wrong, not only that it is: {problems}")

    def test_the_site_refuses_ids_that_name_nothing_or_repeat(self):
        import cea_site
        cases = {
            "serves names a claim": lambda d: d["claims"][1].__setitem__("serves", ["C1"]),
            "serves names nothing": lambda d: d["claims"][1].__setitem__("serves", ["B9"]),
            "breaks_down names nothing": lambda d: d["rejected"][0].__setitem__("breaks_down", ["B9"]),
            "duplicate_of names nothing": lambda d: d["rejected"][0].__setitem__("duplicate_of", ["C9"]),
            "two entries share an id": lambda d: d["broad_statements"].append(
                dict(d["broad_statements"][0])),
        }
        for label, mutate in cases.items():
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, mutate)
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0, f"{label} must be refused")
                    self.assertTrue(cea_claims.validate(rec)[0], "validate refuses it too")

    def test_a_byte_order_mark_in_the_text_is_read_away(self):
        """text.txt was the one reader left on utf-8, so a BOM hid the first page marker."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8-sig")
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            self.assertEqual(cea_claims.validate(d)[0], [],
                             "a BOM must not be read as part of the first page marker")

    def test_an_absurd_page_marker_is_reported_against_the_right_file(self):
        """The last unbounded int() blamed claims.json for a bad text.txt."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text("=== page " + "9" * 5000 + " ===\nx\n", encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            problems, _ = cea_claims.validate(d)
            self.assertTrue(problems)
            self.assertNotIn("claims.json is not valid JSON", "\n".join(problems),
                             "text.txt is at fault, not claims.json")


class CheckEnv(unittest.TestCase):
    """`check-env` is the first command the skill runs, and had no test at all.

    SKILL.md tells the agent to stop when it fails, so a wrong comparison here halts the skill at
    step one on every supported Python.
    """

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cea_claims.main(list(args))
        return code, out.getvalue()

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is not installed")
    def test_a_supported_environment_passes(self):
        code, printed = self.run_cli("check-env")
        self.assertEqual(code, 0, printed)
        self.assertTrue(printed.startswith("CEA_OK:"), printed)
        self.assertIn(platform.python_version(), printed)

    def test_a_missing_pdftotext_is_reported(self):
        with unittest.mock.patch.object(cea_claims.shutil, "which", lambda _name: None):
            code, printed = self.run_cli("check-env")
        self.assertEqual(code, 3)
        self.assertIn("CEA_FAILED", printed)
        self.assertIn("poppler", printed)

    def test_a_pdftotext_that_cannot_answer_is_reported(self):
        """A broken poppler was green-lit, or crashed the skill's very first step."""
        class Answer:
            def __init__(self, code):
                self.returncode, self.stdout, self.stderr = code, "", ""

        with unittest.mock.patch.object(cea_claims.shutil, "which", lambda _n: "/usr/bin/pdftotext"), \
                unittest.mock.patch.object(cea_claims.subprocess, "run", lambda *a, **k: Answer(9)):
            code, printed = self.run_cli("check-env")
        self.assertEqual(code, 3)
        self.assertIn("CEA_FAILED", printed)

    def test_a_pdftotext_that_cannot_be_run_is_reported(self):
        def boom(*_a, **_k):
            raise OSError(8, "Exec format error")

        with unittest.mock.patch.object(cea_claims.shutil, "which", lambda _n: "/usr/bin/pdftotext"), \
                unittest.mock.patch.object(cea_claims.subprocess, "run", boom):
            code, printed = self.run_cli("check-env")
        self.assertEqual(code, 3)
        self.assertIn("CEA_FAILED", printed)
        self.assertIn("cannot be run", printed)

    def test_an_old_python_is_reported(self):
        with unittest.mock.patch.object(cea_claims.sys, "version_info", (3, 9, 0)):
            code, printed = self.run_cli("check-env")
        self.assertEqual(code, 3)
        self.assertIn("3.10 or newer", printed)


class ExtractGuards(unittest.TestCase):
    """The extract guards, without a real PDF.

    Every one of these needed `pdftotext` and a paper in `evals/papers/`, which is gitignored, so
    none of them has ever run in CI. `cmd_extract` reaches the extractor only through
    `pdf_text.extract`, so patching that one function exercises the guards end to end while
    `to_text` stays real and the text comparison is a real comparison.
    """

    def fake(self, body="A sentence on the page."):
        return lambda _path: pdf_text.Extraction(
            pages=[pdf_text.Page(1, [body] * 30)], lineno=False, references=None)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cea_claims.main(list(args))
        return code, out.getvalue()

    def test_a_paper_extracts_and_re_extracts(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", self.fake()):
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            out = Path(tmp) / "out"
            self.assertEqual(self.run_cli("extract", str(pdf), "--out", str(out))[0], 0)
            self.assertTrue((out / "paper" / "text.txt").is_file())
            (out / "paper" / "claims.json").write_text('{"paper": {"pdf": "paper.pdf"}}',
                                                       encoding="utf-8")
            self.assertEqual(self.run_cli("extract", str(pdf), "--out", str(out))[0], 0,
                             "the same paper must re-extract")

    def test_a_different_paper_under_the_same_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            out = Path(tmp) / "out"
            with unittest.mock.patch.object(pdf_text, "extract", self.fake("First paper text.")):
                self.assertEqual(self.run_cli("extract", str(pdf), "--out", str(out))[0], 0)
            (out / "paper" / "claims.json").write_text('{"paper": {"pdf": "paper.pdf"}}',
                                                       encoding="utf-8")
            before = (out / "paper" / "text.txt").read_text(encoding="utf-8")
            with unittest.mock.patch.object(pdf_text, "extract", self.fake("Second paper text.")):
                code, printed = self.run_cli("extract", str(pdf), "--out", str(out))
            self.assertEqual(code, 2, printed)
            self.assertIn("not what this PDF", printed)
            self.assertEqual((out / "paper" / "text.txt").read_text(encoding="utf-8"), before)

    def test_a_record_beside_no_text_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", self.fake()):
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            out = Path(tmp) / "out"
            (out / "paper").mkdir(parents=True)
            (out / "paper" / "claims.json").write_text("{}", encoding="utf-8")
            code, printed = self.run_cli("extract", str(pdf), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("no text.txt", printed)

    def test_an_unreadable_text_is_reported_not_blamed_on_another_paper(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", self.fake()):
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            out = Path(tmp) / "out"
            (out / "paper").mkdir(parents=True)
            (out / "paper" / "claims.json").write_text("{}", encoding="utf-8")
            (out / "paper" / "text.txt").write_bytes(b"\xff\xfe not utf 8")
            code, printed = self.run_cli("extract", str(pdf), "--out", str(out))
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", printed)

    def test_a_publisher_file_name_is_folded_and_announced(self):
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", self.fake()):
            pdf = Path(tmp) / "Smith et al. (2024).pdf"
            pdf.write_bytes(b"%PDF-1.4")
            out = Path(tmp) / "out"
            code, printed = self.run_cli("extract", str(pdf), "--out", str(out))
            self.assertEqual(code, 0, printed)
            self.assertIn("CEA_WARNING", printed)
            self.assertIn("paper_id: Smith-et-al.-2024", printed)
            self.assertTrue((out / "Smith-et-al.-2024" / "text.txt").is_file())

    def test_an_extractor_failure_is_reported_not_raised(self):
        def boom(_path):
            raise RuntimeError("pdftotext died")

        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", boom):
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            code, printed = self.run_cli("extract", str(pdf), "--out", str(Path(tmp) / "out"))
        self.assertEqual(code, 3)
        self.assertIn("CEA_FAILED", printed)
        self.assertIn("pdftotext died", printed)

    def test_a_pdf_with_no_usable_text_is_refused(self):
        empty = lambda _p: pdf_text.Extraction(pages=[pdf_text.Page(1, [])], lineno=False,
                                               references=None)
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(pdf_text, "extract", empty):
            pdf = Path(tmp) / "scan.pdf"
            pdf.write_bytes(b"%PDF-1.4")
            code, printed = self.run_cli("extract", str(pdf), "--out", str(Path(tmp) / "out"))
        self.assertEqual(code, 3)
        self.assertIn("no usable text", printed)


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

    @unittest.skipUnless(shutil.which("pdftotext") and any(PAPERS.glob("*.pdf")),
                         "pdftotext or the test papers are missing")
    def test_a_publisher_file_name_folds_to_a_usable_id(self):
        """extract used to accept ids that validate then rejected, after the whole paper was read."""
        source = next(iter(sorted(PAPERS.glob("*.pdf"))))
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "Smith et al. (2024).pdf"
            shutil.copy2(source, named)
            code, out = self.run_cli("extract", str(named), "--out", tmp)
            self.assertEqual(code, 0, out)
            self.assertIn("paper_id: Smith-et-al.-2024", out)
            self.assertTrue((Path(tmp) / "Smith-et-al.-2024" / "text.txt").is_file())
            self.assertTrue(cea_claims.PAPER_ID.fullmatch("Smith-et-al.-2024"))

    @unittest.skipUnless(shutil.which("pdftotext") and any(PAPERS.glob("*.pdf")),
                         "pdftotext or the test papers are missing")
    def test_two_papers_that_fold_to_one_id_do_not_overwrite(self):
        """Two different papers can reach one id. The second must not replace the first's text."""
        sources = sorted(PAPERS.glob("*.pdf"))
        if len(sources) < 2:
            self.skipTest("two test papers are needed")
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "Smith et al. (2024).pdf"
            second = Path(tmp) / "Smith et al. 2024.pdf"
            shutil.copy2(sources[0], first)
            shutil.copy2(sources[1], second)
            out_dir = Path(tmp) / "out"
            self.assertEqual(self.run_cli("extract", str(first), "--out", str(out_dir))[0], 0)
            record = out_dir / "Smith-et-al.-2024"
            before = (record / "text.txt").read_text(encoding="utf-8")
            (record / "claims.json").write_text(
                json.dumps({"paper": {"pdf": "Smith et al. (2024).pdf"}}), encoding="utf-8")
            code, out = self.run_cli("extract", str(second), "--out", str(out_dir))
            self.assertEqual(code, 2, out)
            self.assertIn("another paper", out)
            self.assertEqual((record / "text.txt").read_text(encoding="utf-8"), before,
                             "the first paper's text must survive")

    @unittest.skipUnless(shutil.which("pdftotext") and any(PAPERS.glob("*.pdf")),
                         "pdftotext or the test papers are missing")
    def test_re_extracting_the_same_paper_is_allowed(self):
        source = next(iter(sorted(PAPERS.glob("*.pdf"))))
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "paper.pdf"
            shutil.copy2(source, named)
            out_dir = Path(tmp) / "out"
            self.assertEqual(self.run_cli("extract", str(named), "--out", str(out_dir))[0], 0)
            (out_dir / "paper" / "claims.json").write_text(
                json.dumps({"paper": {"pdf": "paper.pdf"}}), encoding="utf-8")
            code, out = self.run_cli("extract", str(named), "--out", str(out_dir))
            self.assertEqual(code, 0, out)

    def test_extract_rejects_a_missing_file_and_an_unsafe_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = self.run_cli("extract", str(Path(tmp) / "missing.pdf"), "--out", tmp)
            self.assertEqual(code, 2)
            self.assertIn("no such file", out)
            code, out = self.run_cli("extract", str(Path(tmp) / "missing.pdf"), "--out", tmp, "--id", "../x")
            self.assertEqual(code, 2)
            self.assertIn("invalid paper id", out)

    @unittest.skipUnless(shutil.which("pdftotext") and (PAPERS / "ieeesw26-ai-slop.pdf").is_file(),
                         "pdftotext or the test paper is missing")
    def test_extract_reports_an_output_path_that_is_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "afile"
            target.write_text("x")
            code, out = self.run_cli("extract", str(PAPERS / "ieeesw26-ai-slop.pdf"), "--out", str(target))
            self.assertEqual(code, 2)
            self.assertIn("cannot write", out)



class Respectively(unittest.TestCase):
    """The pairing warning stands until the record says how the pairing goes."""

    def record(self, note_first=None, note_second=None):
        data = valid_claims()
        quote = ("Across the 48 projects, median build time and the failure rate were 4.1 minutes "
                 "and 3%, respectively.")
        data["claims"][0].update(quote=quote, states="Across the 48 projects, median build time was 4.1 minutes.",
                                 note=note_first)
        data["claims"][1].update(quote=quote, states="Across the 48 projects, the failure rate was 3%.",
                                 note=note_second)
        return data

    def warnings(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            return "\n".join(cea_claims.advisories(d, data))

    def test_it_warns_while_no_note_says_how_the_pairing_goes(self):
        self.assertIn("respectively", self.warnings(self.record()))

    def test_one_note_is_not_enough(self):
        data = self.record(note_first="The sentence pairs them by \"respectively\".")
        self.assertIn("respectively", self.warnings(data))

    def test_it_is_silent_once_every_part_records_the_pairing(self):
        """A note records the pairing by naming the number its own part states."""
        data = self.record(note_first="The 4.1 minutes is the build time, the first of the pair.",
                           note_second="The 3% is the failure rate, the second of the pair.")
        self.assertNotIn("pairs items and numbers", self.warnings(data))

    def test_saying_the_pairing_could_not_be_worked_out_does_not_clear_it(self):
        """The word "respectively" used to clear it, whatever the note said around it."""
        said = "The sentence says \"respectively\" but which number goes with which is unclear."
        data = self.record(note_first=said, note_second=said)
        self.assertIn("pairs items and numbers", self.warnings(data))

    def test_naming_where_a_table_stands_does_not_clear_it(self):
        data = self.record(note_first="See Table 3 on page 41 for the per-project figures.",
                           note_second="See Table 3 on page 41 for the per-project figures.")
        self.assertIn("pairs items and numbers", self.warnings(data))


class Framework(unittest.TestCase):
    """The framework page, rendered from Markdown."""

    def render(self, md: str) -> str:
        sys.path.insert(0, str(SCRIPTS))
        import cea_site
        with bounded():
            return cea_site.md_to_html(md)

    def test_an_html_comment_is_not_published(self):
        html = self.render("# Title\n\nVisible.\n\n<!--\nGROUNDING: a quote a reader must not see.\n-->\n")
        self.assertIn("Visible.", html)
        self.assertNotIn("GROUNDING", html)

    def test_a_table_survives_a_fence_above_it(self):
        html = self.render("```\ncode\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<table>", html)
        self.assertIn("<pre><code>code</code></pre>", html)


class Page(unittest.TestCase):
    """The HTML page and the site that `render` and `site` write."""

    def paper(self, parent: Path, paper_id: str, data=None) -> Path:
        d = parent / paper_id
        d.mkdir()
        data = data or valid_claims()
        data["paper"] = dict(data["paper"], id=paper_id)
        (d / "text.txt").write_text(TEXT, encoding="utf-8")
        (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
        return d

    def run_command(self, *argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cea_claims.main(list(argv))
        return code, out.getvalue()

    def test_render_writes_the_page_beside_the_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "fixture")
            code, out = self.run_command("render", str(d))
            self.assertEqual(code, 0, out)
            page = (d / "claims.html").read_text(encoding="utf-8")
            self.assertIn('id="B1"', page)
            self.assertIn('id="C2"', page)
            self.assertIn("Caching halves median build time.", page)
            self.assertIn("claims.html", out)

    def test_the_page_carries_the_record_it_was_built_from(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "fixture")
            self.run_command("render", str(d))
            page = (d / "claims.html").read_text(encoding="utf-8")
            blob = page.split('id="cea-data">')[1].split("</script>")[0]
            self.assertEqual(json.loads(blob.replace("<\\/", "</"))["paper"]["id"], "fixture")

    def test_an_unsettled_record_writes_no_page(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = ("Unsure whether B1 rests on this, recorded as rejected so "
                                         "that the checker can change it into a claim.")
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "fixture", data)
            code, out = self.run_command("render", str(d))
            self.assertEqual(code, 1)
            self.assertIn("CEA_UNRESOLVED", out)
            self.assertIn("R1", out)
            self.assertFalse((d / "claims.html").exists())
            self.assertTrue((d / "claims.md").exists(), "the checker still needs the Markdown")

    def test_site_writes_one_directory_per_paper_and_an_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = self.paper(root, "one"), self.paper(root, "two")
            code, out = self.run_command("site", str(first), str(second), "--out", str(root / "site"))
            self.assertEqual(code, 0, out)
            index = (root / "site" / "index.html").read_text(encoding="utf-8")
            self.assertIn("papers/one/", index)
            self.assertIn("papers/two/", index)
            for paper_id in ("one", "two"):
                here = root / "site" / "papers" / paper_id
                self.assertTrue((here / "index.html").is_file())
                self.assertTrue((here / "claims.json").is_file())
                self.assertTrue((here / "text.txt").is_file())

    def test_only_a_page_inside_a_site_links_back_to_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = self.paper(root, "one")
            self.run_command("render", str(d))
            self.assertNotIn('<a class="nav-home"', (d / "claims.html").read_text(encoding="utf-8"))
            self.run_command("site", str(d), "--out", str(root / "site"))
            page = (root / "site" / "papers" / "one" / "index.html").read_text(encoding="utf-8")
            self.assertIn('<a class="nav-home" href="../../">', page)

    def test_site_writes_nothing_while_one_record_is_unsettled(self):
        data = valid_claims()
        data["rejected"][0]["reason"] = "Unsure whether B1 rests on this."
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settled, unsettled = self.paper(root, "one"), self.paper(root, "two", data)
            code, out = self.run_command("site", str(settled), str(unsettled), "--out", str(root / "site"))
            self.assertEqual(code, 1)
            self.assertIn("CEA_UNRESOLVED", out)
            self.assertFalse((root / "site").exists(), "a half-built site must not reach a server")

    def test_a_main_result_no_claim_serves_is_invalid_without_a_note(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion"})
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "fixture", data)
            code, out = self.run_command("render", str(d))
            self.assertEqual(code, 1)
            self.assertIn("CEA_INVALID", out)
            self.assertFalse((d / "claims.html").exists())

    def test_a_noted_main_result_no_claim_serves_is_shown_as_a_finding(self):
        data = valid_claims()
        data["broad_statements"].append(
            {"id": "B2", "quote": "Build failures are rare in general.", "page": 3,
             "section": "7 Conclusion", "source": "conclusion",
             "note": "The paper reports no failure rate for projects without caching."})
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "fixture", data)
            code, out = self.run_command("render", str(d))
            self.assertEqual(code, 0, out)
            page = (d / "claims.html").read_text(encoding="utf-8")
            self.assertIn("that no narrow claim serves", page)
            self.assertIn("The paper reports no failure rate", page)

    def test_a_paper_without_a_quantitative_main_result_says_so(self):
        data = valid_claims()
        data["broad_statements"], data["claims"] = [], []
        data["rejected"][0]["reason"] = "Describes the data, not a result."
        with tempfile.TemporaryDirectory() as tmp:
            d = self.paper(Path(tmp), "qualitative", data)
            code, out = self.run_command("render", str(d))
            self.assertEqual(code, 0, out)
            page = (d / "claims.html").read_text(encoding="utf-8")
            # The record's own marking, not a claim about the paper: a record whose broad
            # statements were all demoted made the page assert that a paper with a quantitative
            # abstract states no quantitative result.
            self.assertIn("marks no sentence of this paper as stating a quantitative main "
                          "result", page)
            self.assertNotIn("This paper states no quantitative", page)


REPO_ROOT = SCRIPTS.parent


class Schema(unittest.TestCase):
    """The JSON Schema, the validator, and the reference doc name the same fields."""

    REPO = SCRIPTS.parent
    REFERENCE = REPO / "skills" / "extract-claims" / "references" / "record-format.md"

    def test_the_committed_schema_matches_the_code(self):
        committed = json.loads((self.REPO / "claims.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, cea_claims.schema(),
                         "run: python3 scripts/cea_claims.py schema --out claims.schema.json")

    def test_every_field_has_a_type(self):
        fields = set().union(*(r | o for r, o in cea_claims.FIELDS.values()))
        self.assertEqual(fields - set(cea_claims.PROPERTIES), set(),
                         "a field in FIELDS needs an entry in PROPERTIES")
        # An empty entry validates anything, so require a real constraint, not just a key.
        vacuous = sorted(f for f in fields
                         if not {"type", "enum", "anyOf", "$ref"} & set(cea_claims.PROPERTIES[f]))
        self.assertEqual(vacuous, [], "these PROPERTIES entries constrain nothing")

    def test_the_reference_documents_only_real_fields(self):
        import re
        # `format` stands at the top of the record rather than inside an entry, so FIELDS, which
        # names the fields of the entries, does not hold it.
        fields = set().union(*(r | o for r, o in cea_claims.FIELDS.values())) | {"format"}
        text = self.REFERENCE.read_text(encoding="utf-8")
        self.assertIn("## Fields", text, "the reference has no Fields section")
        body = text.split("## Fields", 1)[1].split("\n## ")[0]
        documented = {m.split(".")[-1] for m in re.findall(r"^- `([^`]+)`", body, re.M)}
        self.assertTrue(documented, "the reference's Fields section lists no field")
        self.assertEqual(documented - fields, set(),
                         "the reference documents a field that claims.json does not hold")

    def test_a_record_may_name_its_schema(self):
        data = valid_claims()
        data["$schema"] = "../../claims.schema.json"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, _ = cea_claims.validate(d)
        self.assertEqual(problems, [])

    def test_a_dot_file_is_not_an_orphaned_eval_case(self):
        """Finder drops .DS_Store into any folder it shows, which failed the whole suite."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import gen_plugin_evals
        except ImportError as e:  # a checkout that does not hold the generator
            self.skipTest(f"gen_plugin_evals is not importable: {e}")
        if not (REPO_ROOT / "evals").is_dir():
            self.skipTest("the generated eval cases are not present")
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "evals"
            shutil.copytree(REPO_ROOT / "evals", out_dir,
                            ignore=shutil.ignore_patterns("papers", "results"))
            graders = sorted(out_dir.glob("*/graders"))
            if not graders:
                self.skipTest("no generated case directories to check")
            (graders[0] / ".DS_Store").write_text("", encoding="utf-8")
            kept = (gen_plugin_evals.OUT, gen_plugin_evals.REPO)
            gen_plugin_evals.OUT, gen_plugin_evals.REPO = out_dir, Path(tmp)
            try:
                with contextlib.redirect_stdout(io.StringIO()) as printed:
                    code = gen_plugin_evals.main(["--check"])
            finally:
                gen_plugin_evals.OUT, gen_plugin_evals.REPO = kept
        self.assertEqual(code, 0, printed.getvalue())

    def test_the_generated_eval_cases_are_current(self):
        """`claude plugin eval` cases are generated from the skill-creator evals.json."""
        sys.path.insert(0, str(SCRIPTS))
        try:
            import gen_plugin_evals
        except ImportError as e:  # a checkout that does not hold the generator
            self.skipTest(f"gen_plugin_evals is not importable: {e}")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = gen_plugin_evals.main(["--check"])
        self.assertEqual(code, 0, out.getvalue())

    def test_schema_prints_only_json_to_stdout(self):
        """check.sh pipes this, so the marker has to stay on stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cea_claims.main(["schema"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue()), cea_claims.schema())
        self.assertIn("CEA_SCHEMA", err.getvalue())

    def test_schema_command_writes_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "s.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = cea_claims.main(["schema", "--out", str(out)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out.read_text()), cea_claims.schema())


def _frontmatter(path: Path) -> dict:
    """The YAML frontmatter of a SKILL.md, as far as the Agent Skills spec allows it to go.

    A hand-written parser, because the scripts use the standard library only. It reads `key: value`
    and the folded `key: >-` block that a long description needs, which is all a SKILL.md holds.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        raise AssertionError(f"{path}: no YAML frontmatter")
    fields, key = {}, None
    for line in lines[1:lines.index("---", 1)]:
        if line.startswith("  ") and key:
            fields[key] += (" " if fields[key] else "") + line.strip()
        elif ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            fields[key] = "" if value in (">-", ">", "|", "|-") else value
        else:
            raise AssertionError(f"{path}: frontmatter line {line!r} is not YAML this can read")
    return fields


class Skills(unittest.TestCase):
    """Every SKILL.md keeps to the Agent Skills spec (https://agentskills.io/specification).

    These run in the repository's own suite, so a change that breaks the spec fails here rather
    than waiting for `skills-ref validate` or a plugin install.
    """

    PLUGIN = SCRIPTS.parent
    # name and description are required. The spec allows only these keys.
    ALLOWED = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    # ~5000 tokens, the spec's recommended ceiling for the body an agent loads on activation.
    MAX_BODY = 20_000

    def skills(self):
        return sorted((self.PLUGIN / "skills").glob("*/SKILL.md"))

    def test_there_are_skills_to_check(self):
        self.assertTrue(self.skills())

    def test_frontmatter_keeps_to_the_spec(self):
        import re
        for path in self.skills():
            with self.subTest(skill=path.parent.name):
                f = _frontmatter(path)
                self.assertEqual(set(f) - self.ALLOWED, set(), "unknown frontmatter field")
                name = f.get("name", "")
                self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertLessEqual(len(name), 64)
                self.assertEqual(name, path.parent.name, "name must match its directory")
                self.assertTrue(1 <= len(f.get("description", "")) <= 1024,
                                f"description is {len(f.get('description', ''))} characters, "
                                "and the spec allows 1 to 1024")
                self.assertLessEqual(len(f.get("compatibility", "")), 500)

    def test_the_description_is_measured_the_way_yaml_reads_it(self):
        """The parser above joins folded lines with one space. YAML does not always agree.

        A trailing space, a `>` that keeps the final newline, or a continuation line at a
        different indent all make real YAML longer than the count here, so the character limit
        would be checked against the wrong number. Rule those out rather than parse YAML, which
        the scripts cannot import.
        """
        for path in self.skills():
            with self.subTest(skill=path.parent.name):
                lines = path.read_text(encoding="utf-8").split("\n")
                front = lines[1:lines.index("---", 1)]
                for n, line in enumerate(front, start=2):
                    self.assertEqual(line, line.rstrip(), f"line {n} has trailing whitespace")
                    self.assertFalse(line.rstrip().endswith(": >"),
                                     f"line {n} folds with '>'; use '>-' so YAML adds no newline")
                indents = {len(l) - len(l.lstrip()) for l in front if l.startswith(" ")}
                self.assertLessEqual(len(indents), 1, f"folded lines use mixed indents {sorted(indents)}")

    def test_the_body_stays_small_enough_to_load(self):
        for path in self.skills():
            with self.subTest(skill=path.parent.name):
                body = path.read_text(encoding="utf-8").split("\n---\n", 1)[1]
                self.assertLess(len(body), self.MAX_BODY,
                                "move detail into references/; the spec recommends under 5000 "
                                "tokens for the body loaded on activation")
                self.assertLess(len(body.split("\n")), 500)

    def test_every_file_a_skill_names_exists(self):
        import re
        for path in self.skills():
            with self.subTest(skill=path.parent.name):
                body = path.read_text(encoding="utf-8")
                for ref in sorted(set(re.findall(r"(?<![\w/])references/[\w./-]+", body))):
                    self.assertTrue((path.parent / ref).is_file(), f"{ref} is missing")
                # The scripts are shared, so they sit at the plugin root, not in the skill,
                # and the skills name them as "<plugin root>/scripts/...".
                named = sorted(set(re.findall(r"(?<!\w)scripts/[\w./-]+\.py", body)))
                self.assertTrue(named, "no script reference found; check the pattern")
                for ref in named:
                    self.assertTrue((self.PLUGIN / ref).is_file(), f"{ref} is missing")


class SiteGate(unittest.TestCase):
    """The shape checks `site` runs before `validate`, holding a record to what the pages need."""

    def build(self, mutate):
        import cea_site
        data = valid_claims()
        mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            stray = sorted(str(f.relative_to(tmp)) for f in Path(tmp).rglob("index.html"))
            return written, "\n".join(messages), stray

    def test_every_class_and_id_the_site_pages_emit_is_used_by_their_css(self):
        """The index and the framework page are built here, not by `cea_page`, and were untested.

        A hook renamed where the index emits it leaves the stylesheet rule that names the old
        spelling: the page still renders, unstyled, and nothing errors.
        """
        import cea_site
        data = valid_claims()
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            fw = Path(tmp) / "framework.md"
            fw.write_text("# Framework\n\nA term and what it means.\n\n## A heading\n\n"
                          "- a list item\n\n| a | b |\n| - | - |\n| 1 | 2 |\n", encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site, framework=fw)
            self.assertTrue(written, f"the site did not build: {messages}")
            pages = {"index": site / "index.html", "framework": site / "framework" / "index.html"}
            expected = {
                "index": ["active", "idx", "mark", "n", "nav-home", "nav-repo", "pid",
                          "subtitle", "t"],
                "framework": ["mark", "nav-home", "prose"],
            }
            for label, path in pages.items():
                with self.subTest(page=label):
                    self.assertTrue(path.is_file(), f"{label} was not written")
                    html = path.read_text(encoding="utf-8")
                    # a heading carries a slug so the framework page can be linked into
                    slugs = set(re.findall(r'<h\d id="([^"]+)"', html))
                    stray_c, stray_i, n = unused_hooks(html, slugs)
                    self.assertGreater(n, 3, f"nothing was scanned on {label}")
                    # The site pages inline the page's whole stylesheet, 82 class names for the
                    # nine they use, so "is it styled" cannot tell a renamed hook from one that
                    # landed on another rule. The names are pinned instead. Change them here
                    # when the index or the framework page is meant to change.
                    body = re.sub(r"<(style|script)>.*?</\1>", "", html, flags=re.S)
                    self.assertEqual(
                        sorted({c for m in re.findall(r'class="([^"]*)"', body)
                                for c in m.split()}), expected[label],
                        f"{label} emits different class names than it used to")
                    self.assertEqual(stray_c, [],
                                     f"{label} emits classes nothing styles or scripts")
                    self.assertEqual(stray_i, [],
                                     f"{label} emits ids nothing styles, scripts or links to")

    def test_no_page_of_the_site_emits_the_same_id_twice(self):
        """Two elements sharing an id make the document invalid and the anchor ambiguous.

        The framework page used to wrap the prose in `<section id="framework">`, and a document
        whose first heading is "Framework" slugs to the same name, so every link to it landed on
        the wrapper instead of the heading.
        """
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            fw = Path(tmp) / "framework.md"
            # Headings that used to collide: one with the wrapper's name, two that differ only
            # in case, and two with nothing to make a slug out of.
            fw.write_text("# Framework\n\nWhat the terms mean.\n\n## Narrow claim\n\nOne.\n\n"
                          "### Narrow Claim\n\nTwo.\n\n## !!!\n\nThree.\n\n## ???\n\nFour.\n",
                          encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site, framework=fw)
            self.assertTrue(written, f"the site did not build: {messages}")
            for path in sorted(site.rglob("*.html")):
                with self.subTest(page=str(path.relative_to(site))):
                    # `[^"]+` would not see an empty id, and two of those collide as surely
                    ids = re.findall(r'\sid="([^"]*)"', path.read_text(encoding="utf-8"))
                    self.assertEqual(sorted(i for i in set(ids) if ids.count(i) > 1), [],
                                     "the page emits an id twice")
                    self.assertNotIn("", ids, "the page emits an element with an empty id")

    def test_every_link_inside_the_framework_page_lands_on_one_of_its_headings(self):
        """The framework document is Markdown someone writes, and its own links are not checked.

        A heading renamed, or one that slugs differently than its author expected, leaves a
        link in the published page that scrolls nowhere.
        """
        import cea_site
        doc = ("# Framework\n\nSee [narrow claims](#narrow-claim) and [results](#main-result).\n\n"
               "## Narrow claim\n\nOne.\n\n## Main result\n\nTwo.\n")
        html = cea_site.md_to_html(doc)
        ids = set(re.findall(r'<h\d id="([^"]*)"', html))
        targets = set(re.findall(r'href="#([^"]*)"', html))
        self.assertTrue(targets, "the fixture has no links")
        self.assertEqual(sorted(targets - ids), [],
                         "the framework page links to headings it does not have")

    def test_a_record_missing_a_field_the_pages_need_is_refused(self):
        """It used to raise KeyError after earlier papers were already written."""
        written, messages, stray = self.build(lambda data: data["paper"].pop("pages"))
        self.assertEqual(written, 0)
        self.assertEqual(stray, [])
        self.assertIn("paper.pages", messages)

    def test_no_record_can_leave_a_part_written_site(self):
        """Fuzz every field with wrong values: the build must never raise and never half-write.

        The gate cannot enumerate every shape a record might take, so the write phase stages the
        site and moves it into place only once it is whole. This pins that property rather than
        one more field name.
        """
        import cea_site
        bad_values = (None, 1, True, [], {}, "")
        paths = [("paper", "id"), ("paper", "title"), ("paper", "pdf"), ("paper", "pages"),
                 ("broad_statements",), ("claims",), ("rejected",),
                 ("broad_statements", 0, "id"), ("broad_statements", 0, "quote"),
                 ("broad_statements", 0, "source"), ("claims", 0, "serves"),
                 ("claims", 0, "split_from"), ("claims", 0, "page"), ("rejected", 0, "reason")]
        for path in paths:
            for bad in bad_values:
                with self.subTest(path=".".join(map(str, path)), bad=repr(bad)):
                    data = valid_claims()
                    target = data
                    for key in path[:-1]:
                        target = target[key]
                    target[path[-1]] = bad
                    with tempfile.TemporaryDirectory() as tmp:
                        rec = Path(tmp) / "rec"
                        rec.mkdir()
                        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                        (rec / "claims.json").write_text(json.dumps(data, default=str),
                                                         encoding="utf-8")
                        site = Path(tmp) / "_site"
                        with contextlib.redirect_stdout(io.StringIO()):
                            written, _ = cea_site.build_site([rec], site)
                        leftover = list(site.rglob("*")) if site.exists() else []
                        staging = [d for d in leftover if d.name.startswith(".cea-staging-")]
                        self.assertFalse(staging, "the staging directory must be removed")
                        if written == 0:
                            self.assertEqual([p for p in leftover if p.is_file()], [],
                                             "a refused build writes nothing")
                        else:
                            # The other half of the matrix used to assert nothing at all.
                            page = (site / "papers" / "fixture" / "index.html")
                            if page.is_file():
                                text = page.read_text(encoding="utf-8")
                                self.assertNotIn("&#x27;", text,
                                                 "a Python repr must never reach the page")
                            record = site / "papers" / "fixture" / "claims.json"
                            if record.is_file():
                                json.loads(record.read_text(encoding="utf-8"))

    def test_a_failure_while_publishing_says_so_and_leaves_no_staging(self):
        """The publish phase had no coverage at all: no test reached it, let alone failed in it."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            site = Path(tmp) / "_site"
            def fail_once(*args, **kwargs):
                raise OSError("disk full")

            # the copy into the site, after staging has finished
            with mock.patch.object(cea_site, "_publish", fail_once):
                with contextlib.redirect_stdout(io.StringIO()):
                    written, messages = cea_site.build_site([rec], site)
            joined = "\n".join(messages)
            self.assertEqual(written, 0)
            self.assertIn("may be partly updated", joined)
            self.assertNotIn("run validate", joined, "validate cannot fix a write failure")
            self.assertEqual([d for d in site.rglob("*") if d.name.startswith(".cea-staging-")], [])

    def test_a_rebuild_over_an_existing_site_keeps_going(self):
        """Every SiteGate case used a fresh empty --out, so a populated one was never exercised."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            site = Path(tmp) / "_site"
            (site / "papers" / "gone").mkdir(parents=True)
            (site / "papers" / "gone" / "index.html").write_text("old", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, _ = cea_site.build_site([rec], site)
            self.assertEqual(written, 1)
            self.assertTrue((site / "papers" / "fixture" / "index.html").is_file())
            # documented behaviour: the copy merges, so a dropped paper's directory survives
            self.assertTrue((site / "papers" / "gone" / "index.html").is_file())
            self.assertNotIn("gone", (site / "index.html").read_text(encoding="utf-8"))

    def problems(self, data):
        """What `validate` reports for this record, as one string."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data, default=str), encoding="utf-8")
            return "\n".join(cea_claims.validate(d)[0])

    def record(self, tmp, mutate=None, name="rec"):
        data = valid_claims()
        if mutate:
            mutate(data)
        rec = Path(tmp) / name
        rec.mkdir()
        (rec / "text.txt").write_text(TEXT, encoding="utf-8")
        (rec / "claims.json").write_text(json.dumps(data, default=str), encoding="utf-8")
        return rec

    @unittest.skipIf(os.geteuid() == 0, "root ignores the permission bits this relies on")
    def test_only_the_output_directory_needs_to_be_writable(self):
        """Staging used to sit in the parent, which broke a deploy path such as /var/www/html/cea."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            parent = Path(tmp) / "deploy"
            site = parent / "www"
            site.mkdir(parents=True)
            os.chmod(parent, 0o555)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    written, messages = cea_site.build_site([rec], site)
            finally:
                os.chmod(parent, 0o755)
        self.assertEqual(written, 1, messages)

    def test_the_index_is_copied_after_the_pages(self):
        """An interrupted publish must not leave a new index over pages that are not there."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            order, real_tree, real_file = [], shutil.copytree, shutil.copy2

            def note(fn, seen):
                def wrapper(src, dst, *args, **kwargs):
                    if Path(src).parent.name.startswith(".cea-staging-"):
                        seen.append(Path(src).name)
                    return fn(src, dst, *args, **kwargs)
                return wrapper

            shutil.copytree = note(real_tree, order)
            shutil.copy2 = note(real_file, order)
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cea_site.build_site([rec], site)
            finally:
                shutil.copytree, shutil.copy2 = real_tree, real_file
            self.assertIn("index.html", order)
            self.assertEqual(order[-1], "index.html", f"the index must be copied last, got {order}")

    def test_an_interrupt_while_publishing_is_reported_and_reraised(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            def interrupt(*a, **k):
                raise KeyboardInterrupt()

            err = io.StringIO()
            with mock.patch.object(cea_site, "_publish", interrupt):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    with self.assertRaises(KeyboardInterrupt):
                        cea_site.build_site([rec], site)
            self.assertIn("may be partly updated", err.getvalue())
            self.assertTrue(site.is_dir(), "the site directory must still be there to check")
            self.assertEqual([d for d in site.rglob("*") if d.name.startswith(".cea-staging-")], [],
                             "staging must be removed even when the interrupt propagates")

    def test_an_output_path_that_is_a_file_is_refused(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "afile"
            site.write_text("x", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
        self.assertEqual(written, 0)
        self.assertIn("not a directory", "\n".join(messages))

    def test_a_non_string_title_or_pdf_never_reaches_the_page(self):
        """These published the Python repr of a dict into the record and the page."""
        import cea_site
        for field, bad in (("title", 1), ("title", {"a": 1}), ("pdf", {"a": 1}), ("pdf", None)):
            with self.subTest(field=field, bad=repr(bad)):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, f=field, b=bad: d["paper"].__setitem__(f, b))
                    site = Path(tmp) / "_site"
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], site)
                self.assertEqual(written, 0, f"paper.{field}={bad!r} must be refused")
                self.assertIn("must be text", "\n".join(messages))

    def test_an_entry_page_of_true_is_refused(self):
        """A bool is an int, so `page: true` slipped past the number check."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["claims"][0].__setitem__("page", True))
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
        self.assertEqual(written, 0)
        self.assertIn("page must be", "\n".join(messages))

    def test_an_unsure_selection_reason_blocks_even_beside_a_reason(self):
        """`or` short-circuited between the two fields, so one hid behind the other.

        `unsettled` is checked directly here: an entry carrying both fields is not a record
        `validate` accepts, so it can no longer reach the check through a build.
        """
        import cea_site
        data = valid_claims()
        data["claims"][0]["reason"] = "settled"
        data["claims"][0]["selection_reason"] = "Unsure whether B1 rests on this."
        self.assertEqual([p.split(":")[0] for p in cea_site.unsettled(data)], ["C1"])

    def test_a_valid_record_that_is_unsure_is_still_refused_by_a_build(self):
        """The same rule through the build, on a record `validate` accepts."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["claims"][0].update(
                selection_reason="Unsure whether B1 rests on this."))
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
        self.assertEqual(written, 0)
        self.assertIn("CEA_UNRESOLVED", "\n".join(messages))

    def test_staging_left_by_a_killed_build_is_swept(self):
        """Staging now lives inside the deployed tree, so residue would be published."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            ghost = site / ".cea-staging-OLD" / "papers" / "ghost"
            ghost.mkdir(parents=True)
            (ghost / "index.html").write_text("old", encoding="utf-8")
            # only staging older than an hour is swept, so a live build's is never destroyed
            old_enough = time.time() - 7200
            os.utime(site / ".cea-staging-OLD", (old_enough, old_enough))
            with contextlib.redirect_stdout(io.StringIO()):
                written, _ = cea_site.build_site([rec], site)
            self.assertEqual(written, 1)
            self.assertFalse((site / ".cea-staging-OLD").exists(), "old staging must be swept")

    def test_markdown_that_no_branch_claims_still_finishes(self):
        """A '#' or '|' line no branch claimed used to loop forever, hanging the whole build."""
        import subprocess
        probe = ("import sys; sys.path.insert(0, %r); import cea_site; "
                 "cea_site.md_to_html(sys.argv[1])" % str(SCRIPTS))
        for text in ("#tag\n", "####### deep\n", "| a | b |\nprose\n", "| a | b |",
                     "#\n", "```\nunclosed\n"):
            with self.subTest(text=text[:18]):
                try:
                    done = subprocess.run([sys.executable, "-c", probe, text],
                                          capture_output=True, timeout=5)
                except subprocess.TimeoutExpired:
                    self.fail(f"md_to_html never returned for {text!r}")
                self.assertEqual(done.returncode, 0, done.stderr.decode()[:300])

    def test_a_paper_id_whose_prefix_is_legal_is_still_refused(self):
        """A prefix match would let `ok/../../escape` through and write outside --out."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].__setitem__("id", "ok/../../escape"))
            site = Path(tmp) / "out" / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            outside = [str(f.relative_to(tmp)) for f in Path(tmp).rglob("index.html")]
            self.assertEqual(written, 0, messages)
            self.assertEqual(outside, [], "nothing may be written outside --out")

    def test_an_absolute_or_climbing_pdf_path_is_refused(self):
        import cea_site
        for bad in ("/etc/hosts", "../../x.pdf"):
            with self.subTest(pdf=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pdf", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)
                    self.assertIn("relative path", "\n".join(messages))
                    problems, _ = cea_claims.validate(rec)
                    self.assertTrue(any("paper.pdf" in p for p in problems), problems)

    def test_a_claims_json_that_cannot_be_read_is_refused(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            (rec / "claims.json").write_text("{ this is not json", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            self.assertEqual(written, 0)
            self.assertIn("cannot be read", "\n".join(messages))

    def test_paper_pages_must_be_a_real_whole_number(self):
        import cea_site
        for bad in (True, None, "12", 1.5):
            with self.subTest(pages=repr(bad)):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pages", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0, f"pages={bad!r} must be refused")
                    self.assertIn("whole number", "\n".join(messages))

    def test_each_failure_phase_gives_its_own_remedy(self):
        """Only the publishing phase was pinned. The other two could be deleted unnoticed."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            real = tempfile.mkdtemp
            tempfile.mkdtemp = lambda *a, **k: (_ for _ in ()).throw(OSError("no room"))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            finally:
                tempfile.mkdtemp = real
            self.assertEqual(written, 0)
            self.assertIn("can be created and written", "\n".join(messages))

        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            real_copy = cea_site.copy_paper
            cea_site.copy_paper = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad record"))
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            finally:
                cea_site.copy_paper = real_copy
            self.assertEqual(written, 0)
            self.assertIn("run validate on every record", "\n".join(messages))

    def test_a_site_under_a_path_that_does_not_exist_yet_is_created(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "a" / "b" / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, messages)
            self.assertTrue((site / "index.html").is_file())

    def test_a_title_with_markup_is_escaped_everywhere_it_is_published(self):
        import cea_site
        evil = '<script>alert(1)</script>'
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].__setitem__("title", evil))
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, _ = cea_site.build_site([rec], site)
            self.assertEqual(written, 1)
            for page in (site / "index.html", site / "papers" / "fixture" / "index.html"):
                self.assertNotIn(evil, page.read_text(encoding="utf-8"),
                                 f"{page.name} must escape the title")

    def test_the_framework_link_from_a_paper_page_resolves(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            doc = Path(tmp) / "framework.md"
            doc.write_text("# Framework\n\nTerms.\n", encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                cea_site.build_site([rec], site, doc)
            page = site / "papers" / "fixture" / "index.html"
            href = re.search(r'href="((?:\.\./)*framework/)"', page.read_text(encoding="utf-8"))
            self.assertIsNotNone(href, "the paper page must link the framework")
            self.assertTrue((page.parent / href.group(1) / "index.html").resolve().is_file(),
                            f"{href.group(1)} must resolve to the framework page")

    def test_exactly_one_nav_link_is_marked_active(self):
        import cea_page
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        links = re.findall(r'<a href="#[^"]+"[^>]*>', html)
        active = [a for a in links if 'class="active"' in a]
        self.assertEqual(len(active), 1, f"one link must be active, got {active}")
        self.assertEqual(links[0], active[0], "the first link is the active one")

    def test_a_template_marker_in_the_record_is_not_expanded(self):
        """Substituting key by key re-scanned inserted values, so record text became markup."""
        import cea_page
        for field, where in (("title", "paper"), ("states", "claims"), ("quote", "rejected")):
            for marker in ("@@ANCHOR_MAP@@", "@@DATA@@", "@@STATS@@"):
                with self.subTest(field=field, marker=marker):
                    data = valid_claims()
                    target = data["paper"] if where == "paper" else data[where][0]
                    target[field] = f"text {marker} more"
                    html = cea_page.build(data, Path("claims.json"), Path("out.html"))
                    self.assertIn(marker, html, "the marker must survive as literal text")
                    self.assertNotIn("<a class=\"section-anchor\" href=\"#map\"></a></h1>", html)
        # the page still expands its own markers
        html = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertNotIn("@@", html, "no marker may be left unexpanded in a normal page")
        self.assertEqual(html.count('class="section-anchor"'), 5)

    def test_a_marker_in_a_record_field_never_becomes_markup(self):
        """The sections used to be filled from `fields` after they already held record text."""
        import cea_page
        from html.parser import HTMLParser

        class Handlers(HTMLParser):
            def __init__(self):
                super().__init__()
                self.injected = []

            def handle_starttag(self, tag, attrs):
                if tag == "button" and any(k.startswith("on") for k, _ in attrs):
                    self.injected.append(attrs)

        for marker in ("@@STRIP@@", "@@PDF_LINK@@", "@@DATA@@", "@@MAP@@", "@@CLAIMS@@"):
            for field, where in (("states", "claims"), ("quote", "rejected"), ("title", "paper")):
                with self.subTest(marker=marker, field=field):
                    data = valid_claims()
                    target = data["paper"] if where == "paper" else data[where][0]
                    target[field] = f"text {marker} more"
                    html = cea_page.build(data, Path("claims.json"), Path("out.html"))
                    self.assertIn(marker, html, "the marker must stay literal text")
                    self.assertEqual(html.count('<table class="sectable"'), 1,
                                     "no section may be rendered twice")
                    parser = Handlers()
                    parser.feed(html)
                    self.assertEqual(parser.injected, [], "record text became an attribute")
        clean = cea_page.build(valid_claims(), Path("claims.json"), Path("out.html"))
        self.assertNotIn("@@", clean, "a normal page leaves no marker unexpanded")

    def test_an_unknown_marker_is_left_alone(self):
        import cea_page
        self.assertEqual(cea_page._fill("a @@NOPE@@ b", {"X": "y"}), "a @@NOPE@@ b")

    def test_the_embedded_json_escapes_every_angle_bracket(self):
        """`</` alone leaves `<!--<script`, which changes how a parser reads the block."""
        import cea_page, json as _json
        data = valid_claims()
        data["claims"][0]["note"] = "<!--<script> and </script> and a < b"
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        blob = re.search(r'id="cea-data">(.*?)</script>', html, re.S).group(1)
        self.assertNotIn("<", blob, "no raw < may remain in the embedded record")
        self.assertEqual(_json.loads(blob)["claims"][0]["note"], data["claims"][0]["note"])

    def test_the_tooltip_escapes_every_field_it_shows(self):
        """It escaped the quote and wrote four other record fields into innerHTML raw."""
        import cea_page
        script = cea_page.TEMPLATE
        self.assertIn("function esc(v)", script)
        for raw in ("+ e.page", "+ e.id +", "e.serves.join(', ')", "e.source.replace",
                    "+ e.quote", "(e.quote ||"):
            self.assertNotIn(raw, script, f"{raw} must go through esc()")
        # every record field the tooltip shows must be wrapped, not just the ones listed above
        body = script[script.index("function showTip("):]
        body = body[:body.index("tooltip.style")]
        for reference in re.findall(r"\be\.\w+", body):
            self.assertRegex(body, rf"esc\(\s*(?:\[\]\.concat\(|String\()?{re.escape(reference)}",
                             f"{reference} must be escaped in the tooltip")

    def test_every_record_field_is_escaped_on_the_page(self):
        """Only the title was pinned. Eleven other escapes could be deleted unnoticed."""
        import cea_page
        payload = '<s>&"x'

        def poisoned(drop_claims):
            data = valid_claims()
            for key in ("id", "title", "pdf"):
                data["paper"][key] = payload if key != "pdf" else f"{payload}.pdf"
            for group in ("broad_statements", "claims", "rejected"):
                for entry in data[group]:
                    for key in list(entry):
                        if key == "id":
                            continue
                        # `page` is an int in the fixture, so a string-only sweep never reached it
                        if isinstance(entry[key], (str, int)):
                            entry[key] = payload
            if drop_claims:
                # with no claim serving it, the broad statement renders as a watch card instead,
                # which shows `states` and `note`, both optional, so neither is in the fixture
                data["claims"] = []
                data["broad_statements"][0]["note"] = payload
                # the card shows `states` only when it differs from `quote`, and equal values fall
                # through to the other branch and leave that escape unexercised
                data["broad_statements"][0]["states"] = payload + " differing"
            return data

        for drop_claims in (False, True):
            with self.subTest(watch_card=drop_claims):
                html = cea_page.build(poisoned(drop_claims), Path("claims.json"), Path("out.html"))
                blob = re.search(r'id="cea-data">(.*?)</script>', html, re.S).group(1)
                outside = html.replace(blob, "")
                self.assertNotIn("<s>", outside, "a record field reached the page unescaped")
                self.assertIn("&lt;s&gt;", outside)

    def test_a_blank_line_is_not_a_table_separator(self):
        """An empty set is a subset of every set, so a blank line read as a rule row."""
        import cea_site
        with bounded():
            self.assertNotIn("<table>", cea_site.md_to_html("| a | b |\n\nprose\n"))
        with bounded():
            self.assertIn("<table>", cea_site.md_to_html("| a | b |\n| --- | --- |\n| 1 | 2 |\n"))
            self.assertIn("<table>", cea_site.md_to_html("| a | b |\n|:--|--:|\n| 1 | 2 |\n"))

    def test_fenced_code_on_the_framework_page_is_escaped(self):
        import cea_site
        with bounded():
            html = cea_site.md_to_html("```\n<b>PWND</b> & \"x\"\n```\n")
        self.assertNotIn("<b>PWND</b>", html)
        self.assertIn("&lt;b&gt;PWND&lt;/b&gt;", html)

    def test_a_rule_row_with_trailing_whitespace_still_renders(self):
        """Without the strip, a trailing tab lands in the rule row's character set.

        `splitlines()` already absorbs a carriage return, so CRLF is not the case that bites; an
        editor leaving a tab at the end of the line is.
        """
        import cea_site
        for rule in ("| --- | --- |\t", "| --- | --- | ", "|---|---|"):
            with self.subTest(rule=repr(rule)):
                doc = f"| a | b |\n{rule}\n| 1 | 2 |\n"
                with bounded():
                    self.assertIn("<table>", cea_site.md_to_html(doc))
        with bounded():
            self.assertIn("<table>", cea_site.md_to_html("| a | b |\r\n| --- | --- |\r\n| 1 | 2 |\r\n"))

    def test_a_framework_document_that_cannot_be_read_names_itself(self):
        """The failure used to be reported as a problem with the records."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8")
            doc = Path(tmp) / "framework.md"
            doc.write_bytes("# Übersicht\n".encode("utf-16"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["site", str(rec), "--out", str(Path(tmp) / "_site"),
                                        "--framework", str(doc)])
            printed = out.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("framework document", printed)
        self.assertNotIn("run validate on every record", printed)

    def test_a_framework_document_with_a_byte_order_mark_keeps_its_heading(self):
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "f.md"
            doc.write_text("\ufeff# Terms\n\nProse.\n", encoding="utf-8")
            site = Path(tmp) / "_site"
            site.mkdir()
            cea_site.write_framework(site, doc)
            page = (site / "framework" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<h1", page)
        self.assertNotIn("<p>\ufeff", page)

    def test_framework_markdown_is_escaped(self):
        """The framework page is published and linked from every paper footer, and had no test."""
        import cea_site
        evil = "<script>alert(1)</script>"
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "framework.md"
            doc.write_text(f"# Terms\n\nA note with {evil} in it.\n", encoding="utf-8")
            site = Path(tmp) / "_site"
            site.mkdir()
            cea_site.write_framework(site, doc)
            page = (site / "framework" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(evil, page, "the framework page must escape its Markdown")
        self.assertIn("&lt;script&gt;", page)

    def test_a_claims_json_that_is_not_an_object_is_refused(self):
        import cea_site
        for shape in ("null", "5", "true", "[]", '"x"'):
            with self.subTest(shape=shape):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp)
                    (rec / "claims.json").write_text(shape, encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)
                    self.assertIn("must hold a JSON object", "\n".join(messages))

    def test_a_pdf_named_after_a_published_file_is_refused(self):
        """It overwrote the sanitised record with the checker's raw one."""
        import cea_site
        for bad in ("claims.json", "deep/claims.md", "text.txt"):
            with self.subTest(pdf=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pdf", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)
                    self.assertIn("the site publishes", "\n".join(messages))
                    problems, _ = cea_claims.validate(rec)
                    self.assertTrue(any("paper.pdf" in p for p in problems), problems)

    def test_an_entry_id_with_a_legal_prefix_is_still_refused(self):
        """A prefix match let `R1x` through to cea_page's int(id[1:]), which raised."""
        import cea_site
        for bad in ("R1x", "R1.5", "R01x"):
            with self.subTest(id=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["rejected"][0].__setitem__("id", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0, f"{bad} must be refused")
                    self.assertIn("must be R and a number", "\n".join(messages))

    def test_a_pdf_named_after_a_published_file_in_a_subdirectory_is_refused(self):
        """The name was compared exactly, but the checker's filesystem folds case."""
        import cea_site
        for bad in ("pdfs/Claims.JSON", "pdfs/Claims.MD", "CLAIMS.json", "a/b/TEXT.TXT"):
            with self.subTest(pdf=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pdf", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0, f"{bad} must be refused")
                    self.assertIn("the site publishes", "\n".join(messages))
                    self.assertTrue(any("paper.pdf" in p for p in cea_claims.validate(rec)[0]))
        for good in ("text.txt.pdf", "My Paper.pdf", "claims-2024.pdf"):
            with self.subTest(pdf=good):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, g=good: d["paper"].__setitem__("pdf", g))
                    self.assertEqual([p for p in cea_claims.validate(rec)[0] if "paper.pdf" in p], [],
                                     f"{good} is a legitimate name")

    def test_a_pdf_path_that_names_a_directory_is_refused(self):
        """A trailing slash emptied the basename, so the published-name check saw nothing."""
        import cea_site
        for bad in ("claims.json/", "CLAIMS.JSON/", "claims.json/.", "claims.json//",
                    "claims.html", "a/b/Claims.MD/", "index.html", "TEXT.TXT",
                    ".", "/", " "):
            with self.subTest(pdf=bad):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pdf", b))
                    site = Path(tmp) / "_site"
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], site)
                    self.assertEqual(written, 0, f"{bad} must be refused")
                    self.assertTrue(any("paper.pdf" in p for p in cea_claims.validate(rec)[0]),
                                    f"validate must refuse {bad}")
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].__setitem__("pdf", "fixture.pdf/"))
            (rec / "fixture.pdf").write_bytes(b"%PDF-1.4 paper")
            # `paper.pdf` is the file's name. A trailing slash makes it a directory, so it is no
            # longer the name, and the reference always said a name rather than a path.
            self.assertTrue(any("must be the file's name" in p
                                for p in cea_claims.validate(rec)[0]),
                            "a trailing slash makes it a path, not a name")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, _ = cea_site.build_site([rec], site)
            self.assertEqual(written, 0, "the build follows validate and publishes nothing")
            self.assertFalse(site.exists() and any(site.rglob("*.pdf")),
                             "nothing is published from a record the checks refuse")

    def test_the_name_without_the_slash_still_publishes(self):
        """The rule must not cost the ordinary case it is written for."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].__setitem__("pdf", "fixture.pdf"))
            (rec / "fixture.pdf").write_bytes(b"%PDF-1.4 paper")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, "\n".join(messages))
            self.assertTrue((site / "papers" / "fixture" / "fixture.pdf").is_file())
            published = json.loads((site / "papers" / "fixture" / "claims.json")
                                   .read_text(encoding="utf-8"))
            self.assertEqual(published["paper"]["pdf"], "fixture.pdf",
                             "the published record must name the file it published")

    def test_the_published_pdf_is_named_as_the_record_declares(self):
        """The `<paper_id>.pdf` fallback means the file found and the name declared can differ."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["paper"].__setitem__("pdf", "declared.pdf"))
            (rec / "fixture.pdf").write_bytes(b"%PDF-1.4 paper")   # found via the id fallback
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 1, messages)
            published = site / "papers" / "fixture"
            self.assertTrue((published / "declared.pdf").is_file(),
                            "the file must publish under the name the record declares")
            page = (published / "index.html").read_text(encoding="utf-8")
            self.assertIn('href="declared.pdf"', page, "the page must link what was published")

    def test_the_site_gate_refuses_a_control_character_too(self):
        """The shape gate has to catch it: it runs before validate, and the page reads it."""
        import cea_site
        for bad in ("a\u0000b.pdf", "a\u0001b.pdf", "a\tb.pdf"):
            with self.subTest(pdf=repr(bad)):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, lambda d, b=bad: d["paper"].__setitem__("pdf", b))
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)
                    self.assertIn("control character", "\n".join(messages))
                    self.assertTrue(any("control character" in p
                                        for p in cea_claims.validate(rec)[0]))

    def test_a_page_built_from_a_record_with_a_null_pdf_does_not_raise(self):
        import cea_page
        data = valid_claims()
        data["paper"]["pdf"] = "a\u0000b.pdf"
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        self.assertIn("<h1", html)

    def test_a_paper_pdf_with_a_control_character_never_reaches_the_filesystem(self):
        """validate passed it and render then died with a raw ValueError."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            data = valid_claims()
            data["paper"]["pdf"] = "x\u0000y.pdf"
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            self.assertTrue(any("control character" in p for p in cea_claims.validate(d)[0]))
            data["paper"]["pdf"] = "x" * 5000 + ".pdf"
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cea_claims.main(["render", str(d)]), 0,
                                 "an over-long name is not a file, not a crash")

    def test_the_gate_blocks_and_does_not_only_complain(self):
        """Three branches asserted their message but never that the build stopped."""
        import cea_site
        for label, mutate in (("missing field", lambda d: d["paper"].pop("title")),
                              ("bad pages", lambda d: d["paper"].__setitem__("pages", "12")),
                              ("bad title", lambda d: d["paper"].__setitem__("title", 1))):
            with self.subTest(case=label):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, mutate)
                    site = Path(tmp) / "_site"
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, _ = cea_site.build_site([rec], site)
                    self.assertEqual(written, 0)
                    self.assertFalse(list(site.rglob("*.html")), "nothing may be written")

    def test_an_entry_id_that_is_not_its_letter_and_a_number_is_refused(self):
        """cea_page does int(id[1:]), which raised a bare ValueError through the site path."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp, lambda d: d["rejected"][0].__setitem__("id", "Rx"))
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            self.assertEqual(written, 0)
            self.assertIn("must be R and a number", "\n".join(messages))

    def test_a_successful_build_prints_its_markers(self):
        """skills/site/SKILL.md promises both, and deleting either print went unnoticed."""
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cea_claims.main(["site", str(rec), "--out", str(Path(tmp) / "_site")])
            printed = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("CEA_SITE", printed)
        self.assertIn("CEA_WARNING", printed, "a paper with no PDF must say so")

    def test_every_recorded_id_is_an_anchor_on_the_page_exactly_once(self):
        """CLAUDE.md: ids are the anchors of the published pages."""
        import cea_page
        data = valid_claims()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        for key in ("broad_statements", "claims", "rejected"):
            for entry in data[key]:
                with self.subTest(id=entry["id"]):
                    anchors = re.findall(rf'\sid="{entry["id"]}"', html)
                    self.assertEqual(len(anchors), 1,
                                     f'{entry["id"]} must anchor the page exactly once')

    def test_the_page_and_the_markdown_group_claims_the_same_way(self):
        """CLAUDE.md: the page and claims.md cannot disagree about which claims serve which result."""
        import cea_page
        data = valid_claims()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        markdown = cea_claims.render(data)
        for statement in cea_claims._by_weight(data):
            served = [c["id"] for c in data["claims"] if statement["id"] in c["serves"]]
            with self.subTest(result=statement["id"]):
                for claim in served:
                    self.assertIn(claim, markdown)
                    self.assertRegex(html, rf'\sid="{claim}"')

    def test_the_page_links_the_files_it_was_built_from(self):
        """`near`/`source_links` had no behavioural test: the reader's way back to the record."""
        import cea_page
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "rec"
            record.mkdir()
            for name in ("claims.json", "claims.md", "text.txt"):
                (record / name).write_text("x", encoding="utf-8")
            out = record / "claims.html"
            links = cea_page.source_links(valid_claims()["paper"], record / "claims.json", out)
            for name in ("claims.json", "claims.md", "text.txt"):
                self.assertIn(f'href="{name}"', links, f"{name} sits beside the page")
            # a file that is not there is not linked
            (record / "text.txt").unlink()
            self.assertNotIn("text.txt", cea_page.source_links(
                valid_claims()["paper"], record / "claims.json", out))
            # a record too far above the page is not linked either
            deep = Path(tmp) / "a" / "b" / "c" / "d"
            deep.mkdir(parents=True)
            self.assertEqual(cea_page.near("claims.json", record / "claims.json",
                                           deep / "claims.html"), "",
                             "a path that climbs too far would not survive being moved")

    def test_a_claim_card_states_the_records_own_values(self):
        """The cards were asserted only for existence, never for what they say."""
        import cea_page
        data = valid_claims()
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        claim = data["claims"][0]
        card = re.search(rf'<article[^>]*\sid="{claim["id"]}".*?</article>', html, re.S)
        self.assertIsNotNone(card, "every claim must have a card")
        body = card.group(0)
        self.assertIn(f'p. {claim["page"]}', body, "the card must state the claim's page")
        self.assertIn(cea_claims._flat(claim["section"]), body)
        for served in claim["serves"]:
            self.assertIn(served, body, "the card must name the result it serves")
        self.assertIn(cea_claims._flat(claim["selection_reason"])[:40], body)
        rejected = data["rejected"][0]
        rcard = re.search(rf'<article[^>]*\sid="{rejected["id"]}".*?</article>', html, re.S)
        self.assertIsNotNone(rcard, "every rejected candidate must have a card")
        self.assertIn(cea_claims._flat(rejected["reason"])[:30], rcard.group(0))
        self.assertIn(f'p. {rejected["page"]}', rcard.group(0),
                      "the card must state the candidate's page")
        node = re.search(rf'<button class="node claim" data-id="{claim["id"]}".*?</button>',
                         html, re.S)
        self.assertIsNotNone(node, "every claim must have a map node")
        self.assertIn(f'p. {claim["page"]}', node.group(0),
                      "the map node must state the claim's page")

    def test_the_index_row_states_the_records_own_numbers(self):
        """Every column could be corrupted silently, and only the href was pinned."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            first = self.record(tmp, name="a")
            second = self.record(tmp, lambda d: (d["paper"].update(id="zz", title="Zulu paper"),
                                                 d.update(broad_statements=[], claims=[],
                                                          rejected=[])), name="b")
            site = Path(tmp) / "_site"
            doc = Path(tmp) / "framework.md"
            doc.write_text("# Terms\n\nProse.\n", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([second, first], site, doc)
            self.assertEqual(written, 2, messages)
            index = (site / "index.html").read_text(encoding="utf-8")
        rows = re.findall(r"<tr><td class=\"t\">.*?</tr>", index, re.S)
        self.assertEqual(len(rows), 2)
        self.assertIn("papers/fixture/", rows[0])   # "Fixture" sorts before "Zulu paper"
        self.assertIn("papers/zz/", rows[1])
        numbers = re.findall(r'<td class="n">([^<]*)</td>', rows[0])
        self.assertEqual(numbers[:4], ["1", "2", "1", "1"],
                         f"broad, claims, rejected, stated-in must match the record: {numbers}")
        self.assertEqual(numbers[4], "3", "the page count must be the record's")
        self.assertIn("no main result recorded", rows[1],
                      "a paper with no broad statement must be marked")
        self.assertNotIn("no main result recorded", rows[0])
        self.assertIn('href="framework/"', index, "the framework link must be on the index")

    def test_the_map_never_contradicts_the_record(self):
        """A claim serving two results was filed under the first, and the map called the second
        unserved, contradicting its own card, claims.md, and the watch card built for that case."""
        import cea_page
        data = valid_claims()
        data["broad_statements"].append({"id": "B2", "quote": "Build failures are rare.",
                                         "page": 3, "section": "7 Conclusion",
                                         "source": "conclusion"})
        data["claims"][0]["serves"] = ["B1", "B2"]
        data["claims"][1]["serves"] = ["B1"]
        html = cea_page.build(data, Path("claims.json"), Path("out.html"))
        row = re.search(r'<div class="map-row">(?:(?!map-row).)*?data-id="B2".*?</div>\s*</div>',
                        html, re.S)
        self.assertIsNotNone(row, "B2 must have a map row")
        self.assertNotIn("No narrow claim serves this result", row.group(0),
                         "C1 serves B2, so the map must not say otherwise")
        self.assertNotIn("node result empty", row.group(0),
                         "a served result must not carry the unserved styling")
        self.assertIn("C1", row.group(0), "the map must say where the claim is shown")
        lead = re.search(r'<div class="map-row">(?:(?!map-row).)*?data-id="B1".*?</div>\s*</div>',
                         html, re.S)
        self.assertIsNotNone(lead, "B1 must have a map row")
        self.assertIn('class="node claim" data-id="C1"', lead.group(0),
                      "the result the claims were filed under must still show them")
        self.assertIn('data-id="C2"', lead.group(0))

    def test_an_id_valued_field_must_hold_ids(self):
        """The gate checked `id` but not `serves`/`duplicate_of`/`breaks_down`/`split_from`,
        which the page writes straight into hrefs and attributes."""
        import cea_site
        payload = '"><img src=x onerror=alert(1)>'
        for label, mutate in (
                ("serves", lambda d: d["claims"][0].__setitem__("serves", ["B1", payload])),
                ("duplicate_of", lambda d: d["rejected"][0].__setitem__("duplicate_of", [payload])),
                ("split_from", lambda d: d["rejected"][0].update({"split_from": payload,
                                                                  "states": "x"}))):
            with self.subTest(field=label):
                with tempfile.TemporaryDirectory() as tmp:
                    rec = self.record(tmp, mutate)
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0, f"{label} holding markup must be refused")
                    self.assertIn(label, "\n".join(messages))

    def test_an_unpaired_surrogate_is_reported_by_validate(self):
        """Legal JSON, survives json.loads, and only fails when the page is written."""
        for group, field in (("broad_statements", "note"), ("claims", "section"),
                             ("rejected", "reason")):
            with self.subTest(field=f"{group}.{field}"):
                data = valid_claims()
                data[group][0][field] = "\ud800 lone"
                self.assertIn("surrogate", self.problems(data))

    def test_an_absurd_number_of_digits_is_reported_not_raised(self):
        """Python refuses to convert an integer past 4300 digits, and that ValueError escaped."""
        import cea_site
        for label, mutate in (
                ("page", lambda d: d["claims"][0].__setitem__("page", "9" * 4400 + "-" + "9" * 4400)),
                ("broad id", lambda d: d["broad_statements"][0].__setitem__("id", "B" + "9" * 4400)),
                ("rejected id", lambda d: d["rejected"][0].__setitem__("id", "R" + "9" * 4400))):
            with self.subTest(case=label):
                data = valid_claims()
                mutate(data)
                self.assertTrue(self.problems(data), "validate must report, not raise")
                with tempfile.TemporaryDirectory() as tmp:
                    rec = Path(tmp) / "rec"
                    rec.mkdir()
                    (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                    (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                    with contextlib.redirect_stdout(io.StringIO()):
                        written, _ = cea_site.build_site([rec], Path(tmp) / "_site")
                    self.assertEqual(written, 0)

    def test_a_json_number_past_the_digit_limit_is_reported(self):
        """The int-conversion ValueError is not a JSONDecodeError, so it escaped both handlers."""
        import cea_site
        raw = '{"paper": {"id": "fixture", "title": "t", "pdf": "f.pdf", "pages": %s}, ' \
              '"broad_statements": [], "claims": [], "rejected": []}' % ("9" * 4400)
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(raw, encoding="utf-8")
            problems, _ = cea_claims.validate(rec)   # must report, not raise
            self.assertTrue(problems)
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            self.assertEqual(written, 0)
            self.assertIn("CEA_INVALID", "\n".join(messages))

    def test_a_byte_order_mark_is_read_the_same_way_everywhere(self):
        """validate accepted a BOM'd record and the site refused it, blocking the whole build."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(valid_claims()), encoding="utf-8-sig")
            self.assertEqual(cea_claims.validate(rec)[0], [])
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], Path(tmp) / "_site")
            self.assertEqual(written, 1, messages)

    def test_a_live_staging_directory_is_not_swept(self):
        """A second build used to delete the first build's staging out from under it."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.record(tmp)
            site = Path(tmp) / "_site"
            live = site / ".cea-staging-LIVE"
            live.mkdir(parents=True)
            with contextlib.redirect_stdout(io.StringIO()):
                written, _ = cea_site.build_site([rec], site)
            self.assertEqual(written, 1)
            self.assertTrue(live.is_dir(), "a concurrent build's staging must survive")

    def test_a_non_string_paper_id_is_refused(self):
        """str() in the gate let an int through, and copy_paper then raised on Path / int."""
        written, messages, stray = self.build(lambda data: data["paper"].__setitem__("id", 1))
        self.assertEqual(written, 0)
        self.assertEqual(stray, [])
        self.assertIn("paper.id", messages)

    def test_two_ids_differing_only_in_case_are_refused(self):
        """One directory serves both on a case-insensitive filesystem."""
        import cea_site
        with tempfile.TemporaryDirectory() as tmp:
            recs = []
            for n, pid in enumerate(("Paper-A", "paper-a")):
                data = valid_claims()
                data["paper"]["id"] = pid
                rec = Path(tmp) / f"rec{n}"
                rec.mkdir()
                (rec / "text.txt").write_text(TEXT, encoding="utf-8")
                (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
                recs.append(rec)
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site(recs, site)
            self.assertEqual(written, 0)
            self.assertFalse(site.exists(), "nothing may be written")
            self.assertIn("collides", "\n".join(messages))

    def test_a_paper_id_that_escapes_the_output_directory_is_refused(self):
        written, messages, stray = self.build(
            lambda data: data["paper"].__setitem__("id", "../../escaped"))
        self.assertEqual(written, 0)
        self.assertEqual(stray, [], "nothing may be written outside --out")
        self.assertIn("paper.id", messages)

    def test_a_multi_segment_pdf_path_is_refused(self):
        """`paper.pdf` is the file's name. A path was accepted as long as it was relative, and
        `claims.md` then printed it to the checker, naming a file that exists on one machine."""
        import cea_site
        data = valid_claims()
        data["paper"]["pdf"] = "papers/fixture.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            (rec / "papers").mkdir(parents=True)
            (rec / "papers" / "fixture.pdf").write_bytes(b"%PDF-1.4 fixture")
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 0, "\n".join(messages))
            self.assertIn("must be the file's name", "\n".join(messages))
            self.assertFalse(site.exists() and any(site.rglob("*.pdf")))

    def test_a_page_that_no_longer_matches_the_record_is_removed(self):
        """A stale claims.html used to survive and contradict the record beside it."""
        data = valid_claims()
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cea_claims.main(["render", str(d)]), 0)
            self.assertTrue((d / "claims.html").is_file())
            data["rejected"][0]["reason"] = "Unsure whether B1 rests on this."
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cea_claims.main(["render", str(d)]), 1)
            self.assertFalse((d / "claims.html").exists(),
                             "the page must not outlive the decision it showed")
            self.assertIn("was removed", out.getvalue())

    def test_a_rendered_page_drops_a_local_schema_path(self):
        """render embeds the whole record, so fixing only the site left claims.html leaking."""
        data = valid_claims()
        data["$schema"] = "/home/someone/claims.schema.json"
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cea_claims.main(["render", str(d)]), 0)
            page = (d / "claims.html").read_text(encoding="utf-8")
        self.assertNotIn("/home/someone", page)

    def test_an_entry_field_of_the_wrong_type_is_refused(self):
        """The gate checked that fields exist, not that they hold what the pages read."""
        for label, mutate in (("serves", lambda d: d["claims"][0].__setitem__("serves", 1)),
                              ("source", lambda d: d["broad_statements"][0].__setitem__("source", 7)),
                              ("split_from", lambda d: d["claims"][0].__setitem__("split_from", {}))):
            with self.subTest(field=label):
                written, messages, stray = self.build(mutate)
                self.assertEqual(written, 0)
                self.assertEqual(stray, [])
                self.assertIn("CEA_INVALID", messages)

    def test_a_local_path_never_reaches_claims_md(self):
        """`claims.md` is what the checker reads, and it printed the path verbatim. The path is
        now refused outright, and `copy_paper` still reduces one to its name behind that."""
        import cea_site
        data = valid_claims()
        data["paper"]["pdf"] = "somewhere/deep/fixture.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                written, messages = cea_site.build_site([rec], site)
            self.assertEqual(written, 0, "the record is refused before anything is written")
            self.assertIn("somewhere/deep/fixture.pdf", "\n".join(messages),
                          "the message has to name the path it refuses")
            self.assertFalse(site.exists() and any(site.rglob("claims.md")))

    def test_the_published_record_drops_a_local_schema_path(self):
        import cea_site
        data = valid_claims()
        data["$schema"] = "/home/someone/claims.schema.json"
        with tempfile.TemporaryDirectory() as tmp:
            rec = Path(tmp) / "rec"
            rec.mkdir()
            (rec / "text.txt").write_text(TEXT, encoding="utf-8")
            (rec / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            site = Path(tmp) / "_site"
            with contextlib.redirect_stdout(io.StringIO()):
                cea_site.build_site([rec], site)
            leaked = [str(f) for f in site.rglob("*")
                      if f.is_file() and "/home/someone" in f.read_text(errors="ignore")]
        self.assertEqual(leaked, [], "the checker's local path must not reach the site")


class Unsettled(unittest.TestCase):
    """An entry that leaves the claim-or-not decision open must stop the page."""

    def unsettled_for(self, mutate) -> list[str]:
        data = valid_claims()
        mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "text.txt").write_text(TEXT, encoding="utf-8")
            (d / "claims.json").write_text(json.dumps(data), encoding="utf-8")
            problems, parsed = cea_claims.validate(d)
            self.assertEqual(problems, [], "the record must be valid for this test to mean anything")
            return cea_claims.unsettled(parsed)

    def test_an_unsure_claim_stops_the_page(self):
        def mutate(data):
            data["claims"][0]["selection_reason"] = "Unsure whether B1 rests on this; the checker decides."
        self.assertTrue(self.unsettled_for(mutate), "an unsure selection_reason must be caught")

    def test_an_unsure_rejected_candidate_stops_the_page(self):
        def mutate(data):
            data["rejected"][0]["reason"] = "Unsure whether a main result rests on this."
        self.assertTrue(self.unsettled_for(mutate))

    def test_leading_whitespace_does_not_hide_an_unsure_reason(self):
        """A reason indented by a space is still unsure. It used to publish."""
        for field, entry in (("selection_reason", "claims"), ("reason", "rejected")):
            with self.subTest(field=field):
                def mutate(data, field=field, entry=entry):
                    data[entry][0][field] = "  Unsure whether a main result rests on this."
                self.assertTrue(self.unsettled_for(mutate))

    def test_unsure_inside_the_first_sentence_is_caught(self):
        """The reference's own wording does not put the word first."""
        def mutate(data):
            data["rejected"][0]["reason"] = "I am unsure whether B1 rests on this, so the checker decides."
        self.assertTrue(self.unsettled_for(mutate))

    def test_a_period_before_the_word_does_not_hide_it(self):
        """A decimal, an abbreviation or a statistic used to end the search early."""
        for reason in ("The sentence reports 4.1 minutes, and I am unsure whether B1 rests on it.",
                       "Reported as p < 0.05; unsure whether B1 rests on this.",
                       "Fig. 3 shows the same number, so I am unsure whether B1 rests on it.",
                       "Several passages, e.g. Section 5, make me unsure whether B1 rests on this."):
            with self.subTest(reason=reason[:40]):
                self.assertTrue(self.unsettled_for(
                    lambda data, r=reason: data["rejected"][0].__setitem__("reason", r)))

    def test_the_word_blocks_wherever_it_stands(self):
        """No quote parsing: every reading of the quoting has been wrong in four rounds.

        The word anywhere in the reason blocks, which cannot fail towards publishing an open
        question. A reason that only quotes the paper's own wording blocks too, and is reworded.
        """
        for reason in ('Unsure whether a main result rests on this.',
                       '  Unsure whether B1 rests on this.',
                       'The sentence reports 4.1 minutes, and I am unsure whether B1 rests on it.',
                       'Reported as p < 0.05; unsure whether B1 rests on this.',
                       'Fig. 3 shows the same number, so I am unsure whether B1 rests on it.',
                       'word"word"unsure',
                       'The abstract reports a 13" screen; unsure whether the 5" figure matters.',
                       'The paper says respondents were "unsure", so this is reworded.'):
            with self.subTest(reason=reason[:44]):
                self.assertTrue(self.unsettled_for(
                    lambda data, r=reason: data["rejected"][0].__setitem__("reason", r)))

    def test_a_reason_without_the_word_publishes(self):
        for reason in ("B1 would still stand, because the result is a detail.",
                       "Describes the data, not a result.",
                       "Ensure is not unsureness; this reason is settled."):
            with self.subTest(reason=reason[:44]):
                self.assertEqual(self.unsettled_for(
                    lambda data, r=reason: data["rejected"][0].__setitem__("reason", r)), [])

    def test_a_settled_record_publishes(self):
        self.assertEqual(self.unsettled_for(lambda data: None), [])


if __name__ == "__main__":
    unittest.main()
