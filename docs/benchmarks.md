# Benchmarks

Memplex ships an evaluation harness (`benchmarks/`, **source checkout only** —
it is not part of the installed distribution) that measures retrieval and
memory-keeping quality across seven datasets.

## Current worktree evidence (G003)

The current G003 evidence is documented separately in
[Current worktree benchmark](current-worktree-benchmark.md). It is an **E1,
aggregate-only synthetic smoke run** from a dirty worktree based on commit
`ef9aa8f`; it is not clean-SHA, public-dataset, raw-trace, capacity, or
production evidence. The result tables below predate that run and remain
historical baselines.

The retained bundle was rerun for G008 after invalidating its old global
`warm=true` claim: `config.warm_by_dataset` now records LongMemEval as cold
and the other six datasets as warm. Creation and verification share strict
result/provenance validation, redact URL query/fragment and libpq credentials,
and require `raw.status=null` with a reason even for synthetic inputs. Exact
new values, payload digests, and the invalidation note are in the
[current worktree report](current-worktree-benchmark.md#g008-correction-and-invalidation).

The strict runner requires explicit synthetic mode, refuses an existing output
directory, records source/dataset/config/environment provenance, and writes a
canonical four-file bundle:

```bash
G003_RUN_ROOT="$(mktemp -d)"
.venv/bin/python scripts/run_g003_benchmark.py run \
    --synthetic --dataset all --top-k 10 --seed 17 \
    --run-dir "$G003_RUN_ROOT/bundle"
.venv/bin/python scripts/run_g003_benchmark.py verify \
    --run-dir "$G003_RUN_ROOT/bundle"
```

Verify the retained worktree artifact without rerunning benchmarks:

```bash
.venv/bin/python scripts/run_g003_benchmark.py verify \
    --run-dir artifacts/g003-synthetic-worktree-ef9aa8f-k10
```

The bundle contains `manifest.json` (provenance, configuration, coverage, and
limitations), `datasets.json` (embedded synthetic records), `results.jsonl`
(one aggregate `BenchmarkResult` per line), and `checksums.sha256`. The
checksums detect accidental corruption or an inconsistent bundle; because they
are stored beside the files and are not signed, they do not prove who created
the bundle and do not protect against a malicious rewrite of both payloads and
checksums.

> **Read this first:** the historical numbers on this page are an **offline
> synthetic baseline across six datasets**. Every included dataset was run
> with `--synthetic`, which generates a
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
- For the 2026-08-09 historical run, `--dataset all` covered six concrete
  datasets: `locomo`, `nq`, `triviaqa`, `popqa`, `hotpotqa`, and
  `memory_benchmark`. The composite aliases
  `nq_trivia` / `popqa_hotpot` run their two member datasets together.
- Warm mode seeds each sample into a fresh `MemplexService` (default config)
  before querying, then the runner measures retrieval and, where applicable,
  generation.
- Results are appended to the JSONL output file, one metric per line.
- Query latency is timed per call with `time.perf_counter` and recorded as
  float milliseconds; each result row carries the arithmetic mean plus
  nearest-rank `latency_p50_ms` / `latency_p99_ms` percentiles
  (`benchmarks/base.py::LatencyStats`; nearest-rank keeps small samples
  interpretable — the p99 of three samples is simply the maximum). Seeding
  is outside the timed block, and `warmup_rounds` (default 1) untimed
  queries run before measurement so first-call overhead (FTS cache, DB
  connection setup) does not pollute the samples. Warm vs cold seeding is
  recorded per dataset as `config.warm_by_dataset` in the G003 manifest.

### Sample counts (synthetic)

| Dataset | Samples | Shape |
|---------|---------|-------|
| locomo | 3 | conversation turns + ground-truth memories |
| nq | 5 | question → short answer |
| triviaqa | 5 | question → answer + aliases |
| popqa | 5 | subject / relation / object triples |
| hotpotqa | 3 | question + supporting facts (2-hop) |
| memory_benchmark | 59 | 50 facts + 4 preferences + 5 observations, generated in code |

### LongMemEval (current worktree only)

LongMemEval was not part of the 2026-08-09 six-dataset baseline or the
historical result tables below. Its only result on this page's evidence path is
in the separate [current dirty-worktree E1 bundle](current-worktree-benchmark.md).

`longmemeval` auto-detects two on-disk schemas per entry: the official
LongMemEval release format (`question_id` / `question_type` / `question` /
`answer` / `question_date` / `haystack_session_ids` / `haystack_dates` /
`haystack_sessions` / `answer_session_ids`; the single gold `answer` is
wrapped into a list and the per-session haystack flattened into one
turn-level history) and the repo's synthetic format (`answers` list plus a
flat `session_history`). `haystack_dates` are accepted but not yet
materialised as per-session timestamps — every turn inherits the question
date, so per-session temporal ordering is not reconstructed. Sessions are
seeded as searchable Function records (the `memory_eval` recipe) and each
question is scored over the retrieved summaries, reported overall and per
question type.

Honest scoring note: **multi-hop aggregation questions are not
retrieval-answerable** — they require computation over multiple memories
(`2 + 3 = 5`), which substring retrieval cannot produce. Expect ~0 on
that type in retrieval-only mode; use a generation model over retrieved
context for it. The synthetic fallback corpus pins exactly this split
(positive `token_f1` on single-hop-user and knowledge-update, `0.0` on
multi-hop).

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
- The default embedder is local TF-IDF (`embedding.model = "default"`,
  dim 384) — a lexical bag-of-words. These results measure the FTS5 /
  lexical retrieval path; they do not reflect semantic (neural embedding)
  retrieval quality.

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
- **hop_precision@k** — relevant retrieved slots divided by `k`; one slot that
  mentions multiple supporting hops still counts as one relevant slot.
- **hop_recall@k** — unique required supporting hops covered in the top-k,
  divided by the number of unique required hops.
- **hop_coverage** — fraction of required hops covered anywhere in the
  retrieved set.
- **multihop_accuracy** — 1.0 only when *all* required hops are covered.

LongMemEval answer-quality metrics (`benchmarks/longmemeval.py`; scored
against the gold answers — max over golds, SQuAD convention — over the
concatenated retrieved summaries):

- **token_f1** — primary metric; token-overlap F1 between the prediction
  and a gold answer.
- **exact_match** — 1.0 when the normalised prediction equals a normalised
  gold answer; near zero by construction for concatenated retrieval
  snippets, reported for honesty rather than as a quality target.
- **substring_hit_rate** — auxiliary diagnostic only; 1.0 when a
  normalised gold answer is a substring of the prediction
  (one-directional, so a short prediction contained in a longer gold does
  not count). It supersedes the retired `answer_hit_rate`.

Canonical G003 bundle creation and verification apply an explicit schema to
known normalized metrics (including the metrics above): values must be finite
and within `[0,1]`. Metrics not declared normalized, such as latency or
throughput measurements, may legitimately exceed `1`.

## Historical baseline results

These tables contain only the six datasets measured on 2026-08-09. They do not
contain LongMemEval, and current dirty-worktree results are not merged into
them.

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
