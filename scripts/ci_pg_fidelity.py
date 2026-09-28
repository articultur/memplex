#!/usr/bin/env python3
"""CI-fidelity PostgreSQL gate: external DSN + mandatory pgvector.

Mirrors the CI test-postgres job. Uses the pgtest venv's pgserver to
spin a real PostgreSQL instance and runs the pinned test file list
against it. Exit code 0 = zero regressions in the PG suite.

Runs pytest in-process (``pytest.main``) - there is no subprocess
call and no shell anywhere in this script; the pgserver DSN only
flows into the environment dict. Run with the py3.12 pgtest venv:

    uv venv /tmp/memplex-pgtest-venv --python 3.12
    uv pip install --python /tmp/memplex-pgtest-venv/bin/python -e ".[dev,pgtest]"
    /tmp/memplex-pgtest-venv/bin/python scripts/ci_pg_fidelity.py
"""

import os
import tempfile

import pgserver
import pytest

PG_FILES = (
    "tests/test_ci_postgres_contract.py",
    "tests/test_postgres_integration.py",
    "tests/test_postgres_backup_integration.py",
    "tests/test_sync_postgres_integration.py",
    "tests/test_sync_repository_contract.py",
    "tests/test_postgres_store.py",
    "tests/test_g014_postgres_task_repository.py",
)


def main() -> int:
    server = pgserver.get_server(tempfile.mkdtemp(prefix="memplex-gate-") + "/data")
    os.environ["MEMPLEX_TEST_POSTGRES_DSN"] = server.get_uri()
    os.environ["MEMPLEX_REQUIRE_PGVECTOR"] = "1"
    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    try:
        return int(pytest.main(["-q", "-p", "no:cacheprovider", *PG_FILES]))
    finally:
        server.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
