---
name: cea-extract-claims
description: >-
  Extract the claims of a research paper from its PDF for Claim-Evidence Alignment (CEA): the
  narrow quantitative empirical claims, the broad abstract, contribution, and conclusion
  statements they serve, and the candidates considered and rejected, each with its exact wording,
  page, and reason. Writes claims.json and a readable claims.md, and checks every quote against
  the page text of the PDF. Use this skill whenever someone wants to extract, list, identify, or
  select the claims or key quantitative results of a paper PDF, prepare a paper for a
  claim-evidence, reproducibility, or artifact check, or start a CEA chain, even if they do not
  say "CEA" or "narrow claim". It reads the paper only. For linking an artifact appendix's claims
  to its experiments, use claim-evidence-map.
license: MIT
compatibility: Requires Python 3.10 or newer (standard library only) and pdftotext (poppler).
---

# Extract claims from a paper PDF

This skill drafts the claim selection step of Claim-Evidence Alignment (CEA) for one paper. For each narrow quantitative claim, CEA reconstructs the chain from the claim to its evidence, and a person, the checker, reviews each step an agent drafts. This first step records which claims the paper makes, where it makes them, and why each one was selected. Later steps start from this record. A claim missing here is never checked, and a wrong quote makes every later step work from the wrong passage.

The checker must be able to verify the record. Each statement is quoted exactly with its page, each selection and rejection has a reason that the checker can accept or overturn, and a script confirms that each quote is on its page.

## Stop conditions

- If `check-env` or `extract` fails, stop and report its output. Do not read the PDF yourself instead, because the validator can check quotes only against `text.txt`.
- Do not edit `text.txt`. The validator checks quotes against it.
- A paper may have no narrow claims. A qualitative study, for example, may give counts that describe its data but report no quantitative result. Say so, record the rejected candidates, and do not turn a qualitative finding into a claim.

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

Use the output directory that the user names, or `cea-out` in the current directory. The paper's files go to `<output directory>/<paper_id>/`, where `paper_id` is the PDF's file name without `.pdf`. `extract` also lists pages that are empty in `text.txt`, such as pages that held only references.

`text.txt` contains the paper's text, with a `=== page N ===` line before each page. N is the PDF page counted from 1, not the number printed on the page. Two-column text is in reading order, running headers and page numbers are removed, and so is the references section. Tables come out as rows of cells separated by spaces and can be hard to read. The poor readability of tables matters little, because a number inside a table is evidence, not a claim.

### 2. Read the definitions

Read `references/narrow-claims.md`. It defines narrow claims, broad statements, and rejected candidates, gives the selection question and the rule for frequency words without a number, says when to split a statement, and lists what is out of scope.

### 3. Read the whole paper

Read all of `text.txt`, in parts if it is long. Claims also appear in answers to research questions, figure captions, the discussion, and appendices. Skimming misses them. A missed claim is the error that a checker is least likely to notice.

### 4. Record the broad statements

Quote the statements that summarize findings: in the abstract, in the contribution list, in a summary of findings or a boxed answer to a research question, and in the conclusion. Record a statement only if at least one part of it states a quantitative result, as the reference defines it. Decide a boxed answer sentence by sentence, as the reference describes. A box sentence with a result number can be both a broad statement and a claim. Give each broad statement an id (`B1`, `B2`, ...) and a `source`: `abstract`, `contributions`, `conclusion`, or `other`. Leave out statements that report no finding, such as "we release our dataset" or "we analyze how the tools are configured". Also leave out recommendations in the discussion and novelty claims, such as "the first study of X".

### 5. Select the narrow claims

Look for statements that report a quantitative result of the study, such as a count, a difference, or an accuracy. This includes counts from qualitative coding, and statements with a frequency word but no number when the reference's rule makes them quantitative. For each candidate:

1. Find its most specific wording. A result stated in the results section is often repeated in the abstract, the discussion, or the conclusion. The narrow claim is the most specific statement, usually the sentence in the results section that gives the number. Record a repetition outside the broad statements as a rejected candidate, with `duplicate_of` listing the ids that it repeats.
2. Ask the selection question from the reference. If the answer is yes, record a claim (`C1`, `C2`, ...). The `selection_reason` names the broad statement or contribution that depends on the claim and says how. "Important result" is not a reason that a checker can assess.
3. Link the claim to every broad statement it serves, in `serves`. If a main result depends on a claim but no broad statement covers it, look again for a broad statement that you missed. If there is none, record the statement that states the main result with `source` `other`.
4. Split the statement when its parts need different evidence. Each part becomes a claim with the same `quote`, a `text` that states only that part, and a shared `split_from` id (`S1`, `S2`, ...). Write `text` with the words of the quote. It may repeat context from the same sentence, such as "Across the 48 projects", but must not add anything the quote does not say. If a part loses its referent, such as "them", name the referent in `note`.

Record a rejected candidate (`R1`, `R2`, ...) when a checker could expect a statement to be a claim. Common reasons are a quantitative result that no main result depends on, a repetition, and a number that describes the study, such as a sample size or an agreement score. The reference lists further reasons, such as the accuracy of a model that the findings are read from. Another reason is fine if it says why the statement is not a claim. Record every sentence in the abstract, the contribution list, a boxed answer, or the conclusion whose only numbers describe the study, with one entry for each sentence. You do not need to list the same numbers again from the method section.

Qualitative findings, formal claims, and novelty claims are out of scope. Do not record them, not even as rejected candidates.

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
      "selection_reason": "B1 says caching halves build time, and this comparison is the result behind it; a smaller drop would require revising B1."
    },
    {
      "id": "C2",
      "quote": "Across the 48 projects, median build time fell from 9.2 to 4.1 minutes, and the failure rate stayed at 3%.",
      "text": "Across the 48 projects, the failure rate stayed at 3%.",
      "page": 7,
      "section": "5.1 RQ1: Build time",
      "serves": ["B1"],
      "split_from": "S1",
      "selection_reason": "B1 says failures did not increase; if the failure rate had risen, B1 would be wrong.",
      "note": "The quote does not give the failure rate before caching."
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
  - Join lines with spaces, and write a word broken across two lines as one word. A quote may span consecutive sentences when the claim needs both, and it may leave out a footnote marker attached to a word.
  - Where a figure, table, footnote, or page break interrupts the sentence in `text.txt`, write `[...]` at that point. Never use `[...]` to shorten a sentence, because the checker reads the quote as the paper's full statement.
  - When a page break falls inside a word, write the word whole and give the page as a range such as `"8-9"`. If a table or figure also appears between the two halves of the word in `text.txt`, write `[...]` inside the word, such as `dis[...]tinct`.
- `text`: the quote without `[...]`, or, for one part of a split statement, only that part.
- `page`: N from the `=== page N ===` line, or a string such as `"8-9"` when the quote runs across a page break.
- `section`: the heading as printed, with its number, such as `5.2 RQ2: Review effort`, `III-B Coding Procedure`, or `Abstract`. For a paragraph that starts with a run-in heading, add that heading after a comma, such as `III-A Study Setup, Action Selection`.
- `split_from`: `null` unless the claim is one part of a split statement.
- `duplicate_of` (rejected candidates only): a list of the ids of the claims, rejected candidates, or broad statements that the statement repeats.
- `text` on a rejected candidate: only for a rejected part of a split statement, together with `split_from`.
- `note` (optional, on any entry): facts that the checker can confirm in the paper, such as why a selection is borderline, garbled text, a place where the paper's text and a table disagree, or a value below a threshold that the paper itself sets. Do not say whether a claim is supported, and do not decide which side of a disagreement is right, because later steps assess the evidence.

### 7. Validate

```sh
python3 <skill base directory>/scripts/cea_claims.py validate <output directory>/<paper_id>
```

Fix every problem and run it again until it prints `CEA_VALID`. If a quote is not found, copy it again from `text.txt`. If it was found on another page, correct `page`. The validator also checks that the `text` of a split part uses only words from its quote.

### 8. Render and report

```sh
python3 <skill base directory>/scripts/cea_claims.py render <output directory>/<paper_id>
```

The render command writes `claims.md`, with the claims and rejected candidates in page order. Then tell the user, briefly:

- where `claims.json` and `claims.md` are
- how many broad statements, claims, and rejected candidates you recorded
- which broad statements, if any, no narrow claim serves
- the two or three selections a checker is most likely to overturn, and why
- any extraction problems, such as pages with garbled text

## Left to later steps

Do not interpret the claims here. The constructs, unit of analysis, scope, and claim kind belong to the next CEA step, and a later step decides whether the evidence supports a claim. Adding them now would present unchecked interpretation as part of the record. If a claim's wording is vague, say so in a `note`. Step 6 says what a note may contain.
