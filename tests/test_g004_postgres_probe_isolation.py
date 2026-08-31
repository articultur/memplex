"""Real-catalogue regression coverage for the G004 external-DSN probe."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest

from tests import test_g004_postgres_backup_real_value as backup

__test__ = os.environ.get("MEMPLEX_REQUIRE_PGVECTOR") == "1"


@pytest.fixture
def probe_database(pg_server_dsn: str) -> Iterator[str]:
    """Own a fresh database; never remove an extension from the supplied DSN."""
    import psycopg2
    from psycopg2 import sql

    database = f"g004_probe_{uuid4().hex}"
    with closing(psycopg2.connect(pg_server_dsn)) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(database))
            )
        try:
            yield psycopg2.extensions.make_dsn(pg_server_dsn, dbname=database)
        finally:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


def _catalogue(dsn: str) -> dict[str, list[tuple]]:
    import psycopg2

    with closing(psycopg2.connect(dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM pg_extension ORDER BY extname")
            extensions = cursor.fetchall()
            cursor.execute(
                "SELECT oid, nspname, nspowner, nspacl FROM pg_namespace ORDER BY nspname"
            )
            schemas = cursor.fetchall()
            cursor.execute("SELECT oid, rolname FROM pg_roles ORDER BY rolname")
            roles = cursor.fetchall()
    return {"extensions": extensions, "schemas": schemas, "roles": roles}


def _install_vector(dsn: str) -> None:
    import psycopg2

    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE EXTENSION vector")
        connection.commit()


@pytest.mark.parametrize("installed", [False, True], ids=["absent", "installed"])
@pytest.mark.parametrize("read_only", [False, True], ids=["success", "failure"])
def test_probe_preserves_external_catalogue(
    probe_database: str, installed: bool, read_only: bool
) -> None:
    """Committing probe DDL or removing a pre-existing extension breaks this test."""
    import psycopg2

    if installed:
        _install_vector(probe_database)
    before = _catalogue(probe_database)
    dsn = psycopg2.extensions.make_dsn(
        probe_database, options=f"-c default_transaction_read_only={'on' if read_only else 'off'}"
    )
    if read_only:
        with pytest.raises(AssertionError, match="pgvector probe failed"):
            backup._probe_external_postgres(psycopg2, dsn)
    else:
        backup._probe_external_postgres(psycopg2, dsn)
        backup._probe_external_postgres(psycopg2, dsn)
    after = _catalogue(probe_database)
    print(f"probe installed={installed} read_only={read_only}: before={before!r} after={after!r}")
    assert after == before


@pytest.mark.parametrize("installed", [False, True], ids=["absent", "installed"])
def test_backup_lifecycle_cleans_up_without_changing_external_catalogue(
    probe_database: str, installed: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The real CLI lifecycle must remove only its own schema and application role."""
    if installed:
        _install_vector(probe_database)
    before = _catalogue(probe_database)
    monkeypatch.setenv("MEMPLEX_TEST_POSTGRES_DSN", probe_database)

    backup.test_real_postgres_backup_create_verify_restore_and_drill_preserve_cli_value(tmp_path)

    after = _catalogue(probe_database)
    print(f"lifecycle installed={installed}: before={before!r} after={after!r}")
    assert after == before
