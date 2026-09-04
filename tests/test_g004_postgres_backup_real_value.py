"""Externally gated real-value coverage for the PostgreSQL backup CLI."""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests import g004_cli_runner
from tests.g004_cli_runner import (
    parse_json_stdout,
    process_diagnostic,
    require_executables,
    run_cli,
)

# The ordinary Lite suite does not select this externally provisioned tier.
# An explicit PostgreSQL gate sets this contract before pytest collection.
__test__ = os.environ.get("MEMPLEX_REQUIRE_PGVECTOR") == "1"

MEMPLEX = (sys.executable, "-m", "memplex")


def test_required_postgres_client_tools_fail_with_a_precise_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    require_executables = getattr(g004_cli_runner, "require_executables", None)
    assert require_executables is not None, "generic executable prerequisite helper is missing"

    monkeypatch.setenv("PATH", "")
    with pytest.raises(
        AssertionError,
        match=r"^required executables unavailable: pg_dump, pg_restore$",
    ):
        require_executables(("pg_dump", "pg_restore"))


def test_external_postgres_connection_failure_is_fixed_and_credential_safe() -> None:
    import psycopg2

    probe = globals().get("_probe_external_postgres")
    assert callable(probe), "external PostgreSQL probe helper is missing"
    credential = "g004-connection-secret"
    invalid_dsn = (
        f"postgresql://g004:{credential}@127.0.0.1:1/memplex?connect_timeout=1"
    )

    with pytest.raises(AssertionError) as raised:
        probe(psycopg2, invalid_dsn)

    diagnostic = str(raised.value)
    assert diagnostic == (
        "PostgreSQL prerequisite unavailable: external DSN connection failed"
    )
    assert credential not in diagnostic
    assert invalid_dsn not in diagnostic


def _probe_external_postgres(psycopg2: Any, dsn: str) -> None:
    try:
        connection = psycopg2.connect(dsn)
    except Exception:  # noqa: BLE001 - broad catch, re-raised/wrapped below
        raise AssertionError(
            "PostgreSQL prerequisite unavailable: external DSN connection failed"
        ) from None

    try:
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cursor.execute("SELECT '[1,2,3]'::vector(3) <=> '[1,2,3]'::vector(3)")
                if cursor.fetchone() != (0.0,):
                    raise RuntimeError
                # Probe capability without installing anything on the external DSN.
                # Rollback also preserves an extension that was already installed.
                connection.rollback()
            finally:
                cursor.close()
        except Exception:  # noqa: BLE001 - broad catch, re-raised/wrapped below
            connection.rollback()
            raise AssertionError(
                "PostgreSQL prerequisite unavailable: pgvector probe failed"
            ) from None
    finally:
        connection.close()


def _real_postgres_prerequisites() -> tuple[Any, str]:
    dsn = os.environ.get("MEMPLEX_TEST_POSTGRES_DSN")
    if not dsn:
        raise AssertionError(
            "PostgreSQL prerequisite unavailable: MEMPLEX_TEST_POSTGRES_DSN is required"
        )
    if os.environ.get("MEMPLEX_REQUIRE_PGVECTOR") != "1":
        raise AssertionError(
            "PostgreSQL prerequisite unavailable: MEMPLEX_REQUIRE_PGVECTOR must be 1"
        )
    try:
        import psycopg2
    except ImportError:
        raise AssertionError(
            "PostgreSQL prerequisite unavailable: psycopg2 is required"
        ) from None

    require_executables(("pg_dump", "pg_restore"))
    _probe_external_postgres(psycopg2, dsn)
    return psycopg2, dsn


def _run_json(args: list[str], env: dict[str, str]) -> Any:
    completed = run_cli(args, env=env, timeout=120)
    assert completed.returncode == 0, process_diagnostic(completed)
    return parse_json_stdout(completed)


def _assert_json_contains(payload: Any, value: str) -> None:
    assert value in json.dumps(payload, ensure_ascii=False)


def _assert_nonnegative_number(payload: dict[str, Any], field: str) -> None:
    value = payload[field]
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert value >= 0


def _create_schema(psycopg2: Any, dsn: str, schema: str) -> None:
    from psycopg2 import sql as pg_sql

    connection = psycopg2.connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema))
            )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def _drop_schema_and_role(
    psycopg2: Any,
    dsn: str,
    schema: str,
    role: str,
) -> None:
    from psycopg2 import sql as pg_sql

    connection = psycopg2.connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    pg_sql.Identifier(schema)
                )
            )
            cursor.execute(
                pg_sql.SQL("DROP ROLE IF EXISTS {}").format(pg_sql.Identifier(role))
            )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def _drop_schema(psycopg2: Any, dsn: str, schema: str) -> None:
    from psycopg2 import sql as pg_sql

    connection = psycopg2.connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                pg_sql.SQL("DROP SCHEMA {} CASCADE").format(pg_sql.Identifier(schema))
            )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def test_real_postgres_backup_create_verify_restore_and_drill_preserve_cli_value(
    tmp_path: Path,
) -> None:
    """A real artifact must preserve a public-CLI write across restore and drill."""
    psycopg2, external_dsn = _real_postgres_prerequisites()
    from memplex.storage.migrations import PostgresMigrationRunner
    from tests.test_postgres_backup_integration import _grant_application_acl_contract

    suffix = uuid4().hex
    schema = f"g004_backup_{suffix}"
    role = f"g004_backup_app_{suffix}"
    lookup = f"g004 backup lifecycle lookup {suffix}"
    canary = f"g004-postgres-backup-value-{uuid4().hex}"
    destination = tmp_path / "backups"
    signing_key = base64.b64encode(bytes(range(32))).decode("ascii")

    _create_schema(psycopg2, external_dsn, schema)
    try:
        migration_dsn = psycopg2.extensions.make_dsn(
            external_dsn,
            options=f"-c search_path={schema}",
        )
        app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
        assert PostgresMigrationRunner(migration_dsn).apply().state == "ready"
        _grant_application_acl_contract(migration_dsn, role)
        env = {
            "MEMPLEX_STORAGE_BACKEND": "postgres",
            "MEMPLEX_STORAGE_PATH": app_dsn,
            "MEMPLEX_STORAGE_MIGRATION_DSN": migration_dsn,
            "MEMPLEX_SYNC_ENABLED": "false",
            "MEMPLEX_BACKUP_HMAC_KEY": signing_key,
            "MEMPLEX_BACKUP_KEY_ID": "g004-task-5",
        }

        written = _run_json(
            [
                *MEMPLEX,
                "--output",
                "json",
                "write",
                "--text",
                f"For {lookup}, preserve the unique value {canary}.",
            ],
            env,
        )
        assert written["functions_extracted"] > 0

        created = _run_json(
            [
                *MEMPLEX,
                "--output",
                "json",
                "storage",
                "backup",
                "create",
                "--destination",
                str(destination),
            ],
            env,
        )
        artifact = destination / created["backup_id"]
        assert artifact.is_dir()
        assert artifact.is_relative_to(tmp_path)

        verified = _run_json(
            [*MEMPLEX, "--output", "json", "storage", "backup", "verify", str(artifact)],
            env,
        )
        assert verified["verified"] is True
        assert verified["backup_id"] == created["backup_id"]

        _drop_schema(psycopg2, external_dsn, schema)
        restored = _run_json(
            [
                *MEMPLEX,
                "--output",
                "json",
                "storage",
                "backup",
                "restore",
                str(artifact),
                "--target-schema",
                schema,
            ],
            env,
        )
        assert restored["restored"] is True
        assert restored["schema"] == schema

        recalled = _run_json(
            [*MEMPLEX, "--output", "json", "recall", lookup],
            env,
        )
        assert canary not in recalled.get("query", "")
        _assert_json_contains(recalled, canary)

        _drop_schema(psycopg2, external_dsn, schema)
        drilled = _run_json(
            [
                *MEMPLEX,
                "--output",
                "json",
                "storage",
                "backup",
                "drill",
                "--artifact",
                str(artifact),
                "--target-schema",
                schema,
            ],
            env,
        )
        assert drilled["backup_id"] == created["backup_id"]
        assert drilled["data_verified"] is True
        _assert_nonnegative_number(drilled, "observed_rpo_seconds")
        _assert_nonnegative_number(drilled, "observed_rto_seconds")

        recalled_after_drill = _run_json(
            [*MEMPLEX, "--output", "json", "recall", lookup],
            env,
        )
        assert canary not in recalled_after_drill.get("query", "")
        _assert_json_contains(recalled_after_drill, canary)
    finally:
        _drop_schema_and_role(psycopg2, external_dsn, schema, role)
