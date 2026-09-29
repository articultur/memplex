# LAFS: official-formula result for Memplex's V2 operating points

Vendors the official LAFS algorithm (xiaowu0162/LongMemEval-V2,
`leaderboard/compute_lafs.py`, Apache-2.0) in `scripts/lafs_v2.py` and
scores our operating point against the released reference frontier.

## Formula (official)

LAFS = average of best-accuracy-under-latency-budget(T) over log-uniform
T ∈ [1s, 200s]; a submission's gain = LAFS(reference ∪ submission) −
LAFS(reference). Reference frontier (small tier, paper's main table):
RAG 51.0@0.2s, AgentRunbook-R 58.6@26.9s, AgentRunbook-C 74.9@108.3s,
Codex 69.9@177.2s (dominated off the Pareto set).

## Result (small tier, enterprise point)

| quantity | value |
|---|---:|
| reference LAFS | 55.7648 |
| submission point | Memplex text-projection, 18.96 pp @ p50 3.331s |
| submission LAFS | 55.7648 |
| **LAFS gain** | **0.0000 (exactly)** |

## Reading

- The gain is exactly zero, not approximately: the RAG point (51.0 pp at
  0.2s) sets the under-budget floor at every budget in [1s, 200s], so
  any submission below 51.0 pp accuracy is dominated regardless of
  latency. Our 18.96 pp cannot contribute to the frontier at any speed.
- What it would take to gain > 0 on the small tier: accuracy above
  51.0 pp (any latency), above 58.6 pp below 26.9s, or above 74.9 pp
  below 108.3s. The dynamic-environment (0/35) and vision strata are the
  quantified structural gaps between our text-projection stack and that
  band.
- Latency is not the binding constraint: our per-question p50 (3.3s)
  already sits in the fast band; the frontier gap is purely accuracy.
- The medium-tier reference LAFS becomes computable the same way once
  the medium runs land (reference: RAG 45.9@0.3s / AgentRunbook-R
  57.0@25.8s / AgentRunbook-C 70.1@139.9s / Codex 68.7@185.8s).

Artifacts: `benchmarks/results/lme2-lafs/summary.json` (gitignored);
`scripts/lafs_v2.py` committed.
