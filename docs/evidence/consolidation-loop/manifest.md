# F3: rule-based offline consolidation loop — implementation and probe

**Direction** (Q4 deep-research F3): TSM reports up to +12.2% absolute by
consolidating temporally-adjacent, semantically-related point memories
into sustained memories with algorithmic forgetting (Auto-Dreamer trains
the same loop with GRPO — that component is closed for this project).
Memplex mapping: episodic layer = raw paragraphs, sustained layer = typed
nodes; the loop is the promotion/forgetting bridge between them.

## What shipped

`memplex/consolidation.py` — one offline pass `consolidate(store)`, wired
as a fail-soft phase of the sleep-time maintenance loop
(`sleep_time.run_once()` → `report["consolidated"]`). Disabled by default
(`MEMPLEX_CONSOLIDATION=1`); thresholds via
`MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS` (3),
`MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS` (1),
`MEMPLEX_CONSOLIDATION_PARAGRAPH_TTL_DAYS` (90).

- **Promotion**: near-duplicate paragraph clusters (content-word Jaccard
  >= 0.4 + >= 3 shared tokens, stopword-filtered, punctuation/version
  tolerant) with >= N observations spanning >= S days graduate to a typed
  Fact (subject "user", predicate "stated"). Trust tier takes the cluster
  min (merge-takes-min); namespace carries the consolidation stamp and
  observation count; the node id is content-derived, so re-promotion is
  harmless. Cluster rows get a `consolidated_into` stamp in the same
  atomic commit, making the pass idempotent and the rows TTL-proof.
- **Forgetting**: paragraphs older than the TTL that never consolidated
  are evicted (algorithmic forgetting of one-off noise; the paragraph
  layer historically never pruned — this closes that gap).
- **Contract tests** (`tests/test_consolidation.py`, 6): default-off
  no-op, promotion + idempotency, min-tier merge, TTL eviction with
  sustained-row protection, observation/span gates, no-paragraph-layer
  degradation.
- The query path is untouched by construction — the 0.7669 primary-fusion
  numbers are unaffected.

## Probe (real write path, `scripts/consolidation_probe.py`)

3 rephrases of one statement written across 3 simulated days + 1 aged
one-off noise row:

- default-off: no-op (reported, nothing mutated)
- pass enabled: 1 node promoted (`facts 2→3`), promoted content retrievable
  in the orchestrated top-8, noise row evicted
- second-pass idempotency covered by contract tests

## v2 (this batch): semantic clustering + typed classification

- **Embedder clustering**: `consolidate(..., embedder=...)` upgrades the
  cluster gate from lexical Jaccard to cosine >= 0.85 against running
  cluster means (`MEMPLEX_CONSOLIDATION_EMBED_THRESHOLD`), grouping
  rephrases that share no surface form; embedder failure degrades to the
  lexical fallback. Contract test includes the negative control: rows
  that lexical clustering cannot group promote only with the embedder.
- **Preference classification**: clusters matching a first-person
  preference pattern ("I prefer/like/love/need…") graduate as Preference
  nodes instead of "stated" Facts — zero extra cost, keeps the honest
  fallback for everything else.
- Still open from v1: verbatim-repeat counting needs a write-path
  observation counter (content-addressed dedup collapses exact repeats to
  one row at write time).

## v3 addendum (2026-09-30): cadence synthesis — at probe ceiling

Promoted nodes append a synthesized cadence to their text facet —
`" (observed N times across D days)"` from the observation counter and
span (sub-day spans stay verbatim; node id stays keyed on the raw
canonical so re-promotion is byte-identical). Two follow-up fixes took
the habit-probe implicit arm from 0.0 to the full 1.0:

- **Medoid canonical** (0.5 → the remaining gap closed): frequency ties
  in a semantically-formed cluster admitted peripheral texts — a pool
  filler ("My plant watering day is Wednesday.") won the aloe cluster's
  canonical by earliest-index, so the promoted node named Wednesday, not
  aloe. The canonical is now the cluster medoid (highest mean cosine),
  which is the member the cluster is actually about.
- **Evidence demotion**: paragraphs whose pattern was promoted demote
  x0.5 in the paragraph search leg (never filtered) — the sustained
  node can outrank its own evidence for aggregate questions.

Final: `cadence_phrase_recall` **1.0** (both implicit habits promote
with a subject-bearing, phrase-carrying retrievable node); the
day-token metric stays 0.0 by construction (the mentions never name
the day). 6 new contract tests; full lite suite green.

## Honest scope

- This is a **local mechanism demonstration**, not a reproduction of
  TSM's +12.2% — that number belongs to their benchmark and architecture.
- Verbatim repeats collapse to one paragraph row at write time
  (content-addressed dedup), so the promotion signal is *rephrased*
  repetition; counting exact repeats needs a write-path observation
  counter — deliberate, keeps this pass entirely offline.
- Clustering is lexical (no stemming): word-form drift ("wednesday" vs
  "wednesdays") can split a cluster. A v2 can cluster with the existing
  embedder without leaving rule territory.
- Eviction rides the normal commit; a concurrent peer write that triggers
  the stale-base fold can delay the reclaim by one cycle.
- Promotion graduates everything as a "stated" Fact rather than guessing
  fact vs preference.

Artifacts: `benchmarks/results/consolidation-probe/` (gitignored, as
usual for benchmark outputs).
