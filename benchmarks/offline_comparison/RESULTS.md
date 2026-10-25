# Baseline result: production ingestion blocks comparison

Run date: 2026-10-07 UTC. Product baseline: `213ba65506409e7456b6699e269310feaa9970c5`.
The committed [receipt](results/main-213ba65/receipt.json) pins the dataset, configuration,
complete runtime-source fingerprint, harness, Python/dependency versions and environment.
This report uses newly rerun, persisted results after workspace recovery; no lost prior
artifact or partial SkillFlow result is reused.

## Main fixed 100 result

All 100 manifest IDs were attempted. BM25 completed 100. Memplex completed 0: every
question failed during real full-session ingestion with
`LiteMemoryStore merge contains duplicate Function id`. There were no timeouts.
Memplex recall, query latency and returned-token metrics are **unavailable**, not zero.
There is no successful Memplex-versus-BM25 quality ranking and no SOTA claim.

For the standalone BM25 baseline, 94 non-abstention questions are scored:

| Metric | BM25 |
| --- | ---: |
| Supporting-session recall@5 | 0.942376 |
| Supporting-session recall@10 | 0.955851 |
| Complete evidence coverage@5 | 0.872340 (82/94) |
| Complete evidence coverage@10 | 0.914894 (86/94) |
| Mean returned word tokens@5 | 10,760.38 |
| Mean returned word tokens@10 | 21,107.06 |
| Ingest p50 / p95 | 27.60 / 46.54 ms |
| Query p50 / p95 | 0.344 / 0.826 ms |
| Serialized index bytes p50 / p95 | 297,063.5 / 311,802 |

All 100, including 6 abstention cases, contribute to latency/token/storage summaries.
These are single cold measurements on a shared cloud runner; concurrent verification
may contend for resources. Word counts are not model-token counts. BM25 returns whole
sessions, so its context size is substantial. Storage is serialized term frequencies,
not memory usage. The protocol does not establish a production SLO.

The manifest is question-ID-disjoint:100 pilot and 400 heldout IDs. Nine pilot IDs
have an original/abstention partner across that split, so it is not an independent
history-family holdout. No heldout question was scored or used for tuning.

## Root-cause diagnostic, separate from product fixes

[The minimal real-path diagnostic](results/main-213ba65/ingest-collision.json) retains
two standalone `**Cost:**` headings. Rule extraction produces duplicate Function IDs;
the normal storage merge rejects the batch. The harness assigns no product memory IDs,
does not catch-and-drop duplicate memories, and does not split/rewrite sessions to
manufacture retrieval scores. Fixing this requires a separate production change, then
rerunning the same frozen manifest into a new output directory.

## Separate obsolete-evidence audit

Five synthetic old/new updates all returned both older and newer source sessions in
the first 10 ranked sessions. [Detailed audit](results/main-213ba65/obsolete-audit.json).
This is evidence exposure, not proof of a wrong generated answer. No answer model or
LLM maintenance pass was used, and these five cases are not mixed into public-corpus
recall. The offline configuration also does not evaluate LLM factual-capture behavior.

## Reproduction artifacts

- [Manifest](manifest.json), exact frozen 100/400 IDs
- [Per-question records](results/main-213ba65/records.jsonl), including explicit ingestion errors
- [Recomputed summary](results/main-213ba65/summary.json)
- [Receipt](results/main-213ba65/receipt.json), source/config/data fingerprints
- [Protocol and commands](README.md)

The official 277 MB corpus is deliberately not vendored. Independent review corrected
resume integrity, runtime-asset hashing and provenance-diagnostic accounting before
this final rerun. Fourteen protocol tests pass, including Python without site packages.
Required full repository checks are reported separately; this draft is not a merge
readiness or full-suite-green assertion.

## Local verification status

[Detailed verification](results/main-213ba65/verification.json) records:

- Protocol tests: 14 passed, including `python -S` without site dependencies
- Ruff, 4 architecture contracts, mypy across 128 source files, offline lock check: pass
- Full repository command: 4,873 passed, 5 failed, 15 setup errors, 26 skipped;
  236 subtests passed; coverage 80.92% (75% threshold met)
- Full-suite failures are release-build/isolated-install checks in the offline setup;
  they are explicitly listed, not silently ignored
- Supplemental release subset with the already available cache: 21 passed, 4 failed;
  remaining isolated installs need packages unavailable in their offline caches
- Supplemental public subprocess checks: 8 passed after linking the expected local
  environment path to the already restored environment

These focused rechecks do not turn the failed full run into a full pass. No additional
registry fetch, paid call, merge or deployment was performed for this benchmark.
