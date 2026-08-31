# G004 Task 7 CI pin and acceptance report

Status: **DONE**

Date: 2026-08-30 (Asia/Shanghai)

## Scope

- Modified only `.github/workflows/ci.yml` and `tests/test_release_workflows.py`.
- Added this acceptance report as `task-7-report.md`.
- Preserved all unrelated working-tree changes.
- Did not enable repository-level GitHub Actions, call a mutating `gh` command, stage, commit, or push.

## TDD evidence

1. Updated the release-workflow contract first to require:
   - all four local G004 test modules in the Lite CI job;
   - `--ignore=tests/test_g004_postgres_backup_real_value.py` in ordinary Lite collection;
   - `tests/test_g004_postgres_backup_real_value.py` in the existing real-PostgreSQL job.
2. RED, before editing CI:
   - `.venv/bin/python -m pytest tests/test_release_workflows.py -q`
   - Result: `1 failed, 16 passed`; the failure was the missing first local G004 module in CI.
3. GREEN, after the minimal workflow edit:
   - `.venv/bin/python -m pytest tests/test_release_workflows.py -q`
   - Result: `17 passed in 0.11s`.

## Workflow change

- The existing Lite test step now explicitly runs:
  - `tests/test_g004_cli_runner_contract.py`
  - `tests/test_g004_lite_real_value.py`
  - `tests/test_g004_agent_real_value.py`
  - `tests/test_g004_sync_real_loopback.py`
- The same Lite job still runs the full `tests/` suite, now explicitly excluding only `tests/test_g004_postgres_backup_real_value.py`.
- The existing `test-postgres` job explicitly adds `tests/test_g004_postgres_backup_real_value.py` while retaining `MEMPLEX_REQUIRE_PGVECTOR: "1"`.
- Job names, matrices, permissions, services, runner labels, timeouts, and existing PostgreSQL test files were not changed.

## Verification

### Focused tests

- Local G004 command from the task brief: `46 passed in 7.00s`; `0 failed`, `0 skipped`.
- Dedicated G004 PostgreSQL file through external-DSN pgserver orchestration: `3 collected, 3 passed in 2.36s`; `0 failed`, `0 skipped`.

### Repository static gates

- `uv lock --check`: passed; `Resolved 171 packages in 32ms`.
- `.venv/bin/ruff check memplex tests`: passed; `All checks passed!`.
- `.venv/bin/lint-imports`: passed; `132 files`, `356 dependencies`, `1 kept`, `0 broken`.
- `.venv/bin/mypy`: passed; `Success: no issues found in 23 source files`.

### Full Lite suite

Command used the required Node path:

```bash
PATH=/opt/homebrew/Cellar/node@24/24.19.0/bin:$PATH \
  .venv/bin/python -m pytest tests \
  --ignore=tests/test_g004_postgres_backup_real_value.py \
  -q --cov=memplex --cov-fail-under=68
```

Result:

- Collected: `2996 tests` (plus `2` collection-time skips reported in the execution summary).
- Passed: `2978`.
- Skipped: `20` total.
- Failed: `0`.
- Subtests passed: `236`.
- Coverage: `80.30%` (required `68%`).
- Warnings: `119`.
- Duration: `187.16s`.

### Real PostgreSQL suite

The exact six AGENTS.md PostgreSQL files plus the G004 PostgreSQL file ran against `.venv-pgcheck`'s real pgserver and bundled pgvector. A temporary `/tmp/ci_pg_fidelity.py` wrapper injected the external DSN and PostgreSQL client-tool PATH; it was deleted after verification.

- Collected: `418`.
- Passed: `415`.
- Skipped: `3`.
- Failed: `0`.
- Duration: `88.25s`.
- `MEMPLEX_REQUIRE_PGVECTOR=1` was mandatory throughout the run.

## Change-scope review

- `git diff --check -- .github/workflows/ci.yml tests/test_release_workflows.py`: passed.
- GitNexus pre-edit impact for the modified contract test: LOW risk, `0` direct dependants and `0` affected processes.
- GitNexus `detect-changes`: LOW risk and `0` affected processes; its 11-file worktree view also included unrelated pre-existing user changes, which were not modified by this task.
- Final implementation diff is limited to the two requested CI contract files; this report is the only added deliverable.
