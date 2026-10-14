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
    from memplex.config import SourceContractConfig
    from tests.test_source_v1_pg_transactions import SCOPE
    service._config.source_contract = SourceContractConfig("v1", SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    service._config.storage.backend = "lite" if h.kind == "lite" else "postgres"
    service._config.storage.path = str(h.store._path.parent) if h.kind == "lite" else h.dsn
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


@pytest.mark.parametrize("controlled", [False, True])
def test_task_repository_binds_real_scope_before_ordinary_source_admission(controlled):
    import re

    from memplex.storage.pool import _source_cursor_admission
    from memplex.storage.postgres_tasks import _TASK_CONTEXT, PostgresTaskRepository

    settings, statements = {}, []
    row = None

    def execute(sql, parameters=()):
        nonlocal row
        statements.append((sql, parameters))
        keys = re.findall(r"set_config\('([^']+)', %s, true\)", sql)
        if keys:
            assert len(keys) == len(parameters)
            settings.update(zip(keys, parameters))
        if "AS has_source_authority" in sql:
            # Model actual transaction GUCs, not the lookup's requested scope.
            row = (settings.get("memplex.tenant_id"), settings.get("memplex.workspace_id"), controlled)

    cursor = SimpleNamespace(execute=execute, fetchone=lambda: row)
    PostgresTaskRepository._bind_task_scope(cursor, _TASK_CONTEXT)
    admitted = _source_cursor_admission(SimpleNamespace(_source_resource=None), cursor, _TASK_CONTEXT)
    assert settings == {
        "memplex.tenant_id": "memplex-system", "memplex.subject_id": "background-worker",
        "memplex.workspace_id": "memplex-system", "memplex.agent_id": "background-worker",
        "memplex.session_id": "",
    }
    if controlled:
        with pytest.raises(SourceContractError, match="unsupported_semantics"):
            admitted.execute("UPDATE memplex_facts SET data='{}'")
        assert statements[-1][0] == "SET TRANSACTION READ ONLY"
    else:
        assert admitted is cursor
        admitted.execute("SELECT task_id FROM memplex_background_tasks")


# Task12's bounded diagnostic is confined to this exact test invocation.
_TASK12_OBSERVED_ID = (
    "tests/test_source_v1_mutation_routes.py::"
    "test_raw_namespace_edits_preserve_distinct_physical_identity_and_typed_references"
    "[share-captured_input-lite]"
)


class _Task12TimeProxy:
    def __init__(self, original, record):
        self.original = original
        self.record = record
        self.record_failed = False

    def __getattr__(self, name):
        return getattr(self.original, name)

    def monotonic_ns(self):
        value = self.original.monotonic_ns()
        try:
            self.record(value)
        except Exception:  # noqa: BLE001 - diagnostic failure cannot replace the real clock result
            # Observation must never replace the original clock's outcome.
            self.record_failed = True
        return value


class _Task12ShareObserver:
    """Finite observer for one test call, not a production tracing facility."""

    def __init__(self, backend):
        import threading
        import time

        self.backend = backend
        self.durability = backend._durability
        self.time = time
        self.threading = threading
        self.main_thread = threading.get_ident()
        self.local = threading.local()
        self.running = False
        self.command_id = self.deadline = None
        self.base_id = self.target_id = None
        self.events = []
        self.counts = {}
        self.clock_counts = {}
        self.clock_last = {}
        self.incomplete = set()
        self.sequence = 0
        self.recorder_perf_ns = 0
        self.service_clock_values = []
        self.proxies = []
        self.clock_origins = {}

    def _active(self):
        return self.running and (self.threading.get_ident() == self.main_thread
                                 or getattr(self.local, "bound", False))

    def _safe(self, operation, *args):
        try:
            return operation(*args)
        except Exception:  # noqa: BLE001 - recorder failure is data, never the product outcome
            self.incomplete.add("recorder_error")
            return None

    def _event(self, record):
        start = self.time.perf_counter_ns()
        try:
            limit = 512 if record.get("memory") == "base" else 480
            if len(self.events) >= limit:
                self.incomplete.add("event_limit")
                return
            self.events.append(record)
        finally:
            self.recorder_perf_ns += self.time.perf_counter_ns() - start

    def _begin(self, stage, deadline=None, memory=None):
        self.sequence += 1
        stack = getattr(self.local, "stack", None)
        if stack is None:
            stack = self.local.stack = []
        memory_label = "base" if memory == self.base_id and memory is not None else (
            "target" if memory == self.target_id and memory is not None else "read_or_session")
        if memory is None and stack:
            memory_label = stack[-1]["memory"]
        key = stage + ":" + memory_label
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] > 16:
            self.incomplete.add("stage_limit")
            return None
        if len(stack) >= 32:
            self.incomplete.add("stage_depth")
            return None
        token = {"stage": stage, "occurrence": self.sequence, "memory": memory_label,
                 "deadline_ns": deadline, "thread": self.threading.get_ident(),
                 "perf_ns": self.time.perf_counter_ns(), "cpu_ns": self.time.thread_time_ns(),
                 "checks": 0, "decoded": False}
        stack.append(token)
        if stage == "reconcile_memory":
            if not hasattr(self.local, "reconciles"):
                self.local.reconciles = []
            self.local.reconciles.append(token)
        self._event({**token, "event": "enter"})
        return token

    def _end(self, token, error):
        if token is None:
            return
        now = self.time.perf_counter_ns()
        same_thread = token["thread"] == self.threading.get_ident()
        cpu = self.time.thread_time_ns() - token["cpu_ns"] if same_thread else None
        stack = getattr(self.local, "stack", [])
        if not stack or stack[-1] is not token:
            self.incomplete.add("stage_binding")
        else:
            stack.pop()
        if token["stage"] == "reconcile_memory" and same_thread:
            if self.local.reconciles and self.local.reconciles[-1] is token:
                self.local.reconciles.pop()
            else:
                self.incomplete.add("reconcile_binding")
        self._event({"event": "exit", "stage": token["stage"], "occurrence": token["occurrence"],
                     "memory": token["memory"], "thread": self.threading.get_ident(),
                     "perf_span_ns": now - token["perf_ns"], "same_thread_cpu_ns": cpu,
                     "exception": error})

    def _invoke(self, stage, original, args, kwargs, *, deadline=None, memory=None):
        if not self._active():
            return original(*args, **kwargs)
        token = self._safe(self._begin, stage, deadline, memory)
        error = None
        try:
            return original(*args, **kwargs)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            self._safe(self._end, token, error)

    def _reconcile(self):
        stack = getattr(self.local, "reconciles", ())
        return stack[-1] if stack else None

    def _clock(self, module, value):
        if not self._active():
            return
        self.clock_counts[module] = self.clock_counts.get(module, 0) + 1
        self.clock_last[module] = value
        check = getattr(self.local, "check", None)
        if check is not None:
            token, ordinal, deadline = check
            self._event({"event": "existing_clock", "module": module, "value_ns": value,
                         "stage": "reconcile_entry_check" if ordinal == 1 else "reconcile_after_decode_check",
                         "occurrence": token["occurrence"], "memory": token["memory"],
                         "deadline_ns": deadline, "remaining_ns": None if deadline is None else deadline - value,
                         "deadline_kind": "mutation" if deadline == self.deadline else "read_or_other",
                         "thread": self.threading.get_ident()})
        elif module == "service" and len(self.service_clock_values) < 16:
            self.service_clock_values.append(value)
            self._event({"event": "service_existing_clock", "value_ns": value})
        # Traversal clocks only update the four fixed counters/last values.

    def _check_wrapper(self, original):
        def check(deadline):
            token = self._reconcile() if self._active() else None
            if token is None:
                return original(deadline)
            token["checks"] += 1
            ordinal = token["checks"]
            if ordinal > 2:
                return original(deadline)
            if token["decoded"] != (ordinal == 2):
                self.incomplete.add("reconcile_boundary")
            previous = getattr(self.local, "check", None)
            self.local.check = (token, ordinal, deadline)
            try:
                return original(deadline)
            finally:
                self.local.check = previous
        return check

    def _decode_wrapper(self, original):
        def decode(*args, **kwargs):
            result = self._invoke("decode_ledger", original, args, kwargs)
            token = self._reconcile() if self._active() else None
            if token is not None:
                token["decoded"] = True
            return result
        return decode

    def _plain_wrapper(self, stage, original, *, memory=False):
        def call(*args, **kwargs):
            if stage in {"_check_lite_live", "_read_lite_marker"} and args[0] is not self.durability:
                return original(*args, **kwargs)
            label = stage
            if stage == "_read" and args:
                label = next(("read_" + name for name in ("memory", "changelog", "journal")
                              if args[0] == getattr(self.durability, "_" + name + "_path", None)), "read_other")
            return self._invoke(label, original, args, kwargs,
                                deadline=kwargs.get("deadline_ns"),
                                memory=id(args[0]) if memory and args else None)
        return call

    def _mutation_wrapper(self, original, *, worker):
        def call(*args, **kwargs):
            deadline = args[2] if worker else kwargs["deadline_ns"]
            command_id = id(args[1])
            if worker:
                matches = command_id == self.command_id and deadline == self.deadline
                previous = getattr(self.local, "bound", False)
                self.local.bound = matches
            else:
                self.command_id, self.deadline = command_id, deadline
                matches = True
                if not self.service_clock_values or deadline - 250_000_000 != self.service_clock_values[-1]:
                    self.incomplete.add("deadline_creation_binding")
            try:
                return self._invoke("writer_mutation" if worker else "mutate_source", original,
                                    args, kwargs, deadline=deadline) if matches else original(*args, **kwargs)
            finally:
                if worker:
                    self.local.bound = previous
        return call

    def _lock_wrapper(self, original):
        from contextlib import contextmanager

        @contextmanager
        def lock(deadline):
            token = self._safe(self._begin, "aggregate_writer_lock_acquire", deadline) if self._active() else None
            acquired = False
            try:
                with original(deadline) as value:
                    acquired = True
                    self._safe(self._end, token, None)
                    yield value
            except BaseException as exc:
                if not acquired:
                    self._safe(self._end, token, type(exc).__name__)
                raise
        return lock

    def _fence_wrapper(self, original):
        from contextlib import contextmanager

        @contextmanager
        def fence(*args, **kwargs):
            token = self._safe(self._begin, "read_fence", kwargs.get("deadline_ns")) if self._active() else None
            error = None
            try:
                with original(*args, **kwargs) as session:
                    yield session
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                self._safe(self._end, token, error)
        return fence

    def _commit_wrapper(self, original):
        def commit(base, target, **kwargs):
            if self._active():
                self.base_id, self.target_id = id(base.memory), id(target.memory)
            return self._invoke("commit_locked", original, (base, target), kwargs,
                                deadline=kwargs.get("deadline_ns"))
        return commit

    def install(self, patch):
        from memplex import service as service_module
        from memplex import source_compatibility
        from memplex.storage.lite import durability, single_writer, source_versions

        for module, label in ((service_module, "service"), (source_versions, "source_versions"),
                              (durability, "durability"), (single_writer, "single_writer")):
            self.clock_origins[label] = module.time.monotonic_ns is self.time.monotonic_ns
            if not self.clock_origins[label]:
                self.incomplete.add("nonstdlib_clock")
            proxy = _Task12TimeProxy(module.time, lambda value, name=label: self._safe(self._clock, name, value))
            self.proxies.append(proxy)
            patch.setattr(module, "time", proxy)
        for module in (source_versions, durability):
            patch.setattr(module, "_check_read_deadline", self._check_wrapper(module._check_read_deadline))
        for module in (source_versions, source_compatibility):
            patch.setattr(module, "decode_ledger", self._decode_wrapper(module.decode_ledger))
        for module, name, memory in ((source_versions, "reconcile_memory", True),
                                     (source_versions, "validate_source_memory", True),
                                     (durability, "_validate_memory_payload", True),
                                     (source_versions, "_source_target", False),
                                     (source_compatibility, "_check_lite_live", False),
                                     (source_compatibility, "_read_lite_marker", False)):
            patch.setattr(module, name, self._plain_wrapper(name, getattr(module, name), memory=memory))
        for owner, name in ((self.backend, "_check_source_inputs"),
                            (self.durability, "_load_authoritative_locked"),
                            (self.durability, "_read"),
                            (self.durability, "before_journal_durable_publish")):
            patch.setattr(owner, name, self._plain_wrapper(name, getattr(owner, name)))
        patch.setattr(self.backend, "mutate_source", self._mutation_wrapper(self.backend.mutate_source, worker=False))
        patch.setattr(self.backend, "_mutate_locked", self._mutation_wrapper(self.backend._mutate_locked, worker=True))
        patch.setattr(self.backend, "read_fence", self._fence_wrapper(self.backend.read_fence))
        patch.setattr(self.durability, "writer_lock_until", self._lock_wrapper(self.durability.writer_lock_until))
        patch.setattr(self.durability, "commit_locked", self._commit_wrapper(self.durability.commit_locked))
        writer = getattr(self.durability, "_single_writer", None)
        if writer is not None:
            patch.setattr(writer, "submit_until", self._plain_wrapper("aggregate_submit", writer.submit_until))
        else:
            self.incomplete.add("writer_not_present")

    def snapshot(self, *, files):
        import os
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        threads = self.threading.enumerate()
        result = {"thread_count": len(threads),
                  "lite_writer_threads": sum(t.name == "lite-single-writer" for t in threads),
                  "process_user_cpu": usage.ru_utime, "process_system_cpu": usage.ru_stime,
                  "process_maxrss_platform_units": usage.ru_maxrss,
                  "host_load_average": os.getloadavg() if hasattr(os, "getloadavg") else None}
        writer = getattr(self.durability, "_single_writer", None)
        result["target_queue_depth"] = getattr(writer, "depth", None)
        result["target_writer_ident"] = getattr(writer, "_writer_ident", None)
        if files:
            paths = {name: getattr(self.durability, attr) for name, attr in (
                ("memory", "_memory_path"), ("changelog", "_changelog_path"), ("journal", "_journal_path"))}
            paths["marker"] = paths["memory"].with_name(paths["memory"].name + ".source-admission.json")
            result["post_call_file_sizes"] = {}
            for name, path in paths.items():
                try:
                    result["post_call_file_sizes"][name] = path.stat().st_size
                except FileNotFoundError:
                    result["post_call_file_sizes"][name] = None
        return result

    def report(self, before, after):
        import os
        import platform

        if any(proxy.record_failed for proxy in self.proxies):
            self.incomplete.add("clock_recorder_error")
        return {"schema": 1, "nodeid": _TASK12_OBSERVED_ID, "events": self.events,
                "incomplete": sorted(self.incomplete), "stage_counts": self.counts,
                "clock_counts": self.clock_counts, "clock_last": self.clock_last,
                "original_clocks_are_stdlib": self.clock_origins,
                "mutation_deadline_ns": self.deadline, "event_recorder_perf_ns": self.recorder_perf_ns,
                "before": before, "after": after, "python": platform.python_version(),
                "platform": platform.platform(), "pid": os.getpid(), "backend": type(self.backend).__name__,
                "job": os.environ.get("GITHUB_JOB"), "head_sha": os.environ.get("GITHUB_SHA"),
                "run_id": os.environ.get("GITHUB_RUN_ID"), "attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "clock_metadata": {name: vars(self.time.get_clock_info(name))
                                   for name in ("monotonic", "perf_counter", "thread_time")},
                "limits": ["worker prestart clock unobserved", "queue and lock spans are aggregate",
                           "post-call sizes are not bytes read", "recorder timing is not total perturbation",
                           "native job has a different surrounding subset workload"]}

    def run(self, original, args, kwargs, emit, *, restore=None):
        patch = pytest.MonkeyPatch()
        before = None
        try:
            before = self._safe(lambda: self.snapshot(files=False))
            try:
                self.install(patch)
            except BaseException as exc:
                self.incomplete.add("install_error")
                self._safe(patch.undo)
                if not isinstance(exc, Exception):
                    raise
            self.running = True
            return self._invoke("share", original, args, kwargs)
        finally:
            self.running = False
            self._safe(patch.undo)
            if restore is not None:
                self._safe(restore)
            try:
                after = self._safe(lambda: self.snapshot(files=True))
                self._safe(lambda: emit(self.report(before, after)))
            except BaseException:  # noqa: BLE001 - output cannot supersede the original outcome or interruption
                self.incomplete.add("output_error")


def _task12_emit(report, capsys):
    import json

    with capsys.disabled():
        print("TASK12_SHARE_OBSERVATION " + json.dumps(report, sort_keys=True), flush=True)


@pytest.fixture(autouse=True)
def _observe_task12_share(request):
    if request.node.nodeid != _TASK12_OBSERVED_ID:
        return
    service = request.getfixturevalue("service")
    harness = request.getfixturevalue("publication_backend")
    monkeypatch = request.getfixturevalue("monkeypatch")
    capsys = request.getfixturevalue("capsys")
    original = service.share_with

    def share(*args, **kwargs):
        observer = _Task12ShareObserver(harness.backend)
        return observer.run(original, args, kwargs, lambda report: _task12_emit(report, capsys),
                            restore=lambda: setattr(service, "share_with", original))

    monkeypatch.setattr(service, "share_with", share)


def test_task12_observer_clock_delegates_once_and_records_identical_value():
    values, calls = [], []
    def real_clock():
        calls.append("clock")
        return 314159265
    original = SimpleNamespace(monotonic_ns=real_clock, sleep=object())
    proxy = _Task12TimeProxy(original, values.append)
    assert proxy.monotonic_ns() == 314159265
    assert calls == ["clock"]
    assert values == [314159265]
    assert proxy.sleep is original.sleep
    assert original.monotonic_ns is real_clock


def test_task12_observer_clock_preserves_original_exception():
    failure = RuntimeError("original clock failure")
    calls, values = [], []
    def real_clock():
        calls.append("clock")
        raise failure
    proxy = _Task12TimeProxy(SimpleNamespace(monotonic_ns=real_clock), values.append)
    with pytest.raises(RuntimeError) as caught:
        proxy.monotonic_ns()
    assert caught.value is failure
    assert calls == ["clock"]
    assert values == []


def test_task12_observer_clock_recorder_error_cannot_replace_result():
    def broken_recorder(value):
        raise ValueError("observer failure")
    proxy = _Task12TimeProxy(SimpleNamespace(monotonic_ns=lambda: 7), broken_recorder)
    assert proxy.monotonic_ns() == 7
    assert proxy.record_failed


def _task12_inert_backend():
    from contextlib import contextmanager

    @contextmanager
    def scope(*args, **kwargs):
        yield object()

    durability = SimpleNamespace(
        _load_authoritative_locked=lambda **kwargs: None, _read=lambda path: None,
        before_journal_durable_publish=lambda: None, writer_lock_until=scope,
        commit_locked=lambda base, target, **kwargs: target,
        _single_writer=SimpleNamespace(submit_until=lambda fn, deadline: fn()),
    )
    return SimpleNamespace(_durability=durability, _check_source_inputs=lambda *args: None,
                           mutate_source=lambda *args, **kwargs: None,
                           _mutate_locked=lambda *args: None, read_fence=scope)


def test_task12_observer_exact_id_gate_does_not_resolve_other_fixtures():
    request = SimpleNamespace(node=SimpleNamespace(nodeid=_TASK12_OBSERVED_ID + "-other"),
                              getfixturevalue=lambda name: pytest.fail("unrelated fixture requested: " + name))
    assert _observe_task12_share.__wrapped__(request) is None


def test_task12_observer_exact_id_resolves_only_original_fixture_dependencies(monkeypatch):
    service = SimpleNamespace(share_with=lambda *args, **kwargs: None)
    original = service.share_with
    values = {"service": service, "publication_backend": SimpleNamespace(backend=_task12_inert_backend()),
              "monkeypatch": monkeypatch, "capsys": object()}
    calls = []
    def resolve(name):
        calls.append(name)
        return values[name]
    request = SimpleNamespace(node=SimpleNamespace(nodeid=_TASK12_OBSERVED_ID), getfixturevalue=resolve)
    _observe_task12_share.__wrapped__(request)
    assert calls == ["service", "publication_backend", "monkeypatch", "capsys"]
    assert service.share_with is not original


def test_task12_observer_actual_check_values_distinguish_decode_and_bound_traversal():
    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.running = True
    observer.deadline = 1000
    observer.base_id = 55
    calls = []
    def clock():
        calls.append(len(calls) + 1)
        return calls[-1] * 10
    proxy = _Task12TimeProxy(SimpleNamespace(monotonic_ns=clock),
                            lambda value: observer._clock("durability", value))
    check = observer._check_wrapper(lambda deadline: proxy.monotonic_ns())
    token = observer._begin("reconcile_memory", 1000, 55)
    assert check(1000) == 10
    sentinel = object()
    assert observer._decode_wrapper(lambda: sentinel)() is sentinel
    assert check(1000) == 20
    count = len(observer.events)
    for _ in range(5000):
        check(1000)
    assert len(observer.events) == count
    observer._end(token, None)
    clocks = [event for event in observer.events if event["event"] == "existing_clock"]
    assert [(event["stage"], event["remaining_ns"]) for event in clocks] == [
        ("reconcile_entry_check", 990), ("reconcile_after_decode_check", 980)]
    assert all(event["memory"] == "base" and event["deadline_kind"] == "mutation" for event in clocks)
    assert observer.clock_counts == {"durability": 5002}
    assert len(calls) == 5002
    assert observer.incomplete == set()
    decode_events = [event for event in observer.events if event.get("stage") == "decode_ledger"]
    assert len(decode_events) == 2 and all(event["memory"] == "base" for event in decode_events)


def test_task12_observer_read_deadline_is_not_relabelled_mutation():
    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.running = True
    observer.deadline = 900
    token = observer._begin("reconcile_memory", 500, 55)
    observer._check_wrapper(lambda deadline: observer._clock("durability", 450))(500)
    observer._end(token, None)
    event = next(event for event in observer.events if event["event"] == "existing_clock")
    assert event["remaining_ns"] == 50 and event["deadline_kind"] == "read_or_other"


def test_task12_observer_worker_identity_and_prestart_omission():
    import threading

    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.running = True
    command, other = object(), object()
    observer.command_id, observer.deadline = id(command), 500
    calls = []
    sentinel = object()
    def original(authorization, incoming, deadline):
        calls.append(incoming)
        observer._clock("source_versions", 100)
        return sentinel
    wrapped = observer._mutation_wrapper(original, worker=True)
    results = []
    def worker():
        observer._clock("single_writer", 50)  # Pre-start binding is deliberately absent.
        results.append(wrapped(None, other, 500))
        results.append(wrapped(None, command, 499))
        results.append(wrapped(None, command, 500))
        observer._clock("single_writer", 60)  # Binding has been restored.
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert results == [sentinel, sentinel, sentinel]
    assert calls == [other, command, command]
    assert observer.clock_counts == {"source_versions": 1}
    assert [event["stage"] for event in observer.events] == ["writer_mutation", "writer_mutation"]


@pytest.mark.parametrize("fail", [False, True])
def test_task12_observer_original_context_manager_yield_release_and_error(fail):
    from contextlib import contextmanager

    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.running = True
    calls, sentinel = [], object()
    failure = ValueError("original body")
    @contextmanager
    def scope(*args, **kwargs):
        calls.append("enter")
        try:
            yield sentinel
        finally:
            calls.append("release")
    def run():
        with observer._lock_wrapper(scope)(500), observer._fence_wrapper(scope)(None, deadline_ns=400) as value:
            assert value is sentinel
            if fail:
                raise failure
    if fail:
        with pytest.raises(ValueError) as caught:
            run()
        assert caught.value is failure
    else:
        run()
    assert calls == ["enter", "enter", "release", "release"]
    assert observer.local.stack == []


@pytest.mark.parametrize("failure", [None, ValueError("original"), KeyboardInterrupt("original")])
def test_task12_observer_restores_before_emit_and_preserves_outcome(failure):
    import time

    from memplex import service as service_module
    from memplex.storage.lite import durability, single_writer, source_versions

    backend = _task12_inert_backend()
    observer = _Task12ShareObserver(backend)
    observer.snapshot = lambda **kwargs: {"post_call": kwargs["files"]}
    modules = (service_module, durability, single_writer, source_versions)
    original_times = [module.time for module in modules]
    original_clock = time.monotonic_ns
    original_mutate, original_lock = backend.mutate_source, backend._durability.writer_lock_until
    calls, reports, sentinel = [], [], object()
    def original():
        calls.append("original")
        assert all(isinstance(module.time, _Task12TimeProxy) for module in modules)
        assert time.monotonic_ns is original_clock
        service_module.time.monotonic_ns()
        if failure is not None:
            raise failure
        return sentinel
    def emit(report):
        assert [module.time for module in modules] == original_times
        assert backend.mutate_source is original_mutate
        assert backend._durability.writer_lock_until is original_lock
        reports.append(report)
    if failure is None:
        assert observer.run(original, (), {}, emit) is sentinel
    else:
        with pytest.raises(type(failure)) as caught:
            observer.run(original, (), {}, emit)
        assert caught.value is failure
    assert calls == ["original"] and len(reports) == 1
    assert reports[0]["clock_counts"] == {"service": 1}
    assert time.monotonic_ns is original_clock


def test_task12_observer_record_and_emit_failures_preserve_original_result():
    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.snapshot = lambda **kwargs: None
    def broken(*args):
        raise ValueError("recorder")
    observer._event = broken
    sentinel = object()
    assert observer.run(lambda: sentinel, (), {}, broken) is sentinel
    assert "recorder_error" in observer.incomplete
    assert not observer.running


def test_task12_observer_event_limit_reserves_base_boundary():
    observer = _Task12ShareObserver(_task12_inert_backend())
    for _ in range(1000):
        observer._event({"memory": "read_or_session"})
    assert len(observer.events) == 480
    for _ in range(100):
        observer._event({"memory": "base"})
    assert len(observer.events) == 512
    assert observer.incomplete == {"event_limit"}


def test_task12_observer_cpu_delta_requires_same_thread():
    observer = _Task12ShareObserver(_task12_inert_backend())
    token = observer._begin("control")
    token["thread"] = -1
    observer._end(token, None)
    assert observer.events[-1]["same_thread_cpu_ns"] is None


def test_task12_observer_post_call_output_uses_capture_disabled(capsys):
    # This intentionally appears in the owned runner log even for a PASS.
    # It is a helper control, not an observed business node/report.
    _task12_emit({"pure_helper_control": "post_call_visibility"}, capsys)


@pytest.mark.parametrize("exhausted", [False, True])
def test_task12_observer_installed_inert_pipeline_binds_real_check_sequence(monkeypatch, exhausted):
    """Inert decode/mutate functions, no store construction or business invocation."""
    import time

    from memplex import service as service_module
    from memplex.storage.lite import durability, single_writer, source_versions

    backend = _task12_inert_backend()
    base, target = SimpleNamespace(memory={}), SimpleNamespace(memory={})
    calls, reports = [], []
    clock_value = 0
    def pure_clock():
        nonlocal clock_value
        clock_value += 1_000_000
        calls.append(clock_value)
        return clock_value
    clock = SimpleNamespace(monotonic_ns=pure_clock)
    for module in (service_module, durability, source_versions, single_writer):
        monkeypatch.setattr(module, "time", clock)
    def inert_decode(memory):
        nonlocal clock_value
        if exhausted:
            clock_value += 300_000_000
        return memory
    def inert_reconcile(memory, *, deadline_ns=None):
        source_versions._check_read_deadline(deadline_ns)
        value = source_versions.decode_ledger(memory)
        source_versions._check_read_deadline(deadline_ns)
        return value
    def inert_validate(memory, *, deadline_ns=None):
        return source_versions.reconcile_memory(memory, deadline_ns=deadline_ns)
    monkeypatch.setattr(source_versions, "decode_ledger", inert_decode)
    monkeypatch.setattr(source_versions, "reconcile_memory", inert_reconcile)
    monkeypatch.setattr(durability, "_validate_memory_payload", inert_validate)
    def inert_commit(base, target, *, deadline_ns):
        durability._validate_memory_payload(base.memory, deadline_ns=deadline_ns)
        return target
    backend._durability.commit_locked = inert_commit
    def inert_locked(auth, command, deadline):
        with backend._durability.writer_lock_until(deadline):
            return backend._durability.commit_locked(base, target, deadline_ns=deadline)
    backend._mutate_locked = inert_locked
    backend.mutate_source = lambda auth, command, *, deadline_ns: backend._mutate_locked(auth, command, deadline_ns)
    observer = _Task12ShareObserver(backend)
    observer.snapshot = lambda **kwargs: None
    command = object()
    def inert_share():
        return backend.mutate_source(None, command, deadline_ns=service_module.time.monotonic_ns() + 250_000_000)
    global_clock = time.monotonic_ns
    if exhausted:
        with pytest.raises(SourceContractError, match="budget_exhausted"):
            observer.run(inert_share, (), {}, reports.append)
    else:
        assert observer.run(inert_share, (), {}, reports.append) is target
    assert time.monotonic_ns is global_clock
    assert len(calls) == 3
    # These pure controls deliberately supplied inert clock functions above;
    # the observer must flag that provenance, not claim a real backend clock.
    assert len(reports) == 1 and reports[0]["incomplete"] == ["nonstdlib_clock"]
    assert not any(reports[0]["original_clocks_are_stdlib"].values())
    observed = [event for event in reports[0]["events"] if event["event"] == "existing_clock"]
    assert [event["value_ns"] for event in observed] == calls[1:]
    assert observed[0]["remaining_ns"] == 249_000_000
    assert observed[1]["remaining_ns"] == (-52_000_000 if exhausted else 248_000_000)
    assert all(event["memory"] == "base" and event["deadline_kind"] == "mutation" for event in observed)
    assert source_versions.reconcile_memory is inert_reconcile
    assert backend._durability.commit_locked is inert_commit


# Pure controls for the bounded C8 yield/cleanup repair; no store is constructed.
def _task12_observer_original_bindings(backend):
    from memplex import service as service_module
    from memplex import source_compatibility
    from memplex.storage.lite import durability, single_writer, source_versions

    bindings = [(module, "time") for module in (service_module, source_versions, durability, single_writer)]
    bindings += [(module, "_check_read_deadline") for module in (source_versions, durability)]
    bindings += [(module, "decode_ledger") for module in (source_versions, source_compatibility)]
    bindings += [(source_versions, "reconcile_memory"), (source_versions, "validate_source_memory"),
                 (durability, "_validate_memory_payload"), (source_versions, "_source_target"),
                 (source_compatibility, "_check_lite_live"), (source_compatibility, "_read_lite_marker"),
                 (backend, "_check_source_inputs"), (backend._durability, "_load_authoritative_locked"),
                 (backend._durability, "_read"), (backend._durability, "before_journal_durable_publish"),
                 (backend, "mutate_source"), (backend, "_mutate_locked"), (backend, "read_fence"),
                 (backend._durability, "writer_lock_until"), (backend._durability, "commit_locked"),
                 (backend._durability._single_writer, "submit_until")]
    return [(owner, name, getattr(owner, name)) for owner, name in bindings]


@pytest.mark.parametrize("fail", [False, True])
def test_task12_observer_lock_preserves_non_none_yield(fail):
    from contextlib import contextmanager

    observer = _Task12ShareObserver(_task12_inert_backend())
    observer.running = True
    calls, sentinel = [], object()
    failure = ValueError("original lock body")
    @contextmanager
    def scope(deadline):
        assert deadline == 500
        calls.append("enter")
        try:
            yield sentinel
        finally:
            calls.append("release")
    caught = None
    try:
        with observer._lock_wrapper(scope)(500) as value:
            assert value is sentinel
            if fail:
                raise failure
    except ValueError as exc:
        caught = exc
    assert caught is (failure if fail else None)
    assert calls == ["enter", "release"]
    assert observer.local.stack == []


@pytest.mark.parametrize("position", range(1, 25))
@pytest.mark.parametrize("install_error", [ValueError, KeyboardInterrupt], ids=["exception", "interrupt"])
@pytest.mark.parametrize("original_error", [None, ValueError, KeyboardInterrupt], ids=["return", "error", "interrupt"])
def test_task12_observer_partial_install_restores_every_binding(monkeypatch, position, install_error, original_error):
    backend = _task12_inert_backend()
    observer = _Task12ShareObserver(backend)
    observer.snapshot = lambda **kwargs: None
    bindings = _task12_observer_original_bindings(backend)
    assert len(bindings) == 24
    interruption = install_error("partial install")
    failure = original_error("original outcome") if original_error else None
    sentinel, calls, reports, patches, seen = object(), [], [], [], []
    real_setattr = pytest.MonkeyPatch.setattr
    def interrupted_setattr(patch, owner, name, value):
        real_setattr(patch, owner, name, value)
        patches.append(patch)
        seen.append((owner, name))
        if len(seen) == position:
            raise interruption
    monkeypatch.setattr(pytest.MonkeyPatch, "setattr", interrupted_setattr)
    def original():
        calls.append("original")
        assert all(getattr(owner, name) is value for owner, name, value in bindings)
        if failure is not None:
            raise failure
        return sentinel
    def emit(report):
        reports.append((report, all(getattr(owner, name) is value for owner, name, value in bindings)))
    result = caught = None
    try:
        try:
            result = observer.run(original, (), {}, emit)
        except BaseException as exc:  # noqa: BLE001 - compare injected interruption identity after cleanup
            caught = exc
        restored = all(getattr(owner, name) is value for owner, name, value in bindings)
    finally:
        # The RED deliberately exposes a leak; keep the pure test process isolated.
        for patch in patches:
            patch.undo()
    assert len(seen) == position
    assert restored
    assert not observer.running
    assert len(reports) == 1 and reports[0][1]
    if install_error is KeyboardInterrupt:
        assert caught is interruption and calls == [] and result is None
    else:
        assert calls == ["original"] and caught is failure
        assert result is (sentinel if failure is None else None)
        assert "install_error" in reports[0][0]["incomplete"]


@pytest.mark.parametrize("original_error", [None, ValueError, KeyboardInterrupt], ids=["return", "error", "interrupt"])
@pytest.mark.parametrize("output_error", [None, ValueError, KeyboardInterrupt], ids=["emit", "emit-error", "emit-interrupt"])
def test_task12_observer_fixture_restores_all_bindings_before_output(monkeypatch, original_error, output_error):
    backend = _task12_inert_backend()
    sentinel, calls, emitted = object(), [], []
    failure = original_error("original outcome") if original_error else None
    output_failure = output_error("observer output") if output_error else None
    def original():
        calls.append("original")
        if failure is not None:
            raise failure
        return sentinel
    service = SimpleNamespace(share_with=original)
    bindings = [*_task12_observer_original_bindings(backend), (service, "share_with", original)]
    monkeypatch.setattr(_Task12ShareObserver, "snapshot", lambda *args, **kwargs: None)
    def emit(report, capsys):
        emitted.append((report, all(getattr(owner, name) is value for owner, name, value in bindings)))
        if output_failure is not None:
            raise output_failure
    monkeypatch.setitem(globals(), "_task12_emit", emit)
    values = {"service": service, "publication_backend": SimpleNamespace(backend=backend),
              "monkeypatch": monkeypatch, "capsys": object()}
    request = SimpleNamespace(node=SimpleNamespace(nodeid=_TASK12_OBSERVED_ID), getfixturevalue=values.__getitem__)
    _observe_task12_share.__wrapped__(request)
    assert service.share_with is not original
    result = caught = None
    try:
        result = service.share_with()
    except BaseException as exc:  # noqa: BLE001 - assert the original outcome wins over output failure
        caught = exc
    assert len(emitted) == 1 and emitted[0][1]
    assert calls == ["original"] and caught is failure
    assert result is (sentinel if failure is None else None)
    assert all(getattr(owner, name) is value for owner, name, value in bindings)
