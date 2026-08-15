# ADR-002: Split modules via end-of-file re-exports + ordered circular imports

**Status**: Accepted (2026-08)

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
For `runner.py`'s four sub-modules, an ordered circular import works
because the constants are defined before the end-of-file re-export block.

## Consequences
- All import paths and monkeypatches unchanged (verified: 3203 lite +
  728 PG zero regression across four split waves).
- The re-export blocks' **position** is load-bearing — moving a constant
  definition below them breaks the import silently. This is documented
  in `docs/architecture.md` and enforced by convention, not by machine.
- Remaining debt: `_register_legacy_sync_routes` (54) and
  `_probe_application_access` (30) carry per-function `# noqa: C901`.
