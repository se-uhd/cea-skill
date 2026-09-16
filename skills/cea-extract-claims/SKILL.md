---
name: cea-extract-claims
description: >-
  Extract the claims of a research paper from its PDF for Claim-Evidence Alignment (CEA): the narrow
  quantitative empirical claims, the broad statements from the abstract, contributions,
  research-question answers, and conclusion that they serve, and the candidates considered and
  rejected, each with its exact wording, page, and reason. Writes claims.json and a readable
  claims.md, and checks every quote against the page text of the PDF. Use this skill whenever
  someone wants to extract, list, identify, or select the claims or key quantitative results of a
  paper PDF, prepare a paper for a claim-evidence, reproducibility, or artifact check, or start a
  CEA chain, even if they do not say "CEA" or "narrow claim". It reads the paper only. For linking
  an artifact appendix's claims to its experiments, use claim-evidence-map.
license: MIT
compatibility: Requires Python 3.10 or newer (standard library only) and pdftotext (poppler).
---

# Extract claims from a paper PDF

This skill drafts the claim selection step of Claim-Evidence Alignment (CEA) for one paper. For each narrow quantitative claim, CEA reconstructs the chain from the claim to its evidence, and a person, the checker, reviews each step that an agent drafts. This first step records which claims the paper makes, where it makes them, and why each claim was selected. Later steps start from this record. A claim missing here is never checked, and a wrong quote makes every later step work from the wrong passage.

The checker must be able to verify the record. Each statement is quoted exactly with its page, and each selection and rejection has a reason that the checker can accept or overturn. A script, the validator, confirms that each quote is on its page.

## Stop conditions

- If `check-env` or `extract` fails, stop and report its output. Do not read the PDF yourself instead, because the validator can check quotes only against `text.txt`.
- Do not edit `text.txt`. The validator checks quotes against it.
- A paper may have no narrow claims. A qualitative study, for example, may give counts that describe its data but report no quantitative result. Say that the paper has no narrow claims, record the rejected candidates, and do not turn a qualitative finding into a claim.
- A paper may also have no broad statements, when every main result it states is qualitative. Record the rejected candidates, say that the paper has no broad statements and no claims, and do not turn a qualitative main result into a broad statement. The paper still has main results. None of them is recorded as a broad statement, so no reason can name a broad statement id. Every reason then names the ground that excludes its candidate, or says that no main result depends on it.

## Scripts

The scripts are in `scripts/` in this skill's base directory, which Claude Code shows when it loads the skill. Run them as:

```sh
python3 <skill base directory>/scripts/cea_claims.py <command> ...
```

The first output line starts with `CEA_OK`, `CEA_EXTRACTED`, `CEA_VALID`, or `CEA_RENDERED` on success, and with `CEA_FAILED` or `CEA_INVALID` otherwise.

## Workflow

### 1. Extract the text

```sh
python3 <skill base directory>/scripts/cea_claims.py check-env
python3 <skill base directory>/scripts/cea_claims.py extract <paper.pdf> --out <output directory>
```

Use the output directory that the user names, or `cea-out` in the current directory. The paper's files go to `<output directory>/<paper_id>/`, where `paper_id` is the PDF's file name without `.pdf`. `extract` also lists pages that are empty in `text.txt`, such as pages that held only references. In a two-column paper, it lists the pages that it left in one column as well. The lines of two columns can be mixed on any page, whatever the report says, so text of one column can interrupt a sentence of the other. A sentence stays quotable unless text that is not its own runs inside it and `[...]` cannot cover the break, because `[...]` stands only between two lines, never inside one. Where that happens, quote the longest run of the sentence that stands unbroken in `text.txt`, and say in `note` that the page mixes the two columns and that the quote is part of the sentence on that page. This is the one case where a quote is not a whole sentence, so the validator's warning about a quote that starts or ends in the middle of a sentence is expected here. Name the page in your report as well. Cells of a table printed between the lines of a sentence are normal and do not stop a quote.

`text.txt` contains the paper's text, with a `=== page N ===` line before each page. N is the PDF page counted from 1, not the number printed on the page. Two-column text is in reading order. Running headers, page numbers, and the references section are removed. Tables come out as rows of cells separated by spaces and can be hard to read. A number inside a table is evidence, not a claim, but read the tables anyway. They decide whether a frequency word rests on counts, and they show where the text and a table disagree.

### 2. Read the definitions

Read `references/narrow-claims.md`. It defines main results, broad statements, narrow claims, and rejected candidates. It also gives the selection question and the rule for frequency words without a number, says when to split a statement, and lists what is out of scope.

### 3. Read the whole paper

Read all of `text.txt`, in parts if it is long. A candidate is a statement that could be a claim until the selection question decides. Claims also appear in answers to research questions, figure captions, the discussion, and appendices. Skimming misses them. A statement missing from the whole record is the error that a checker is least likely to notice, so record a candidate that you are unsure about as a rejected candidate with its reason, where the checker can change it into a claim. A detail selected as a claim costs the checker a full chain and hides the claims that matter. While reading, look for a sentence saying that the paper does not quantify prevalence, usually in the limitations. The reference's rule 3 for frequency words turns on it, and it often stands pages after the statements it governs.

### 4. List the main results as broad statements

Before judging any candidate, list the paper's main results and key contributions as the reference defines them, and record each main result once as a broad statement, quoting the sentence that states it. A qualitative main result gets no broad statement, as the reference says. List it all the same and name it in your report, because the ground for rejecting a coded count turns on whether every main result of the paper is qualitative. The reference's section "Broad statements" says which sentences qualify, which sentence to quote when several state the same main result, and where the other sentences go. Give each broad statement an id (`B1`, `B2`, ...) and a `source`: `abstract`, `contributions`, `rq_answer`, `conclusion`, or `other` for a main result that only the results or the discussion states. A broad statement with `source` `other` needs a `note` that says why, and so does a broad statement that no claim serves.

### 5. Select the narrow claims

Look for statements that report a quantitative result of the study, such as a count, a difference, or an accuracy. Also look for counts from qualitative coding, and for statements with a frequency word but no number when the reference's rule makes them quantitative. For each candidate:

1. Find its most specific wording, as the reference defines it. A result stated in the results section is often repeated in the abstract, the discussion, or the conclusion. The narrow claim is the most specific statement, usually the sentence in the results section that gives the number. Record a repetition outside the broad statements as a rejected candidate, with `duplicate_of` listing the ids that it repeats, where the sentence restates the result as a finding. A passing mention, such as the number named again in the threats section or inside a sentence about something else, is left out, because a checker would not take it for a claim.
2. Split the statement when the reference's section "Splitting" says so. Each part gets the same `quote`, a `text` that states only that part, and a shared `split_from` id (`S1`, `S2`, ...). Write `text` with the words of the quote. `text` may repeat context from the same sentence, such as "Across the 48 projects", but must not add anything that the quote does not say. If a part no longer says what a word such as "them" refers to, name it in `note`.
3. Ask the selection question from the reference, for each part of a split statement separately. Name the main result by the id of its broad statement, as in "B1 says ...", and say how it would fail or need substantial revision if the candidate were false in the sense that the reference defines. Then record it:
   - The main result would fail: a claim (`C1`, `C2`, ...), whose `selection_reason` says how it would fail.
   - The reference's rule for a main result that rests on several results together applies: a claim as well, because that rule decides. Its `selection_reason` names the broad statement and says which basis of it this claim supplies, as in "B1 rests on four factor results together, and this is the one for manual triggering".
   - The main result would still stand: a rejected candidate whose `reason` names that main result by the id of its broad statement, as in "B2 would still stand, because ...".
   - The question never reaches the candidate, because it is out of scope: a rejected candidate whose `reason` names one of the grounds that the reference's section "Selection question" lists. Only where the record has no broad statement does a reason say instead that no main result depends on the candidate.
   - You cannot tell whether the main result rests on it: a rejected candidate that says so in its `reason`, as in "Unsure whether B2 rests on this, recorded as rejected so that the checker can change it into a claim", and names in `note` the passages that support each choice.

   "Important result" is not a reason that a checker can assess.
4. Link each claim to every broad statement that it serves, in `serves`. If no broad statement states the main result that a candidate seems to support, look again in the abstract, the contribution list, the boxed answers, the conclusion, and the discussion. If none states it, record the candidate as a rejected candidate whose `reason` is that no broad statement states the main result it would serve, and name in `note` any passage that builds on it, so that the checker can change it into a claim. A claim quotes the same sentence as a broad statement only under the reference's rule that applies when a broad statement's own sentence alone gives the number of its main result, whatever its source.

Take each claim and each rejected candidate where the sentence gives a number for something that a table or a figure also reports, or where a sentence near it names the table or figure reporting the result. Find that table or figure and compare the two. Reading the cell that the sentence points at is part of this comparison. Combining cells, or working a value out from several, is not. Where the text and the table or figure give a quantity or an ordering differently, record both in `note` as printed, with both locations, and do not say which is right.

Record a rejected candidate (`R1`, `R2`, ...) when a checker could expect a statement to be a claim. Common reasons are a result that leaves its broad statement standing, named by that statement's id, a repetition, and a number that describes the study, such as a sample size or an agreement score. Only where the record has no broad statement does a reason say that no main result depends on the candidate. The reference lists further reasons, such as the accuracy of a model that the paper only reads other findings from. Another reason is fine if it says why the statement is not a claim. A sentence in the abstract, the contribution list, a boxed answer, or the conclusion is a rejected candidate when it gives at least one number, every one of its numbers describes the study, and it states no quantitative result. Record one entry for each such sentence. A sentence recorded as a broad statement is never also a rejected candidate on its own, although one part of it can be a rejected part when the reference's rule splits it. Where a broad statement's sentence also gives a number that describes the study, record that number from the method sentence instead, unless a summary sentence that is already a rejected candidate states it. Also record the sizes of the study, as the reference's section "Results and descriptions of the study" describes.

Qualitative findings, formal claims, and novelty claims are out of scope. Do not record them, not even as rejected candidates. A statement whose basis the paper does not make clear is the exception, and the reference's rule for frequency words records it as a rejected candidate with a note. A summary sentence that combines a qualitative finding with numbers that describe the study is still a rejected candidate under the rule above.

### 6. Write claims.json

Write `<output directory>/<paper_id>/claims.json`. Use this structure:

```json
{
  "paper": {
    "id": "icse25-build-cache",
    "title": "Remote Build Caching in Open-Source Projects",
    "pdf": "papers/icse25-build-cache.pdf",
    "pages": 12
  },
  "broad_statements": [
    {
      "id": "B1",
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
      "text": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes.",
      "page": 7,
      "section": "5.1 RQ1: Build time",
      "serves": ["B1"],
      "split_from": "S1",
      "selection_reason": "B1 says caching halves build time, and this comparison is the result behind it. If median build time had not fallen by about half, B1 would fail."
    },
    {
      "id": "C2",
      "quote": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes, and the failure rate stayed at 3%.",
      "text": "Across the 48 projects, the failure rate stayed at 3%.",
      "page": 7,
      "section": "5.1 RQ1: Build time",
      "serves": ["B1"],
      "split_from": "S1",
      "selection_reason": "B1 says failures did not increase. If the failure rate had risen, B1 would be wrong.",
      "note": "Table 4 gives the failure rate after caching as 4.1%."
    }
  ],
  "rejected": [
    {
      "id": "R1",
      "quote": "We collected 1,203 builds from 48 projects.",
      "page": 5,
      "section": "4.2 Data collection",
      "reason": "Describes the data, not a result."
    },
    {
      "id": "R2",
      "quote": "As Section 5.1 showed, caching cut median build time from 9.2 to 4.1 minutes.",
      "page": 9,
      "section": "6 Discussion",
      "duplicate_of": ["C1"],
      "reason": "Repeats C1."
    }
  ]
}
```

- `paper.pdf`: the path to the PDF as the user gave it. `paper.pages`: the number of pages in `text.txt`.
- `quote`: the exact wording, copied from `text.txt` as extracted, including characters that the extraction garbled (explain them in `note`).
  - Join lines with spaces, and write a word broken across two lines as one word. A quote may span consecutive sentences when the claim needs both, and it may leave out a footnote marker attached to a word. Quote whole sentences. The validator warns about a quote that starts or ends in the middle of a sentence.
  - Where a figure, table, footnote, or page break interrupts the sentence in `text.txt`, write `[...]` at that point. The validator accepts `[...]` only between two lines of `text.txt`, never inside a line. Where the paper itself prints `[...]`, for example in a quoted post, copy it as it stands, and the validator reads it as the paper's own text. Never use `[...]` to shorten a sentence, because the checker reads the quote as the paper's full statement.
  - When a page break falls inside a word, write the word whole and give the page as a range such as `"8-9"`. If a table or figure also appears between the two halves of the word in `text.txt`, write `[...]` inside the word, such as `dis[...]tinct`.
- `text`: the quote without `[...]`, or, for one part of a split statement, only that part.
- `page`: N from the `=== page N ===` line, or a string such as `"8-9"` when the quote runs across a page break. A range names two consecutive pages, and only when the quote needs both.
- `section`: the heading as printed, with its number, such as `5.2 RQ2: Review effort`, `III-B Coding Procedure`, or `Abstract`. For a paragraph that begins with its own short heading, such as "Action Selection.", add that heading after a comma, such as `III-A Study Setup, Action Selection`. For a statement in a caption, a footnote, or a boxed answer, give the enclosing section and then the label of the caption, footnote, or box after a comma, such as `IV-A Annotation Overview, Fig. 2` or `5 Impact on Code Churn, Summary RQ3`.
- `split_from`: `null` unless the claim is one part of a split statement.
- `duplicate_of` (rejected candidates only): a list of the ids of the claims, rejected candidates, or broad statements that the statement repeats.
- `text` on a rejected candidate: only for a rejected part of a split statement, together with `split_from`.
- `note` (optional, on any entry): only facts that the checker can confirm in the paper, each with its location. A note can say:
  - that the extraction garbled characters in the quote
  - that two passages, or the text and a table or figure, give a quantity or an ordering differently, quoting both as printed and naming both locations
  - that a value is below a threshold that the paper states
  - where a number that the sentence leaves out is printed, or that it is printed nowhere
  - what a word such as "them" refers to when a part of a split statement no longer says it
  - which main results a sentence states, when it states more than one, and which broad statement already states a main result that it repeats
  - why a broad statement has no claim, or why it has `source` `other`
  - which main result or passage might depend on a rejected candidate
  - what is unclear when you cannot tell what a frequency word rests on
  - that the page mixes the two columns, and that the quote is part of a sentence on it
  - for a borderline decision, the passages that support each choice

  Do not compare the claim with the data, model, sample, or population behind it. Do not add, divide, or otherwise compute values from tables. Do not say whether a claim is supported or which side of a disagreement is right, because later steps assess the evidence.

### 7. Validate

```sh
python3 <skill base directory>/scripts/cea_claims.py validate <output directory>/<paper_id>
```

Fix every problem and run it again until it prints `CEA_VALID`. After that line, it can print `warning:` lines, for example about a quote that ends in the middle of a sentence. Read each warning, and fix the record where it is right. If a quote is not found, copy it again from `text.txt`. If it was found on another page, correct `page`. The validator also checks that the `text` of a split part uses only words from its quote. The validator warns when no part of a split statement keeps a negation of the quote. The negation words are not (except in "not only"), no, never, neither, nor, none, without, cannot, and a word ending in "n't". Keep the negation unless it belongs to a clause that is out of scope, such as a qualitative finding. It also warns about:

- a split quote with "respectively"
- a part that reorders numbers or loses a comparison word
- a part that separates a negation from the word it negates
- a quote that contains another entry's quote, which the reference's rule for a box sentence and its context expects
- a `selection_reason` that names none of the broad statements in `serves`
- a `[...]` that skips text reading like part of the sentence
- two entries that quote the same sentence on different pages
- a rejected candidate's `reason` that names neither a main result nor one of the reference's grounds. Expect this warning where the reference gives a ground that the script does not know, such as a candidate whose basis the paper leaves unclear, and leave the reason as the reference asks

It cannot tell whether a part pairs a number with the wrong item, so reread each part against its quote.

### 8. Render and report

```sh
python3 <skill base directory>/scripts/cea_claims.py render <output directory>/<paper_id>
```

The render command writes `claims.md`, with the claims and rejected candidates in page order. Then tell the user, briefly:

- where `claims.json` and `claims.md` are
- how many broad statements, claims, and rejected candidates you recorded
- which broad statements, if any, no narrow claim serves, or that the paper has none
- any selection you were unsure about, with its id and one sentence saying why, and say so if there were none
- the paper's main results that got no broad statement, and why each of them is qualitative
- whether the paper's own sentence about not quantifying prevalence removed statements that give a frequency only in words, and where that sentence stands, and say so if the paper has no such sentence
- any extraction problems, such as pages with garbled text

## Left to later steps

Do not interpret the claims here. The next CEA step interprets each claim, as the reference's section "Not part of this step" describes, and a later step decides whether the evidence supports a claim. An interpretation added now would look like a checked part of the record.
