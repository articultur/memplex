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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **memplex** (10788 symbols, 27826 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/memplex/context` | Codebase overview, check index freshness |
| `gitnexus://repo/memplex/clusters` | All functional areas |
| `gitnexus://repo/memplex/processes` | All execution flows |
| `gitnexus://repo/memplex/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
