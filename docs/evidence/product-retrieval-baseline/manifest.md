# Product-path retrieval baseline matrix (P0.2) — Raw-RAG control dominates counting-question full coverage

The decision memo `docs/research/counting-retrieval-decision-2026-10.md` fixed an
acceptance framework whose control arms are "默认非编排 / orchestrated top-24 /
计数组合变体 / Raw-RAG 基线" at session-level any/full coverage before any
implementation. This matrix runs three of those arms (plus the parity
composition) locally, no API, per question, and delivers the headline negative
for the typed stack: **on counting-intent questions the pure-BM25 raw-session
control reaches full gold-session coverage on 87/99 questions (0.8788) vs
7/99 (0.0707) for orchestrated and 40/99 (0.4040) for parity**. The gold
sessions are enumerable by a session-unit leg over raw text at top-24; the
product stack's composition does not enumerate them.

## Design

- **Runner**: `scripts/product_retrieval_baseline.py` (local, no API). Pool:
  133 `multi-session` questions from `.memplex/benchmarks/data/longmemeval_s_cleaned.json`
  (the gap_forensics `DATA`; multi-session filter per script). Gold sessions =
  `answer_session_ids`, 344 total (270 in the 99 counting-intent questions);
  per-question `gold_n` ranges 2–5, no empty-gold rows.
- **Same-corpus four compositions**: every question gets a FRESH lite store
  seeded with exactly its own haystack (repo runner lineage — gap_forensics and
  the product-parity lineage both work this way), then all four compositions
  query that same per-question corpus at `top_k=24`; temp store torn down per
  question:
  - `default` — `service.query`, `orchestrated=False`, default fusion
    (paragraph fallback fill);
  - `orchestrated` — `service.query`, `orchestrated=True`, default fusion;
  - `parity` — `orchestrated=True` + `MEMPLEX_PARAGRAPH_FUSION=primary`
    (the product-parity composition);
  - `rawrag` — pure BM25 (`local_bm25_search`) over the raw session texts:
    same corpus, same top-k, no typed memories, no service stack.
- **Session-level coverage, any/full**: `any` = ≥1 gold session surfaces in
  top-24; `full` = the complete enumeration (all gold sessions in top-24) —
  the thing counting/aggregation questions need. Split by counting intent.
- **Scale design note (measured 2026-10-24)**: an earlier variant seeded the
  whole pool union (5908 sessions) into one store behind a single deferred
  commit; three runs failed on scale (throughput collapsed ≥7x, 16/min →
  4.5–6/min, 6h timeout without finishing seeding; generational-GC traversals,
  only partially mitigated by periodic `gc.freeze`). Per-question fresh stores
  do the same total write count in the fast small-store regime.

## Numbers (n=133; counting 99, non-counting 34; top-24)

| composition | any/counting | full/counting | any/non-counting | full/non-counting |
|---|---|---|---|---|
| default | 50/99 = 0.5051 | 7/99 = 0.0707 | 21/34 = 0.6176 | 3/34 = 0.0882 |
| orchestrated | 50/99 = 0.5051 | 7/99 = 0.0707 | 21/34 = 0.6176 | 3/34 = 0.0882 |
| parity | 96/99 = 0.9697 | 40/99 = 0.4040 | 34/34 = 1.0000 | 16/34 = 0.4706 |
| rawrag | 98/99 = 0.9899 | **87/99 = 0.8788** | 34/34 = 1.0000 | 32/34 = 0.9412 |

Verification: all 16 cells above recomputed from the per-question `rows.json`
on 2026-10-24 and matching `baseline.json` exactly (`ALL MATCH: True`).
`default` and `orchestrated` are **identical row-by-row** — every one of the
133 rows has the same `any`/`full`/`hits` triple under both, consistent with
the disclosed no-LLM degradation limit below; this identity is measured, not
assumed.

## Counting-intent annotation

The counting/non-counting split uses the keyword heuristic inherited verbatim
from `gap_forensics.counting_intent` (itself a copy of the harness runner's
`counting_intent()` in `scripts/run_lme_official_j.py`). It is a **heuristic,
not ground truth** — 99/133 questions classified counting-intent by keyword.
The same caveat as the gap-forensics manifest applies: conclusions keyed to
"counting questions" inherit this classifier's error rate.

## Honest limitations

1. **Retrieval-level instrument, no answer accuracy** — coverage is a
   necessary-condition metric; the scored harness protocol (same answerer,
   same judge, per-question pairing) is the decision gate and was NOT run
   here.
2. **No query-enhancement LLM** (`MEMPLEX_LLM_QUERY_ENHANCEMENT=false`):
   `orchestrated` degrades to its fail-closed single-query fanout, so
   `default` vs `orchestrated` differing zero in this matrix is a **local
   no-LLM delta only** and must not be extrapolated to enhanced runs.
3. **Single run** (deterministic retrieval at temperature-0 embedding; no
   repeat variance shown).
4. **Coverage attribution rules inherited from gap_forensics and not
   unit-identical across arms**: service compositions attribute coverage by
   paragraph `raw_text` `[sid @ date]` prefix mapping of result ids, or by a
   ≥40-char normalized summary containing the gold body's first 80 chars;
   `rawrag` ranks session ids directly. Same corpus and same top-k, but the
   units of ranking differ (typed memories/paragraphs vs whole sessions) —
   that unit difference is part of what the Raw-RAG control exists to
   measure, not a defect to fix.
5. **Per-question fresh store ≠ one shared store**: each question's corpus is
   its own haystack only; pool-union interference is out of scope by design.
6. **Smoke-mode footgun**: `MEMPLEX_PRB_LIMIT=N` truncates the pool for smoke
   runs only; this run is the full 133-question multi-session pool (full runs
   must leave it unset).

## Evidence level, runner, artifacts

- **Evidence level**: E1 (matching `docs/evidence/product-gap-forensics/`
  manifest's `evidence_level`; local deterministic instrument, per-question
  rows retained, aggregate reproduction verified).
- **Runner**: `scripts/product_retrieval_baseline.py` (local, no API).
- **Artifacts**: `baseline.json` (summary) and `rows.json` (per-question
  rows: `qid`, `gold_n`, `counting`, per-composition `any`/`full`/`hits`)
  — committed alongside this manifest in
  `docs/evidence/product-retrieval-baseline/` (byte-copies of the
  runner's `benchmarks/results/product-retrieval-baseline/` outputs,
  checked in 2026-10-24 so the evidence pack is self-contained instead
  of referencing a gitignored directory).
- **Date**: 2026-10-24.
