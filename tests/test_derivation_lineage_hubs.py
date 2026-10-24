"""ADR-013 derivation lineage for entity_hubs and consolidation.

Derived nodes (hubs from cross-session entities, consolidation from
paragraph clusters) must:
1. Carry ``trust_tier = 1`` (agent_inferred) for hubs — cross-session
   conclusions the system synthesized, not user-authored text.
2. Have ``source_paragraphs`` pointing at their source paragraph rows.
3. Carry ``memplex_source_refs`` + ``memplex_derivation`` namespace
   stamps (bind_derivation_lineage) so ``is_node_visible`` re-checks
   every source at read time — revoking any source hides the derivation.
4. Inherit the most restrictive source visibility.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.auth import local_development_context
from memplex.authorization import AuthorizationGate
from memplex.config import MemplexConfig
from memplex.entity_hubs import _sessions_from_paragraphs, build_entity_hubs
from memplex.service import MemplexService

_EXTRACT_OUTPUT = (
    "ENTITY: parrot || SESSIONS: 0,1 || EVIDENCE: [s1] Kiwi the parrot [s2] parrot named Kiwi\n"
    "ENTITY: kayak || SESSIONS: 0 || EVIDENCE: [s1] kayak the frobnitz canal\n"
)


def _service(tmp_path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def test_sessions_from_paragraphs_tracks_row_ids():
    paragraphs = {
        "p1": {"raw_text": "hello", "source": "s1", "created_at": "2026-01-01"},
        "p2": {"raw_text": "world", "source": "s1", "created_at": "2026-01-02"},
        "p3": {"raw_text": "foo", "source": "s2", "created_at": "2026-01-03"},
    }
    sessions = _sessions_from_paragraphs(paragraphs)
    assert len(sessions) == 2
    assert sessions[0]["row_ids"] == ["p1", "p2"]
    assert sessions[1]["row_ids"] == ["p3"]


def _seed_two_sessions(svc):
    """Seed two paragraph source-groups (the hub pass groups rows by
    their ``source`` hint; both write_text calls get the same hint, so
    the second row is re-pointed to a distinct session source)."""
    svc.write_text(
        "Kayak the frobnitz canal. Kiwi the blue-throated parrot lives in the attic.",
        source_type="text",
    )
    svc.write_text(
        "The parrot named Kiwi sings every morning in the garden.",
        source_type="observation",
    )
    rows = list(svc.store._paragraphs.values())
    assert len(rows) >= 2, f"need >=2 paragraphs, got {len(rows)}"
    rows[-1]["source"] = "observation-session-2"


def test_hub_carries_agent_inferred_tier_and_lineage(tmp_path):
    os.environ["MEMPLEX_ENTITY_HUBS"] = "1"
    svc = _service(tmp_path)
    try:
        _seed_two_sessions(svc)
        paragraphs = svc.store._paragraphs
        assert paragraphs

        report = build_entity_hubs(svc.store, lambda prompt: _EXTRACT_OUTPUT)
        assert report.created, f"hubs must be created, got: {report}"

        hub_id = report.created[0]
        hub = svc.store._functions[hub_id]
        # agent_inferred = 1 per ADR-013 tier system
        assert hub.trust_tier == 1, (
            f"hub must be trust_tier=1 (agent_inferred), got {hub.trust_tier}"
        )
        # Lineage: namespace carries source refs + derivation stamp
        ns = hub.namespace or {}
        assert ns.get("memplex_derivation") is not None, "hub must carry derivation stamp"
        assert ns.get("memplex_source_refs"), "hub must carry source refs"
        # Source paragraphs populated
        assert hub.source_paragraphs, "hub must reference source paragraph rows"
    finally:
        os.environ.pop("MEMPLEX_ENTITY_HUBS", None)
        svc.stop()


def test_hub_revocation_propagates(tmp_path):
    """Deleting a source paragraph row hides the hub (fail-closed lineage)."""
    os.environ["MEMPLEX_ENTITY_HUBS"] = "1"
    svc = _service(tmp_path)
    try:
        _seed_two_sessions(svc)
        report = build_entity_hubs(svc.store, lambda prompt: _EXTRACT_OUTPUT)
        assert report.created
        hub_id = report.created[0]
        hub = svc.store._functions[hub_id]

        # Hub is visible while sources live
        context = local_development_context()
        assert svc._auth.visible_node(hub_id, context) is not None

        # Revoke ALL sources (delete every source paragraph)
        for para_id in hub.source_paragraphs:
            svc.store.delete_paragraph(para_id)
        # Re-bind identity so the node has tenant_id for the visibility check
        from memplex.auth import bind_node_identity

        if hub.tenant_id is None:
            bind_node_identity(hub, context, visibility=hub.visibility or "user")
            svc.store._functions[hub_id] = hub
            svc.store._commit_current_state()

        # Hub must now be invisible: every source is gone = revoked
        assert svc._auth.visible_node(hub_id, context) is None, (
            "hub with all sources revoked must be invisible"
        )
    finally:
        os.environ.pop("MEMPLEX_ENTITY_HUBS", None)
        svc.stop()


def test_hub_visibility_clamped_to_strictest_source():
    """bind_derivation_lineage clamps derived visibility to the most
    restrictive among its sources."""
    from memplex.models import FieldValue, Function, SourceType

    node = Function(
        id="test-derive-clamp",
        name="test",
        source_type=SourceType.WIKI,
        action=[FieldValue(desc="derived from user+session")],
    )
    node.visibility = "workspace"  # pre-clamp wide

    sources = [
        SimpleNamespace(id="src-1", visibility="user"),
        SimpleNamespace(id="src-2", visibility="workspace"),
    ]
    AuthorizationGate.bind_derivation_lineage(node, sources)
    assert node.visibility == "user", (
        f"most restrictive source is 'user', got {node.visibility}"
    )
    assert node.namespace["memplex_source_refs"] == "src-1,src-2"
    assert node.namespace["memplex_derivation"] is not None
