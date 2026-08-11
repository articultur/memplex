"""Service-level authorization must bind identity before storage access."""

from __future__ import annotations

import pytest

from memplex.config import MemplexConfig
from memplex.models import ExtractedData, Fact, Function, GraphData, Preference, SourceDocument
from memplex.service import MemplexService


def _context(*, tenant: str, subject: str, session: str = "session"):
    from memplex.auth import AuthorizationContext, Principal

    return AuthorizationContext(
        principal=Principal(
            tenant_id=tenant,
            subject_id=subject,
            roles=frozenset({"member"}),
            authentication_id=f"credential-{subject}",
        ),
        workspace_id="shared-workspace",
        agent_id="codex",
        session_id=session,
        request_id=f"request-{subject}",
    )


@pytest.fixture
def service(tmp_path) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.llm.query_enhancement = False
    config.embedding.model = "default"
    return MemplexService(config=config)


def test_authorized_write_stamps_all_nodes_before_first_store_call(
    service: MemplexService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tenant="tenant-a", subject="alice")
    function = Function(id="function-a", name="tenant canary", name_normalized="tenant canary")
    fact = Fact(id="fact-a", name="fact", subject="A", predicate="is", object_="scoped")
    preference = Preference(id="preference-a", name="preference", aspect="tone", preference="short")
    extracted = ExtractedData(
        functions=[function],
        facts=[fact],
        preferences=[preference],
        graph=GraphData(nodes=[function]),
    )
    monkeypatch.setattr(service._engine, "extract", lambda source: extracted)

    observed: list[tuple[str, str, str]] = []
    original_add_fact = service.store.add_fact
    original_add_preference = service.store.add_preference
    original_merge = service.store.merge

    def add_fact(node):
        observed.append(("fact", node.tenant_id, node.owner_subject_id))
        original_add_fact(node)

    def add_preference(node):
        observed.append(("preference", node.tenant_id, node.owner_subject_id))
        original_add_preference(node)

    def merge(graph):
        observed.extend(
            ("function", node.tenant_id, node.owner_subject_id) for node in graph.nodes
        )
        return original_merge(graph)

    monkeypatch.setattr(service.store, "add_fact", add_fact)
    monkeypatch.setattr(service.store, "add_preference", add_preference)
    monkeypatch.setattr(service.store, "merge", merge)

    service.write(
        SourceDocument(type="text", content="tenant canary"),
        authorization=context,
    )

    assert observed == [
        ("fact", "tenant-a", "alice"),
        ("preference", "tenant-a", "alice"),
        ("function", "tenant-a", "alice"),
    ]


def test_cross_tenant_get_search_timeline_and_mutations_fail_closed(
    service: MemplexService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memplex.auth import MemoryNotFoundError, bind_node_identity

    alice = _context(tenant="tenant-a", subject="alice")
    bob = _context(tenant="tenant-b", subject="bob")
    function = Function(
        id="alice-function",
        name="alice boundary canary",
        name_normalized="alice boundary canary",
    )
    bind_node_identity(function, alice)
    service.store.add(function, SourceDocument(type="text"))

    assert service.get(function.id, authorization=alice) is not None
    assert service.get(function.id, authorization=bob) is None
    assert service.get_timeline(function.id, authorization=bob) == []

    result = service.query(
        "alice boundary canary",
        top_k=20,
        authorization=bob,
        explain=True,
    )
    assert result.results == []
    assert not any(
        item.get("id") == function.id
        for item in (result.explanation or {}).get("results", [])
    )

    with pytest.raises(MemoryNotFoundError, match="Memory not found"):
        service.update_memory(function.id, "action", "tampered", authorization=bob)
    with pytest.raises(MemoryNotFoundError, match="Memory not found"):
        service.delete(function.id, authorization=bob)
    with pytest.raises(MemoryNotFoundError, match="Memory not found"):
        service.submit_feedback(
            function.id,
            "action",
            0,
            "wrong",
            authorization=bob,
        )
    with pytest.raises(MemoryNotFoundError, match="Memory not found"):
        service.apply_resolution(
            function.id,
            "action",
            "reject",
            authorization=bob,
        )

    assert service.get(function.id, authorization=alice) is not None


def test_authorized_observation_listing_and_pending_reviews_are_scoped(
    service: MemplexService,
) -> None:
    from memplex.auth import bind_node_identity
    from memplex.models import MemoryFeedback, Observation
    from memplex.models.feedback import FeedbackVerdict

    alice = _context(tenant="tenant-a", subject="alice")
    bob = _context(tenant="tenant-b", subject="bob")
    alice_function = Function(
        id="alice-review",
        name="alice review",
        name_normalized="alice review",
    )
    alice_observation = Observation(id="alice-observation", name="alice observation")
    bind_node_identity(alice_function, alice)
    bind_node_identity(alice_observation, alice)
    service.store.add(alice_function, SourceDocument(type="text"))
    service.store.add_observation(alice_observation)
    service._feedback_store.record(
        MemoryFeedback(
            memory_id=alice_function.id,
            field_role="action",
            value_index=0,
            verdict=FeedbackVerdict.WRONG,
            owner="alice",
        )
    )

    assert [item.id for item in service.list_observations(authorization=alice)] == [
        alice_observation.id
    ]
    assert service.list_observations(authorization=bob) == []
    assert service.get_pending_reviews(authorization=bob) == []
    assert [item.memory_id for item in service.get_pending_reviews(authorization=alice)] == [
        alice_function.id
    ]


def test_authorized_feedback_persists_principal_scope(service: MemplexService) -> None:
    from memplex.auth import bind_node_identity

    alice = _context(tenant="tenant-a", subject="alice")
    function = Function(
        id="alice-feedback",
        name="alice feedback",
        name_normalized="alice feedback",
    )
    bind_node_identity(function, alice)
    service.store.add(function, SourceDocument(type="text"))

    service.submit_feedback(
        function.id,
        "action",
        0,
        "wrong",
        authorization=alice,
    )

    feedback = service._feedback_store._records[-1]
    assert feedback.tenant_id == "tenant-a"
    assert feedback.owner_subject_id == "alice"
    assert feedback.workspace_id == "shared-workspace"
    assert feedback.provenance["authentication_id"] == "credential-alice"


def test_session_feedback_inherits_memory_visibility_and_remains_private(
    service: MemplexService,
) -> None:
    from memplex.auth import bind_node_identity

    alice = _context(tenant="tenant-a", subject="alice", session="session-alice")
    bob = _context(tenant="tenant-a", subject="bob", session="session-bob")
    function = Function(
        id="alice-session-feedback",
        name="alice session feedback",
        name_normalized="alice session feedback",
    )
    bind_node_identity(function, alice, visibility="session")
    service.store.add(function, SourceDocument(type="text"))

    service.submit_feedback(
        function.id,
        "action",
        0,
        "wrong",
        authorization=alice,
    )

    feedback = service._feedback_store._records[-1]
    assert feedback.visibility == "session"
    assert feedback.provenance["agent_id"] == "codex"
    assert feedback.provenance["session_id"] == "session-alice"

    alice_feedback = service._feedback_store.authorized(alice)
    bob_feedback = service._feedback_store.authorized(bob)
    assert [item.memory_id for item in alice_feedback.get_history(function.id)] == [
        function.id
    ]
    assert bob_feedback.get_history(function.id) == []
    assert [item.memory_id for item in alice_feedback.get_pending()] == [function.id]
    assert bob_feedback.get_pending() == []
