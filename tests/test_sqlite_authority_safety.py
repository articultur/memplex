"""SQLite write-authority safety acceptance (external review phase 2).

The three named data-safety risks, reproduced dynamically before fixing:
T1 deleting every record under rw authority and reloading resurrects the
   last JSON snapshot (the empty-authoritative-store window);
T2 a tampered payload row (stale content_hash) loads unverified;
T3 a corrupted authority database silently falls back to the stale JSON
   pair instead of failing closed.

These tests encode the acceptance bar: deletion must not resurrect,
tampering must be refused, authority corruption must not silently
rewind state.
"""

import json
import os
import sqlite3

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest


def _rw_service(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = "rw"
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    return svc


def _teardown_env():
    os.environ.pop("MEMPLEX_LITE_SQLITE_AUTHORITY", None)


def _db_path(tmp_path):
    return tmp_path / "s.sqlite3" / "shadow_v2.sqlite3"


def _seed_and_snapshot(svc, n=9):
    """Write enough records that the 8-generation JSON snapshot cadence
    has exported a NON-empty pair - the precondition for the
    resurrection window."""
    for i in range(n):
        svc.write_text(
            f"Lighthouse log entry {i}: the keeper saw storm {i}.",
            source_type="text",
        )


def _clear_everything(svc):
    with svc.store.deferred_commit():
        svc.store._functions.clear()
        svc.store._edges.clear()
        svc.store._facts.clear()
        svc.store._preferences.clear()
        svc.store._paragraphs.clear()
        svc.store._observations.clear()


def test_T1_delete_to_empty_stays_empty_after_reload(tmp_path):
    svc = _rw_service(tmp_path)
    try:
        _seed_and_snapshot(svc)
        assert svc.store._paragraphs, "precondition: records exist"
        _clear_everything(svc)
        assert not svc.store._paragraphs
    finally:
        svc.stop()

    reopened = _rw_service(tmp_path)
    try:
        counts = [
            len(reopened.store._functions),
            len(reopened.store._facts),
            len(reopened.store._preferences),
            len(reopened.store._paragraphs),
            len(reopened.store._observations),
        ]
        assert counts == [0, 0, 0, 0, 0], (
            "records resurrected from the stale JSON snapshot after the "
            "authoritative store was deleted to empty"
        )
    finally:
        reopened.stop()
        _teardown_env()


def test_T2_tampered_payload_refused(tmp_path):
    from memplex.storage.lite.sqlite_v2 import SQLiteAuthorityError

    svc = _rw_service(tmp_path)
    try:
        _seed_and_snapshot(svc, n=3)
    finally:
        svc.stop()

    conn = sqlite3.connect(str(_db_path(tmp_path)))
    row = conn.execute(
        "SELECT rowid, payload_json FROM memories LIMIT 1"
    ).fetchone()
    tampered = json.loads(row[1])
    tampered["tampered"] = True
    conn.execute(
        "UPDATE memories SET payload_json = ? WHERE rowid = ?",
        (json.dumps(tampered), row[0]),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SQLiteAuthorityError):
        _rw_service(tmp_path).stop()
    _teardown_env()


def test_T3_corrupted_authority_db_fails_closed(tmp_path):
    from memplex.storage.lite.sqlite_v2 import SQLiteAuthorityError

    svc = _rw_service(tmp_path)
    try:
        _seed_and_snapshot(svc, n=3)
    finally:
        svc.stop()

    # Media-level corruption: SQLite reconstructs from the WAL when only
    # the main file is damaged (verified: that path is a non-event), so
    # the corruption must take the WAL and shm too.
    _db_path(tmp_path).write_bytes(b"not a database at all" * 100)
    for suffix in ("-wal", "-shm"):
        sidecar = _db_path(tmp_path).with_name(_db_path(tmp_path).name + suffix)
        if sidecar.exists():
            os.remove(sidecar)

    with pytest.raises(SQLiteAuthorityError):
        _rw_service(tmp_path).stop()
    _teardown_env()


def test_shadow_mirror_lag_still_falls_back_to_json(tmp_path, monkeypatch):
    """The mirror semantics must survive the hardening: a store written
    WITHOUT rw authority (shadow mirror only) that ends up empty must
    still fall back to the JSON pair - the mirror lagging is normal."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_SHADOW", "1")
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY", raising=False)
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    try:
        _seed_and_snapshot(svc, n=3)
        assert svc.store._paragraphs
        # Wipe the mirror only: the JSON pair (the authority here) keeps
        # the records, and a reload must recover them from JSON.
        svc.stop()
        db = _db_path(tmp_path)
        if db.exists():
            os.remove(db)
    except BaseException:
        svc.stop()
        raise
    svc2 = MemplexService(config=config)
    svc2.start()
    try:
        assert svc2.store._paragraphs, (
            "a wiped shadow mirror must fall back to the JSON pair"
        )
    finally:
        svc2.stop()


def test_T4_restore_mirrors_into_sqlite_authority(tmp_path, monkeypatch):
    """External review: restore wrote only JSON - under rw a restart would
    read the pre-restore rows out of the authority DB and undo it."""
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "rw")
    from memplex.models import Function, SourceDocument, SourceType
    from memplex.storage.lite.store import LiteMemoryStore

    path = tmp_path / "s.sqlite3" / "memory.json"
    store = LiteMemoryStore(path, deployment_profile="development")
    source = SourceDocument(type="test", source_type=SourceType.WIKI)
    store.add(Function(id="before", name="before", name_normalized="before"), source)
    manifest = store.create_backup(tmp_path / "backups", bytes(range(32)), "k")
    artifact = tmp_path / "backups" / manifest.backup_id
    store.delete("before")
    store.add(Function(id="after", name="after", name_normalized="after"), source)

    store.restore_backup(artifact, bytes(range(32)))

    reopened = LiteMemoryStore(path, deployment_profile="development")
    assert reopened.get("before") is not None, (
        "restore undone after restart: the authority DB still held the pre-restore state"
    )
    assert reopened.get("after") is None
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY")


def test_T5_fingerprint_tracks_sqlite_authority(tmp_path, monkeypatch):
    """External review: freshness fingerprint covered only the JSON pair,
    so a peer's rw commit (SQLite-only) was invisible to a second
    instance's unchanged-files short-circuit."""
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "rw")
    from memplex.models import Function, SourceDocument, SourceType
    from memplex.storage.lite.store import LiteMemoryStore

    path = tmp_path / "s.sqlite3" / "memory.json"
    store = LiteMemoryStore(path, deployment_profile="development")
    source = SourceDocument(type="test", source_type=SourceType.WIKI)
    store.add(Function(id="a", name="a", name_normalized="a"), source)
    fingerprint_before = store._pair_fingerprint

    # A peer rw commit changes only the authority DB.
    db = tmp_path / "s.sqlite3" / "shadow_v2.sqlite3"
    import sqlite3 as sq

    conn = sq.connect(str(db))
    conn.execute(
        "INSERT INTO memories (kind, payload_json, content_hash) VALUES (?, ?, ?)",
        ("function", '{"id": "peer"}', "irrelevant-for-stat"),
    )
    conn.commit()
    conn.close()

    assert store._pair_fingerprint == fingerprint_before, "precondition"
    assert not store._pair_files_unchanged(), (
        "a peer's SQLite-only commit must invalidate the unchanged-files "
        "short-circuit"
    )
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY")


def test_T6_unknown_kind_fails_closed_under_rw(tmp_path, monkeypatch):
    """External review: an unknown record kind returned None (JSON
    fallback) even on a write-authoritative store."""
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "rw")
    from memplex.models import Function, SourceDocument, SourceType
    from memplex.storage.lite.sqlite_v2 import SQLiteAuthorityError
    from memplex.storage.lite.store import LiteMemoryStore

    path = tmp_path / "s.sqlite3" / "memory.json"
    store = LiteMemoryStore(path, deployment_profile="development")
    source = SourceDocument(type="test", source_type=SourceType.WIKI)
    store.add(Function(id="a", name="a", name_normalized="a"), source)

    import sqlite3 as sq

    from memplex.storage.lite.sqlite_v2 import _content_hash

    conn = sq.connect(str(tmp_path / "s.sqlite3" / "shadow_v2.sqlite3"))
    conn.execute(
        "INSERT INTO memories (kind, payload_json, content_hash) VALUES (?, ?, ?)",
        ("mystery-kind", "{}", _content_hash("{}")),
    )
    conn.commit()
    conn.close()

    with pytest.raises(SQLiteAuthorityError, match="unknown record kind"):
        LiteMemoryStore(path, deployment_profile="development")
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY")
