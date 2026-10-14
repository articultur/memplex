"""Native adapter reads, current cache consumption and legacy compatibility."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.mcp_server import MCPServer
from memplex.context import ContextCandidate
from memplex.models import Fact, FieldValue, Function, PendingReview, QueryResult, QueryScope
from memplex.retrieval.multi_path import MultiPathRetriever
from memplex.source_contract import PolicyEntry, SourceKind, SourceOperation
from memplex.working_memory import WorkingMemory
from tests.test_factual_capture import service as legacy_service
from tests.test_source_v1_pg_transactions import CONTEXT, POLICY, _command, _mutate
from tests.test_source_v1_pg_transactions import source_backend as source_backend  # noqa: PLC0414
from tests.test_source_v1_policy import policy_authority
from tests.test_source_v1_publication import (
    publication_backend as publication_backend,  # noqa: PLC0414
)
from tests.test_source_v1_publication import publish_command

SECRET = "adapteroldsecret"
CONTROL = "adaptercurrentcontrol"


@pytest.fixture
def adapters(publication_backend, tmp_path, monkeypatch):
    h = publication_backend
    contexts = {agent: replace(CONTEXT, agent_id=agent, principal=replace(
        CONTEXT.principal, authentication_id="credential-" + agent))
        for agent in ("codex", "cli", "hermes")}
    contexts["peer"] = replace(contexts["codex"], principal=replace(
        contexts["codex"].principal, authentication_id="credential-peer"))
    entries = tuple(PolicyEntry(c.principal.authentication_id, c.principal.tenant_id,
        c.principal.subject_id, c.workspace_id, c.agent_id, c.principal.roles)
        for c in contexts.values())
    h.backend.replace_policy(1, replace(POLICY, revision=2, entries=POLICY.entries + entries), policy_authority(h))
    svc = legacy_service(tmp_path / "service")
    original_store = svc.store
    svc.store = h.store
    svc._embedding_service = svc._reranker = svc._cross_reranker = svc._llm = None
    svc._detect_scope = lambda text: QueryScope.ALL
    svc._retriever = MultiPathRetriever(h.store, embedding_service=None, wiki_searcher=None)
    svc._working_memory = WorkingMemory()
    svc._config.working_memory.enabled = True
    svc._config.working_memory.inject_limit = 8
    registry = [{"credential_id": c.principal.authentication_id,
        "token_sha256": hashlib.sha256(("token-" + key).encode()).hexdigest(),
        "tenant_id": c.principal.tenant_id, "subject_id": c.principal.subject_id,
        "workspace_id": c.workspace_id, "agent_id": c.agent_id, "roles": sorted(c.principal.roles)}
        for key, c in contexts.items()]
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps(registry))
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "token-codex")
    monkeypatch.setenv("MEMPLEX_AGENT_ID", "codex")
    monkeypatch.setenv("MEMPLEX_SESSION_ID", CONTEXT.session_id)
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    h.service, h.contexts, h.tmp_path = svc, contexts, tmp_path
    yield h
    if hasattr(h, "hermes"):
        h.hermes._owns_service = False
        h.hermes.shutdown()
    svc.store = original_store
    svc.stop()


def _put(h, memory_id="current", text=CONTROL, **fields):
    command = _command(name=memory_id, value=text)
    return _mutate(h.backend, replace(command, payload={
        **command.payload, "namespace": {"domain": "adapter", "memplex_subject_id": "alice"}, **fields}))


def _change(h, receipt, text="changed", **fields):
    command = _command(name=receipt.ref.key.logical_id, value=text,
        expected=receipt.ref.token, operation=SourceOperation.REPLACE)
    return _mutate(h.backend, replace(command, payload={
        **command.payload, "namespace": {"domain": "adapter", "memplex_subject_id": "alice"}, **fields}))


def _runtime(h, key="codex", **kwargs):
    return AgentMemoryRuntime(service=h.service, authorization=h.contexts[key], **kwargs)


def _mcp(h, name, **arguments):
    server = MCPServer()
    server._service = h.service
    response = server._handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments}})
    assert "error" not in response, response
    return json.loads(response["result"]["content"][0]["text"])


def _http(h, route, **params):
    from fastapi.testclient import TestClient

    from memplex.adapters.http_api import create_app

    app = create_app(h.service._config)
    app.state.memplex_service = h.service
    # Deliberately no lifespan: the fixture owns the real native store.
    client = TestClient(app, client=("127.0.0.1", 50000))
    try:
        response = client.get(route, params=params, headers={
            "Authorization": "Bearer token-codex", "X-Memplex-Session-ID": CONTEXT.session_id})
        assert response.status_code in {200, 404}, response.text
        return response.json()
    finally:
        client.close()


@pytest.mark.parametrize("key", ["peer"])
def test_prefetch_key_includes_complete_principal(adapters, key):
    h = adapters
    first, second = _runtime(h), _runtime(h, key)
    assert first.authorization_context.principal.subject_id == second.authorization_context.principal.subject_id
    assert first._cache_key("query") != second._cache_key("query")


@pytest.mark.parametrize("option,value", [("top_k", 2), ("token_budget", 300)])
def test_prefetch_key_includes_query_options(adapters, option, value):
    assert _runtime(adapters)._cache_key("query") != _runtime(adapters, **{option: value})._cache_key("query")


def test_hot_reference_isolation_and_current_consumption(adapters, monkeypatch):
    h = adapters
    receipt = _put(h)
    first, second = _runtime(h), _runtime(h, "peer")
    # Isolate hot nomination; final service projection and native fence stay real.
    for runtime in (first, second):
        monkeypatch.setattr(runtime, "_search_memory_candidates", lambda *a, **kw: QueryResult([], QueryScope.ALL, 0))
    h.service._publish_hot_references(("current",), context=first.authorization_context)
    assert CONTROL in first.before_prompt("unrelated").context
    assert CONTROL not in second.before_prompt("unrelated").context
    _change(h, receipt, "newhotbody")
    assert CONTROL not in first.before_prompt("unrelated").context


def test_mcp_reviews_serialize_final_projection(adapters, monkeypatch):
    h = adapters
    node = Function(id="review", name="review", action=[FieldValue(desc=SECRET)],
        tenant_id=CONTEXT.principal.tenant_id, workspace_id=CONTEXT.workspace_id,
        owner_subject_id="alice", visibility="workspace", namespace={"domain": "adapter"})
    command = replace(_command(name="review", kind=SourceKind.FUNCTION), payload=node.to_dict())
    receipt = _mutate(h.backend, command)
    monkeypatch.setattr(h.service._feedback_store_for(h.contexts["codex"]), "get_pending",
        lambda: [PendingReview("review", "action", [FieldValue(desc="cachedreviewsecret")])])
    original = h.service.get_pending_reviews

    def candidates(*, authorization=None, **kwargs):
        reviews = original(authorization=authorization, **kwargs)
        current = replace(node, action=[FieldValue(desc=CONTROL)])
        _mutate(h.backend, replace(command, expected=receipt.ref.token,
            operation=SourceOperation.REPLACE, payload=current.to_dict()))
        return reviews

    monkeypatch.setattr(h.service, "get_pending_reviews", candidates)
    result = _mcp(h, "memory_pending_reviews")
    assert result["total"] == 1
    assert CONTROL in json.dumps(result)
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("surface", ["http_query", "http_get", "mcp_search", "mcp_get", "mcp_facts",
    "runtime_search", "before_prompt", "prefetch", "cli_query", "cli_get", "cli_facts", "hermes"])
def test_native_adapters_drop_stale_derivation_keep_control(adapters, surface, monkeypatch, capsys):
    h = adapters
    source = _put(h, "source", "adapterquery source")
    _put(h, "control", "adapterquery " + CONTROL)
    command = publish_command(h.backend, source, value="adapterquery " + SECRET)
    derived = h.backend.publish_if_current(CONTEXT, command,
        deadline_ns=time.monotonic_ns() + 2_000_000_000).publication_id
    runtime = _runtime(h)

    def read(memory_id=derived):
        return _surface(h, surface, monkeypatch, capsys, memory_id=memory_id, runtime=runtime)

    assert SECRET in str(read())
    _change(h, source, "adapterquery updated source")
    result = str(read())
    assert SECRET not in result
    if surface in {"http_get", "mcp_get", "cli_get"}:
        result = str(read("control"))
    assert CONTROL in result


def test_prefetch_consumption_revalidates_after_source_change(adapters):
    h = adapters
    source = _put(h, text=SECRET)
    runtime = _runtime(h)
    h.service._context_prefetch_cache.put(runtime._cache_key("query"), (ContextCandidate("current", "retrieval"),))
    _change(h, source, CONTROL)
    result = runtime.before_prompt("query")
    assert result.source == "prefetch" and CONTROL in result.context and SECRET not in result.context


@pytest.mark.parametrize("surface", ["http_query", "http_get", "mcp_search", "mcp_get", "mcp_facts",
    "runtime_search", "before_prompt", "prefetch", "cli_query", "cli_get", "cli_facts", "hermes"])
@pytest.mark.parametrize("mismatch", ["session", "origin"])
def test_session_mismatch_denied_across_adapters(adapters, surface, mismatch, monkeypatch, capsys):
    h = adapters
    agent = "cli" if surface.startswith("cli_") else "hermes" if surface == "hermes" else "codex"
    source = _put(h, text="adapterquery " + SECRET, visibility="session",
        origin_session=CONTEXT.session_id, provenance={"agent_id": agent})
    _put(h, "control", "adapterquery " + CONTROL)
    assert SECRET in str(_surface(h, surface, monkeypatch, capsys))
    _change(h, source, "adapterquery " + SECRET, visibility="session",
        origin_session="other-session" if mismatch == "session" else CONTEXT.session_id,
        provenance={"agent_id": "other-agent" if mismatch == "origin" else agent})
    result = str(_surface(h, surface, monkeypatch, capsys))
    assert SECRET not in result
    if surface in {"http_get", "mcp_get", "cli_get"}:
        result = str(_surface(h, surface, monkeypatch, capsys, memory_id="control"))
    assert CONTROL in result


def test_unbound_development_markers_are_rejected_but_legacy_remains(tmp_path, monkeypatch):
    from memplex.source_contract import CARRIER_KEY, SourceContractError

    svc = legacy_service(tmp_path)
    try:
        legacy = Fact(id="ordinary", subject="item", predicate="is", object_=CONTROL)
        svc.store.add_fact(legacy)
        marked = Fact(id="marked", subject="item", predicate="is", object_=SECRET,
            namespace={CARRIER_KEY: "malformed"})
        with pytest.raises(SourceContractError):
            svc.store.add_fact(marked)
        lookup = svc.store.get_fact
        monkeypatch.setattr(svc.store, "get_fact", lambda memory_id: marked if memory_id == "marked" else lookup(memory_id))
        assert svc.get("ordinary").object_ == CONTROL
        assert svc.get("marked") is None
        assert SECRET not in str(svc.list_facts(include_invalidated=True))
    finally:
        svc.stop()


def _cli(h, surface, monkeypatch, capsys, *, memory_id="current", query="adapterquery"):
    from memplex.adapters import cli

    with monkeypatch.context() as patch:
        patch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "token-cli")
        patch.setattr(cli, "_make_service", lambda _path: h.service)
        patch.setattr(h.service, "stop", lambda: None)
        args = argparse.Namespace(config=None, text=query, memory_id=memory_id, top_k=10,
            max_tokens=4000, explain=False, output="json", as_of="1990-01-01", all=True, limit=100)
        code = getattr(cli, "cmd_" + surface.removeprefix("cli_"))(args)
        output = capsys.readouterr()
        return json.loads(output.out) if code == 0 else {"error": output.err}


def _hermes(h, monkeypatch):
    from tests.test_hermes_memory_provider import _install, _load_plugin

    if not hasattr(h, "hermes"):
        _, _, plugin = _install(h.tmp_path / "hermes-host")
        module, _ = _load_plugin(plugin, h.tmp_path, "t11_hermes_native")
        provider = module.MemplexMemoryProvider(service_factory=lambda: h.service)
        provider._config = {"user_id": "alice", "project_path": CONTEXT.workspace_id}
        provider._hermes_home = str(h.tmp_path / "hermes-status")
        h.hermes = provider
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", "token-hermes")
    return h.hermes


def test_cache_maintenance_failure_preserves_committed_outcome(adapters, monkeypatch):
    from memplex.source_contract import SourceBusyError

    h = adapters
    source = _put(h)
    def unavailable(_context):
        raise SourceBusyError()
    monkeypatch.setattr(h.service, "_context_reference_namespace", unavailable)
    h.service._publish_hot_references(("current",), context=h.contexts["codex"])
    assert not h.service._working_memory._references
    replacement = {**_command(name="current", value="durablecurrent").payload,
                   "namespace": {"memplex_subject_id": "alice"}}
    committed = h.service.replace_source(source.ref.key, source.ref.token, replacement,
        authorization=h.contexts["codex"])
    assert committed.ref.token.content_revision == source.ref.token.content_revision + 1
    assert h.service.get("current", authorization=h.contexts["codex"]).object_ == "durablecurrent"
    with pytest.raises(SourceBusyError):
        _runtime(h).before_prompt("current")


@pytest.mark.parametrize("cache_failure", [False, True])
def test_runtime_capture_cache_failure_preserves_publication(adapters, monkeypatch, cache_failure):
    from memplex.source_contract import SourceBusyError
    from tests.test_source_v1_capture import A, B, CaptureSpy, extracted_input

    h = adapters
    spy = CaptureSpy(h.service._config.llm)
    h.service._llm = spy
    inputs = extracted_input()
    inputs.source_contract_receipt = {"contract_version": 1, "publications": ["MODEL_FORGED_RECEIPT"]}
    h.service._engine = SimpleNamespace(extract=lambda source: deepcopy(inputs))
    monkeypatch.setattr("memplex.service.CoreEngine", lambda **kwargs: h.service._engine)
    runtime = _runtime(h)
    def unavailable(_context):
        raise SourceBusyError()
    if cache_failure:
        monkeypatch.setattr(h.service, "_context_reference_namespace", unavailable)
    result = runtime.write_text(A + " " + B)
    assert len(spy.calls) == 1
    assert result.factual_capture["accepted"] == 1
    assert result.source_contract_receipt is not None
    derived = [fact for fact in result.facts if fact.id.startswith("fact_derived_v1_")]
    assert len(derived) == 1
    current = h.service.get(derived[0].id, authorization=h.contexts["codex"])
    assert current is not None
    assert current.tenant_id == CONTEXT.principal.tenant_id
    assert current.owner_subject_id == current.owner == "alice"
    assert current.workspace_id == CONTEXT.workspace_id and current.visibility == "workspace"
    assert current.origin_session == CONTEXT.session_id
    assert current.namespace["memplex_subject_id"] == "alice"
    assert current.namespace["memplex_workspace_id"] == CONTEXT.workspace_id
    assert current.provenance["agent_id"] == "codex"
    assert current.provenance["session_id"] == CONTEXT.session_id
    assert current.trust_tier == 1 and h.service.is_safe_for_model(current)
    assert "MODEL_FORGED_RECEIPT" not in json.dumps(result.source_contract_receipt)
    if not cache_failure:
        assert "PostgreSQL" in runtime.before_prompt("unrelated hot recall").context


def test_mcp_observations_resolve_current_bodies(adapters, monkeypatch):
    from memplex.models import Observation

    h = adapters
    node = Observation(id="observation", category="note", event=CONTROL, context=CONTROL,
        owner="alice", owner_subject_id="alice", tenant_id=CONTEXT.principal.tenant_id,
        workspace_id=CONTEXT.workspace_id, visibility="workspace", namespace={"memplex_subject_id": "alice"})
    command = replace(_command(name=node.id, kind=SourceKind.OBSERVATION), payload=node.to_dict())
    receipt = _mutate(h.backend, command)
    assert CONTROL in str(_mcp(h, "memory_observations"))
    _mutate(h.backend, replace(command, operation=SourceOperation.REPLACE, expected=receipt.ref.token,
        payload=replace(node, event="observationcurrent", context="observationcurrent").to_dict()))
    result = _mcp(h, "memory_observations")
    assert "observationcurrent" in str(result) and CONTROL not in str(result)


def test_cold_runtime_and_unadmitted_restart_fail_closed(adapters):
    from memplex.storage.lite.durability import LiteStorageIntegrityError
    from memplex.storage.lite.store import LiteMemoryStore

    h = adapters
    source = _put(h, text="adapterquery " + SECRET)
    assert SECRET in _runtime(h).before_prompt("adapterquery").context
    _change(h, source, "adapterquery " + CONTROL)
    h.service._context_prefetch_cache.invalidate()
    h.service._working_memory.clear()
    current = _runtime(h).before_prompt("adapterquery")
    assert CONTROL in current.context and SECRET not in current.context
    if h.kind == "lite":
        # T12 owns trusted restart admission. An ordinary off-mode constructor
        # must keep refusing an already-V1 bank, even after caches are gone.
        with pytest.raises(LiteStorageIntegrityError, match="trusted source read session"):
            LiteMemoryStore(h.store._path)


def _surface(h, surface, monkeypatch, capsys, *, memory_id="current", runtime=None):
    runtime = runtime or _runtime(h)
    if surface == "http_query":
        return _http(h, "/memories", q="adapterquery")
    if surface == "http_get":
        return _http(h, "/memories/" + memory_id, historical=True, include_invalidated=True)
    if surface == "mcp_search":
        return _mcp(h, "memory_search", query="adapterquery")
    if surface == "mcp_get":
        return _mcp(h, "memory_get", memory_id=memory_id, historical=True)
    if surface == "mcp_facts":
        return _mcp(h, "memory_facts", include_invalidated=True, as_of="1990-01-01")
    if surface == "runtime_search":
        return runtime.search_memories("adapterquery")
    if surface.startswith("cli_"):
        return _cli(h, surface, monkeypatch, capsys, memory_id=memory_id)
    if surface == "hermes":
        return _hermes(h, monkeypatch).prefetch("adapterquery", session_id=CONTEXT.session_id)
    return getattr(runtime, surface)("adapterquery")


@pytest.mark.parametrize("surface", ["http_get", "mcp_get", "mcp_facts", "cli_get", "cli_facts"])
def test_historical_flag_never_restores_expired_v1(adapters, surface, monkeypatch, capsys):
    h = adapters
    _put(h, text=SECRET, invalid_at="2000-01-01T00:00:00+00:00")
    _put(h, "control")
    assert SECRET not in str(_surface(h, surface, monkeypatch, capsys))
    assert h.service.get("control", authorization=h.contexts["codex"]).object_ == CONTROL


def test_legacy_historical_json_and_cache_key_stay_compatible(tmp_path):
    from memplex.context import ContextCacheKey
    from memplex.serialization import dataclass_to_dict

    svc = legacy_service(tmp_path)
    try:
        fact = Fact(id="legacy", subject="item", predicate="is", object_=CONTROL,
            invalid_at="2000-01-01T00:00:00+00:00")
        svc.store.add_fact(fact)
        assert [node.id for node in svc.list_facts(include_invalidated=True)] == ["legacy"]
        assert svc.list_facts() == []
        detail = dataclass_to_dict(svc.get("legacy"))
        assert detail["object_"] == CONTROL
        assert "source_receipt" not in detail and "source_contract_receipt" not in detail
        context = CONTEXT
        runtime = AgentMemoryRuntime(service=svc, authorization=context)
        assert runtime._cache_key("  query \n text ") == ContextCacheKey(
            svc.storage_namespace(), context.principal.tenant_id, context.principal.subject_id,
            context.workspace_id, context.agent_id, context.session_id, "query text")
    finally:
        svc.stop()


def test_two_native_lite_authorities_partition_same_logical_ids(tmp_path, monkeypatch):
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.source_versions import (
        _RESOURCE_ISSUER,
        SourceValidationHooks,
        _issue_admission,
    )
    from tests.test_source_v1_pg_transactions import SCOPE

    svc = legacy_service(tmp_path / "service")
    original = svc.store
    stores = []
    keys = []
    try:
        for number in (1, 2):
            store = LiteMemoryStore(tmp_path / str(number) / "memory.json")
            stores.append(store)
            backend = store.source_versions()
            scope = replace(SCOPE, authority_id=f"{number}1111111-1111-4111-8111-111111111111")
            hooks = SourceValidationHooks(lambda snapshot, context: True, lambda snapshot: True)
            backend.initialize_authority(scope, POLICY, admission=_issue_admission(
                store._durability, scope, hooks, issuer=_RESOURCE_ISSUER))
            svc.store = store
            monkeypatch.setattr(svc, "storage_namespace", lambda: "same-physical-cache-name")
            keys.append(AgentMemoryRuntime(service=svc, authorization=CONTEXT)._cache_key("query"))
        assert keys[0] != keys[1]
    finally:
        svc.store = original
        svc.stop()


def test_model_forged_receipt_cannot_skip_legacy_stamping(tmp_path, monkeypatch):
    from tests.test_source_v1_capture import B, extracted_input

    svc = legacy_service(tmp_path)
    svc._config.llm.factual_capture = False
    svc._llm = None
    inputs = extracted_input(both=False)
    inputs.source_contract_receipt = {"contract_version": 1, "complete": True}
    svc._engine = SimpleNamespace(extract=lambda source: deepcopy(inputs))
    runtime = AgentMemoryRuntime(service=svc, authorization=CONTEXT)
    try:
        result = runtime.write_text(B)
        assert result.source_contract_receipt == inputs.source_contract_receipt
        stored = svc.get(result.facts[0].id, authorization=CONTEXT)
        assert stored.namespace["memplex_user_id"] == "alice"
        assert stored.namespace["memplex_source_session_id"] == CONTEXT.session_id
        assert stored.namespace["memplex_storage_namespace"] == svc.storage_namespace()
    finally:
        svc.stop()


@pytest.mark.parametrize("surface", ["http_reviews", "mcp_reviews", "mcp_observations"])
@pytest.mark.parametrize("mismatch", ["session", "origin"])
def test_collection_origin_acl_preserves_independent_control(adapters, surface, mismatch, monkeypatch):
    from memplex.models import Observation

    h = adapters
    receipts = []
    commands = []
    for identifier, body in (("restricted", SECRET), ("control", CONTROL)):
        identity = {"id": identifier, "tenant_id": CONTEXT.principal.tenant_id,
            "owner": "alice", "owner_subject_id": "alice", "workspace_id": CONTEXT.workspace_id,
            "visibility": "session" if identifier == "restricted" else "workspace",
            "origin_session": CONTEXT.session_id, "provenance": {"agent_id": "codex"},
            "namespace": {"memplex_subject_id": "alice"}}
        if surface == "mcp_observations":
            node = Observation(**identity, category="note", event=body, context=body)
            kind = SourceKind.OBSERVATION
        else:
            node = Function(**identity, name=identifier, action=[FieldValue(desc=body)])
            kind = SourceKind.FUNCTION
        command = replace(_command(name=identifier, kind=kind), payload=node.to_dict())
        commands.append(command)
        receipts.append(_mutate(h.backend, command))
    if surface != "mcp_observations":
        monkeypatch.setattr(h.service._feedback_store_for(h.contexts["codex"]), "get_pending",
            lambda: [PendingReview(name, "action", []) for name in ("restricted", "control")])
    def read():
        if surface == "http_reviews":
            return _http(h, "/memories/pending_reviews")
        return _mcp(h, "memory_observations" if surface == "mcp_observations" else "memory_pending_reviews")
    assert SECRET in str(read()) and CONTROL in str(read())
    command = commands[0]
    _mutate(h.backend, replace(command, expected=receipts[0].ref.token, operation=SourceOperation.REPLACE,
        payload={**command.payload, "origin_session": "other" if mismatch == "session" else CONTEXT.session_id,
                 "provenance": {"agent_id": "other" if mismatch == "origin" else "codex"}}))
    result = str(read())
    assert SECRET not in result and CONTROL in result


def test_http_detail_is_detached_and_timeline_has_no_body(adapters):
    h = adapters
    _put(h, text=SECRET)
    detail = _http(h, "/memories/current")
    assert detail["object_"] == SECRET
    detail["object_"] = "CALLER MUTATION"
    detail["namespace"]["memplex_subject_id"] = "intruder"
    assert h.service.get("current", authorization=h.contexts["codex"]).object_ == SECRET
    timeline = _http(h, "/memories/current/timeline")
    assert timeline and SECRET not in str(timeline)
    assert set(timeline[0]) == {"state", "incarnation", "content_revision", "auth_revision", "commit_seq"}
    assert "source_receipt" not in detail and "source_contract_receipt" not in detail


def test_cached_candidates_remain_references_and_policy_revocation_denies(adapters):
    from memplex.source_contract import SourceContractError

    h = adapters
    _put(h, text="adapterquery " + CONTROL)
    runtime = _runtime(h)
    runtime.prefetch("adapterquery")
    assert h.service._context_prefetch_cache._entries
    for candidates in h.service._context_prefetch_cache._entries.values():
        assert all(type(candidate) is ContextCandidate for candidate in candidates)
        assert CONTROL not in repr(candidates)
    h.service._publish_hot_references(("current",), context=runtime.authorization_context)
    assert h.service._working_memory._references
    assert CONTROL not in repr(h.service._working_memory._references)
    with h.backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        policy = session.authority().policy
    denied = replace(policy, revision=3, entries=tuple(entry for entry in policy.entries
        if entry.authentication_id != "credential-codex"))
    h.backend.replace_policy(2, denied, policy_authority(h))
    with pytest.raises(SourceContractError, match="denied"):
        runtime.before_prompt("adapterquery")


class _AdapterServiceFacade:
    """Delegate real service behavior while modeling an incomplete integration."""

    def __init__(self, service, helper=None, *, noncallable=False):
        self.service, self.helper, self.noncallable = service, helper, noncallable

    def __getattr__(self, name):
        if name == self.helper:
            if self.noncallable:
                return None
            raise AttributeError(name)
        return getattr(self.service, name)


@pytest.mark.parametrize("facade", ["native", "missing", "noncallable"])
def test_round1_mcp_missing_helper_never_releases_captured_review(adapters, monkeypatch, facade):
    h = adapters
    service = h.service
    node = Function(id="review", name="review", action=[FieldValue(desc=SECRET)],
        tenant_id=CONTEXT.principal.tenant_id, workspace_id=CONTEXT.workspace_id,
        owner_subject_id="alice", visibility="workspace", namespace={"domain": "adapter"})
    command = replace(_command(name="review", kind=SourceKind.FUNCTION), payload=node.to_dict())
    receipt = _mutate(h.backend, command)
    monkeypatch.setattr(service._feedback_store_for(h.contexts["codex"]), "get_pending",
        lambda: [PendingReview("review", "action", [])])
    original = service.get_pending_reviews
    reached = []

    def candidates(*, authorization=None, **kwargs):
        reviews = original(authorization=authorization, **kwargs)
        assert SECRET in str(reviews)
        reached.append(_mutate(h.backend, replace(command, expected=receipt.ref.token,
            operation=SourceOperation.REPLACE, payload=replace(node, action=[FieldValue(desc=CONTROL)]).to_dict())))
        return reviews

    monkeypatch.setattr(service, "get_pending_reviews", candidates)
    if facade != "native":
        h.service = _AdapterServiceFacade(service, "_v1_source_store", noncallable=facade == "noncallable")
    result = _mcp(h, "memory_pending_reviews")
    assert len(reached) == 1
    assert SECRET not in json.dumps(result)
    if facade == "native":
        assert result["total"] == 1 and CONTROL in json.dumps(result)
    else:
        assert result == {"total": 0, "reviews": []}


@pytest.mark.parametrize("noncallable", [False, True])
@pytest.mark.parametrize("variant", ["credential", "top_k", "token_budget"])
def test_round1_missing_cache_helper_cannot_select_legacy_key(adapters, variant, noncallable):
    from memplex.source_contract import SourceContractError

    h = adapters
    _put(h, text="adapterquery " + CONTROL)
    first = _runtime(h)
    second = (_runtime(h, "peer") if variant == "credential" else
              _runtime(h, **{variant: getattr(first, variant) + 1}))
    assert first._cache_key("adapterquery") != second._cache_key("adapterquery")
    assert CONTROL in first.before_prompt("adapterquery").context
    h.service = _AdapterServiceFacade(h.service, "_context_reference_namespace", noncallable=noncallable)
    for runtime in (_runtime(h), _runtime(h, "peer")):
        with pytest.raises(SourceContractError, match="unsupported_capability"):
            runtime._cache_key("adapterquery")
        with pytest.raises(SourceContractError, match="unsupported_capability"):
            runtime.before_prompt("adapterquery")


def test_round1_actual_legacy_facade_keeps_key_and_json(tmp_path):
    from memplex.context import ContextCacheKey
    from memplex.serialization import dataclass_to_dict

    service = legacy_service(tmp_path)
    try:
        service.store.add_fact(Fact(id="legacy", subject="item", predicate="is", object_=CONTROL))
        facade = _AdapterServiceFacade(service)
        runtime = AgentMemoryRuntime(service=facade, authorization=CONTEXT)
        assert runtime._cache_key("query") == ContextCacheKey(
            service.storage_namespace(), CONTEXT.principal.tenant_id, CONTEXT.principal.subject_id,
            CONTEXT.workspace_id, CONTEXT.agent_id, CONTEXT.session_id, "query")
        result = dataclass_to_dict(facade.get("legacy"))
        assert result["object_"] == CONTROL
        assert "source_receipt" not in result and "source_contract_receipt" not in result
    finally:
        service.stop()
