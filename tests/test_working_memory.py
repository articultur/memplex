"""Tests for the working-memory hot-context tier (memplex/working_memory.py)."""

from __future__ import annotations

import os
import time

import pytest

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
    """End to end: enabled tier captures typed writes and prepends on recall."""
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
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

        svc.write(
            SourceDocument(
                type="conversation",
                content="The deployment pipeline now requires two reviewers.",
                source_type=SourceType.MEETING,
            )
        )
        runtime = AgentMemoryRuntime(service=svc, agent="codex")
        ctx = runtime._recall("pipeline", source="live").context
        assert "[WORKING MEMORY]" in ctx
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


def test_hot_entry_visible_tenant_parity():
    """Store-boundary parity (independent review): an entry authored under
    one real principal tenant is denied to a recalling runtime of another
    tenant, regardless of workspace/visibility match."""
    from memplex.adapters.agent_runtime import hot_entry_visible

    entry = _entry(
        tenant_id="corp-1",
        owner_subject_id="alice",
        workspace_id="ws-a",
        visibility="workspace",
    )
    kwargs = {
        "effective_tenant_id": "corp-1",
        "subject_id": "bob",
        "workspace_id": "ws-a",
        "session_id": "s1",
        "agent_id": "codex",
    }
    assert hot_entry_visible(entry, recalling_tenant_id="corp-1", **kwargs)
    assert not hot_entry_visible(entry, recalling_tenant_id="corp-2", **kwargs)


def test_hot_entry_visible_tenantless_entries_follow_visibility():
    """Entries without tenant provenance (sleep-time precompute, direct
    tier users) have no tenant identity to cross: scope partition plus the
    visibility rules decide, as before."""
    from memplex.adapters.agent_runtime import hot_entry_visible

    kwargs = {
        "recalling_tenant_id": "corp-1",
        "effective_tenant_id": "corp-1",
        "subject_id": "alice",
        "workspace_id": "ws-a",
        "session_id": "s1",
        "agent_id": "codex",
    }
    assert hot_entry_visible(_entry(visibility="tenant"), **kwargs)
    user_entry = _entry(owner_subject_id="alice", workspace_id="ws-a", visibility="user")
    assert hot_entry_visible(user_entry, **kwargs)
    assert not hot_entry_visible(
        user_entry,
        **{**kwargs, "subject_id": "mallory"},
    )
    ws_entry = _entry(owner_subject_id="alice", workspace_id="ws-a", visibility="workspace")
    assert hot_entry_visible(ws_entry, **kwargs)
    assert not hot_entry_visible(
        ws_entry,
        **{**kwargs, "workspace_id": "ws-b"},
    )
    session_entry = _entry(
        owner_subject_id="alice",
        workspace_id="ws-a",
        visibility="session",
        session_id="s1",
        agent_id="codex",
    )
    assert hot_entry_visible(session_entry, **kwargs)
    assert not hot_entry_visible(
        session_entry,
        **{**kwargs, "session_id": "s2"},
    )
    assert not hot_entry_visible(_entry(visibility="banana"), **kwargs)
    assert hot_entry_visible(_entry(visibility="private", owner_subject_id="alice"), **kwargs)


def test_hot_entry_visible_session_agent_bound():
    """Store parity on the agent dimension (independent review): a session
    entry is visible only to the capturing agent — the store's session
    branch additionally requires source_agent == recalling agent."""
    from memplex.adapters.agent_runtime import hot_entry_visible

    entry = _entry(
        tenant_id="corp-1",
        owner_subject_id="alice",
        workspace_id="ws-a",
        visibility="session",
        session_id="s1",
        agent_id="codex",
    )
    kwargs = {
        "recalling_tenant_id": "corp-1",
        "effective_tenant_id": "corp-1",
        "subject_id": "alice",
        "workspace_id": "ws-a",
        "session_id": "s1",
    }
    assert hot_entry_visible(entry, **{**kwargs, "agent_id": "codex"})
    # Another agent on the same tenant/user/workspace/session: denied.
    assert not hot_entry_visible(entry, **{**kwargs, "agent_id": "claude-code"})
    # An agent-less session entry never matches a named agent
    # (store parity: a session node without source_agent is denied).
    agentless = _entry(
        tenant_id="corp-1",
        owner_subject_id="alice",
        workspace_id="ws-a",
        visibility="session",
        session_id="s1",
    )
    assert not hot_entry_visible(agentless, **{**kwargs, "agent_id": "codex"})


def test_hot_entry_visible_compatibility_tier_scoped_to_local():
    """The local-development shared tier is honoured only inside the mapped
    tenant:local scope, only for the compatibility identity, and only for
    entries whose tenant stamp is the development tenant (or absent)."""
    from memplex.adapters.agent_runtime import hot_entry_visible

    dev = {"owner_subject_id": "local-development", "workspace_id": "local-development"}
    local_kwargs = {
        "recalling_tenant_id": "local-process-abc",
        "effective_tenant_id": "local",
        "subject_id": "alice",
        "workspace_id": "/tmp/ws",
        "session_id": "s1",
        "agent_id": "codex",
    }
    # Service default write (tenant "local"): shared single-machine tier.
    assert hot_entry_visible(
        _entry(tenant_id="local", visibility="workspace", **dev), **local_kwargs
    )
    assert hot_entry_visible(
        _entry(visibility="tenant", **dev), **local_kwargs
    )
    # A real-tenant runtime never takes the compatibility branch: a corp
    # writer claiming the compatibility identity is judged by normal rules.
    assert not hot_entry_visible(
        _entry(tenant_id="corp-2", visibility="workspace", **dev),
        recalling_tenant_id="corp-1",
        effective_tenant_id="corp-1",
        subject_id="x",
        workspace_id="ws-a",
        session_id="s1",
        agent_id="codex",
    )
    # A local-process-authored entry masquerading with the compatibility
    # identity cannot enter the shared tier either (tenant stamp betrays it).
    assert not hot_entry_visible(
        _entry(tenant_id="local-process-xyz", visibility="workspace", **dev),
        **local_kwargs,
    )
    # Dev user/session entries stay owner/session/agent bound.
    assert hot_entry_visible(
        _entry(tenant_id="local", visibility="user", **dev),
        **{**local_kwargs, "subject_id": "local-development"},
    )
    session_kwargs = {
        **local_kwargs,
        "subject_id": "local-development",
        "session_id": "local-development",
        "agent_id": "memplex",
    }
    assert hot_entry_visible(
        _entry(
            tenant_id="local",
            visibility="session",
            session_id="local-development",
            agent_id="memplex",
            **dev,
        ),
        **session_kwargs,
    )
    # ...and a different agent never sees the dev session entry.
    assert not hot_entry_visible(
        _entry(
            tenant_id="local",
            visibility="session",
            session_id="local-development",
            agent_id="memplex",
            **dev,
        ),
        **{**session_kwargs, "agent_id": "codex"},
    )


def _entry(**kwargs):
    from memplex.working_memory import WorkingMemoryEntry

    return WorkingMemoryEntry(content="x", **kwargs)


def test_add_rejects_unknown_visibility_fail_closed():
    """The hot tier never silently widens: unknown visibility raises.

    Regression guard for the vocabulary break where a host "user"/"session"
    write was silently coerced to tenant-wide visibility.
    """
    wm = WorkingMemory()
    for bad in ("public", "banana", ""):
        with pytest.raises(ValueError):
            wm.add("k", "v", visibility=bad)
    assert len(wm) == 0


def test_private_alias_maps_to_user_not_rejected():
    """Legacy "private" writes map onto "user" (alias, not rejection)."""
    wm = WorkingMemory()
    wm.add("k", "legacy private note", visibility="private", owner_subject_id="alice")
    (entry,) = list(wm._entries.values())
    assert entry.visibility == "user"


def test_add_stores_session_id():
    wm = WorkingMemory()
    wm.add(
        "k",
        "session note",
        visibility="session",
        owner_subject_id="alice",
        session_id="s1",
    )
    (entry,) = list(wm._entries.values())
    assert entry.session_id == "s1"


def test_recall_acl_filters_user_workspace_session_entries():
    """Hot-tier ACL mirrors the node model: user→owner, workspace→ws,
    session→owner+session, tenant→explicit tenant-wide, unknown→denied."""

    wm = WorkingMemory()
    wm.add("shared", "team note", scope="tenant:t1",
           workspace_id="ws-a", visibility="workspace")
    wm.add("other-ws", "other workspace note", scope="tenant:t1",
           workspace_id="ws-b", visibility="workspace")
    wm.add("alice-user", "alice user note", scope="tenant:t1",
           owner_subject_id="alice", workspace_id="ws-a", visibility="user")
    wm.add("alice-session", "alice session note", scope="tenant:t1",
           owner_subject_id="alice", workspace_id="ws-a", visibility="session",
           session_id="s1")
    wm.add("legacy", "legacy private note", scope="tenant:t1",
           owner_subject_id="alice", workspace_id="ws-a", visibility="private")

    def acl_for(subject: str, workspace: str, session: str = "s1"):
        def check(entry) -> bool:
            if entry.workspace_id is not None and entry.workspace_id != workspace:
                return False
            visibility = entry.visibility
            if visibility == "tenant":
                return True
            if visibility == "workspace":
                return entry.workspace_id == workspace
            if visibility == "user" or visibility == "private":
                return entry.owner_subject_id == subject
            if visibility == "session":
                return (
                    entry.owner_subject_id == subject
                    and entry.session_id == session
                )
            return False

        return check

    # bob, same workspace as alice: sees the shared note only - not the
    # other workspace's, not alice's user note, not her session note.
    bob = wm.recall_context(limit=10, scope="tenant:t1", acl=acl_for("bob", "ws-a"))
    assert bob == ["team note"]
    # alice, same session: sees her user + session notes (private alias
    # already mapped to user at write time).
    alice = wm.recall_context(limit=10, scope="tenant:t1", acl=acl_for("alice", "ws-a"))
    assert set(alice) == {"team note", "alice user note", "alice session note",
                          "legacy private note"}
    # alice, later session: the session note drops, the user note stays.
    later = wm.recall_context(
        limit=10, scope="tenant:t1", acl=acl_for("alice", "ws-a", session="s2")
    )
    assert set(later) == {"team note", "alice user note", "legacy private note"}
    # same owner, different workspace: the hot tier is workspace-partitioned,
    # so even alice's own user note captured in ws-a stays in ws-a.
    cross_ws = wm.recall_context(
        limit=10, scope="tenant:t1", acl=acl_for("alice", "ws-b")
    )
    assert cross_ws == ["other workspace note"]
    # no acl argument = historical behaviour (scope filtering only).
    assert len(wm.recall_context(limit=10, scope="tenant:t1")) == 5


def test_service_write_stamps_node_visibility_on_hot_entries(tmp_path):
    """The write path stamps the writer's ACL triple on hot entries so
    the injection filter has provenance to enforce."""
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
        entries = [
            e
            for e in svc._working_memory._entries.values()
        ]
        assert entries, "capture pins hot entries"
        assert all(e.workspace_id == "ws-a" for e in entries)
        assert all(e.owner_subject_id == "alice" for e in entries)
        # The capturing principal's REAL tenant is stamped for recall-time
        # store-boundary parity.
        assert all(e.tenant_id == "t1" for e in entries)
        # The context carries no agent → no agent stamp (session entries
        # without one are denied at recall, mirroring the store).
        assert all(e.agent_id is None for e in entries)
        # Real node-model vocabulary only - the host's user/session writes
        # must never be coerced to tenant-wide (or legacy "private").
        assert all(e.visibility in {"user", "workspace", "session"} for e in entries)
        # Session identity is threaded through: the context carries no
        # session, so entries record the effective "default" bucket.
        assert all(e.session_id == "default" for e in entries)
    finally:
        svc.stop()


def test_service_local_process_tenant_shares_development_scope(tmp_path):
    """Capture/recall scope unification: an adapter runtime authenticates
    under a per-user ``local-process-*`` tenant, and its hot entries must
    land under the SAME partition its recall reads (the local development
    tenant) - while a real tenant keeps its own partition."""
    from memplex.auth import AuthorizationContext, Principal
    from memplex.config import MemplexConfig
    from memplex.models import SourceDocument, SourceType
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.working_memory.enabled = True
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    try:
        runtime_ctx = AuthorizationContext(
            principal=Principal(
                subject_id="alice",
                tenant_id="local-process-deadbeefcafe",
                roles=frozenset({"local-process"}),
                authentication_id="local-process",
            ),
            workspace_id="ws-a",
            session_id="s1",
            agent_id="codex",
        )
        svc.write(
            SourceDocument(
                type="conversation",
                content="Alice keeps a blue parrot named Kiwi.",
                source_type=SourceType.MEETING,
            ),
            authorization=runtime_ctx,
        )
        corp_ctx = AuthorizationContext(
            principal=Principal(subject_id="bob", tenant_id="corp-1"),
            workspace_id="ws-b",
            agent_id="claude-code",
        )
        svc.write(
            SourceDocument(
                type="conversation",
                content="The deployment pipeline now requires two reviewers.",
                source_type=SourceType.MEETING,
            ),
            authorization=corp_ctx,
        )
        entries = svc._working_memory._entries
        assert entries, "capture pins hot entries"
        local_keys = [k for k in entries if "parrot" in entries[k].content.lower()]
        corp_keys = [k for k in entries if "reviewers" in entries[k].content.lower()]
        assert local_keys, "local-process capture produced hot entries"
        assert corp_keys, "corp capture produced hot entries"
        # Same effective tenant mapping as the recall side: the runtime's
        # local-process tenant is captured under the development partition.
        assert all(k.startswith("tenant:local::") for k in local_keys)
        # A real tenant is partitioned as itself (no cross-tenant sharing).
        assert all(k.startswith("tenant:corp-1::") for k in corp_keys)
        local_entries = [entries[k] for k in local_keys]
        assert all(e.session_id == "s1" for e in local_entries)
        # The REAL (unmapped) capturing tenant is stamped on the entries
        # for recall-time store-boundary parity.
        assert all(
            e.tenant_id == "local-process-deadbeefcafe" for e in local_entries
        )
        assert all(e.agent_id == "codex" for e in local_entries)
        corp_entries = [entries[k] for k in corp_keys]
        assert all(e.tenant_id == "corp-1" for e in corp_entries)
        assert all(e.agent_id == "claude-code" for e in corp_entries)
        assert all(e.visibility in {"user", "workspace", "session"} for e in local_entries)
    finally:
        svc.stop()
