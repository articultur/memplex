"""Tests for the PostgreSQL memory backend (R1).

No live PostgreSQL is required: these tests cover (a) Function <-> JSONB
serialization round-trip, (b) SQL construction via a mock psycopg2
connection, and (c) the create_store factory routing for the postgres
backend.
"""

import json
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models import FieldValue, Function, SourceDocument, SourceType  # noqa: E402
from memplex.storage.postgres import (  # noqa: E402
    PostgresMemoryStore,
    _func_from_json,
    _func_to_json,
)

# ── Serialization round-trip ─────────────────────────────────────────


def _sample_func(fid="pg-1", name="login"):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.lower(),
        domain="auth",
        confidence=0.9,
        source_type=SourceType.CODE,
        trigger=[
            FieldValue(desc="user logs in", sources=["t"], source_method="manual", weight=1.0)
        ],
        action=[FieldValue(desc="call auth()", sources=["t"], source_method="manual", weight=1.0)],
        attributes={"ns": "test"},
    )


def test_func_json_roundtrip_preserves_fields():
    f = _sample_func()
    data = _func_to_json(f)
    # JSONB-safe (serialisable).
    s = json.dumps(data)
    restored = _func_from_json(json.loads(s))
    assert restored.id == f.id
    assert restored.name == f.name
    assert restored.domain == f.domain
    assert restored.source_type == SourceType.CODE
    assert [fv.desc for fv in restored.trigger] == ["user logs in"]
    assert restored.attributes == {"ns": "test"}


def test_func_to_json_includes_search_text_fields():
    f = _sample_func()
    data = _func_to_json(f)
    assert "trigger_text" in data
    assert "user logs in" in data["trigger_text"]
    assert "action_text" in data


def test_func_from_json_tolerates_missing_fields():
    restored = _func_from_json({"id": "x", "name": "n"})
    assert restored.id == "x"
    assert restored.source_type == SourceType.WIKI  # default
    assert restored.trigger == []


def test_func_from_json_bad_source_type_falls_back_to_wiki():
    restored = _func_from_json({"id": "x", "source_type": "not-a-real-type"})
    assert restored.source_type == SourceType.WIKI


# ── Mock-connection fixture ──────────────────────────────────────────


class _MockCursor:
    def __init__(self):
        self.executed = []  # list of (sql, params)
        self._result = []
        self._fetchone_val = None

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_val

    def fetchall(self):
        return self._result

    def close(self):
        pass


class _MockConn:
    def __init__(self):
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self._cursor = _MockCursor()

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def pg_store(monkeypatch):
    """A PostgresMemoryStore with a mock connection (no real DB)."""
    store = PostgresMemoryStore(dsn="dbname=fake")
    mock_conn = _MockConn()
    monkeypatch.setattr(store, "_connect", lambda: mock_conn)
    # Bypass schema setup (already "done" by the mock).
    monkeypatch.setattr(store, "_ensure_schema", lambda: None)
    store._conn = mock_conn
    return store, mock_conn


# ── Write operations (SQL + params verified) ─────────────────────────


def test_add_executes_upsert_sql(pg_store):
    store, conn = pg_store
    store.add(_sample_func(), SourceDocument(type="text", source_type=SourceType.WIKI))
    assert conn.commits == 1
    sql = conn._cursor.executed[-1][0]
    assert "INSERT INTO memplex_functions" in sql
    assert "ON CONFLICT" in sql
    params = conn._cursor.executed[-1][1]
    assert params[0] == "pg-1"  # func id


def test_delete_executes_delete_sql(pg_store):
    store, conn = pg_store
    store.delete("pg-1")
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_functions" in s for s in sqls)
    assert any("DELETE FROM memplex_edges" in s for s in sqls)


def test_increment_access_executes_update(pg_store):
    store, conn = pg_store
    store.increment_access("pg-1")
    sql = conn._cursor.executed[-1][0]
    assert "UPDATE memplex_functions" in sql
    assert "access_count" in sql


def test_increment_access_batch_commits_once(pg_store):
    store, conn = pg_store
    store.increment_access_batch(["a", "b", "c"])
    # Batch path uses one transaction -> one commit.
    assert conn.commits == 1
    update_count = sum(1 for s, _ in conn._cursor.executed if "UPDATE" in s)
    assert update_count == 3


def test_add_observation_executes_insert(pg_store):
    store, conn = pg_store
    from memplex.models import Observation

    store.add_observation(
        Observation(
            id="obs-1",
            name="x",
            event="deploy",
            context="3am",
            confidence=0.5,
            source_type=SourceType.WIKI,
        )
    )
    sql = conn._cursor.executed[-1][0]
    assert "INSERT INTO memplex_observations" in sql


# ── Read operations (mock results) ───────────────────────────────────


def test_get_returns_function_when_found(pg_store):
    store, conn = pg_store
    f = _sample_func()
    conn._cursor._fetchone_val = (json.dumps(_func_to_json(f)),)
    got = store.get("pg-1")
    assert got is not None
    assert got.id == "pg-1"


def test_get_returns_none_when_missing(pg_store):
    store, conn = pg_store
    conn._cursor._fetchone_val = None
    assert store.get("missing") is None


def test_vector_search_uses_tsquery(pg_store):
    store, conn = pg_store
    f = _sample_func()
    conn._cursor._result = [("pg-1", json.dumps(_func_to_json(f)), 0.9)]
    results = store.vector_search("login", top_k=5)
    assert len(results) == 1
    assert results[0].func_id == "pg-1"
    sql = conn._cursor.executed[-1][0]
    assert "plainto_tsquery" in sql


def test_list_functions_paginates(pg_store):
    store, conn = pg_store
    f1 = _sample_func("pg-a")
    f2 = _sample_func("pg-b")
    conn._cursor._result = [(json.dumps(_func_to_json(f1)),), (json.dumps(_func_to_json(f2)),)]
    funcs = store.list_functions(offset=0, limit=10)
    assert len(funcs) == 2
    sql = conn._cursor.executed[-1][0]
    assert "OFFSET" in sql and "LIMIT" in sql


def test_clear_deletes_all_tables(pg_store):
    store, conn = pg_store
    store.clear()
    sqls = [s for s, _ in conn._cursor.executed]
    assert any("DELETE FROM memplex_functions" in s for s in sqls)
    assert any("DELETE FROM memplex_edges" in s for s in sqls)
    assert any("DELETE FROM memplex_observations" in s for s in sqls)


# ── Factory routing ──────────────────────────────────────────────────


def test_factory_postgres_backend_returns_postgres_store(monkeypatch):
    from memplex.storage import create_store

    store = create_store("postgres", path="dbname=fake")
    assert isinstance(store, PostgresMemoryStore)


def test_factory_postgres_without_dsn_raises():
    from memplex.storage import create_store

    with pytest.raises(ValueError, match="DSN"):
        create_store("postgres")


def test_factory_unknown_backend_still_raises():
    from memplex.storage import create_store

    with pytest.raises(ValueError):
        create_store("not-a-backend")


# ── Construction lazy (no psycopg2 needed to import/construct) ───────


def test_postgres_store_constructs_without_psycopg2():
    """Construction must not require psycopg2 (lazy connect)."""
    store = PostgresMemoryStore(dsn="dbname=fake")
    assert store._dsn == "dbname=fake"
    assert store._conn is None  # no connection attempted yet
