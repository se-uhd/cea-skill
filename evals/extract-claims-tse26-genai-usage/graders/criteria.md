---
type: llm
focus:
  source: file
  path: cea-out/tse26-genai-usage/claims.json
---

PASS only if every statement below holds of the record in the file you are shown.
FAIL if any of them does not.

- Every selected claim has a selection reason that names a main result or key contribution and says how it would fail if the claim were false
- At least one rejected candidate is recorded, and every rejected candidate is a quoted statement with a page and a reason
- No number that only describes the study, such as a sample size, corpus size, codebook size, or agreement score, is recorded as a claim
- No purely qualitative finding or summary is recorded as a claim, a broad statement, or a rejected candidate, except a summary sentence in which the numbers describe the study, a statement whose basis the paper leaves unclear, which is a rejected candidate with a note
- No note judges whether a claim is supported; notes state facts that can be confirmed in the paper
- The dataset size of 1,292 GenAI mentions and the inter-rater agreement scores are recorded as rejected candidates with reasons
- Broad statements state that code churn does not generally increase after GenAI adoption and that effects are stronger for generation tasks
- Separate claims serve the two churn results: no general increase in churn, and stronger effects for generation tasks
- The counts behind the RQ1 rankings, such as the 105 code generation instances or the 176 source-file mentions, are recorded as claims
- A note records at least one disagreement between the text and Table 9 that needs no arithmetic, for example the 10 repositories with a downward trend and a positive slope on page 12 against 3 in Table 9
- Details are rejected candidates, not claims, including the most common regression discontinuity (RDD) pattern for each churn type (12 repositories each) and the 3/26 repositories in the discussion
