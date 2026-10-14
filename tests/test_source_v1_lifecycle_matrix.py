"""W09 lifecycle regressions on admitted native stores, with independent U.

Foreign read cells use two real authorities. They prove the combined authority
and scope boundary; one-coordinate invalid-reference controls below separately
pin the tenant/workspace publication checks. A01-A40 are frozen mappings, not
forty concurrency executions; T14 owns those named interleavings.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
import uuid
from contextlib import ExitStack, closing
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from memplex.auth import AuthorizationContext, Principal
from memplex.authorization import is_own_node_visible, source_authorization_node
from memplex.config import SourceContractConfig
from memplex.context import ContextCandidate
from memplex.models import Fact, QueryResult, QueryScope, SearchResult
from memplex.query_pipeline import QueryPipeline
from memplex.source_codec import _canonical, output_digest
from memplex.source_contract import (
    ALGORITHM_VERSION,
    PUBLICATION_PREFIX,
    AuthorityScope,
    CaptureManifest,
    DerivationEnvelope,
    ExpectedAbsent,
    PolicyEntry,
    PolicySnapshot,
    PublishCommand,
    SourceClass,
    SourceContractError,
    SourceKey,
    SourceKind,
    SourceMutation,
    SourceOperation,
    ValidationLimits,
)
from memplex.source_validation import finalize_at, validate_sources
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceValidationHooks,
    _issue_admission,
)
from tests.source_v1_case_support import load_case_manifest
from tests.test_factual_capture import service as legacy_service
from tests.test_source_v1_adapter_reads import (
    _close_case_owned_queues as _close_case_owned_queues,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_source_v1_pg_transactions import CONTEXT, POLICY, SCOPE
from tests.test_source_v1_pg_transactions import (
    source_backend as source_backend,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_source_v1_policy import policy_authority
from tests.test_source_v1_publication import (
    publication_backend as publication_backend,  # noqa: PLC0414 - pytest fixture registration
)

MANIFEST = Path(__file__).parent / "fixtures/source_v1/base_cases.json"
CASES = load_case_manifest(MANIFEST)
FROZEN_INPUTS = json.loads(MANIFEST.read_text())
INPUTS = {row["case_id"]: row for row in FROZEN_INPUTS["cases"]}
READ_BRANCHES = tuple((case, entry) for case in CASES for entry in (
    ("service", "http") if case.surface == "query" else ("service",)))
QUERY = "w09lookup"


def _context(scope, subject="alice"):
    return AuthorizationContext(Principal(scope.tenant_id, subject, frozenset({"member"}),
        "credential-" + subject), scope.workspace_id, agent_id="source-agent", session_id="session-" + subject)


def _policy(context):
    p = context.principal
    return PolicySnapshot(1, (PolicyEntry(p.authentication_id, p.tenant_id, p.subject_id,
        context.workspace_id, context.agent_id, p.roles),), frozenset({ALGORITHM_VERSION}))


def _native_reader(source, scope, path, cleanup, request):
    """One extra real native authority, only for foreign-reader cells."""
    context = _context(scope)
    cfg = SourceContractConfig("v1", scope.authority_id, scope.tenant_id, scope.workspace_id)
    hooks = SourceValidationHooks(lambda snapshot, ctx: snapshot.payload.get("tenant_id") == ctx.principal.tenant_id,
                                 lambda snapshot: snapshot.payload.get("unsafe") is not True)
    if source.kind == "lite":
        from memplex.source_compatibility import _admit_lite
        from memplex.storage.lite.store import LiteMemoryStore
        store = LiteMemoryStore(path / "memory.json")
        backend = store.source_versions()
        admission = _issue_admission(store._durability, scope, hooks, issuer=_RESOURCE_ISSUER)
        backend.initialize_authority(scope, _policy(context), admission=admission)
        _admit_lite(store, cfg, issuer=_RESOURCE_ISSUER)
        return SimpleNamespace(kind="lite", store=store, backend=backend, scope=scope, context=context)
    import psycopg2

    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore
    server_dsn = request.getfixturevalue("pg_server_dsn")
    schema = "w09_reader_" + uuid.uuid4().hex
    with closing(psycopg2.connect(server_dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')

    def drop():
        with closing(psycopg2.connect(server_dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    cleanup.callback(drop)
    dsn = psycopg2.extensions.make_dsn(server_dsn, options=f"-c search_path={schema}")
    resources = PostgresStorageResources._for_source_validity(dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    cleanup.callback(resources.close)
    resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    store = PostgresMemoryStore(dsn, ready_pool=resources.ready_pool, require_authorization=True)
    admission = resources._install_source_versions(store, scope, hooks, issuer=_RESOURCE_ISSUER)
    store.source_versions().initialize_authority(scope, _policy(context), admission=admission)
    resources._source_startup(cfg, store, issuer=_RESOURCE_ISSUER)
    return SimpleNamespace(kind="postgres", store=store, backend=store.authorized(context).source_versions(),
                           resources=resources, dsn=dsn, scope=scope, context=context)


def _service(bank, path, cleanup):
    svc = legacy_service(path)
    original = svc.store
    svc.store = bank.store
    svc._config.source_contract = SourceContractConfig("v1", bank.scope.authority_id,
                                                     bank.scope.tenant_id, bank.scope.workspace_id)
    svc._config.storage.backend = bank.kind
    svc._config.storage.path = str(bank.store._path.parent) if bank.kind == "lite" else bank.dsn
    svc._embedding_service = svc._reranker = svc._cross_reranker = svc._llm = None
    svc._detect_scope = lambda text: QueryScope.ALL
    svc._config.working_memory.enabled = True
    svc._config.working_memory.inject_limit = 8
    from memplex.working_memory import WorkingMemory
    svc._working_memory = WorkingMemory()

    def close():
        svc.store = original
        svc.stop()
    cleanup.callback(close)
    return svc


def _write(bank, name, text, *, receipt=None, operation=None, **fields):
    operation = operation or (SourceOperation.REPLACE if receipt else SourceOperation.CREATE)
    payload = None if operation is SourceOperation.DELETE else Fact(
        id=name, subject=QUERY, predicate="is", object_=text,
        tenant_id=bank.scope.tenant_id, workspace_id=bank.scope.workspace_id,
        owner_subject_id=bank.context.principal.subject_id, visibility="workspace",
        namespace={"domain": "w09", "memplex_subject_id": bank.context.principal.subject_id}).to_dict()
    if payload is not None:
        payload.update(fields)
    command = SourceMutation(SourceKey(bank.scope.authority_id, bank.scope.tenant_id,
        bank.scope.workspace_id, SourceKind.FACT, name), receipt.ref.token if receipt else ExpectedAbsent.ABSENT,
        operation, payload, SourceClass.DIRECT_INPUT, {}, ())
    return bank.backend.mutate_source(bank.context, command, deadline_ns=time.monotonic_ns() + 2_000_000_000)


def _publish(bank, source, value, *, key="d" * 64, private=False, grants="", expected=ExpectedAbsent.ABSENT):
    with bank.backend.read_fence(bank.context, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        authority = session.authority()
    output = Fact(id="candidate", subject=QUERY, predicate="is", object_=value, trust_tier=1,
        tenant_id=bank.scope.tenant_id, workspace_id=bank.scope.workspace_id,
        owner_subject_id=bank.context.principal.subject_id, visibility="user" if private else "workspace",
        source_paragraphs=[value.replace("old-body", "old-citation")],
        provenance={"source": value.replace("old-body", "old-metadata")},
        namespace={"domain": "w09", "memplex_subject_id": bank.context.principal.subject_id,
                   "memplex_grants": grants}).to_dict()
    refs = (source.ref,)
    manifest = CaptureManifest(str(uuid.uuid4()), bank.scope.authority_id, authority.policy.revision,
        authority.commit_seq, refs, {}, hashlib.sha256(_canonical(refs).encode()).hexdigest(), ALGORITHM_VERSION)
    envelope = DerivationEnvelope(1, "factual_capture", ALGORITHM_VERSION, key,
        PUBLICATION_PREFIX + uuid.uuid4().hex, str(uuid.uuid4()), 1, bank.scope.authority_id,
        authority.policy.revision, refs, (), refs, output_digest(output), 1)
    command = PublishCommand(str(uuid.uuid4()), key, expected, manifest, envelope, output)
    return command, bank.backend.publish_if_current(bank.context, command,
        deadline_ns=time.monotonic_ns() + 2_000_000_000)


def _published_ref(bank, receipt):
    with bank.backend.read_fence(bank.context, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        return SimpleNamespace(ref=session.source(session.resolve_id(receipt.publication_id)).head.ref)


def _change(source, receipt, case):
    old = receipt.ref.token
    if case.change == "expiry":
        return receipt
    if case.change == "delete":
        current = _write(source, "S", "", receipt=receipt, operation=SourceOperation.DELETE)
    elif case.change == "incarnation":
        deleted = _write(source, "S", "", receipt=receipt, operation=SourceOperation.DELETE)
        current = _write(source, "S", "source-body", receipt=deleted, operation=SourceOperation.RECREATE)
        assert current.ref.token.incarnation != old.incarnation
        with pytest.raises(SourceContractError, match="source_changed"):
            _write(source, "S", "stale-write", receipt=receipt)
    elif case.change == "auth":
        current = _write(source, "S", "source-body", receipt=receipt, visibility="user")
        assert current.ref.token.auth_revision == old.auth_revision + 1
        assert current.ref.token.content_revision == old.content_revision
    else:
        current = _write(source, "S", "source-revised", receipt=receipt)
    assert current.commit_seq > receipt.commit_seq
    if case.change != "auth":
        assert current.ref.token != old
    return current


def _nominate(svc, memory_ids, fragments, monkeypatch):
    candidates = [SearchResult(mid, fragments[2], fragments[2], .8 - index / 10,
        fragments[0] + " " + fragments[1], origin=fragments[2], graph_context={"citation": fragments[1]})
        for index, mid in enumerate(memory_ids)]
    monkeypatch.setattr(QueryPipeline, "_parallel_scope_search", lambda *args: [copy.deepcopy(candidates)])
    return candidates


def _runtime(svc, context, candidates, monkeypatch):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    runtime = AgentMemoryRuntime(service=svc, authorization=context, agent="codex")
    monkeypatch.setattr(runtime, "_search_memory_candidates",
        lambda *args, **kwargs: QueryResult(copy.deepcopy(candidates), QueryScope.ALL, 0))
    return runtime


def _prepare_runtime(svc, context, surface, ids, candidates, monkeypatch):
    runtime = _runtime(svc, context, candidates, monkeypatch)
    if surface == "hot":
        monkeypatch.setattr(runtime, "_search_memory_candidates",
            lambda *args, **kwargs: QueryResult([], QueryScope.ALL, 0))
        svc._publish_hot_references(tuple(ids), context=context)
    else:
        svc._context_prefetch_cache.put(runtime._cache_key(QUERY),
            tuple(ContextCandidate(mid, "retrieval") for mid in ids))
    return runtime


def _read(svc, context, case, entry, ids, candidates, monkeypatch, prepared=None):
    if case.surface == "query" and entry == "http":
        from fastapi.testclient import TestClient

        from memplex.adapters.http_api import create_app
        token = "w09-test-token"
        p = context.principal
        monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps([{
            "credential_id": p.authentication_id, "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "tenant_id": p.tenant_id, "subject_id": p.subject_id, "workspace_id": context.workspace_id,
            "agent_id": context.agent_id, "roles": sorted(p.roles)}]))
        monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
        monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
        app = create_app(svc._config)
        app.state.memplex_service = svc
        client = TestClient(app, client=("127.0.0.1", 50000))
        try:
            response = client.get("/memories", params={"q": QUERY}, headers={
                "Authorization": "Bearer " + token, "X-Memplex-Session-ID": context.session_id})
        finally:
            client.close()
        assert response.status_code == 200, response.text
        data = response.json()
        return [item["func_id"] for item in data["results"]], json.dumps(data)
    if case.surface == "query":
        result = svc.query(QUERY, explain=True, authorization=context)
        return [item.func_id for item in result.results], json.dumps(asdict(result), default=str)
    if case.surface == "get":
        nodes = [svc.get(mid, authorization=context) for mid in ids]
        return [node.id for node in nodes if node], json.dumps([node.to_dict() for node in nodes if node])
    if case.surface == "context":
        result = svc.assemble_context([ContextCandidate(mid, "retrieval") for mid in ids],
            authorization=context, runtime_filter=lambda node: True, max_tokens=4000)
        return list(result.memory_ids), result.context
    runtime = prepared or _prepare_runtime(svc, context, case.surface, ids, candidates, monkeypatch)
    result = runtime.before_prompt(QUERY)
    assert result.source == ("prefetch" if case.surface == "prefetch" else "live")
    assert result.total == len(case.allowed_ids)
    # Runtime intentionally returns context rather than raw IDs; independently
    # check exact accepted IDs through the same service's final assembly below.
    current = svc.assemble_context([ContextCandidate(mid, "retrieval") for mid in ids],
        authorization=context, runtime_filter=lambda node: True, max_tokens=4000)
    return list(current.memory_ids), result.context


def _restart(bank, svc, cleanup, path):
    """Trusted cold open of the same durable bank, no copied ledger or cache."""
    from memplex.service import MemplexService
    cfg = copy.deepcopy(svc._config)
    if bank.kind == "lite":
        restarted = MemplexService(cfg)
        cleanup.callback(restarted.stop)
        return restarted
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore
    resources = PostgresStorageResources._for_source_validity(bank.dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    cleanup.callback(resources.close)
    resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    store = PostgresMemoryStore(bank.dsn, ready_pool=resources.ready_pool, require_authorization=True)
    resources._source_startup(cfg.source_contract, store, issuer=_RESOURCE_ISSUER)
    fresh = SimpleNamespace(**{**vars(bank), "store": store, "backend": store.authorized(bank.context).source_versions()})
    return _service(fresh, path, cleanup)


@pytest.mark.parametrize("case,entry", READ_BRANCHES, ids=[f"{case.case_id}-{entry}" for case, entry in READ_BRANCHES])
@pytest.mark.parametrize("temperature", ["warm", "cold", "restart"])
def test_lifecycle_case(publication_backend, case, entry, temperature, tmp_path, monkeypatch, request, record_property):
    h = publication_backend
    source = SimpleNamespace(**{**vars(h), "scope": SCOPE, "context": CONTEXT})
    record_property("case_id", case.case_id)
    record_property("backend", source.kind)
    record_property("entry", entry)
    record_property("temperature", temperature)
    record_property("stage", "setup")
    with ExitStack() as cleanup:
        if case.identity == "subject":
            bob = _context(SCOPE, "bob")
            entry_bob = _policy(bob).entries[0]
            h.backend.replace_policy(1, replace(POLICY, revision=2, entries=POLICY.entries + (entry_bob,)), policy_authority(h))
        reader = source
        if case.identity in {"workspace", "tenant"}:
            scope = AuthorityScope("22222222-2222-4222-8222-222222222222",
                "reader-tenant" if case.identity == "tenant" else SCOPE.tenant_id, "reader-workspace")
            reader = _native_reader(source, scope, tmp_path / "reader", cleanup, request)
        elif case.identity == "subject":
            context = _context(SCOPE, "bob")
            backend = h.store.authorized(context).source_versions() if h.kind == "postgres" else h.backend
            reader = SimpleNamespace(**{**vars(source), "context": context, "backend": backend})
        frozen = INPUTS[case.case_id]
        expected_scope = frozen["scope"]
        assert asdict(source.scope) == {"authority_id": expected_scope["source_authority"],
            "tenant_id": expected_scope["source_tenant"], "workspace_id": expected_scope["source_workspace"]}
        assert asdict(reader.scope) == {"authority_id": expected_scope["reader_authority"],
            "tenant_id": expected_scope["reader_tenant"], "workspace_id": expected_scope["reader_workspace"]}
        assert reader.context.principal.subject_id == frozen["principal"]["reader_subject"]
        assert reader.context.principal.authentication_id == frozen["principal"]["reader_credential"]
        assert reader.context.agent_id == frozen["principal"]["agent"]
        assert reader.context.principal.roles == frozenset(frozen["principal"]["roles"])
        record_property("principal_scope", json.dumps({"source": asdict(source.scope),
            "reader": asdict(reader.scope), "reader_subject": reader.context.principal.subject_id,
            "reader_credential": reader.context.principal.authentication_id}, sort_keys=True))
        svc = _service(reader, tmp_path / "service", cleanup)
        expiry = datetime.now(UTC) + timedelta(hours=1)
        fields = {"valid_until": expiry.isoformat()} if case.change == "expiry" else {}
        s = _write(source, "S", "source-body", **fields)
        premise = s
        if case.change == "upstream":
            _, middle = _publish(source, s, "middle-body", key="a" * 64)
            premise = _published_ref(source, middle)
        _, d = _publish(source, premise, case.forbidden_fragments[0], private=case.identity == "subject")
        _write(reader, "U", case.case_id + ":control")
        ids = (d.publication_id, "U")
        candidates = _nominate(svc, ids, case.forbidden_fragments, monkeypatch)
        # A real valid D exists before every change, including foreign cells.
        source_svc = _service(source, tmp_path / "source-service", cleanup)
        assert source_svc.get(d.publication_id, authorization=CONTEXT) is not None
        prepared = None
        if case.surface in {"hot", "prefetch"}:
            prepared = _prepare_runtime(svc, reader.context, case.surface, ids, candidates, monkeypatch)
            record_property("reference_nomination_before_commit", True)
        record_property("logical_id_map", json.dumps({"D": d.publication_id, "U": "U"}))
        record_property("stage", "change_commit")
        changed = _change(source, s, case)
        if case.change == "expiry":
            from memplex.storage.lite.source_versions import _LiteReadSession
            from memplex.storage.postgres_sources import _PostgresReadSession
            final_time = expiry + timedelta(seconds=1)
            session_type = _LiteReadSession if h.kind == "lite" else _PostgresReadSession
            original_clock = session_type.utc_now

            def clock(session):
                original_clock(session)  # Keep the native deadline/lifetime and PG L marker.
                return final_time
            monkeypatch.setattr(session_type, "utc_now", clock)
        if temperature in {"cold", "restart"}:
            svc._context_prefetch_cache.invalidate()
            svc._working_memory.clear()
            prepared = None
        if temperature == "restart":
            svc = _restart(reader, svc, cleanup, tmp_path / "restarted-service")
            candidates = _nominate(svc, ids, case.forbidden_fragments, monkeypatch)
        record_property("stage", "read_after_commit")
        started = time.monotonic_ns()
        actual_ids, text = _read(svc, reader.context, case, entry, ids, candidates, monkeypatch, prepared)
        public_read_elapsed_ns = time.monotonic_ns() - started
        assert actual_ids == list(case.allowed_ids)
        assert case.case_id + ":control" in text
        assert all(fragment not in text for fragment in case.forbidden_fragments)
        # This independent native proof records the first rejection and source
        # lookup count. Public projection above remains the acceptance boundary.
        backend = svc._v1_source_store(reader.context)
        diagnostic_fence_request_started = time.monotonic_ns()
        with backend.read_fence(reader.context, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
            source_attempts = resolve_attempts = 0
            native_source, native_resolve = session.source, session.resolve_id

            def counted_source(key):
                nonlocal source_attempts
                source_attempts += 1
                return native_source(key)

            def counted_resolve(memory_id):
                nonlocal resolve_attempts
                resolve_attempts += 1
                return native_resolve(memory_id)

            # Count only this separate diagnostic session's own calls. The
            # public request and all native method bodies remain unchanged.
            session.source, session.resolve_id = counted_source, counted_resolve
            fence_started = time.monotonic_ns()
            proof = validate_sources(session, ids, authorization=reader.context,
                own_acl=lambda snapshot, context: is_own_node_visible(source_authorization_node(snapshot), context),
                safe_for_model=lambda snapshot: svc.is_safe_for_model(source_authorization_node(snapshot)),
                limits=ValidationLimits(), deadline_ns=time.monotonic_ns() + 250_000_000)
            final = finalize_at(proof, at=session.utc_now())
            fence_body_ns = time.monotonic_ns() - fence_started
        diagnostic_fence_request_elapsed_ns = time.monotonic_ns() - diagnostic_fence_request_started
        expected_reason = FROZEN_INPUTS["backend_reason_overrides"][h.kind].get(
            case.identity, case.expected_reason)
        assert final.dropped == {expected_reason: 1}
        assert set(final.accepted) == {"U"}
        record_property("first_rejection", expected_reason)
        record_property("diagnostic_source_lookup_attempts", source_attempts)
        record_property("diagnostic_resolve_id_attempts", resolve_attempts)
        record_property("successful_unique_source_snapshots", len(proof.sources))
        record_property("diagnostic_validation_fence_body_ns", fence_body_ns)
        record_property("diagnostic_fence_request_elapsed_ns", diagnostic_fence_request_elapsed_ns)
        record_property("valid_control_fraction", 1.0)
        record_property("accepted_candidate_fraction", .5)
        record_property("public_read_elapsed_ns", public_read_elapsed_ns)
        record_property("change_commit_seq", changed.commit_seq)
        record_property("stage", "oracles_passed")


@pytest.mark.parametrize("coordinate", ["workspace_id", "tenant_id"])
def test_one_coordinate_foreign_dependency_denied_without_publication(publication_backend, coordinate):
    h = publication_backend
    bank = SimpleNamespace(**{**vars(h), "scope": SCOPE, "context": CONTEXT})
    source = _write(bank, "S", "source-body")
    bad_ref = replace(source.ref, key=replace(source.ref.key, **{coordinate: "foreign"}))
    with bank.backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        before = session.authority().commit_seq
    with pytest.raises(SourceContractError, match="unsupported_semantics"):
        _publish(bank, SimpleNamespace(ref=bad_ref), "forbidden-cross-domain-body")
    with bank.backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        assert session.authority().commit_seq == before
        assert session.target("d" * 64) is None


def test_nonsemantic_access_counters_keep_derived_valid(publication_backend, tmp_path):
    h = publication_backend
    bank = SimpleNamespace(**{**vars(h), "scope": SCOPE, "context": CONTEXT})
    with ExitStack() as cleanup:
        svc = _service(bank, tmp_path / "counter-service", cleanup)
        source = _write(bank, "S", "source-body")
        _, d = _publish(bank, source, "COUNTER_POSITIVE_BODY")
        current = _write(bank, "S", "source-body", receipt=source,
                         access_count=27, last_accessed_at="2026-10-14T00:00:00+00:00")
        assert current.ref.token == source.ref.token
        assert current.ref.semantic_digest == source.ref.semantic_digest
        assert svc.get(d.publication_id, authorization=CONTEXT).object_ == "COUNTER_POSITIVE_BODY"


def test_independent_e2_survives_invalid_e1(publication_backend, tmp_path, monkeypatch):
    h = publication_backend
    bank = SimpleNamespace(**{**vars(h), "scope": SCOPE, "context": CONTEXT})
    with ExitStack() as cleanup:
        svc = _service(bank, tmp_path / "evidence-service", cleanup)
        e1 = _write(bank, "S", "evidence-one")
        e2 = _write(bank, "E2", "evidence-two")
        _, d1 = _publish(bank, e1, "FORBIDDEN_E1_BODY")
        _, d2 = _publish(bank, e2, "INDEPENDENT_E2_BODY", key="b" * 64)
        _write(bank, "U", "INDEPENDENT_U_BODY")
        _write(bank, "S", "evidence-one-revised", receipt=e1)
        ids = (d1.publication_id, d2.publication_id, "U")
        _nominate(svc, ids, ("FORBIDDEN_E1_BODY", "FORBIDDEN_E1_CITATION", "FORBIDDEN_E1_METADATA"), monkeypatch)
        result = svc.query(QUERY, authorization=CONTEXT)
        assert [item.func_id for item in result.results] == [d2.publication_id, "U"]
        text = json.dumps(asdict(result), default=str)
        assert "FORBIDDEN_E1" not in text
        assert "INDEPENDENT_E2_BODY" in text and "INDEPENDENT_U_BODY" in text


def test_explicit_cross_subject_grant_and_regrant_do_not_recertify(publication_backend, tmp_path):
    h = publication_backend
    bank = SimpleNamespace(**{**vars(h), "scope": SCOPE, "context": CONTEXT})
    bob = replace(_context(SCOPE, "bob"), agent_id="reader-agent")
    charlie = replace(_context(SCOPE, "charlie"), agent_id="ungranted-agent")
    h.backend.replace_policy(1, replace(POLICY, revision=2, entries=POLICY.entries + (
        _policy(bob).entries[0], _policy(charlie).entries[0])), policy_authority(h))
    granted = {"domain": "w09", "memplex_subject_id": "alice", "memplex_grants": "reader-agent"}
    with ExitStack() as cleanup:
        svc = _service(bank, tmp_path / "grant-service", cleanup)
        ungranted = _write(bank, "S", "source-body", visibility="user")
        _, before_grant = _publish(bank, ungranted, "PREGRANT_PRIVATE_BODY", private=True)
        assert svc.get(before_grant.publication_id, authorization=bob) is None
        source = _write(bank, "S", "source-body", receipt=ungranted, visibility="user", namespace=granted)
        assert source.ref.token.auth_revision == ungranted.ref.token.auth_revision + 1
        assert source.ref.token.content_revision == ungranted.ref.token.content_revision
        _, d = _publish(bank, source, "GRANTED_BODY", private=True, grants="reader-agent", expected=before_grant.target)
        assert svc.get(before_grant.publication_id, authorization=bob) is None
        assert svc.get(d.publication_id, authorization=CONTEXT).object_ == "GRANTED_BODY"
        granted_node = svc.get(d.publication_id, authorization=bob)
        assert granted_node is not None, "explicitly granted Bob/reader-agent must read the fresh private publication"
        assert granted_node.object_ == "GRANTED_BODY"
        assert svc.get(d.publication_id, authorization=charlie) is None
        revoked = _write(bank, "S", "source-body", receipt=source, visibility="user")
        assert revoked.ref.token.auth_revision == source.ref.token.auth_revision + 1
        assert svc.get(d.publication_id, authorization=bob) is None
        regranted = _write(bank, "S", "source-body", receipt=revoked, visibility="user", namespace=granted)
        assert regranted.ref.token.auth_revision == revoked.ref.token.auth_revision + 1
        assert regranted.ref.token.content_revision == source.ref.token.content_revision
        assert svc.get(d.publication_id, authorization=bob) is None
        bob_backend = bank.store.authorized(bob).source_versions() if bank.kind == "postgres" else bank.backend
        reader = SimpleNamespace(**{**vars(bank), "context": bob, "backend": bob_backend})
        _write(reader, "U", "GRANT_INDEPENDENT_U")
        assert svc.get("U", authorization=bob).object_ == "GRANT_INDEPENDENT_U"


@pytest.mark.parametrize("configuration", ["sqlite", "wrapper", "sync", "legacy_reader"])
def test_unsupported_configurations_explicitly_deny(tmp_path, monkeypatch, configuration):
    from memplex.service import MemplexService
    from memplex.storage.lite.durability import LiteStorageIntegrityError
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.sync import SyncableStore
    from tests.test_source_v1_compatibility import legacy_config, native_v1
    if configuration == "sync":
        cfg = legacy_config(tmp_path)
        cfg.source_contract = SourceContractConfig("v1", SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
        cfg.sync.enabled = True
        with pytest.raises(SourceContractError, match="unsupported_capability"):
            MemplexService(cfg)
        return
    if configuration == "wrapper":
        store = LiteMemoryStore(tmp_path / "memory.json")
        wrapper = SyncableStore(store)
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
            store._durability, SCOPE, SourceValidationHooks(lambda *args: True, lambda *args: True), issuer=_RESOURCE_ISSUER))
        assert wrapper.source_versions() is None
        with pytest.raises(SourceContractError):
            wrapper.get_fact("D")
        return
    store = native_v1(tmp_path)
    if configuration == "sqlite":
        monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "read")
        assert store.source_versions() is None
        with pytest.raises(LiteStorageIntegrityError):
            LiteMemoryStore(store._path)
    else:
        with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
            MemplexService(legacy_config(tmp_path))
