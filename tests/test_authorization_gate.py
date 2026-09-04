"""Direct unit tests for the extracted AuthorizationGate visibility rules.

The gate (``memplex/authorization.py``) was moved out of ``MemplexService``;
these tests pin its tenant/workspace/user/session ACL semantics independently
of the service so the extraction stays behaviourally faithful.
"""

import os
from types import SimpleNamespace

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.auth import AuthorizationContext, Principal
from memplex.authorization import AuthorizationGate


def _gate(profile: str = "development") -> AuthorizationGate:
    cfg = SimpleNamespace(deployment=SimpleNamespace(profile=profile))
    return AuthorizationGate(cfg, lambda: None, lambda: None)


def _context(tenant="tenant-a", subject="alice", workspace="ws-a", session="s1", agent="ag"):
    return AuthorizationContext(
        principal=Principal(
            tenant_id=tenant, subject_id=subject, roles=frozenset({"agent"}),
        ),
        workspace_id=workspace,
        agent_id=agent,
        session_id=session,
    )


def _node(**kw):
    base = {"tenant_id": "tenant-a", "owner_subject_id": "alice", "owner": "alice", "workspace_id": "ws-a", "visibility": "workspace", "namespace": {}, "provenance": {"agent_id": "ag"}, "origin_session": "s1"}
    base.update(kw)
    return SimpleNamespace(**base)


# ── is_production / require_authorization ────────────────────────────


def test_is_production_reflects_profile():
    assert _gate("production").is_production() is True
    assert _gate("development").is_production() is False


def test_require_authorization_passes_through_context():
    gate = _gate("development")
    ctx = _context()
    assert gate.require_authorization(ctx) is ctx


def test_require_authorization_production_requires_context():
    gate = _gate("production")
    try:
        gate.require_authorization(None)
        assert False, "expected PermissionError"
    except PermissionError:
        pass


# ── is_node_visible: tenant fail-closed ──────────────────────────────


def test_is_node_visible_rejects_other_tenant():
    gate = _gate()
    ctx = _context(tenant="tenant-a")
    node = _node(tenant_id="tenant-b")
    assert gate.is_node_visible(node, ctx) is False


def test_is_node_visible_workspace_scope():
    gate = _gate()
    ctx = _context(workspace="ws-a")
    assert gate.is_node_visible(_node(visibility="workspace", workspace_id="ws-a"), ctx) is True
    assert gate.is_node_visible(_node(visibility="workspace", workspace_id="other"), ctx) is False


def test_is_node_visible_user_scope():
    gate = _gate()
    ctx = _context(subject="alice")
    assert gate.is_node_visible(_node(visibility="user", owner_subject_id="alice"), ctx) is True
    assert gate.is_node_visible(_node(visibility="user", owner_subject_id="bob"), ctx) is False


def test_is_node_visible_session_scope_requires_all_four():
    gate = _gate()
    ctx = _context(workspace="ws-a", subject="alice", session="s1", agent="ag")
    assert gate.is_node_visible(_node(visibility="session"), ctx) is True
    # Wrong session → invisible
    assert gate.is_node_visible(_node(visibility="session", origin_session="other"), ctx) is False
    # Wrong agent → invisible
    assert gate.is_node_visible(
        _node(visibility="session", provenance={"agent_id": "other"}), ctx
    ) is False


def test_is_node_visible_identityless_only_via_local_dev():
    gate = _gate()
    ctx = _context()  # not a local-development context
    assert gate.is_node_visible(_node(tenant_id=None), ctx) is False
