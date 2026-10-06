"""Committed PostgreSQL raw snapshots use coherent SQL row identity, offline."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from memplex.auth import (
    AuthorizationContext,
    Principal,
    bind_node_identity,
    local_development_context,
)
from memplex.config import MemplexConfig
from memplex.context import ContextCandidate
from memplex.models import Fact
from memplex.service import MemplexService
from memplex.storage.postgres import PostgresMemoryStore


@pytest.fixture
def pg_raw_source(monkeypatch, tmp_path):
    context = AuthorizationContext(Principal("tenant", "alice"), "workspace", agent_id="agent", session_id="session")
    state = {
        "payload": {"raw_text": "CURRENT-PG-RAW", "trust_tier": 2, "source": "source.md#1"},
        "identity": ("raw", "tenant", "alice", "workspace", "user", "agent", "session"),
        "queries": [], "contexts": [], "absent": False, "context_historical": False,
    }

    class Cursor:
        def execute(self, sql, args):
            state["queries"].append((sql, args))

        def fetchone(self):
            if state["absent"]:
                return None
            sql = state["queries"][-1][0]
            if "SELECT data FROM" in sql:
                return (deepcopy(state["payload"]),)
            row_id, *identity = state["identity"]
            return (row_id, deepcopy(state["payload"]), *identity, state["context_historical"])

    @contextmanager
    def transaction(_binder, authorization):
        state["contexts"].append(authorization)
        yield None, Cursor()

    store = object.__new__(PostgresMemoryStore)
    store._require_authorization = True
    store._pool_manager = SimpleNamespace(transaction=transaction)
    for name in ("get", "get_fact", "get_preference", "get_observation"):
        monkeypatch.setattr(store, name, lambda _id: None)
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    service = MemplexService(config=config)
    original_store = service.store
    service.store = store
    yield service, store, context, state
    service.store = original_store
    service.stop()


def _assemble(service, context):
    return service.assemble_context([ContextCandidate("raw", "retrieval")],
                                    authorization=context, runtime_filter=lambda _node: True, max_tokens=4096)


@pytest.mark.parametrize("local", [False, True])
def test_fix1_actual_pg_payload_shape_renders_with_persisted_row_metadata(pg_raw_source, local):
    service, store, context, state = pg_raw_source
    if local:
        context = local_development_context()
        state["identity"] = ("raw", "local", "local-development", "local-development",
                             "workspace", "memplex", "local-development")
    # Public CRUD shape remains the real three-field payload, without an ID.
    assert store.authorized(context).get_paragraph("raw") == state["payload"]
    state["queries"].clear()
    result = _assemble(service, context)
    assert result.memory_ids == ("raw",)
    assert "CURRENT-PG-RAW" in result.context
    assert "trust=LOW" in result.context
    assert len(state["queries"]) == 1
    sql, args = state["queries"][0]
    assert "tenant_id" in sql and "owner_subject" in sql and "source_session" in sql
    assert "current_setting('memplex.tenant_id'" in sql
    assert args == ("raw",)
    assert state["contexts"][-1] == context


def test_fix1_pg_context_snapshot_contains_sql_identity_without_changing_crud(pg_raw_source):
    _service, store, context, state = pg_raw_source
    row = store.authorized(context).read_context_nodes(["raw"])["raw"]
    assert row["id"] == "raw"
    assert row["tenant_id"] == "tenant"
    assert row["owner_subject_id"] == "alice"
    assert row["workspace_id"] == "workspace"
    assert row["origin_session"] == "session"
    assert row["provenance"]["agent_id"] == "agent"
    assert store.authorized(context).get_paragraph("raw") == state["payload"]


@pytest.mark.parametrize("identity", [
    ("raw", "tenant", "bob", "workspace", "user", "agent", "session"),
    ("raw", "other", "alice", "workspace", "workspace", "agent", "session"),
    ("raw", "tenant", "alice", "other", "workspace", "agent", "session"),
    ("raw", None, "alice", None, "workspace", "agent", "session"),
    ("raw", "tenant", "alice", "workspace", "unknown", "agent", "session"),
    ("raw", "tenant", "alice", "workspace", "session", "other", "session"),
    ("wrong-row", "tenant", "alice", "workspace", "user", "agent", "session"),
    ("raw", ["tenant"], "alice", "workspace", "user", "agent", "session"),
])
def test_fix1_pg_payload_cannot_override_sql_identity_or_fill_missing_claims(pg_raw_source, identity):
    service, _store, context, state = pg_raw_source
    state["identity"] = identity
    state["payload"].update({
        "id": "raw", "tenant_id": "tenant", "owner_subject_id": "alice", "workspace_id": "workspace",
        "visibility": "user", "origin_session": "session", "owner": "alice",
        "namespace": {"memplex_tenant_id": "tenant", "memplex_subject_id": "alice", "memplex_grants": "agent"},
        "provenance": {"agent_id": "agent"},
    })
    result = _assemble(service, context)
    assert result.context == ""
    assert result.memory_ids == ()


def test_fix1_pg_stored_identity_wins_over_payload_conflicts(pg_raw_source):
    service, _store, context, state = pg_raw_source
    state["payload"].update({"id": "forged", "tenant_id": "other", "workspace_id": "other",
                             "owner_subject_id": "bob", "visibility": "unknown"})
    assert _assemble(service, context).memory_ids == ("raw",)


@pytest.mark.parametrize("payload", [None, [], "bad json", {"raw_text": {"bad": "shape"}}])
def test_fix1_pg_malformed_raw_payload_fails_closed(pg_raw_source, payload):
    service, _store, context, state = pg_raw_source
    state["payload"] = payload
    assert _assemble(service, context).context == ""


@pytest.mark.parametrize("position,value", [(1, None), (1, ""), (4, ""), (5, None)])
def test_fix1_pg_malformed_sql_columns_are_not_local_compatibility_identity(pg_raw_source, position, value):
    service, _store, _context, state = pg_raw_source
    identity = ["raw", "local", "local-development", "local-development", "workspace", "memplex", "local-development"]
    identity[position] = value
    state["identity"] = tuple(identity)
    assert _assemble(service, local_development_context()).context == ""


def test_pg_historical_raw_snapshot_remains_readable_without_current_body(pg_raw_source):
    service, store, context, state = pg_raw_source
    state["context_historical"] = True
    state["payload"]["context_historical"] = False
    snapshot = store.authorized(context).read_context_nodes(["raw"])["raw"]
    assert snapshot["raw_text"] == "CURRENT-PG-RAW"
    assert snapshot["context_historical"] is True
    assert store.authorized(context).get_paragraph("raw") == state["payload"]
    assert "raw" in service.resolve_context_nodes(["raw"], authorization=context)
    assembled = _assemble(service, context)
    assert assembled.context == ""
    assert assembled.memory_ids == ()
    assert assembled.dropped["empty"] == 1


def test_pg_historical_raw_source_still_authorizes_derived_lineage(pg_raw_source, monkeypatch):
    service, store, context, state = pg_raw_source
    state["context_historical"] = True
    derived = Fact(id="derived", subject="Related policy", predicate="is", object_="STILL-CURRENT")
    bind_node_identity(derived, context)
    derived.namespace.update(memplex_source_refs="raw", memplex_derivation="test")
    monkeypatch.setattr(store, "get_fact", lambda node_id: deepcopy(derived) if node_id == derived.id else None)
    assert derived.id in service.resolve_context_nodes([derived.id], authorization=context)
    assembled = service.assemble_context(
        [ContextCandidate(derived.id, "retrieval"), ContextCandidate("raw", "retrieval")],
        authorization=context, runtime_filter=lambda _node: True, max_tokens=4096,
    )
    assert assembled.memory_ids == (derived.id,)
    assert "STILL-CURRENT" in assembled.context
    assert "CURRENT-PG-RAW" not in assembled.context
    assert store.authorized(context).read_context_nodes(["raw"])["raw"]["context_historical"] is True


@pytest.mark.parametrize("historical", [None, 0, 1, "false", "true", {}, []])
def test_pg_malformed_historical_sql_flag_fails_closed(pg_raw_source, historical):
    service, store, context, state = pg_raw_source
    state["context_historical"] = historical
    state["payload"]["context_historical"] = False
    assert store.authorized(context).read_context_nodes(["raw"]) == {}
    assert _assemble(service, context).context == ""
    assert store.authorized(context).get_paragraph("raw") == state["payload"]
