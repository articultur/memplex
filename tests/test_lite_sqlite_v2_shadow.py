"""Shadow store contract tests (ADR-012 Phase A).

Phase A is zero-behavior-change by contract: the shadow flush runs after
every successful durable commit when enabled, never propagates failures,
and produces a row-for-row mirror that lite_v2_diff verifies as EQUAL.
"""

import json
import os
import sqlite3

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

from memplex.storage.lite.sqlite_v2 import (
    ShadowSqliteWriter,
    collect_changelog_events,
    collect_nodes_by_kind,
)


def test_flush_and_row_shape(tmp_path):
    writer = ShadowSqliteWriter(tmp_path / "shadow_v2.sqlite3")
    nodes = {
        "function": [{"id": "f1", "name": "fact one", "updated_at": "2026-09-24T00:00:00Z"}],
        "fact": [{"id": "x1", "subject": "db", "object_": "postgres"}],
    }
    events = [{"event_type": "created", "func_id": "f1"}]
    assert writer.flush_state(nodes, events, generation=7)
    writer.close()

    conn = sqlite3.connect(str(tmp_path / "shadow_v2.sqlite3"))
    rows = list(
        conn.execute("SELECT id, kind, payload_json, content_hash FROM memories ORDER BY id")
    )
    assert [r[0] for r in rows] == ["f1", "x1"]
    assert rows[0][1] == "function" and rows[1][1] == "fact"
    import hashlib

    expected_hash = hashlib.sha256(rows[0][2].encode()).hexdigest()
    assert rows[0][3] == expected_hash
    events_out = [json.loads(r[0]) for r in conn.execute("SELECT event_json FROM change_log")]
    assert events_out == events
    generation = conn.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
    assert generation[0] == "7"


def test_flush_is_replace_all(tmp_path):
    writer = ShadowSqliteWriter(tmp_path / "shadow_v2.sqlite3")
    writer.flush_state({"function": [{"id": "f1", "name": "v1"}]}, [], generation=1)
    assert writer.flush_state({"function": [{"id": "f2", "name": "v2"}]}, [], generation=2)
    writer.close()
    conn = sqlite3.connect(str(tmp_path / "shadow_v2.sqlite3"))
    ids = [r[0] for r in conn.execute("SELECT id FROM memories")]
    assert ids == ["f2"], "stale shadow rows must not survive a flush"


def test_flush_failure_is_swallowed(tmp_path, monkeypatch):
    writer = ShadowSqliteWriter(tmp_path / "shadow_v2.sqlite3")
    writer.flush_state({"function": [{"id": "f1"}]}, [], generation=1)
    monkeypatch.setattr(
        writer, "_connection", lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    )
    assert writer.flush_state({"function": []}, [], generation=2) is False


def test_shadow_disabled_by_default():
    from memplex.storage.lite.sqlite_v2 import shadow_enabled

    monkey_env = os.environ.pop("MEMPLEX_LITE_SQLITE_SHADOW", None)
    try:
        assert shadow_enabled() is False
    finally:
        if monkey_env is not None:
            os.environ["MEMPLEX_LITE_SQLITE_SHADOW"] = monkey_env


def test_collectors_read_store_shapes(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    svc.write_text("Alice keeps a blue parrot named Kiwi.", source_type="text")
    try:
        nodes = collect_nodes_by_kind(svc.store)
        assert sum(len(v) for v in nodes.values()) >= 1
        events = collect_changelog_events(svc.store)
        assert isinstance(events, list)
    finally:
        svc.stop()


def test_collectors_serialize_changelog_events(tmp_path):
    """Regression: ChangelogEvent has no to_dict; dict(event) raised out
    of the hook and silently skipped the flush for any commit that
    appended a changelog event."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    svc.write_text("Bob prefers decaf coffee in the evenings.", source_type="text")
    try:
        events = collect_changelog_events(svc.store)
        assert events, "preference write must append a changelog event"
        assert all(
            set(event)
            == {"func_id", "timestamp", "event_type", "description", "source", "actor"}
            for event in events
        ), f"events must use the pair's canonical serialization: {events}"
        nodes = collect_nodes_by_kind(svc.store)
        assert sum(len(v) for v in nodes.values()) >= 1
    finally:
        svc.stop()


def test_hook_swallows_collector_failure(tmp_path, monkeypatch):
    """The log-only contract must hold for collector failures too, not
    just writer failures: the durable pair is already committed by the
    time the hook runs, so nothing after it may raise."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService
    from memplex.storage.lite import sqlite_v2

    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_SHADOW", "1")
    monkeypatch.setattr(
        sqlite_v2,
        "collect_changelog_events",
        lambda store: (_ for _ in ()).throw(TypeError("boom")),
    )
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    try:
        svc.write_text("Carol adopts a grey cat named Misty.", source_type="text")
    finally:
        svc.stop()


def test_end_to_end_shadow_matches_pair(tmp_path, monkeypatch, capsys):
    """ADR-012 Phase A acceptance: after real writes (including one that
    appends a changelog event), scripts/lite_v2_diff.py must report the
    shadow and the authoritative JSON pair as 100% EQUAL."""
    import importlib.util
    from pathlib import Path

    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_SHADOW", "1")
    store_dir = tmp_path / "s.sqlite3"
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(store_dir)
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    svc.write_text("Alice keeps a blue parrot named Kiwi.", source_type="text")
    svc.write_text("Bob prefers decaf coffee in the evenings.", source_type="text")
    svc.stop()

    assert (store_dir / "shadow_v2.sqlite3").exists(), "shadow db must exist after commits"
    script = Path(__file__).resolve().parent.parent / "scripts" / "lite_v2_diff.py"
    spec = importlib.util.spec_from_file_location("lite_v2_diff", script)
    assert spec is not None and spec.loader is not None
    diff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(diff)
    monkeypatch.setattr("sys.argv", ["lite_v2_diff.py", str(store_dir)])
    assert diff.main() == 0, f"diff gate failed:\n{capsys.readouterr().out}"


def test_authority_read_reconstructs_pair(tmp_path, monkeypatch):
    """Phase-B B1: with the shadow db populated, loading under
    MEMPLEX_LITE_SQLITE_AUTHORITY=read must yield the same resident
    state as the JSON-authority load (nodes, edges, events, generation)."""
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_SHADOW", "1")
    store_dir = tmp_path / "s.sqlite3"
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(store_dir)
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    svc.write_text("Alice keeps a blue parrot named Kiwi.", source_type="text")
    svc.write_text("Bob prefers decaf coffee in the evenings.", source_type="text")
    svc.stop()
    assert (store_dir / "shadow_v2.sqlite3").exists()

    def resident_snapshot(authority: str) -> dict:
        os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = authority
        cfg = MemplexConfig()
        cfg.storage.backend = "lite"
        cfg.storage.path = str(store_dir)
        cfg.llm.query_enhancement = False
        svc = MemplexService(config=cfg)
        svc.start()
        try:
            store = svc.store
            return {
                "nodes": sorted(
                    n.id
                    for grp in (store._functions, store._facts, store._preferences)
                    for n in grp.values()
                ),
                "paras": sorted(store._paragraphs),
                "edges": len(store._edges),
                "events": len(store._changelog.snapshot()),
                "gen": store._generation,
            }
        finally:
            svc.stop()
            os.environ.pop("MEMPLEX_LITE_SQLITE_AUTHORITY", None)

    read_state = resident_snapshot("read")
    json_state = resident_snapshot("json")
    assert read_state["nodes"], "authority read must yield a non-empty resident"
    assert read_state == json_state, (
        f"authority modes diverge: read={read_state} json={json_state}"
    )

    import sqlite3 as sq

    conn = sq.connect(str(store_dir / "shadow_v2.sqlite3"))
    meta_gen = int(dict(conn.execute("SELECT key, value FROM meta"))["generation"])
    conn.close()
    assert read_state["gen"] == meta_gen, (
        "authority read must carry the SQLite generation"
    )

    # Discriminating check: a row that exists ONLY in SQLite must be
    # visible under read authority and invisible under JSON authority -
    # otherwise the flag silently no-ops on synchronized stores.
    import hashlib
    import json as _json

    only_sqlite = {
        "id": "fact_sqliteonly",
        "memory_type": "fact",
        "name": "sqlite-only row",
        "domain": None,
        "confidence": 1.0,
        "source_type": "wiki",
        "owner": None,
        "tenant_id": None,
        "owner_subject_id": None,
        "workspace_id": None,
        "visibility": None,
        "provenance": {},
        "version": 1,
        "created_at": "2026-09-26T00:00:00+00:00",
        "updated_at": "2026-09-26T00:00:00+00:00",
        "origin_session": None,
        "access_count": 0,
        "last_accessed_at": None,
        "source_paragraphs": [],
        "needs_review": False,
        "needs_review_until": None,
        "content_hash": None,
        "namespace": {},
        "knowledge_tier": None,
        "trust_tier": 3,
        "subject": "probe",
        "predicate": "proves",
        "object": "authority",
        "valid_until": None,
        "valid_from": None,
        "invalid_at": None,
    }
    payload = _json.dumps(only_sqlite, default=str, sort_keys=True)
    conn = sq.connect(str(store_dir / "shadow_v2.sqlite3"))
    conn.execute(
        "INSERT INTO memories (id, kind, payload_json, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
        (
            "fact_sqliteonly",
            "fact",
            payload,
            hashlib.sha256(payload.encode()).hexdigest(),
            "2026-09-26T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()

    read_extra = resident_snapshot("read")
    json_after = resident_snapshot("json")
    assert "fact_sqliteonly" in read_extra["nodes"], (
        "read authority must see the SQLite-only row"
    )
    assert "fact_sqliteonly" not in json_after["nodes"], (
        "json authority must not see the SQLite-only row"
    )
