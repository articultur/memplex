# Sparse leg + FTS5 field weights — shipped opt-in, honest negative on paraphrase

The merged roadmap adopted "BGE-M3 sparse leg (model already present,
nearly free) + FTS5 field weights" as the cheapest new retrieval levers.
Both are now implemented, verified active at the unit level, and
measured on the paraphrase-robustness benchmark: **neither moves recall
when the bge-m3 dense leg is in the stack**. Shipped as opt-in knobs
(defaults byte-identical to legacy).

## What shipped

- **FTS5 field weights** (`MEMPLEX_FTS_FIELD_WEIGHTS`, default "1,1,1"):
  bm25() column weights for (name, domain, body) in the FTS sidecar,
  bound parameters, parse/arity failure fails closed to the default.
  The three SQL statements in `_score_match_query` are now fully static
  literals (previously an f-string with a constant table name).
- **Sparse lexical leg** (`MEMPLEX_SPARSE_LEG=1`): `EmbeddingService.
  embed_sparse_batch` exposes bge-m3 lexical weights via FlagEmbedding
  (optional import, lazy second model instance, absent/failure = leg
  off); `VectorSearchIndex.sparse_search` caches corpus weights per
  doc (SHA-1 digest) and scores queries by shared-token weight sums
  normalized per query; the store fuses it as a third leg. Document
  projection shared with the dense leg (`_semantic_documents`), result
  construction shared (`_results_from_ranked`).
- Tests: `tests/test_sparse_leg.py` (5) — ranking/normalization,
  corpus caching, fail-closed without the API, store-level fusion
  behind the flag, weights parsing.

## Probe: paraphrase-robustness, three configs (2026-09-29, CPU bge-m3)

| config | overall r@1 | low-overlap r@1 | low r@5 |
|--------|------------:|----------------:|--------:|
| baseline (FTS5/BM25 + dense) | 0.860 | 0.676 | 0.946 |
| + FTS5 weights 8,2,1 | 0.860 | 0.676 | 0.946 |
| + sparse leg | 0.860 | 0.676 | 0.946 |

Both knobs verified ACTIVE, not silently no-op'ed: the FTS weights flip
a name-hit/body-hit ordering at the unit level (body wins unweighted,
name wins at weight 50); the sparse backend returns real lexical
weights and the leg surfaces the right document through the service
path. The retriever field in all three reports confirms the bge-m3
hybrid stack.

## Honest reading

- With the dense leg present, the remaining low-overlap recall@1 gap
  (0.676) is a dense-ranking/fusion gap, not a lexical-weighting gap;
  neither cheap lever touches it. The 2026-09-04 zero-overlap catastrophe
  (0.03 r@1 on TF-IDF) was already closed by bge-m3 itself.
- The knobs stay available: FTS weights for name-heavy corpora (entity
  lookups), the sparse leg for morphology/multilingual cases (BM25's
  unicode61 tokenizer cannot stem; bge-m3 lexical weights can) — this
  benchmark's English single-token strata just do not exercise them.
  A Chinese-paraphrase probe would be the discriminating follow-up.
- FlagEmbedding is an env-only dependency for the probe (installed via
  `uv pip install`, NOT in pyproject/lock; `uv sync` removes it). The
  leg fails closed without it, so the shipped default needs nothing.

Artifacts: `benchmarks/results/paraphrase-{baseline,fts-weights,sparse-leg}-2026-09-29.json`
(gitignored).
