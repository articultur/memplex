# Paired-diff run report format

`scripts/paired_diff_report.py BASE NEW` compares two official-J runs
(summary.json + hypotheses.jsonl) and reports, per question type: J
before/after, delta, and per-question flip counts — fixed (wrong→right),
regressed (right→wrong), net-new. Exit code is 1 whenever any pool
regresses, so CI can treat masked regressions as failures.

## First application: v13.1 → v13.2

| type | base J | new J | Δ | fixed | regressed | net-new |
|------|-------:|------:|---|------:|----------:|--------:|
| temporal-reasoning | 0.8797 | 0.8872 | +0.0075 | 8 | 5 | +1 |
| other five pools | unchanged | unchanged | 0 | 0 | 0 | 0 |
| TOTAL | 0.914 | 0.916 | +0.002 | 8 | 5 | +1 |

The headline +0.002 hides 13 question-level flips inside the temporal
pool — 8 fixes bought with 5 regressions. That churn is exactly what the
format exists to surface; future run pairs (and the v14 merge line)
should always ship with this table rather than totals alone.

Artifacts: `benchmarks/results/lme-v131-v132-paired-diff/` (gitignored).
