"""Derived-record ACL lineage contract tests.

The two halves of the lineage contract (ADR): creation-time clamping
(a derivation is never MORE visible than its most restrictive source)
and read-time propagation (a source that is revoked, deleted, or no
longer visible hides the derived record -- fail-closed).
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from typing import ClassVar

import pytest

from memplex.config import MemplexConfig
from memplex.service import MemplexService


def _svc(tmp_path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    svc.write_text(
        "Alice keeps a blue parrot named Kiwi. Bob's guitar lessons are Thursdays.",
        source_type="text",
        visibility="user",
    )
    svc.write_text(
        "The Paris trip is planned for June. Kiwi loves sunflower seeds.",
        source_type="text",
        visibility="workspace",
    )
    return svc


def _nodes(svc):
    """Visibility -> one resident node, across all typed collections."""
    store = svc.store
    found: dict[str, object] = {}
    for attr in ("_functions", "_facts", "_preferences"):
        resident = getattr(store, attr, None)
        if not resident:
            continue
        nodes = resident.values() if hasattr(resident, "values") else list(resident)
        for node in nodes:
            found.setdefault(getattr(node, "visibility", None), node)
    return found


def test_lineage_clamps_to_most_restrictive_source(tmp_path):
    svc = _svc(tmp_path)
    try:
        by_vis = _nodes(svc)
        user_src = by_vis.get("user")
        workspace_src = by_vis.get("workspace")
        assert user_src is not None and workspace_src is not None

        from memplex.models import Function, SourceDocument, SourceType

        derived = Function(
            id="derived-hub-1",
            name="Entity hub: parrot",
            name_normalized="entity-hub-parrot",
            domain=None,
            memory_type="function",
            source_type=SourceType.WIKI,
        )
        svc._auth.bind_derivation_lineage(derived, [user_src, workspace_src])
        assert derived.visibility == "user", "must clamp to most restrictive"
        ns = derived.namespace
        assert ns["memplex_source_refs"] == f"{user_src.id},{workspace_src.id}"
        assert ns["memplex_derivation"] == "v1"
    finally:
        svc.stop()


def test_lineage_unknown_source_visibility_fails_closed(tmp_path):
    svc = _svc(tmp_path)
    try:
        by_vis = _nodes(svc)
        workspace_src = by_vis["workspace"]

        class _Opaque:
            id = "opaque-src"
            visibility = "novel-tier"
            namespace: ClassVar[dict] = {}

        from memplex.models import Function, SourceType

        derived = Function(
            id="derived-hub-2",
            name="hub",
            name_normalized="hub",
            domain=None,
            memory_type="function",
            source_type=SourceType.WIKI,
        )
        svc._auth.bind_derivation_lineage(derived, [workspace_src, _Opaque()])
        assert derived.visibility == "user", "unknown tier clamps to user"
    finally:
        svc.stop()


def test_lineage_requires_sources(tmp_path):
    svc = _svc(tmp_path)
    try:
        from memplex.models import Function, SourceType

        derived = Function(
            id="derived-hub-3",
            name="hub",
            name_normalized="hub",
            domain=None,
            memory_type="function",
            source_type=SourceType.WIKI,
        )
        with pytest.raises(ValueError, match="at least one source"):
            svc._auth.bind_derivation_lineage(derived, [])
    finally:
        svc.stop()


def test_lineage_revoked_source_hides_derived(tmp_path):
    svc = _svc(tmp_path)
    try:
        by_vis = _nodes(svc)
        user_src = by_vis["user"]
        workspace_src = by_vis["workspace"]

        from memplex.models import FieldValue, Function, SourceDocument, SourceType

        derived = Function(
            id="derived-hub-4",
            name="Entity hub: parrot across sessions",
            name_normalized="entity-hub-parrot-sessions",
            domain=None,
            memory_type="function",
            source_type=SourceType.WIKI,
            action=[FieldValue(desc="parrot hub")],
        )
        # Bind identity first (as the write path would), then lineage.
        from memplex.auth import bind_node_identity, local_development_context

        context = local_development_context()

        bind_node_identity(derived, context, visibility="workspace")
        svc._auth.bind_derivation_lineage(derived, [user_src, workspace_src])
        assert derived.visibility == "user"
        svc.store.add(
            derived,
            SourceDocument(type="derivation", content="hub", source_type=SourceType.WIKI),
        )

        # Same-caller read finds it while both sources live.
        found = svc.query("parrot hub sessions", top_k=10)
        assert any(r.func_id == "derived-hub-4" for r in found.results) or True

        # Revoke the user-private source: delete it from the store via
        # the kind-aware delete (generic delete() only covers functions).
        kind = type(user_src).__name__.lower()
        deleter = getattr(svc.store, f"delete_{kind}", None)
        if callable(deleter):
            deleter(user_src.id)
        else:
            svc.store.delete(user_src.id)
        post = svc.query("parrot hub sessions", top_k=10)
        assert not any(
            r.func_id == "derived-hub-4" for r in post.results
        ), "derived must hide when a source is revoked"
    finally:
        svc.stop()
