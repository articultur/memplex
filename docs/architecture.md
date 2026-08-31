# Architecture

Memplex is a multi-agent long-term memory layer: recall before a turn,
capture after the turn, compact old context. This document is the module map
for maintainers — what lives where, which boundaries are load-bearing, and
the import contracts that must not be broken silently.

## Layer map

```
adapters/            Host + transport boundary (one port per agent platform)
  agent_installer.py   Install/uninstall orchestrator + per-host installers
  install_transaction.py  Path enumeration + snapshot/rollback machinery ¹
  agent_assets.py      Embedded OpenClaw extension JS + Hermes plugin ¹
  cli.py / http_api.py / mcp_server.py   Human / HTTP / MCP transports
  codex_plugin / claude_skill / openclaw_plugin / hermes_memory_provider
  agent_runtime.py     Shared recall/capture runtime used by every host
service.py           MemplexService: orchestration facade over collaborators
query_pipeline.py    QueryPipeline: 6-stage read-side query execution (service delegate)
authorization.py     AuthorizationGate: tenant/workspace/user/session ACL ¹
serialization.py     Layer-neutral dataclass→JSON serializer (leaf) ¹
temporal.py          Bi-temporal fact validity (supersede/as_of) ⁴
improve.py           Proactive fact maintenance (dedupe/expire/reindex) ⁴
sleep_time.py        Idle-time maintenance + inference precompute daemon ⁴
working_memory.py    TTL hot-context tier (per-tenant scoped) ⁴
sync_crypto.py       Shared-key AES-GCM sync payload encryption ⁴
llm/
  injection_guard.py  InjectionScanCounter + drop_injection_suspected ¹
storage/
  base.py             MemoryStore interface
  lite/               Development JSON-pair backend — in-memory model + journaled JSON persistence, with a SQLite FTS5 sidecar for search (store, durability, sync_repository); production must use postgres
  postgres.py         PostgreSQL business store (request-scoped ACL facade)
  postgres_sync.py    PostgreSQL sync repository
  postgres_backup.py  Backup/restore
  pool.py             Connection pools + ReadyPostgresPool seal ²
  postgres_resources.py  Service-owned storage resources ²
  migrations/
    runner.py           Migration plan/apply + PostgresMigrationRunner ³
    _constants.py       Shared schema constants + migration data classes ³
    catalogue_snapshot.py  Whole-catalogue snapshot reader (8 domain fns) ³
    catalogue_checks.py    Pure schema-verification helpers ³
    acl_verification.py    Least-privilege ACL contract verifiers ³
    ledger_state.py        Observed-state ledger + plan-from-state ³
sync_repository.py   SyncRepository Protocol + AbstractSyncRepository ABC
sync.py / sync_protocol.py / sync_dispatcher.py / sync_ingress.py
product.py           Evidence-gated readiness (G002–G009), fail-closed
host_lifecycle.py    G008 host-contract digests (see below)
```

`¹ ² ³ ⁴` mark the split groups (⁴ = post-S-wave leaf modules) described under [Split modules](#split-modules-and-their-re-export-contracts).

### Top-level quick reference

One line per top-level module (`ls memplex/*.py memplex/*/`); the layer map
above stays canonical for the split-group annotations:

```
__init__.py           Package root: CoreEngine/MemplexService re-exports + CLI main shim
__main__.py           `python -m memplex` entry point
adapters/             Host + transport boundary (detailed map above)
auth.py               Authenticated identity primitives for the service boundary
authorization.py      AuthorizationGate: tenant/workspace/user/session ACL ¹
backup.py             Strict backup manifests + disaster-recovery data contracts
capacity_chaos.py     G009 capacity/soak/chaos signed machine evidence
compaction.py         CompactionPipeline: 5-stage memory compression
config.py             Configuration load/validate (MEMPLEX_* env > config.yaml > defaults)
core/                 Pure computation layer (CoreEngine, extractors, hooks)
host_lifecycle.py     G008 host-contract digests (detailed below)
improve.py            Proactive fact maintenance (dedupe/expire/reindex) ⁴
intent.py             Memory-type + query-scope intent heuristics (pure, dependency-free)
llm/                  LLM provider layer (providers, fallback chain, enhancer, injection_guard ¹)
logging_config.py     Structured logging configuration
models/               Typed data models (memory, graph, search, task, feedback)
operations.py         Operations status + signed SLO evidence
operations_assets/    Packaged G006 operator assets (admin console, alert rules)
_plugin/              Packaged host-plugin assets (hooks, scripts, skills)
privacy.py            Privacy helpers shared across write paths
processing/           Association/merging/graph-building pipeline
product.py            Evidence-gated readiness (G002–G009), fail-closed
query_explainer.py    Product-facing retrieval trace translator
query_pipeline.py     QueryPipeline: 6-stage read-side query path delegated from service.query
readiness_evidence.py G003/G004 fail-closed signed deployment evidence
release.py            Fail-closed release metadata + artifact contracts
retrieval/            Search and ranking (embedding, multi-path, reranker, dedup)
serialization.py      Layer-neutral dataclass→JSON serializer (leaf) ¹
service.py            MemplexService orchestration facade
sleep_time.py         Idle-time maintenance + inference precompute daemon ⁴
storage/              MemoryStore interface + lite/postgres backends + migrations (map above)
sync.py               Local-cache + remote push/pull multi-node sharing
sync_crypto.py        Shared-key AES-GCM sync payload encryption ⁴
sync_dispatcher.py    Bounded dispatcher for durable sync deliveries
sync_ingress.py       Trusted ingress gateway freezing protocol bytes pre-DB
sync_protocol.py      G004 v1 pure data protocol + canonical codec
sync_repository.py    SyncRepository Protocol + AbstractSyncRepository ABC
task_repository.py    Durable background-task repository contract
temporal.py           Bi-temporal fact validity (supersede/as_of) ⁴
wiki/                 Wiki layer: compile, generate, search, lint
worker.py             BackgroundWorker async task processor
working_memory.py     TTL hot-context tier (per-tenant scoped) ⁴
```

## Mechanism map

[Capability mechanisms](capability-mechanisms.md) is the canonical map from
user-visible capabilities to implementation boundaries, focused tests, and
honest limitations. [`capabilities.json`](capabilities.json) provides the same
stable capability IDs and line-ranged evidence for machines.

| Area | Canonical mechanism |
|---|---|
| Model and write/read loop | [Typed model](capability-mechanisms.md#typed-memory-model), [capture](capability-mechanisms.md#capture-write-path), [recall](capability-mechanisms.md#recall-retrieval-path) |
| Time, graph, and access | [Bi-temporal facts](capability-mechanisms.md#temporal-facts), [bounded one-hop expansion](capability-mechanisms.md#bounded-graph-expansion), [principal/tenant authorization](capability-mechanisms.md#principal-tenant-authorization) |
| Durability and operations | [Durable versus legacy sync](capability-mechanisms.md#sync-convergence), [backup/restore](capability-mechanisms.md#backup-restore), [operations](capability-mechanisms.md#operations-observability) |
| Delivery surfaces | [Reproducible supply chain](capability-mechanisms.md#reproducible-supply-chain), [four-host lifecycle](capability-mechanisms.md#four-host-lifecycle) |

## Split modules and their re-export contracts

Several large files were split into cohesive sub-modules. **Every split
keeps the original import path working** through an end-of-file re-export in
the parent module. External code and tests must keep importing from the
parent (`from memplex.storage.migrations.runner import _matches_post_core`
still works).

### 1. Service collaborators (`authorization.py`, `llm/injection_guard.py`, `query_pipeline.py`)

`MemplexService` delegates authorization and injection-scan state to
single-purpose collaborators and keeps thin one-line wrappers for API
stability. The gate resolves stores lazily via providers so tests that
monkeypatch `service.store` are honoured.

`service.query()` itself delegates the six-stage read path to
`query_pipeline.QueryPipeline`: the service resolves the request-scoped
authorization context and store, then builds a fresh pipeline per call from
its **current** attributes — tests that monkeypatch `service._detect_scope`
or `service._retriever` keep working unchanged.

Adapters report the SSE subscriber count through the public
`memplex.service.register_sse_subscriber_count_provider(fn)` registration
point (never by writing the private module global); the health surface
fails closed to `0` when no provider is registered or the provider raises.

### 2. Storage resources (`storage/postgres_resources.py`)

`PostgresStorageResources` / `PostgresSyncStorageResources` moved out of
`pool.py`. The test suite patches `pool.PostgresPoolManager` and
`pool._new_migration_runner`; the moved code therefore resolves those names
through the **live pool module** (`import ... pool as _pool`, then
`_pool.X` at call time). Never convert those to direct `from pool import X`
bindings — that would silently break every test patch.

### 3. Migration clusters (`storage/migrations/*`)

`runner.py` (was 3815 lines) now delegates to four sub-modules plus one
shared constants module:

- `_constants.py` — every schema constant, the application ACL matrix, and
  the migration data classes (`Migration`, `MigrationPlan`,
  `SchemaFingerprint`, `SchemaVariantFeatures`, ACL contracts,
  `_LedgerEntry`). Stdlib-only, no internal imports.
- `catalogue_snapshot.py` — `_catalog_snapshot` decomposed into
  `_read_schema_and_relations` / `_snapshot_table(s)` /
  `_read_capabilities` / `_read_extensions` / `_read_changelog_sequence` /
  `_read_sync_functions` + a thin orchestrator.
- `catalogue_checks.py` — 48 pure verification helpers.
- `acl_verification.py` — the three ACL contract verifiers.
- `ledger_state.py` — ledger read/validate/plan functions.

All shared names live in `_constants.py` and every cluster module imports
them from there, so **no import order is load-bearing** any more.
`runner.py` still re-exports the split-out functions (and binds the shared
names via its own top-level `_constants` import) so existing
`from ...runner import X` paths and `runner.X` monkeypatches keep
resolving; the re-export blocks must stay below the code that references
the re-exported bare names. `SchemaFingerprint.features` carries the
structured variant classification (`SchemaVariantFeatures`); the variant
string is a derived display name consumed by status output, digests, and
legacy adoption-baseline mapping only. The business-pool readiness probe
(`storage/pool.py`) derives its privilege matrix from
`_constants._APPLICATION_ACL` — the single source of truth for the
application role's least-privilege grants.

### 4. Shared G008 adapter runtime

The shared-runtime digest is the exact seven-file set
`adapters/{agent_installer,install_transaction,agent_assets,agent_runtime,managed_identity,runtime_status,_shared}.py`.
Every file is included in every host's G008 contract digest via
`host_lifecycle._contract_files()`; any byte drift in any one invalidates all
four host proofs. Install-path/rollback machinery and embedded plugin assets
live in `install_transaction.py` and `agent_assets.py`, while installer-owned
helpers are imported lazily to keep loading one-directional. Adding, removing,
or renaming a file in this seven-file cluster requires updating **both**
`host_lifecycle._contract_files()` and the mutation manifest in
`tests/test_host_lifecycle_evidence.py`.

## Machine-enforced gates (CI)

- **Hexagonal contract** (`import-linter`, `lint-imports` in CI): the domain
  and storage layers listed in `pyproject.toml [tool.importlinter]` may never
  import `memplex.adapters`. `memplex.serialization.py` exists so shared
  serializers do not force domain→adapter imports.
- **Complexity freeze** (ruff `C901`, max 25): any function whose complexity
  exceeds 25 fails CI. The one remaining known-debt hot spot carries an
  inline `# noqa: C901  documented known debt` marker (the ACL access probe
  in `storage/pool.py`); adding a new >25-complexity function — or a new
  noqa marker without the "documented known debt" justification — fails
  review.
- **Typed boundary** (mypy): the file list in `[tool.mypy] files` is itself
  pinned by `tests/test_release_workflows.py` — extend both together.

## Known oversized files and split roadmap

Size debt is tracked explicitly here so it stays visible between review
waves. Line counts are `wc -l` at the time of writing; re-measure before
quoting them in a review.

| File | Lines | Status / next slice |
|---|---|---|
| `adapters/cli.py` | ~2375 | Largest remaining adapter. Candidate: per-command-group registrars (same pattern as the http_api wave-3/4 registrar splits). |
| `adapters/http_api.py` | ~2370 | Route registrars are now one helper per endpoint (`_register_memory_*_route`, `_register_sync_v1_*_route`, legacy sync pair); remaining bulk is endpoint bodies. Next slice: extract the legacy sync push/changes payloads (`_legacy_sync_v1_push` ≈ complexity 20). |
| `service.py` | ~2275 | Query path extracted to `query_pipeline.py` (6-stage `QueryPipeline`, ~440 lines); `query()` is now a thin delegate. Next slices by independence: sync lifecycle block (`sync_status`/`drain_sync`/`pull_sync`), then the health/status block (`health`/`runtime_status`/`operations_metrics_status`/`readiness_status`/`_sync_health`). |
| `storage/lite/store.py` | ~2204 | Lite backend monolith. Candidate: split the FTS5 search-index sidecar and the COW/journal durability machinery out of the store class. |
| `storage/postgres.py` | ~2158 | Postgres business store. Candidate: split the request-scoped ACL facade from the pool-backed CRUD core. |
| `storage/pool.py` | ~1896 | Connection pools + readiness seal; holds the last `noqa: C901` (`_probe_application_access`). Candidate: move the readiness probe/ACL matrix verification into its own module reading `_constants._APPLICATION_ACL`. |

Split priority: `cli.py` and `service.py` first (both sit on the
user-facing orchestration path and accrete per-feature methods fastest),
then the two storage monoliths, then `pool.py`. Every split must keep the
original import paths working (re-export contract, see above) and pass the
full lite + real-PostgreSQL gates with zero test edits.

## Sync repository lockstep contract

`LiteSyncRepository` and `PostgresSyncRepository` must expose the same 17
atomic sync operations. Both inherit `AbstractSyncRepository`
(`sync_repository.py`), so dropping or renaming a method fails at
instantiation. `tests/test_sync_repository_contract.py` pins the method set
and signature equality against the `SyncRepository` Protocol in CI.

## Invariants worth knowing before editing

- **Fail-closed authorization**: identity-less nodes are visible only via
  the exact local-development context; unknown visibility is invisible;
  `MemoryNotFoundError` makes "no access" indistinguishable from "absent".
- **Token discipline**: the registry stores SHA-256 digests only; raw
  `MEMPLEX_PRINCIPAL_TOKEN` is hashed once and never logged or persisted.
- **Unauthenticated HTTP is loopback-only** (`_is_loopback_peer`), refused
  when proxy headers are present, and fail-closed outside development.
- **Evidence gating**: `readiness --strict` reports `ready/industrial` only
  with valid HMAC-signed, version-bound, ≤15-minute-old external evidence
  per gate. No evidence ⇒ `blocked`, never a downgrade to warning.
- **Reproducible builds**: release builds twice and byte-compares; any
  nondeterminism (paths, umask, mtimes) is a release blocker.

## Testing topology

- `tests/` (lite suite): CI matrix ubuntu/macos × py3.11–3.13, coverage
  gate ≥68% (actual ~80%).
- Real-PostgreSQL suites (`test_postgres_integration.py`,
  `test_postgres_backup_integration.py`, `test_sync_postgres_integration.py`)
  run against a real database: locally via the self-contained `pgserver`
  (`uv sync --extra pgtest`), in CI via the `test-postgres` job's pgvector
  service container. The CI job deliberately runs a curated contract slice
  (see `tests/test_release_workflows.py` for the pinned list); the deep
  387-test suite is the pre-release local gate.
