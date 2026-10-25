# Frozen offline LongMemEval-S pilot

This is a retrieval/conformance pilot, not answer accuracy, a dense-model comparison,
or a state-of-the-art claim. It uses the public cleaned LongMemEval-S corpus through
the actual public `MemplexService.write` and `query` paths. No `store.add`, oracle
memory insertion, paid models, model credentials or synthetic corpus fallback.

## Corpus and frozen split

- Official repository: https://github.com/xiaowu0162/LongMemEval
- Official cleaned dataset: https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned
- Revision: `98d7416c24c778c2fee6e6f3006e7a073259d48f`
- File: `longmemeval_s_cleaned.json`, 277,383,467 bytes
- SHA256: `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`
- License: MIT, as declared by the official dataset card; upstream code uses MIT
- Dataset files are cached outside the repository and are **not** redistributed here

`manifest.json` freezes 100 questions before tuning. The remaining 400 are question-ID-disjoint heldout questions, not independent history families.
Nine pilot IDs have an original/abstention partner in heldout; future generalization
claims must account for this overlap rather than treating the groups as independent.
Strata use official question type, with `_abs` IDs grouped as abstention. Allocation
is proportional with largest-remainder rounding, then SHA256(seed + ':' + id)
ordering. The seed is `memplex-offline-pilot-20261007-v1`. Input file order has no
influence. The run command rejects any shortened, replaced or overlapping manifest.
The restored run uses the same selection algorithm and seed as the earlier interrupted
workspace; earlier lost artifacts are not presented as auditable evidence.

## Reproduction

Fetch the pinned URL from `manifest.json` into a local cache using an ordinary file
downloader. This preparation step requires network; evaluation itself is offline.
Use the repository's locked Python environment, then:

```sh
python -m unittest tests.test_offline_comparison -v
python -m benchmarks.offline_comparison.longmemeval run \
  --dataset /path/to/cache/longmemeval_s_cleaned.json \
  --manifest benchmarks/offline_comparison/manifest.json \
  --out /path/to/results --timeout 120
python -m benchmarks.offline_comparison.obsolete_audit --out /path/to/obsolete-audit.json
```

The 120-second bound is per complete question bank. A timeout or ingestion/query
exception is an explicit error, never zero recall. All 100 are attempted. BM25 is
retained independently if product ingestion fails. Nonzero exit status means the
comparison is blocked or incomplete; inspect `summary.json` and `records.jsonl`.
Resume is allowed only with the identical manifest, product source, harness,
dependencies, runtime receipt and timeout. Use a new output directory after changes.
A truncated JSONL final line fails closed and needs manual recovery; it is never
silently discarded. Records without their matching receipt are rejected.
No run option selects a favorable smaller subset. Gold answers are not used at all;
evidence IDs and abstention labels remain evaluator-only. `has_answer` is removed
from every history turn before either retriever receives input.

## Precisely what is measured

Each question gets a fresh isolated Lite bank. Complete sessions are sorted by date,
written individually and chronologically; turn role and date are included in text.
Repeated headings, content and all distractors are preserved. This is the real public
product ingestion path on the development Lite backend, **not a production PostgreSQL
deployment benchmark**. The background worker, compaction and LLM enhancements are
disabled. Rule extraction and the product's local TF-IDF embedding are used. There is
no dense baseline because no pre-provisioned dense model was selected. Config is
explicit and receipts record its value. Child processes have an empty credential
context and network calls fail closed. No user config is loaded.

The BM25 comparator indexes full session text with Unicode word tokens, k1=1.5,
b=0.75. The product retrieves up to 100 nodes, then maps genuine output node IDs
back to their ingested source sessions. Sessions are ranked by their first retrieved
node, never by evidence labels; duplicate session IDs are collapsed. Node IDs shared
by multiple ingested sessions map to all such sessions in stable ID order. This can
make session recall optimistic when many sessions share a node. Unmapped candidates across the entire returned candidate list
are reported rather than guessed from gold text. Both systems output the first 10
ranked sessions. Different retrieval units and candidate budgets are explicit;
these are system-level retrieval comparisons, not identical algorithm benchmarks.

- Supporting-session recall@5/@10 is macro-averaged across non-abstention questions
- Complete coverage@5/@10 requires **all** gold evidence session IDs in the prefix
- Abstention questions remain in the fixed100 run and latency accounting, but their
  retrieval recall is undefined; the official benchmark similarly excludes them
- Returned tokens are Unicode **word counts**, not a model tokenizer's billing units
- BM25 counts returned full sessions; product counts selected evidence summaries
  once each, not the entire source session or all candidate nodes
- Ingest/query p50/p95 are empirical wall times; query measurement is cold and once
  per fresh bank. This small sample cannot establish service-level latency SLOs
- BM25 storage is serialized term-frequency bytes, whereas Memplex storage is
  on-disk bank bytes; neither is total RAM or an apples-to-apples storage efficiency score
- Dates are supplied in history text. No assertion is made that current product
  metadata normalizes relative dates or that retrieval resolves temporal truth

`obsolete_audit.py` is a separate five-scenario synthetic audit of old/new evidence
exposure. Its outcomes are never mixed with public-benchmark recall. Seeing obsolete
evidence does not prove a generated answer reused it; no answer model is called.

## Baseline ingestion diagnostic

Before the workspace reset, all100 attempted questions failed during full-session
public ingestion with `LiteMemoryStore merge contains duplicate Function id`. That
historical observation is **not** current-run evidence. A direct public-service minimal diagnostic is:

```python
source = SourceDocument(type='text', content='**Cost:**\n\nOne price.\n\n**Cost:**\n\nAnother price.')
service.write(source)
```

The rule extractor can produce two Functions with the same content-derived ID for
the repeated standalone heading. The benchmark never assigns or changes these IDs.
A fix belongs in a separate production change, followed by the exact same frozen
pilot. Splitting sessions or removing repeated text to evade this failure would
change the protocol and must not silently replace this baseline.

The label-free command-line reproducer includes a harmless introductory paragraph
so the role prefix does not swallow the first standalone heading:

```sh
python -m benchmarks.offline_comparison.reproduce_ingest_collision --out /path/to/diagnostic.json
```
