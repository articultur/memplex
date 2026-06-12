"""CompactionPipeline -- 5-stage memory compression pipeline.

Stages::

    1. Extract   -- extract atomic facts from history
    2. Dedup     -- remove exact + semantic duplicates
    3. Summarize -- generate summaries, trim oversized FieldValue lists
    4. Prune     -- remove stale, low-confidence, deprecated entries
    5. Archive   -- move low-frequency memories to cold storage

Concurrency safety::

    Compaction runs under a mutually-exclusive lock (FileLock for
    Lite/Standard, PGAdvisoryLock for Enterprise).  If the lock is
    already held, ``run()`` returns immediately with ``skipped=True``.

Usage::

    pipeline = CompactionPipeline(store, embedding_service, config)
    result = await pipeline.run(CompactionScope.GLOBAL)
"""

from __future__ import annotations

import abc
import fcntl
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone


def _ensure_aware(dt: datetime) -> datetime:
    """Normalize a datetime to offset-aware UTC for safe arithmetic."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from memplex.config import MemplexConfig
from memplex.models import (
    CompactionResult,
    CompactionScope,
    CompactionStageResult,
    FieldValue,
    Memory,
)
from memplex.retrieval.dedup import MemoryDeduplicator
from memplex.retrieval.embedding import EmbeddingService
from memplex.storage.base import MemoryStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Compaction lock abstraction ────────────────────────────────────────


class CompactionLock(abc.ABC):
    """Abstract base class for compaction mutual-exclusion locks.

    ``try_acquire`` is non-blocking: returns ``False`` immediately when
    the lock is already held.
    """

    @abc.abstractmethod
    async def try_acquire(self) -> bool:
        """Attempt to acquire the lock.  Return ``True`` on success."""

    @abc.abstractmethod
    async def release(self) -> None:
        """Release the lock.  No-op when not held."""


class FileLock(CompactionLock):
    """POSIX ``fcntl.flock``-based file lock (Lite / Standard backends).

    Lock file: ``lock_dir / {key_sha1}.lock``.
    Suitable for single-machine multi-process scenarios.
    """

    def __init__(self, key: str, lock_dir: Path) -> None:
        key_hash = hashlib.sha1(key.encode()).hexdigest()[:16]
        self._lock_path = lock_dir / f"{key_hash}.lock"
        self._lock_dir = lock_dir
        self._fd: Optional[int] = None

    async def try_acquire(self) -> bool:
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
            return True
        except BlockingIOError:
            os.close(fd)
            return False

    async def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class PGAdvisoryLock(CompactionLock):
    """PostgreSQL ``pg_try_advisory_lock`` (Enterprise backend).

    The lock ID is derived by hashing *key* to a positive int64.
    Advisory locks are released when the connection is returned to the
    pool, so process crashes automatically clear them.
    """

    def __init__(self, key: str, pool: object) -> None:
        self._pool = pool
        raw = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        self._lock_id: int = raw % (2**63)
        self._conn = None

    async def try_acquire(self) -> bool:
        conn = await self._pool.acquire()
        result = await conn.fetchval("SELECT pg_try_advisory_lock($1)", self._lock_id)
        if result:
            self._conn = conn
            return True
        await self._pool.release(conn)
        return False

    async def release(self) -> None:
        if self._conn is not None:
            await self._conn.fetchval("SELECT pg_advisory_unlock($1)", self._lock_id)
            await self._pool.release(self._conn)
            self._conn = None


# ── Checkpoint ────────────────────────────────────────────────────────


@dataclass
class _Checkpoint:
    """Checkpoint written after each stage for crash-recovery."""

    stage_name: str
    processed_offset: int
    processed_ids: List[str]
    timestamp: str


# ── CompactionPipeline ────────────────────────────────────────────────


class CompactionPipeline:
    """5-stage compaction pipeline.

    Parameters
    ----------
    store:
        The active :class:`MemoryStore` backend.
    embedding_service:
        For semantic dedup and summarisation.
    config:
        Full :class:`MemplexConfig` (read compaction sub-config).
    """

    STAGES = ["extract", "dedup", "summarize", "prune", "archive"]

    def __init__(
        self,
        store: MemoryStore,
        embedding_service: EmbeddingService,
        config: MemplexConfig,
    ) -> None:
        self._store = store
        self._embedding = embedding_service
        self._config = config
        self._pg_pool: Optional[object] = None  # injected for Enterprise

    # ── Lock helpers ────────────────────────────────────────────────

    @staticmethod
    def _lock_key(scope: CompactionScope) -> str:
        return f"compaction:{scope.value}"

    def _build_lock(self, scope: CompactionScope) -> CompactionLock:
        key = self._lock_key(scope)
        backend = getattr(self._config, "storage", None)
        backend_name = getattr(backend, "backend", "lite") if backend else "lite"
        if backend_name == "enterprise" and self._pg_pool is not None:
            return PGAdvisoryLock(key=key, pool=self._pg_pool)
        lock_dir = Path.home() / ".memplex" / "locks"
        return FileLock(key=key, lock_dir=lock_dir)

    # ── Public API ──────────────────────────────────────────────────

    async def run(self, scope: CompactionScope) -> CompactionResult:
        """Execute the compaction pipeline with mutual-exclusion.

        Returns ``CompactionResult(skipped=True)`` immediately when the
        lock cannot be acquired.
        """
        lock = self._build_lock(scope)
        acquired = await lock.try_acquire()
        if not acquired:
            logger.warning(
                "Compaction skipped: another instance holds the lock for scope=%s",
                scope,
            )
            return CompactionResult(
                total_processed=0,
                total_removed=0,
                total_merged=0,
                duration_ms=0,
                stages=[],
                skipped=True,
            )
        try:
            return await self._run_pipeline(scope)
        finally:
            await lock.release()

    # ── Pipeline execution ──────────────────────────────────────────

    async def _run_pipeline(self, scope: CompactionScope) -> CompactionResult:
        """Execute each stage sequentially."""
        start_time = time.monotonic()
        stage_results: List[CompactionStageResult] = []
        total_processed = 0
        total_removed = 0
        total_merged = 0

        for stage in self.STAGES:
            result = await self._execute_stage(stage, scope)
            stage_results.append(result)
            total_processed += result.processed
            total_removed += result.removed
            total_merged += result.merged

            if result.abort:
                logger.warning("Compaction aborted at stage %s", stage)
                break

            # Write checkpoint after each completed stage
            self._write_checkpoint(stage, result)

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return CompactionResult(
            total_processed=total_processed,
            total_removed=total_removed,
            total_merged=total_merged,
            duration_ms=elapsed_ms,
            stages=stage_results,
            skipped=False,
        )

    async def _execute_stage(self, stage: str, scope: CompactionScope) -> CompactionStageResult:
        """Dispatch to the correct stage handler."""
        handlers = {
            "extract": self._execute_extract,
            "dedup": self._execute_dedup,
            "summarize": self._execute_summarize,
            "prune": self._execute_prune,
            "archive": self._execute_archive,
        }
        handler = handlers.get(stage)
        if handler is None:
            return CompactionStageResult(
                stage=stage, processed=0, removed=0, merged=0, duration_ms=0
            )
        return await handler(scope)

    # ── Stage: Extract ──────────────────────────────────────────────

    async def _execute_extract(self, scope: CompactionScope) -> CompactionStageResult:
        """Extract atomic facts from stored memories.

        In the current implementation this is a no-op pass-through that
        counts the total number of functions.  Full extraction logic is
        wired by the application layer via LLM providers.
        """
        t0 = time.monotonic()
        functions = self._store.list_functions(limit=100000)
        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="extract",
            processed=len(functions),
            removed=0,
            merged=0,
            duration_ms=elapsed,
        )

    # ── Stage: Dedup ────────────────────────────────────────────────

    async def _execute_dedup(self, scope: CompactionScope) -> CompactionStageResult:
        """Dedup stage: remove exact and semantic duplicates."""
        t0 = time.monotonic()
        functions = self._store.list_functions(limit=100000)
        memories: List[Memory] = list(functions)

        threshold = self._config.compaction.dedup_threshold
        deduplicator = MemoryDeduplicator(self._embedding, threshold=threshold)
        result = deduplicator.deduplicate(memories)
        elapsed = int((time.monotonic() - t0) * 1000)

        return CompactionStageResult(
            stage="dedup",
            processed=result.original_count,
            removed=result.exact_removed + result.semantic_removed,
            merged=result.semantic_removed,
            duration_ms=elapsed,
        )

    # ── Stage: Summarize ────────────────────────────────────────────

    async def _execute_summarize(self, scope: CompactionScope) -> CompactionStageResult:
        """Summarize stage: generate summaries and trim oversized FieldValue lists.

        When a role field exceeds ``max_values_per_field`` (default 20):
        - Sort by ``weight * observation`` descending
        - Mark low-score entries as ``status="deprecated"`` for later Prune
        """
        t0 = time.monotonic()
        functions = self._store.list_functions(limit=100000)
        max_values = self._config.compaction.field_max_values
        processed = 0
        trimmed = 0

        for func in functions:
            trimmed_this = False
            for role in ("trigger", "condition", "action", "benefit"):
                values: List[FieldValue] = getattr(func, role, [])
                if len(values) <= max_values:
                    continue

                # Sort by weight * observation composite score
                def _score(fv: FieldValue) -> float:
                    return fv.weight * (fv.observation if fv.observation is not None else 1.0)

                values.sort(key=_score, reverse=True)
                for fv in values[max_values:]:
                    if fv.status != "deprecated":
                        fv.status = "deprecated"
                        trimmed += 1
                        trimmed_this = True
            if trimmed_this:
                processed += 1

        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="summarize",
            processed=processed,
            removed=0,
            merged=0,
            duration_ms=elapsed,
        )

    # ── Stage: Prune ────────────────────────────────────────────────

    async def _execute_prune(self, scope: CompactionScope) -> CompactionStageResult:
        """Prune stage: clean low-confidence, stale, and deprecated entries.

        Removal criteria (all thresholds from config):
        - ``confidence < prune_confidence_threshold``
        - Age > ``prune_max_age_days`` AND ``access_count < prune_min_access_count``
        - ``needs_review=True`` AND ``needs_review_until`` has expired
        - FieldValue entries with ``status="deprecated"``
        """
        t0 = time.monotonic()
        functions = self._store.list_functions(limit=100000)
        conf_thresh = self._config.compaction.prune_confidence_threshold
        max_age_days = self._config.compaction.prune_max_age_days
        min_access = self._config.compaction.prune_min_access_count
        review_ttl = self._config.compaction.needs_review_ttl_days

        removed = 0
        processed = len(functions)
        now = datetime.now(timezone.utc)

        for func in functions:
            should_delete = False

            # Low confidence
            if func.confidence < conf_thresh:
                should_delete = True

            # Stale and rarely accessed
            if not should_delete:
                updated = func.updated_at
                if isinstance(updated, str):
                    try:
                        updated = datetime.fromisoformat(updated)
                    except (ValueError, TypeError):
                        updated = None
                if updated is not None:
                    age_days = (now - _ensure_aware(updated)).days
                    if age_days > max_age_days and func.access_count < min_access:
                        should_delete = True

            # Expired needs_review
            if not should_delete and func.needs_review:
                review_until = func.needs_review_until
                if isinstance(review_until, str):
                    try:
                        review_until = datetime.fromisoformat(review_until)
                    except (ValueError, TypeError):
                        review_until = None
                if review_until is not None and now > _ensure_aware(review_until):
                    should_delete = True
                elif review_until is None:
                    # No expiry set -- use TTL from creation
                    created = func.created_at
                    if isinstance(created, str):
                        try:
                            created = datetime.fromisoformat(created)
                        except (ValueError, TypeError):
                            created = None
                    if created is not None and (now - _ensure_aware(created)).days > review_ttl:
                        should_delete = True

            # Prune deprecated FieldValue entries (not the whole Function)
            if not should_delete:
                for role in ("trigger", "condition", "action", "benefit"):
                    values: List[FieldValue] = getattr(func, role, [])
                    before = len(values)
                    kept = [fv for fv in values if fv.status != "deprecated"]
                    if len(kept) < before:
                        setattr(func, role, kept)
                        removed += before - len(kept)

            if should_delete:
                self._store.delete(func.id)
                removed += 1

        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="prune",
            processed=processed,
            removed=removed,
            merged=0,
            duration_ms=elapsed,
        )

    # ── Stage: Archive ──────────────────────────────────────────────

    async def _execute_archive(self, scope: CompactionScope) -> CompactionStageResult:
        """Archive stage: move low-frequency memories to cold storage.

        Archive directory: ``~/.memplex/archive/``.
        Memories with very low access count and age beyond the max age
        threshold are serialised to JSON files and then soft-deleted.
        """
        t0 = time.monotonic()
        archive_dir = Path.home() / ".memplex" / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)

        functions = self._store.list_functions(limit=100000)
        max_age_days = self._config.compaction.prune_max_age_days
        now = datetime.now(timezone.utc)
        archived = 0

        for func in functions:
            # Only archive very old, very rarely accessed memories
            updated = func.updated_at
            if isinstance(updated, str):
                try:
                    updated = datetime.fromisoformat(updated)
                except (ValueError, TypeError):
                    updated = None
            if updated is None:
                continue

            age_days = (now - _ensure_aware(updated)).days
            if age_days > max_age_days and func.access_count == 0:
                # Write to archive
                archive_file = archive_dir / f"{func.id}.json"
                try:
                    import json

                    from memplex.worker import _json_serializer

                    with open(archive_file, "w", encoding="utf-8") as fh:
                        json.dump(
                            {
                                "id": func.id,
                                "name": func.name,
                                "domain": func.domain,
                                "archived_at": now.isoformat(),
                                "original_updated_at": str(updated),
                            },
                            fh,
                            default=_json_serializer,
                            indent=2,
                        )
                    self._store.delete(func.id)
                    archived += 1
                except Exception as exc:
                    logger.warning("Failed to archive %s: %s", func.id, exc)

        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="archive",
            processed=len(functions),
            removed=archived,
            merged=0,
            duration_ms=elapsed,
        )

    # ── Checkpoint ──────────────────────────────────────────────────

    def _write_checkpoint(self, stage: str, result: CompactionStageResult) -> None:
        """Write a checkpoint after each stage for crash-recovery."""
        checkpoint_dir = Path.home() / ".memplex" / "checkpoints"
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        cp = _Checkpoint(
            stage_name=stage,
            processed_offset=result.processed,
            processed_ids=[],
            timestamp=datetime.now().isoformat(),
        )
        cp_file = checkpoint_dir / "latest.json"
        try:
            import json

            with open(cp_file, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "stage_name": cp.stage_name,
                        "processed_offset": cp.processed_offset,
                        "processed_ids": cp.processed_ids,
                        "timestamp": cp.timestamp,
                    },
                    fh,
                    indent=2,
                )
        except OSError as exc:
            logger.warning("Failed to write checkpoint: %s", exc)
