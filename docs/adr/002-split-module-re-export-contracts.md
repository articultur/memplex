# ADR-002: Split modules via end-of-file re-exports

**Status**: Accepted (2026-08); amended 2026-08 (migrations cluster now
imports shared names from `_constants.py` instead of ordered circular
imports)

## Context
Monolith files (runner.py 3815, pool.py 2615, agent_installer 2516)
needed decomposition without breaking the test suite's ~15 monkeypatch
sites or any external import path.

## Decision
Extract cohesive clusters to new modules; re-export from the parent at
**end-of-file**. The test suite patches `pool.PostgresPoolManager` /
`pool._new_migration_runner` at module scope — the moved code therefore
resolves those names through the **live pool module** (`import pool as
_pool` + `_pool.X`), never via direct `from pool import X` bindings.
For `runner.py`'s four sub-modules, the shared schema constants and data
classes live in `storage/migrations/_constants.py` (stdlib-only, no
internal imports); every cluster module imports them from there, so no
import order is load-bearing. `runner.py` binds the same names via its own
top-level `_constants` import, keeping every `from ...runner import X`
path and `runner.X` monkeypatch resolving.

## Consequences
- All import paths and monkeypatches unchanged (verified: 3203 lite +
  728 PG zero regression across four split waves).
- The migrations cluster has no circularity left: `_constants.py` is the
  single source of shared names. The end-of-file re-export blocks in
  `runner.py` must still stay below the code that references the
  re-exported bare names.
- Remaining debt: `_register_legacy_sync_routes` (54) and
  `_probe_application_access` (30) carry per-function `# noqa: C901`.
