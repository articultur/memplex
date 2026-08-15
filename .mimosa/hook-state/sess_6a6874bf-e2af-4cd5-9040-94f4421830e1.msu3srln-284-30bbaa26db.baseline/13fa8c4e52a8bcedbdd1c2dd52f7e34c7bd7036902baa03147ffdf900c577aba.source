"""Test memplex/compaction.py: lock primitives and pipeline wiring.

The full CompactionPipeline.run is heavy integration (covered indirectly
via service.compact); these tests cover the FileLock primitive, lock-key
derivation, and lock-backend selection -- the pieces most likely to
silently regress. The unreachable PGAdvisoryLock/_pg_pool dead code was
removed in Wave 2a; removal is pinned by tests below.
"""

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


import memplex.compaction as compaction_module  # noqa: E402
from memplex.compaction import (  # noqa: E402
    CompactionPipeline,
    FileLock,
)
from memplex.config import MemplexConfig  # noqa: E402
from memplex.models import CompactionScope  # noqa: E402


def _run(coro):
    """Run a coroutine on a private event loop.

    Using ``asyncio.run`` here pollutes the global loop state on Python
    3.13 and breaks ``asyncio.get_event_loop()`` callers in sibling test
    modules (notably tests/test_llm.py). We manage a fresh loop explicitly
    without touching the global loop via ``set_event_loop``.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── FileLock ─────────────────────────────────────────────────────────


def test_file_lock_acquire_then_release(tmp_path):
    lock = FileLock(key="compaction:project", lock_dir=tmp_path)
    assert _run(lock.try_acquire()) is True
    assert lock._fd is not None
    _run(lock.release())
    assert lock._fd is None


def test_file_lock_second_instance_is_blocked(tmp_path):
    """A second process trying to acquire the same lock file must fail."""
    lock1 = FileLock(key="same-key", lock_dir=tmp_path)
    lock2 = FileLock(key="same-key", lock_dir=tmp_path)
    assert _run(lock1.try_acquire()) is True
    assert _run(lock2.try_acquire()) is False  # held by lock1
    _run(lock1.release())
    # After release, lock2 (or a fresh one) can acquire.
    lock3 = FileLock(key="same-key", lock_dir=tmp_path)
    assert _run(lock3.try_acquire()) is True
    _run(lock3.release())


def test_file_lock_creates_lock_dir_if_missing(tmp_path):
    missing = tmp_path / "nested" / "locks"
    assert not missing.exists()
    lock = FileLock(key="k", lock_dir=missing)
    assert _run(lock.try_acquire()) is True
    assert missing.exists()
    _run(lock.release())


def test_file_lock_release_is_idempotent_when_not_held(tmp_path):
    lock = FileLock(key="k", lock_dir=tmp_path)
    # release before acquire must not raise.
    _run(lock.release())


def test_file_lock_different_keys_are_independent(tmp_path):
    a = FileLock(key="key-a", lock_dir=tmp_path)
    b = FileLock(key="key-b", lock_dir=tmp_path)
    assert _run(a.try_acquire()) is True
    assert _run(b.try_acquire()) is True  # different lock file
    _run(a.release())
    _run(b.release())


# ── PGAdvisoryLock / _pg_pool removal (Wave 2a) ──────────────────────


def test_pg_advisory_lock_is_removed():
    """The unreachable Enterprise advisory lock was dead code: ``_pg_pool``
    was initialised to ``None`` with no injection point, so the PG branch
    could never run. It is gone; the module must not re-grow it."""
    import memplex.compaction as compaction_module

    assert not hasattr(compaction_module, "PGAdvisoryLock")
    assert not hasattr(CompactionPipeline, "_pg_pool")


# ── CompactionPipeline lock wiring ───────────────────────────────────


def test_lock_key_derivation():
    assert CompactionPipeline._lock_key(CompactionScope.PROJECT) == "compaction:project"
    assert CompactionPipeline._lock_key(CompactionScope.SESSION) == "compaction:session"


def test_pipeline_builds_file_lock_for_lite_backend():
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"

    # build a minimal stub store; _build_lock only reads config.backend
    class _StubStore:
        pass

    pipe = CompactionPipeline(store=_StubStore(), embedding_service=None, config=cfg)
    lock = pipe._build_lock(CompactionScope.PROJECT)
    assert isinstance(lock, FileLock)


def test_pipeline_builds_file_lock_for_enterprise_backend():
    """With PGAdvisoryLock removed, every backend uses the FileLock."""
    cfg = MemplexConfig()
    cfg.storage.backend = "enterprise"

    class _StubStore:
        pass

    pipe = CompactionPipeline(store=_StubStore(), embedding_service=None, config=cfg)
    lock = pipe._build_lock(CompactionScope.PROJECT)
    assert isinstance(lock, FileLock)


# ── Stage behaviour against a real lite store (Wave 1 fixes) ─────────

import json  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

from memplex.models import FieldValue, Function  # noqa: E402
from memplex.retrieval.embedding import EmbeddingService  # noqa: E402
from memplex.storage.lite.store import LiteMemoryStore  # noqa: E402


def _make_pipeline(tmp_path, **cfg_overrides):
    """Real LiteMemoryStore + offline TF-IDF embeddings + default config."""
    cfg = MemplexConfig()
    for key, val in cfg_overrides.items():
        setattr(cfg.compaction, key, val)
    store = LiteMemoryStore(path=tmp_path / "memory.json")
    embedding = EmbeddingService(model="tfidf")
    pipe = CompactionPipeline(store=store, embedding_service=embedding, config=cfg)
    return pipe, store


def _insert(store, func):
    """Insert a fixture through the controlled bulk mutation boundary."""
    store.apply_compaction(replacements=[func], delete_ids=[])


def _func(fid, name, updated_at=None, **kwargs):
    f = Function(id=fid, name=name, name_normalized=name.strip().lower(), **kwargs)
    if updated_at is not None:
        f.updated_at = updated_at
    return f


def test_dedup_stage_actually_deletes_exact_duplicates(tmp_path):
    """Dedup must remove duplicates from the store, not just count them."""
    pipe, store = _make_pipeline(tmp_path)
    older = "2024-01-01T00:00:00+00:00"
    newer = "2024-06-01T00:00:00+00:00"
    _insert(store, _func("dup-a", "same name", updated_at=older,
                         action=[FieldValue(desc="do the thing")]))
    _insert(store, _func("dup-b", "same name", updated_at=newer,
                         action=[FieldValue(desc="do the thing")]))

    result = _run(pipe._execute_dedup(CompactionScope.GLOBAL))

    assert result.removed == 1
    # The newer duplicate wins; the older one is really gone from the store.
    assert store.get("dup-a") is None
    assert store.get("dup-b") is not None
    # ... and from disk.
    reloaded = LiteMemoryStore(path=tmp_path / "memory.json")
    assert reloaded.get("dup-a") is None
    assert reloaded.get("dup-b") is not None


def test_dedup_stage_wires_chunk_threshold_from_config(tmp_path, monkeypatch):
    """compaction.chunk_threshold must reach MemoryDeduplicator."""
    captured = {}

    class _SpyDedup:
        def __init__(self, embedding, threshold=0.95, chunk_threshold=20000, use_faiss=True):
            captured["chunk_threshold"] = chunk_threshold

        def deduplicate(self, memories):
            from memplex.models import DedupResult

            return DedupResult(
                original_count=len(memories),
                final_count=len(memories),
                exact_removed=0,
                semantic_removed=0,
                deduplicated=list(memories),
            )

    monkeypatch.setattr("memplex.compaction.MemoryDeduplicator", _SpyDedup)
    pipe, _store = _make_pipeline(tmp_path, chunk_threshold=7)
    _run(pipe._execute_dedup(CompactionScope.GLOBAL))
    assert captured["chunk_threshold"] == 7


def test_dedup_stage_writes_merged_content_back(tmp_path):
    """Semantic merge: surviving memory gains the removed one's field values."""
    pipe, store = _make_pipeline(tmp_path, dedup_threshold=0.5)
    newer = "2024-06-01T00:00:00+00:00"
    older = "2024-01-01T00:00:00+00:00"
    # Base (newest) keeps its id; the other's benefit must be merged in.
    _insert(store, _func("keep", "deploy application", updated_at=newer,
                         action=[FieldValue(desc="run the deploy script")]))
    _insert(store, _func("drop", "deploy application", updated_at=older,
                         action=[FieldValue(desc="run the deploy script")],
                         benefit=[FieldValue(desc="ships faster")]))

    result = _run(pipe._execute_dedup(CompactionScope.GLOBAL))

    assert result.removed == 1
    assert store.get("drop") is None
    survivor = store.get("keep")
    assert survivor is not None
    assert any(fv.desc == "ships faster" for fv in survivor.benefit)
    # Merged content is persisted, not just mutated in memory.
    reloaded = LiteMemoryStore(path=tmp_path / "memory.json")
    assert any(fv.desc == "ships faster" for fv in reloaded.get("keep").benefit)


def test_summarize_stage_persists_deprecated_marks(tmp_path):
    """Oversized FieldValue lists are marked deprecated AND saved to disk."""
    pipe, store = _make_pipeline(tmp_path, field_max_values=2)
    fvs = [FieldValue(desc=f"action {i}", weight=float(i)) for i in range(4)]
    _insert(store, _func("big", "big function", action=fvs))

    result = _run(pipe._execute_summarize(CompactionScope.GLOBAL))

    assert result.processed == 1
    reloaded = LiteMemoryStore(path=tmp_path / "memory.json")
    actions = reloaded.get("big").action
    deprecated = [fv for fv in actions if fv.status == "deprecated"]
    assert len(deprecated) == 2  # persisted, not only in-memory


def test_prune_stage_persists_field_trimming(tmp_path):
    """Pruned deprecated FieldValues stay pruned after a reload."""
    pipe, store = _make_pipeline(tmp_path)
    _insert(store, _func("p1", "prunable", action=[
        FieldValue(desc="keep me"),
        FieldValue(desc="drop me", status="deprecated"),
    ]))

    result = _run(pipe._execute_prune(CompactionScope.GLOBAL))

    assert result.removed == 1
    reloaded = LiteMemoryStore(path=tmp_path / "memory.json")
    descs = [fv.desc for fv in reloaded.get("p1").action]
    assert descs == ["keep me"]


def test_prune_whole_function_never_reinserts_deleted_replacement(tmp_path):
    pipe, store = _make_pipeline(tmp_path, prune_confidence_threshold=0.9)
    _insert(store, _func("drop-whole", "drop whole", confidence=0.1))
    result = _run(pipe._execute_prune(CompactionScope.GLOBAL))
    assert result.removed == 1
    assert store.get("drop-whole") is None
    reopened = LiteMemoryStore(path=tmp_path / "memory.json")
    assert reopened.get("drop-whole") is None
    assert reopened.get_timeline("drop-whole") == []


def test_archive_stage_writes_full_function_body(tmp_path, monkeypatch):
    """Archive JSON must contain the complete Function, not an id/name stub."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func(
        "old1", "ancient memory", updated_at=old, access_count=0,
        trigger=[FieldValue(desc="when deploy starts")],
        condition=[FieldValue(desc="on linux")],
        action=[FieldValue(desc="run ansible")],
        benefit=[FieldValue(desc="zero downtime")],
        attributes={"team": "infra"},
    ))

    result = _run(pipe._execute_archive(CompactionScope.GLOBAL))

    assert result.removed == 1
    assert store.get("old1") is None
    archive_file = tmp_path / ".memplex" / "archive" / "old1.json"
    data = json.loads(archive_file.read_text(encoding="utf-8"))
    assert data["name"] == "ancient memory"
    assert data["trigger"][0]["desc"] == "when deploy starts"
    assert data["condition"][0]["desc"] == "on linux"
    assert data["action"][0]["desc"] == "run ansible"
    assert data["benefit"][0]["desc"] == "zero downtime"
    assert data["attributes"] == {"team": "infra"}
    assert data["archived_at"]


def test_archive_new_directories_fsync_each_new_parent_entry(tmp_path, monkeypatch):
    """A newly-created archive directory is durable before a source can go."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func("old-parent", "ancient", updated_at=old, access_count=0))
    fsynced: list[Path] = []
    real_fsync_dir = compaction_module._fsync_directory
    monkeypatch.setattr(
        compaction_module,
        "_fsync_directory",
        lambda directory: (fsynced.append(Path(directory)), real_fsync_dir(Path(directory)))[1],
    )

    result = _run(pipe._execute_archive(CompactionScope.GLOBAL))

    assert result.removed == 1
    # tmp -> records .memplex; .memplex -> records archive; archive -> records file rename.
    assert fsynced[:3] == [tmp_path, tmp_path / ".memplex", tmp_path / ".memplex" / "archive"]


def test_archive_parent_directory_fsync_failure_keeps_source(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func("old-parent-fail", "ancient", updated_at=old, access_count=0))
    monkeypatch.setattr(
        compaction_module,
        "_fsync_directory",
        lambda _directory: (_ for _ in ()).throw(OSError("parent fsync")),
    )

    result = _run(pipe._execute_archive(CompactionScope.GLOBAL))

    assert result.removed == 0
    assert store.get("old-parent-fail") is not None


def test_archive_fsync_failure_never_deletes_source(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    (tmp_path / ".memplex" / "archive").mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func("old-fsync", "ancient", updated_at=old, access_count=0))
    real_fsync = os.fsync
    calls = 0

    def fail_directory_fsync(fd):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("dir fsync failed")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    result = _run(pipe._execute_archive(CompactionScope.GLOBAL))
    assert result.removed == 0
    assert store.get("old-fsync") is not None


@pytest.mark.parametrize("fault", ["write", "file_fsync", "rename", "dir_fsync"])
def test_archive_every_durable_stage_failure_keeps_source(tmp_path, monkeypatch, fault):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    (tmp_path / ".memplex" / "archive").mkdir(parents=True)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func("old-stage", "ancient", updated_at=old, access_count=0))
    if fault == "write":
        monkeypatch.setattr(json, "dump", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write")))
    elif fault == "rename":
        monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("rename")))
    else:
        real_fsync = os.fsync
        calls = 0

        def fail_fsync(fd):
            nonlocal calls
            calls += 1
            if calls == (1 if fault == "file_fsync" else 2):
                raise OSError(fault)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_fsync)
    result = _run(pipe._execute_archive(CompactionScope.GLOBAL))
    assert result.removed == 0
    assert store.get("old-stage") is not None


def test_archive_stale_snapshot_rejects_without_deleting_or_counting(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pipe, store = _make_pipeline(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
    _insert(store, _func("old-stale", "ancient", updated_at=old, access_count=0))
    original = pipe._function_snapshot

    from memplex.models import SourceDocument, SourceType

    def stale_snapshot_with_peer():
        generation, functions = original()
        store.add(
            _func("peer-write", "peer"),
            SourceDocument(type="test", source_type=SourceType.WIKI),
        )
        return generation, functions

    monkeypatch.setattr(pipe, "_function_snapshot", stale_snapshot_with_peer)
    with pytest.raises(Exception, match="stale"):
        _run(pipe._execute_archive(CompactionScope.GLOBAL))
    assert store.get("old-stale") is not None
    assert store.get("peer-write") is not None


def test_checkpoint_machinery_removed(tmp_path, monkeypatch):
    """The write-only checkpoint stub is gone; full runs leave no checkpoints."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert not hasattr(CompactionPipeline, "_write_checkpoint")

    pipe, store = _make_pipeline(tmp_path)
    _insert(store, _func("x1", "some function"))
    result = _run(pipe._run_pipeline(CompactionScope.GLOBAL))

    assert result.skipped is False
    assert len(result.stages) == 5
    assert not (tmp_path / ".memplex" / "checkpoints").exists()


def test_dedup_stage_wires_dedup_use_faiss_from_config(tmp_path, monkeypatch):
    """compaction.dedup_use_faiss must reach MemoryDeduplicator."""
    captured = {}

    class _SpyDedup:
        def __init__(self, embedding, threshold=0.95, chunk_threshold=20000, use_faiss=True):
            captured["use_faiss"] = use_faiss

        def deduplicate(self, memories):
            from memplex.models import DedupResult

            return DedupResult(
                original_count=len(memories),
                final_count=len(memories),
                exact_removed=0,
                semantic_removed=0,
                deduplicated=list(memories),
            )

    monkeypatch.setattr("memplex.compaction.MemoryDeduplicator", _SpyDedup)
    pipe, _store = _make_pipeline(tmp_path, dedup_use_faiss=False)
    _run(pipe._execute_dedup(CompactionScope.GLOBAL))
    assert captured["use_faiss"] is False
