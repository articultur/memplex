"""Tests for the working-memory hot-context tier (memplex/working_memory.py)."""

from __future__ import annotations

import os
import time

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.working_memory import WorkingMemory  # noqa: E402


def test_add_and_recency_ordered_recall():
    wm = WorkingMemory(default_ttl_seconds=60)
    wm.add("a", "first turn")
    time.sleep(0.01)
    wm.add("b", "second turn")
    assert wm.recall_context() == ["second turn", "first turn"]
    assert len(wm) == 2


def test_ttl_expiry_drops_entries():
    wm = WorkingMemory(default_ttl_seconds=0.01)
    wm.add("x", "ephemeral")
    time.sleep(0.02)
    assert wm.recall_context() == []
    assert len(wm) == 0


def test_pinned_entries_survive_ttl_and_cap():
    wm = WorkingMemory(max_entries=3, default_ttl_seconds=60)
    wm.add("pin1", "pinned fact", pinned=True, ttl_seconds=0.01)
    wm.add("tmp1", "t1")
    wm.add("tmp2", "t2")
    wm.add("tmp3", "t3")  # cap: evicts oldest unpinned (tmp1)
    time.sleep(0.02)
    live = wm.recall_context()
    assert "pinned fact" in live  # pinned survives its own short ttl
    assert "t1" not in live and "t2" in live and "t3" in live


def test_pin_unpin_lifecycle():
    wm = WorkingMemory(default_ttl_seconds=0.01)
    wm.add("k", "v")
    assert wm.pin("k") is True
    assert wm.pin("missing") is False
    time.sleep(0.02)
    assert wm.recall_context() == ["v"]  # pinned survives
    assert wm.unpin("k", ttl_seconds=0.01) is True
    time.sleep(0.02)
    assert wm.recall_context() == []


def test_add_refresh_and_remove():
    wm = WorkingMemory()
    wm.add("k", "old")
    wm.add("k", "new")  # same key refreshes, no growth
    assert len(wm) == 1
    assert wm.recall_context() == ["new"]
    assert wm.remove("k") is True
    assert wm.remove("k") is False


def test_limit_and_invalid_inputs():
    wm = WorkingMemory()
    for i in range(10):
        wm.add(f"k{i}", f"c{i}")
    assert len(wm.recall_context(limit=3)) == 3
    wm.add("", "empty key ignored")
    wm.add("k", "")
    assert len(wm) == 10


def test_service_integration_injects_on_recall(tmp_path):
    """End to end: enabled tier captures typed writes and prepends on recall."""
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.working_memory.enabled = True
    cfg.working_memory.default_ttl_seconds = 60

    svc = MemplexService(config=cfg)
    svc.start()
    try:
        from memplex.models import SourceDocument, SourceType

        svc.write(
            SourceDocument(
                type="conversation",
                content="The deployment pipeline now requires two reviewers.",
                source_type=SourceType.MEETING,
            )
        )
        runtime = AgentMemoryRuntime(service=svc, agent="codex")
        ctx = runtime._recall("pipeline", source="live").context
        assert "[WORKING MEMORY]" in ctx
    finally:
        svc.stop()


def test_service_disabled_by_default(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    svc = MemplexService(config=cfg)
    assert svc._working_memory is None
