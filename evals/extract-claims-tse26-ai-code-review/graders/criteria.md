---
type: llm
focus:
  source: file
  path: cea-out/tse26-ai-code-review/claims.json
---

PASS only if every statement below holds of the record in the file you are shown.
FAIL if any of them does not.

- Every selected claim has a selection reason that names a main result or key contribution and says how it would fail if the claim were false
- At least one rejected candidate is recorded, and every rejected candidate is a quoted statement with a page and a reason
- No number that only describes the study, such as a sample size, corpus size, codebook size, or agreement score, is recorded as a claim
- No purely qualitative finding or summary is recorded as a claim, a broad statement, or a rejected candidate, except a summary sentence in which the numbers describe the study, a statement whose basis the paper leaves unclear, which is a rejected candidate with a note
- No note judges whether a claim is supported; notes state facts that can be confirmed in the paper
- The abstract sentence that gives 16 actions, more than 22,000 comments and 178 repositories is recorded as a rejected candidate with a reason
- A claim reports the accuracy of the LLM-assisted framework against human annotations
- Claims report the factor results for review granularity (file-level vs. hunk-level), for trigger mode, and for comment content (share of code or length)
- The accuracy of the Random Forest model used to explain the factors is recorded as a rejected candidate, not as a claim
- The RQ1 result that 82.6% of repositories customized a parameter is recorded as a claim whose `serves` list includes a broad statement stating that result
- The trigger-mode claim quotes the page-10 sentence giving the whole-sample correlation for automatic triggering (Trigger auto), and its note names Table XIII; the page-10 sentence about the two actions with manually triggered comments and the single-action example on page 11 are rejected candidates whose reasons name that claim
- Details are rejected candidates, not claims, including that coderabbitai/ai-pr-reviewer achieved the highest valid and addressed rate
