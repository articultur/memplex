# Changelog

All notable changes to Memplex are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- G002 生产 readiness 合同：`principal_tenant_acl` 仅在 `production + postgres +`
  可解析的非空 `MEMPLEX_PRINCIPALS_JSON` 时通过；非法 registry 以不泄露凭据内容的
  fail 状态报告。该变化不关闭后续工业门禁，整体仍为 Developer Preview。
- 中文生产与四宿主身份文档：统一 HTTP/CLI/MCP/Sync 的 principal、受管身份优先级、
  `user`/`workspace`/`session` 可见性，以及 development local-process 的同 UID 边界。
- Structured observation categories: the agent capture path now persists an
  `Observation` node per captured turn with a classified `category`
  (`bugfix` | `decision` | `change` | `discovery` | `note`), browsable via
  the new `memplex observations` CLI command and the MCP
  `memory_observations` tool; wiki observation pages carry the category too.
- Token cost visibility across read surfaces: `QueryResult.max_tokens`,
  per-result `est_tokens`, and `tokens_used` / `truncated` on MCP
  `memory_search` / `memory_get`, CLI `query` / `recall`, and
  `memplex agent recall` output.
- Optional LLM observation compression: long captured turns are compressed
  via `LLMEnhancer.compress_observation` before storage, with rule-based
  head/tail truncation when no LLM is available
  (`MEMPLEX_LLM_OBSERVATION_COMPRESSION=false` to disable).
- Sync protocol extended to all four node types: Fact / Preference /
  Observation nodes now flow through `/sync/push` and `/sync/changes` with
  client-side push/pull, completing multi-node sync beyond Function nodes.
- Real-PostgreSQL integration test suite (`tests/test_postgres_integration.py`,
  49 tests via pgserver, skipped unless run in `.venv-pgcheck`): covers
  hybrid RRF search with pgvector 0.6.2, sync, and feedback paths against
  PostgreSQL 16.2.
- Shared hook policy module (`memplex/core/hooks/policy.py`) as the single
  source of truth for rate limiting, dedup, and narrative filtering, wired
  into both the collector and the plugin hook runners.
- `memplex health --strict` for CI-friendly non-zero exit on warnings.
- Public `SQLiteFTSIndex.rebuild()` for worker-driven index refresh.
- `tests/conftest.py` shared fixtures; `fastapi` + `uvicorn` added to the
  `dev` extra.
- `docs/benchmarks.md` with reproducible baseline numbers, plus a README
  Benchmarks section.

### Fixed
- MCP memory tools now enforce the shared runtime identity/visibility boundary
  for search, writes, ID reads/mutations, feedback reviews, observations, and
  scope previews; identity comes only from managed/process state, so tool args
  cannot spoof it even when a managed environment is absent.
- Codex turn-state files are partitioned and validated by user, workspace, and
  session instead of session alone.
- Codex, Claude Code, OpenClaw, and Hermes single-host installs now restore
  their exact preinstall managed paths when a later install step fails; the
  four-host transaction now preserves the exact pre-call state of hosts that
  were already installed when a later host fails, including symbolic links
  and setuid/setgid/sticky permission bits.
- Claude Code hooks now enter through one fixed launcher instead of repeating
  inline plugin/Python discovery shell programs in every hook declaration.
- Model-facing search, review, observation, and scope-preview fan-out now has
  runtime hard caps; legacy typed compatibility reads migrate once into the
  current workspace and fail closed if provenance cannot be persisted, with
  public explanations rebuilt after runtime authorization so denied record
  metadata cannot survive only in the trace.
- OpenClaw and Hermes documentation now distinguishes their host entrypoints
  from the bridge-backed runtime/ABI boundary and fences absent Hermes CLI
  verification from installed-host claims.
- QueryScope.ALL now divides one candidate budget across active retrieval
  paths instead of multiplying it per path; scope preview reports only bounded
  scan counts, and the Hermes manifest pins the audited upstream ABI source by
  version, tag commit, source revision, URL, and SHA-256.
- Graph retrieval now accounts seed and one-hop neighbor reads against its path
  budget; Lite uses a maintained adjacency index, while PostgreSQL applies an
  indexed bidirectional query with `LIMIT` before joining Function rows.
- Postgres backend (verified against real PostgreSQL): `increment_access`
  crashed on `to_jsonb(%s)` parameter binding; the ivfflat index on an
  empty table silently produced zero vector-leg results; `_obs_to_json`
  bypassed `Observation.to_dict()` so owner filtering always came back empty.
- `claude_skill.py` hook script read nonexistent environment variables; now
  follows the stdin JSON hook contract.
- Benchmark runner: `--dataset all` silently omitted `memory_benchmark`,
  per-dataset result attribution was scrambled, HotpotQA metrics were
  mis-attributed, and `--synthetic` runs could be silently overwritten by a
  stale cache.
- MCP `memory_observations` truncated before filtering, dropping matching
  observations; now filters first.
- Lite store `add_fact` / `add_preference` upserts overwrote `updated_at`
  with local time, breaking LWW source-timestamp semantics for typed nodes;
  incoming timestamps are now preserved (aligned with the Function path).
- `tests/test_sync.py` contained an always-true `or True` assertion.

### Changed
- Serializer convergence: lite store `_save`/`_load` and postgres JSON
  helpers now delegate to the models' standard `to_dict`/`from_dict`
  instead of hand-written per-field serializers (postgres wrappers kept as
  thin shims for its PG-only fields).
- Removed three dead LLM config fields (`semantic_extraction`,
  `conflict_resolution`, `summarization`) from `config.py`.

## [3.3.0] - 2026-08-09

### Added
- Service-layer orchestration wiring: postgres storage/feedback backends
  unblocked, Fact/Preference persistence on the write path, wiki
  `DualIndexSearch` + embedding service injected into multi-path retrieval,
  shared store/engine/embedding/config injected into the background worker,
  pgvector embedder injection for the postgres hybrid search leg,
  `query(owner=)` filter, and owner-aware recall for Fact/Preference nodes.
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
- `Fact` and `Preference` memory node types, completing the 4-type model
  (Function / Fact / Preference / Observation).
- Benchmark CLI runner and evaluation harness in the top-level
  `benchmarks/` package (LoCoMo, NQ/TriviaQA, PopQA/HotpotQA).
- `schema_version` field written into the memory.json header on save
  (no automated migration function yet; the field is forward-looking).
- HTTP API rate limiting (per-API-key).
- `/metrics` Prometheus endpoint.

### Changed
- `StorageConfig.backend` default `"standard"` -> `"lite"` (the placeholder
  name is mapped to lite; `"postgres"` is the other selectable backend).
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
- Wiki layer wired in: `COMPILE_WIKI` worker task now runs real
  `WikiCompiler.compile_all` into the configured `wiki.dir` (new `wiki`
  config section, `MEMPLEX_WIKI_DIR` / `MEMPLEX_WIKI_ENABLED`);
  `MultiPathRetriever` accepts an optional `wiki_searcher` (e.g.
  `DualIndexSearch`); community detection honors
  `graph.community_detection_enabled` / `graph.community_min_size`.

### Fixed
- `Function` / `FieldValue` / `Observation` gained standard full-field
  `to_dict` / `from_dict` (including `needs_review_until`,
  `priority_from_source`, `source_authority`) as the convergence target
  for the drifted lite/postgres/http_api serializers.
- `ANTHROPIC_API_KEY` env var now works as fallback when
  `MEMPLEX_LLM_ANTHROPIC_API_KEY` is not set.
- `llm.max_input_length` is now honored by all LLM enhancer prompts
  (was silently hard-coded to 10000).
- LLM trigger-extraction output schema now declares `weight`
  (was parsed from the response but never requested).
- Injection guard bilingual pattern `disregard 上一条` no longer requires
  a leading space, so payloads at content start are caught.
- `MemoryFeedback` normalizes tz-aware datetimes to naive UTC at
  construction (naive/aware mixes raised TypeError on comparison).
- Postgres backend CRUD aligned with the `MemoryStore` base contract
  (list/get/update/delete semantics now match the lite store).
- Reranker query-time embedding no longer pollutes TF-IDF corpus
  statistics (uses `embed_query` when available, `embed` otherwise).
- Wiki package internals (compiler, generator, search) completed and wired
  into the default retrieval path (see Changed).
- Worker background handlers implemented (previously stubbed no-ops).
- `compaction.dedup_use_faiss` is now honored by the dedup stage
  (`MemoryDeduplicator(use_faiss=...)`, previously a dead config).
- `Function.MAX_VALUES_PER_FIELD` is now enforced when merging field
  values in both lite and postgres backends (previously declared only).

### Removed
- `LLMEnhancer.semantic_extract_trigger`, `LLMEnhancer.resolve_conflict`,
  and `LLMEnhancer.summarize` (plus their private helpers
  `_rule_based_extract`, `_authority_based_resolve`, `_parse_resolution`):
  no production caller existed -- extraction runs through `CoreEngine`,
  conflict handling through `processing/merger/conflict_resolver.py`, and
  no compaction-stage consumer existed for the `Summary` return type.
- `memplex/metrics.py` (superseded by operator report surface).
- `memplex/logging_utils.py` (zero references).
- `LLMConfig.reranking` + `MEMPLEX_LLM_RERANKING` env mapping: dead
  toggle with no LLM-reranking implementation anywhere in the codebase.
- `GraphConfig.semantic_similar_ttl_days` /
  `GraphConfig.semantic_similar_sync_on_merge` +
  `MEMPLEX_GRAPH_SEMANTIC_SIMILAR_TTL_DAYS` /
  `MEMPLEX_GRAPH_SEMANTIC_SIMILAR_SYNC_ON_MERGE` env mappings: dead config
  with no consumer (no edge-TTL expiry or merge-time resync machinery
  exists); the remaining `semantic_similar_threshold` /
  `semantic_similar_max_edges` are now wired into `GraphBuilder`
  SEMANTIC_SIMILAR edge detection.

## [3.2.7] - 2026-05-24

### Added
- Productized operator workflows: scope/policy/corpus/inbox/report/doctor.
- Agent installer registry for Codex/Claude Code/OpenClaw/Hermes.
- LiteMemoryStore with SQLite FTS5/BM25 + trigram search.
- Offline-first embedding (TF-IDF default, HF opt-in).
- Multi-path retrieval (RAG + Wiki + Graph) with 5-dim reranker.
- Compaction pipeline (extract, dedup, summarize, prune, archive).
