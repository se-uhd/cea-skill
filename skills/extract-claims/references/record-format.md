# The claims.json record

The structure of `claims.json`, the fields whose format needs explaining, how to quote from
`text.txt`, and what the
validator warns about. Step 6 of the skill writes this file and step 7 validates it. The shape alone is also
published as JSON Schema, which `python3 <plugin root>/scripts/cea_claims.py schema` prints. The rules below
are what the schema cannot say.

## Structure

```json
{
  "format": 2,
  "paper": {
    "id": "icse25-build-cache",
    "title": "Remote Build Caching in Open-Source Projects",
    "pdf": "icse25-build-cache.pdf",
    "pages": 12
  },
  "main_results": [
    {
      "id": "R1",
      "quote": "Remote caching halves median build time without increasing build failures.",
      "page": 1,
      "section": "Abstract",
      "source": "abstract"
    }
  ],
  "claims": [
    {
      "id": "C1",
      "quote": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes, and the failure rate stayed at 3%.",
      "states": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes.",
      "page": 7,
      "section": "5.1 RQ1: Build time",
      "serves": ["R1"],
      "split_from": "S1",
      "selection_reason": "R1 says caching halves build time, and this comparison is the result behind it. If median build time had not fallen by about half, R1 would fail."
    },
    {
      "id": "C2",
      "quote": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes, and the failure rate stayed at 3%.",
      "states": "Across the 48 projects, the failure rate stayed at 3%.",
      "page": 7,
      "section": "5.1 RQ1: Build time",
      "serves": ["R1"],
      "split_from": "S1",
      "selection_reason": "R1 says failures did not increase. If the failure rate had risen, R1 would be wrong.",
      "note": "Table 4 gives the failure rate after caching as 4.1%."
    }
  ],
  "excluded": [
    {
      "id": "E1",
      "quote": "We collected 1,203 builds from 48 projects.",
      "page": 5,
      "section": "4.2 Data collection",
      "reason": "Describes the data, not a result."
    },
    {
      "id": "E2",
      "quote": "As Section 5.1 showed, caching cut median build time from 9.2 to 4.1 minutes.",
      "page": 9,
      "section": "6 Discussion",
      "duplicate_of": ["R1", "C1"],
      "reason": "Repeats R1's result, whose number C1 gives."
    }
  ]
}
```

## Fields

- Ids run in page order within each list, which is the order `render` prints them in. They are the permanent anchors of the published page, so a later change never renumbers them: a new entry takes the next free number even where that puts it out of order.
- `format`: the record format, `2` for this build. A record without it is read as format 1, which this build does not write, so it is refused and told what changed. It says which meaning the fields carry, so that a record written against an older format cannot pass every check and publish a number that means something else. `validate` refuses a record whose format this build does not read, stamped or not, and names what has changed since. Raise it only together with the field whose meaning changed.

- `paper.pdf`: the file name that `extract` printed after `pdf:`, which is the copy it put in the record's own directory beside `claims.json`. A name, not a path: no directories and no `..`, because the site publishes that file beside the page and a path would name a file on one machine only. Where `extract` printed no `pdf:` line, it said why. Write the paper's file name anyway, and the page will name the paper without linking it. `paper.pages`: the number of pages in `text.txt`.
- A digit that ends a name is part of the name, not a footnote marker. That covers a hyphenated name such as `deepseek-v3` or `gpt-4`, one the extraction broke across two lines, a version such as `v1.0` or `@v1`, and a source label such as `[E07]` or `(P13)`. Copy the digit. A quote that drops it names a model, release, or source the paper does not.
- `quote`: the exact wording, copied from `text.txt` as extracted, including characters and whole words that the extraction garbled, such as a summation sign that comes out inside the word beside it (explain them in `note`).
  - Join lines with spaces, and write a word broken across two lines as one word. A quote may span consecutive sentences when the claim needs both, and it may leave out a footnote marker attached to a word. Quote whole sentences. The validator warns about a quote that starts or ends in the middle of a sentence.
  - Where a figure, table, footnote, or page break puts text that is not the sentence's own between two of its lines in `text.txt`, write `[...]` at that point. One `[...]` may skip at most 4000 characters, which is under a page of most papers. Where more than that stands between two lines of one sentence, the validator says so and names the page: quote the part of the sentence that stands unbroken and say in `note` what interrupts it. A page break with nothing between the two halves needs no marker. The validator accepts `[...]` only between two lines of `text.txt`, never inside a line. It refuses a quote whose `[...]` skips a section heading, because a sentence does not cross one; one whose `[...]` skips the paper's own running text, because that joins text the paper does not join; and one that starts in a row of a table and ends in another row, because two rows are two records and a marker stands for a table interrupting a sentence, not for the step from one row to the next. Quote one row on its own, with no marker. Where the paper itself prints `[...]`, for example in a quoted post, copy it as it stands, and the validator reads it as the paper's own text. Never use `[...]` to shorten a sentence, because the checker reads the quote as the paper's full statement.
  - When a page break falls inside a word, write the word whole and give the page as a range such as `"8-9"`. If a table or figure also appears between the two halves of the word in `text.txt`, write `[...]` inside the word, such as `dis[...]tinct`.
- A quote whose own lines in `text.txt` read as a row of a table rather than as a sentence draws a warning on a claim. A number that stands only in a table is evidence for a claim, not a claim itself. An excluded candidate may quote such a row, which is the rule being followed.
- `states` gives the quote's words in the order the quote gives them, so that each value stays beside what it measures. Swapping two measures between the parts of one sentence is refused. Where a part needs a word the quote elides, such as a subject the sentence states once for two clauses, write that word in square brackets: `15 [projects] had a significant downward trend`. One such borrow per part, and never a number.
- The rule reaches what the words say, not what they mean: a sentence's own words in its own order can still pair a value with the wrong item, because a coordinated sentence prints both pairs. Check the pairing against the paper, and say in `note` how it goes.
- `states` on a claim: the words of the quote that the claim states. Every claim has it. For a claim that is not one part of a split statement it repeats the quote, without any `[...]`. For one part it gives only that part.
- `page`: N from the `=== page N ===` line, or a string such as `"8-9"` when the quote runs across a page break. A range names two consecutive pages, and only when the quote needs both.
- `section`: the heading as printed, with its number, such as `5.2 RQ2: Review effort`, `III-B Coding Procedure`, or `Abstract`. Where the heading spells out a research question or another full sentence, keep its number and the words that identify it, such as `IV-B RQ2`, because the section only has to lead the checker to the page. For a paragraph that begins with its own short heading, such as "Action Selection.", add that heading after a comma, such as `III-A Study Setup, Action Selection`. For a list or a box that has no heading of its own, give the enclosing section and then what it is, as in `1 Introduction, contribution list`. For a statement in a caption, a footnote, or a boxed answer, give the enclosing section and then the label of the caption, footnote, or box after a comma, such as `IV-A Annotation Overview, Fig. 2` or `5.2 Results, Summary RQ3`.
- `split_from`: `null` unless the claim is one part of a split statement.
- `duplicate_of` (excluded candidates only): a list of the ids of the claims, excluded candidates, or main results that the statement repeats. A sentence that repeats a main result names that main result, so that the record shows how many places the paper states it.
- `breaks_down` (excluded candidates only): a list of the ids of the main results whose result the statement divides into parts. A breakdown is not a place where the paper states the result, so it is kept apart from `duplicate_of`, which counts the places.
- `states` on an excluded candidate: only for a rejected part of a split statement, together with `split_from`.
- `states` on a main result (optional): the clause that the statement is recorded for, where its sentence also repeats another main result. Where one sentence states two main results it is one entry all the same, and the `note` names both, because a second statement on the same sentence is refused. Its words come from the quote, and the `note` names the statement that holds the rest.
- `note` (optional, on any entry): only facts that the checker can confirm in the paper, each with its location. A note can say:
  - that the extraction garbled characters in the quote
  - that two passages, or the text and a table or figure, give a quantity or an ordering differently, quoting both as printed and naming both locations
  - that a value is below a threshold that the paper states
  - where a number that the sentence leaves out is printed, or that it is printed nowhere
  - what a word such as "them" or "these posts" refers to when the quote does not say it, whether or not the entry is one part of a split statement
  - which rows or parts a table prints where the sentence gives a whole, named as the table prints them
  - how the numbers of a sentence that pairs them with "respectively" go with its items, naming the number this part states
  - which main results a sentence states, when it states more than one, and which main result already states a main result that it repeats
  - why a main result has no claim, or why it has `source` `other`
  - which main result or passage might depend on an excluded candidate
  - what is unclear when you cannot tell what a frequency word rests on
  - that text which is not part of the sentence runs inside one of its lines on the page, and that the quote is the part that stands unbroken
  - for a borderline decision, the passages that support each choice

  Do not compare the claim with the data, model, sample, or population behind it. Do not add, divide, or otherwise compute values from tables. Do not say whether a claim is supported or which side of a disagreement is right, because later steps assess the evidence.

## What the validator warns about

- a `note` that says its values are the parts of a number, naming that number as in "the parts of the sentence's 1,203", and then names values that do not add up to it. The check reads that phrasing, so a note that gives the rows without saying what they are the parts of is not checked
- a split quote with "respectively", until every part's note says how the pairing goes
- a part, or a main result's `states`, that reorders numbers or loses a comparison word
- a part, or a main result's `states`, that keeps a word the quote negates without the negation before it
- a quote that contains another entry's quote, which the reference's rule for a box sentence and its context expects
- a `selection_reason` that names none of the main results in `serves`
- a quote that starts or ends in the middle of a word or a number
- two entries that quote the same sentence on different pages
- an excluded candidate's `reason` that runs under three words, or gives a verdict instead of a ground, such as "Important result", or that says no main result depends on the candidate although the record has main results
- main results that the same claims serve, which read as one main result stated several times
- two main results that share most, but not all, of their claims
- a heading copied whole into `section`
- a `duplicate_of` that names a claim but no main result, which leaves the repetition uncounted
- a main result whose claims all serve another statement that more claims serve, which reads as a breakdown of that result rather than a result of its own
- a boxed answer whose `source` is not `rq_answer`
