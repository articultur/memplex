"""Shared pytest configuration for the Memplex test suite.

Sets the storage backend to the implemented ``"lite"`` backend before any
test module is imported.  Several test files still carry their own
``os.environ.setdefault`` boilerplate at the top; this conftest acts as the
unified fallback so new test files do not need to repeat it.
"""

import os
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


@pytest.fixture(scope="session")
def pg_server_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run one private real PostgreSQL server for the PostgreSQL test module.

    Database-server startup is deliberately session-scoped, while every test
    receives its own schema below.  This keeps real-catalogue tests fast
    enough to run together without treating ``public`` as their scratchpad.
    """
    external_dsn = os.environ.get("MEMPLEX_TEST_POSTGRES_DSN")
    if external_dsn:
        # CI supplies pgvector through a service container.  Retain the
        # per-test schema fixture below, so an external server never turns
        # function-scoped catalogue tests into shared-public-state tests.
        yield external_dsn
        return

    pgserver = pytest.importorskip(
        "pgserver", reason="pgserver not installed (use .venv-pgcheck)"
    )
    data_dir = tmp_path_factory.mktemp("memplex-pgtest") / "data"
    server = pgserver.get_server(str(data_dir))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def pg_function_dsn(pg_server_dsn: str) -> Iterator[str]:
    """Yield one empty, uniquely named schema and remove it after one test.

    The options-based search path makes ordinary application, migration and
    catalogue connections target the same schema.  Cleanup uses the server
    DSN rather than the scoped DSN, so a failed migration cannot prevent
    ``DROP SCHEMA ... CASCADE`` from reclaiming its test catalogue.
    """
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    schema = f"g003_function_{uuid.uuid4().hex}"
    connection = psycopg2.connect(pg_server_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    scoped_dsn = psycopg2.extensions.make_dsn(
        pg_server_dsn,
        options=f"-c search_path={schema}",
    )
    try:
        yield scoped_dsn
    finally:
        cleanup = psycopg2.connect(pg_server_dsn)
        try:
            cursor = cleanup.cursor()
            try:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                cleanup.commit()
            finally:
                cursor.close()
        except BaseException:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()
