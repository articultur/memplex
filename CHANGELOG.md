# Changelog

All notable changes to Memplex are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and this project adheres
to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

2026-08-28 multi-dimensional review remediation:

- **Dead config removed**: 6 keys that were declared, validated, and
  tested but had zero runtime consumers (`observation.max_per_minute`,
  `logging.sanitize_sensitive`, `encryption.enabled`,
  `encryption.key_path`, `operations.startup/shutdown_timeout_seconds`)
  deleted along with the `ObservationConfig` and `EncryptionConfig`
  dataclasses; `logging.level` now wired into `configure_logging(level)`
  as a fallback below `MEMPLEX_LOG_LEVEL` at all three entry points.
- **admin.html XSS**: all template interpolations now escapeHtml
  (memory_type, knowledge_tier, updated_at were missed); models
  `from_dict` enforces `memory_type`/`knowledge_tier` enum sets at the
  deserialization boundary.
- **Corpus deny-list gap**: `.ssh/`, `.aws/`, `.kube/`, `.gnupg/`
  directories and `id_rsa`, `id_ed25519`, `id_ecdsa`, `*.pem`, `*.key`,
  `.netrc`, `.npmrc` files added to `PRIVATE_CORPUS_PATTERNS`.
- **CI hang risk**: `timeout-minutes: 45` added to test and
  test-postgres jobs; `pytest-timeout` (120s) prevents per-test hangs
  from consuming the entire GitHub Actions 360-minute budget.
- **DNS rebinding TOCTOU**: `url_handler.fetch` now pins the resolved
  IP from the SSRF safety check into the connection URL (HTTP) rather
  than performing a second independent DNS resolution.
- **Rate-limit lockout**: a full bucket registry now evicts the
  earliest-reset entry instead of rejecting new clients with 429.
- **Worker claim misalignment**: `_run_once` aligns `_active_task_id`
  with the actually-claimed task after a queue race.
- **tmp file leak**: `durability._write_and_fsync` cleans up its
  temporary file on write/fsync failure (previously accumulated on
  full-disk retries).
- **Worker timestamp parsing**: all `fromisoformat` calls wrapped as
  `TaskStoreIntegrityError`; `_lease_matches` returns False on
  unparseable lease timestamps.
- **HTTP max_tokens**: `/memories` route now caps at 32,000 (matching
  the MCP-side clamp).
- **Reranker N+1**: batch `get_many` + per-id fallback replaces
  per-result `storage.get()` calls.
- **count_functions()**: lightweight O(1)/SQL-COUNT contract added to
  `MemoryStore` ABC + both backends; `service._count_functions_exact`
  no longer paginates the full store for compaction scheduling.
- **Supersede N+1**: fact supersession batches into a single
  `list_facts()` call per write batch instead of per-fact.
- **Lite read amplification**: `_refresh_for_read` short-circuits on
  an (mtime_ns, size) fingerprint when both pair files are unchanged;
  FTS index signature only invalidates on generation change.
- **SSE reverse dependency**: service.py no longer imports
  `adapters.http_api._SSE_SUBSCRIBERS`; a domain-owned registration
  callback replaces the hexagonal contract violation.
- **Docs drift**: AGENTS.md mypy count now references pyproject (not
  hardcoded 12); architecture.md lite backend correctly described as
  JSON-pair + FTS5 sidecar (not "SQLite backend"); fictional C901
  per-file-ignores mechanism description corrected to inline noqa
  markers.

- **V1 (High, privilege escalation)**: `promote()` now requires the memory
  owner (or local development) — a cross-agent grant holder could
  previously promote the owner's private memory to team tier, leaking it
  workspace-wide through a read-only grant. Grant = read, never widen.
- **V2 (Medium, injection)**: `share_with()` rejects agent_ids containing
  commas/whitespace — the comma-joined grant store would silently split
  one id into multiple grants.
- **V3 (Medium, cross-tenant leak)**: WorkingMemory entries are scoped
  per tenant; sleep-time's graph scan now goes through the authorization
  gate's store facade instead of the raw base store.
- **S2 (domain binding blindness)**: typed `domain` and `knowledge_tier`
  fields are projected into namespace metadata at write time AND at
  filter time, so domain-bound agents actually see their domain's
  knowledge instead of zero results.
- **S3 (silent data loss)**: `store.add` merge path now carries
  `knowledge_tier`/`visibility`/`provenance`/`namespace` from the
  incoming node — `promote()`/`share_with()` on Function nodes no longer
  silently no-op.
- **S4 (cross-user team recall)**: runtime read-filters gain a team-tier
  branch (workspace + knowledge_tier=team, no per-user pinning) and the
  runtime post-check admits team-tier workspace nodes cross-user —
  promoted team knowledge is now actually recallable by a different
  member's runtime.
- Vacuous tests replaced with non-empty assertions; cross-user team
  recall and V1/V2 regressions are pinned by 3 new E2E tests.
- docs/architecture.md now maps the 5 post-S-wave leaf modules;
  memory-vs-knowledge.md grant wording corrected (read-only).

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

2026-08-30 quality-gates remediation (all-dimensions review):

- **Wheel package-data gap**: the built wheel shipped only 5 migration SQLs
  (missing `0006_background_tasks.sql`) and no `operations_assets/` at all —
  `admin.html` is loaded by file path so the `/admin` console silently
  degraded. `package-data` now covers `*.html`, and
  `tests/test_wheel_contents.py` pins the wheel contents contract.
- **CI coverage gate not enforced**: `ci.yml` passed only `--cov-report`;
  pytest-cov never read pyproject's `fail_under`, so coverage could regress
  silently. CI now passes `--cov-fail-under=75` and pyproject's
  `fail_under` is raised 68 → 75 (measured ~80.5%).
- **Readiness-probe ACL drift**: `pool.py` probed sync privileges from a
  hand-copied table list that had drifted from `runner._APPLICATION_ACL`
  (mixed `memplex_background_tasks` into the sync group, missed the
  `memplex_sync_ingress_principals` negative probe). The probe now derives
  from the runner ACL single source of truth.
- **`operations_metrics_status` under sync**: with sync enabled,
  `_postgres_resources` is `PostgresSyncStorageResources`, which lacks the
  pool-metric attributes — the endpoint raised `AttributeError`. The
  service now degrades safely (getattr with 0 default, aligned with the
  existing None semantics); regression test included.
- **Unclosed `<private>` tag observability**: the fail-open path (raw text
  kept) now emits a `privacy_unclosed_private_tag` warning log so likely
  redaction misses are visible; behavior unchanged.

### Added

- **Curation console** (`/admin`): static web page + JSON API
  (`/admin/api/memories|promote|share|facts`) with the same auth as
  every HTTP surface. Operators can browse memories, promote to
  knowledge tiers, and query bi-temporal history from a browser.
- mypy strict gate 21 → 23 files (operations.py + adapters/cli.py
  admitted; fixed 8 real type errors incl. latent None-deref in the
  backup executor paths).

- mypy strict gate expanded 16 → 21 files (service, reranker,
  multi_path, core/engine, sync admitted; all clean). The orchestration
  facade itself is now fully typed.
- ADR-009: MemplexService remains an orchestration facade — the
  boundary rule is new capabilities land in leaf modules, service only
  wires them.
- docs/curation-ui.md: design for the human curation console
  (recommended: web admin page reusing promote/share/facts APIs).

- **CLI + MCP surface for the knowledge tier**: `memplex promote <id>
  --tier team`, `memplex share <id> --agent <id>`, `memplex facts
  [--as-of ...] [--all]` commands; MCP tools `memory_promote`,
  `memory_share`, `memory_facts` with schema descriptions.
- mypy strict gate expanded from 12 to 16 files (temporal,
  working_memory, sleep_time, improve admitted; all clean).
- Mutation-testing baseline for `memplex/temporal.py`: 83% kill rate
  (59/71 completed mutants), survivor equivalence documented in
  `docs/mutation-testing.md`.

- HTTP-level integration tests for the S-wave features (5 tests):
  encrypted sync push tamper/reject/fail-closed/plaintext-compat,
  promoted team knowledge via HTTP recall, working-memory cross-tenant
  leak check.
- `docs/adr/`: 8 architecture decision records covering the session's
  key design choices (evidence gating, module splits, sync lockstep,
  one-store/two-lifecycles, read-only grants, shared-key encryption,
  bi-temporal supersede, complexity freeze).

- **Team knowledge tiering**: `MemoryNode.knowledge_tier`
  (personal/domain/team) with `service.promote()` — provenance-stamped,
  version-bumped promotion; `team` widens visibility to the workspace.
- **Cross-agent grants**: `service.share_with(memory_id, agent_id)` —
  additive idempotent grants honoured by the authorization gate for
  user-private nodes (`memplex_grants` namespace key, fail-closed).
- **Agent-domain binding**: `agent_domains.agent_domains` config scopes a
  runtime's recall to its bound knowledge domains (every visibility branch
  exploded with `domain` pinned).
- `docs/memory-vs-knowledge.md`: the one-store/two-lifecycle design answer.

- **Bi-temporal fact validity** (Zep/Graphiti-style): `Fact.valid_from` /
  `invalid_at`; the write path supersedes contradicted same-slot facts
  (subject+predicate) instead of overwriting, and
  `service.list_facts(as_of=...)` answers point-in-time queries — the
  "agent changed its mind" history stays auditable (`memplex/temporal.py`).
- **`improve()` maintenance verb** (Cognee-style): dedupes contradicting
  valid facts into temporal history, expires shelf-lapsed `valid_until`
  facts, rebuilds the FTS index. Exposed as `memplex improve` CLI
  (`memplex/improve.py`).
- **Sleep-time compute** (Letta-style, opt-in `sleep_time.enabled`):
  `SleepTimeAgent` daemon waits for sustained worker idle, reruns
  `improve()`, and precomputes graph-association inferences for the
  hottest memories into the working-memory tier as `[SLEEP-TIME]` entries
  (`memplex/sleep_time.py`).

- **6-dimensional reranker**: new per-memory `confidence` dimension
  (Hindsight-style belief strength, clamped/neutral-on-missing) and a
  configurable exponential recency half-life
  (`reranker.recency_halflife_days`, Mnemosyne-style; default 60 days
  preserves the previous decay curve).
- **LongMemEval benchmark** (`benchmarks/longmemeval.py`): official
  question-format loader, session seeding via the memory_eval recipe,
  per-question-type answer-hit scoring, deterministic synthetic fallback,
  factory registration, and docs. Honest scoring note: multi-hop
  aggregation questions are retrieval-unreachable and pinned at 0.
- **Sync payload encryption** (`memplex/sync_crypto.py`, opt-in via
  `MEMPLEX_SYNC_ENCRYPTION_KEY` + `memplex[sync-crypto]` extra):
  AES-256-GCM envelopes on `/sync/push` and `/sync/v1/batches`, fail-closed
  on tamper/wrong key, inert passthrough when unset. NOTE: this is
  shared-key hop/at-rest protection, not Mnemosyne-style server-blind E2E —
  the applying server holds the same key.
- **Factual capture** (`LLMEnhancer.factualize`, `llm.factual_capture`):
  retain()-style prompt pipeline resolving coreferences and normalising
  relative time to absolute dates; appended to capture content on write
  when a real LLM provider is configured. Off by default.
- **Working-memory tier** (`memplex/working_memory.py`,
  `working_memory.enabled`): TTL hot-context store (max-entries cap,
  pin/unpin) that captures typed writes and prepends live entries to agent
  recall as `[WORKING MEMORY]`. In-process and opt-in; durability paths
  unchanged.

- Mutation-testing pilot SUCCEEDED with cosmic-ray (in-place mutation):
  `memplex/sync_ingress.py` baseline is 77 killed / 33 survived / 1
  incompetent of 111 mutants (~70% kill rate, up from 68% after adding
  nested-payload / boundary / immutability tests). Most remaining survivors
  are provably equivalent mutants under JCS canonicalisation (int/float
  render identically below 2^53). Config lives in
  `[cosmic-ray]` (pyproject.toml); `scripts/mutation_pilot.sh` runs it.
  The 35 survivors are the documented test-strength backlog. Not wired into
  per-PR CI (~15 min runtime); run before releases.

- `docs/architecture.md`: module map, split-module re-export contracts,
  ordered-circular-import rules, sync lockstep ABC, and editing invariants;
  linked from README.

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

2026-08-30 quality-gates remediation:

- **Sync payload key rotation**: `sync_crypto` envelopes now carry a
  domain-separated `kid`, support `MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS`
  for rotation (kid-pinned decrypt; legacy kid-less envelopes stay
  wire-compatible), cache the derived AEAD ring keyed on the env values,
  and split `is_configured()` into a never-raising `is_enabled()` plus a
  fail-closed `_require_key()`.
- **HTTP auth observability**: failed authentications emit a structured
  warning (reason + client IP, never credentials) and increment
  `app.state.auth_failures_total`, exposed on the authenticated `/health`
  route; shared-secret mode logs a one-time startup warning that it grants
  a single all-powerful `local_development_context` (no tenant isolation),
  documented in SECURITY.md.
- **CLI ergonomics**: global `--verbose` / `MEMPLEX_DEBUG` preserves
  tracebacks (default stays one-line errors); exit codes unified
  (argparse usage = 2, runtime = 1); `stepup` help text documents it as an
  `install` alias.
- **OpenAPI hygiene**: legacy `/sync/changes|push|events` routes are
  registered with `deprecated=True`; the FastAPI app version is read from
  `importlib.metadata.version("memplex")`.
- **CI hardening**: `bench-smoke` job (deterministic 50-fact synthetic
  recall@10 smoke, `scripts/ci_bench_smoke.py`), `prometheus-config` job
  (promtool 2.53.4 `check rules`/`check config` against the new
  `deploy/prometheus/prometheus.yml` + `alertmanager.yml` examples), and a
  scheduled `mutation-nightly.yml` that re-runs the cosmic-ray pilot and
  diffs kill/survive counts against the 77/33 baseline.
- **Benchmark methodology**: shared `LatencyStats`
  (`time.perf_counter`, float ms, mean/p50/p99, warmup rounds) across all
  runners; LongMemEval scores on token-F1/EM (substring hit kept as a
  one-way diagnostic) and its loader accepts the official
  `haystack_sessions`/`answer` schema; LoCoMo loader accepts the official
  `locomo10.json` structure and persona consistency scores on token-F1.
- **Contract tests**: `test_wheel_contents.py` (wheel package-data vs
  shipped assets + real build), and a G008 coverage guard asserting
  `host_lifecycle._contract_files()` equals the evidence test's coverage
  map (previously the map covered only 14 of 21 contract files).

### Changed

- test_sync_reliability TCP-timeout test: the invariant is idempotence,
  not first-attempt timing — the first dispatch may either time out or
  deliver, the retry must deliver exactly once.

- Legacy sync registrar split (4th attempt, successful): the 458-line
  `_register_legacy_sync_routes` is now a 5-line orchestrator calling
  the extracted `_register_legacy_sync_endpoint_routes` — the parent
  complexity drops from 54 to effectively zero (the child carries the
  documented noqa).

- C901 debt paydown: 7 of the 8 exempted functions refactored below the
  complexity-25 gate — including `_validate_sync_state` (60), split into
  eleven per-collection validators on the second attempt — `create_app` (was 1247 lines/complex), `service.query`
  (33), `config.validate` (27), `_decode_pair` (26), and both ACL verifiers
  (32/36, split into per-domain helpers). The per-file exemption list
  ratcheted from 8 entries to 2 (`_validate_sync_state` 60 and
  `_probe_application_access` reduced 50 → 30 via six per-step-verified
  probe helpers (background-task CRUD, sync-v5 event, function/feedback RLS,
  vector capability, production principal); the last CRUD seam is
  SQL-order-sensitive and remains documented open debt).
- mypy strict gate expanded from 6 to 9 files (`sync_repository.py`,
  `privacy.py`, `query_explainer.py` admitted; all clean).
- Decomposed the 1247-line `create_app` monolith into four domain route
  registrars (`_register_memory_routes` / `_register_health_routes` /
  `_register_sync_routes` / `_register_metrics_routes`); `create_app` is now
  a ~190-line orchestrator.
- Complexity freeze-gate: ruff `C901` (mccabe, max-complexity 25) enabled via
  `extend-select`; the 8 existing >25 functions are pinned as per-file
  known-debt so any NEW violation fails CI.
- Architecture contract gate: `import-linter` forbidden contract "Domain and
  storage layers never import host adapters" runs in CI. Enabling it caught
  and fixed a real violation: `memplex.sync` imported
  `adapters.http_api._dataclass_to_dict`; the serializer now lives in the
  layer-neutral `memplex/serialization.py` (re-exported from
  `adapters/_shared.py` for import-path stability).
- mypy strict gate expanded from 3 to 12 files (`serialization.py`,
  `authorization.py`, `sync_ingress.py`, `sync_repository.py`,
  `privacy.py`, `query_explainer.py` admitted; all clean).

- CI `test-postgres` now runs the **full** real-PostgreSQL suites
  (`test_postgres_integration.py`, `test_postgres_backup_integration.py`,
  `test_sync_postgres_integration.py`) against the pgvector service container
  instead of a 4-test hybrid slice; the pinned list in
  `tests/test_release_workflows.py` is updated to match.
- Decomposed the 698-line `_catalog_snapshot` monolith into
  `storage/migrations/catalogue_snapshot.py` with eight domain functions
  (schema/relations, per-table entry, tables, capabilities, extensions,
  changelog sequence, sync functions, orchestrator).
- Split `adapters/agent_installer.py` (2516 → ~1694 lines):
  install-path enumeration + snapshot/rollback machinery →
  `adapters/install_transaction.py`; embedded OpenClaw extension JS and
  Hermes plugin assets → `adapters/agent_assets.py`. Both new files are
  added to the G008 host-contract digest set (`host_lifecycle._contract_files`)
  and the mutation-coverage manifest, so any byte drift still invalidates
  every host's readiness evidence.

- Real-PostgreSQL integration suite now runs in CI: a dedicated
  `test-postgres` job (Python 3.12 + `pgserver`) exercises
  `test_postgres_integration.py`, `test_postgres_backup_integration.py`, and
  `test_sync_postgres_integration.py` against a real PostgreSQL, so the
  flagship backend is no longer silently skipped in CI. Added a `pgtest`
  optional extra for the self-contained `pgserver` binary.
- Dependency upper bounds added to every runtime extra (`pyyaml<7`,
  `numpy<3`, `fastapi<1`, `openai<3`, etc.) to bound supply-chain drift; the
  lockfile remains the reproducible source of truth.
- `ruff` capped to `<0.16`: ruff 0.16 broadened default rule selection and
  reported ~1.8k pre-existing violations; the codebase is lint-clean on the
  0.15 line, which is now the locked dev version.
- `httpx` added to the `dev` extra — `starlette`'s `TestClient` requires it
  and a clean `uv sync --extra dev` was silently missing it.

- Serializer convergence: lite store `_save`/`_load` and postgres JSON
  helpers now delegate to the models' standard `to_dict`/`from_dict`
  instead of hand-written per-field serializers (postgres wrappers kept as
  thin shims for its PG-only fields).
- Removed three dead LLM config fields (`semantic_extraction`,
  `conflict_resolution`, `summarization`) from `config.py`.

2026-08-30 quality-gates remediation:

- **mypy gate 23 → 54 files** with `disallow_untyped_defs = true`
  (previously half-annotated defs passed); newly admitted: the G008
  contract cluster, `storage/pool.py`, `storage/lite/store.py`, and other
  cleaned leaf modules. import-linter now holds 3 contracts — the stale
  "SSE subscriber bridge" exemption comment is gone, `memplex.service` has
  a dedicated direct-import ban, and `memplex.product` is narrowed to its
  two declared adapter helpers.
- **Coverage floor 68 → 75** (`--cov-fail-under` now actually enforced in
  CI) and `timeout = 120` moved into pytest ini so local runs and CI share
  the per-test hard stop.
- **Extras hygiene**: `vector` is now `memplex[embedding]` + chromadb (no
  more duplicated sentence-transformers pin), and `all` finally includes
  `sync-crypto`.
- Legacy npm packages (`npm/agent-installer`, `npm/hermes-installer`,
  v0.2.0, unreferenced by any workflow) moved to `npm/archive/`; release
  publishes only `npm/memplex`.
- Docs: README calls 3.3.0 the public stable (it was already shipped and
  tagged in this changelog), reranker docs/comments now say 6-dimensional
  (the `confidence` weight existed in code), and the Unreleased section's
  duplicated type headings were consolidated without dropping entries.
- **mypy gate 54 → 124 files (~92% of the package)**: nearly all remaining
  modules admitted (worker, release, product, sync_protocol, sync_dispatcher,
  storage/postgres{,_sync,_backup}, feedback, lite/durability, host_lifecycle,
  readiness_evidence, logging_config, query_pipeline, ...), with
  `types-PyYAML` added to the dev extra. `follow_imports="silent"` was tried
  and reverted with a recorded reason (numpy 2.x stubs use 3.12-only `type`
  statements under `python_version = "3.11"`). The import-linter forbidden
  contract now covers every adapter-free top-level module (+16).

### Reverted

- Mutation-testing pilot (mutmut on `sync_ingress`) attempted and withdrawn:
  all 28 generated mutants spuriously survived (activation never took effect
  in the sandbox, most likely a mutmut-3.7 pytest-plugin interaction). A gate
  that green-lies is worse than no gate; not shipping it.

### Security

- Request-time loopback enforcement: `_require_auth` now refuses
  unauthenticated requests from non-loopback peers even when the startup
  bind guard was bypassed (defense-in-depth via `_is_remote_peer`).

### Refactor

2026-08-30 quality-gates remediation:

- **Ordered circular imports eliminated**: the 25 shared constants and
  dataclasses borrowed by the migrations sub-modules now live in a new
  dependency-free `memplex/storage/migrations/_constants.py`; `runner.py`
  re-exports them for import-path stability, and the cluster no longer
  depends on definition-before-re-export lexical ordering.
- **Schema-fingerprint variants structured**: variant classification now
  produces a `SchemaVariantFeatures` dataclass (layout / edge-integrity /
  reliable-sync / background-tasks / vector-dim); the string name is a
  display-only rendering, replacing the string-splicing construction and
  `endswith` reverse-parsing.
- **Shared backend error messages**: duplicated user-facing error strings
  across the lite and postgres backends (8 groups, 38 sites) now come from
  `memplex/storage/_messages.py`.
- **Legacy sync registrar decomposed**: the 465-line
  `_register_legacy_sync_endpoint_routes` (C901 noqa) is a thin
  orchestrator over per-endpoint helpers; the noqa marker is removed.
- Repo hygiene: `artifacts/` and `_tmp_home/` gitignored, the stray
  `postgresql:/` pgserver leftover directory removed, `task-7-report.md`
  moved under `docs/reports/`, and `.git/info/exclude` merged into
  `.gitignore`.
- **Query pipeline extracted from `MemplexService`**: the six-stage read
  path (`query()` plus `_parallel_scope_search` / `_apply_token_budget` /
  `_filter_by_owner`) now lives in `memplex/query_pipeline.py`
  (`QueryPipeline`, dependency-injected, fully annotated, mypy-gated);
  `service.py` shrank 2574 → ~2280 lines and `query()` keeps its exact
  public contract as a thin delegate. The SSE subscriber-count wiring was
  formalized into a public `register_sse_subscriber_count_provider()` —
  `http_api` no longer writes a service-private module global.
- **HTTP route registrars decomposed**: `_register_sync_v1_routes` (220
  lines, C901 noqa) and `_register_memory_routes` (239 lines, complexity
  23 — two under the freeze line) are now thin orchestrators over
  per-endpoint helpers. The only remaining `noqa: C901` in the package is
  the documented `pool._probe_application_access`.
- Extracted the authorization / ACL responsibility out of `MemplexService`
  into a new `memplex/authorization.py` module (`AuthorizationGate` +
  `_TypedNodeLookup`). All tenancy / workspace / user / session visibility
  logic (`_require_authorization`, `_is_node_visible`,
  `_filter_authorized_results`, and friends) now lives in one cohesive,
  independently-tested collaborator; the service keeps thin delegating
  wrappers for API stability. `service.py` shrank from 2311 to ~2148 lines.
- Closed the Lite/PostgreSQL sync-repository lockstep hazard: both backends
  now inherit a shared `AbstractSyncRepository` ABC (`memplex/sync_repository.py`)
  whose 17 abstract methods are enforced at instantiation, so the two can no
  longer silently drift. A contract test pins the shared method set in CI.
- Split the 2615-line `memplex/storage/pool.py`: the
  `PostgresStorageResources` / `PostgresSyncStorageResources` classes moved to
  a new `memplex/storage/postgres_resources.py` (re-exported from `pool` for
  import-path and monkeypatch stability). `pool.py` is now ~1774 lines.
  Monkeypatched symbols (`PostgresPoolManager`, `_new_migration_runner`) are
  resolved through the live `pool` module so the existing test patches keep
  working unchanged.
- Split the 3815-line `memplex/storage/migrations/runner.py` into three cohesive
  sub-modules, all re-exported from `runner` for import-path and monkeypatch
  stability: the pure catalogue-verification cluster (46 helpers + 11 schema
  constants + `_normalise_sql`) → `catalogue_checks.py`; the ACL-contract
  verifiers (`_verify_application_acl` / `_verify_ingress_acl` /
  `_verify_acl_contracts`) → `acl_verification.py`; the observed-state ledger
  functions (`_read_ledger_if_present` / `_validate_ledger` /
  `_plan_from_observed_state` / `_validate_legacy_belongs_to_edges`) →
  `ledger_state.py`. `runner.py` is now ~2008 lines (was 3815). The sub-modules
  borrow schema constants/data classes from `runner` via ordered circular
  imports (resolved because `runner` defines them before its end-of-file
  re-exports); the test suite's `monkeypatch.setattr(runner, ...)` patches keep
  working because `PostgresMigrationRunner` resolves bare names against the
  `runner` module global at call time.
- Extracted injection-scan state and the read-side injection filter out of
  `MemplexService` into `memplex/llm/injection_guard.py`
  (`InjectionScanCounter`, `drop_injection_suspected`), reducing the service's
  responsibilities. Six fail-soft `except ...: pass` sites now log at debug
  instead of swallowing silently.

## [3.3.2] - 2026-09-04

### Fixed

- The npm wrapper package declares its `repository` field, which the
  npm registry requires to match the provenance source repository when
  publishing with `--provenance`.

## [3.3.1] - 2026-09-04

### Fixed

- Release pipeline first-flight fixes: the offline release builder now
  loads `memplex/release.py` by path (the package `__init__` pulled the
  runtime dependency set into the network-disabled build venv), the
  release bundle stages outside the repository's tracked `release/`
  directory, artifact downloads merge into the target path, and the
  pinned `gh-action-pypi-publish` moved to v1.14.2 whose container
  image still exists.
- G008 real-host lifecycle proof runs against the published wheel:
  pytest's symlinked `*current` fixture pointers fold into the basetemp
  digest, agent-identity `source_root` assertions compare against the
  installed package root, and the four-host status checks read the CLI's
  nested install report.

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
