# Benchmarks

Memplex ships an evaluation harness (`benchmarks/`, **source checkout only** —
it is not part of the installed distribution) that measures retrieval and
memory-keeping quality across six datasets.

> **Read this first:** the numbers on this page are an **offline synthetic
> baseline**. Every dataset was run with `--synthetic`, which generates a
> handful (3–5) of hand-written samples per dataset — or, for
> `memory_benchmark`, 59 programmatically generated memories. They verify
> that the pipeline works end-to-end and give a reproducible smoke-level
> reference. They are **not** real-distribution performance and must not be
> quoted as such. See [Synthetic vs. real data](#synthetic-vs-real-data).

## Environment

| Dimension | Value |
|-----------|-------|
| Date | 2026-08-09 |
| Memplex version | 3.3.0 (source checkout) |
| Python | 3.13.12 |
| Machine | Apple M4 (arm64), macOS |
| Storage backend | `lite` (SQLite FTS5) |
| Embeddings | default local TF-IDF (`embedding.model = "default"`, dim 384) |
| LLM features | off (no API keys; semantic extraction / query enhancement / compression disabled) |
| Mode | warm (memories seeded before evaluation) |
| top-K | 10 (CLI default) and 5 |

## Methodology

Each run is:

```bash
MEMPLEX_STORAGE_BACKEND=lite memplex benchmark run \
    --dataset all --synthetic --top-k 10 \
    --output .memplex/benchmarks/baseline_k10.jsonl
```

- `--synthetic` skips HuggingFace downloads and generates data locally
  (cached files are bypassed, so regeneration is deterministic).
- `--dataset all` covers all six concrete datasets: `locomo`, `nq`,
  `triviaqa`, `popqa`, `hotpotqa`, `memory_benchmark`. The composite aliases
  `nq_trivia` / `popqa_hotpot` run their two member datasets together.
- Warm mode seeds each sample into a fresh `MemplexService` (default config)
  before querying, then the runner measures retrieval and, where applicable,
  generation.
- Results are appended to the JSONL output file, one metric per line.

### Sample counts (synthetic)

| Dataset | Samples | Shape |
|---------|---------|-------|
| locomo | 3 | conversation turns + ground-truth memories |
| nq | 5 | question → short answer |
| triviaqa | 5 | question → answer + aliases |
| popqa | 5 | subject / relation / object triples |
| hotpotqa | 3 | question + supporting facts (2-hop) |
| memory_benchmark | 59 | 50 facts + 4 preferences + 5 observations, generated in code |

### Caveats

- With 3–5 samples per dataset, a single sample swings a metric by
  0.2–0.33. Treat the tables as smoke signals, not rankings.
- Some metrics vary between runs (e.g. LoCoMo `recency_accuracy`,
  `mrr`) because access counts and timestamps shift with repeated seeding
  into the default store.
- NQ/TriviaQA/PopQA/HotpotQA runners always compute metrics at
  k ∈ {1, 5, 10}, regardless of `--top-k`; with `--top-k 5` only 5
  candidates are retrieved, so `@10` columns there mean "over the 5
  retrieved". LoCoMo reports only the requested k.
- `latency_ms` is mean per-query retrieval latency (~2.2 s on the M4 lite
  setup; the store is tiny, so this is dominated by fixed per-query cost,
  not corpus size).

## Metric definitions

Standard IR / text metrics (`benchmarks/metrics.py`):

- **recall@k** — fraction of ground-truth memories found in the top-k.
- **precision@k** — fraction of top-k slots holding a ground-truth memory.
- **mrr** — mean reciprocal rank of the first relevant result.
- **f1 / exact_match / bleu / rouge_l** — token-level F1, exact string
  match, BLEU-4, and ROUGE-L between the generated answer and the reference
  (generation phase; with no LLM the "generation" is the top retrieved
  content, so these stay low).

Memplex-specific metrics:

- **fact_retention_rate** — fraction of seeded Fact memories still
  retrievable by ID after seeding (`memory_benchmark`).
- **preference_retention_rate / observation_retention_rate** — same, for
  Preference and Observation memories, retrieved by natural query.
- **recency_ranking** — agreement between recency order and retrieval rank
  for memories of varying age (`memory_benchmark`; LoCoMo's
  `recency_accuracy` is the analogous pairwise check).
- **hop_precision@k / hop_recall@k** — per retrieved slot / per required
  hop coverage of HotpotQA supporting-fact entities in the top-k.
- **hop_coverage** — fraction of required hops covered anywhere in the
  retrieved set.
- **multihop_accuracy** — 1.0 only when *all* required hops are covered.

## Baseline results

### top-k = 10 (CLI default)

| Dataset | Metric | Value | Samples |
|---------|--------|-------|---------|
| memory_benchmark | fact_retention_rate | 1.0000 | 50 |
| memory_benchmark | preference_retention_rate | 1.0000 | 4 |
| memory_benchmark | observation_retention_rate | 1.0000 | 5 |
| memory_benchmark | mrr | 0.5267 | 50 |
| memory_benchmark | recency_ranking | 0.2589 | 50 |
| locomo | recall@10 | 1.0000 | 3 |
| locomo | precision@10 | 0.2000 | 3 |
| locomo | mrr | 0.8333 | 3 |
| locomo | recency_accuracy | 0.5000 | 3 |
| locomo | bleu | 0.2252 | 3 |
| locomo | rouge_l | 0.2184 | 3 |
| locomo | exact_match | 0.0000 | 3 |
| popqa | recall@10 | 1.0000 | 5 |
| popqa | recall@5 | 1.0000 | 5 |
| popqa | recall@1 | 0.2000 | 5 |
| popqa | mrr | 0.4567 | 5 |
| popqa | exact_match | 0.2000 | 5 |
| popqa | f1 | 0.2402 | 5 |
| triviaqa | recall@10 | 0.2000 | 5 |
| triviaqa | precision@10 | 0.0600 | 5 |
| triviaqa | mrr | 0.2667 | 5 |
| triviaqa | exact_match | 0.0000 | 5 |
| triviaqa | f1 | 0.1498 | 5 |
| hotpotqa | hop_coverage | 0.8333 | 3 |
| hotpotqa | hop_recall@10 | 0.8333 | 3 |
| hotpotqa | multihop_accuracy | 0.6667 | 3 |
| hotpotqa | mrr | 0.1500 | 3 |
| hotpotqa | f1 | 0.0303 | 3 |
| nq | recall@10 | 0.2000 | 5 |
| nq | precision@10 | 0.0200 | 5 |
| nq | mrr | 0.0250 | 5 |
| nq | exact_match | 0.0000 | 5 |
| nq | f1 | 0.0333 | 5 |

### top-k = 5

| Dataset | Metric | Value | Samples |
|---------|--------|-------|---------|
| memory_benchmark | fact_retention_rate | 1.0000 | 50 |
| memory_benchmark | preference_retention_rate | 1.0000 | 4 |
| memory_benchmark | observation_retention_rate | 1.0000 | 5 |
| memory_benchmark | mrr | 0.5400 | 50 |
| memory_benchmark | recency_ranking | 0.3392 | 50 |
| locomo | recall@5 | 0.5000 | 3 |
| locomo | precision@5 | 0.2000 | 3 |
| locomo | mrr | 1.0000 | 3 |
| locomo | recency_accuracy | 0.5000 | 3 |
| locomo | bleu | 0.2252 | 3 |
| locomo | rouge_l | 0.2184 | 3 |
| popqa | recall@5 | 1.0000 | 5 |
| popqa | recall@1 | 0.2000 | 5 |
| popqa | mrr | 0.5167 | 5 |
| popqa | exact_match | 0.2000 | 5 |
| popqa | f1 | 0.2402 | 5 |
| triviaqa | recall@5 | 0.2000 | 5 |
| triviaqa | precision@5 | 0.1200 | 5 |
| triviaqa | mrr | 0.2667 | 5 |
| triviaqa | f1 | 0.1498 | 5 |
| hotpotqa | hop_coverage | 0.6667 | 3 |
| hotpotqa | hop_recall@5 | 0.6667 | 3 |
| hotpotqa | multihop_accuracy | 0.3333 | 3 |
| hotpotqa | mrr | 0.1111 | 3 |
| hotpotqa | f1 | 0.0303 | 3 |
| nq | recall@5 | 0.0000 | 5 |
| nq | precision@5 | 0.0000 | 5 |
| nq | mrr | 0.0000 | 5 |
| nq | f1 | 0.0333 | 5 |

Mean per-query retrieval latency across datasets: ≈ 2.1–2.4 s (both k
settings; dominated by fixed per-query overhead on the lite backend).

Raw artifacts: `.memplex/benchmarks/baseline_k10.jsonl`,
`.memplex/benchmarks/baseline_k5.jsonl` (one metric per line, with
per-metric latency and sample count).

### How to read these

- `memory_benchmark` retention of 1.0 confirms Facts, Preferences, and
  Observations survive seeding and are retrievable — the core memory-loop
  guarantee.
- PopQA/LoCoMo recall@10 of 1.0 on synthetic data confirms the
  FTS5 + reranker path returns seeded content for near-verbatim queries.
- Near-zero NQ numbers reflect the synthetic NQ samples querying for
  general-knowledge answers that were never seeded as memories — expected
  on this data, not a retrieval regression.
- Low generation metrics (BLEU/ROUGE/F1/exact_match) are expected with no
  LLM: the "answer" is raw retrieved content, not a synthesized response.

## Reproduce

From the repository root:

```bash
pip install -e .

# Full synthetic baseline, both k settings (~15 min total on an M4;
# memory_benchmark dominates the runtime)
MEMPLEX_STORAGE_BACKEND=lite memplex benchmark run \
    --dataset all --synthetic --top-k 10 \
    --output .memplex/benchmarks/baseline_k10.jsonl
MEMPLEX_STORAGE_BACKEND=lite memplex benchmark run \
    --dataset all --synthetic --top-k 5 \
    --output .memplex/benchmarks/baseline_k5.jsonl

# Single dataset
memplex benchmark run --dataset locomo --synthetic --top-k 10

# List datasets
memplex benchmark list
```

## Synthetic vs. real data

Without `--synthetic`, `memplex benchmark run` downloads the real datasets
(requires the `datasets` package and network access):

| Dataset | HuggingFace source |
|---------|--------------------|
| popqa | `mteb/popqa` |
| hotpotqa | `hotpotqa/hotpot_qa` (fullwiki) |
| nq | `natural_questions` |
| triviaqa | `triviaqa` (rc) |
| locomo | GitHub SNAP Research (synthetic fallback built in) |
| memory_benchmark | none — always generated in code |

Real runs differ from this baseline in every important way: thousands of
samples instead of 3–5, natural language distributions instead of
hand-written Q&A, and genuine distractor corpora. Numbers from real runs
are not comparable to the tables above; publish them separately when
measured.
