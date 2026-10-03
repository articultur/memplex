"""Tests for the working-memory hot-context tier (memplex/working_memory.py)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.working_memory import WorkingMemory


def test_add_and_recency_ordered_recall():
    wm = WorkingMemory(default_ttl_seconds=60)
    wm.add("a", "first turn")
    time.sleep(0.01)
    wm.add("b", "second turn")
    assert wm.recall_context() == ["second turn", "first turn"]
    assert len(wm) == 2


def test_ttl_expiry_drops_entries():
    wm = WorkingMemory(default_ttl_seconds=0.01)
    wm.add("x", "ephemeral")
    time.sleep(0.02)
    assert wm.recall_context() == []
    assert len(wm) == 0


def test_pinned_entries_survive_ttl_and_cap():
    wm = WorkingMemory(max_entries=3, default_ttl_seconds=60)
    wm.add("pin1", "pinned fact", pinned=True, ttl_seconds=0.01)
    wm.add("tmp1", "t1")
    wm.add("tmp2", "t2")
    wm.add("tmp3", "t3")  # cap: evicts oldest unpinned (tmp1)
    time.sleep(0.02)
    live = wm.recall_context()
    assert "pinned fact" in live  # pinned survives its own short ttl
    assert "t1" not in live and "t2" in live and "t3" in live


def test_pin_unpin_lifecycle():
    wm = WorkingMemory(default_ttl_seconds=0.01)
    wm.add("k", "v")
    assert wm.pin("k") is True
    assert wm.pin("missing") is False
    time.sleep(0.02)
    assert wm.recall_context() == ["v"]  # pinned survives
    assert wm.unpin("k", ttl_seconds=0.01) is True
    time.sleep(0.02)
    assert wm.recall_context() == []


def test_add_refresh_and_remove():
    wm = WorkingMemory()
    wm.add("k", "old")
    wm.add("k", "new")  # same key refreshes, no growth
    assert len(wm) == 1
    assert wm.recall_context() == ["new"]
    assert wm.remove("k") is True
    assert wm.remove("k") is False


def test_limit_and_invalid_inputs():
    wm = WorkingMemory()
    for i in range(10):
        wm.add(f"k{i}", f"c{i}")
    assert len(wm.recall_context(limit=3)) == 3
    wm.add("", "empty key ignored")
    wm.add("k", "")
    assert len(wm) == 10


def test_service_integration_injects_on_recall(tmp_path):
    """Committed IDs and host-annotated ordinary before_prompt stay usable."""
    from memplex.adapters.agent_runtime import AgentMemoryRuntime, describe_memory_scope
    from memplex.auth import AuthorizationContext, Principal
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.working_memory.enabled = True
    cfg.working_memory.default_ttl_seconds = 60

    svc = MemplexService(config=cfg)
    svc.start()
    try:
        from memplex.models import SourceDocument, SourceType

        context = AuthorizationContext(
            principal=Principal(subject_id="alice", tenant_id="t1"),
            workspace_id="ws-a", agent_id="codex", session_id="session-a",
        )
        runtime = AgentMemoryRuntime(
            service=svc, agent="codex", authorization=context, project_path=tmp_path,
        )
        svc.write(
            SourceDocument(
                type="conversation",
                content="The deployment pipeline now requires two reviewers.",
                source_type=SourceType.MEETING,
            ),
            authorization=context,
        )
        refs = svc._working_memory.recall_references(
            storage_namespace=svc.storage_namespace(), tenant_id=context.principal.tenant_id,
        )
        assert refs, "successful captures publish resolvable source IDs"
        assert set(refs) <= set(svc._store_for(context).read_context_nodes(refs))
        nodes = svc._store_for(context).read_context_nodes(refs)
        assert any("two reviewers" in nodes[node_id].name for node_id in refs)
        assert svc._working_memory.recall_context(scope="tenant:t1") == []
        scope = describe_memory_scope(
            agent="codex", user_id="alice", session_id="session-a", project_path=tmp_path,
            storage_namespace=svc.storage_namespace(), workspace_id="ws-a",
        )
        svc.annotate_memories(refs, attributes=scope["write_namespace"], authorization=context)
        assert svc._working_memory.recall_references(
            storage_namespace=svc.storage_namespace(), tenant_id="t1",
        ) == ()
        recalled = runtime.before_prompt("pipeline")
        assert "two reviewers" in recalled.context
        assert recalled.total > 0
    finally:
        svc.stop()


def test_service_disabled_by_default(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    svc = MemplexService(config=cfg)
    assert svc._working_memory is None


def test_recall_acl_filters_workspace_and_private_entries():
    """V4: the hot tier re-checks entries against the recalling principal.

    A workspace-restricted capture must not cross workspaces inside the
    same tenant; a private capture stays with its owner - the same
    boundary the store's ACL enforces on the retrieval path.
    """
    from memplex.working_memory import WorkingMemory

    wm = WorkingMemory()
    wm.add("shared", "team note", scope="tenant:t1",
           workspace_id="ws-a", visibility="workspace")
    wm.add("other-ws", "other workspace note", scope="tenant:t1",
           workspace_id="ws-b", visibility="workspace")
    wm.add("alice-private", "alice private note", scope="tenant:t1",
           owner_subject_id="alice", workspace_id="ws-a", visibility="private")

    def acl_for(subject: str, workspace: str):
        def check(entry) -> bool:
            if entry.workspace_id is not None and entry.workspace_id != workspace:
                return False
            if entry.visibility == "private":
                return entry.owner_subject_id == subject
            return True
        return check

    # bob, same workspace as alice: sees the shared note, not the other
    # workspace's, not alice's private one.
    bob = wm.recall_context(limit=10, scope="tenant:t1", acl=acl_for("bob", "ws-a"))
    assert bob == ["team note"]
    # alice sees her private note too.
    alice = wm.recall_context(limit=10, scope="tenant:t1", acl=acl_for("alice", "ws-a"))
    assert set(alice) == {"team note", "alice private note"}
    # no acl argument = historical behaviour (scope filtering only).
    assert len(wm.recall_context(limit=10, scope="tenant:t1")) == 3


def test_service_write_stamps_node_visibility_on_hot_entries(tmp_path):
    """Hot references resolve current committed ACLs without copying them."""
    from memplex.auth import AuthorizationContext, Principal
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.working_memory.enabled = True
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    try:
        from memplex.models import SourceDocument, SourceType

        ctx = AuthorizationContext(
            principal=Principal(subject_id="alice", tenant_id="t1"),
            workspace_id="ws-a",
        )
        svc.write(
            SourceDocument(
                type="conversation",
                content="Alice keeps a blue parrot named Kiwi.",
                source_type=SourceType.MEETING,
            ),
            authorization=ctx,
        )
        refs = svc._working_memory.recall_references(
            storage_namespace=svc.storage_namespace(), tenant_id="t1",
        )
        assert refs, "successful capture registers source IDs"
        nodes = svc._store_for(ctx).read_context_nodes(refs)
        assert set(refs) <= set(nodes)
        assert all(nodes[node_id].workspace_id == "ws-a" for node_id in refs)
        assert all(nodes[node_id].owner_subject_id == "alice" for node_id in refs)
        assert all(nodes[node_id].visibility in {"workspace", "user", "session"} for node_id in refs)
        assert svc._working_memory._entries == {}
    finally:
        svc.stop()


def test_reference_keys_do_not_alias_delimiters():
    wm = WorkingMemory()
    ns, tenant = "source::store", "tenant::one"
    assert wm.add_reference("id", storage_namespace=ns, tenant_id=tenant)
    assert wm.add_reference("one::id", storage_namespace=ns, tenant_id="tenant")
    assert wm.recall_references(storage_namespace=ns, tenant_id=tenant) == ("id",)
    assert wm.recall_references(storage_namespace=ns, tenant_id="tenant") == ("one::id",)
    assert wm.recall_references(storage_namespace="other", tenant_id=tenant) == ()
    assert wm.remove_reference("id", storage_namespace=ns, tenant_id="tenant") is False
    assert wm.remove_reference("id", storage_namespace=ns, tenant_id=tenant) is True
    assert wm.recall_references(storage_namespace=ns, tenant_id="tenant") == ("one::id",)


def test_all_pinned_rejects_new_reference_at_cap():
    wm = WorkingMemory(max_entries=3)
    for node_id in ("one", "two", "three"):
        assert wm.add_reference(node_id, storage_namespace="ns", tenant_id="tenant", pinned=True)
    assert len(wm) == 3
    assert wm.add_reference("new", storage_namespace="ns", tenant_id="tenant") is False
    wm.add("legacy", "uncommitted legacy string", pinned=True)
    assert len(wm) == 3
    assert wm.recall_context() == []
    assert wm.recall_references(storage_namespace="other", tenant_id="tenant") == ()


def test_pin_only_suspends_ttl(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("memplex.working_memory.time.monotonic", lambda: clock[0])
    wm = WorkingMemory(default_ttl_seconds=5)
    assert wm.add_reference("id", storage_namespace="ns", tenant_id="tenant")
    clock[0] = 104
    assert wm.set_reference_pinned("id", storage_namespace="ns", tenant_id="tenant", pinned=True)
    clock[0] = 200
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ("id",)
    assert wm.recall_references(storage_namespace="ns", tenant_id="another") == ()
    assert wm.set_reference_pinned("id", storage_namespace="ns", tenant_id="tenant", pinned=False)
    clock[0] = 204
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ("id",)
    clock[0] = 205
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ()
    assert not wm.set_reference_pinned("id", storage_namespace="ns", tenant_id="tenant", pinned=True)
    assert len(wm) == 0


def test_reference_recency_and_refresh_use_insertion_order(monkeypatch):
    monkeypatch.setattr("memplex.working_memory.time.monotonic", lambda: 100.0)
    wm = WorkingMemory()
    for node_id in ("first", "second", "third", "first"):
        assert wm.add_reference(node_id, storage_namespace="ns", tenant_id="tenant")
    assert len(wm) == 3
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ("first", "third", "second")
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant", limit=1) == ("first",)
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant", limit=0) == ()
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant", limit=-1) == ()


def test_reference_and_legacy_containers_share_one_cap():
    wm = WorkingMemory(max_entries=3)
    wm.add("legacy-old", "old")
    assert wm.add_reference("ref-old", storage_namespace="ns", tenant_id="tenant")
    wm.add("legacy-pinned", "pinned", pinned=True)
    assert wm.add_reference("ref-new", storage_namespace="ns", tenant_id="tenant")
    assert len(wm) == 3
    assert wm.recall_context() == ["pinned"]
    wm.add("legacy-new", "new")
    assert len(wm) == 3
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ("ref-new",)
    assert set(wm.recall_context()) == {"pinned", "new"}
    wm.clear()
    assert len(wm) == 0
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ()


def test_expired_entries_release_shared_capacity(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("memplex.working_memory.time.monotonic", lambda: clock[0])
    wm = WorkingMemory(max_entries=2, default_ttl_seconds=2)
    wm.add("legacy", "expiring")
    assert wm.add_reference("old", storage_namespace="ns", tenant_id="tenant")
    clock[0] = 102
    assert wm.add_reference("new", storage_namespace="ns", tenant_id="tenant", pinned=True)
    assert len(wm) == 1
    assert wm.recall_context() == []
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ("new",)


def test_reference_entries_have_only_source_and_lifecycle_data():
    from dataclasses import asdict

    wm = WorkingMemory()
    assert not wm.add_reference("", storage_namespace="ns", tenant_id="tenant")
    assert wm.add_reference("source", storage_namespace="ns", tenant_id="tenant")
    entry = next(iter(wm._references.values()))
    assert set(asdict(entry)) == {"memory_id", "ttl_seconds", "pinned", "expires_at", "insertion_sequence"}
    assert wm.recall_context() == []


def test_reference_custom_ttl_restarts_from_unpin(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("memplex.working_memory.time.monotonic", lambda: clock[0])
    wm = WorkingMemory(default_ttl_seconds=90)
    assert wm.add_reference("id", storage_namespace="ns", tenant_id="tenant", pinned=True, ttl_seconds=3)
    clock[0] = 200
    assert wm.set_reference_pinned("id", storage_namespace="ns", tenant_id="tenant", pinned=False)
    clock[0] = 203
    assert wm.recall_references(storage_namespace="ns", tenant_id="tenant") == ()
