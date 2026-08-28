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
    catalogue_snapshot.py  Whole-catalogue snapshot reader (8 domain fns) ³
    catalogue_checks.py    Pure schema-verification helpers ³
    acl_verification.py    Least-privilege ACL contract verifiers ³
    ledger_state.py        Observed-state ledger + plan-from-state ³
sync_repository.py   SyncRepository Protocol + AbstractSyncRepository ABC
sync.py / sync_protocol.py / sync_dispatcher.py / sync_ingress.py
product.py           Evidence-gated readiness (G002–G009), fail-closed
host_lifecycle.py    G008 host-contract digests (see below)
```

`¹ ² ³ ⁴` mark the split groups (⁴ = post-S-wave leaf modules) described under [Split modules](#split-modules).

## Split modules and their re-export contracts

Several large files were split into cohesive sub-modules. **Every split
keeps the original import path working** through an end-of-file re-export in
the parent module. External code and tests must keep importing from the
parent (`from memplex.storage.migrations.runner import _matches_post_core`
still works).

### 1. Service collaborators (`authorization.py`, `llm/injection_guard.py`)

`MemplexService` delegates authorization and injection-scan state to
single-purpose collaborators and keeps thin one-line wrappers for API
stability. The gate resolves stores lazily via providers so tests that
monkeypatch `service.store` are honoured.

### 2. Storage resources (`storage/postgres_resources.py`)

`PostgresStorageResources` / `PostgresSyncStorageResources` moved out of
`pool.py`. The test suite patches `pool.PostgresPoolManager` and
`pool._new_migration_runner`; the moved code therefore resolves those names
through the **live pool module** (`import ... pool as _pool`, then
`_pool.X` at call time). Never convert those to direct `from pool import X`
bindings — that would silently break every test patch.

### 3. Migration clusters (`storage/migrations/*`)

`runner.py` (was 3815 lines) now delegates to four sub-modules:

- `catalogue_snapshot.py` — `_catalog_snapshot` decomposed into
  `_read_schema_and_relations` / `_snapshot_table(s)` /
  `_read_capabilities` / `_read_extensions` / `_read_changelog_sequence` /
  `_read_sync_functions` + a thin orchestrator.
- `catalogue_checks.py` — 46 pure verification helpers + schema constants.
- `acl_verification.py` — the three ACL contract verifiers.
- `ledger_state.py` — ledger read/validate/plan functions.

These use **ordered circular imports**: the sub-modules borrow data classes
and schema constants from `runner`, which works only because `runner`
defines those names *before* its end-of-file re-imports of the sub-modules.
The re-export blocks therefore must stay at their current position — moving
a constant definition below the first `from ...catalogue_checks import`
line breaks the import silently.

### 4. Agent installer (`adapters/install_transaction.py`, `adapters/agent_assets.py`)

Install-path/rollback machinery and embedded plugin assets moved out of
`agent_installer.py`. `_package_version` / `_target_dir` /
`_managed_identity_payload` stay in `agent_installer` and are imported
**lazily inside functions** in the new modules to keep loading
one-directional. All three files participate in the G008 host-contract
digests via `host_lifecycle._contract_files().shared_runtime`; any byte
drift in any of them invalidates every host's readiness evidence. When
adding files to this cluster, update **both** `_contract_files` and the
mutation-coverage manifest in `tests/test_host_lifecycle_evidence.py`.

## Machine-enforced gates (CI)

- **Hexagonal contract** (`import-linter`, `lint-imports` in CI): the domain
  and storage layers listed in `pyproject.toml [tool.importlinter]` may never
  import `memplex.adapters`. `memplex.serialization.py` exists so shared
  serializers do not force domain→adapter imports.
- **Complexity freeze** (ruff `C901`, max 25): any function whose complexity
  exceeds 25 fails CI. Known-debt hot spots carry inline `# noqa: C901`
  markers in source (see the legacy sync-route registrars in
  `adapters/http_api.py` and the ACL access probe in `storage/pool.py`);
  adding a new >25-complexity function — or a new noqa marker without the
  "documented known debt" justification — fails review.
- **Typed boundary** (mypy): the file list in `[tool.mypy] files` is itself
  pinned by `tests/test_release_workflows.py` — extend both together.

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
