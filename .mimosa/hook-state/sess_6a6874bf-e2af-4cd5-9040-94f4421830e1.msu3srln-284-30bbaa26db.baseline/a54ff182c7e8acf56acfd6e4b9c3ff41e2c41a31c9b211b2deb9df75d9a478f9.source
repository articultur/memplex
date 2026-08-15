# Memplex — Repo Agent Instructions

## Non-negotiable gates (run before claiming any refactor/change is done)

1. `uv lock --check` — lockfile must match pyproject.
2. `.venv/bin/ruff check memplex tests` — lint gate incl. C901 complexity freeze
2b. `.venv/bin/lint-imports` — hexagonal architecture contract
2c. `.venv/bin/mypy` — typed-boundary gate (12 files, pinned) (ruff is pinned `<0.16`; do not bump without fixing the ~1.8k 0.16-rule violations first).
3. Full lite suite: `.venv/bin/python -m pytest tests -q --cov=memplex --cov-fail-under=68`
   (~3,100 tests, ~7 min; suite count grows — always report the real number).
4. Real-PostgreSQL gate for any `storage/`, `sync*`, or migrations change —
   CI-fidelity run (external DSN + mandatory pgvector):
   `python /tmp/ci_pg_fidelity.py` pattern — i.e. `MEMPLEX_TEST_POSTGRES_DSN=<uri> MEMPLEX_REQUIRE_PGVECTOR=1 pytest tests/test_postgres_integration.py tests/test_postgres_backup_integration.py tests/test_sync_postgres_integration.py tests/test_sync_repository_contract.py tests/test_g014_postgres_task_repository.py tests/test_ci_postgres_contract.py` (~730 tests via pgserver).
   Zero regressions in BOTH suites is the acceptance bar.

## Architecture invariants (see docs/architecture.md for the full map)

- **Ordered circular imports**: `storage/migrations/{catalogue_checks,acl_verification,ledger_state,catalogue_snapshot}.py` borrow constants/dataclasses from `runner.py`. This only works because `runner.py` defines them BEFORE its end-of-file re-export imports. Never move a constant definition below those import blocks.
- **Live-module routing**: `storage/postgres_resources.py` must resolve `PostgresPoolManager` / `_new_migration_runner` via `_pool.X` attribute access (not `from pool import X`) — the test suite monkeypatches `pool.*` and direct imports would silently break ~15 patches.
- **G008 contract file set**: `memplex/adapters/{agent_installer,install_transaction,agent_assets,agent_runtime,managed_identity,runtime_status,_shared}.py` are hashed into every host's readiness proof. Adding/removing/renaming any file in this cluster requires updating BOTH `host_lifecycle._contract_files()` AND the mutation manifest in `tests/test_host_lifecycle_evidence.py`.
- **Sync lockstep**: both sync repositories inherit `AbstractSyncRepository` (17 abstract methods). Adding a sync operation = add it to the ABC + Protocol + both backends + `tests/test_sync_repository_contract.py`.
- **Pinned workflows**: `tests/test_release_workflows.py` pins the CI job shapes and the PG test file list. Changing `.github/workflows/ci.yml` requires updating that pin in the same commit.

## Style & workflow

- Commits: Conventional Commits (`feat:` / `fix:` / `test:` / `chore:` / `docs:`), English subject lines. User communicates in Chinese — replies in Chinese are fine.
- Architecture doc `docs/architecture.md` is the module map; update it when module boundaries move.
- Honesty bar: report real test counts and failures; never claim completion from a subset of gates. Evidence (command output) over assertion.
- Do not commit unless the user explicitly asks; when asked to push, just commit+push (no surprise subagents/evaluations).
