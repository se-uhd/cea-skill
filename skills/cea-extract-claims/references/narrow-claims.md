# Narrow claims

In Claim-Evidence Alignment (CEA), the claim selection step records each claim with its exact wording, its location, and the reason for selecting it. Later steps reconstruct, for each claim, the chain that connects the claim to its evidence in the paper and the research artifact. An agent drafts the record, and a person, the checker, reviews it.

## Scope

CEA covers narrow quantitative empirical claims. Its sources are the published paper and the published research artifact, and this step reads only the paper. CEA excludes:

- qualitative claims, such as themes or codes derived from interviews or discourse
- formal claims, such as proofs or properties of an algorithm
- novelty claims, such as "the first study of X"

A statement is quantitative if the paper bases it on counting or measuring the study's own data, so that it gives an amount, a share, a ranking, a difference, or a trend. Counts and rankings that come from qualitative coding are quantitative in this sense, for example "code generation was the most common task (105 instances)". The checker can recount them in the coded data, and the coding procedure belongs to their chain. Where every main result of the paper is qualitative, no main result depends on such a count, so it is a rejected candidate on that ground and its reason says so. For a statement without a number, the section "Frequency words without a number" decides.

## Main results

A main result or key contribution is a finding that the paper's results, discussion, and conclusions put forward as what the study shows. List the main results before judging any candidate. They are of four kinds:

1. a finding that a boxed answer gives to its research question
2. a finding that the abstract, the contribution list, or the conclusion states
3. a finding presented in the results or the discussion as a main, key, central, principal, or most important finding or contribution, when no sentence of the kinds above states it
4. a tool, model, or framework that the paper presents as a contribution and evaluates

A boxed answer is a passage that the paper labels as the answer, summary, or finding for a research question, such as "Answer to RQ1:" or "Summary RQ2:", whether or not it is printed in a box. A list of findings in the introduction counts as the contribution list.

The same finding in several places is one main result. The following are not main results, even when a summary states them or they give a number:

- a breakdown, a subgroup, an exception, an example, or the pattern of a subset of a main result
- a recommendation, an implication, or a design lesson. The finding that it rests on is a candidate, and the selection question for that candidate names one of the other main results.
- a sentence in the results or the discussion that only repeats a candidate's own number or ranking. The rules for boxed answers under "Broad statements" decide about a sentence in a boxed answer.
- a statement that the paper mentions once and does not build on
- a finding that the discussion only highlights, calls notable, or bases a recommendation on

## Broad statements

A summary sentence is a sentence in the abstract, the contribution list, a boxed answer, or the conclusion.

Record each main result once as a broad statement, quoting the sentence that states it. A sentence that states more than one main result stays one broad statement, and its `note` names each main result that it states. When several sentences state the same main result, quote the sentence that comes first in this order: the boxed answer, the abstract, the contribution list, the conclusion. Record each other sentence that repeats it as a rejected candidate, with `duplicate_of` naming the broad statement. A sentence that repeats one main result and also states a main result that no earlier source states is the broad statement for the main result that no earlier source states, and its `note` names the broad statement that already states the repeated main result.

Use a sentence from the results or the discussion only for a main result of kind 3 in the list above, with `source` `other` and a `note` that says why no summary sentence states it. A broad statement needs a quantitative part, in numbers or in words, unless it presents a tool, model, or framework and the paper evaluates how well it performs.

Other sentences in the abstract, the contribution list, a boxed answer, or the conclusion are not broad statements:

- A purely qualitative summary, such as a taxonomy described in the abstract, is left out.
- A summary sentence that gives at least one number, where every number describes the study, such as the size of the corpus, is a rejected candidate, with one entry per sentence that covers all of its numbers. The rule also holds when the sentence combines such numbers with a qualitative finding. A summary sentence without numbers is left out. A sentence that also states a quantitative main result, in numbers or in words, is a broad statement instead, and its numbers that describe the study are recorded from the method sentence that states them, unless a summary sentence that is already a rejected candidate states them.
- A contribution that only announces an analysis, such as "we analyze how the tools are configured", is left out.

Judge a boxed answer sentence by sentence:

- Each sentence that gives a finding in answer to the research question is one broad statement with `source` `rq_answer`. The source is `rq_answer` wherever the box stands, including inside a results section, because the source says which kind of sentence states the main result, not which section prints it. A box can give several findings, such as one about tasks and one about file types, and a single sentence of the box can state more than one. A sentence that adds a breakdown, a subgroup, or an exception of a finding in the same box is not a broad statement.
- A number in the box is backed by the most specific sentence that states it. If a sentence in the results or the discussion states it more specifically, that sentence is the claim candidate.
- When a box sentence needs the sentence before it as context, for example because it starts with "However" or refers to "them", quote the two sentences together in whatever record the second sentence gets. If the sentence before it states a finding of its own, that earlier sentence keeps its own record as well. If the second sentence gets no record of its own, leave the pair out and keep the record of the sentence before it.

A broad statement's own sentence can be the only sentence in the paper's text that gives the number of its main result, for example when the results give that number only in a table. Record such a sentence both as the broad statement and as a claim with the same quote that serves it, whatever its source. Where a sentence in the results or the discussion states the number more specifically, that sentence is the claim instead, as the rule for a number in a box says above.

A paper can have no broad statements, when every main result it states is qualitative. Record the rejected candidates, record no claims, and say so in the report.

Broad statements help to find claims. Supporting a broad statement does not make a statement a claim, because the selection question decides. A broad statement that no claim serves needs a `note` that says why, for example that the paper gives no result for it.

## Narrow claims and rejected candidates

A narrow claim is a specific statement of a quantitative result in the paper's text, which includes figure and table captions and footnotes. A number that appears only inside a table or a figure is evidence for a claim, not a claim.

An item is one of the things that a main result names and counts or measures on its own, such as a task category, a file type, a tool, or a group of tools.

A result is often stated several times. A sentence states it more specifically than another only if it covers the same items, measures, and subgroups and gives the numbers. When a main result names several items, such as three kinds of task, and the results give each item's number in its own sentence, those sentences together are its most specific wording, and each of those sentences is a claim candidate. A sentence that gives the number for one of the items that the main result names is a claim candidate, even where the numbers of the other items stand in a table or in no sentence at all.

A detail is a sentence that gives numbers for a subset of an item, for a single example, or for a pattern, subgroup, or category that the main result does not name. Ask the selection question for a detail as for any other candidate. A detail almost always fails it.

A rejected candidate is a statement that a checker could expect to be a claim but that was not selected. Its record says why. If you are unsure whether a candidate passes the selection question, record it as a rejected candidate, say in its `reason` that you are unsure, and name in `note` the main result that might depend on it and the passages that support each choice, so that the checker can change it into a claim.

## Frequency words without a number

Words such as "dominant", "most", "often", "widely", and "growing" do not decide by themselves whether a statement is quantitative. What the paper bases the word on decides:

1. A statement that reports cited or background work is not a claim of this paper. Leave it out, except as described under "Results and descriptions of the study".
2. If the paper bases the word on counts or measurements, such as a number nearby, a table or figure of counts, or a statistical test, the statement is quantitative. If the paper bases the word on the authors' reading of quotes or examples, the statement is qualitative. Leave it out. If you cannot tell, record a rejected candidate and say in `note` what is unclear.
3. If the paper says that it does not quantify prevalence, for example that its approach captures the range of views but does not measure how common each view is, leave out every statement that gives a frequency only in words. The rule holds even when the paper counts codes elsewhere, including the code that the word describes. The rule also holds when the same sentence adds that the frequencies do not generalize, and when the word rests on an analysis such as co-occurrence. The "cannot tell" case of rule 2 does not apply. A sentence that only says that the frequencies do not generalize is a limit of scope and does not trigger this rule. A sentence that gives a count from the coding stays a quantitative candidate, even when it also states a rank in words. Only a sentence without a count of its own is left out.
4. A quantitative statement without a number in the abstract, the contribution list, a boxed answer, or the conclusion is a broad statement if it states a main result. Otherwise leave it out. Its claim candidates are the sentences in the results or the discussion that state the same result more specifically, which are the sentences that give the counts or measurements that the frequency word rests on. If no sentence in the results or the discussion gives any of them, the vague sentence in the results or the discussion is the candidate, as in rule 5. Where the results and the discussion state the result nowhere, the broad statement has no claim, and its `note` says so. If there is no candidate, the broad statement's `note` says so.
5. For a quantitative statement without a number in the results or the discussion, look for a sentence that states the same result more specifically, in the sense of "Narrow claims and rejected candidates": It covers the same items, measures, and subgroups and gives the numbers. Each sentence that gives the number for one of the items that the vague sentence names is a claim candidate, and the vague sentence is a repetition, unless every such sentence fails the selection question, which the last case of this rule covers. Where no sentence gives a number for any of those items, the vague sentence stays the claim candidate, and `note` then says whether the numbers are in a table, in a figure, or printed nowhere. Where one item's number stands only in a table, the sentences that do give numbers are the claims, and `note` names the table for the remaining item. Where every sentence that gives a number fails the selection question, for example because it gives the number for a single example or a single tool, the vague sentence stays the claim candidate, and `note` names the table, the figure, or the sentence where each number stands.

## Selection question

Select a narrow claim if the answer to this question is yes:

> If this statement were false or unsupported, would a main result or key contribution fail or need substantial revision?

"False" means that what the candidate asserts does not hold in the way that the main result uses it, because its direction, ordering, comparison, or threshold reverses or disappears. A somewhat different number does not make a candidate false. A count of none makes a candidate false only when the main result says that something occurs or recurs, such as "developers repeatedly revert generated code". A reason stating that a different number would change a count, a breakdown, or a sentence that repeats the number does not show that a main result fails.

Name the main result and say how it would fail or need substantial revision. Imagine only this statement false, and leave the paper's other results standing. If the main result would still stand, the candidate is a rejected candidate, and its reason names that main result by the id of its broad statement. A candidate that the question never reaches, because it is out of scope, names the ground that excludes it instead:

- its numbers describe the study
- it repeats another entry
- it reports cited work, or a number that a participant states
- it is the accuracy of a model that the paper only reads other findings from
- no broad statement states the main result that it would serve
- another ground that this reference gives

A candidate that the question does reach names a broad statement id, whichever way it is answered. Where the record has no broad statement, the paper's main results are qualitative and no id exists to name. Every candidate then meets the ground that no broad statement states its main result, so that ground says nothing: name the ground that excludes the candidate on its own terms, and say that no main result of the paper depends on it only where no other ground fits. Details usually fail this test. Examples are a rank below the ranks that the main result names, a count for a pattern that the main result does not name, the rate of a single tool when the main result compares groups of tools, and a remark that a robustness check changed nothing.

When the paper bases a main result on several results together, select each result that the summary, the discussion, or the conclusion names as a basis, unless it is the count for one pattern, subgroup, or category of another result that this record selects as well. Where two results stand in that relation, select the result that covers the whole sample and record the other as a rejected candidate. Do not reject a result because another result would carry the main result alone. Taken on its own, each of these results would fail the selection question, since the main result stands without any one of them. This rule overrides that outcome. A main result about variation, such as "results vary widely across projects", rests on the result that shows the variation as a whole, not on the count for each pattern.

Selecting details hides the claims that matter among them and costs the checker a chain for each detail.

## Splitting

Split a statement when its parts need different evidence, meaning a different measure, dataset, subgroup, or analysis, so that one part could be false while the other holds. For example, "X is faster and uses less memory" has two parts. Do not split the two sides of one comparison, difference, or ranking, because the comparison is one part. A sentence that gives counts for several categories is split when the main result names only some of them: the counts for the named categories are one part, and the counts for the unnamed categories are a rejected part. Splitting along named and unnamed categories is not splitting a comparison. A part that states a negated finding keeps the negation with the word that it negates. The negation words are not (except in "not only"), no, never, neither, nor, none, without, cannot, and a word ending in "n't". A clause that is out of scope, such as a qualitative finding, is not a part and is not recorded, so its negation is not kept. `cea_claims.py validate`, the script that checks the record against the paper's text, warns when no part keeps a negation of the quote, and when a negation is separated from its word. Ask the selection question for each part. A part that passes is a claim, and a part that fails is a rejected candidate with the same quote, its own `text`, and the shared `split_from`.

## Results and descriptions of the study

Numbers that describe how the study was done are rejected candidates, not claims, even though they are quantitative. Examples are sample sizes, corpus sizes, the number of codes in a codebook, and inter-rater agreement. An agreement score that only supports the coding procedure describes the study. A number that says how much material the study retrieved, sampled, annotated, or coded describes the study wherever the paper repeats it, including in a boxed answer. Such a number is a result only where the paper compares it across groups or over time, or relates it to something else. Every summary sentence that is a rejected candidate under "Broad statements" gets its own entry, so the same size can appear in more than one of them. Record a size of the study, such as the number of documents, posts, participants, repositories, or codes analyzed, once more where no such sentence states it, quoting the sentence outside the summaries that does, usually in the method section. Where that sentence also gives a size that is already recorded, record it anyway, because one entry covers all the numbers of its sentence.

A model's accuracy is a rejected candidate when the paper only reads other findings from the model, such as feature importances or labels. It is a claim candidate when the paper presents the model as a contribution, or when a research question asks how well the outcome can be predicted. Where one model has several accuracy figures, select the figures that the main result's wording rests on, not figures per class, per subset, or for models that the paper compared only incidentally. Where the setup that the study used still leaves several, select the figure for the outcome that the later analysis rests on, and record the other figures as rejected candidates whose reason names the broad statement that still stands and the selected figure, as in "B2 would still stand; C3 is the figure the later analysis rests on".

A number that a participant states, such as "30 PRs per day" in a quoted post, and a result of cited work are not results of this study. Record them as rejected candidates only where a checker could take them for results of this study, as in a comparison with prior work in the discussion. Otherwise leave them out.

## Not part of this step

The next CEA step interprets each claim: the constructs and their relation, the unit of analysis, the scope, and the claim kind, which is descriptive, associational, predictive, or causal. Later steps reconstruct the chain to the evidence and assess whether the evidence supports the claim. None of these interpretations and assessments belongs in the claim record.
