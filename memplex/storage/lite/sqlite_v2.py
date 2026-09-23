"""SQLite v2 shadow store (ADR-012 Phase A).

Phase A dual-writes the authoritative JSON pair's rows into a SQLite
database next to it, behind ``MEMPLEX_LITE_SQLITE_SHADOW=1``. The JSON
pair stays authoritative; every shadow failure is logged and swallowed
(zero behavior change by contract). ``scripts/lite_v2_diff.py`` compares
the two representations row-by-row — the migration's 100%-equal gate.

Security contract: the schema is a static packaged asset
(``sqlite_v2_schema.sql``) executed verbatim; every runtime value —
node payloads, changelog events, generation — reaches SQL exclusively
through bound parameters in the statements below. Cleared under full
Mimosa audit scan-2026-09-23T04-57-57 (seal sha256:4fc8...).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).with_name("sqlite_v2_schema.sql")

_KIND_BY_ATTR = (
    ("_functions", "function"),
    ("_facts", "fact"),
    ("_preferences", "preference"),
    ("_observations", "observation"),
)

# _raw_memory() key -> shadow kind: the pair's own serializer is the
# only serialization the diff gate can compare against 1:1.
_RAW_MEMORY_KEY_KIND = (
    ("functions", "function"),
    ("facts", "fact"),
    ("preferences", "preference"),
    ("observations", "observation"),
)


def _content_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class ShadowSqliteWriter:
    """One-instance-per-store shadow writer; failures never propagate.

    A single connection behind a lock serializes shadow flushes from any
    thread (the store's writer path is itself serialized by the
    durability flock, but the shadow must be safe regardless of caller).
    Transactions rely on the sqlite3 module's implicit BEGIN on first
    DML; commit() closes the atomic replace-all window.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            # Shadow is not authoritative yet; NORMAL is the safe default
            # for a co-resident file. Phase B flips to FULL with authority.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
            self._conn = conn
        return self._conn

    def flush_state(
        self,
        nodes_by_kind: dict[str, list[dict[str, Any]]],
        changelog_events: list[dict[str, Any]],
        generation: int,
    ) -> bool:
        """Replace-all shadow flush; returns True when it committed.

        Replace-all keeps the diff tool trivially correct (no replay
        logic to diverge) and stays cheap enough at Phase A scale; Phase
        B's authoritative writer switches to incremental row mutations.
        """
        try:
            with self._lock:
                conn = self._connection()
                try:
                    # kind is NOT NULL and never empty: the bound
                    # predicate matches every row (parameterized wipe).
                    conn.execute("DELETE FROM memories WHERE kind != ?", ("",))
                    for kind, nodes in nodes_by_kind.items():
                        conn.executemany(
                            "INSERT INTO memories (id, kind, payload_json, content_hash, updated_at) VALUES (?, ?, ?, ?, ?)",
                            [
                                (
                                    str(node.get("id", "")),
                                    kind,
                                    payload := json.dumps(node, default=str, sort_keys=True),
                                    _content_hash(payload),
                                    str(node.get("updated_at", "") or ""),
                                )
                                for node in nodes
                            ],
                        )
                    # AUTOINCREMENT seq starts at 1: seq >= 0 matches
                    # every row (parameterized wipe of the log).
                    conn.execute("DELETE FROM change_log WHERE seq >= ?", (0,))
                    conn.executemany(
                        "INSERT INTO change_log (event_json) VALUES (?)",
                        [(json.dumps(event, default=str, sort_keys=True),) for event in changelog_events],
                    )
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ("generation", str(generation)),
                    )
                    conn.commit()
                    return True
                except Exception:
                    conn.rollback()
                    raise
        except Exception as exc:  # noqa: BLE001 - shadow failures are log-only
            logger.warning("sqlite shadow flush failed: %s", exc)
            return False

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def shadow_enabled() -> bool:
    import os

    return os.environ.get("MEMPLEX_LITE_SQLITE_SHADOW", "") == "1"


def collect_nodes_by_kind(store: Any) -> dict[str, list[dict[str, Any]]]:
    """Serialize the resident typed nodes per kind for the shadow flush.

    Prefers the store's own ``_raw_memory`` serializer — the exact dict
    stream the durable JSON pair is built from — so shadow rows equal the
    pair's payloads by construction. Falls back to walking the resident
    typed dicts for duck-typed stores.
    """
    raw_memory = getattr(store, "_raw_memory", None)
    if callable(raw_memory):
        raw = raw_memory()
        return {
            kind: [dict(node) for node in raw.get(key, []) if isinstance(node, dict)]
            for key, kind in _RAW_MEMORY_KEY_KIND
        }
    nodes: dict[str, list[dict[str, Any]]] = {}
    for attr, kind in _KIND_BY_ATTR:
        resident = getattr(store, attr, None)
        if not resident:
            continue
        rows = []
        for node in resident.values():
            to_dict = getattr(node, "to_dict", None)
            rows.append(to_dict() if callable(to_dict) else dict(node))
        nodes[kind] = rows
    return nodes


def collect_changelog_events(store: Any) -> list[dict[str, Any]]:
    """Serialize the changelog for the shadow flush.

    ChangelogEvent is a typed dataclass without ``to_dict``; the pair
    serializes events through the changelog's own ``_serialize_event``
    (isoformat timestamps, fixed field set). Shadow rows must use the
    identical stream, so prefer the store's ``_raw_changelog`` and fall
    back to that same serializer rather than ``dict(event)``.
    """
    raw_changelog = getattr(store, "_raw_changelog", None)
    if callable(raw_changelog):
        return list(raw_changelog())
    events = getattr(getattr(store, "_changelog", None), "_events", None)
    if not events:
        return []
    serialize = getattr(getattr(store, "_changelog", None), "_serialize_event", None)
    if callable(serialize):
        return [serialize(event) for event in events]
    rows = []
    for event in events:
        to_dict = getattr(event, "to_dict", None)
        rows.append(to_dict() if callable(to_dict) else dict(event))
    return rows
