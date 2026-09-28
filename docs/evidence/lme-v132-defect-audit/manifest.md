# v13.2 (J=0.916) failure re-audit — corrected ceiling 0.956

The 0.944 ceiling was calibrated on the v12 run (28 defects / 51
failures); this re-audits the current best run's 42 failures with the
same Stanford taxonomy (D1 ambiguous / D2 incorrect answer key /
D3 grading issue / S genuine system miss).

## Protocol

- Input: v132's 42 failures (`autoeval_label is False`),
  classifier glm-5.3 (thinking disabled, temperature 0), the official
  judge inputs per question (question, gold, hypothesis, retrieved
  context).
- v132's hypotheses lack `retrieved_context`; it was joined from the
  v131 records for the same question ids (same haystack, same pipeline
  family, 500/500 joined).
- Incident, documented for honesty: the first run burned against an
  exhausted bigmodel quota (1310 rate-limit) and produced 42 empty
  classifications — that output was discarded, not published; the run
  here completed after the quota reset.

## Result

| class | n | share of failures |
|-------|--:|------------------:|
| D1 ambiguous question | 5 | 12% |
| D2 incorrect answer key | 15 | 36% |
| D3 grading issue | 2 | 5% |
| S genuine system miss | 20 | 48% |

- Defects: 22/42 → **corrected ceiling ≈ 0.956** for a
  perfect-defect-free system (vs 0.944 on v12).
- True headroom: 20 questions (0.916 → 0.956).
- Per type: multi-session 9 S + 7 D2 + 2 D1 (the largest S pool, as in
  every prior audit); temporal 8 S + 5 D2 + 2 D1; knowledge-update
  1 S + 2 D2 + 1 D1; assistant 1 S + 1 D2; preference 1 S + 1 D3;
  user 1 D3.

## Caveats

- Context came from v131 records (v132 omits the field) — same
  evidence sources, different run metadata.
- The classifier is the same model family as the scoring judge; the
  v12 calibration used the same setup, so the two ceilings are
  comparable to each other.
- D2 remains the dominant defect class (15/42), consistent with the
  original audit's D2-heavy profile.

Artifacts: `benchmarks/results/lme-v132-defect-audit/` (gitignored).
