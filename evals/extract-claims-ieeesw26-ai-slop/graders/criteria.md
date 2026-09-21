---
type: llm
focus:
  source: file
  path: cea-out/ieeesw26-ai-slop/claims.json
---

PASS only if every statement below holds of the record in the file you are shown.
FAIL if any of them does not.

- No narrow claim is selected
- Every rejected candidate's reason says why it is not a claim: it names a ground the reference gives, or says that no main result depends on the count
- At least one rejected candidate is recorded, and every rejected candidate is a quoted statement with a page and a reason
- No number that only describes the study, such as a sample size, corpus size, codebook size, or agreement score, is recorded as a claim
- No purely qualitative finding or summary is recorded as a claim, a broad statement, or a rejected candidate, except a summary sentence in which the numbers describe the study, a statement whose basis the paper leaves unclear, which is a rejected candidate with a note, or the paper's own sentence declining to quantify prevalence, which is a rejected candidate recording the ground on which statements giving a frequency only in words were left out
- No note judges whether a claim is supported; notes state facts that can be confirmed in the paper
- The corpus and codebook counts (15 documents, 1,154 posts, 15 codes) are recorded as rejected candidates with reasons
- The coding counts on page 3 are recorded as rejected candidates with reasons, not as claims and not left out, including both the 84.7% of posts coded and the three most frequent codes at 44.2% of codings
- Statements that give a frequency only in words, such as 'a dominant theme' or 'widely discussed', are not recorded at all, not even as rejected candidates (this includes word-only rankings such as 'pairs most often with')
- The thematic clusters (Review Friction, Quality Degradation, Forces and Consequences) are not recorded as a claim or a broad statement, and the abstract sentence that names them appears only as a rejected candidate whose reason is that its numbers describe the study
- A rejected candidate quotes the paper's own sentence declining to quantify prevalence, with a reason naming it as the ground on which the statements giving a frequency only in words were left out
