"""Per-build native Function acquisition, freshness, and invocation isolation.

Event-gated concurrency runs in the existing bounded child harness. Counts
observe the real listing, and edge assertions prevent an empty-result shortcut.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
from copy import deepcopy
from dataclasses import asdict

import pytest

from memplex.models import FieldValue, GraphData, SourceDocument
from memplex.processing.graph_builder import GraphBuilder
from memplex.storage.lite.store import LiteMemoryStore
from tests.test_graph_builder import _make_config, _make_func, _VocabEmbedding
from tests.test_typed_write_batch_concurrency import (
    _join_threads,
    _observe_contender_boundary,
    _run_bounded,
    _stop_process,
    _thread_target,
)
from tests.test_typed_write_batch_recovery import _close, _configure

_WAIT = 10
_SOURCE = SourceDocument(type="test")


def _node(identifier, name=None, *, word="alpha", domain="snapshot", refs=()):
    node = _make_func(
        identifier, name or identifier, domain=domain,
        triggers=[FieldValue(desc=word, sources=["original"])],
        actions=[FieldValue(desc=word)], cross_refs=list(refs),
    )
    node.name_normalized = identifier
    return node


def _targets(edges, kind, source=None):
    return [edge.target for edge in edges if edge.edge_type == kind and (source is None or edge.source == source)]


def _observe_listing(store, monkeypatch):
    calls = []
    original = store.list_functions

    def listing(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "list_functions", listing)
    return calls


@pytest.fixture(params=["1", "0"], ids=["queue-on", "queue-off"])
def native_store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", request.param)
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY", raising=False)
    store = LiteMemoryStore(tmp_path / "memory.json")
    yield store
    _close(store)


def test_one_native_listing_per_public_build(native_store, monkeypatch):
    native_store.add(_node("stored", "Native"), _SOURCE)
    builder = GraphBuilder(native_store)
    calls = _observe_listing(native_store, monkeypatch)
    first = _node("first", refs=[{"target": "Native"}, {"target": "Native"}])
    second = _node("second", refs=[{"target": "Native"}])
    first.action = second.action = [FieldValue(desc="Native")]
    assert builder.build_from_batch([]) == []
    assert calls == []
    edges = builder.build_from_batch([first, second])
    assert _targets(edges, "REFERENCES") == ["stored", "stored"]
    assert _targets(edges, "DEPENDS_ON") == ["stored", "stored"]
    assert _targets(edges, "CONFLICTS_WITH", "second") == ["stored", "first"]
    assert calls == [((), {"limit": 100000})]
    assert _targets(builder.process(first), "REFERENCES") == ["stored"]
    assert calls == [((), {"limit": 100000})] * 2
    assert builder.build_from_batch([]) == []
    assert len(calls) == 2


def test_warmed_builder_sees_peer_add_and_same_id_replacement(native_store):
    native_store.add(_node("shared", "Old"), _SOURCE)
    peer = LiteMemoryStore(native_store._path)
    builder = GraphBuilder(native_store)
    source = _node("query")
    source.action = [FieldValue(desc="Old New Added")]
    try:
        for _ in range(2):
            assert _targets(builder.process(source), "DEPENDS_ON") == ["shared"]
        peer.replace_function(_node("shared", "New", word="gamma"))
        peer.add(_node("added", "Added", word="gamma"), _SOURCE)
        edges = builder.process(source)
        assert _targets(edges, "DEPENDS_ON") == ["added", "shared"]
        assert _targets(edges, "CONFLICTS_WITH") == []
        assert _targets(edges, "ASSOCIATED_WITH") == ["added", "shared"]
        assert [edge.evidence for edge in edges if edge.edge_type == "DEPENDS_ON"] == [
            ["query references Added"], ["query references New"],
        ]
    finally:
        _close(peer)


def test_same_id_embedding_is_fresh_after_peer_body_update(native_store):
    native_store.add(_node("stored", "Target", domain=None), _SOURCE)
    peer = LiteMemoryStore(native_store._path)
    builder = GraphBuilder(native_store, _make_config(), _VocabEmbedding())
    source = _node("query", domain=None)
    try:
        edges = builder.process(source)
        assert _targets(edges, "SEMANTIC_SIMILAR") == ["stored"]
        assert next(edge.weight for edge in edges if edge.edge_type == "SEMANTIC_SIMILAR") == pytest.approx(1)
        peer.replace_function(_node("stored", "Target", word="gamma", domain=None))
        assert _targets(builder.process(source), "SEMANTIC_SIMILAR") == []
        peer.update_function_role("stored", "action", "alpha alpha alpha alpha")
        restored = builder.process(source)
        assert _targets(restored, "SEMANTIC_SIMILAR") == ["stored"]
        assert next(edge.weight for edge in restored if edge.edge_type == "SEMANTIC_SIMILAR") == pytest.approx(4 / 20**0.5)
    finally:
        _close(peer)


@pytest.mark.parametrize("failure", [PermissionError, RuntimeError])
def test_failed_acquisition_has_no_stale_fallback_or_midbuild_retry(native_store, monkeypatch, failure):
    native_store.add(_node("stored", "Native"), _SOURCE)
    builder = GraphBuilder(native_store, _make_config(), _VocabEmbedding())
    source = _node("query", refs=[{"target": "Native"}, {"target_id": "explicit"}])
    source.action = [FieldValue(desc="Native alpha")]
    for _ in range(2):
        assert "stored" in _targets(builder.process(source), "REFERENCES")
    original = native_store.list_functions
    attempts = []

    def denied(*args, **kwargs):
        attempts.append((args, kwargs))
        raise failure("listing denied")

    monkeypatch.setattr(native_store, "list_functions", denied)
    edges = builder.build_from_batch([source, _node("batch-peer")])
    assert all(edge.target != "stored" for edge in edges)
    assert _targets(edges, "REFERENCES") == ["explicit"]
    assert _targets(edges, "CONFLICTS_WITH") == ["query"]
    assert attempts == [((), {"limit": 100000})]
    monkeypatch.setattr(native_store, "list_functions", original)
    native_store.add(_node("new", "Fresh"), _SOURCE)
    assert _targets(builder.process(source), "ASSOCIATED_WITH") == ["new", "stored"]


def test_processing_failure_and_baseexception_do_not_poison_next_build(native_store, monkeypatch):
    native_store.add(_node("stored", "Native"), _SOURCE)
    builder = GraphBuilder(native_store)
    calls = _observe_listing(native_store, monkeypatch)
    original = GraphBuilder._name_reference_texts

    def fail(self, source):
        raise ValueError("processing failure")

    monkeypatch.setattr(GraphBuilder, "_name_reference_texts", fail)
    with pytest.raises(ValueError, match="processing failure"):
        builder.process(_node("query"))
    monkeypatch.setattr(GraphBuilder, "_name_reference_texts", original)
    assert _targets(builder.process(_node("query")), "ASSOCIATED_WITH") == ["stored"]
    assert len(calls) == 2
    real_list = native_store.list_functions

    def interrupted(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(native_store, "list_functions", interrupted)
    with pytest.raises(KeyboardInterrupt):
        builder.process(_node("query"))
    monkeypatch.setattr(native_store, "list_functions", real_list)
    assert _targets(builder.process(_node("query")), "ASSOCIATED_WITH") == ["stored"]
    invalid = _node("invalid")
    invalid.domain = 0
    with pytest.raises(ValueError, match="domain"):
        builder.process(invalid)


def test_native_duplicate_order_full_edge_parity_and_caller_immutability(native_store):
    first = _node("z-first", "alpha Target")
    second = _node("a-second", "alpha Target")
    native_store.add(first, _SOURCE)
    native_store.add(second, _SOURCE)
    source = _node("query", "alpha Source", refs=[
        {"target": "alpha Target"}, {"target": "alpha Target"},
        {"target": "ALPHA TARGET"}, {"target": ""}, {"target_id": "explicit"},
    ])
    source.action = [FieldValue(desc="alpha Target", sources=["caller"])]
    graph = GraphData(nodes=[_node("local")], edges=[])
    inputs_before = deepcopy((source.to_dict(), asdict(graph)))
    stored_before = [node.to_dict() for node in native_store.list_functions()]
    cfg = _make_config(max_edges=1)
    cfg.graph.depends_on_max_edges = cfg.graph.associated_with_max_edges = 1
    edges = GraphBuilder(native_store, cfg, _VocabEmbedding()).process(source, graph)
    assert [(edge.edge_type, edge.target, edge.weight, edge.evidence) for edge in edges] == [
        ("REFERENCES", "z-first", 1, ["cross_reference: alpha Source -> alpha Target"]),
        ("REFERENCES", "explicit", 1, ["cross_reference from alpha Source"]),
        ("DEPENDS_ON", "z-first", 1, ["alpha Source references alpha Target"]),
        ("CONFLICTS_WITH", "z-first", 1, ["conflicting definitions in domain snapshot"]),
        ("CONFLICTS_WITH", "a-second", 1, ["conflicting definitions in domain snapshot"]),
        ("CONFLICTS_WITH", "local", 1, ["conflicting definitions in domain snapshot"]),
        ("BELONGS_TO", "domain_snapshot", 1, ["alpha Source belongs to snapshot"]),
        ("ASSOCIATED_WITH", "a-second", 0.5, ["shared domain: snapshot"]),
        ("SEMANTIC_SIMILAR", "z-first", 1, ["embedding cosine similarity 1.000"]),
    ]
    assert (source.to_dict(), asdict(graph)) == inputs_before
    assert [node.to_dict() for node in native_store.list_functions()] == stored_before


def test_native_active_deferred_prefix_remains_visible(native_store):
    builder = GraphBuilder(native_store)
    with native_store.deferred_commit():
        native_store.add(_node("pending", "Pending"), _SOURCE)
        assert native_store.read_context_nodes(["pending"]) == {}
        edges = builder.process(_node("query", refs=[{"target": "Pending"}]))
        assert _targets(edges, "REFERENCES") == ["pending"]


def test_native_limit_default_offset_and_first_name_match():
    inert = _node("inert", domain=None)
    inert.name = ""
    visible = _node("visible", "Boundary", domain=None)
    excluded = _node("excluded", "Beyond", domain=None)

    class OrderedPage:
        def __init__(self):
            self.calls = []

        def list_functions(self, offset=0, limit=1000, owner=None):
            self.calls.append((offset, limit, owner))
            return ([inert] * 99999 + [visible, excluded])[offset:offset + limit]

    store = OrderedPage()
    source = _node("query", domain=None, refs=[{"target": "Boundary"}, {"target": "Beyond"}])
    edges = GraphBuilder(store).process(source)
    assert _targets(edges, "REFERENCES") == ["visible"]
    assert store.calls == [(0, 100000, None)]


def _peer_mutate(store):
    store.replace_function(_node("shared", "New", word="gamma"))
    store.add(_node("added", "Added", word="gamma"), _SOURCE)


def _process_peer(path, mode, authority, ready, start, finished):
    _configure(mode)
    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = authority
    peer = LiteMemoryStore(path)
    try:
        ready.set()
        assert start.wait(_WAIT), "Peer start missing"
        _peer_mutate(peer)
        finished.set()
    finally:
        _close(peer)


def _snapshot_peer_scenario(root, mode, authority, peer_kind, phase):
    _configure(mode)
    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = authority
    reader = LiteMemoryStore(root / "memory.json")
    reader.add(_node("shared", "Old"), _SOURCE)
    builder = GraphBuilder(reader, _make_config(), _VocabEmbedding())
    first = _node("first", refs=[{"target": "Old"}])
    second = _node("second", refs=[{"target": "New"}, {"target": "Added"}])
    for node in (first, second):
        if phase == "before":
            node.cross_references = []
        node.action = [FieldValue(desc="Old New Added alpha")]
    # No name lookup may accidentally refresh the warmed native fingerprint.
    for _ in range(2):
        assert _targets(builder.process(_node("warm")), "ASSOCIATED_WITH") == ["shared"]
    context = multiprocessing.get_context("spawn")
    ready, start, finished = (context.Event() for _ in range(3))
    errors, results, calls = [], {}, []
    peer_store = None
    if peer_kind == "process":
        peer = context.Process(target=_process_peer, args=(reader._path, mode, authority, ready, start, finished))
    else:
        peer_store = LiteMemoryStore(reader._path)

        def mutate():
            ready.set()
            assert start.wait(_WAIT), "Peer start missing"
            _peer_mutate(peer_store)

        peer = threading.Thread(target=_thread_target, args=(mutate, results, errors, "peer", finished), daemon=True)
    acquired, release, built = threading.Event(), threading.Event(), threading.Event()
    original = reader.list_functions

    def listing(*args, **kwargs):
        rows = original(*args, **kwargs)
        calls.append((args, kwargs))
        if phase == "after" and len(calls) == 1:
            acquired.set()  # The real native listing has returned and released its lock.
            assert release.wait(_WAIT), "Graph acquisition pause never released"
        return rows

    reader.list_functions = listing
    graph_worker = threading.Thread(
        target=_thread_target,
        args=(lambda: builder.build_from_batch([first, second]), results, errors, "graph", built), daemon=True,
    )
    try:
        peer.start()
        assert ready.wait(_WAIT), "Peer never opened"
        if phase == "after":
            graph_worker.start()
            assert acquired.wait(_WAIT), "Graph never acquired the native snapshot"
        start.set()
        assert finished.wait(_WAIT), "Peer could not commit while graph processing was paused"
        if phase == "after":
            assert not built.is_set()
        release.set()
        if phase == "before":
            graph_worker.start()
        _join_threads([graph_worker], errors)
        peer.join(_WAIT)
        assert not peer.is_alive()
        if peer_kind == "process":
            assert peer.exitcode == 0
        assert not errors
        edges = results["graph"]
        if phase == "after":
            assert _targets(edges, "REFERENCES", "first") == ["shared"]
            assert _targets(edges, "REFERENCES", "second") == []
            assert _targets(edges, "SEMANTIC_SIMILAR", "first") == ["shared"]
            assert _targets(edges, "CONFLICTS_WITH", "second") == ["shared", "first"]
        else:
            assert _targets(edges, "DEPENDS_ON", "first") == ["added", "shared"]
            assert _targets(edges, "ASSOCIATED_WITH", "second") == ["added", "shared"]
            assert _targets(edges, "SEMANTIC_SIMILAR") == []
        assert calls == [((), {"limit": 100000})]
        fresh = builder.build_from_batch([first, second])
        assert _targets(fresh, "REFERENCES", "first") == []
        if phase == "after":
            assert _targets(fresh, "REFERENCES", "second") == ["shared", "added"]
        assert _targets(fresh, "DEPENDS_ON", "second") == ["added", "shared"]
        assert _targets(fresh, "SEMANTIC_SIMILAR") == []
        assert _targets(fresh, "CONFLICTS_WITH", "second") == ["first"]
        assert calls == [((), {"limit": 100000})] * 2
    finally:
        start.set()
        release.set()
        if graph_worker.ident is not None:
            graph_worker.join(_WAIT)
        if peer_kind == "process":
            _stop_process(peer)
            peer.close()
        else:
            peer.join(_WAIT)
            _close(peer_store)
        _close(reader)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("authority", ["", "rw"], ids=["json", "sqlite-rw"])
@pytest.mark.parametrize("peer_kind", ["thread", "process"])
@pytest.mark.parametrize("phase", ["before", "after"], ids=["completed-before-entry", "committed-after-acquisition"])
def test_native_peer_commit_boundary(tmp_path, mode, authority, peer_kind, phase):
    _run_bounded(tmp_path, _snapshot_peer_scenario, mode, authority, peer_kind, phase)


def _acquisition_wait_scenario(root, mode, authority):
    _configure(mode)
    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = authority
    reader = LiteMemoryStore(root / "memory.json")
    reader.add(_node("shared", "Old"), _SOURCE)
    reader.list_functions()  # Initialize the actual queue before observing admission.
    peer = LiteMemoryStore(reader._path)
    staged, release, attempted, done = (threading.Event() for _ in range(4))
    results, errors = {}, []

    def pause():
        staged.set()
        assert release.wait(_WAIT), "Native publication pause never released"

    if authority == "rw":
        original_commit = peer._sqlite_authoritative_commit

        def pause_sqlite(base, target):
            pause()
            return original_commit(base, target)

        peer._sqlite_authoritative_commit = pause_sqlite
    else:
        peer._durability.before_journal_durable_publish = pause
    writer = threading.Thread(
        target=_thread_target,
        args=(lambda: peer.replace_function(_node("shared", "New")), results, errors, "writer", threading.Event()),
        daemon=True,
    )
    _observe_contender_boundary(reader, mode, "read", attempted)
    contender = threading.Thread(
        target=_thread_target,
        args=(lambda: GraphBuilder(reader).process(_node("query", refs=[{"target": "New"}])), results, errors, "graph", done),
        name="contender", daemon=True,
    )
    try:
        writer.start()
        assert staged.wait(_WAIT)
        contender.start()
        assert attempted.wait(_WAIT)
        assert not done.is_set(), "Graph crossed an in-flight native writer lock"
        release.set()
        _join_threads([writer, contender], errors)
        assert _targets(results["graph"], "REFERENCES") == ["shared"]
    finally:
        release.set()
        for worker in (writer, contender):
            if worker.ident is not None:
                worker.join(_WAIT)
        _close(peer)
        _close(reader)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("authority", ["", "rw"], ids=["json", "sqlite-rw"])
def test_refresh_waits_for_real_native_acquisition(tmp_path, mode, authority):
    _run_bounded(tmp_path, _acquisition_wait_scenario, mode, authority)


def _isolation_scenario(root, mode, reentrant):
    _configure(mode)
    store = LiteMemoryStore(root / "memory.json")
    store.add(_node("shared", "Old"), _SOURCE)
    peer = LiteMemoryStore(store._path)
    builder = GraphBuilder(store, _make_config(), _VocabEmbedding())
    refs = [{"target": "Old"}, {"target": "New"}]
    outer = [_node("outer-one", refs=refs), _node("outer-two", refs=refs)]
    inner = [_node("inner-one", refs=refs), _node("inner-two", refs=refs)]
    for node in outer:
        node.action = [FieldValue(desc="Old alpha")]
    for node in inner:
        node.action = [FieldValue(desc="New alpha")]
    original = GraphBuilder._name_reference_texts
    paused, release = threading.Event(), threading.Event()
    results, errors = {}, []

    def intercept(self, source):
        if source.id == "outer-two":
            if reentrant:
                peer.replace_function(_node("shared", "New", word="gamma"))
                results["inner"] = builder.build_from_batch(inner)
            else:
                paused.set()
                assert release.wait(_WAIT)
        return original(self, source)

    GraphBuilder._name_reference_texts = intercept
    worker = threading.Thread(
        target=_thread_target,
        args=(lambda: builder.build_from_batch(outer), results, errors, "outer", threading.Event()), daemon=True,
    )
    try:
        if reentrant:
            results["outer"] = builder.build_from_batch(outer)
        else:
            worker.start()
            assert paused.wait(_WAIT)
            peer.replace_function(_node("shared", "New", word="gamma"))
            results["inner"] = builder.build_from_batch(inner)
            release.set()
            _join_threads([worker], errors)
        assert _targets(results["outer"], "CONFLICTS_WITH") == ["shared", "shared", "outer-one"]
        assert _targets(results["inner"], "CONFLICTS_WITH") == ["inner-one"]
        assert [(edge.target, edge.evidence) for edge in results["outer"] if edge.edge_type == "DEPENDS_ON"] == [
            ("shared", ["outer-one references Old"]), ("shared", ["outer-two references Old"]),
        ]
        assert [(edge.target, edge.evidence) for edge in results["inner"] if edge.edge_type == "DEPENDS_ON"] == [
            ("shared", ["inner-one references New"]), ("shared", ["inner-two references New"]),
        ]
        outer_semantic = [edge for edge in results["outer"] if edge.edge_type == "SEMANTIC_SIMILAR"]
        assert [edge.target for edge in outer_semantic] == ["shared", "shared"]
        assert [edge.weight for edge in outer_semantic] == pytest.approx([1, 1])
        assert _targets(results["inner"], "SEMANTIC_SIMILAR") == []
        assert [edge.evidence for edge in results["outer"] if edge.edge_type == "REFERENCES"] == [
            ["cross_reference: outer-one -> Old"], ["cross_reference: outer-two -> Old"],
        ]
        assert [edge.evidence for edge in results["inner"] if edge.edge_type == "REFERENCES"] == [
            ["cross_reference: inner-one -> New"], ["cross_reference: inner-two -> New"],
        ]
    finally:
        release.set()
        if worker.ident is not None:
            worker.join(_WAIT)
        GraphBuilder._name_reference_texts = original
        _close(peer)
        _close(store)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("reentrant", [False, True], ids=["overlap", "reentrant"])
def test_public_builder_keeps_batch_state_private(tmp_path, mode, reentrant):
    _run_bounded(tmp_path, _isolation_scenario, mode, reentrant)


def test_independent_builders_and_invalidation_do_not_share_corpus(native_store, tmp_path):
    native_store.add(_node("one", "Target"), _SOURCE)
    other = LiteMemoryStore(tmp_path / "other.json")
    other.add(_node("two", "Target"), _SOURCE)
    first, second = GraphBuilder(native_store), GraphBuilder(other)
    source = _node("query", refs=[{"target": "Target"}])
    try:
        assert _targets(first.process(source), "REFERENCES") == ["one"]
        assert _targets(second.process(source), "REFERENCES") == ["two"]
        first.invalidate_cache()
        assert _targets(first.process(source), "REFERENCES") == ["one"]
        assert _targets(second.process(source), "REFERENCES") == ["two"]
    finally:
        _close(other)


def test_constructor_selected_config_and_injected_formatter_remain_in_use(native_store):
    native_store.add(_node("first", domain=None), _SOURCE)
    native_store.add(_node("second", domain=None), _SOURCE)

    class Formatter(_VocabEmbedding):
        def __init__(self):
            self.formatted = []

        def function_to_text(self, function):
            self.formatted.append(function.id)
            return "beta"

    config = _make_config(max_edges=1)
    formatter = Formatter()
    builder = GraphBuilder(native_store, config, formatter)
    # The public constructor selected the original graph configuration object.
    config.graph = _make_config(max_edges=2).graph
    edges = builder.process(_node("query", word="gamma", domain=None))
    assert _targets(edges, "SEMANTIC_SIMILAR") == ["first"]
    assert formatter.formatted == ["query", "first", "second"]


@pytest.mark.parametrize("target", [["Native"], {"name": "Native"}], ids=["list-target", "dict-target"])
def test_unhashable_name_target_remains_unmatched_without_losing_other_edges(native_store, target):
    native_store.add(_node("stored", "Native"), _SOURCE)
    source = _node("query", refs=[
        {"target": target}, {"target": "Native"}, {"target_id": "explicit"},
    ])
    edges = GraphBuilder(native_store).process(source)
    assert [(edge.edge_type, edge.target) for edge in edges] == [
        ("REFERENCES", "stored"), ("REFERENCES", "explicit"),
        ("CONFLICTS_WITH", "stored"), ("BELONGS_TO", "domain_snapshot"),
        ("ASSOCIATED_WITH", "stored"),
    ]
