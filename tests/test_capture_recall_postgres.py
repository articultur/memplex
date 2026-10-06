"""Capture isolation through a real PostgreSQL role that cannot bypass RLS."""

from __future__ import annotations

import os
from contextlib import closing
from copy import deepcopy

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.auth import AuthorizationContext, Principal
from memplex.context import current_node_text
from memplex.service import MemplexService
from tests.test_postgres_integration import (
    _admin_query,
    _drop_unprivileged_role,
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

        def create_service():
            service = MemplexService(config=deepcopy(config))
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
