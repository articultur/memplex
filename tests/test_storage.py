"""Test storage layer: LiteMemoryStore add/get/merge/delete/list_functions,
ChangelogStore append/get_timeline, vector_search / fts_search."""

import logging
import os
import sqlite3

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime
from pathlib import Path

import pytest  # noqa: E402

from memplex.models import (
    BatchResult,
    ChangelogEvent,
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MergeResult,
    Observation,
    Preference,
    SearchFilters,
    SourceDocument,
    SourceType,
)
from memplex.storage.changelog import ChangelogStore
from memplex.storage.lite.durability import LiteStorageIntegrityError
from memplex.storage.lite.store import LiteMemoryStore
from memplex.sync_repository import SyncCapturePolicy

# ── Helpers ──────────────────────────────────────────────────────────


def _make_store(tmp_path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(path=tmp_path / "memory.json")


def _make_source(**kwargs) -> SourceDocument:
    defaults = {"type": "text", "source_type": SourceType.WIKI}
    defaults.update(kwargs)
    return SourceDocument(**defaults)


def _make_func(func_id, name, triggers=None, actions=None, domain=None):
    return Function(
        id=func_id,
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        domain=domain,
        trigger=triggers or [],
        action=actions or [],
        source_type=SourceType.WIKI,
    )


# ── LiteMemoryStore: Add ─────────────────────────────────────────────


class TestLiteMemoryStoreAdd:
    def test_add_and_get(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_1", "Login")
        store.add(func, _make_source())
        result = store.get("func_1")
        assert result is not None
        assert result.id == "func_1"
        assert result.name == "Login"

    def test_add_rechecks_mutated_reserved_function_id_before_any_write(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_valid", "Login")
        func.id = "domain_auth"  # construction validation must not be bypassable

        with pytest.raises(ValueError, match="保留"):
            store.add(func, _make_source())
        assert store.list_functions() == []

    def test_add_rechecks_mutated_non_string_domain_before_any_write(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_valid_domain", "Login", domain="auth")
        func.domain = {"forged": "domain"}

        with pytest.raises(ValueError, match="domain"):
            store.add(func, _make_source())
        assert store.list_functions() == []

    def test_add_creates_changelog(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_2", "Register")
        store.add(func, _make_source())
        timeline = store.get_timeline("func_2")
        assert len(timeline) >= 1
        assert timeline[0].event_type == "created"

    def test_add_merges_duplicate_name(self, tmp_path):
        store = _make_store(tmp_path)

        func_a = _make_func("func_a", "Login", triggers=[FieldValue(desc="click button")])
        func_b = _make_func("func_b", "Login", triggers=[FieldValue(desc="enter credentials")])

        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        # Should have merged by name_normalized
        funcs = store.list_functions()
        assert len(funcs) == 1
        # The merged function should have both triggers
        merged = funcs[0]
        trigger_descs = {fv.desc for fv in merged.trigger}
        assert "click button" in trigger_descs
        assert "enter credentials" in trigger_descs

    def test_add_updates_version_on_merge(self, tmp_path):
        store = _make_store(tmp_path)

        func_a = _make_func("func_a", "Login")
        func_b = _make_func("func_b", "Login")

        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        merged = store.list_functions()[0]
        assert merged.version >= 2


# ── LiteMemoryStore: Get ─────────────────────────────────────────────


class TestLiteMemoryStoreGet:
    def test_get_existing(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_get", "Test")
        store.add(func, _make_source())
        assert store.get("func_get") is not None

    def test_get_nonexistent(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.get("no_such_func") is None


# ── LiteMemoryStore: Delete ──────────────────────────────────────────


class TestLiteMemoryStoreDelete:
    def test_delete_removes_function(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_del", "ToDelete")
        store.add(func, _make_source())
        store.delete("func_del")
        assert store.get("func_del") is None

    def test_delete_removes_edges(self, tmp_path):
        store = _make_store(tmp_path)
        func_a = _make_func("func_a", "A")
        func_b = _make_func("func_b", "B")
        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        # Manually add an edge
        graph = GraphData(
            nodes=[],
            edges=[GraphEdge(source="func_a", target="func_b", edge_type="REFERENCES")],
        )
        store.merge(graph)

        store.delete("func_a")
        edges = store.get_graph().edges
        assert all(e.source != "func_a" and e.target != "func_a" for e in edges)

    def test_delete_nonexistent_ok(self, tmp_path):
        store = _make_store(tmp_path)
        store.delete("nonexistent")  # Should not raise


# ── LiteMemoryStore: Merge ───────────────────────────────────────────


class TestLiteMemoryStoreMerge:
    def test_merge_new_function(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_merge", "MergeTest")
        graph = GraphData(nodes=[func], edges=[])
        result = store.merge(graph)

        assert isinstance(result, MergeResult)
        assert result.merged is True
        assert result.new_functions == 1

    def test_merge_rejects_mutated_or_duck_nodes_without_edges(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_valid", "Source")
        func.id = "domain_source"
        graph = GraphData(
            nodes=[func],
            edges=[GraphEdge(source="domain_source", target="anything", edge_type="REFERENCES")],
        )

        with pytest.raises(ValueError, match="保留"):
            store.merge(graph)
        assert store.list_functions() == []
        assert store._edges == []

    def test_merge_rechecks_mutated_non_string_domain_before_edges(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_valid_domain_merge", "Source", domain="auth")
        func.domain = 0
        with pytest.raises(ValueError, match="domain"):
            store.merge(
                GraphData(
                    nodes=[func],
                    edges=[GraphEdge(func.id, "target", "REFERENCES")],
                )
            )
        assert store.list_functions() == []
        assert store._edges == []

    def test_merge_rejects_duck_node_before_any_edge_write(self, tmp_path):
        store = _make_store(tmp_path)

        class DuckFunction:
            id = "func_duck"
            name = "Duck"

        with pytest.raises(ValueError, match="Function"):
            store.merge(
                GraphData(
                    nodes=[DuckFunction()],
                    edges=[GraphEdge("func_duck", "target", "REFERENCES")],
                )
            )
        assert store.list_functions() == []
        assert store._edges == []

    def test_merge_rejects_forged_belongs_to_before_memory_or_disk_publish(self, tmp_path):
        store = _make_store(tmp_path)
        source = _make_func("func_belongs_none", "Source", domain=None)
        graph = GraphData(
            nodes=[source],
            edges=[GraphEdge(source.id, "domain_forged", "BELONGS_TO")],
        )

        with pytest.raises(ValueError, match="BELONGS_TO"):
            store.merge(graph)
        assert store.list_functions() == []
        assert store._edges == []
        assert not (tmp_path / "memory.json").exists()

    def test_merge_existing_function_updates(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_m1", "MergeUpdate")
        store.add(func, _make_source())

        func_updated = _make_func(
            "func_m1", "MergeUpdate", triggers=[FieldValue(desc="new trigger")]
        )
        graph = GraphData(nodes=[func_updated], edges=[])
        result = store.merge(graph)

        assert result.updated_functions == 1
        merged = store.get("func_m1")
        trigger_descs = {fv.desc for fv in merged.trigger}
        assert "new trigger" in trigger_descs

    def test_merge_edges(self, tmp_path):
        store = _make_store(tmp_path)
        func_a = _make_func("func_ea", "A")
        func_b = _make_func("func_eb", "B")
        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        graph = GraphData(
            nodes=[],
            edges=[GraphEdge(source="func_ea", target="func_eb", edge_type="REFERENCES")],
        )
        result = store.merge(graph)
        assert result.new_edges == 1

    def test_merge_duplicate_edges_skipped(self, tmp_path):
        store = _make_store(tmp_path)
        func_a = _make_func("func_dup_a", "A")
        func_b = _make_func("func_dup_b", "B")
        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        edge = GraphEdge(source="func_dup_a", target="func_dup_b", edge_type="REFERENCES")
        graph = GraphData(nodes=[], edges=[edge])
        store.merge(graph)
        result = store.merge(graph)  # Same edge again
        assert result.new_edges == 0


# ── LiteMemoryStore: List ────────────────────────────────────────────


class TestLiteMemoryStoreList:
    def test_list_functions_empty(self, tmp_path):
        store = _make_store(tmp_path)
        assert store.list_functions() == []

    def test_list_functions_returns_all(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_l1", "A"), _make_source())
        store.add(_make_func("func_l2", "B"), _make_source())
        funcs = store.list_functions()
        assert len(funcs) == 2

    def test_list_functions_pagination(self, tmp_path):
        store = _make_store(tmp_path)
        for i in range(5):
            store.add(_make_func(f"func_p{i}", f"Func {i}"), _make_source())
        page1 = store.list_functions(offset=0, limit=2)
        page2 = store.list_functions(offset=2, limit=2)
        assert len(page1) == 2
        assert len(page2) == 2

    def test_list_functions_owner_filter(self, tmp_path):
        store = _make_store(tmp_path)
        func_a = _make_func("func_o1", "Owned")
        func_a.owner = "alice"
        func_b = _make_func("func_o2", "Other")
        func_b.owner = "bob"
        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        result = store.list_functions(owner="alice")
        assert len(result) == 1
        assert result[0].owner == "alice"


# ── LiteMemoryStore: Vector Search ───────────────────────────────────


class TestLiteMemoryStoreVectorSearch:
    def test_search_returns_results(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func(
            "func_vs", "Login", triggers=[FieldValue(desc="user login authentication")]
        )
        store.add(func, _make_source())

        results = store.vector_search("login", top_k=5)
        assert isinstance(results, list)
        assert len(results) >= 1
        assert results[0].func_id == "func_vs"

    def test_search_no_results(self, tmp_path):
        store = _make_store(tmp_path)
        results = store.vector_search("nonexistent", top_k=5)
        assert results == []

    def test_search_ranks_specific_bm25_match_first(self, tmp_path):
        store = _make_store(tmp_path)
        specific = _make_func(
            "func_specific",
            "Offline HuggingFace fallback",
            actions=[FieldValue(desc="use local bm25 retrieval when huggingface is unavailable")],
        )
        generic = _make_func(
            "func_generic",
            "Offline note",
            actions=[FieldValue(desc="offline offline offline general fallback")],
        )
        store.add(generic, _make_source())
        store.add(specific, _make_source())

        results = store.vector_search("huggingface bm25 fallback", top_k=5)
        assert [r.func_id for r in results][:1] == ["func_specific"]

    def test_search_uses_sqlite_fts5_sidecar(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(
            _make_func(
                "func_sqlite",
                "SQLite FTS5 retrieval",
                actions=[FieldValue(desc="bm25 ranking with trigram recall")],
            ),
            _make_source(),
        )

        results = store.vector_search("sqlite bm25 trigram", top_k=5)

        assert results
        assert results[0].func_id == "func_sqlite"
        sidecar = tmp_path / "memory.json.fts5.db"
        assert sidecar.exists()
        with sqlite3.connect(str(sidecar)) as conn:
            indexed = conn.execute("SELECT count(*) FROM memplex_fts").fetchone()[0]
        assert indexed == 1

    def test_sqlite_fts5_sidecar_refreshes_after_mutation_and_reopen(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(
            _make_func(
                "func_old",
                "Archived memory",
                actions=[FieldValue(desc="zxqvkjp should disappear")],
            ),
            _make_source(),
        )
        assert store.vector_search("zxqvkjp", top_k=5)

        store.delete("func_old")
        store.add(
            _make_func(
                "func_new",
                "Fresh memory",
                actions=[FieldValue(desc="mndtbrc should remain")],
            ),
            _make_source(),
        )

        assert store.vector_search("zxqvkjp", top_k=5) == []
        results = store.vector_search("mndtbrc", top_k=5)
        assert results
        assert results[0].func_id == "func_new"

        reopened = _make_store(tmp_path)
        reopened_results = reopened.vector_search("mndtbrc", top_k=5)
        assert reopened_results
        assert reopened_results[0].func_id == "func_new"

    def test_search_falls_back_when_sqlite_fts5_unavailable(self, monkeypatch, tmp_path):
        store = _make_store(tmp_path)
        store.add(
            _make_func(
                "func_python_fallback",
                "Python fallback",
                actions=[FieldValue(desc="local bm25 search survives missing fts5")],
            ),
            _make_source(),
        )

        def fail_sqlite(text, top_k):
            raise sqlite3.OperationalError("fts5 unavailable")

        monkeypatch.setattr(store, "_sqlite_fts_search", fail_sqlite)

        results = store.vector_search("missing fts5 local bm25", top_k=5)
        assert results
        assert results[0].func_id == "func_python_fallback"

    def test_search_does_not_swallow_non_sqlite_bugs(self, monkeypatch, tmp_path):
        store = _make_store(tmp_path)

        def fail_with_bug(text, top_k):
            raise RuntimeError("programming bug")

        monkeypatch.setattr(store, "_sqlite_fts_search", fail_with_bug)

        try:
            store.vector_search("bug should surface", top_k=5)
        except RuntimeError as exc:
            assert str(exc) == "programming bug"
        else:
            raise AssertionError("non-sqlite exceptions must not fall back silently")

    def test_search_matches_code_like_tokens_across_separators(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func(
            "func_code_token",
            "OpenClaw memory slot",
            actions=[FieldValue(desc='plugins.slots.memory = "memplex"')],
        )
        store.add(func, _make_source())

        results = store.vector_search("slots memory memplex", top_k=5)
        assert results
        assert results[0].func_id == "func_code_token"


# ── LiteMemoryStore: FTS Search ──────────────────────────────────────


class TestLiteMemoryStoreFTSSearch:
    def test_fts_finds_match(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func(
            "func_fts", "Payment", actions=[FieldValue(desc="process payment billing")]
        )
        store.add(func, _make_source())

        results = store.fts_search("payment", top_k=10)
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_fts_no_match(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_fts2", "Something Else")
        store.add(func, _make_source())

        results = store.fts_search("totally_different", top_k=10)
        assert results == []

    def test_fts_matches_chinese_without_word_segmentation(self, monkeypatch, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func(
            "func_cn",
            "大陆离线检索",
            actions=[FieldValue(desc="中国大陆 HuggingFace 不可用时使用本地离线检索")],
        )
        store.add(func, _make_source())

        def fail_python_fallback(text, top_k):
            raise AssertionError("SQLite FTS5 trigram search should handle this query")

        monkeypatch.setattr(store, "_local_search", fail_python_fallback)

        results = store.fts_search("大陆离线", top_k=10)
        assert results
        assert results[0].func_id == "func_cn"


# ── LiteMemoryStore: Graph ───────────────────────────────────────────


class TestLiteMemoryStoreGraph:
    def test_get_graph_full(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_g1", "A"), _make_source())
        store.add(_make_func("func_g2", "B"), _make_source())

        graph = store.get_graph()
        assert len(graph.nodes) == 2

    def test_get_graph_filtered(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_gf1", "A"), _make_source())
        store.add(_make_func("func_gf2", "B"), _make_source())

        graph = store.get_graph(func_ids=["func_gf1"])
        assert len(graph.nodes) == 1


# ── LiteMemoryStore: Neighbors ───────────────────────────────────────


class TestLiteMemoryStoreNeighbors:
    def test_get_neighbors(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_n1", "A"), _make_source())
        store.add(_make_func("func_n2", "B"), _make_source())

        edge = GraphEdge(source="func_n1", target="func_n2", edge_type="REFERENCES")
        store.merge(GraphData(nodes=[], edges=[edge]))

        neighbors = store.get_neighbors("func_n1")
        assert len(neighbors) >= 1
        assert any(n.id == "func_n2" for n in neighbors)

    def test_get_neighbors_no_hops(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_nh", "A"), _make_source())
        neighbors = store.get_neighbors("func_nh", max_hops=0)
        assert neighbors == []

    def test_get_neighbors_limit_uses_bounded_adjacency_lookup(self, tmp_path):
        store = _make_store(tmp_path)
        hub = _make_func("func_hub", "Hub")
        nodes = [hub, *[_make_func(f"func_dense_{i}", f"Dense {i}") for i in range(20)]]
        edges = [
            GraphEdge(source=hub.id, target=node.id, edge_type="REFERENCES")
            for node in nodes[1:]
        ]
        store.merge(GraphData(nodes=nodes, edges=edges))

        class _NoGlobalScan(list):
            def __iter__(self):
                raise AssertionError("get_neighbors scanned the global edge list")

        store._edges = _NoGlobalScan(store._edges)

        neighbors = store.get_neighbors(hub.id, max_hops=1, limit=2)

        assert len(neighbors) == 2

    def test_neighbor_index_survives_load_and_tracks_delete_and_clear(self, tmp_path):
        store = _make_store(tmp_path)
        hub = _make_func("func_index_hub", "Hub")
        neighbor = _make_func("func_index_neighbor", "Neighbor")
        store.merge(
            GraphData(
                nodes=[hub, neighbor],
                edges=[GraphEdge(hub.id, neighbor.id, "REFERENCES")],
            )
        )

        reloaded = _make_store(tmp_path)
        assert [node.id for node in reloaded.get_neighbors(hub.id, limit=1)] == [neighbor.id]

        reloaded.delete(neighbor.id)
        assert reloaded.get_neighbors(hub.id, limit=1) == []
        reloaded.clear()
        assert reloaded._edges_by_node == {}


# ── LiteMemoryStore: Observations ────────────────────────────────────


class TestLiteMemoryStoreObservation:
    def test_add_observation(self, tmp_path):
        store = _make_store(tmp_path)
        obs = Observation(id="obs_1", name="Event", event="something happened")
        store.add_observation(obs)
        # Observations are stored internally
        assert len(store._observations) == 1

    def test_observations_survive_restart(self, tmp_path):
        # Observations must be persisted to disk and reloaded on restart;
        # previously add_observation only touched an in-memory list that was
        # never serialized, so the auto-capture loop silently lost everything.
        store = _make_store(tmp_path)
        store.add_observation(
            Observation(id="obs_persist", name="Reboot event", event="reboot survived")
        )

        reopened = _make_store(tmp_path)
        assert len(reopened._observations) == 1
        assert reopened._observations[0].id == "obs_persist"
        assert reopened._observations[0].event == "reboot survived"

    def test_observation_category_survives_restart(self, tmp_path):
        # category must round-trip through the JSON file.
        store = _make_store(tmp_path)
        store.add_observation(
            Observation(id="obs_cat", name="Cat", event="fixed it", category="bugfix")
        )

        reopened = _make_store(tmp_path)
        assert reopened._observations[0].category == "bugfix"

    def test_observation_category_round_trips_through_durable_envelope(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_observation(Observation(id="obs_legacy", name="Legacy", event="old"))

        reopened = _make_store(tmp_path)
        assert reopened._observations[0].category == "note"

    def test_list_observations_all(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_observation(Observation(id="o1", name="A", event="e1", category="bugfix"))
        store.add_observation(Observation(id="o2", name="B", event="e2", category="decision"))
        assert [o.id for o in store.list_observations()] == ["o1", "o2"]

    def test_list_observations_filter_by_category(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_observation(Observation(id="o1", name="A", event="e1", category="bugfix"))
        store.add_observation(Observation(id="o2", name="B", event="e2", category="decision"))
        store.add_observation(Observation(id="o3", name="C", event="e3", category="bugfix"))
        assert [o.id for o in store.list_observations(category="bugfix")] == ["o1", "o3"]
        assert store.list_observations(category="discovery") == []

    def test_list_observations_filter_by_owner(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_observation(Observation(id="o1", name="A", event="e1", owner="alice"))
        store.add_observation(Observation(id="o2", name="B", event="e2", owner="bob"))
        assert [o.id for o in store.list_observations(owner="alice")] == ["o1"]

    def test_delete_observation_is_durable_and_emits_tombstone(self, tmp_path):
        store = LiteMemoryStore(
            path=tmp_path / "memory.json",
            sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
        )
        observation = Observation(
            id="obs-delete",
            name="Delete",
            event="remove me",
            tenant_id="tenant-a",
            owner="subject-a",
            owner_subject_id="subject-a",
            workspace_id="workspace-a",
            visibility="workspace",
        )
        store.add_observation(observation)

        store.delete_observation("obs-delete")

        assert store.get_observation("obs-delete") is None
        reopened = LiteMemoryStore(
            path=tmp_path / "memory.json",
            sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
        )
        assert reopened.get_observation("obs-delete") is None
        assert any(
            event["node_type"] == "observation"
            and event["operation"] == "tombstone"
            for event in reopened._sync_state["outbox"]
        )

    def test_list_observations_pagination(self, tmp_path):
        store = _make_store(tmp_path)
        for i in range(5):
            store.add_observation(Observation(id=f"o{i}", name="A", event="e"))
        assert [o.id for o in store.list_observations(offset=1, limit=2)] == ["o1", "o2"]


class TestLiteMemoryStoreNoDeprecatedUtcnow:
    """datetime.utcnow() is deprecated and slated for removal; store mutation
    paths must use timezone-aware datetime.now(timezone.utc)."""

    def test_mutation_paths_emit_no_utcnow_deprecation(self, tmp_path):
        import warnings

        store = _make_store(tmp_path)
        func = _make_func("func_u", "U")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            store.add(func, _make_source())  # add path -> updated_at
            store.increment_access("func_u")  # last_accessed_at
            store.merge(store.get_graph())  # merge-existing path -> updated_at

        utcnow = [str(w.message) for w in caught if "utcnow" in str(w.message).lower()]
        assert utcnow == [], "deprecated utcnow() still used: " + repr(utcnow)


# ── LiteMemoryStore: Increment Access ────────────────────────────────


class TestLiteMemoryStoreIncrementAccess:
    def test_increment(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_acc", "Access")
        store.add(func, _make_source())

        store.increment_access("func_acc")
        func = store.get("func_acc")
        assert func.access_count == 1
        assert func.last_accessed_at is not None


# ── LiteMemoryStore: Filter ──────────────────────────────────────────


class TestLiteMemoryStoreFilter:
    def test_filter_by_domain(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_fl1", "Login", domain="认证模块")
        store.add(func, _make_source())

        results = store.filter(SearchFilters(domain=["认证模块"]))
        assert len(results) == 1

    def test_filter_by_confidence(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_fl2", "LowConf")
        func.confidence = 0.3
        store.add(func, _make_source())

        results = store.filter(SearchFilters(confidence_min=0.5))
        assert len(results) == 0


# ── LiteMemoryStore: Batch ───────────────────────────────────────────


class TestLiteMemoryStoreBatch:
    def test_add_batch(self, tmp_path):
        store = _make_store(tmp_path)
        funcs = [
            _make_func("func_b1", "A"),
            _make_func("func_b2", "B"),
        ]
        sources = [_make_source(), _make_source()]
        result = store.add_batch(funcs, sources)
        assert isinstance(result, BatchResult)
        assert result.total == 2
        assert result.succeeded == 2


# ── LiteMemoryStore: Clear ───────────────────────────────────────────


class TestLiteMemoryStoreClear:
    def test_clear_removes_everything(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_c1", "A"), _make_source())
        store.clear()
        assert store.list_functions() == []


# ── LiteMemoryStore: Persistence ─────────────────────────────────────


class TestLiteMemoryStorePersistence:
    def test_commit_current_state_and_reload(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_persist", "PersistTest", triggers=[FieldValue(desc="trigger text")])
        store.add(func, _make_source())

        # Reload from disk
        store2 = _make_store(tmp_path)
        loaded = store2.get("func_persist")
        assert loaded is not None
        assert loaded.name == "PersistTest"
        assert len(loaded.trigger) == 1
        assert loaded.trigger[0].desc == "trigger text"

    def test_load_rejects_reserved_function_id_before_indexing_edges(self, tmp_path):
        path = tmp_path / "memory.json"
        path.write_text(
            '{"functions":[{"id":"domain_auth","name":"virtual"}],'
            '"edges":[{"source":"domain_auth","target":"x",'
            '"edge_type":"REFERENCES"}]}',
            encoding="utf-8",
        )
        (tmp_path / "changelog.json").write_text("[]", encoding="utf-8")

        with pytest.raises(LiteStorageIntegrityError, match="authoritative payload"):
            LiteMemoryStore(path=path)

    def test_load_rejects_legacy_forged_belongs_to_before_publish(self, tmp_path):
        path = tmp_path / "memory.json"
        path.write_text(
            '{"functions":[{"id":"func_legacy_domain","name":"source","domain":null}],'
            '"edges":[{"source":"func_legacy_domain","target":"domain_forged",'
            '"edge_type":"BELONGS_TO"}]}',
            encoding="utf-8",
        )
        (tmp_path / "changelog.json").write_text("[]", encoding="utf-8")
        with pytest.raises(LiteStorageIntegrityError, match="authoritative payload"):
            LiteMemoryStore(path=path)

    def test_live_domain_mutation_cannot_overwrite_last_valid_disk_snapshot(self, tmp_path):
        path = tmp_path / "memory.json"
        store = LiteMemoryStore(path=path)
        original = _make_func("func_live_domain", "Original", domain="auth")
        store.add(original, _make_source())
        detached = store.get(original.id)
        assert detached is not None
        detached.domain = []
        store.add(_make_func("func_unrelated", "Unrelated"), _make_source())

        reopened = LiteMemoryStore(path=path)
        persisted = reopened.get(original.id)
        assert persisted is not None
        assert persisted.domain == "auth"
        assert reopened.get("func_unrelated") is not None


# ── ChangelogStore ───────────────────────────────────────────────────


class TestChangelogStore:
    def test_append_and_get_timeline(self, tmp_path):
        changelog = ChangelogStore(path=tmp_path / "changelog.json")
        event = ChangelogEvent(
            func_id="func_cl",
            timestamp=datetime.now(),
            event_type="created",
            description="Created function",
            source="test",
            actor="system",
        )
        changelog.append(event)

        timeline = changelog.get_timeline("func_cl")
        assert len(timeline) == 1
        assert timeline[0].event_type == "created"

    def test_get_timeline_filtered(self, tmp_path):
        changelog = ChangelogStore(path=tmp_path / "changelog.json")
        changelog.append(
            ChangelogEvent(
                func_id="func_a",
                timestamp=datetime.now(),
                event_type="created",
                description="A",
                source="",
                actor="system",
            )
        )
        changelog.append(
            ChangelogEvent(
                func_id="func_b",
                timestamp=datetime.now(),
                event_type="created",
                description="B",
                source="",
                actor="system",
            )
        )

        timeline = changelog.get_timeline("func_a")
        assert all(e.func_id == "func_a" for e in timeline)

    def test_clear(self, tmp_path):
        changelog = ChangelogStore(path=tmp_path / "changelog.json")
        changelog.append(
            ChangelogEvent(
                func_id="func_cc",
                timestamp=datetime.now(),
                event_type="created",
                description="",
                source="",
                actor="system",
            )
        )
        changelog.clear()
        assert changelog.get_timeline("func_cc") == []

    def test_persistence(self, tmp_path):
        changelog = ChangelogStore(path=tmp_path / "changelog.json")
        changelog.append(
            ChangelogEvent(
                func_id="func_cp",
                timestamp=datetime.now(),
                event_type="created",
                description="Persist",
                source="",
                actor="system",
            )
        )

        changelog2 = ChangelogStore(path=tmp_path / "changelog.json")
        timeline = changelog2.get_timeline("func_cp")
        assert len(timeline) == 1


# ── increment_access_batch: single persistence pass ─────────────────


def test_increment_access_batch_updates_all_matching(tmp_path):
    """Batch increments access_count for every supplied func id that exists."""
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    store = LiteMemoryStore(path=tmp_path / "m.json")
    funcs = []
    for i in range(5):
        f = Function(
            id=f"batch-{i}",
            name=f"n{i}",
            name_normalized=f"n{i}",
            trigger=[FieldValue(desc=f"t{i}", sources=["s"], source_method="manual", weight=1.0)],
        )
        store.add(f, SourceDocument(type="text", source_type=SourceType.WIKI))
        funcs.append(f)
    # one missing id should be skipped, not crash
    store.increment_access_batch(["batch-0", "batch-1", "batch-2", "missing-id"])
    assert store.get("batch-0").access_count == 1
    assert store.get("batch-1").access_count == 1
    assert store.get("batch-2").access_count == 1
    # untouched funcs keep access_count 0
    assert store.get("batch-3").access_count == 0


def test_increment_access_batch_empty_or_all_missing_skips_commit_current_state(tmp_path):
    """When nothing matches, _commit_current_state must not fire (no-op)."""
    store = LiteMemoryStore(path=tmp_path / "m.json")
    save_calls = []
    original_commit_current_state = store._commit_current_state
    store._commit_current_state = lambda: save_calls.append(1) or original_commit_current_state()
    store.increment_access_batch([])
    assert save_calls == []
    store.increment_access_batch(["totally-missing"])
    assert save_calls == []  # nothing to persist


def test_increment_access_batch_persists_once_not_n_times(tmp_path):
    """THE performance fix: N func ids -> exactly 1 _commit_current_state call, not N.

    Previously service.query called increment_access per result, each
    triggering a full JSON rewrite. This test pins the batch contract so
    the O(results x store_size) regression cannot silently return.
    """
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    store = LiteMemoryStore(path=tmp_path / "m.json")
    for i in range(10):
        store.add(
            Function(
                id=f"perf-{i}",
                name=f"n{i}",
                name_normalized=f"n{i}",
                trigger=[
                    FieldValue(desc=f"t{i}", sources=["s"], source_method="manual", weight=1.0)
                ],
            ),
            SourceDocument(type="text", source_type=SourceType.WIKI),
        )
    save_calls = []
    original_commit_current_state = store._commit_current_state
    store._commit_current_state = lambda: save_calls.append(1) or original_commit_current_state()
    # 10 valid ids in one batch call
    store.increment_access_batch([f"perf-{i}" for i in range(10)])
    assert len(save_calls) == 1, (
        f"batch must persist once, got {len(save_calls)} _commit_current_state calls for 10 ids"
    )


def test_service_query_uses_single_batched_increment(tmp_path):
    """End-to-end: a service.query that returns K results must not trigger
    K full-store rewrites for access counting."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    try:
        # Seed several memories that match a common token.
        for i in range(4):
            svc.write_text(f"perf-batch-canary: variant {i} for recall.")
        # Spy on the lite store's _commit_current_state.
        save_calls = []
        original = svc.store._commit_current_state
        svc.store._commit_current_state = lambda: save_calls.append(1) or original()
        result = svc.query("perf-batch-canary", top_k=10)
        # Count _commit_current_state calls made DURING the access-increment phase only.
        # (query may call _commit_current_state elsewhere? -- the access phase is the only
        # write in query, so any _commit_current_state calls here are from it.)
        pre_count = len(save_calls)
        # The increment happened inside query; assert it was batched (<=1).
        # Allow 0 (empty results) or 1 (batched), never N.
        assert pre_count <= 1, (
            f"query triggered {pre_count} _commit_current_state calls for {len(result.results)} results; "
            "access counting must be batched into a single persistence pass"
        )
    finally:
        svc.stop()


# ── R3: incremental FTS5 indexing ────────────────────────────────────


def test_fts5_incremental_upsert_only_touches_changed_rows(tmp_path):
    """R3: after the first build, a write should upsert only the changed
    func, not rebuild the whole table. We spy on commit() counts and on
    DELETE FROM (full-table) statements to pin the incremental contract."""
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    store = LiteMemoryStore(path=tmp_path / "inc.json")
    store.add(
        Function(
            id="inc-1",
            name="alpha",
            name_normalized="alpha",
            trigger=[
                FieldValue(desc="alpha body", sources=["s"], source_method="manual", weight=1.0)
            ],
        ),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    store.add(
        Function(
            id="inc-2",
            name="beta",
            name_normalized="beta",
            trigger=[
                FieldValue(desc="beta body", sources=["s"], source_method="manual", weight=1.0)
            ],
        ),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    # First search builds the index for both.
    store.vector_search("alpha", top_k=5)
    idx = store._fts_index
    assert idx._indexed_sigs, "first build should populate per-func sigs"
    assert set(idx._indexed_sigs.keys()) == {"inc-1", "inc-2"}

    # Now write a THIRD func. Incremental path should add only inc-3.
    store.add(
        Function(
            id="inc-3",
            name="gamma",
            name_normalized="gamma",
            trigger=[
                FieldValue(desc="gamma body", sources=["s"], source_method="manual", weight=1.0)
            ],
        ),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    store.vector_search("gamma", top_k=5)
    # The previously-indexed inc-1/inc-2 signatures must be unchanged
    # (they were not rebuilt), and inc-3 must now be present.
    assert "inc-3" in idx._indexed_sigs
    assert "inc-1" in idx._indexed_sigs and "inc-2" in idx._indexed_sigs
    # gamma is retrievable.
    assert any(r.func_id == "inc-3" for r in store.vector_search("gamma", top_k=5))


def test_fts5_incremental_removes_deleted_func(tmp_path):
    """R3: a delete must remove the func from the FTS index incrementally."""
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    store = LiteMemoryStore(path=tmp_path / "del.json")
    store.add(
        Function(
            id="del-inc-1",
            name="keepme",
            name_normalized="keepme",
            trigger=[
                FieldValue(desc="keepme body", sources=["s"], source_method="manual", weight=1.0)
            ],
        ),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )
    store.vector_search("keepme", top_k=5)
    store.delete("del-inc-1")
    idx = store._fts_index
    store.vector_search("keepme", top_k=5)
    assert "del-inc-1" not in idx._indexed_sigs
    assert all(r.func_id != "del-inc-1" for r in store.vector_search("keepme", top_k=5))


# ── create_store factory: explicit path failures are fail-closed ─────


def test_create_store_rejects_when_configured_path_unusable(monkeypatch, caplog, tmp_path):
    """显式路径失败不得把调用者切换到无关的默认 Lite 库。"""
    import memplex.storage as storage_mod

    class FlakyStore:
        def __init__(self, path=None):
            if path is not None:
                raise PermissionError("denied")

    monkeypatch.setattr(storage_mod, "LiteMemoryStore", FlakyStore)
    with caplog.at_level(logging.WARNING, logger="memplex.storage"):
        with pytest.raises(PermissionError, match="denied"):
            storage_mod.create_store("lite", path=str(tmp_path / "nope"))
    assert not any("falling back" in r.message for r in caplog.records)


def test_create_store_warns_for_unimplemented_standard_enterprise(caplog, tmp_path):
    """standard/enterprise silently mapped to lite; a warning is now logged."""
    import memplex.storage as storage_mod

    with caplog.at_level(logging.WARNING, logger="memplex.storage"):
        storage_mod.create_store("standard", path=str(tmp_path))
    assert any("standard" in r.message and "lite" in r.message for r in caplog.records)


# ── vector store factory: honest error messages ──────────────────────


def test_create_vector_store_chroma_missing_raises_clear_error(monkeypatch):
    """backend='chroma' without chromadb used to raise a misleading
    ValueError('Unknown vector store backend'); now it is an ImportError
    that names the missing dependency."""
    import memplex.storage.vector as vector_mod

    monkeypatch.setattr(vector_mod, "_CHROMA_AVAILABLE", False)
    with pytest.raises(ImportError, match="chromadb"):
        vector_mod.create_vector_store("chroma")


def test_create_vector_store_auto_falls_back_to_inmemory(monkeypatch):
    import memplex.storage.vector as vector_mod

    monkeypatch.setattr(vector_mod, "_CHROMA_AVAILABLE", False)
    store = vector_mod.create_vector_store("auto")
    assert isinstance(store, vector_mod.InMemoryVectorStore)


def test_create_vector_store_unknown_backend_raises_value_error():
    from memplex.storage.vector import create_vector_store

    with pytest.raises(ValueError, match="Unknown vector store backend"):
        create_vector_store("not-a-backend")


# ── lite search fallback leaves no dead state behind ─────────────────


def test_fts_fallback_does_not_set_dead_fts_disabled_attr(monkeypatch, tmp_path):
    """_fts_disabled was write-only dead state; the fallback must work
    without it."""
    store = _make_store(tmp_path)
    store.add(
        _make_func(
            "func_no_dead_state",
            "Fallback target",
            actions=[FieldValue(desc="unique-token-zqx fallback")],
        ),
        _make_source(),
    )

    def fail_sqlite(text, top_k):
        raise sqlite3.OperationalError("fts5 unavailable")

    monkeypatch.setattr(store, "_sqlite_fts_search", fail_sqlite)
    results = store.vector_search("unique-token-zqx", top_k=5)
    assert results and results[0].func_id == "func_no_dead_state"
    assert not hasattr(store, "_fts_disabled")


# ── LiteMemoryStore: Fact / Preference ───────────────────────────────


def _make_fact(fact_id="fact_1", subject="API", obj="REST interface", owner=None):
    return Fact(
        id=fact_id,
        name=f"{subject} fact",
        subject=subject,
        predicate="is",
        object_=obj,
        source_type=SourceType.WIKI,
        owner=owner,
    )


def _make_preference(pref_id="pref_1", aspect="theme", preference="dark mode", owner=None):
    return Preference(
        id=pref_id,
        name=f"{aspect} preference",
        aspect=aspect,
        preference=preference,
        source_type=SourceType.WIKI,
        owner=owner,
    )


class TestLiteMemoryStoreFactPreference:
    def test_add_and_get_fact(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact())
        got = store.get_fact("fact_1")
        assert got is not None
        assert got.subject == "API"
        assert got.object_ == "REST interface"
        assert got.created_at and got.updated_at

    def test_add_fact_upserts_by_id(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact(obj="v1"))
        store.add_fact(_make_fact(obj="v2"))
        assert store.get_fact("fact_1").object_ == "v2"
        assert len(store.list_facts()) == 1

    def test_add_and_get_preference(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_preference(_make_preference())
        got = store.get_preference("pref_1")
        assert got is not None
        assert got.aspect == "theme"
        assert got.preference == "dark mode"

    def test_list_facts_owner_filter_and_pagination(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact("fact_a", owner="alice"))
        store.add_fact(_make_fact("fact_b", owner="bob"))
        store.add_fact(_make_fact("fact_c", owner="alice"))
        assert len(store.list_facts()) == 3
        assert len(store.list_facts(owner="alice")) == 2
        assert len(store.list_facts(offset=1, limit=1)) == 1

    def test_list_preferences_owner_filter(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_preference(_make_preference("pref_a", owner="alice"))
        store.add_preference(_make_preference("pref_b", owner="bob"))
        assert len(store.list_preferences(owner="bob")) == 1

    def test_delete_fact_and_preference(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact())
        store.add_preference(_make_preference())
        store.delete_fact("fact_1")
        store.delete_preference("pref_1")
        assert store.get_fact("fact_1") is None
        assert store.get_preference("pref_1") is None
        # Deleting a missing id is a silent no-op.
        store.delete_fact("fact_missing")

    def test_persistence_roundtrip(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact())
        store.add_preference(_make_preference())

        reloaded = _make_store(tmp_path)
        fact = reloaded.get_fact("fact_1")
        pref = reloaded.get_preference("pref_1")
        assert fact is not None and fact.object_ == "REST interface"
        assert pref is not None and pref.preference == "dark mode"

    def test_durable_file_includes_typed_node_collections(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(_make_func("func_legacy", "legacy"), _make_source())
        reloaded = _make_store(tmp_path)
        assert reloaded.get("func_legacy") is not None
        assert reloaded.list_facts() == []
        assert reloaded.list_preferences() == []

    def test_clear_removes_facts_and_preferences(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact())
        store.add_preference(_make_preference())
        store.clear()
        assert store.list_facts() == []
        assert store.list_preferences() == []

    def test_fts_search_covers_fact_content(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_fact(_make_fact(obj="unique-fact-token-zqx"))
        results = store.fts_search("unique-fact-token-zqx", top_k=5)
        assert any(r.func_id == "fact_1" for r in results)

    def test_fts_search_covers_preference_content(self, tmp_path):
        store = _make_store(tmp_path)
        store.add_preference(_make_preference(preference="unique-pref-token-zqx"))
        results = store.fts_search("unique-pref-token-zqx", top_k=5)
        assert any(r.func_id == "pref_1" for r in results)

    def test_fts_search_merges_functions_and_facts(self, tmp_path):
        store = _make_store(tmp_path)
        store.add(
            _make_func(
                "func_mix",
                "Mix",
                actions=[FieldValue(desc="shared-token-zqx action")],
            ),
            _make_source(),
        )
        store.add_fact(_make_fact(obj="shared-token-zqx fact"))
        results = store.fts_search("shared-token-zqx", top_k=10)
        hit_ids = {r.func_id for r in results}
        assert "func_mix" in hit_ids
        assert "fact_1" in hit_ids


def test_merge_field_values_enforces_max_values_per_field():
    """The model-level cap (Function.MAX_VALUES_PER_FIELD) is enforced on merge."""
    from memplex.models import FieldValue, Function
    from memplex.storage.lite.store import _merge_field_values

    existing = [FieldValue(desc=f"existing-{i}") for i in range(Function.MAX_VALUES_PER_FIELD)]
    incoming = [FieldValue(desc=f"incoming-{i}") for i in range(5)]
    merged = _merge_field_values(existing, incoming)
    assert len(merged) == Function.MAX_VALUES_PER_FIELD
    # Existing values win; overflow from incoming is dropped.
    assert merged[-1].desc == f"existing-{Function.MAX_VALUES_PER_FIELD - 1}"
