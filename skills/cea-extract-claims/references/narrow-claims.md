# Narrow claims

In Claim-Evidence Alignment (CEA), the claim selection step records each claim with its exact wording, its location, and the reason for selecting it. CEA calls this record mapping level M1. Later steps reconstruct, for each claim, the chain that connects the claim to its evidence in the paper and the research artifact. An agent drafts the record, and a person, the checker, reviews it.

## Scope

CEA covers narrow quantitative empirical claims. Its sources are the published paper and the published research artifact, and this step reads only the paper. CEA excludes:

- qualitative claims, such as themes or codes derived from interviews or discourse
- formal claims, such as proofs or properties of an algorithm
- novelty claims, such as "the first study of X"

A statement is quantitative if counting or measuring the study's own data could show it to be false. That includes an amount, a share, a ranking, a difference, or a trend, with or without a number. Counts and rankings that come from qualitative coding are quantitative in this sense, for example "code generation was the most common task (105 instances)". The checker can recount them in the coded data, and the coding procedure belongs to their chain.

## Narrow claims, broad statements, and rejected candidates

A narrow claim is a specific statement of a quantitative result in the paper. Statements in the abstract or the contribution list are usually broader. CEA does not reconstruct chains for these broad statements directly, but uses them to find the narrow claims that they depend on. Record a broad statement only if at least one part of it states a quantitative result. A purely qualitative summary, such as a taxonomy described in the abstract, is left out. A summary whose only numbers describe the study, such as the size of the corpus, is not a broad statement either. Record it as a rejected candidate. A contribution that only announces an analysis, such as "we analyze how the tools are configured", and a recommendation in the discussion report no finding and are left out.

A claim is a statement in the paper's text, which includes figure and table captions and footnotes. A number that appears only inside a table or a figure is evidence for a claim, not a claim. A boxed answer to a research question is decided sentence by sentence:

- A sentence without a result number is a broad statement if it states a quantitative result in words, as described under "Frequency words without a number". Otherwise leave it out. A number that only describes the data, such as the number of mentions analyzed, is not a result number.
- A sentence with a result number is backed by the most specific sentence that states that number. If a sentence in the results states it more specifically, that sentence is the claim candidate. The box sentence is then a broad statement if no other broad statement covers the result, and a repetition otherwise.
- If no sentence states the number more specifically, record the box sentence both as a broad statement and as a claim with the same quote that serves it.
- When a box sentence needs the sentence before it as context, such as "However, most of them had a negative slope.", record the two sentences together as one broad statement.

A rejected candidate is a statement that a checker could expect to be a claim but that was not selected. Its record says why.

## Frequency words without a number

Words such as "dominant", "most", "often", "widely", and "growing" do not decide by themselves whether a statement is quantitative. What the paper bases the word on decides:

1. A statement that reports cited or background work is not a claim of this paper. Leave it out, except as described under "Results and descriptions of the study".
2. If the paper bases the word on counts or measurements, such as a number nearby, a table or figure of counts, or a statistical test, the statement is quantitative. If the paper bases the word on the authors' reading of quotes or examples, or says that it does not quantify prevalence, the statement is qualitative. Leave it out. When the paper says that it does not quantify prevalence, that statement decides, even if a table or figure gives counts for related codes. The disclaimer rule covers only words without a number. A sentence that gives a count from the coding stays a quantitative candidate in such a paper, and a word-only ranking, such as "pairs most often with", is left out. If you cannot tell, record the statement as a rejected candidate and say in `note` what is unclear.
3. A quantitative statement without a number in the abstract, the contribution list, a boxed answer to a research question, or the conclusion is a broad statement. The sentences that give its numbers are the claims that serve it. If no sentence does, that part of the broad statement has no claim, and `note` says so.
4. For a quantitative statement without a number in the results or the discussion, look for a more specific sentence that states the same result with its number. If there is one, that sentence is the claim candidate, and the vague sentence is a repetition. If there is none, the vague sentence is the claim candidate, and `note` says whether the number is in a table or a figure or missing.

## Selection question

Select a narrow claim if the answer to this question is yes:

> If this statement were false or unsupported, would a main result or key contribution fail or need substantial revision?

Record why each claim was selected. A reason that the checker can assess names the main result or contribution that depends on the claim and says how it depends on it.

## Splitting

Split a statement into separate claims when its parts need different evidence. For example, "X is faster and uses less memory" becomes two claims.

## Results and descriptions of the study

Numbers that describe how the study was done are rejected candidates, not claims, even though they are quantitative. Examples are sample sizes, corpus sizes, the number of codes in a codebook, and inter-rater agreement. Whether a number is a result depends on what the paper claims. An agreement score that only supports the coding procedure describes the study, but the accuracy of a classifier that the paper presents as a contribution is a result.

Record each sentence in the abstract, the contribution list, a boxed answer, or the conclusion whose only numbers describe the study as a rejected candidate, with one entry per sentence that covers all of its numbers. The same numbers in the method section need not be listed again.

The accuracy of a model that the paper uses to reach its findings is not a claim either. An example is a random forest whose feature importances explain what influences an outcome. Its accuracy belongs to the analysis step in the chain behind those findings, which a later step reconstructs. Record it as a rejected candidate. A model that the paper presents as a contribution is different. Its accuracy is a result, even when the paper also uses the model to produce its labels.

A number that a participant states, such as "30 PRs per day" in a quoted post, and a result of cited work are not results of this study. Record them as rejected candidates only where a checker could take them for results of this study, as in a comparison with prior work in the discussion. Otherwise leave them out.

## Not part of this step

The next CEA step interprets each claim: the constructs and their relation, the unit of analysis, the scope, and the claim kind, which is descriptive, associational, predictive, or causal. Later steps reconstruct the chain to the evidence and assess whether the evidence supports the claim. None of these interpretations and assessments belongs in the claim record.
