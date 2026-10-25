"""Direct unit tests for the extracted AuthorizationGate visibility rules.

The gate (``memplex/authorization.py``) was moved out of ``MemplexService``;
these tests pin its tenant/workspace/user/session ACL semantics independently
of the service so the extraction stays behaviourally faithful.
"""

import os
from types import SimpleNamespace

import pytest

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


_IDENTITY_PAIRS = [
    (None, None), ("", ""), (None, "expected"), ("", "expected"),
    ("expected", None), ("expected", ""), ("other", "expected"),
]


@pytest.mark.parametrize(("node_workspace", "request_workspace"), _IDENTITY_PAIRS)
def test_workspace_visibility_rejects_missing_empty_or_different_identity(
    node_workspace, request_workspace,
):
    # The gate also fails closed for malformed callers bypassing constructors.
    context = SimpleNamespace(
        principal=SimpleNamespace(tenant_id="tenant-a", subject_id="alice"),
        workspace_id=request_workspace, agent_id="ag", session_id="s1",
    )
    assert _gate().is_node_visible(_node(workspace_id=node_workspace), context) is False


@pytest.mark.parametrize("field", ["workspace", "subject", "session", "agent"])
@pytest.mark.parametrize(("node_value", "request_value"), _IDENTITY_PAIRS)
def test_session_visibility_rejects_missing_empty_or_different_identity(
    field, node_value, request_value,
):
    values = {"workspace": "ws-a", "subject": "alice", "session": "s1", "agent": "ag"}
    values[field] = request_value
    context = SimpleNamespace(
        principal=SimpleNamespace(tenant_id="tenant-a", subject_id=values["subject"]),
        workspace_id=values["workspace"], agent_id=values["agent"], session_id=values["session"],
    )
    node = _node(visibility="session")
    if field == "workspace":
        node.workspace_id = node_value
    elif field == "subject":
        node.owner_subject_id = node_value
        node.owner = node_value
    elif field == "session":
        node.origin_session = node_value
    else:
        node.provenance = {"agent_id": node_value}
    assert _gate().is_node_visible(node, context) is False


@pytest.mark.parametrize("explicit_lookup", [False, True])
@pytest.mark.parametrize("inherited_path", [frozenset(), frozenset({"n1"})])
def test_legacy_iterative_lineage_compatibility(monkeypatch, explicit_lookup, inherited_path):
    """Exhaust all three-node graphs against the pre-integration policy oracle."""
    from itertools import product

    from memplex.auth import local_development_context

    gate = _gate()

    def prior_visible(node, context, lookup, path):
        if not gate._is_node_in_scope(node, context):
            return False
        if not explicit_lookup and gate.identity_value(node, "tenant_id", "memplex_tenant_id") is None:
            return True
        refs = gate._source_ids(node)
        if not refs:
            return True
        node_id = str(getattr(node, "id", "") or "")
        if node_id in path:
            return False
        for ref in refs:
            try:
                source = lookup(ref)
            except OSError:
                return False
            if source is None or not prior_visible(source, context, lookup, path | {node_id}):
                return False
        return True

    # Existing own-node policies are retained, including identity-less local
    # compatibility, user grants, workspace/session, and malformed visibility.
    variants = [
        ({}, _context()),
        ({"tenant_id": None}, local_development_context()),
        ({"tenant_id": "other"}, _context()),
        ({"visibility": "user", "owner_subject_id": "bob"}, _context()),
        ({"visibility": "user", "owner_subject_id": "bob", "grant": True}, _context()),
        ({"visibility": "session"}, _context()),
        ({"visibility": "team"}, _context()),
    ]
    for masks in product(range(8), repeat=3):
        for changes, context in variants:
            nodes = {}
            for index, mask in enumerate(masks):
                namespace = {"memplex_source_refs": ",".join(f"n{bit}" for bit in range(3) if mask & (1 << bit))}
                values = dict(changes) if index == 1 else {}
                if values.pop("grant", False):
                    namespace["memplex_grants"] = "ag"
                nodes[f"n{index}"] = _node(id=f"n{index}", namespace=namespace, **values)
            if context.principal.tenant_id == "local":
                # Both the root and one source deliberately lack identity.
                nodes["n0"].tenant_id = None
            lookup = nodes.get
            monkeypatch.setattr(gate, "typed_lookup_for", lambda _context, lookup=lookup: SimpleNamespace(get=lookup))
            kwargs = {"source_lookup": lookup} if explicit_lookup else {}
            expected = prior_visible(nodes["n0"], context, lookup, inherited_path)
            assert gate.is_node_visible(nodes["n0"], context, _source_path=inherited_path, **kwargs) is expected


@pytest.mark.parametrize("condition", ["missing", "revoked", "error", "alias", "idless", "identityless", "alias_conflict"])
def test_legacy_iterative_lookup_compatibility(monkeypatch, condition):
    from memplex.auth import local_development_context

    gate = _gate()
    root = _node(id="root", namespace={"memplex_source_refs": "left,right"})
    left = _node(id="left", namespace={"memplex_source_refs": "leaf"})
    right = _node(id="right", namespace={"memplex_source_refs": "leaf"})
    leaf = _node(id="leaf")
    nodes = {"left": left, "right": right, "leaf": leaf}
    context = _context()
    if condition == "missing":
        del nodes["leaf"]
    elif condition == "revoked":
        leaf.visibility = "user"
        leaf.owner_subject_id = "bob"
    elif condition == "alias":
        leaf.id = "actual-leaf"
    elif condition == "alias_conflict":
        left.id = right.id = "shared"
        right.visibility = "user"
        right.owner_subject_id = "bob"
    elif condition == "idless":
        del leaf.id
    elif condition == "identityless":
        context = local_development_context()
        for node in [root, left, right, leaf]:
            node.tenant_id = None
        leaf.namespace = {"memplex_source_refs": "missing"}

    def lookup(node_id):
        if condition == "error" and node_id == "leaf":
            raise OSError("unavailable")
        return nodes.get(node_id)

    monkeypatch.setattr(gate, "typed_lookup_for", lambda _context: SimpleNamespace(get=lookup))
    assert gate.is_node_visible(root, context) is (condition in {"alias", "idless", "identityless"})
    assert gate.is_node_visible(root, context, source_lookup=lookup) is (condition in {"alias", "idless"})
    # A second evaluation cannot reuse an earlier positive memo after revocation.
    nodes.pop("leaf", None)
    if condition != "identityless":
        assert gate.is_node_visible(root, context) is False
