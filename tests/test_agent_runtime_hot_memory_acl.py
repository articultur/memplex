"""E2E: AgentMemoryRuntime hot-memory (working-memory) visibility roundtrips.

Real host path under test: ``capture_turn`` (the after_response hook) with
metadata ``memplex_visibility`` ∈ {user, workspace, session} must be
recallable by ``before_prompt`` exactly within the node-model boundary —
and never outside it. These tests run the REAL runtime → service → hot-tier
path (no ACL mocks), on local-process runtimes (the adapter default), whose
tenant must resolve to the same hot-tier partition on capture and recall.

Capture turns use preference-style sentences because the observation-body
extraction routes them verbatim into the hot entry content; each entry
therefore carries a unique marker word plus its own ``memplex_visibility``
metadata line, which is what the assertions match on.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.config import MemplexConfig
from memplex.service import MemplexService

# (marker, user message) pairs; the assistant reply only pads the turn.
USER_TURN = ("decaf", "Alice prefers decaf espresso beans for late shifts.")
WORKSPACE_TURN = ("staged rollouts", "The team prefers staged rollouts for pipeline deploys.")
SESSION_TURN = ("violet terminals", "Zeta prefers violet terminals after midnight.")

_VISIBILITY_BY_MARKER = {
    "decaf": "user",
    "staged rollouts": "workspace",
    "violet terminals": "session",
}


def _service(tmp_path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.working_memory.enabled = True
    cfg.working_memory.default_ttl_seconds = 600
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _runtime(
    svc: MemplexService,
    *,
    user: str,
    session: str,
    workspace: str,
    agent: str = "codex",
):
    return AgentMemoryRuntime(
        service=svc,
        agent=agent,
        user_id=user,
        session_id=session,
        project_path=workspace,
    )


def _turn_message(marker: str) -> str:
    return {
        "decaf": USER_TURN,
        "staged rollouts": WORKSPACE_TURN,
        "violet terminals": SESSION_TURN,
    }[marker][1]


def _capture(rt, marker: str) -> None:
    rt.capture_turn(
        _turn_message(marker),
        "Noted.",
        metadata={"memplex_visibility": _VISIBILITY_BY_MARKER[marker]},
    )


def _hot_block(ctx: str) -> str:
    """The injected [WORKING MEMORY] block (entries are multi-line)."""
    text = str(ctx)
    if not text.startswith("[WORKING MEMORY]"):
        return ""
    return text.split("\n\n", 1)[0]


def test_local_process_user_visibility_roundtrip(tmp_path):
    """A local-process runtime's own user-restricted hot entry is recallable
    by its own before_prompt, under the shared development partition."""
    svc = _service(tmp_path)
    try:
        rt = _runtime(
            svc, user="alice", session="s1", workspace=str(tmp_path / "wsA")
        )
        _capture(rt, "decaf")
        # The entry landed under the SAME partition recall reads: an adapter
        # local-process tenant maps onto the local development tenant.
        keys = list(svc._working_memory._entries)
        assert keys, "capture pinned hot entries"
        assert all(k.startswith("tenant:local::") for k in keys)
        block = _hot_block(rt.before_prompt("espresso").context)
        assert "decaf espresso" in block
        assert '"memplex_visibility": "user"' in block
    finally:
        svc.stop()


def test_local_process_tenant_parity_blocks_cross_principal_sharing(tmp_path):
    """Regression (independent review): two local-process runtimes sharing a
    canonical project path authenticate under DIFFERENT real tenants, and a
    workspace-visibility hot entry must not cross that boundary — the same
    tenant equality the store's ACL enforces
    (authorization.AuthorizationGate.is_node_visible). Before the fix the
    hot tier injected the entry the store refuses."""
    svc = _service(tmp_path)
    try:
        ws = str(tmp_path / "shared-ws")
        alice = _runtime(svc, user="alice", session="s1", workspace=ws)
        alice.capture_turn(
            *WORKSPACE_TURN, metadata={"memplex_visibility": "workspace"}
        )
        bob = _runtime(svc, user="bob", session="s1", workspace=ws)
        assert (
            alice.authorization_context.principal.tenant_id
            != bob.authorization_context.principal.tenant_id
        )

        bob_block = _hot_block(bob.before_prompt("deploys").context)
        assert "staged rollouts" not in bob_block
        # Store parity: bob's retrieval returns nothing either — the hot
        # tier must not be wider than the store.
        assert bob.search_memories("staged rollouts deploys").results == []

        alice_block = _hot_block(alice.before_prompt("deploys").context)
        assert "staged rollouts" in alice_block
        assert len(alice.search_memories("staged rollouts deploys").results) >= 1
    finally:
        svc.stop()


def test_same_real_tenant_shares_workspace_hot_entries(tmp_path):
    """The V4 team hot context holds WITHIN one real tenant: two
    host-identities sharing tenant + workspace share workspace entries
    (hot tier AND store); user entries stay with their owner."""
    from memplex.auth import AuthorizationContext, Principal

    svc = _service(tmp_path)
    try:

        def member(subject: str, workspace: str) -> AgentMemoryRuntime:
            return AgentMemoryRuntime(
                service=svc,
                agent="codex",
                authorization=AuthorizationContext(
                    principal=Principal(subject_id=subject, tenant_id="corp-1"),
                    workspace_id=workspace,
                    session_id="s1",
                ),
            )

        mia = member("mia", "wsT")
        _capture(mia, "staged rollouts")
        _capture(mia, "decaf")

        cole = member("cole", "wsT")
        block = _hot_block(cole.before_prompt("deploys espresso").context)
        assert "staged rollouts" in block
        assert "decaf" not in block
        # Note: the runtime's own store search binds non-team nodes to the
        # subject (agent_runtime._node_in_namespace) — that is the search
        # path's separate boundary. The hot tier's contract here is the
        # service ACL (workspace → same tenant + same workspace), which is
        # what this test pins; the cross-tenant regression test below pins
        # the store-contrast where the service ACL itself refuses.
    finally:
        svc.stop()


def test_same_tenant_cross_workspace_hot_entries_hidden(tmp_path):
    """Within one real tenant the tier stays workspace-partitioned."""
    from memplex.auth import AuthorizationContext, Principal

    svc = _service(tmp_path)
    try:

        def member(subject: str, workspace: str) -> AgentMemoryRuntime:
            return AgentMemoryRuntime(
                service=svc,
                agent="codex",
                authorization=AuthorizationContext(
                    principal=Principal(subject_id=subject, tenant_id="corp-1"),
                    workspace_id=workspace,
                    session_id="s1",
                ),
            )

        mia = member("mia", "wsA")
        _capture(mia, "staged rollouts")

        zoe = member("zoe", "wsB")
        block = _hot_block(zoe.before_prompt("deploys").context)
        assert "staged rollouts" not in block
    finally:
        svc.stop()


def test_cross_workspace_mutually_invisible(tmp_path):
    """A runtime in another workspace sees neither the user nor the
    workspace entry captured in a different workspace."""
    svc = _service(tmp_path)
    try:
        alice = _runtime(
            svc, user="alice", session="s1", workspace=str(tmp_path / "wsA")
        )
        _capture(alice, "decaf")
        _capture(alice, "staged rollouts")

        carol = _runtime(
            svc, user="carol", session="s1", workspace=str(tmp_path / "wsB")
        )
        carol_block = _hot_block(
            carol.before_prompt("decaf espresso staged rollouts").context
        )
        assert "decaf" not in carol_block
        assert "staged rollouts" not in carol_block
    finally:
        svc.stop()


def test_session_visibility_only_same_session(tmp_path):
    """Session-restricted hot entries flow only inside the capturing
    session: same identity, different session → hidden."""
    svc = _service(tmp_path)
    try:
        ws = str(tmp_path / "wsA")
        alice_s1 = _runtime(svc, user="alice", session="s1", workspace=ws)
        _capture(alice_s1, "violet terminals")

        # Fresh runtime instance, identical identity: same session sees it.
        same_session = _runtime(svc, user="alice", session="s1", workspace=ws)
        block = _hot_block(same_session.before_prompt("terminals").context)
        assert "violet terminals" in block

        # Same user + workspace, different session: hidden.
        other_session = _runtime(svc, user="alice", session="s2", workspace=ws)
        other_block = _hot_block(other_session.before_prompt("terminals").context)
        assert "violet terminals" not in other_block
    finally:
        svc.stop()


def test_session_entry_agent_bound_cross_agent_hidden(tmp_path):
    """Regression (independent review): session hot entries carry the
    capturing agent, and another agent on the same tenant/user/workspace/
    session must not receive them — mirroring the store ACL's
    origin_session/source_agent rule
    (authorization.AuthorizationGate.is_node_visible,
    agent_runtime._node_in_namespace). Before the fix the shared
    "default" session bucket let a claude-code runtime inject a codex
    session capture the store refuses."""
    svc = _service(tmp_path)
    try:
        ws = str(tmp_path / "wsA")
        codex = _runtime(svc, user="alice", session="default", workspace=ws)
        _capture(codex, "violet terminals")

        claude = _runtime(
            svc, user="alice", session="default", workspace=ws, agent="claude-code"
        )
        block = _hot_block(claude.before_prompt("violet terminals").context)
        assert "violet terminals" not in block
        # Store parity: the store refuses the same node too.
        assert claude.search_memories("violet terminals").results == []

        # Same agent, same identity: still visible.
        same_agent = _runtime(svc, user="alice", session="default", workspace=ws)
        same_block = _hot_block(same_agent.before_prompt("violet terminals").context)
        assert "violet terminals" in same_block
        assert len(same_agent.search_memories("violet terminals").results) >= 1
    finally:
        svc.stop()


def test_unknown_visibility_raises_fail_closed(tmp_path):
    """An unsupported metadata visibility is rejected at the runtime
    boundary - never silently widened onto the hot tier."""
    svc = _service(tmp_path)
    try:
        rt = _runtime(
            svc, user="alice", session="s1", workspace=str(tmp_path / "wsA")
        )
        with pytest.raises(ValueError):
            rt.capture_turn(USER_TURN[1], "Noted.", metadata={"memplex_visibility": "banana"})
        assert len(svc._working_memory) == 0
    finally:
        svc.stop()


def test_real_tenant_runtime_never_takes_compatibility_branch(tmp_path):
    """Hardening pin: inside a REAL tenant, entries claiming the
    local-development compatibility identity are judged by the normal
    rules (workspace match) — the shared-tier branch only exists inside
    the mapped ``tenant:local`` scope."""
    from memplex.auth import AuthorizationContext, Principal
    from memplex.models import SourceDocument, SourceType

    svc = _service(tmp_path)
    try:
        spoofer = AuthorizationContext(
            principal=Principal(
                subject_id="local-development", tenant_id="corp-1"
            ),
            workspace_id="local-development",
        )
        svc.write(
            SourceDocument(
                type="conversation",
                content="The contingency plan prefers stealth rollouts.",
                source_type=SourceType.MEETING,
            ),
            authorization=spoofer,
        )

        mallory = AgentMemoryRuntime(
            service=svc,
            agent="codex",
            authorization=AuthorizationContext(
                principal=Principal(subject_id="mallory", tenant_id="corp-1"),
                workspace_id="ws-mallory",
                session_id="s1",
            ),
        )
        block = _hot_block(mallory.before_prompt("contingency stealth").context)
        assert "stealth rollouts" not in block

        # Same-tenant, same-workspace colleague: normal workspace rule
        # still admits the entry.
        colleague = AgentMemoryRuntime(
            service=svc,
            agent="codex",
            authorization=AuthorizationContext(
                principal=Principal(subject_id="colleague", tenant_id="corp-1"),
                workspace_id="local-development",
                session_id="s1",
            ),
        )
        colleague_block = _hot_block(
            colleague.before_prompt("contingency stealth").context
        )
        assert "stealth rollouts" in colleague_block
    finally:
        svc.stop()


def test_service_default_writes_share_runtime_partition(tmp_path):
    """Single-machine seamlessness: service-side default captures (local
    development context) and adapter runtime captures resolve the same
    hot-tier partition, so one before_prompt sees both."""
    from memplex.models import SourceDocument, SourceType

    svc = _service(tmp_path)
    try:
        svc.write(
            SourceDocument(
                type="conversation",
                content="The release train leaves every Tuesday.",
                source_type=SourceType.MEETING,
            )
        )
        rt = _runtime(
            svc, user="alice", session="s1", workspace=str(tmp_path / "wsA")
        )
        _capture(rt, "decaf")
        block = _hot_block(rt.before_prompt("release train espresso").context)
        assert "decaf espresso" in block
        assert "Tuesday" in block
    finally:
        svc.stop()
