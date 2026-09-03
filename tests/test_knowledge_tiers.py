"""Tests for knowledge tiering: promote / share_with / agent-domain binding.

The team-knowledge model: personal memory (default) → promoted knowledge
(personal/domain/team tiers) with team tier = workspace-shared; cross-agent
grants let one agent's private memory be visible to a named peer agent;
agent-domain binding scopes recall to the agent's bound knowledge domains.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.config import MemplexConfig
from memplex.models import Fact, Function, SourceType
from memplex.service import MemplexService


def _service(tmp_path, **cfg_over):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    for key, value in cfg_over.items():
        obj = cfg
        parts = key.split(".")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _private_fact(fid, owner="alice", **kw):
    defaults = {"id": fid, "tenant_id": "t1", "owner_subject_id": owner, "workspace_id": "w1", "subject": "deploy", "predicate": "uses", "object_": "blue-green", "updated_at": "2026-08-15T00:00:00+00:00", "valid_from": "2026-08-15T00:00:00+00:00", "visibility": "user"}
    defaults.update(kw)
    return Fact(**defaults)




def _alice():
    from memplex.auth import AuthorizationContext, Principal

    return AuthorizationContext(
        principal=Principal(tenant_id="t1", subject_id="alice"),
        workspace_id="w1",
    )

# ── promote ──────────────────────────────────────────────────────────


def test_promote_to_team_widens_visibility_and_stamps_provenance(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-team"))
        result = svc.promote("f-team", "team", authorization=_alice())
        assert result["tier"] == "team"
        node = svc.store.get_fact("f-team")
        assert node.knowledge_tier == "team"
        assert node.visibility == "workspace"
        assert node.provenance["promoted_to_tier"] == "team"
        assert node.version == 2
    finally:
        svc.stop()


def test_promote_domain_tier_keeps_private_visibility(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-domain"))
        svc.promote("f-domain", "domain", authorization=_alice())
        node = svc.store.get_fact("f-domain")
        assert node.knowledge_tier == "domain"
        assert node.visibility == "user"  # domain ≠ automatically shared
    finally:
        svc.stop()


def test_promote_rejects_unknown_tier_and_missing_memory(tmp_path):
    svc = _service(tmp_path)
    try:
        with pytest.raises(ValueError, match="tier"):
            svc.promote("whatever", "enterprise", authorization=_alice())
        with pytest.raises(Exception, match="not found|missing|unknown"):
            svc.promote("does-not-exist", "team", authorization=_alice())
    finally:
        svc.stop()


# ── share_with (cross-agent grant) ──────────────────────────────────


def test_share_with_grants_named_agent_visibility(tmp_path):
    from memplex.auth import AuthorizationContext, Principal

    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-share", owner="alice"))
        result = svc.share_with("f-share", "agent-bob", authorization=_alice())
        assert result["granted_agents"] == ["agent-bob"]

        # Bob (different subject, same tenant) could NOT see it before the
        # grant; the gate now admits him via the namespace grant.
        bob = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="bob"),
            workspace_id="w1",
            agent_id="agent-bob",
        )
        assert svc._auth.is_node_visible(svc.store.get_fact("f-share"), bob) is True
        # An unrelated agent still cannot.
        eve = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="eve"),
            workspace_id="w1",
            agent_id="agent-eve",
        )
        assert svc._auth.is_node_visible(svc.store.get_fact("f-share"), eve) is False
    finally:
        svc.stop()


def test_share_with_is_idempotent_and_owner_gated(tmp_path):
    from memplex.auth import AuthorizationContext, Principal

    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-idem", owner="alice"))
        svc.share_with("f-idem", "agent-bob", authorization=_alice())
        result = svc.share_with("f-idem", "agent-bob", authorization=_alice())
        assert result["granted_agents"] == ["agent-bob"]  # no duplicates

        # A non-owner probing a user-private memory gets the uniform
        # opaque not-found (fail-closed before the PermissionError stage).
        mallory = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="mallory"),
            workspace_id="w1",
        )
        from memplex.auth import MemoryNotFoundError

        with pytest.raises(MemoryNotFoundError):
            svc.share_with("f-idem", "agent-bob", authorization=mallory)
    finally:
        svc.stop()


def test_grants_survive_serialization_roundtrip(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-round", owner="alice"))
        svc.share_with("f-round", "agent-carol", authorization=_alice())
        node = svc.store.get_fact("f-round")
        assert node.namespace["memplex_grants"] == "agent-carol"
    finally:
        svc.stop()


# ── agent-domain binding ─────────────────────────────────────────────


def test_domain_binding_filters_runtime_recall(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    svc = _service(tmp_path, **{"agent_domains.agent_domains": {"codex": ["database"]}})
    try:
        from memplex.auth import AuthorizationContext, Principal

        auth = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="dev-user"),
            workspace_id="w1",
            agent_id="codex",
        )
        for fid, name, domain in (
            ("fn-db", "postgres tuning", "database"),
            ("fn-ui", "react components", "frontend"),
        ):
            svc.store.add(
                Function(
                    id=fid,
                    name=name,
                    name_normalized=name.lower(),
                    domain=domain,
                    memory_type="function",
                    source_type=SourceType.MEETING,
                    tenant_id="t1",
                    owner_subject_id="dev-user",
                    workspace_id="w1",
                    visibility="workspace",
                ),
                None,
            )
        runtime = AgentMemoryRuntime(service=svc, agent="codex", authorization=auth)
        filters = runtime._domain_scoped_filters()
        # Every branch pins domain=database
        assert filters and all(branch.get("domain") == "database" for branch in filters)

        result = runtime.search_memories("tuning components", top_k=10)
        # Non-empty assertion (S7 fix): the original all() was vacuously
        # true on an empty result set — domain binding was actually broken.
        assert len(result.results) > 0, "domain-bound recall returned nothing"
        assert all(r.domain == "database" for r in result.results)
        assert not any(r.domain == "frontend" for r in result.results)

        unbound = AgentMemoryRuntime(service=svc, agent="hermes")
        assert all(
            branch.get("domain") is None for branch in unbound._domain_scoped_filters()
        )
    finally:
        svc.stop()


def test_unbound_agent_sees_all_domains(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    svc = _service(tmp_path)
    try:
        runtime = AgentMemoryRuntime(service=svc, agent="codex")
        filters = runtime._domain_scoped_filters()
        assert filters  # visibility branches present, no domain pinning
    finally:
        svc.stop()


def test_team_tier_cross_user_recall_at_runtime(tmp_path):
    """S4 fix: team-tier knowledge is recallable by a DIFFERENT user's
    runtime in the same workspace — the team branch bypasses the
    per-user pinning that previously returned zero."""
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    from memplex.auth import AuthorizationContext, Principal

    svc = _service(tmp_path)
    try:
        # Alice captures a function and promotes it to team tier.
        svc.store.add(
            Function(
                id="team-fn",
                name="team convention",
                name_normalized="team convention",
                domain=None,
                memory_type="function",
                source_type=SourceType.MEETING,
                visibility="user",
                tenant_id="t1",
                owner_subject_id="alice",
                workspace_id="w1",
            ),
            None,
        )
        alice = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="alice"),
            workspace_id="w1",
        )
        svc.promote("team-fn", "team", authorization=alice)

        # Bob's runtime in the same workspace recalls it.
        bob_runtime = AgentMemoryRuntime(
            service=svc,
            agent="hermes",
            authorization=AuthorizationContext(
                principal=Principal(tenant_id="t1", subject_id="bob"),
                workspace_id="w1",
            ),
        )
        result = bob_runtime.search_memories("team convention", top_k=10)
        assert any(r.func_id == "team-fn" for r in result.results), (
            "team-tier knowledge not visible cross-user at runtime"
        )
    finally:
        svc.stop()


def test_v1_grant_holder_cannot_promote(tmp_path):
    """V1 fix: a cross-agent grant holder can read but NEVER widen —
    promoting someone else's private memory to team would leak it
    workspace-wide through a read-only grant."""
    from memplex.auth import AuthorizationContext, MemoryNotFoundError, Principal

    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-v1", owner="alice"))
        alice = _alice()
        svc.share_with("f-v1", "agent-bob", authorization=alice)
        bob = AuthorizationContext(
            principal=Principal(tenant_id="t1", subject_id="bob"),
            workspace_id="w1",
            agent_id="agent-bob",
        )
        # Bob CAN read (grant) but CANNOT promote — either opaque
        # not-found (if ACL blocks) or PermissionError (if owner check).
        with pytest.raises((PermissionError, MemoryNotFoundError)):
            svc.promote("f-v1", "team", authorization=bob)
        # Alice's memory remains private.
        node = svc.store.get_fact("f-v1")
        assert node.knowledge_tier is None
        assert node.visibility == "user"
    finally:
        svc.stop()


def test_v2_comma_injection_rejected(tmp_path):
    """V2 fix: agent_id with commas cannot split into multiple grants."""
    svc = _service(tmp_path)
    try:
        svc.store.add_fact(_private_fact("f-v2", owner="alice"))
        with pytest.raises(ValueError, match="comma"):
            svc.share_with("f-v2", "trusted,mallory", authorization=_alice())
    finally:
        svc.stop()
