---
name: extract-claims
description: >-
  Extract a research paper's claims from its PDF for Claim-Evidence Alignment (CEA): the narrow
  quantitative claims, the main results from the abstract, contributions, research-question
  answers, and conclusion they serve, and the excluded claim candidates, each with its exact wording,
  page, and reason. A claim is one a main result would fail without: the numbers a paper's
  conclusions rest on. Writes claims.json, claims.md, and a claims.html page, and checks every quote
  against the PDF's text. Use it when someone wants to extract, list, identify, or select the claims
  or key quantitative results of a paper PDF, prepare a paper for a claim-evidence, reproducibility,
  or artifact check, or start a CEA chain, even if they do not say "CEA" or "claim". It reads
  the paper only and comes first, before any evidence is examined. Later steps judge that evidence.
  Linking those claims to an artifact's experiments is a later CEA step, not one this plugin covers. For a site of several papers' pages, use the site skill.
license: MIT
compatibility: Requires Python 3.10 or newer (standard library only) and pdftotext (poppler).
---

# Extract claims from a paper PDF

This skill drafts the claim selection step of Claim-Evidence Alignment (CEA) for one paper. For each narrow quantitative claim, CEA reconstructs the chain from the claim to its evidence, and a person, the checker, reviews each step that an agent drafts. This first step records which claims the paper makes, where it makes them, and why each claim was selected. Later steps start from this record. A claim missing here is never checked, and a wrong quote makes every later step work from the wrong passage.

The checker must be able to verify the record. Each statement is quoted exactly with its page, and each selection and rejection has a reason that the checker can accept or overturn. A script, the validator, confirms that each quote is on its page. A candidate is a statement that could be a claim; one that is not selected is an excluded claim candidate where a checker could expect it to be a claim. A main result quotes the sentence that states it: the finding the paper puts forward as what the study shows, or the tool, model, or framework the paper presents as a contribution and evaluates.

## Stop conditions

- If `check-env` or `extract` fails, stop and report its output. Do not read the PDF yourself instead, because the validator can check quotes only against `text.txt`.
- Do not edit `text.txt`.
- A paper may have no claims. A qualitative study, for example, may give counts that describe its data but report no quantitative result. Say that the paper has no claims, record the excluded claim candidates, and do not turn a qualitative finding into a claim.
- A paper may also have no main results recorded, when every finding it puts forward is qualitative. Record the excluded claim candidates, say that the paper has no main results and no claims, and do not record a qualitative finding as a main result. No main result id then exists for a reason to name, so every reason names the ground that excludes its candidate, as "Selection question" in `references/framework.md` describes.

## Scripts

The scripts are in `scripts/` at the root of the plugin, beside the `skills/` directory that holds this skill, and the other CEA skills share them. Claude Code shows the skill's base directory when it loads the skill, and the plugin root is two levels above it. Run them as:

```sh
python3 <plugin root>/scripts/cea_claims.py <command> ...
```

The first output line starts with `CEA_OK`, `CEA_EXTRACTED`, `CEA_VALID`, or `CEA_RENDERED` on success, and with `CEA_FAILED`, `CEA_INVALID`, or `CEA_UNRESOLVED` otherwise, except that a `CEA_WARNING` line can come first, for example when `extract` had to derive the paper id. The exit code decides, not the first marker: `render` prints `CEA_RENDERED` for `claims.md` and then fails on an open decision. The other lines carry no marker and are meant to be read, such as the page count and the list of tables and figures that `extract` prints.

## Workflow

### 1. Extract the text

```sh
python3 <plugin root>/scripts/cea_claims.py check-env
python3 <plugin root>/scripts/cea_claims.py extract <paper.pdf> --out <output directory>
```

Use the output directory that the user names, or `cea-out` in the current directory. The paper's files go to `<output directory>/<paper_id>/`, where `paper_id` is the PDF's file name without `.pdf`, folded to letters, digits, dot, dash, and underscore so that it can name a directory in the site. `extract` prints the id it derived, and `--id` sets it instead. `extract` copies the PDF in beside `text.txt` and prints the name to write as `paper.pdf`, so the record is complete on its own and the site can publish the paper beside the page. Where it prints no `pdf:` line, it says why, and the page will name the paper without linking it. `extract` also lists pages that are empty in `text.txt`, such as pages that held only references. It lists two-column pages left in one column too.

The lines of two columns can be mixed on any page, whatever `extract` lists, so text of one column can interrupt a sentence of the other. A `[...]` may not skip a section heading, nor the paper's own running text: the validator refuses such a quote, because a sentence does not cross a section and a quote may not join what the paper keeps apart. A sentence stays quotable unless text that is not its own runs inside it and `[...]` cannot cover the break, because `[...]` stands only between two lines, never inside one. Where such a break falls inside a line, quote the longest run of the sentence that stands unbroken in `text.txt`. Say in `note` that other text, such as the other column's, runs inside one of the sentence's lines on that page, and that the quote is the part that stands unbroken. Such a quote is the one case where a quote is not a whole sentence. The validator's warning about a quote that starts or ends in the middle of a sentence is expected here. Name the page in your report as well. Cells of a table printed between the lines of a sentence are normal and do not stop a quote.

`text.txt` contains the paper's text, with a `=== page N ===` line before each page. N is the PDF page counted from 1, not the number printed on the page. Two-column text is in reading order. Running headers, page numbers, and the references section are removed. Tables come out as rows of cells separated by spaces and can be hard to read. A number inside a table is evidence, not a claim, but read the tables anyway. They decide whether a frequency word rests on counts, and they show where the text and a table disagree.

### 2. Read the definitions

Read `references/framework.md`: its "Scope", its "Glossary", and all of "1. Claim selection", the part this skill applies. It defines main results, claims, and excluded claim candidates. The steps below name its subsections in quotation marks, as in "Splitting".

### 3. Read the whole paper

Read all of `text.txt`, in parts if it is long. Claims also appear in answers to research questions, figure captions, the discussion, and appendices. Skimming misses them. A statement missing from the whole record is the error that a checker is least likely to notice, and a detail selected as a claim costs the checker a full chain and hides the claims that matter. While reading, look for a sentence saying that the paper does not quantify prevalence, usually in the limitations. The reference's rule 3 for frequency words turns on it, and it often stands pages after the statements it governs.

### 4. List the main results

Before judging any candidate, list the paper's main results and key contributions, and record each main result once, quoting the sentence that states it. A qualitative finding gets no main result entry. List it all the same and name it in your report, because the ground for rejecting a coded count turns on whether every main result of the paper is qualitative. "Recording a main result" says which sentences qualify, which sentence to quote when several state the same main result, and where the other sentences go. Give each main result an id (`R1`, `R2`, ...) and a `source`: `abstract`, `contributions`, `rq_answer`, `conclusion`, or `other` for a main result that only the results or the discussion states. A main result with `source` `other` needs a `note` that says why, and so does a main result that no claim serves.

### 5. Select the claims

Look for statements that report a quantitative result of the study, such as a count, a difference, or an accuracy. Also look for counts from qualitative coding, and for statements with a frequency word but no number when the reference's rule makes them quantitative. For each candidate:

1. Find its most specific wording, as the reference defines it. A result stated in the results section is often repeated in the abstract, the discussion, or the conclusion. The claim is the most specific statement, usually the sentence in the results section that gives the number. Record a repetition outside the main results as an excluded claim candidate, with `duplicate_of` listing the ids that it repeats, where the sentence restates the result as a finding. Where it repeats a main result, name that main result, not only the claim that gives its number. Where a summary sentence divides a main result into parts instead of restating it, record it as a breakdown, with `breaks_down` in place of `duplicate_of`, or, where no main result states that result, with the `reason` naming the entry that does. A passing mention, such as the number named again in the threats section or inside a sentence about something else, is left out, because a checker would not take it for a claim.
2. Split the statement when "Splitting" says so. Each part gets the same `quote`, a `states` that gives only that part, and a shared `split_from` id (`S1`, `S2`, ...). Write `states` with the words of the quote. `states` may repeat context from the same sentence, such as "Across the 48 projects", but must not add anything that the quote does not say. If a part no longer says what a word such as "them" refers to, name it in `note`.
3. First ask whether any main result is recorded for the finding the candidate would serve. If none does, item 4 decides and the selection question is not asked. Otherwise ask the selection question from the reference, for each part of a split statement separately. Name the main result by its id, as in "R1 says ...", and say how it would fail or need substantial revision if the candidate were false in the sense that the reference defines. Then record the candidate:
   - The main result would fail: a claim (`C1`, `C2`, ...), whose `selection_reason` says how it would fail.
   - The reference's rule for a main result that rests on several results together applies: a claim as well, because that rule decides. Its `selection_reason` names the main result and says which basis of it this claim supplies, as in "R1 rests on the results for four factors together, and this is the one for manual triggering".
   - The main result would still stand: an excluded claim candidate whose `reason` names that main result by its id, as in "R2 would still stand, because ...".
   - The question never reaches the candidate, because it is out of scope: an excluded claim candidate whose `reason` names one of the grounds that "Selection question" lists. Only where the record has no main result, and no other ground fits, does a reason say instead that nothing depends on the candidate.
   - You cannot tell whether the main result rests on it: an excluded claim candidate that says so in its `reason`, as in "Unsure whether R2 rests on this, recorded as an excluded claim candidate so that the checker can change it into a claim", and names in `note` the passages that support each choice.

   "Important result" is not a reason that a checker can assess.
4. Link each claim to every main result that it serves, in `serves`. If no main result is recorded for the finding a candidate seems to support, look again in the abstract, the contribution list, the boxed answers, the conclusion, and the discussion. If none states it, record the candidate as an excluded claim candidate. Its `reason` is that no main result is recorded for the finding it would serve. Name in `note` any passage that builds on it, so that the checker can change it into a claim. Where the record has no main result at all, that ground holds of every candidate and so says nothing: name the ground that excludes this one on its own terms, as "Selection question" describes. A claim quotes the same sentence as a main result only under the reference's rule that applies when a main result's own sentence alone gives its number, whatever its source.

`extract` prints the paper's tables and figures with their pages, and reading `text.txt` fills in what each one counts. That short list makes the comparison below a lookup rather than a search of the whole paper for every number.

Compare an entry with a table or figure where its sentence gives a number for a quantity that the list names, or where the sentence immediately before or after it names one. Read the cell that the sentence points at. Do not combine cells, and do not work a value out from several. These cases end in a `note` instead of a comparison:

- The table prints only the parts of the sentence's value, whether as counts under a share or as rows under a total. Say so in `note`, name the rows as printed, and leave the arithmetic to a later step. Such a table is the usual case where a paper's text gives category totals, so a comparison that reads one cell is often impossible, and saying that in the note is the whole of what this step asks.
- The text and the table or figure give a quantity or an ordering differently. Record both in `note` as printed, with both locations, and do not say which is right. A table that splits what the sentence groups, or prints the sentence's number under a different heading, gives it differently in this sense, even where a cell matches. The `note` then names the heading the table prints it under, so that the checker can see the mismatch without opening the paper.
- The extraction has left a figure as scattered labels. Say so in `note` and compare nothing.

Record an excluded claim candidate (`E1`, `E2`, ...) when a checker could expect a statement to be a claim. Common reasons are a result that leaves its main result standing (named by that statement's id), a repetition, and a number that describes the study, such as a sample size or an agreement score. The reference lists further reasons, such as the accuracy of a model that the paper only reads other findings from. Another reason works if it says why the statement is not a claim. Also record the sizes of the study, which "Results and descriptions of the study" sets out, including the size of the codebook. The sentences that overlap with the main results follow their own rules:

- A summary sentence is an excluded claim candidate when it gives at least one number, every one of its numbers describes the study, and it states no quantitative result. Record one entry for each such sentence.
- A sentence recorded as a main result is never also an excluded claim candidate on its own, although one part of it can be an excluded part when the reference's rule splits it.
- Where a main result's sentence also gives a number that describes the study, record that number from the method sentence instead, unless a summary sentence that is already an excluded claim candidate states it.

Qualitative findings, formal claims, and novelty claims are out of scope. Do not record them, not even as excluded claim candidates. The exceptions are:

- a summary sentence that repeats or breaks down a recorded main result, whether or not it gives a quantitative part, because the record has to show every place the paper states that result
- a statement whose basis the paper does not make clear, which the reference's rule for frequency words records as an excluded claim candidate with a note
- a summary sentence that combines a qualitative finding with numbers that describe the study, which is an excluded claim candidate under the rule above

### 6. Write claims.json

Read `references/record-format.md` and write `<output directory>/<paper_id>/claims.json` as that reference describes. It gives the structure, the fields whose format needs explaining, and the rules for quoting from `text.txt`, and it passes over the fields this file has already defined, such as `source` and `serves`.

### 7. Validate

```sh
python3 <plugin root>/scripts/cea_claims.py validate <output directory>/<paper_id>
```

Fix every problem and run it again until it prints `CEA_VALID`. After that line, it can print `warning:` lines, for example about a quote that ends in the middle of a sentence. Read each warning, and fix the record where it is right. If a quote is not found, copy it again from `text.txt`. If it was found on another page, correct `page`. The validator also checks that the `states` of a split part uses only words from its quote. The validator warns when no part of a split statement keeps a negation of the quote. "Splitting" lists the negation words. Keep the negation unless it belongs to a clause that is out of scope, such as a qualitative finding. Most of its other warnings are listed in `references/record-format.md`, under "What the validator warns about". It also checks a `note` that says its values are the parts of a number, naming that number, and adds them up, so that a note gives the parts of its number rather than a total.

Recording a main result as an excluded claim candidate leaves an id that no longer exists in every claim that served it, and in the `duplicate_of` or `breaks_down` of every excluded claim candidate that named it, which is a problem rather than a warning. Judge those claims again under item 3 of step 5, and point those excluded claim candidates at the entry's new id, before running the validator again.

It cannot tell whether a part pairs a number with the wrong item, so reread each part against its quote.

### 8. Render and report

```sh
python3 <plugin root>/scripts/cea_claims.py render <output directory>/<paper_id>
```

The render command writes `claims.md` and `claims.html`. `claims.md` is the working document that the checker reads and it is always written. `claims.html` is the page for readers outside the check, so it is written only when every entry states a decision. Where an excluded claim candidate's `reason` or a claim's `selection_reason` uses the word "unsure" anywhere, quoted or not, the command prints `CEA_UNRESOLVED`, names those entries, writes no page, and exits 1. A main result that no claim serves reaches the page as a finding, under its own heading with the note that says why the paper's own evidence does not reach it. The validator therefore rejects such a statement without a note. Report that to the user rather than working around it. The checker settles each one in `claims.json`, by turning the candidate into a claim or by replacing the reason with the ground that excludes it, and then you render again.

`claims.md` The claims stand under each main result they serve, the result the paper states in the most places first, and within a result in page order, because the paper puts no order on the claims that support one result. Main results that the same claims serve are shown together as one result, which usually means that the paper states one result twice, so check that grouping against the quotes. A list of every claim in page order and the excluded claim candidates come next. Then tell the user, briefly:

- where `claims.json` and `claims.md` are
- how many main results, claims, and excluded claim candidates you recorded
- which main results, if any, no claim serves, or that the paper has none
- any selection you were unsure about, with its id and one sentence saying why, and say so if there were none
- the paper's main results that got no main result, and why each of them is qualitative
- whether the paper's own sentence about not quantifying prevalence removed statements that give a frequency only in words, and where that sentence stands, and say so if the paper has no such sentence
- which entries you compared with a table or a figure, which of those disagree, and which numbers no table reports
- any extraction problems, such as pages with garbled text, or a figure the extraction left as scattered labels

## Left to later steps

Do not interpret the claims here. The next CEA step interprets each claim, as "What claim selection does not decide" describes, and a later step decides whether the evidence supports a claim. An interpretation added now would look like a checked part of the record.
