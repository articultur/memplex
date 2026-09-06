"""Contract tests for LiteMemoryStore.deferred_commit batching."""

from __future__ import annotations

from pathlib import Path

import pytest

from memplex.models.memory import Function
from memplex.models.source import SourceDocument, SourceType
from memplex.storage.lite.store import LiteMemoryStore


def _function(name: str) -> Function:
    return Function(
        id=f"func-{name}",
        name=name,
        name_normalized=name.lower().replace(" ", "_"),
        domain=None,
        memory_type="function",
        source_type=SourceType.WIKI,
    )


def _source(text: str = "content") -> SourceDocument:
    return SourceDocument(type="test", content=text, source_type=SourceType.WIKI)


def _new_store(tmp_path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(tmp_path / "memory.json")


def test_deferred_commit_persists_batch_and_is_readable_after_reload(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    with store.deferred_commit():
        for index in range(50):
            store.add(_function(f"fn-{index}"), _source(f"text {index}"))
    assert len(store.list_functions()) == 50

    reloaded = LiteMemoryStore(tmp_path / "memory.json")
    assert len(reloaded.list_functions()) == 50


def test_deferred_commit_batches_into_single_generation_bump(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    before = store._committed_pair.generation if store._committed_pair else 0
    with store.deferred_commit():
        for index in range(20):
            store.add(_function(f"fn-{index}"), _source())
    after = store._committed_pair.generation
    assert after - before == 1


def test_deferred_commit_keeps_prefix_when_one_mutation_fails(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    with store.deferred_commit():
        store.add(_function("good-1"), _source())
        store.add(_function("good-2"), _source())
        bad = _function("bad")
        bad.provenance = {1: "actor"}
        with pytest.raises(Exception, match="Lite"):
            store.add(bad, _source())
        store.add(_function("good-3"), _source())
    names = {f.name for f in store.list_functions()}
    assert {"good-1", "good-2", "good-3"} <= names


def test_deferred_commit_supports_nested_scopes(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    with store.deferred_commit():
        store.add(_function("outer"), _source())
        with store.deferred_commit():
            store.add(_function("inner"), _source())
        # Still inside the outermost scope: nothing durable yet.
        assert store._commit_defer_depth == 1
    assert store._commit_defer_depth == 0
    assert len(LiteMemoryStore(tmp_path / "memory.json").list_functions()) == 2


def test_deferred_commit_commits_on_exception_path(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    with pytest.raises(RuntimeError, match="boom"), store.deferred_commit():
        store.add(_function("kept"), _source())
        raise RuntimeError("boom")
    assert store._commit_defer_depth == 0
    reloaded = LiteMemoryStore(tmp_path / "memory.json")
    assert any(f.name == "kept" for f in reloaded.list_functions())


def test_full_decode_audit_still_runs_periodically_outside_batching(tmp_path: Path) -> None:
    store = _new_store(tmp_path)
    calls = []
    original = store._decode_pair

    def counting_decode(pair):
        calls.append(pair.transaction_id)
        return original(pair)

    store._decode_pair = counting_decode  # type: ignore[method-assign]
    for index in range(40):
        store.add(_function(f"fn-{index}"), _source())
    # Unbatched commits: the double decode audit fires on the first commit
    # and every _FULL_DECODE_AUDIT_INTERVAL-th one after that.
    from memplex.storage.lite.store import _FULL_DECODE_AUDIT_INTERVAL

    assert len(calls) >= 2 * (40 // _FULL_DECODE_AUDIT_INTERVAL + 1) - 2
    assert len(calls) <= 2 * 40
