"""Working-memory invalidation hook: main-store mutations clear the
mirrored hot-tier entries (known-issue 13 closure).

The hot tier used to be an independent short-lived consistency boundary:
a deleted memory kept surfacing in the [WORKING MEMORY] block until its
TTL elapsed. service.delete() and service.update_memory() now call
_invalidate_hot_entries, dropping both key spellings (node id and
fn:{id}) across every tenant scope.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.config import MemplexConfig
from memplex.service import MemplexService
from memplex.working_memory import WorkingMemory


def _service(tmp_path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.working_memory.enabled = True
    cfg.working_memory.default_ttl_seconds = 600
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def test_remove_by_suffix_drops_both_spellings():
    wm = WorkingMemory()
    wm.add("fact_123", "alpha", scope="tenant:acme")
    wm.add("fn:fn_456", "beta", scope="tenant:acme")
    wm.add("fn_789", "gamma", scope="tenant:other")
    wm.add("unrelated", "keep", scope="tenant:acme")

    assert wm.remove_by_suffix("fn_456") == 1
    assert wm.remove_by_suffix("fact_123") == 1
    assert wm.remove_by_suffix("missing") == 0

    keys = set(wm._entries)
    assert "tenant:acme::fn:fn_456" not in keys
    assert "tenant:acme::fact_123" not in keys
    assert "tenant:other::fn_789" in keys
    assert "tenant:acme::unrelated" in keys


def test_delete_memory_clears_hot_entry(tmp_path):
    """service.delete() drops the mirrored hot entry for the node id
    (both spellings) so recall stops surfacing the deleted memory."""
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Alice prefers decaf espresso beans for late shifts.",
            source_type="text",
        )
        facts = list(svc.store._facts.values())
        preferences = list(svc.store._preferences.values())
        targets = facts + preferences
        assert targets, "extraction must produce a typed node"
        node = targets[0]
        # Mirror what the write path does: the node id lands in the hot tier.
        svc._working_memory.add(
            str(node.id), "mirrored content", scope="tenant:local"
        )
        assert any(str(node.id) in k for k in svc._working_memory._entries)

        svc.delete(node.id)

        assert not any(
            str(node.id) in k for k in svc._working_memory._entries
        ), "hot entry for a deleted memory must be dropped"
        # fn: spelling too
        assert svc._working_memory.remove_by_suffix(node.id) == 0
    finally:
        svc.stop()


def test_delete_memory_clears_fn_spelling(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "The ferry schedule lookup runs every morning.",
            source_type="text",
        )
        functions = list(svc.store._functions.values())
        assert functions, "extraction must produce a function"
        func = functions[0]
        svc._working_memory.add(
            f"fn:{func.id}", func.name, scope="tenant:local"
        )
        assert any(f"fn:{func.id}" in k for k in svc._working_memory._entries)

        svc.delete(func.id)

        assert not any(
            f"fn:{func.id}" in k for k in svc._working_memory._entries
        ), "fn:-mirrored hot entry must be dropped on delete"
    finally:
        svc.stop()


def test_update_memory_invalidates_stale_hot_entry(tmp_path):
    """update_memory drops the pre-update hot entry; the stale text must
    not keep surfacing until TTL."""
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "When the ferry arrives, notify the harbour master.",
            source_type="text",
        )
        functions = list(svc.store._functions.values())
        assert functions
        func = functions[0]
        svc._working_memory.add(
            f"fn:{func.id}", "old ferry name", scope="tenant:local"
        )

        svc.update_memory(func.id, "action", "notify the coast guard instead")

        assert not any(
            f"fn:{func.id}" in k for k in svc._working_memory._entries
        ), "stale hot entry must be invalidated on update"
    finally:
        svc.stop()
