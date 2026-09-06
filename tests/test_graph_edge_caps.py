"""Contract tests for per-node heuristic edge caps."""

from __future__ import annotations

from pathlib import Path

from memplex.config import GraphConfig, MemplexConfig
from memplex.models.memory import Function
from memplex.models.source import SourceDocument, SourceType
from memplex.processing.graph_builder import GraphBuilder
from memplex.storage.lite.store import LiteMemoryStore


def _function(name: str, domain: str | None, text: str) -> Function:
    from memplex.models.memory import FieldValue

    return Function(
        id=f"func-{name}",
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        domain=domain,
        memory_type="function",
        source_type=SourceType.WIKI,
        trigger=[FieldValue(desc=text)],
        action=[FieldValue(desc=text)],
    )


def _new_store(tmp_path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(tmp_path / "memory.json")


def test_associated_with_edges_are_capped_in_id_order(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    config = MemplexConfig()
    config.graph.associated_with_max_edges = 5
    builder = GraphBuilder(store=store, config=config)
    for index in range(30):
        store.add(
            _function(f"fn-{index:03d}", "security", f"security topic {index}"),
            SourceDocument(type="t", content=f"body {index}", source_type=SourceType.WIKI),
        )
    builder.invalidate_cache()
    edges = builder.build_from_batch(
        [_function("fn-new", "security", "a fresh security function")]
    )
    associated = [e for e in edges if e.edge_type == "ASSOCIATED_WITH"]
    # Cap applies per function: at most 5 same-domain neighbours, chosen in
    # deterministic id order.
    assert len(associated) <= 5
    targets = sorted(e.target for e in associated)
    assert targets == [f"func-fn-{i:03d}" for i in range(len(targets))]


def test_associated_with_cap_defaults_to_twenty(tmp_path: Path) -> None:
    assert GraphConfig().associated_with_max_edges == 20
    assert GraphConfig().depends_on_max_edges == 20


def test_depends_on_edges_are_capped_longest_name_first(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    # 30 corpus functions whose names all appear in the new function's text.
    for index in range(30):
        store.add(
            _function(f"mod-{index}", "security", f"unrelated body {index}"),
            SourceDocument(type="t", content=f"body {index}", source_type=SourceType.WIKI),
        )
    builder = GraphBuilder(store=store, config=MemplexConfig())
    corpus_names = [f.name for f in store.list_functions()]
    text = " ".join(f"uses {name} components" for name in corpus_names)
    edges = builder.build_from_batch(
        [_function("aggregator", "integration", text)]
    )
    depends = [e for e in edges if e.edge_type == "DEPENDS_ON"]
    assert len(depends) <= 20
