# G003 current worktree benchmark

## Status

This report describes one **E1 aggregate-only synthetic smoke run**. Its base
commit is `ef9aa8f4f224fbe9ddcc9de5de603fbbf11352b2`, but the source worktree was
dirty (`source.dirty=true`; diff digest
`f9ca1657ca3b39b7e301b8eb4142ba2787587d897232df826f120239d805554e`). It is
not evidence for the clean commit, a current clean SHA, public datasets, raw
per-sample behavior, capacity, production readiness, or production behavior.

The corrected run was created on 2026-08-30 Asia/Shanghai
(`2026-08-30T00:32:22.083487Z`) with Python 3.13.15 on macOS/arm64, branch
`fix/review-swave`, Lite storage, `--dataset all --synthetic --top-k 10
--seed 17`, and a recorded `uv.lock` digest of
`148d12dc7710af9f7d1d684599c5bf1e03cbc1c999e3c27eca36fde7149880cc`.

### G008 correction and invalidation

The previous bundle's global `config.warm=true` is **invalidated** as a run-wide
configuration claim: LongMemEval was actually invoked with `warm=false`.
This is a fresh execution, not a relabeling of old results. The manifest now
records `config.warm_by_dataset`: `longmemeval=false`, and `hotpotqa`,
`locomo`, `memory_benchmark`, `nq`, `popqa`, and `triviaqa` are all `true`.
The prior manifest SHA-256 was
`9f4dcfac5a0bafa14972929817cec80944a474bfc680ed8ba756fd2576b32cd2`;
its timestamps, latency values, and result digest no longer describe the
retained run. The old LoCoMo recency accuracy `0.5` and PopQA retrieval MRR
`0.6333` are replaced here by the freshly observed `0.6667` and `0.7`.
These run-to-run differences are not evidence of a quality improvement or
bit-for-bit reproducibility from the seed alone.

G008 also closes empty/incomplete-result and missing/malformed-provenance
validation gaps, redacts URL query/fragment and libpq credentials, and requires
`raw.status=null` with a nonempty reason for every aggregate-only bundle,
including synthetic inputs. No actual retained secret leak was found in the
review. Checksums are still unsigned and do not establish authenticity.

The source digest records the worktree at bundle creation, before this
artifact replacement and documentation update; it is not the final worktree
digest. Existing timezone-less LoCoMo and memory result timestamps are retained
without inventing a timezone; manifest provenance timestamps remain zoned.

## Artifact

- [`manifest.json`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/manifest.json)
  records source provenance, environment, configuration, coverage, dataset
  digests, limitations, and evidence level.
- [`datasets.json`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/datasets.json)
  embeds the exact synthetic inputs.
- [`results.jsonl`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/results.jsonl)
  contains 56 aggregate metric rows; it contains no per-query raw traces.
- [`checksums.sha256`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/checksums.sha256)
  covers the other three contract files.

SHA-256 checksums detect accidental corruption and inconsistent file sets.
They are stored in the same unsigned directory, so they do not authenticate the
producer and cannot detect a malicious rewrite that replaces both a payload and
its checksum. This bundle is not signed or published as an immutable artifact.

The regenerated payload SHA-256 values are:

```text
01419f00cf4f3488d446b553638154f096fd7b9cfd0f260f0d0deec3c846830a  datasets.json
c06d0c1a8bf63e78277970114a55ec95ddc2d894b5135062ad82f0a6142bca53  manifest.json
6f81b84cf70722480466a85188660c00ff1be50f291f47dd5f2d4dc638bda268  results.jsonl
```

## Coverage

| Dimension | Status | Evidence boundary |
|---|---|---|
| retrieval | passed | Synthetic retrieval aggregates were produced. |
| temporal_multihop | passed | Synthetic temporal or multi-hop aggregates were produced. |
| acl | not_measured | The run did not exercise ACL enforcement. |
| sync | not_measured | The run did not exercise synchronization. |
| latency_capacity | not_measured | Aggregate latency is not a capacity measurement. |
| recovery | not_measured | The run did not exercise recovery. |
| host_integration | not_measured | The run did not exercise host integration. |

Only retrieval and temporal/multihop passed the runner's aggregate production
check. The five `not_measured` dimensions must not be interpreted as passes.

## Datasets and sample counts

The manifest contains exactly seven datasets. `memory_benchmark.json` embeds
one provenance placeholder because its workload is generated in code; its
actual metric denominators are 50 facts, 4 preferences, and 5 observations.

| Dataset | Embedded records | Actual result samples | Source |
|---|---:|---|---|
| hotpotqa | 3 | 3 | generated synthetic |
| locomo | 3 | 3 | generated synthetic |
| longmemeval | 3 | 3 overall; 1 per question type | generated synthetic |
| memory_benchmark | 1 placeholder | 50 facts; 4 preferences; 5 observations | generated in code |
| nq | 5 | 5 | generated synthetic |
| popqa | 5 | 5 | generated synthetic |
| triviaqa | 5 | 5 | generated synthetic |

## Exact aggregate results

Each entry is `metric=value (samples, latency_ms)`, transcribed from
[`results.jsonl`](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/results.jsonl).
Repeated LongMemEval rows are retained because the artifact contains both.
The retained LongMemEval rows use the retired benchmark name
`longmemeval_answer_hit` and metric `answer_hit_rate`, transcribed as stored.
The current runner emits `longmemeval_answer_quality` rows instead:
`token_f1` (primary), `exact_match`, and `substring_hit_rate` — the
one-directional auxiliary diagnostic that supersedes `answer_hit_rate`. The
retained values follow the old metric definition and are not reproducible by
the current runner.

| Benchmark / dataset | Metrics |
|---|---|
| `hotpotqa_retrieval` / `hotpotqa` | `multihop_accuracy=0.6667 (3,55)`; `hop_coverage=0.8333 (3,55)`; `mrr=0.6667 (3,55)`; `hop_precision@1=1.0 (3,55)`; `hop_recall@1=0.6667 (3,55)`; `hop_precision@5=0.3333 (3,55)`; `hop_recall@5=0.8333 (3,55)`; `hop_precision@10=0.1667 (3,55)`; `hop_recall@10=0.8333 (3,55)` |
| `hotpotqa_generation` / `hotpotqa` | `exact_match=0.0 (3,50)`; `f1=0.1628 (3,50)` |
| `locomo_retrieval` / `locomo` | `recall@10=1.0 (3,53)`; `precision@10=0.2 (3,53)`; `mrr=1.0 (3,53)` |
| `locomo_recency` / `locomo` | `recency_accuracy=0.6667 (3,51)` |
| `locomo_generation` / `locomo` | `bleu=0.2252 (3,11)`; `rouge_l=0.2184 (3,11)`; `exact_match=0.0 (3,11)` |
| `longmemeval_answer_hit` / `longmemeval` | `answer_hit_rate=0.3333 (3,53)`; `answer_hit_rate=0.3333 (3,52)` |
| `longmemeval_answer_hit` / `longmemeval::knowledge-update` | `answer_hit_rate=0.0 (1,53)`; `answer_hit_rate=0.0 (1,52)` |
| `longmemeval_answer_hit` / `longmemeval::multi-hop` | `answer_hit_rate=0.0 (1,53)`; `answer_hit_rate=0.0 (1,52)` |
| `longmemeval_answer_hit` / `longmemeval::single-hop-user` | `answer_hit_rate=1.0 (1,53)`; `answer_hit_rate=1.0 (1,52)` |
| `memory_fact_retention` / `memory_benchmark` | `fact_retention_rate=1.0 (50,54)`; `mrr=1.0 (50,54)` |
| `memory_recency_decay` / `memory_benchmark` | `recency_ranking=0.2589 (50,52)` |
| `memory_preference_persistence` / `memory_benchmark` | `preference_retention_rate=1.0 (4,53)` |
| `memory_observation_tracking` / `memory_benchmark` | `observation_retention_rate=1.0 (5,52)` |
| `nq_retrieval` / `nq` | `mrr=0.2 (5,54)`; `precision@1=0.2 (5,54)`; `recall@1=0.2 (5,54)`; `precision@5=0.04 (5,54)`; `recall@5=0.2 (5,54)`; `precision@10=0.02 (5,54)`; `recall@10=0.2 (5,54)` |
| `nq_generation` / `nq` | `exact_match=0.0 (5,51)`; `f1=0.0267 (5,51)` |
| `popqa_retrieval` / `popqa` | `mrr=0.7 (5,53)`; `exact_match=0.4 (5,53)`; `recall@1=0.4 (5,53)`; `recall@5=1.0 (5,53)`; `recall@10=1.0 (5,53)` |
| `popqa_generation` / `popqa` | `exact_match=0.0 (5,52)`; `f1=0.2402 (5,52)` |
| `triviaqa_retrieval` / `triviaqa` | `mrr=0.1667 (5,54)`; `precision@1=0.0 (5,54)`; `recall@1=0.0 (5,54)`; `precision@5=0.12 (5,54)`; `recall@5=0.2 (5,54)`; `precision@10=0.06 (5,54)`; `recall@10=0.2 (5,54)` |
| `triviaqa_generation` / `triviaqa` | `exact_match=0.0 (5,51)`; `f1=0.1665 (5,51)` |

These values are smoke-level aggregates, not quality rankings. In particular,
the prior artifact's `hop_precision@1=1.3333` was impossible for a normalized
precision and is **invalidated**, not reinterpreted. It resulted from counting
two supporting hops found in one retrieved slot as two relevant slots. G005
corrected the definition to relevant retrieved slots divided by `k`, while
`hop_recall@k` retains unique supporting-hop coverage. The regenerated result
is `hop_precision@1=1.0`; bundle creation and verification now reject known
normalized metrics that are non-finite or outside `[0,1]`, even if payload
checksums are recomputed.

## Run and verify

The strict runner requires explicit synthetic mode and a destination that does
not already exist:

```bash
G003_RUN_ROOT="$(mktemp -d)"
.venv/bin/python scripts/run_g003_benchmark.py run \
    --synthetic --dataset all --top-k 10 --seed 17 \
    --run-dir "$G003_RUN_ROOT/bundle"
.venv/bin/python scripts/run_g003_benchmark.py verify \
    --run-dir "$G003_RUN_ROOT/bundle"
```

Verify this retained artifact:

```bash
.venv/bin/python scripts/run_g003_benchmark.py verify \
    --run-dir artifacts/g003-synthetic-worktree-ef9aa8f-k10
```

The current environment did not have the public-dataset dependencies or cache,
so public datasets were unavailable and were not run. The strict runner accepts
only explicit `--synthetic` runs; it does not silently substitute synthetic
records for an unavailable public dataset. A public-data baseline remains open.

## Historical boundary

The tables in [Benchmarks](benchmarks.md#historical-baseline-results) remain
historical. Do not overwrite or reinterpret them as results from this dirty
worktree artifact. The baseline qualification gaps remain open: clean-SHA
public datasets, per-sample raw traces, independent signatures and immutable
publication, plus measured ACL, sync, latency/capacity, recovery, and host
integration evidence.
