# Contributing to Memplex

Thank you for improving Memplex. Start with the [canonical real-value CLI guide](docs/guides/real-value-cli.md)
when a change affects user-facing command examples or evidence claims.

## Workflow

Open a focused change, preserve unrelated worktree edits, and add or update tests before changing
behavior. Keep module boundaries consistent with `docs/architecture.md`. Use Conventional Commit
subjects only when a repository owner explicitly asks for a commit. Contributor automation
does not commit, stage, or push by default.

## Required gates

Run these non-negotiable repository gates before claiming a refactor or change is complete:

1. `uv lock --check` — lockfile must match pyproject.
2. `.venv/bin/ruff check memplex tests` — lint gate incl. C901 complexity freeze
2b. `.venv/bin/lint-imports` — hexagonal architecture contract
2c. `.venv/bin/mypy` — typed-boundary gate (file list pinned in `pyproject.toml [tool.mypy]` — that list is the authoritative count; `tests/test_release_workflows.py` pins it against drift) (ruff is pinned `<0.16`; do not bump without fixing the ~1.8k 0.16-rule violations first).
3. Full lite suite: `.venv/bin/python -m pytest tests -q --cov=memplex --cov-fail-under=68`
   The suite count is dynamic; report the actual dynamically selected test count from that run.
4. Real-PostgreSQL gate for any `storage/`, `sync*`, or migrations change —
   load `MEMPLEX_TEST_POSTGRES_DSN` from an approved secret source, then run the mandatory pgvector
   CI-fidelity selection:
   `MEMPLEX_REQUIRE_PGVECTOR=1 pytest tests/test_postgres_integration.py tests/test_postgres_backup_integration.py tests/test_sync_postgres_integration.py tests/test_sync_repository_contract.py tests/test_g014_postgres_task_repository.py tests/test_ci_postgres_contract.py`
   Zero regressions in BOTH suites is the acceptance bar.

Report the actual selected, passed, skipped, xfailed, and failed counts. A focused passing test is
evidence for its scope, not a substitute for a required full gate.

For Task 6 documentation work, run these focused checks while iterating:

```text
.venv/bin/python -m pytest tests/test_g004_cli_runner_contract.py -q
.venv/bin/ruff check tests/g004_cli_runner.py tests/test_g004_cli_runner_contract.py
```

These focused checks do not replace the full gates above.

## Node PATH caveat

The OpenClaw test requires Node `>=24.15.0`. On this development machine the default PATH has
previously selected Node v24.9.0 while a compatible Node v24.19.0 was installed at
`/opt/homebrew/Cellar/node@24/24.19.0/bin`. If the OpenClaw test rejects Node, inspect `node
--version` and prepend that compatible directory to PATH for the test process. Do not change the
test or dependency requirement to hide an environment mismatch.

## Pull requests

Describe the behavior changed, the evidence collected, and any limits. Do not include credentials,
private memory, PostgreSQL DSNs, backup artifacts, or raw diagnostics that contain them. Review
the [Code of Conduct](CODE_OF_CONDUCT.md), [security policy](SECURITY.md), and
[governance](GOVERNANCE.md) before submitting.
