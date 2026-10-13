"""V1 source commands own writes; legacy and unknown mutators fail closed."""
from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from memplex.authorization import AuthorizationGate
from memplex.config import MemplexConfig
from memplex.models import Fact, Function, GraphData, Observation, Preference, SourceDocument
from memplex.service import MemplexService
from memplex.source_contract import SourceContractError, SourceOperation
from tests.test_source_v1_pg_transactions import CONTEXT, NATIVE, _command, _fence, _mutate
from tests.test_source_v1_pg_transactions import (
    protocol_backend as protocol_backend,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_source_v1_pg_transactions import (
    source_backend as source_backend,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_source_v1_publication import (
    publication_backend as publication_backend,  # noqa: PLC0414 - pytest fixture registration
)


@pytest.fixture
def service(publication_backend):
    h = publication_backend
    service = MemplexService.__new__(MemplexService)
    service.store = h.store
    service._config = MemplexConfig()
    service.feedback_store = SimpleNamespace(resolve=lambda *args: pytest.fail("partial feedback resolution"))
    service._auth = AuthorizationGate(service._config, lambda: service.store, lambda: service.feedback_store)
    service._invalidate_hot_references = lambda *args, **kwargs: None
    service.scan_nodes_before_persistence = lambda nodes: None
    return service


def read(backend, key):
    with _fence(backend) as session:
        return session.source(key)


@pytest.mark.parametrize("route,delta", [("replace", (1, 0)), ("share", (0, 1)), ("revoke", (0, 1)),
                                         ("metadata", (1, 0)), ("promote", (1, 1))])
def test_all_registered_mutations_advance_correct_revision(publication_backend, service, route, delta):
    h = publication_backend
    command = _command()
    command = replace(command, payload={**command.payload, "visibility": "user"})
    first = _mutate(h.backend, command)
    if route == "share":
        service.share_with("source", "reader-agent", authorization=CONTEXT)
    elif route == "promote":
        service.promote("source", "team", authorization=CONTEXT)
    elif route == "metadata":
        service.annotate_memories(["source"], needs_review=True, authorization=CONTEXT)
    else:
        payload = dict(read(h.backend, command.key).payload)
        if route == "revoke":
            payload["namespace"] = {**payload["namespace"], "memplex_grants": "reader-agent"}
            first = _mutate(h.backend, replace(command, operation=SourceOperation.REPLACE,
                                              expected=first.ref.token, payload=payload))
            payload["namespace"] = {**payload["namespace"], "memplex_grants": ""}
        else:
            payload["object"] = "corrected"
        service.replace_source(command.key, first.ref.token, payload, authorization=CONTEXT)
    after = read(h.backend, command.key)
    assert after.head.ref.token.content_revision == first.ref.token.content_revision + delta[0]
    assert after.head.ref.token.auth_revision == first.ref.token.auth_revision + delta[1]


LEGACY_ROUTES = ("add", "add_batch", "add_fact", "add_preference", "add_observation", "persist_paragraphs",
                 "update_function_role", "replace_function", "merge", "delete", "delete_fact",
                 "delete_preference", "delete_observation", "annotate", "annotate_nodes", "clear")


@pytest.mark.parametrize("route", LEGACY_ROUTES)
def test_legacy_mutation_admission_is_closed(publication_backend, route):
    h = publication_backend
    _mutate(h.backend, _command())
    store = h.store.authorized(CONTEXT) if h.kind == "postgres" else h.store
    method = getattr(store, route, None)
    if method is None:
        pytest.skip("backend does not expose this legacy mutator")
    node = Function(id="fn", name="fn")
    calls = {"add": (node, SourceDocument(type="text")), "add_batch": ([node], [SourceDocument(type="text")]),
             "add_fact": (Fact(id="new", subject="x", predicate="is", object_="y"),),
             "add_preference": (Preference(id="pref", aspect="x", preference="y"),),
             "add_observation": (Observation(id="obs", event="x"),), "persist_paragraphs": ([SimpleNamespace(id="p", raw_text="evidence", source="test")],),
             "update_function_role": ("fn", "action", "new"), "replace_function": (node,),
             "merge": (GraphData(nodes=[node]),), "delete": ("source",), "delete_fact": ("source",),
             "delete_preference": ("source",), "delete_observation": ("source",), "annotate": (["source"],),
             "annotate_nodes": (["source"],), "clear": ()}
    kwargs = {"trust_tier": 1, "source_hint": "source", "authorization": CONTEXT} if route == "persist_paragraphs" else {}
    with pytest.raises(SourceContractError):
        method(*calls[route], **kwargs)
    assert read(h.backend, _command().key).payload["object"] == "one"


def test_resolution_rejects_before_feedback_write(publication_backend, service):
    _mutate(publication_backend.backend, _command())
    with pytest.raises(SourceContractError):
        service.apply_resolution("source", "action", "merge", "replacement", authorization=CONTEXT)


def test_unknown_resident_mutation_is_reloaded(publication_backend):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("resident objects are Lite only")
    _mutate(h.backend, _command())
    before = copy.deepcopy(h.store._facts["source"])
    h.store._facts["source"].object_ = "speculative"
    with pytest.raises(SourceContractError):
        h.store._commit_current_state()
    assert h.store.get_fact("source") == before


def function_command(*, raw=False):
    from memplex.models import FieldValue
    from memplex.source_contract import SourceClass, SourceKey, SourceKind, SourceMutation
    from tests.test_source_v1_pg_transactions import SCOPE

    base = _command(name="raw" if raw else "fn")
    if raw:
        payload = {"id": "raw", "raw_text": "original evidence", "trust_tier": 1,
                   "created_at": "2026-01-01T00:00:00+00:00", "source": "test", "owner_subject_id": "alice",
                   "tenant_id": SCOPE.tenant_id, "workspace_id": SCOPE.workspace_id, "visibility": "workspace"}
        kind = SourceKind.RAW_PARAGRAPH
    else:
        payload = Function(id="fn", name="function", action=[FieldValue(desc="original")],
                           source_paragraphs=["raw"], tenant_id=SCOPE.tenant_id,
                           workspace_id=SCOPE.workspace_id, owner_subject_id="alice", visibility="workspace").to_dict()
        kind = SourceKind.FUNCTION
    return SourceMutation(SourceKey(SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id, kind, base.key.logical_id),
                          base.expected, base.operation, payload, SourceClass.DIRECT_INPUT, {}, ())


def test_typed_edit_versions_raw_historical_in_same_commit(publication_backend, service):
    h = publication_backend
    raw = _mutate(h.backend, function_command(raw=True))
    old = _mutate(h.backend, function_command())
    result = service.update_memory("fn", "action", "corrected", authorization=CONTEXT)
    assert result.success
    current_raw = read(h.backend, raw.ref.key)
    current_typed = read(h.backend, old.ref.key)
    assert current_raw.payload["premise_superseded"]
    assert current_raw.head.ref.token.content_revision == raw.ref.token.content_revision + 1
    assert current_typed.head.ref.token.content_revision == old.ref.token.content_revision + 1
    assert current_raw.head.last_commit_seq == current_typed.head.last_commit_seq


def test_access_only_keeps_source_token(publication_backend):
    h = publication_backend
    first = _mutate(h.backend, replace(function_command(), payload={**function_command().payload, "source_paragraphs": []}))
    store = h.store.authorized(CONTEXT) if h.kind == "postgres" else h.store
    store.increment_access("fn")
    store.increment_access_batch(["fn"])
    current = read(h.backend, first.ref.key)
    assert current.head.ref == first.ref
    assert current.payload["access_count"] == 2


@pytest.mark.parametrize("alias", ["memplex_grants", "memplex_source_agent", "memplex_agent"])
def test_declared_acl_alias_is_auth_only_and_business_stays_semantic(publication_backend, alias):
    h = publication_backend
    command = _command()
    first = _mutate(h.backend, command)
    before = read(h.backend, first.ref.key)
    changed = {**before.payload, "namespace": {**before.payload["namespace"], alias: "new-agent"}}
    receipt = _mutate(h.backend, replace(command, expected=first.ref.token,
                                         operation=SourceOperation.REPLACE, payload=changed))
    assert receipt.ref.semantic_digest == first.ref.semantic_digest
    assert receipt.ref.token.content_revision == first.ref.token.content_revision
    assert receipt.ref.token.auth_revision == first.ref.token.auth_revision + 1
    current = read(h.backend, first.ref.key)
    changed = {**current.payload, "namespace": {**current.payload["namespace"], "business": "changed"}}
    last = _mutate(h.backend, replace(command, expected=receipt.ref.token,
                                      operation=SourceOperation.REPLACE, payload=changed))
    assert last.ref.token.content_revision == receipt.ref.token.content_revision + 1
    assert last.ref.token.auth_revision == receipt.ref.token.auth_revision


@pytest.mark.parametrize("alias", ["memplex_grants", "memplex_source_agent", "memplex_agent"])
def test_acl_alias_tamper_fails_locator_integrity(publication_backend, alias):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite pair fixture corruption; PG payload integrity covered by native source tests")
    from memplex.storage.lite.durability import LiteStorageIntegrityError
    from tests.test_source_v1_lite_format import write_fixture

    _mutate(h.backend, _command())
    pair = h.store._durability.load_authoritative()
    pair.memory["facts"][0]["namespace"][alias] = "tampered"
    write_fixture(h.store, pair)
    with pytest.raises(LiteStorageIntegrityError), _fence(h.backend):
        pass


@pytest.mark.parametrize("alias", ["memplex_source_agent", "memplex_agent"])
def test_session_acl_alias_preserves_fallback_and_provenance_priority(alias):
    from memplex.authorization import is_own_node_visible, source_authorization_node
    from memplex.source_codec import semantic_digest
    from memplex.source_contract import SourceSnapshot
    from tests.source_v1_support import source_snapshot

    snapshot = source_snapshot("raw")
    payload = {**snapshot.payload, "tenant_id": CONTEXT.principal.tenant_id, "workspace_id": CONTEXT.workspace_id,
               "owner_subject_id": CONTEXT.principal.subject_id, "visibility": "session", "origin_session": CONTEXT.session_id,
               "namespace": {alias: CONTEXT.agent_id}, "provenance": {}}
    node = source_authorization_node(SourceSnapshot(snapshot.head, payload))
    assert is_own_node_visible(node, CONTEXT)
    node.namespace[alias] = "other"
    assert not is_own_node_visible(node, CONTEXT)
    node.provenance["agent_id"] = CONTEXT.agent_id
    assert is_own_node_visible(node, CONTEXT)
    node.provenance["agent_id"] = "other"
    node.namespace[alias] = CONTEXT.agent_id
    assert not is_own_node_visible(node, CONTEXT)
    before = semantic_digest({"attributes": {alias: "old"}}, input_context={}, root_refs=())
    assert semantic_digest({"attributes": {alias: "new"}}, input_context={}, root_refs=()) != before
    before = semantic_digest({alias: "old"}, input_context={}, root_refs=())
    assert semantic_digest({alias: "new"}, input_context={}, root_refs=()) != before


def test_multi_annotation_and_every_resolution_have_zero_partial_writes(publication_backend, service):
    h = publication_backend
    first = _mutate(h.backend, _command())
    second = _mutate(h.backend, _command(name="second"))
    before = [read(h.backend, item.ref.key) for item in (first, second)]
    with pytest.raises(SourceContractError):
        service.annotate_memories(["source", "second"], needs_review=True, authorization=CONTEXT)
    for action in ["accept", "reject", "merge"]:
        with pytest.raises(SourceContractError):
            service.apply_resolution("source", "action", action, "new", authorization=CONTEXT)
    assert before == [read(h.backend, item.ref.key) for item in (first, second)]


def test_source_delete_and_stale_replace_use_cas(publication_backend, service):
    h = publication_backend
    first = _mutate(h.backend, _command())
    service.replace_source(first.ref.key, first.ref.token, {**read(h.backend, first.ref.key).payload, "object": "current"},
                           authorization=CONTEXT)
    with pytest.raises(SourceContractError):
        service.replace_source(first.ref.key, first.ref.token, _command().payload, authorization=CONTEXT)
    service.delete("source", authorization=CONTEXT)
    assert read(h.backend, first.ref.key).head.state.value == "deleted"


def test_raw_side_effect_rolls_back_with_typed_edit(publication_backend, service, monkeypatch):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite pre-durable fault injection")
    raw = _mutate(h.backend, function_command(raw=True))
    typed = _mutate(h.backend, function_command())
    before = [read(h.backend, item.ref.key) for item in (raw, typed)]
    def crash():
        raise OSError("same decision failed")
    monkeypatch.setattr(h.store._durability, "before_journal_durable_publish", crash)
    with pytest.raises(OSError):
        service.update_memory("fn", "action", "corrected", authorization=CONTEXT)
    assert before == [read(h.backend, item.ref.key) for item in (raw, typed)]


@pytest.mark.parametrize("route", ["typed_batch", "compaction", "unsealed_commit"])
def test_remaining_lite_maintenance_admission(publication_backend, route):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite-only maintenance interfaces")
    from memplex.storage.lite.durability import LitePair
    from memplex.storage.typed_batch import TypedBatchInput

    _mutate(h.backend, _command())
    before = h.store._durability.load_authoritative()
    with pytest.raises(SourceContractError):
        if route == "typed_batch":
            h.store.run_typed_write_batch(TypedBatchInput((), ()), lambda *args: pytest.fail("planner entered"))
        elif route == "compaction":
            h.store.apply_compaction(replacements=[], delete_ids=["source"])
        else:
            h.store._durability.commit_locked(before, LitePair(before.memory, before.changelog, before.generation + 1, "unknown"))
    assert h.store._durability.load_authoritative() == before


def test_v2_metadata_batch_and_resolution_keep_legacy_behavior(tmp_path):
    from memplex.storage.lite.store import LiteMemoryStore

    store = LiteMemoryStore(tmp_path / "legacy.json")
    resolved = []
    service = MemplexService.__new__(MemplexService)
    service.store, service._config = store, MemplexConfig()
    feedback = SimpleNamespace(resolve=lambda *args: resolved.append(args))
    service._auth = AuthorizationGate(service._config, lambda: store, lambda: feedback)
    service._invalidate_hot_references = lambda *args, **kwargs: None
    for identifier in ("one", "two"):
        store.add_fact(Fact.from_dict(dict(_command(name=identifier).payload)))
    result = service.annotate_memories(["one", "two"], needs_review=True, authorization=CONTEXT)
    assert len(result) == 2
    assert all(store.get_fact(identifier).needs_review for identifier in ("one", "two"))
    assert service.apply_resolution("one", "action", "accept", authorization=CONTEXT)["status"] == "resolved"
    assert resolved == [("one", "action", "accept")]


def test_unknown_postgres_mutator_is_denied_before_sql(request):
    resources, store, _, issuer, hooks = request.getfixturevalue("protocol_backend")
    from tests.test_source_v1_pg_transactions import SCOPE

    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    for sql in ["UPDATE memplex_facts SET data='{}'", "DELETE FROM memplex_functions",
                "WITH removed AS (DELETE FROM memplex_facts RETURNING id) SELECT * FROM removed"]:
        with pytest.raises(SourceContractError), resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (_, cursor):
            cursor.execute(sql)


def test_captured_source_body_dependencies_and_context_are_versioned(publication_backend, service):
    from memplex.source_contract import SourceClass

    h = publication_backend
    raw_command = function_command(raw=True)
    raw = _mutate(h.backend, raw_command)
    second_command = replace(raw_command, key=replace(raw_command.key, logical_id="raw-two"),
                             payload={**raw_command.payload, "id": "raw-two"})
    second_raw = _mutate(h.backend, second_command)
    captured = replace(_command(), source_class=SourceClass.CAPTURED_INPUT, root_refs=(raw.ref,),
                       input_context={"capture": "one"})
    first = _mutate(h.backend, captured)
    service.replace_source(first.ref.key, first.ref.token, {**read(h.backend, first.ref.key).payload, "object": "edit"},
                           authorization=CONTEXT)
    current = read(h.backend, first.ref.key)
    assert current.head.ref.token.content_revision == 2
    second = _mutate(h.backend, replace(captured, expected=current.head.ref.token, operation=SourceOperation.REPLACE,
                                        payload=current.payload, root_refs=(second_raw.ref,)))
    assert second.ref.token.content_revision == 3
    third = _mutate(h.backend, replace(captured, expected=second.ref.token, operation=SourceOperation.REPLACE,
                                       payload=current.payload, root_refs=(second_raw.ref,), input_context={"capture": "two"}))
    assert third.ref.token.content_revision == 4
    assert third.ref.token.auth_revision == first.ref.token.auth_revision


@pytest.mark.parametrize("route", ["replace", "delete_source", "delete", "share", "promote", "metadata", "resolution"])
def test_derived_old_mutation_routes_are_explicitly_unsupported(publication_backend, service, route):
    from tests.test_source_v1_publication import publish, publish_command

    h = publication_backend
    source = _mutate(h.backend, _command())
    receipt = publish(h.backend, publish_command(h.backend, source))
    with _fence(h.backend) as session:
        key = session.resolve_id(receipt.publication_id)
        before = session.source(key)
        authority = session.authority()
    assert before is not None
    calls = {
        "replace": lambda: service.replace_source(key, before.head.ref.token, before.payload, authorization=CONTEXT),
        "delete_source": lambda: service.delete_source(key, before.head.ref.token, authorization=CONTEXT),
        "delete": lambda: service.delete(receipt.publication_id, authorization=CONTEXT),
        "share": lambda: service.share_with(receipt.publication_id, "other", authorization=CONTEXT),
        "promote": lambda: service.promote(receipt.publication_id, "team", authorization=CONTEXT),
        "metadata": lambda: service.annotate_memories([receipt.publication_id], needs_review=True, authorization=CONTEXT),
        "resolution": lambda: service.apply_resolution(receipt.publication_id, "action", "accept", authorization=CONTEXT),
    }
    with pytest.raises(SourceContractError, match="unsupported_semantics"):
        calls[route]()
    with _fence(h.backend) as session:
        assert session.source(key) == before
        assert session.authority() == authority
        assert session.target("d" * 64).token == receipt.target


@pytest.mark.parametrize("row", [None, ("wrong", CONTEXT.workspace_id, False),
                                  (CONTEXT.principal.tenant_id, "wrong", False),
                                  (CONTEXT.principal.tenant_id, CONTEXT.workspace_id, 0)])
def test_unknown_or_unbound_postgres_scope_is_not_legacy(row):
    from memplex.storage.pool import _source_cursor_admission

    cursor = SimpleNamespace(execute=lambda *args: None, fetchone=lambda: row)
    with pytest.raises(SourceContractError):
        _source_cursor_admission(SimpleNamespace(_source_resource=None), cursor, CONTEXT)


def test_postgres_authority_query_failure_is_not_legacy():
    from memplex.storage.pool import _source_cursor_admission

    def denied(*args):
        raise PermissionError("authority lookup denied")
    with pytest.raises(PermissionError):
        _source_cursor_admission(SimpleNamespace(_source_resource=None), SimpleNamespace(execute=denied), CONTEXT)


@pytest.mark.parametrize("controlled", [False, True])
def test_postgres_admission_uses_current_authority_without_adapter_flag(controlled):
    from memplex.storage.pool import _source_cursor_admission

    statements = []
    cursor = SimpleNamespace(execute=lambda *args: statements.append(args),
                             fetchone=lambda: (CONTEXT.principal.tenant_id, CONTEXT.workspace_id, controlled))
    admitted = _source_cursor_admission(SimpleNamespace(_source_resource=None), cursor, CONTEXT)
    if controlled:
        with pytest.raises(SourceContractError):
            admitted.execute("UPDATE memplex_facts SET data='{}'")
        assert statements[-1][0] == "SET TRANSACTION READ ONLY"
    else:
        assert admitted is cursor


@NATIVE
def test_native_stale_legacy_handle_cannot_modify_v1(request, pg_function_dsn):
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore

    # Construct this ordinary handle before the test-only V1 authority seed.
    resources = PostgresStorageResources(pg_function_dsn)
    resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    stale = PostgresMemoryStore(pg_function_dsn, ready_pool=resources.ready_pool, require_authorization=True)
    try:
        _, backend, _, _, _, _ = request.getfixturevalue("source_backend")
        first = _mutate(backend, _command())
        assert stale.source_versions() is None
        with pytest.raises(SourceContractError):
            stale.authorized(CONTEXT).add_fact(Fact.from_dict(dict(_command(value="bypass").payload)))
        assert read(backend, first.ref.key).payload["object"] == "one"
        assert read(backend, first.ref.key).head.ref == first.ref
    finally:
        resources.close()


@pytest.mark.parametrize("source_class", ["direct_input", "captured_input"])
@pytest.mark.parametrize("route,delta", [("share", (0, 1)), ("metadata", (1, 0)),
                                         ("replace", (1, 0)), ("review", None), ("promote", None)])
def test_raw_convenience_routes_use_canonical_payload_or_explicit_rejection(publication_backend, service, source_class, route, delta):
    from memplex.source_contract import SourceClass

    h = publication_backend
    command = replace(function_command(raw=True), source_class=SourceClass(source_class))
    first = _mutate(h.backend, command)
    before = read(h.backend, first.ref.key)
    with _fence(h.backend) as session:
        authority = session.authority()
    calls = {
        "share": lambda: service.share_with("raw", "reader-agent", authorization=CONTEXT),
        "metadata": lambda: service.annotate_memories(["raw"], attributes={"memplex_note": "checked"}, authorization=CONTEXT),
        "replace": lambda: service.replace_source(first.ref.key, first.ref.token,
                                                   {**dict(before.payload), "raw_text": "corrected"}, authorization=CONTEXT),
        "review": lambda: service.annotate_memories(["raw"], needs_review=True, authorization=CONTEXT),
        "promote": lambda: service.promote("raw", "team", authorization=CONTEXT),
    }
    if delta is None:
        with pytest.raises(SourceContractError, match="unsupported_semantics"):
            calls[route]()
        assert read(h.backend, first.ref.key) == before
        with _fence(h.backend) as session:
            assert session.authority() == authority
    else:
        calls[route]()
        after = read(h.backend, first.ref.key)
        assert after.head.ref.token.content_revision == first.ref.token.content_revision + delta[0]
        assert after.head.ref.token.auth_revision == first.ref.token.auth_revision + delta[1]
        assert after.payload["raw_text"] == ("corrected" if route == "replace" else before.payload["raw_text"])
        assert set(after.payload) - set(before.payload) <= {"namespace"}
        assert after.payload["owner_subject_id"] == before.payload["owner_subject_id"]
        if route == "share":
            assert after.payload["namespace"]["memplex_grants"] == "reader-agent"
        if route == "metadata":
            assert after.payload["namespace"]["memplex_note"] == "checked"


@pytest.mark.parametrize("interruption", [None, ValueError, KeyboardInterrupt, SystemExit, BaseException])
def test_postgres_read_fence_does_not_lend_admission_to_nested_transactions(protocol_backend, monkeypatch, interruption):
    from contextlib import nullcontext

    from memplex.source_contract import AuthorityState, SourceState
    from memplex.storage.postgres_sources import _PostgresReadSession
    from tests.test_source_v1_pg_transactions import POLICY, SCOPE

    resources, store, pool, issuer, hooks = protocol_backend
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    backend = store.source_versions()
    monkeypatch.setattr(_PostgresReadSession, "authority", lambda self: AuthorityState(SCOPE, SourceState.LIVE, 1, POLICY, 1))
    outer = pytest.raises(interruption) if interruption is not None else nullcontext()
    with outer, _fence(backend) as session:
        with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (connection, cursor):
            assert connection is not session._cursor.connection
            with pytest.raises(SourceContractError, match="unsupported_semantics"):
                cursor.execute("UPDATE memplex_facts SET data='{}'")
        with pytest.raises(SourceContractError, match="unsupported_semantics"):
            store.authorized(CONTEXT).add_fact(Fact.from_dict(dict(_command().payload)))
        if interruption is not None:
            raise interruption("cancel the read fence")
    assert resources.pool_manager.business_lease_count == 0
    with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (_, cursor), pytest.raises(SourceContractError, match="unsupported_semantics"):
        cursor.execute("UPDATE memplex_facts SET data='{}'")
    assert all(not any("UPDATE memplex_facts SET data='{}'" in sql for sql, _ in connection.cursor_instance.executed)
               for connection in pool.connections)


@NATIVE
@pytest.mark.parametrize("interruption", [None, ValueError, KeyboardInterrupt])
def test_native_read_fence_cannot_lend_admission_to_another_connection(source_backend, interruption):
    from contextlib import nullcontext

    store, backend, resources, _, _, _ = source_backend
    first = _mutate(backend, _command())
    outer = pytest.raises(interruption) if interruption is not None else nullcontext()
    with outer, _fence(backend) as session:
        with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (connection, cursor):
            assert connection is not session._cursor.connection
            with pytest.raises(SourceContractError, match="unsupported_semantics"):
                cursor.execute("UPDATE memplex_facts SET data='{}'")
        with pytest.raises(SourceContractError, match="unsupported_semantics"):
            store.authorized(CONTEXT).add_fact(Fact.from_dict(dict(_command(value="unregistered").payload)))
        if interruption is not None:
            raise interruption("cancel native fence")
    assert resources.pool_manager.business_lease_count == 0
    with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (_, cursor), pytest.raises(SourceContractError):
        cursor.execute("UPDATE memplex_facts SET data='{}'")
    assert read(backend, first.ref.key).head.ref == first.ref
    assert read(backend, first.ref.key).payload["object"] == "one"


@pytest.mark.parametrize("interruption", [None, ValueError, KeyboardInterrupt, SystemExit, BaseException])
def test_source_transaction_capability_is_one_use_even_if_binding_is_cancelled(protocol_backend, interruption):
    import pickle
    import time

    from memplex.storage.pool import _source_transaction
    from tests.test_source_v1_pg_transactions import SCOPE

    resources, store, _, issuer, hooks = protocol_backend
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    def bind(cursor, context):
        if interruption is not None:
            raise interruption("cancel before physical admission")
        store._bind_transaction_scope(cursor, context)
    transaction = _source_transaction(resources.pool_manager, bind, CONTEXT, resource=resources,
                                      issuer=issuer, deadline_ns=time.monotonic_ns() + 250_000_000)
    capability = transaction._source_admission
    with pytest.raises(TypeError):
        pickle.dumps(capability)
    if interruption is not None:
        with pytest.raises(interruption), transaction:
            pytest.fail("cancelled transaction body entered")
    else:
        with transaction as (connection, cursor):
            assert cursor.connection is connection
            cursor.execute("SELECT 1")
            assert capability._consumed
            assert capability._transaction is None
    assert resources.pool_manager.business_lease_count == 0
    # Re-entry cannot reuse either a completed or a pre-bind cancelled seal.
    with pytest.raises(SourceContractError, match="unsupported_capability"), transaction:
        pytest.fail("re-entered an already spent source transaction")
    with resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT) as (_, cursor), pytest.raises(SourceContractError):
        cursor.execute("UPDATE memplex_facts SET data='{}'")


@pytest.mark.parametrize("mismatch", ["transaction", "connection", "cursor", "resource"])
def test_source_transaction_capability_rejects_wrong_physical_binding(protocol_backend, mismatch):
    from memplex.storage.pool import _SourceTransactionAdmission
    from tests.test_source_v1_pg_transactions import SCOPE

    resources, store, _, issuer, hooks = protocol_backend
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    manager = resources.pool_manager
    transaction = manager.transaction(store._bind_transaction_scope, CONTEXT)
    capability = _SourceTransactionAdmission(manager, transaction, resource=resources, issuer=issuer)
    capability.claim(transaction)
    connection, cursor = object(), object()
    transaction._connection, transaction._cursor = connection, cursor
    other = manager.transaction(store._bind_transaction_scope, CONTEXT)
    if mismatch == "resource":
        manager._source_resource = object()
    try:
        with pytest.raises(SourceContractError, match="unsupported_capability"):
            capability.consume(other if mismatch == "transaction" else transaction,
                               object() if mismatch == "connection" else connection,
                               object() if mismatch == "cursor" else cursor)
        assert capability._consumed and capability._transaction is None
        with pytest.raises(SourceContractError):
            capability.consume(transaction, connection, cursor)
    finally:
        manager._source_resource = resources


def test_fake_transaction_admission_cannot_replace_a_resource_seal(protocol_backend):
    from tests.test_source_v1_pg_transactions import SCOPE

    resources, store, _, issuer, hooks = protocol_backend
    resources._install_source_versions(store, SCOPE, hooks, issuer=issuer)
    transaction = resources.pool_manager.transaction(store._bind_transaction_scope, CONTEXT)
    transaction._source_admission = SimpleNamespace(claim=lambda _: None, consume=lambda *args: pytest.fail("forged capability consumed"))
    with pytest.raises(SourceContractError, match="unsupported_capability"), transaction:
        pytest.fail("forged transaction entered")
    assert resources.pool_manager.business_lease_count == 0


@pytest.mark.parametrize("source_class", ["direct_input", "captured_input"])
def test_raw_delete_keeps_generic_source_cas(publication_backend, service, source_class):
    from memplex.auth import MemoryNotFoundError
    from memplex.source_contract import SourceClass, SourceState

    h = publication_backend
    first = _mutate(h.backend, replace(function_command(raw=True), source_class=SourceClass(source_class)))
    receipt = service.delete_source(first.ref.key, first.ref.token, authorization=CONTEXT)
    assert receipt.state is SourceState.DELETED
    after = read(h.backend, first.ref.key)
    assert after.head.state is SourceState.DELETED and not after.payload
    with pytest.raises(MemoryNotFoundError):
        service.delete_source(first.ref.key, first.ref.token, authorization=CONTEXT)


@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("source_class", ["direct_input", "captured_input"])
@pytest.mark.parametrize("route", ["share", "metadata"])
def test_raw_namespace_edits_preserve_distinct_physical_identity_and_typed_references(publication_backend, service, source_class, route):
    import json

    from memplex.source_contract import SourceClass

    h = publication_backend
    raw_command = function_command(raw=True)
    raw = _mutate(h.backend, replace(raw_command, source_class=SourceClass(source_class),
                                    payload={**raw_command.payload, "id": "physical-raw"}))
    typed_command = function_command()
    typed = _mutate(h.backend, replace(typed_command,
                                      payload={**typed_command.payload, "source_paragraphs": ["physical-raw"]}))
    if route == "share":
        service.share_with("raw", "reader-agent", authorization=CONTEXT)
    else:
        service.annotate_memories(["physical-raw"], attributes={"memplex_note": "checked"}, authorization=CONTEXT)
    current_raw = read(h.backend, raw.ref.key)
    assert json.loads(current_raw.head.payload_locator)["id"] == "physical-raw"
    assert current_raw.head.ref.token.incarnation == raw.ref.token.incarnation
    assert current_raw.head.ref.token.content_revision == raw.ref.token.content_revision + (route == "metadata")
    assert current_raw.head.ref.token.auth_revision == raw.ref.token.auth_revision + (route == "share")
    with _fence(h.backend) as session:
        assert session.resolve_id("physical-raw") == session.resolve_id("raw") == raw.ref.key
    pair = h.store._durability.load_authoritative()
    assert [row["id"] for row in pair.memory["paragraphs"]] == ["physical-raw"]
    assert service.update_memory("fn", "action", "corrected", authorization=CONTEXT).success
    final_raw, final_typed = read(h.backend, raw.ref.key), read(h.backend, typed.ref.key)
    assert json.loads(final_raw.head.payload_locator)["id"] == "physical-raw"
    assert final_raw.head.last_commit_seq == final_typed.head.last_commit_seq
    assert final_raw.payload["premise_superseded"]
    assert final_typed.payload["source_paragraphs"] == ("physical-raw",)
