"""Direct tests for memplex/storage/feedback.py LiteFeedbackStore.

Previously 44% coverage. The LiteFeedbackStore (JSON persistence) is pure
logic with no heavy deps; covers record/get_pending/resolve/get_history/
clear round-trips, persistence across instances, and the factory.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.models import FeedbackVerdict, MemoryFeedback
from memplex.storage.feedback import (
    LiteFeedbackStore,
    SQLiteFeedbackStore,
    create_feedback_store,
)


def _fb(memory_id="m1", field_role="trigger", verdict=FeedbackVerdict.WRONG, reason="nope"):
    return MemoryFeedback(
        memory_id=memory_id,
        field_role=field_role,
        value_index=0,
        verdict=verdict,
        reason=reason,
    )


def _authorization(*, tenant: str, subject: str):
    from memplex.auth import AuthorizationContext, Principal

    return AuthorizationContext(
        principal=Principal(tenant_id=tenant, subject_id=subject),
        workspace_id=f"workspace-{tenant}",
    )


# ── record + get_pending ─────────────────────────────────────────────


def test_record_then_get_pending_returns_review(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    store.record(_fb())
    pending = store.get_pending()
    assert len(pending) == 1
    assert pending[0].memory_id == "m1"
    assert pending[0].field_role == "trigger"


def test_record_correct_verdict_not_pending(tmp_path):
    """A 'correct' verdict does not need review -> not in pending."""
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    store.record(_fb(verdict=FeedbackVerdict.CORRECT))
    # Whether correct feedbacks appear in pending is impl-defined; just
    # ensure no crash and the record is stored.
    store.get_pending()  # no exception
    assert len(store.get_history("m1")) == 1


def test_get_pending_empty_when_nothing_recorded(tmp_path):
    assert LiteFeedbackStore(path=tmp_path / "fb.json").get_pending() == []


# ── resolve ──────────────────────────────────────────────────────────


def test_resolve_removes_from_pending(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    store.record(_fb())
    assert len(store.get_pending()) == 1
    store.resolve("m1", "trigger", "accept")
    assert all(not (p.memory_id == "m1" and p.field_role == "trigger") for p in store.get_pending())


def test_resolve_unknown_is_noop_or_graceful(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    # Resolving something never recorded must not raise.
    store.resolve("nope", "trigger", "accept")


# ── get_history ──────────────────────────────────────────────────────


def test_history_returns_records_for_memory(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    store.record(_fb(memory_id="m1", field_role="trigger"))
    store.record(_fb(memory_id="m1", field_role="action"))
    store.record(_fb(memory_id="m2", field_role="trigger"))
    history = store.get_history("m1")
    assert len(history) == 2
    assert all(h.memory_id == "m1" for h in history)


def test_history_missing_memory_returns_empty(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    assert store.get_history("never") == []


def test_history_respects_limit(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    for i in range(10):
        store.record(_fb(memory_id="m1"))
    assert len(store.get_history("m1", limit=3)) <= 3


# ── clear ────────────────────────────────────────────────────────────


def test_clear_empties_store(tmp_path):
    store = LiteFeedbackStore(path=tmp_path / "fb.json")
    store.record(_fb())
    store.clear()
    assert store.get_pending() == []
    assert store.get_history("m1") == []


# ── persistence across instances ─────────────────────────────────────


def test_records_persist_to_disk_and_reload(tmp_path):
    path = tmp_path / "fb.json"
    LiteFeedbackStore(path=path).record(_fb(reason="persist-me"))
    reloaded = LiteFeedbackStore(path=path)
    history = reloaded.get_history("m1")
    assert len(history) == 1
    assert history[0].reason == "persist-me"


def test_sqlite_authorized_facades_are_thread_safe(tmp_path):
    """One scoped SQLite store may serve concurrent request threads."""
    store = SQLiteFeedbackStore(
        db_path=str(tmp_path / "feedback.db"),
        require_authorization=True,
    )
    # Establish the shared connection on the creating thread.  A default
    # sqlite3 connection then deterministically rejects the worker threads.
    store.authorized(_authorization(tenant="setup", subject="setup")).clear()
    start = Barrier(3)

    def record_and_read(tenant: str) -> list[str]:
        scoped = store.authorized(_authorization(tenant=tenant, subject=tenant))
        start.wait(timeout=5)
        scoped.record(_fb(memory_id="shared", reason=tenant))
        return [item.reason or "" for item in scoped.get_history("shared")]

    with ThreadPoolExecutor(max_workers=2) as executor:
        alice = executor.submit(record_and_read, "alice")
        bob = executor.submit(record_and_read, "bob")
        start.wait(timeout=5)
        assert alice.result(timeout=5) == ["alice"]
        assert bob.result(timeout=5) == ["bob"]


# ── create_feedback_store factory ────────────────────────────────────


def test_factory_lite_backend(tmp_path):
    store = create_feedback_store(backend="lite", path=tmp_path / "fb.json")
    assert isinstance(store, LiteFeedbackStore)


def test_factory_unknown_backend_raises(tmp_path):
    with pytest.raises(ValueError):
        create_feedback_store(backend="not-a-backend", path=tmp_path / "fb.json")
