# Changelog

All notable changes to Memplex are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Multi-node memory sharing: central server + local cache sync architecture
  (`MEMPLEX_REMOTE_URL`, LWW conflict resolution, tombstone propagation).
- P2P mesh sync (`MEMPLEX_PEERS`).
- SSE push notifications for near-real-time sync (`/sync/events`).
- Background auto-pull worker (`MEMPLEX_SYNC_PULL_INTERVAL`).
- Native PostgreSQL backend (`memplex[postgres]`, JSONB + tsvector + pgvector).
- Incremental FTS5 indexing (per-func upsert instead of full rebuild).
- Scheduled background compaction (threshold-triggered on write).
- Indirect injection defense on all write paths (write/write_text/update_memory).
- `<private>...</private>` content redaction on all write paths.
- Structured JSON logging (`MEMPLEX_LOG_JSON`).
- Async non-blocking sync push (ThreadPoolExecutor + flush_push).
- Version-aware tombstones (delete-vs-edit conflict fix).
- Redis pub/sub SSE broadcast with auto-probe (`MEMPLEX_REDIS_URL` or auto).
- Read-write split for sync (`MEMPLEX_READ_URL`).
- Congestion-aware health indicators + SSE connection limit + doctor advice.
- Incremental `/sync/changes` query (`list_changes_since`).
- `storage_path` in health/stats output.
- `memplex sync pull` / `memplex sync status` CLI commands.
- HTTP bind security check with tests.
- `memplex doctor --smoke` readiness check.
- Coverage gate in CI (`--cov-fail-under=68`), macOS in test matrix.
- `pip-audit --strict` security job in CI.
- Expanded injection pattern detection (30+ regexes across 6 bypass families).
- `memplex backup` / `memplex restore` CLI commands.
- `schema_version` in memory.json header + migration helper.
- HTTP API rate limiting (per-API-key).
- `/metrics` Prometheus endpoint.

### Changed
- `StorageConfig.backend` default `"standard"` -> `"lite"` (only implemented backend).
- FastAPI `on_event` deprecated API -> `lifespan` async context manager.
- `service.py` split: extracted `query_explainer`, `intent`, `retrieval/multi_path`,
  `processing/function_builder`, `adapters/_shared`.
- `core/engine.py` dead `_detect_memory_type` removed; `_paragraphs_to_functions`
  and `_build_edges_rule_based` extracted to processing modules.
- `cli.build_parser` split into 6 domain helpers.
- `agent_installer` uses registry dict instead of if/elif dispatch.
- `corpus_index(dry_run=True)` status field bug fixed (dict merge order).
- `function_builder` dead if/else branch collapsed.
- `increment_access` batched to single persistence pass per query.
- `benchmarks/` moved to top-level (out of core package).
- `logging_utils.py` removed (dead code).
- `stepup` CLI alias verified as intentional (not a typo).
- Deduplication: `_dataclass_to_dict` unified into `adapters/_shared`.

### Removed
- `memplex/metrics.py` (superseded by operator report surface).
- `memplex/logging_utils.py` (zero references).

## [3.2.7] - 2026-05-24

### Added
- Productized operator workflows: scope/policy/corpus/inbox/report/doctor.
- Agent installer registry for Codex/Claude Code/OpenClaw/Hermes.
- LiteMemoryStore with SQLite FTS5/BM25 + trigram search.
- Offline-first embedding (TF-IDF default, HF opt-in).
- Multi-path retrieval (RAG + Wiki + Graph) with 5-dim reranker.
- Compaction pipeline (extract, dedup, summarize, prune, archive).
