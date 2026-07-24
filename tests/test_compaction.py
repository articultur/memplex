"""Test memplex/compaction.py: lock primitives and pipeline wiring.

Previously zero direct coverage (test evaluation #1). The full
CompactionPipeline.run is heavy integration (covered indirectly via
service.compact); these tests cover the lock primitives, lock-key
derivation, and lock-backend selection -- the pieces most likely to
silently regress and which contain the untested PGAdvisoryLock path.
"""

import asyncio
import os
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.compaction import (  # noqa: E402
    CompactionLock,
    CompactionPipeline,
    FileLock,
    PGAdvisoryLock,
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


# ── PGAdvisoryLock (stubbed pool) ────────────────────────────────────


class _FakeConn:
    def __init__(self, lock_succeeds: bool = True):
        self._ok = lock_succeeds
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        if sql.startswith("SELECT pg_try_advisory_lock"):
            return self._ok
        return True  # pg_advisory_unlock


class _FakePool:
    def __init__(self, lock_succeeds: bool = True):
        self._conn = _FakeConn(lock_succeeds)
        self.released = False

    async def acquire(self):
        return self._conn

    async def release(self, conn):
        self.released = True


def test_pg_advisory_lock_acquire_success():
    pool = _FakePool(lock_succeeds=True)
    lock = PGAdvisoryLock(key="enterprise-scope", pool=pool)
    assert _run(lock.try_acquire()) is True
    assert lock._conn is pool._conn


def test_pg_advisory_lock_acquire_failure_returns_conn_to_pool():
    pool = _FakePool(lock_succeeds=False)
    lock = PGAdvisoryLock(key="enterprise-scope", pool=pool)
    assert _run(lock.try_acquire()) is False
    assert pool.released is True
    assert lock._conn is None


def test_pg_advisory_lock_release_calls_unlock_and_returns_conn():
    pool = _FakePool(lock_succeeds=True)
    lock = PGAdvisoryLock(key="k", pool=pool)
    _run(lock.try_acquire())
    pool.released = False  # reset to observe release path
    _run(lock.release())
    # The unlock SQL was issued and the conn was returned.
    assert any("pg_advisory_unlock" in c[0] for c in pool._conn.calls)
    assert pool.released is True
    assert lock._conn is None


def test_pg_advisory_lock_lock_id_is_stable_positive_int64():
    pool1 = _FakePool()
    pool2 = _FakePool()
    l1 = PGAdvisoryLock(key="same", pool=pool1)
    l2 = PGAdvisoryLock(key="same", pool=pool2)
    assert l1._lock_id == l2._lock_id
    assert 0 <= l1._lock_id < 2**63


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


def test_pipeline_builds_pg_lock_for_enterprise_when_pool_injected():
    cfg = MemplexConfig()
    cfg.storage.backend = "enterprise"
    pool = _FakePool()

    class _StubStore:
        pass

    pipe = CompactionPipeline(store=_StubStore(), embedding_service=None, config=cfg)
    pipe._pg_pool = pool  # inject
    lock = pipe._build_lock(CompactionScope.PROJECT)
    assert isinstance(lock, PGAdvisoryLock)
