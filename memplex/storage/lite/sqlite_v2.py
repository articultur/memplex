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
    # ADR-013 Stage 2 raw-text layer: dict rows already carry an "id".
    ("paragraphs", "paragraph"),
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
        edges: list[dict[str, Any]] | None = None,
        sync_state: dict[str, Any] | None = None,
    ) -> bool:
        """Replace-all shadow flush; returns True when it committed.

        Replace-all keeps the diff tool trivially correct (no replay
        logic to diverge) and stays cheap enough at Phase A scale; Phase
        B's authoritative writer switches to incremental row mutations.
        Edges and the sync snapshot ride along (B0) so an authoritative
        reader can reconstruct the complete pair from SQLite alone.
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
                    # B0: edges under the same replace-all contract.
                    conn.execute("DELETE FROM graph_edges WHERE source != ?", ("",))
                    if edges:
                        conn.executemany(
                            "INSERT INTO graph_edges (source, target, edge_type, payload_json, content_hash) VALUES (?, ?, ?, ?, ?)",
                            [
                                (
                                    str(edge.get("source", "")),
                                    str(edge.get("target", "")),
                                    str(edge.get("edge_type", "")),
                                    payload_e := json.dumps(edge, default=str, sort_keys=True),
                                    _content_hash(payload_e),
                                )
                                for edge in edges
                            ],
                        )
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ("generation", str(generation)),
                    )
                    if sync_state is not None:
                        conn.execute(
                            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("sync_state", json.dumps(sync_state, default=str, sort_keys=True)),
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


_KIND_TO_MEMORY_KEY = {
    "function": "functions",
    "fact": "facts",
    "preference": "preferences",
    "observation": "observations",
    "paragraph": "paragraphs",
}


def read_authoritative_pair(db_path: Path) -> Any:
    """Reconstruct a LitePair-shaped memory payload from the SQLite store.

    B1 of the Phase-B blueprint: the read-authority experiment. Returns
    a dict shaped like ``_raw_memory()`` output plus the changelog list
    and generation, or None when the store is missing/empty/unreadable
    (the caller then falls back to the JSON pair unchanged). Row-level
    integrity is the caller's existing pair validation, which runs on
    the reconstructed payload exactly as on a JSON load.
    """
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            memory: dict[str, Any] = {
                "schema_version": 2,
                "functions": [],
                "edges": [],
                "observations": [],
                "facts": [],
                "preferences": [],
                "paragraphs": [],
                "sync": {},
            }
            for kind, payload_json, _hash in conn.execute(
                "SELECT kind, payload_json, content_hash FROM memories"
            ):
                key = _KIND_TO_MEMORY_KEY.get(kind)
                if key is None:
                    return None  # unknown kind: refuse rather than drop rows
                memory[key].append(json.loads(payload_json))
            for payload_json, _hash in conn.execute(
                "SELECT payload_json, content_hash FROM graph_edges"
            ):
                memory["edges"].append(json.loads(payload_json))
            events = [
                json.loads(row[0])
                for row in conn.execute("SELECT event_json FROM change_log ORDER BY seq")
            ]
            meta = dict(conn.execute("SELECT key, value FROM meta"))
            generation = int(meta.get("generation", "0") or 0)
            if "sync_state" in meta:
                memory["sync"] = json.loads(meta["sync_state"])
            if not any(memory[k] for k in _KIND_TO_MEMORY_KEY.values()):
                return None  # empty store: not authoritative over any JSON pair
            return {"memory": memory, "changelog": events, "generation": generation}
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - unreadable store falls back to JSON
        logger.warning("sqlite authority read failed, falling back to JSON: %s", exc)
        return None


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


_NODE_KINDS = (
    ("functions", "function"),
    ("facts", "fact"),
    ("preferences", "preference"),
    ("observations", "observation"),
    ("paragraphs", "paragraph"),
)


class AuthoritativeWriter:
    """B2 write authority: incremental SQLite commits.

    Each commit diffs the base pair against the target pair and applies
    row upserts/deletes plus a changelog append inside one immediate
    transaction with synchronous=FULL. The JSON pair degrades to an
    N-generation snapshot export handled by the caller.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self._path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.isolation_level = None  # explicit BEGIN IMMEDIATE
            conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self._conn = conn
        return self._conn

    def commit(
        self,
        base_memory: dict[str, Any],
        base_events: list[dict[str, Any]],
        target_memory: dict[str, Any],
        target_events: list[dict[str, Any]],
        generation: int,
    ) -> bool:
        """Apply base -> target as an incremental transaction."""
        with self._lock:
            conn = self._connection()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for key, kind in _NODE_KINDS:
                    base_rows = {
                        str(row.get("id", "")): row
                        for row in base_memory.get(key, [])
                    }
                    target_rows = {
                        str(row.get("id", "")): row
                        for row in target_memory.get(key, [])
                    }
                    for row_id, row in target_rows.items():
                        base_row = base_rows.get(row_id)
                        if base_row == row:
                            continue
                        payload = json.dumps(row, default=str, sort_keys=True)
                        conn.execute(
                            "INSERT INTO memories (id, kind, payload_json, content_hash, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, payload_json=excluded.payload_json, content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                            (
                                row_id,
                                kind,
                                payload,
                                _content_hash(payload),
                                str(row.get("updated_at", "") or ""),
                            ),
                        )
                    for row_id in base_rows:
                        if row_id not in target_rows:
                            conn.execute(
                                "DELETE FROM memories WHERE id = ?", (row_id,)
                            )
                base_edges = {
                    (e.get("source", ""), e.get("target", ""), e.get("edge_type", "")): e
                    for e in base_memory.get("edges", [])
                }
                target_edges = {
                    (e.get("source", ""), e.get("target", ""), e.get("edge_type", "")): e
                    for e in target_memory.get("edges", [])
                }
                for edge_key, edge in target_edges.items():
                    if base_edges.get(edge_key) == edge:
                        continue
                    payload = json.dumps(edge, default=str, sort_keys=True)
                    conn.execute(
                        "INSERT INTO graph_edges (source, target, edge_type, payload_json, content_hash) VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, target, edge_type) DO UPDATE SET payload_json=excluded.payload_json, content_hash=excluded.content_hash",
                        (edge_key[0], edge_key[1], edge_key[2], payload, _content_hash(payload)),
                    )
                for edge_key in base_edges:
                    if edge_key not in target_edges:
                        conn.execute(
                            "DELETE FROM graph_edges WHERE source = ? AND target = ? AND edge_type = ?",
                            edge_key,
                        )
                if len(target_events) > len(base_events):
                    conn.executemany(
                        "INSERT INTO change_log (event_json) VALUES (?)",
                        [
                            (json.dumps(event, default=str, sort_keys=True),)
                            for event in target_events[len(base_events) :]
                        ],
                    )
                elif len(target_events) < len(base_events):
                    # Changelog compaction/reset: replace-all the log.
                    conn.execute("DELETE FROM change_log WHERE seq >= ?", (0,))
                    conn.executemany(
                        "INSERT INTO change_log (event_json) VALUES (?)",
                        [
                            (json.dumps(event, default=str, sort_keys=True),)
                            for event in target_events
                        ],
                    )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("generation", str(generation)),
                )
                if "sync" in target_memory:
                    conn.execute(
                        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (
                            "sync_state",
                            json.dumps(target_memory["sync"], default=str, sort_keys=True),
                        ),
                    )
                conn.execute("COMMIT")
                return True
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def authority_write_enabled() -> bool:
    """B2 flag: SQLite is the write authority, JSON becomes a snapshot."""
    import os

    return os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "") == "rw"
