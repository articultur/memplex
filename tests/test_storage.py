"""Test storage layer: LiteMemoryStore add/get/merge/delete/list_functions,
ChangelogStore append/get_timeline, vector_search / fts_search."""

import os
import sqlite3

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime
from pathlib import Path

from memplex.models import (
    BatchResult,
    ChangelogEvent,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MergeResult,
    Observation,
    SearchFilters,
    SourceDocument,
    SourceType,
)
from memplex.storage.changelog import ChangelogStore
from memplex.storage.lite.store import LiteMemoryStore

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

    def test_add_creates_changelog(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func("func_2", "Register")
        store.add(func, _make_source())
        timeline = store.get_timeline("func_2")
        assert len(timeline) >= 1
        assert timeline[0].event_type == "created"

    def test_add_merges_duplicate_name(self, tmp_path):
        store = _make_store(tmp_path)

        func_a = _make_func(
            "func_a", "Login", triggers=[FieldValue(desc="click button")]
        )
        func_b = _make_func(
            "func_b", "Login", triggers=[FieldValue(desc="enter credentials")]
        )

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
            edges=[
                GraphEdge(source="func_ea", target="func_eb", edge_type="REFERENCES")
            ],
        )
        result = store.merge(graph)
        assert result.new_edges == 1

    def test_merge_duplicate_edges_skipped(self, tmp_path):
        store = _make_store(tmp_path)
        func_a = _make_func("func_dup_a", "A")
        func_b = _make_func("func_dup_b", "B")
        store.add(func_a, _make_source())
        store.add(func_b, _make_source())

        edge = GraphEdge(
            source="func_dup_a", target="func_dup_b", edge_type="REFERENCES"
        )
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
            actions=[
                FieldValue(
                    desc="use local bm25 retrieval when huggingface is unavailable"
                )
            ],
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
            actions=[
                FieldValue(desc="中国大陆 HuggingFace 不可用时使用本地离线检索")
            ],
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


# ── LiteMemoryStore: Observations ────────────────────────────────────


class TestLiteMemoryStoreObservation:
    def test_add_observation(self, tmp_path):
        store = _make_store(tmp_path)
        obs = Observation(id="obs_1", name="Event", event="something happened")
        store.add_observation(obs)
        # Observations are stored internally
        assert len(store._observations) == 1


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
    def test_save_and_reload(self, tmp_path):
        store = _make_store(tmp_path)
        func = _make_func(
            "func_persist", "PersistTest", triggers=[FieldValue(desc="trigger text")]
        )
        store.add(func, _make_source())

        # Reload from disk
        store2 = _make_store(tmp_path)
        loaded = store2.get("func_persist")
        assert loaded is not None
        assert loaded.name == "PersistTest"
        assert len(loaded.trigger) == 1
        assert loaded.trigger[0].desc == "trigger text"


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
