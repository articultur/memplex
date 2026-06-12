"""Test GraphBuilder: REFERENCES, DEPENDS_ON, CONFLICTS_WITH edges,
build_from_batch, no-store degradation."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path

from memplex.models import (
    EdgeType,
    FieldValue,
    Function,
    SourceType,
)
from memplex.processing.graph_builder import GraphBuilder
from memplex.storage.lite.store import LiteMemoryStore

# ── Helpers ──────────────────────────────────────────────────────────


def _make_func(
    func_id, name, domain=None, triggers=None, actions=None, cross_refs=None
):
    return Function(
        id=func_id,
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        domain=domain,
        trigger=triggers or [],
        action=actions or [],
        cross_references=cross_refs or [],
    )


def _make_store(tmp_path=None):
    if tmp_path is None:
        import tempfile

        tmp_path = Path(tempfile.mkdtemp()) / "memory.json"
    return LiteMemoryStore(path=tmp_path)


# ── REFERENCES edge ─────────────────────────────────────────────────


class TestReferencesEdge:
    def test_cross_reference_produces_edge(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login", domain="认证模块")
        func_b = _make_func(
            "func_b", "Dashboard", domain="首页模块", cross_refs=[{"target": "Login"}]
        )

        # Store func_a so name resolution works
        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        ref_edges = [e for e in edges if e.edge_type == EdgeType.REFERENCES.value]
        assert len(ref_edges) >= 1
        assert ref_edges[0].source == "func_b"

    def test_cross_reference_with_target_id(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login")
        func_b = _make_func(
            "func_b",
            "Dashboard",
            cross_refs=[{"target_id": "func_a", "target": "Login"}],
        )

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        ref_edges = [e for e in edges if e.edge_type == EdgeType.REFERENCES.value]
        assert len(ref_edges) >= 1


# ── DEPENDS_ON edge ─────────────────────────────────────────────────


class TestDependsOnEdge:
    def test_name_reference_in_action(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login")
        func_b = _make_func(
            "func_b", "Dashboard", actions=[FieldValue(desc="uses Login module")]
        )

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        dep_edges = [e for e in edges if e.edge_type == EdgeType.DEPENDS_ON.value]
        assert len(dep_edges) >= 1
        assert dep_edges[0].target == "func_a"


# ── CONFLICTS_WITH edge ─────────────────────────────────────────────


class TestConflictsWithEdge:
    def test_conflicting_triggers_same_domain(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func(
            "func_a",
            "Login A",
            domain="认证模块",
            triggers=[FieldValue(desc="user clicks login button")],
        )
        func_b = _make_func(
            "func_b",
            "Login B",
            domain="认证模块",
            triggers=[FieldValue(desc="user clicks login button")],
        )

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        conflict_edges = [
            e for e in edges if e.edge_type == EdgeType.CONFLICTS_WITH.value
        ]
        assert len(conflict_edges) >= 1

    def test_no_conflict_different_domains(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func(
            "func_a",
            "Login A",
            domain="认证模块",
            triggers=[FieldValue(desc="user clicks login")],
        )
        func_b = _make_func(
            "func_b",
            "Login B",
            domain="支付模块",
            triggers=[FieldValue(desc="user clicks login")],
        )

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        conflict_edges = [
            e for e in edges if e.edge_type == EdgeType.CONFLICTS_WITH.value
        ]
        assert len(conflict_edges) == 0


# ── build_from_batch ─────────────────────────────────────────────────


class TestBuildFromBatch:
    def test_batch_builds_edges(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login", domain="认证模块")
        func_b = _make_func(
            "func_b",
            "Dashboard",
            domain="首页模块",
            cross_refs=[{"target_id": "func_a"}],
        )

        # Store func_a so it's available for edge detection
        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.build_from_batch([func_a, func_b])
        assert isinstance(edges, list)
        # Should have at least ASSOCIATED_WITH or BELONGS_TO edges
        assert len(edges) >= 1

    def test_batch_empty(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)
        edges = builder.build_from_batch([])
        assert edges == []


# ── BELONGS_TO edge ─────────────────────────────────────────────────


class TestBelongsToEdge:
    def test_domain_membership(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func = _make_func("func_1", "Login", domain="认证模块")
        edges = builder.process(func)

        belong_edges = [e for e in edges if e.edge_type == EdgeType.BELONGS_TO.value]
        assert len(belong_edges) >= 1
        assert "domain_" in belong_edges[0].target


# ── ASSOCIATED_WITH edge ─────────────────────────────────────────────


class TestAssociatedWithEdge:
    def test_shared_domain(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login", domain="认证模块")
        func_b = _make_func("func_b", "Logout", domain="认证模块")

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.process(func_b)
        assoc_edges = [
            e for e in edges if e.edge_type == EdgeType.ASSOCIATED_WITH.value
        ]
        assert len(assoc_edges) >= 1


# ── No store / degradation ──────────────────────────────────────────


class TestGraphBuilderNoStore:
    def test_no_store_accepted(self):
        """GraphBuilder accepts None store without raising."""
        builder = GraphBuilder(store=None)
        assert builder._store is None

    def test_process_with_no_store_uses_name_resolve(self):
        """With no store, _resolve_by_name returns None gracefully."""
        builder = GraphBuilder(store=None)
        func = _make_func("func_ns", "Test", domain="通用")
        edges = builder.process(func)
        # Should not raise; BELONGS_TO edge should still be created
        assert isinstance(edges, list)


# ── Edge dedup ──────────────────────────────────────────────────────


class TestEdgeDedup:
    def test_no_duplicate_edges(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        func_a = _make_func("func_a", "Login", domain="认证模块")
        func_b = _make_func("func_b", "Logout", domain="认证模块")

        from memplex.models import SourceDocument

        store.add(func_a, SourceDocument(type="text", source_type=SourceType.WIKI))

        edges = builder.build_from_batch([func_a, func_b])
        edge_keys = [(e.source, e.target, e.edge_type) for e in edges]
        assert len(edge_keys) == len(set(edge_keys))


# ── Cache invalidation ──────────────────────────────────────────────


class TestCacheInvalidation:
    def test_invalidate_cache(self, tmp_path):
        store = _make_store(tmp_path / "mem.json")
        builder = GraphBuilder(store=store)

        builder._funcs_cache = []
        builder.invalidate_cache()
        assert not hasattr(builder, "_funcs_cache")
