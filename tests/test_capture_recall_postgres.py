"""Capture isolation through a real PostgreSQL role that cannot bypass RLS."""

from __future__ import annotations

import json
import os
from contextlib import closing
from copy import deepcopy

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.auth import AuthorizationContext, Principal
from memplex.context import current_node_text
from memplex.models import (
    Fact,
    FieldValue,
    Function,
    Observation,
    Paragraph,
    Preference,
    SourceDocument,
)
from memplex.service import MemplexService
from memplex.storage.migrations.runner import PostgresMigrationRunner, VectorCapabilityRequest
from tests.test_postgres_integration import (
    _admin_execute,
    _admin_query,
    _authorization,
    _BagOfWordsEmbedder,
    _drop_unprivileged_role,
    _grant_vector_type_usage,
    _production_service_config,
    psycopg2,
)

FACT_TEXT = "The Atlas parcel routing code is AMBER-7382."
FUNCTION_TEXT = "Remember same-text-token for this namespace."


def _context(*, subject="alice", workspace="workspace-a", session="s1"):
    return AuthorizationContext(
        principal=Principal(tenant_id="capture-team", subject_id=subject),
        workspace_id=workspace,
        agent_id="codex",
        session_id=session,
    )


@pytest.fixture
def capture_service_factory(pg_function_dsn, monkeypatch):
    # This gate exercises SQL identity/RLS, independently of vector support.
    # The aggregate PG CI contract must still fail when pgvector is missing.
    if os.environ.get("MEMPLEX_REQUIRE_PGVECTOR") == "1":
        with (
            closing(psycopg2.connect(pg_function_dsn)) as connection,
            connection.cursor() as cursor,
        ):
            try:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
                connection.commit()
            except psycopg2.Error as exc:
                pytest.fail(f"pgvector is required by this CI gate: {exc}")
    monkeypatch.setenv("MEMPLEX_PGVECTOR_DIM", "0")
    monkeypatch.setenv("MEMPLEX_RAW_PARAGRAPH_LAYER", "1")
    config, role = _production_service_config(pg_function_dsn)
    config.embedding.model = "tfidf"
    config.llm.provider = "rule-based"
    config.llm.fallback_chain = ["rule-based"]
    config.llm.query_enhancement = False
    config.llm.factual_capture = False
    config.llm.observation_compression = False
    config.sync.enabled = False
    config.sleep_time.enabled = False
    config.wiki.enabled = False
    services = []
    try:
        # Check the application's actual connection, not just the admin role.
        with (
            closing(psycopg2.connect(config.storage.path)) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles "
                "WHERE rolname = current_user"
            )
            assert cursor.fetchone() == (False, False)
        assert _admin_query(
            pg_function_dsn,
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'memplex_paragraphs'::regclass",
        ) == [(True, True)]

        def create_service(*, vector_dim=0):
            if vector_dim:
                PostgresMigrationRunner(pg_function_dsn).ensure_vector_capability(
                    VectorCapabilityRequest(dim=vector_dim, policy="required"), "production",
                )
                _grant_vector_type_usage(pg_function_dsn, role)
            monkeypatch.setenv("MEMPLEX_PGVECTOR_DIM", str(vector_dim))
            service = MemplexService(config=deepcopy(config))
            if vector_dim:
                service.store.set_embedder(_BagOfWordsEmbedder(vector_dim))
            services.append(service)
            return service

        yield create_service
    finally:
        for service in services:
            service.stop()
        _drop_unprivileged_role(pg_function_dsn, role)


def test_capture_equal_function_text_preserves_each_postgres_subject(
    capture_service_factory,
):
    service = capture_service_factory()
    contexts = [_context(), _context(subject="bob", session="s2")]
    runtimes = [AgentMemoryRuntime(service=service, authorization=c) for c in contexts]

    for runtime in runtimes:
        runtime.capture_turn(FUNCTION_TEXT, "Recorded.")

    functions = service._store_for(contexts[0]).list_functions(limit=100)
    matches = [f for f in functions if "same-text-token" in current_node_text(f)]
    assert {f.owner_subject_id for f in matches} == {"alice", "bob"}
    assert len({f.id for f in matches}) == 2
    assert len({f.name_normalized for f in matches}) == 2
    assert {f.name for f in matches} == {FUNCTION_TEXT}
    for runtime in runtimes:
        assert "same-text-token" in runtime.before_prompt("same-text-token").context


@pytest.mark.parametrize(
    "next_context",
    [_context(session="s2"), _context(workspace="workspace-b", session="s2")],
    ids=["fresh-session", "other-workspace"],
)
def test_observation_write_repeated_text_keeps_scoped_postgres_raw_refs(
    capture_service_factory, next_context,
):
    first_context = _context()
    first_service = capture_service_factory()
    first = first_service.write_text(
        FACT_TEXT, source_type="observation", authorization=first_context,
    )
    first_refs = tuple(first.facts[0].source_paragraphs)
    assert first_refs
    first_service.stop()

    restarted = capture_service_factory()
    second = restarted.write_text(
        FACT_TEXT, source_type="observation", authorization=next_context,
    )
    second_refs = tuple(second.facts[0].source_paragraphs)
    assert second_refs
    assert set(first_refs).isdisjoint(second_refs)

    for context, references in (
        (first_context, first_refs), (next_context, second_refs),
    ):
        scoped = restarted._store_for(context)
        for paragraph_id in references:
            paragraph = scoped.get_paragraph(paragraph_id)
            assert paragraph is not None
            assert paragraph["raw_text"] == FACT_TEXT
        recalled = AgentMemoryRuntime(service=restarted, authorization=context).before_prompt(
            "Atlas parcel routing code"
        )
        assert "AMBER-7382" in recalled.context

    if first_context.workspace_id != next_context.workspace_id:
        assert first.facts[0].id != second.facts[0].id
        assert restarted.get(
            second.facts[0].id, authorization=first_context,
        ) is None


def test_capture_repeated_fact_across_postgres_sessions_survives_restart(
    capture_service_factory,
):
    first_service = capture_service_factory()
    AgentMemoryRuntime(service=first_service, authorization=_context()).capture_turn(
        FACT_TEXT, "Recorded.",
    )
    first_service.stop()

    restarted = capture_service_factory()
    fresh = AgentMemoryRuntime(service=restarted, authorization=_context(session="s2"))
    fresh.capture_turn(FACT_TEXT, "Recorded.")

    assert "AMBER-7382" in fresh.before_prompt("Atlas parcel routing code").context


@pytest.mark.parametrize(
    "next_context",
    [_context(session="s2"), _context(workspace="workspace-b", session="s2")],
    ids=["fresh-session", "other-workspace"],
)
def test_user_capture_function_preserves_each_postgres_writer(
    capture_service_factory, next_context,
):
    first_context = _context()
    first_service = capture_service_factory()
    first = first_service.write_text(
        FUNCTION_TEXT, source_type="observation", visibility="user",
        authorization=first_context,
    )
    assert len(first.functions) == 1
    first_service.stop()

    restarted = capture_service_factory()
    second = restarted.write_text(
        FUNCTION_TEXT, source_type="observation", visibility="user",
        authorization=next_context,
    )
    assert len(second.functions) == 1
    original, repeated = first.functions[0], second.functions[0]
    assert original.id != repeated.id
    assert original.name_normalized != repeated.name_normalized
    assert original.name == repeated.name == FUNCTION_TEXT

    for context, memory_id in (
        (first_context, original.id), (next_context, repeated.id),
    ):
        stored = restarted.get(memory_id, authorization=context)
        assert stored is not None
        assert stored.visibility == "user"
        assert stored.owner_subject_id == context.principal.subject_id
        assert stored.workspace_id == context.workspace_id
        assert stored.origin_session == context.session_id
        assert stored.provenance["agent_id"] == context.agent_id
        assert "same-text-token" in current_node_text(stored)


def test_workspace_capture_name_alias_preserves_each_postgres_writer(
    capture_service_factory,
):
    # Both paragraphs have the same extracted display/normalized name before
    # capture scoping, but distinct fields and content-derived IDs.
    heading = "Remember the deployment checklist for Atlas routing."
    first_text = heading + "\nRun the AMBER verification step."
    second_text = heading + "\nRun the INDIGO verification step."
    assert first_text[:50] == second_text[:50]
    first_context = _context()
    first_service = capture_service_factory()
    first = first_service.write_text(
        first_text, source_type="observation", visibility="workspace",
        authorization=first_context,
    )
    assert len(first.functions) == 1
    first_service.stop()

    next_context = _context(session="s2")
    restarted = capture_service_factory()
    second = restarted.write_text(
        second_text, source_type="observation", visibility="workspace",
        authorization=next_context,
    )
    assert len(second.functions) == 1
    original, updated = first.functions[0], second.functions[0]
    assert original.id != updated.id
    assert original.name_normalized != updated.name_normalized
    assert original.name == updated.name

    for context, memory_id, expected, excluded in (
        (first_context, original.id, "AMBER", "INDIGO"),
        (next_context, updated.id, "INDIGO", "AMBER"),
    ):
        stored = restarted.get(memory_id, authorization=context)
        assert stored is not None
        assert stored.origin_session == context.session_id
        assert stored.workspace_id == context.workspace_id
        assert expected in current_node_text(stored)
        assert excluded not in current_node_text(stored)


def _assert_persisted_fact_and_raw_references(service, context, fact_id, references):
    """Read durable typed and raw rows before relying on retrieval behavior."""
    scoped = service._store_for(context)
    stored = scoped.get_fact(fact_id)
    assert isinstance(stored, Fact)
    assert "AMBER-7382" in current_node_text(stored)
    assert references
    for paragraph_id in references:
        paragraph = scoped.get_paragraph(paragraph_id)
        assert paragraph is not None
        assert paragraph["raw_text"] == FACT_TEXT


@pytest.mark.parametrize(
    "next_context",
    [_context(session="s2"), _context(workspace="workspace-b", session="s2")],
    ids=["fresh-session", "other-workspace"],
)
def test_fact_cold_recall_precedes_recapture_and_keeps_both_raw_reference_sets(
    capture_service_factory, next_context,
):
    first_context = _context()
    first_service = capture_service_factory()
    first = first_service.write_text(
        FACT_TEXT, source_type="observation", authorization=first_context,
    )
    assert len(first.facts) == 1
    first_id = first.facts[0].id
    first_refs = tuple(first.facts[0].source_paragraphs)
    first_service.stop()

    restarted = capture_service_factory()
    assert restarted._working_memory is None
    fresh_context = _context(session="fresh-before-recapture")
    _assert_persisted_fact_and_raw_references(restarted, fresh_context, first_id, first_refs)
    # No write or capture has occurred since restart: this must be cold retrieval.
    cold = AgentMemoryRuntime(service=restarted, authorization=fresh_context)
    assert "AMBER-7382" in cold.before_prompt("Atlas parcel routing code").context

    second = restarted.write_text(
        FACT_TEXT, source_type="observation", authorization=next_context,
    )
    assert len(second.facts) == 1
    second_id = second.facts[0].id
    second_refs = tuple(second.facts[0].source_paragraphs)
    assert second_refs and set(first_refs).isdisjoint(second_refs)
    restarted.stop()

    final_service = capture_service_factory()
    assert final_service._working_memory is None
    records = (
        (first_context, first_id, first_refs),
        (next_context, second_id, second_refs),
    )
    # Check BOTH persisted records/reference sets before the first recall assertion.
    for context, memory_id, references in records:
        _assert_persisted_fact_and_raw_references(final_service, context, memory_id, references)
    for context, memory_id, _references in records:
        recalled = AgentMemoryRuntime(service=final_service, authorization=context).before_prompt(
            "Atlas parcel routing code",
        )
        assert "AMBER-7382" in recalled.context
        assert memory_id in recalled.context
    if first_context.workspace_id != next_context.workspace_id:
        assert first_id != second_id
        assert final_service._store_for(first_context).get_fact(second_id) is None
        assert final_service._store_for(next_context).get_fact(first_id) is None
        assert all(final_service._store_for(first_context).get_paragraph(ref) is None for ref in second_refs)
        assert all(final_service._store_for(next_context).get_paragraph(ref) is None for ref in first_refs)


def _fact_recall_context(**changes):
    return _authorization(**({
        "tenant": "fact-lexical-team", "subject": "alice", "workspace": "workspace-a",
        "agent": "codex", "session": "session-a",
    } | changes))


@pytest.mark.parametrize(
    "visibility,change",
    [
        ("workspace", {"workspace": "workspace-b"}),
        ("user", {"subject": "bob"}),
        ("workspace", {"tenant": "other-tenant"}),
        ("session", {"session": "session-b"}),
        ("session", {"agent": "hermes"}),
        ("session", {"subject": "bob"}),
        ("session", {"workspace": "workspace-b"}),
    ],
    ids=["workspace", "user", "tenant", "session", "agent", "session-user", "session-workspace"],
)
def test_fact_lexical_acl_denial_keeps_visible_positive_control(
    capture_service_factory, visibility, change,
):
    service = capture_service_factory()
    assert service._working_memory is None
    owner = _fact_recall_context()
    other = _fact_recall_context(**change)
    query = "quartz parcel routing"
    target = Fact(
        id="lexical-private-fact", subject=query, predicate="is", object_="PRIVATE-FACT-CANARY",
        visibility=visibility,
    )
    control = Fact(
        id="lexical-visible-control", subject=query, predicate="is", object_="VISIBLE-CONTROL",
        visibility="session",
    )
    service._store_for(owner).add_fact(target)
    service._store_for(other).add_fact(control)

    owner_store = service._store_for(owner)
    other_store = service._store_for(other)
    assert owner_store.get_fact(target.id) is not None
    assert other_store.get_fact(target.id) is None
    assert other_store.get_fact(control.id) is not None
    assert target.id in {item.func_id for item in owner_store.vector_search(query, top_k=20)}
    denied_results = other_store.vector_search(query, top_k=20)
    assert {item.func_id for item in denied_results} == {control.id}
    assert "PRIVATE-FACT-CANARY" not in repr(denied_results)
    assert "PRIVATE-FACT-CANARY" in AgentMemoryRuntime(
        service=service, authorization=owner,
    ).before_prompt(query).context
    recalled = AgentMemoryRuntime(service=service, authorization=other).before_prompt(query)
    assert "VISIBLE-CONTROL" in recalled.context
    assert "PRIVATE-FACT-CANARY" not in recalled.context
    assert recalled.total == 1


@pytest.mark.parametrize("vector_dim", [0, 32], ids=["lexical-only", "pgvector-enabled"])
def test_mixed_function_fact_cold_recall_preserves_retrieval_scope(
    capture_service_factory, vector_dim,
):
    service = capture_service_factory(vector_dim=vector_dim)
    owner = _fact_recall_context()
    scoped = service._store_for(owner)
    query = "quartz parcel"
    function = Function(
        id="mixed-function", name=query, name_normalized=query,
        trigger=[FieldValue(desc=query)], action=[FieldValue(desc="FUNCTION-ANSWER")],
    )
    vector_only = Function(
        id="vector-only-function", name="quartz freight", name_normalized="quartz freight",
        trigger=[FieldValue(desc="quartz freight")], action=[FieldValue(desc="VECTOR-ONLY-ANSWER")],
    )
    fact = Fact(id="mixed-fact", subject=query, predicate="is", object_="FACT-ANSWER")
    # These real persisted types must not become candidates in this bounded change.
    excluded = Fact(id="unmatched-fact", subject="unrelated", predicate="is", object_="FACT-NO-MATCH")
    scoped.add(function, SourceDocument(type="test"))
    scoped.add(vector_only, SourceDocument(type="test"))
    scoped.add_fact(fact)
    scoped.add_fact(excluded)
    scoped.add_observation(Observation(id="excluded-observation", event=query, context="OBSERVATION-ONLY"))
    scoped.add_preference(Preference(id="excluded-preference", aspect=query, preference="PREFERENCE-ONLY"))
    scoped.persist_paragraphs(
        [Paragraph(id="excluded-raw", source="test", section="", raw_text=f"{query} RAW-ONLY")],
        trust_tier=1, source_hint="mixed-raw",
    )
    service.stop()

    restarted = capture_service_factory(vector_dim=vector_dim)
    assert restarted._working_memory is None
    assert restarted.store._vector_dim == vector_dim
    scoped = restarted._store_for(_fact_recall_context(session="fresh-session"))
    assert scoped.get_fact(fact.id).object_ == "FACT-ANSWER"
    assert scoped.get(function.id) is not None
    expected = {function.id, fact.id} | ({vector_only.id} if vector_dim else set())
    results = scoped.vector_search(query, top_k=20)
    assert {item.func_id for item in results} == expected
    assert {item.func_id for item in scoped.fts_search(query, top_k=20)} == expected
    assert next(item.summary for item in results if item.func_id == fact.id) == "quartz parcel is FACT-ANSWER"
    recalled = AgentMemoryRuntime(
        service=restarted, authorization=_fact_recall_context(session="fresh-session"), top_k=20,
    ).before_prompt(query)
    assert "FUNCTION-ANSWER" in recalled.context
    assert "FACT-ANSWER" in recalled.context
    assert ("VECTOR-ONLY-ANSWER" in recalled.context) is bool(vector_dim)
    assert all(text not in recalled.context for text in (
        "FACT-NO-MATCH", "OBSERVATION-ONLY", "PREFERENCE-ONLY", "RAW-ONLY",
    ))
    assert recalled.total == len(expected)


def test_runtime_capture_fact_is_cold_recalled_before_any_recapture(capture_service_factory):
    first = capture_service_factory()
    context = _context()
    AgentMemoryRuntime(service=first, authorization=context).capture_turn(FACT_TEXT, "Recorded.")
    facts = [node for node in first._store_for(context).list_facts() if "AMBER-7382" in current_node_text(node)]
    assert len(facts) == 1
    fact_id, references = facts[0].id, tuple(facts[0].source_paragraphs)
    first.stop()

    restarted = capture_service_factory()
    assert restarted._working_memory is None
    fresh_context = _context(session="fresh-before-any-recapture")
    _assert_persisted_fact_and_raw_references(restarted, fresh_context, fact_id, references)
    recalled = AgentMemoryRuntime(service=restarted, authorization=fresh_context).before_prompt(
        "Atlas parcel routing code",
    )
    assert recalled.source == "live"
    assert fact_id in recalled.context
    assert "AMBER-7382" in recalled.context


def _write_lexical_positive_controls(service, context, query):
    scoped = service._store_for(context)
    function = Function(
        id="z-valid-function", name=query, name_normalized=f"{query} control",
        action=[FieldValue(desc="FUNCTION-POSITIVE-CONTROL")],
    )
    fact = Fact(
        id="z-valid-fact", subject=query, predicate="is", object_="FACT-POSITIVE-CONTROL",
    )
    scoped.add(function, SourceDocument(type="test"))
    scoped.add_fact(fact)
    assert scoped.get(function.id) is not None
    assert scoped.get_fact(fact.id) is not None
    return {function.id, fact.id}


@pytest.mark.parametrize(
    "collision",
    ["both-match", "only-fact-matches", "denied-function"],
)
def test_fact_lexical_collision_keeps_visible_function_precedence(
    capture_service_factory, collision,
):
    service = capture_service_factory()
    assert service.store._vector_dim == 0
    owner = _fact_recall_context()
    function_owner = _fact_recall_context(subject="bob") if collision == "denied-function" else owner
    query = "quartz"
    expected_ids = _write_lexical_positive_controls(service, owner, query)
    function_name = "unrelated freight" if collision == "only-fact-matches" else query
    function = Function(
        id="same-typed-id", name=function_name, name_normalized=f"{function_name} collision",
        action=[FieldValue(desc="FUNCTION-COLLISION-BODY")], visibility="user",
    )
    fact = Fact(
        id=function.id, subject=query, predicate="is", object_="FACT-COLLISION-BODY",
    )
    service._store_for(function_owner).add(function, SourceDocument(type="test"))
    scoped = service._store_for(owner)
    scoped.add_fact(fact)
    assert service._store_for(function_owner).get(function.id) is not None
    assert scoped.get_fact(fact.id).object_ == "FACT-COLLISION-BODY"
    assert (scoped.get(function.id) is None) is (collision == "denied-function")
    if collision != "only-fact-matches":
        expected_ids.add(function.id)

    results = scoped.vector_search(query, top_k=20)
    assert {item.func_id for item in results} == expected_ids
    assert len(results) == len(expected_ids)
    # A same-ID Function and Fact must never award two lexical RRF credits.
    assert [item.relevance_score for item in results] == pytest.approx([
        1 / (61 + rank) for rank in range(len(expected_ids))
    ])
    if collision == "both-match":
        assert next(item.summary for item in results if item.func_id == function.id) == function_name
    elif collision == "denied-function":
        assert next(item.summary for item in results if item.func_id == fact.id) == "quartz is FACT-COLLISION-BODY"
    recalled = AgentMemoryRuntime(service=service, authorization=owner).before_prompt(query)
    assert "FUNCTION-POSITIVE-CONTROL" in recalled.context
    assert "FACT-POSITIVE-CONTROL" in recalled.context
    assert ("FUNCTION-COLLISION-BODY" in recalled.context) is (collision == "both-match")
    assert ("FACT-COLLISION-BODY" in recalled.context) is (collision == "denied-function")
    assert recalled.total == len(expected_ids)


def test_legacy_fact_without_payload_kind_retains_lexical_body(
    capture_service_factory, pg_function_dsn,
):
    service = capture_service_factory()
    owner = _fact_recall_context()
    query = "quartz"
    expected_ids = _write_lexical_positive_controls(service, owner, query)
    fact = Fact(id="legacy-fact", subject=query, predicate="is", object_="LEGACY-FACT-BODY")
    service._store_for(owner).add_fact(fact)
    _admin_execute(
        pg_function_dsn,
        "UPDATE memplex_facts SET data = data - 'memory_type' WHERE tenant_id = %s AND id = %s",
        (owner.principal.tenant_id, fact.id),
    )
    assert _admin_query(
        pg_function_dsn,
        "SELECT data ? 'memory_type' FROM memplex_facts WHERE tenant_id = %s AND id = %s",
        (owner.principal.tenant_id, fact.id),
    ) == [(False,)]
    expected_ids.add(fact.id)
    results = service._store_for(owner).vector_search(query, top_k=20)
    assert {item.func_id for item in results} == expected_ids
    legacy = next(item for item in results if item.func_id == fact.id)
    assert legacy.summary == "quartz is LEGACY-FACT-BODY"
    recalled = AgentMemoryRuntime(service=service, authorization=owner).before_prompt(query)
    assert "LEGACY-FACT-BODY" in recalled.context
    assert "FUNCTION-POSITIVE-CONTROL" in recalled.context
    assert "FACT-POSITIVE-CONTROL" in recalled.context
    assert recalled.total == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", 17), ("object", ["quartz"]), ("namespace", None),
        ("provenance", None), ("source_type", ["meeting"]),
    ],
    ids=["numeric-subject", "list-object", "null-namespace", "null-provenance", "list-source-type"],
)
def test_malformed_fact_projection_preserves_valid_function_and_fact(
    capture_service_factory, pg_function_dsn, field, value,
):
    service = capture_service_factory()
    owner = _fact_recall_context()
    query = "quartz"
    expected_ids = _write_lexical_positive_controls(service, owner, query)
    malformed = Fact(
        id="a-malformed-fact", subject=query, predicate=query, object_=f"{query} MALFORMED-FACT-BODY",
    )
    service._store_for(owner).add_fact(malformed)
    # Construct a legacy/corrupt JSON row without changing relational identity,
    # policies, role privileges, or the application-role query connection.
    _admin_execute(
        pg_function_dsn,
        "UPDATE memplex_facts SET data = jsonb_set(data, %s, %s::jsonb) "
        "WHERE tenant_id = %s AND id = %s",
        ([field], json.dumps(value), owner.principal.tenant_id, malformed.id),
    )
    assert _admin_query(
        pg_function_dsn,
        "SELECT data -> %s FROM memplex_facts WHERE tenant_id = %s AND id = %s",
        (field, owner.principal.tenant_id, malformed.id),
    ) == [(value,)]
    # The malformed row has more matching terms than either valid control,
    # so filling top_k requires skipping it before the final projection cap.
    results = service._store_for(owner).vector_search(query, top_k=2)
    assert {item.func_id for item in results} == expected_ids
    assert len(results) == 2
    assert "MALFORMED-FACT-BODY" not in repr(results)
    recalled = AgentMemoryRuntime(service=service, authorization=owner).before_prompt(query)
    assert "FUNCTION-POSITIVE-CONTROL" in recalled.context
    assert "FACT-POSITIVE-CONTROL" in recalled.context
    assert "MALFORMED-FACT-BODY" not in recalled.context
    assert recalled.total == 2
