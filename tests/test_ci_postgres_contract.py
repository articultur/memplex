"""Non-skippable PostgreSQL capabilities required by the CI database service."""

from __future__ import annotations

import os

import pytest


def _psycopg2_for_real_postgres_contract():
    """Keep local pgserver-free runs optional, but fail a configured CI gate closed."""
    try:
        import psycopg2
    except ImportError:
        if os.environ.get("MEMPLEX_TEST_POSTGRES_DSN"):
            pytest.fail("MEMPLEX_TEST_POSTGRES_DSN requires psycopg2 to be installed")
        pytest.skip("psycopg2 not installed (install the pgtest extra)")
    return psycopg2


def test_ci_postgres_service_provides_a_working_pgvector_extension(pg_function_dsn: str) -> None:
    """CI must exercise the extension and cosine operator, not merely start PostgreSQL."""
    psycopg2 = _psycopg2_for_real_postgres_contract()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            assert cursor.fetchone() is not None
            cursor.execute("SELECT '[1,2,3]'::vector(3) <=> '[1,2,3]'::vector(3)")
            assert cursor.fetchone() == (0.0,)
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()
