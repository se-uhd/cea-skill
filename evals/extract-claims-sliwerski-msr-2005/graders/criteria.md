---
type: llm
focus:
  source: file
  path: cea-out/sliwerski-msr-2005/claims.json
---

PASS only if every statement below holds of the record in the file you are shown.
FAIL if any of them does not.

- Every selected claim has a selection reason that names a main result or key contribution and says how it would fail if the claim were false
- At least one excluded claim candidate is recorded, and every excluded claim candidate is a quoted statement with a page and a reason
- No number that only describes the study, such as a sample size, corpus size, codebook size, or agreement score, is recorded as a claim
- No note judges whether a claim is supported; notes state facts that can be confirmed in the paper
- Main results state the pattern by the day of week that fix-inducing changes were applied, the pattern by their size, and that in ECLIPSE fixes are three times as likely to induce a later change as ordinary enhancements
- Claims serve the size result for both projects: the ECLIPSE comparison of Table 3, that fix-inducing transactions are roughly three times larger, and the MOZILLA comparison of Table 4
- Claims serve the day-of-week result for both projects: that the likelihood P (bug) that a change induces a fix is highest on Friday in ECLIPSE (Table 5) and in MOZILLA (Table 6)
- A claim gives the ECLIPSE comparison of P (bug | fix) with P (bug | ¬fix), almost three times higher for fixes, and serves the main result that fixes are three times as likely to induce a later change
- The sentence saying that this comparison does not hold for MOZILLA is an excluded claim candidate, not a claim
- The database sizes (78,954 and 109,658 transactions, 278,010 and 392,972 revisions) and the link counts (25,317 and 53,574) are recorded as excluded claim candidates with reasons, not as claims
- Details are excluded claim candidates, not claims, including the day on which most fixes are made in each project and the day on which a fix is most likely to be undone
- The two page-4 size claims quote whole sentences: the one saying that fix-inducing transactions are roughly three times larger, and the one saying that Table 4 shows a similar trend for MOZILLA
