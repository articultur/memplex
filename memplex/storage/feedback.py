"""FeedbackStore -- three-tier feedback persistence.

Tiers:
    Lite     -- in-memory dict + JSON file
    SQLite   -- SQLite database (connection lazily created)
    Postgres -- asyncpg async PostgreSQL backend

Usage::

    store = create_feedback_store("lite")
    store.record(MemoryFeedback(...))
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Protocol, runtime_checkable

from memplex.models import (
    FeedbackVerdict,
    MemoryFeedback,
    PendingReview,
)

logger = logging.getLogger(__name__)


# ── Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class FeedbackStore(Protocol):
    """Feedback persistence interface."""

    def record(self, feedback: MemoryFeedback) -> None: ...

    def get_pending(self) -> List[PendingReview]: ...

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None: ...

    def get_history(self, memory_id: str, limit: int = 50) -> List[MemoryFeedback]: ...

    def clear(self) -> None: ...


# ── Serialization helpers ───────────────────────────────────────────


def _serialize_feedback(fb: MemoryFeedback) -> dict:
    ts = fb.timestamp
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    reviewed = fb.needs_review_until
    if isinstance(reviewed, datetime):
        reviewed = reviewed.isoformat()
    resolved = fb.resolved_at
    if isinstance(resolved, datetime):
        resolved = resolved.isoformat()
    return {
        "memory_id": fb.memory_id,
        "field_role": fb.field_role,
        "value_index": fb.value_index,
        "verdict": fb.verdict.value if isinstance(fb.verdict, FeedbackVerdict) else fb.verdict,
        "reason": fb.reason,
        "source": fb.source,
        "timestamp": ts,
        "owner": fb.owner,
        "feedback_type": fb.feedback_type,
        "old_value": fb.old_value,
        "new_value": fb.new_value,
        "needs_review": fb.needs_review,
        "needs_review_until": reviewed,
        "resolved_at": resolved,
        "resolution": fb.resolution,
    }


def _deserialize_feedback(d: dict) -> MemoryFeedback:
    verdict = d.get("verdict", "correct")
    if isinstance(verdict, str):
        try:
            verdict = FeedbackVerdict(verdict)
        except ValueError:
            verdict = FeedbackVerdict.CORRECT

    ts = d.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    elif ts is None:
        ts = datetime.now()

    reviewed = d.get("needs_review_until")
    if isinstance(reviewed, str):
        reviewed = datetime.fromisoformat(reviewed)

    resolved = d.get("resolved_at")
    if isinstance(resolved, str):
        resolved = datetime.fromisoformat(resolved)

    return MemoryFeedback(
        memory_id=d["memory_id"],
        field_role=d.get("field_role", ""),
        value_index=d.get("value_index", 0),
        verdict=verdict,
        reason=d.get("reason"),
        source=d.get("source", "user"),
        timestamp=ts,
        owner=d.get("owner"),
        feedback_type=d.get("feedback_type", "field_value"),
        old_value=d.get("old_value"),
        new_value=d.get("new_value"),
        needs_review=d.get("needs_review", True),
        needs_review_until=reviewed,
        resolved_at=resolved,
        resolution=d.get("resolution"),
    )


# ── LiteFeedbackStore ────────────────────────────────────────────────


class LiteFeedbackStore:
    """In-memory dict + JSON persistence."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or Path("~/.memplex/feedback.json").expanduser()
        self._records: List[MemoryFeedback] = []
        self._load()

    def record(self, feedback: MemoryFeedback) -> None:
        self._records.append(feedback)
        self._save()

    def get_pending(self) -> List[PendingReview]:
        groups: Dict[str, List[MemoryFeedback]] = {}
        for fb in self._records:
            if not fb.needs_review or fb.resolved_at is not None:
                continue
            key = f"{fb.memory_id}:{fb.field_role}"
            groups.setdefault(key, []).append(fb)

        pending: List[PendingReview] = []
        for key, fbs in groups.items():
            mem_id, role = key.split(":", 1)
            pending.append(
                PendingReview(
                    memory_id=mem_id,
                    field_role=role,
                    conflicting_values=[],  # Populated by caller with actual FieldValues
                    detected_at=fbs[0].timestamp if fbs else None,
                    source=fbs[0].source if fbs else "",
                )
            )
        return pending

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        for fb in self._records:
            if (
                fb.memory_id == memory_id
                and fb.field_role == field_role
                and fb.needs_review
                and fb.resolved_at is None
            ):
                fb.needs_review = False
                fb.resolved_at = datetime.now()
                fb.resolution = resolution
        self._save()

    def get_history(self, memory_id: str, limit: int = 50) -> List[MemoryFeedback]:
        matching = [fb for fb in self._records if fb.memory_id == memory_id]
        matching.sort(
            key=lambda fb: fb.timestamp if isinstance(fb.timestamp, datetime) else datetime.min,
            reverse=True,
        )
        return matching[:limit]

    def clear(self) -> None:
        self._records.clear()
        self._save()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = [_deserialize_feedback(d) for d in raw]
        except Exception:
            logger.warning("Failed to load feedback from %s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [_serialize_feedback(fb) for fb in self._records]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(self._path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise


# ── SQLiteFeedbackStore ──────────────────────────────────────────────


class SQLiteFeedbackStore:
    """SQLite-backed feedback store.  Connection is created lazily."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path or str(Path("~/.memplex/feedback.db").expanduser())
        self._conn = None

    def _ensure_conn(self):
        if self._conn is not None:
            return
        import sqlite3

        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                memory_id   TEXT NOT NULL,
                field_role  TEXT NOT NULL,
                value_index INTEGER DEFAULT 0,
                verdict     TEXT NOT NULL,
                reason      TEXT,
                source      TEXT DEFAULT 'user',
                timestamp   TEXT,
                owner       TEXT,
                feedback_type TEXT DEFAULT 'field_value',
                old_value   TEXT,
                new_value   TEXT,
                needs_review INTEGER DEFAULT 1,
                needs_review_until TEXT,
                resolved_at TEXT,
                resolution  TEXT
            )
        """)
        self._conn.commit()

    def record(self, feedback: MemoryFeedback) -> None:
        self._ensure_conn()
        self._conn.execute(
            "INSERT INTO feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                feedback.memory_id,
                feedback.field_role,
                feedback.value_index,
                feedback.verdict.value
                if isinstance(feedback.verdict, FeedbackVerdict)
                else feedback.verdict,
                feedback.reason,
                feedback.source,
                feedback.timestamp.isoformat()
                if isinstance(feedback.timestamp, datetime)
                else feedback.timestamp,
                feedback.owner,
                feedback.feedback_type,
                feedback.old_value,
                feedback.new_value,
                1 if feedback.needs_review else 0,
                feedback.needs_review_until.isoformat()
                if isinstance(feedback.needs_review_until, datetime)
                else feedback.needs_review_until,
                feedback.resolved_at.isoformat()
                if isinstance(feedback.resolved_at, datetime)
                else feedback.resolved_at,
                feedback.resolution,
            ),
        )
        self._conn.commit()

    def get_pending(self) -> List[PendingReview]:
        self._ensure_conn()
        rows = self._conn.execute(
            "SELECT DISTINCT memory_id, field_role, source, MIN(timestamp) "
            "FROM feedback WHERE needs_review=1 AND resolved_at IS NULL "
            "GROUP BY memory_id, field_role"
        ).fetchall()
        return [
            PendingReview(
                memory_id=r[0],
                field_role=r[1],
                detected_at=r[3],
                source=r[2] or "",
            )
            for r in rows
        ]

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        self._ensure_conn()
        now = datetime.now().isoformat()
        self._conn.execute(
            "UPDATE feedback SET needs_review=0, resolved_at=?, resolution=? "
            "WHERE memory_id=? AND field_role=? AND needs_review=1 AND resolved_at IS NULL",
            (now, resolution, memory_id, field_role),
        )
        self._conn.commit()

    def get_history(self, memory_id: str, limit: int = 50) -> List[MemoryFeedback]:
        self._ensure_conn()
        rows = self._conn.execute(
            "SELECT memory_id, field_role, value_index, verdict, reason, source, "
            "timestamp, owner, feedback_type, old_value, new_value, "
            "needs_review, needs_review_until, resolved_at, resolution "
            "FROM feedback WHERE memory_id=? ORDER BY timestamp DESC LIMIT ?",
            (memory_id, limit),
        ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def clear(self) -> None:
        self._ensure_conn()
        self._conn.execute("DELETE FROM feedback")
        self._conn.commit()

    @staticmethod
    def _row_to_feedback(r: tuple) -> MemoryFeedback:
        return MemoryFeedback(
            memory_id=r[0],
            field_role=r[1],
            value_index=r[2],
            verdict=FeedbackVerdict(r[3]),
            reason=r[4],
            source=r[5] or "user",
            timestamp=datetime.fromisoformat(r[6]) if r[6] else datetime.now(),
            owner=r[7],
            feedback_type=r[8] or "field_value",
            old_value=r[9],
            new_value=r[10],
            needs_review=bool(r[11]),
            needs_review_until=datetime.fromisoformat(r[12]) if r[12] else None,
            resolved_at=datetime.fromisoformat(r[13]) if r[13] else None,
            resolution=r[14],
        )


# ── PostgresFeedbackStore ────────────────────────────────────────────


class PostgresFeedbackStore:
    """Async PostgreSQL-backed feedback store via asyncpg.

    The pool is created lazily on first use.
    """

    def __init__(self, dsn: str = "", **pool_kwargs) -> None:
        self._dsn = dsn
        self._pool_kwargs = pool_kwargs
        self._pool = None

    async def _ensure_pool(self):
        if self._pool is not None:
            return
        import asyncpg  # type: ignore

        self._pool = await asyncpg.create_pool(self._dsn, **self._pool_kwargs)
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS feedback (
                    memory_id   TEXT NOT NULL,
                    field_role  TEXT NOT NULL,
                    value_index INTEGER DEFAULT 0,
                    verdict     TEXT NOT NULL,
                    reason      TEXT,
                    source      TEXT DEFAULT 'user',
                    timestamp   TIMESTAMPTZ,
                    owner       TEXT,
                    feedback_type TEXT DEFAULT 'field_value',
                    old_value   TEXT,
                    new_value   TEXT,
                    needs_review BOOLEAN DEFAULT TRUE,
                    needs_review_until TIMESTAMPTZ,
                    resolved_at TIMESTAMPTZ,
                    resolution  TEXT
                )
            """)

    async def record(self, feedback: MemoryFeedback) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO feedback VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
                feedback.memory_id,
                feedback.field_role,
                feedback.value_index,
                feedback.verdict.value
                if isinstance(feedback.verdict, FeedbackVerdict)
                else feedback.verdict,
                feedback.reason,
                feedback.source,
                feedback.timestamp,
                feedback.owner,
                feedback.feedback_type,
                feedback.old_value,
                feedback.new_value,
                feedback.needs_review,
                feedback.needs_review_until,
                feedback.resolved_at,
                feedback.resolution,
            )

    async def get_pending(self) -> List[PendingReview]:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT memory_id, field_role, source, MIN(timestamp) as detected_at "
                "FROM feedback WHERE needs_review=TRUE AND resolved_at IS NULL "
                "GROUP BY memory_id, field_role, source"
            )
            return [
                PendingReview(
                    memory_id=r["memory_id"],
                    field_role=r["field_role"],
                    detected_at=r["detected_at"],
                    source=r["source"] or "",
                )
                for r in rows
            ]

    async def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE feedback SET needs_review=FALSE, resolved_at=now(), resolution=$1 "
                "WHERE memory_id=$2 AND field_role=$3 AND needs_review=TRUE AND resolved_at IS NULL",
                resolution,
                memory_id,
                field_role,
            )

    async def get_history(self, memory_id: str, limit: int = 50) -> List[MemoryFeedback]:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM feedback WHERE memory_id=$1 ORDER BY timestamp DESC LIMIT $2",
                memory_id,
                limit,
            )
            return [self._row_to_feedback(r) for r in rows]

    async def clear(self) -> None:
        await self._ensure_pool()
        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM feedback")

    @staticmethod
    def _row_to_feedback(r) -> MemoryFeedback:
        return MemoryFeedback(
            memory_id=r["memory_id"],
            field_role=r["field_role"],
            value_index=r["value_index"],
            verdict=FeedbackVerdict(r["verdict"]),
            reason=r["reason"],
            source=r["source"] or "user",
            timestamp=r["timestamp"] or datetime.now(),
            owner=r["owner"],
            feedback_type=r["feedback_type"] or "field_value",
            old_value=r["old_value"],
            new_value=r["new_value"],
            needs_review=r["needs_review"],
            needs_review_until=r["needs_review_until"],
            resolved_at=r["resolved_at"],
            resolution=r["resolution"],
        )


# ── Factory ──────────────────────────────────────────────────────────


def create_feedback_store(
    backend: str = "lite",
    **kwargs,
):
    """Create a feedback store by backend name.

    Parameters
    ----------
    backend:
        ``"lite"`` | ``"sqlite"`` | ``"postgres"``
    """
    if backend == "lite":
        return LiteFeedbackStore(path=kwargs.get("path"))
    if backend == "sqlite":
        return SQLiteFeedbackStore(db_path=kwargs.get("db_path"))
    if backend == "postgres":
        return PostgresFeedbackStore(
            dsn=kwargs.get("dsn", ""),
            **{k: v for k, v in kwargs.items() if k not in ("dsn", "path", "db_path")},
        )
    raise ValueError(f"Unknown feedback store backend: {backend!r}")
