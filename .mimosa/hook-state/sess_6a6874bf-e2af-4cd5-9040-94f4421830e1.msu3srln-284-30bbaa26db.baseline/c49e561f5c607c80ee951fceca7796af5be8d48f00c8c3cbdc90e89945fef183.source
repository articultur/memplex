"""Test GraphBuilder: REFERENCES, DEPENDS_ON, CONFLICTS_WITH edges,
build_from_batch, no-store degradation."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path

import pytest

from memplex.models import (
    EdgeType,
    FieldValue,
    Function,
    SourceType,
    domain_node_id,
)
from memplex.processing.graph_builder import GraphBuilder
from memplex.storage.lite.store import LiteMemoryStore

# ── Helpers ──────────────────────────────────────────────────────────


def _make_func(func_id, name, domain=None, triggers=None, actions=None, cross_refs=None):
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


@pytest.mark.parametrize("domain", [True, 0, 1.5, {}, [], ()])
def test_domain_node_id_rejects_non_string_values(domain):
    with pytest.raises(ValueError, match="domain"):
        domain_node_id(domain)


def test_graph_builder_rechecks_mutated_domain_before_emitting_edges(tmp_path):
    store = _make_store(tmp_path / "mem.json")
    builder = GraphBuilder(store=store)
    func = _make_func("func_domain_mutated", "Domain", domain="auth")
    func.domain = 0
    with pytest.raises(ValueError, match="domain"):
        builder.process(func)


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
        func_b = _make_func("func_b", "Dashboard", actions=[FieldValue(desc="uses Login module")])

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
        conflict_edges = [e for e in edges if e.edge_type == EdgeType.CONFLICTS_WITH.value]
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
        conflict_edges = [e for e in edges if e.edge_type == EdgeType.CONFLICTS_WITH.value]
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

    def test_domain_node_id_preserves_graph_builder_legacy_whitespace_and_unicode(self, tmp_path):
        assert domain_node_id("  A  B ") == "domain___a__b_"
        assert domain_node_id("通 用") == "domain_通_用"

        builder = GraphBuilder(store=_make_store(tmp_path / "mem.json"))
        function = _make_func("func-domain-parity", "Parity", domain="  A  B ")
        edge = next(
            item
            for item in builder.process(function)
            if item.edge_type == EdgeType.BELONGS_TO.value
        )
        assert edge.target == domain_node_id(function.domain)


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
        assoc_edges = [e for e in edges if e.edge_type == EdgeType.ASSOCIATED_WITH.value]
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


# ── SEMANTIC_SIMILAR edge (Wave 2a: graph.semantic_similar_* wiring) ──


class _VocabEmbedding:
    """Deterministic embedder: dims keyed by a fixed vocabulary.

    Texts sharing vocabulary words get high cosine similarity; disjoint
    texts get 0.0 -- no reliance on ``hash()`` stability across runs.
    """

    VOCAB = ("alpha", "beta", "gamma")

    def embed(self, text):
        tokens = text.lower().split()
        vec = [float(tokens.count(word)) for word in self.VOCAB]
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return vec
        return [v / norm for v in vec]


def _make_config(threshold=0.85, max_edges=10):
    from memplex.config import MemplexConfig

    cfg = MemplexConfig()
    cfg.graph.semantic_similar_threshold = threshold
    cfg.graph.semantic_similar_max_edges = max_edges
    return cfg


class TestSemanticSimilarEdge:
    def _store_with(self, tmp_path, funcs):
        from memplex.models import SourceDocument

        store = _make_store(tmp_path / "mem.json")
        for f in funcs:
            store.add(f, SourceDocument(type="text", source_type=SourceType.WIKI))
        return store

    def test_similar_functions_produce_semantic_similar_edge(self, tmp_path):
        func_a = _make_func("func_a", "alpha helper", actions=[FieldValue(desc="alpha work")])
        func_b = _make_func("func_b", "alpha worker", actions=[FieldValue(desc="alpha work")])
        store = self._store_with(tmp_path, [func_a])

        builder = GraphBuilder(
            store=store, config=_make_config(), embedding_service=_VocabEmbedding()
        )
        edges = builder.process(func_b)
        sim_edges = [e for e in edges if e.edge_type == EdgeType.SEMANTIC_SIMILAR.value]
        assert len(sim_edges) == 1
        assert sim_edges[0].source == "func_b"
        assert sim_edges[0].target == "func_a"
        assert sim_edges[0].weight >= 0.85

    def test_dissimilar_functions_produce_no_edge(self, tmp_path):
        func_a = _make_func("func_a", "gamma helper", actions=[FieldValue(desc="gamma work")])
        func_b = _make_func("func_b", "alpha worker", actions=[FieldValue(desc="alpha work")])
        store = self._store_with(tmp_path, [func_a])

        builder = GraphBuilder(
            store=store, config=_make_config(), embedding_service=_VocabEmbedding()
        )
        edges = builder.process(func_b)
        assert [e for e in edges if e.edge_type == EdgeType.SEMANTIC_SIMILAR.value] == []

    def test_threshold_from_config_is_respected(self, tmp_path):
        """An unreachable threshold disables SEMANTIC_SIMILAR entirely."""
        func_a = _make_func("func_a", "alpha helper", actions=[FieldValue(desc="alpha work")])
        func_b = _make_func("func_b", "alpha worker", actions=[FieldValue(desc="alpha work")])
        store = self._store_with(tmp_path, [func_a])

        builder = GraphBuilder(
            store=store,
            config=_make_config(threshold=1.1),  # cosine can never exceed 1.0
            embedding_service=_VocabEmbedding(),
        )
        edges = builder.process(func_b)
        assert [e for e in edges if e.edge_type == EdgeType.SEMANTIC_SIMILAR.value] == []

    def test_max_edges_caps_edges_per_function(self, tmp_path):
        others = [
            _make_func(f"func_{i}", f"alpha helper {i}", actions=[FieldValue(desc="alpha work")])
            for i in range(3)
        ]
        func_new = _make_func("func_new", "alpha worker", actions=[FieldValue(desc="alpha work")])
        store = self._store_with(tmp_path, others)

        builder = GraphBuilder(
            store=store, config=_make_config(max_edges=2), embedding_service=_VocabEmbedding()
        )
        edges = builder.process(func_new)
        sim_edges = [e for e in edges if e.edge_type == EdgeType.SEMANTIC_SIMILAR.value]
        assert len(sim_edges) == 2

    def test_no_embedding_service_produces_no_edges(self, tmp_path):
        """Default behaviour unchanged: without an embedding service the
        SEMANTIC_SIMILAR detector is inactive."""
        func_a = _make_func("func_a", "alpha helper", actions=[FieldValue(desc="alpha work")])
        func_b = _make_func("func_b", "alpha worker", actions=[FieldValue(desc="alpha work")])
        store = self._store_with(tmp_path, [func_a])

        builder = GraphBuilder(store=store, config=_make_config())
        edges = builder.process(func_b)
        assert [e for e in edges if e.edge_type == EdgeType.SEMANTIC_SIMILAR.value] == []
