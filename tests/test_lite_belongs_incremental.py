"""Contract tests for the incremental BELONGS_TO resident validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from memplex.models.memory import Function
from memplex.models.source import SourceDocument, SourceType
from memplex.storage.lite.store import LiteMemoryStore


def _function(name: str, domain: str | None) -> Function:
    return Function(
        id=f"func-{name}",
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        domain=domain,
        memory_type="function",
        source_type=SourceType.WIKI,
    )


def _source(text: str = "content") -> SourceDocument:
    return SourceDocument(type="test", content=text, source_type=SourceType.WIKI)


def _new_store(tmp_path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(tmp_path / "memory.json")


def test_new_belongs_edge_with_wrong_target_rejected_incrementally(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    with store.deferred_commit():
        store.add(_function("auth", "security"), _source())
    # A merge carrying a BELONGS_TO edge whose target is not the domain
    # node must be rejected at the merged-view preflight.
    from memplex.models.graph import GraphData, GraphEdge

    bad_edge = GraphEdge(
        source="func-auth",
        target="wrong-node-id",
        edge_type="BELONGS_TO",
        weight=1.0,
        evidence=[],
    )
    graph = GraphData(nodes=[], edges=[bad_edge])
    with pytest.raises(ValueError, match="BELONGS_TO"):
        store.merge(graph)


def test_domain_change_requeues_outgoing_belongs_edges(tmp_path: Path) -> None:
    from memplex.models.graph import GraphData, GraphEdge, domain_node_id

    store = _new_store(tmp_path)
    with store.deferred_commit():
        store.add(_function("auth", "security"), _source())
    belongs = GraphEdge(
        source="func-auth",
        target=domain_node_id("security"),
        edge_type="BELONGS_TO",
        weight=1.0,
        evidence=[],
    )
    store.merge(GraphData(nodes=[], edges=[belongs]))
    assert store._validate_resident_graph(full=True) is None

    # replace_function() with a different domain invalidates the stale
    # out-edge; the incremental contract catches it inside the replace's
    # own commit (earlier than the old whole-graph-per-write audit, same
    # failure mode).
    moved = _function("auth", "networking")
    with pytest.raises(ValueError, match="BELONGS_TO"):
        store.replace_function(moved)


def test_delete_removes_function_edges_and_index_entries(tmp_path: Path) -> None:
    from memplex.models.graph import GraphData, GraphEdge, domain_node_id

    store = _new_store(tmp_path)
    with store.deferred_commit():
        store.add(_function("auth", "security"), _source())
    belongs = GraphEdge(
        source="func-auth",
        target=domain_node_id("security"),
        edge_type="BELONGS_TO",
        weight=1.0,
        evidence=[],
    )
    store.merge(GraphData(nodes=[], edges=[belongs]))
    store._validate_resident_graph()  # drain the queue
    store.delete("func-auth")
    # delete() drops the function together with its incident edges, so
    # neither validation leaves dangling references and the index forgets it.
    store._validate_resident_graph()
    store._validate_resident_graph(full=True)
    assert "func-auth" not in store._domain_by_id
    assert all(edge.source != "func-auth" for edge in store._edges)


def test_periodic_commit_audit_runs_full_graph_contract(tmp_path: Path) -> None:
    from memplex.storage.lite.store import _FULL_DECODE_AUDIT_INTERVAL

    store = _new_store(tmp_path)
    full_calls = 0
    original = store._validate_resident_graph

    def counting(full: bool = False) -> None:  # type: ignore[arg-type]
        nonlocal full_calls
        if full:
            full_calls += 1
        return original(full=full)  # type: ignore[arg-type]

    store._validate_resident_graph = counting  # type: ignore[method-assign]
    for index in range(_FULL_DECODE_AUDIT_INTERVAL + 2):
        store.add(_function(f"fn-{index}", "general"), _source())
    assert full_calls >= 1
