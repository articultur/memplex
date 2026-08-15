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
    base = dict(
        id=fid,
        tenant_id="t1",
        owner_subject_id=owner,
        workspace_id="w1",
        subject="deploy",
        predicate="uses",
        object_="blue-green",
        updated_at="2026-08-15T00:00:00+00:00",
        valid_from="2026-08-15T00:00:00+00:00",
        visibility="user",
    )
    base.update(kw)
    return Fact(**base)




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
        with pytest.raises(Exception):
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
                ),
                None,
            )
        runtime = AgentMemoryRuntime(service=svc, agent="codex")
        filters = runtime._domain_scoped_filters()
        # Every branch pins domain=database
        assert filters and all(branch.get("domain") == "database" for branch in filters)

        result = runtime.search_memories("tuning components", top_k=10)
        assert all(r.domain == "database" for r in result.results)

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
