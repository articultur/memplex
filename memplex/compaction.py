"""CompactionPipeline -- 5-stage memory compression pipeline.

Stages::

    1. Extract   -- extract atomic facts from history
    2. Dedup     -- remove exact + semantic duplicates
    3. Summarize -- generate summaries, trim oversized FieldValue lists
    4. Prune     -- remove stale, low-confidence, deprecated entries
    5. Archive   -- move low-frequency memories to cold storage

Concurrency safety::

    Compaction runs under a mutually-exclusive FileLock (POSIX
    ``fcntl.flock``).  If the lock is already held, ``run()`` returns
    immediately with ``skipped=True``.

Usage::

    pipeline = CompactionPipeline(store, embedding_service, config)
    result = await pipeline.run(CompactionScope.GLOBAL)
"""

from __future__ import annotations

import abc
import hashlib
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timezone


def _ensure_aware(dt: datetime) -> datetime:
    """Normalize a datetime to offset-aware UTC for safe arithmetic."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from memplex.config import MemplexConfig
from memplex.models import (
    CompactionResult,
    CompactionScope,
    CompactionStageResult,
    FieldValue,
    Function,
    Memory,
)
from memplex.retrieval.dedup import MemoryDeduplicator
from memplex.retrieval.embedding import EmbeddingService
from memplex.storage.base import MemoryStore
from memplex.storage.lite.durability import _load_fcntl

logger = logging.getLogger(__name__)


def _fsync_directory(directory: Path) -> None:
    """Persist directory entries such as archive creation and rename."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_directory_tree_durable(directory: Path) -> None:
    """Create missing ancestors one at a time and fsync each parent entry."""
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for child in reversed(missing):
        child.mkdir(exist_ok=True)
        # A directory fsync covers the newly created child entry in its parent.
        _fsync_directory(child.parent)


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
    """POSIX ``fcntl.flock``-based file lock.

    Lock file: ``lock_dir / {key_sha1}.lock``.
    Suitable for single-machine multi-process scenarios.
    """

    def __init__(self, key: str, lock_dir: Path) -> None:
        key_hash = hashlib.sha1(key.encode()).hexdigest()[:16]
        self._lock_path = lock_dir / f"{key_hash}.lock"
        self._lock_dir = lock_dir
        self._fd: int | None = None

    async def try_acquire(self) -> bool:
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR)
        fcntl = _load_fcntl()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._fd = fd
            return True
        except BlockingIOError:
            os.close(fd)
            return False

    async def release(self) -> None:
        if self._fd is not None:
            fcntl = _load_fcntl()
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


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

    STAGES: ClassVar[list[str]] = ["extract", "dedup", "summarize", "prune", "archive"]

    def __init__(
        self,
        store: MemoryStore,
        embedding_service: EmbeddingService,
        config: MemplexConfig,
    ) -> None:
        self._store = store
        self._embedding = embedding_service
        self._config = config

    # ── Lock helpers ────────────────────────────────────────────────

    @staticmethod
    def _lock_key(scope: CompactionScope) -> str:
        return f"compaction:{scope.value}"

    def _build_lock(self, scope: CompactionScope) -> CompactionLock:
        key = self._lock_key(scope)
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
        stage_results: list[CompactionStageResult] = []
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
        _generation, functions = self._function_snapshot()
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
        """Dedup stage: remove exact and semantic duplicates.

        Memories dropped by :meth:`MemoryDeduplicator.deduplicate` are
        actually deleted from the store, and merged field-values are
        written back onto the surviving memory, so the reported
        removed/merged counts reflect real state changes.
        """
        t0 = time.monotonic()
        expected_generation, functions = self._function_snapshot()
        memories: list[Memory] = list(functions)

        threshold = self._config.compaction.dedup_threshold
        chunk_threshold = self._config.compaction.chunk_threshold
        deduplicator = MemoryDeduplicator(
            self._embedding,
            threshold=threshold,
            chunk_threshold=chunk_threshold,
            use_faiss=self._config.compaction.dedup_use_faiss,
        )
        result = deduplicator.deduplicate(memories)

        kept_by_id = {m.id: m for m in result.deduplicated}
        orig_by_id = {m.id: m for m in memories}

        delete_ids = [m.id for m in memories if m.id not in kept_by_id]
        removed = len(delete_ids)

        # Write merged content back onto surviving live objects.
        touched = False
        for kept in result.deduplicated:
            original = orig_by_id.get(kept.id)
            if original is not None and kept is not original:
                self._sync_merged_fields(original, kept)
                touched = True
        if self._apply_compaction(list(result.deduplicated), delete_ids, expected_generation):
            # One durable pair transaction: no survivor is published without
            # all losers being deleted.
            pass
        else:
            for identifier in delete_ids:
                self._store.delete(identifier)
            if touched:
                self._replace_functions(list(result.deduplicated))

        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="dedup",
            processed=result.original_count,
            removed=removed,
            merged=result.semantic_removed,
            duration_ms=elapsed,
        )

    @staticmethod
    def _sync_merged_fields(target: Memory, merged: Memory) -> None:
        """Copy merged field-values from *merged* onto the live *target*."""
        for role in ("trigger", "condition", "action", "benefit"):
            if hasattr(merged, role):
                setattr(target, role, list(getattr(merged, role)))
        target.source_paragraphs = list(getattr(merged, "source_paragraphs", []))
        if getattr(merged, "updated_at", None):
            target.updated_at = merged.updated_at

    def _replace_functions(self, functions: list[Memory]) -> None:
        """Use an explicit persistence API; never reflect into `_save`."""
        replace = getattr(self._store, "replace_function", None)
        if callable(replace):
            for function in functions:
                replace(function)
            return
        from memplex.models import SourceDocument, SourceType

        for function in functions:
            # Compaction only ever processes Function nodes (list_functions).
            self._store.add(
                cast(Function, function),
                SourceDocument(type="compaction", source_type=SourceType.WIKI),
            )

    def _function_snapshot(self) -> tuple[int | None, list[Memory]]:
        """Bind compaction input to one store generation when supported."""
        snapshot = getattr(self._store, "compaction_snapshot", None)
        if callable(snapshot):
            generation, functions = snapshot()
            return generation, list(functions)
        return None, list(self._store.list_functions(limit=100000))

    def _apply_compaction(
        self, replacements: list[Memory], delete_ids: list[str], expected_generation: int | None
    ) -> bool:
        apply = getattr(self._store, "apply_compaction", None)
        if not callable(apply):
            return False
        if expected_generation is None:
            apply(replacements=replacements, delete_ids=delete_ids)
        else:
            apply(
                replacements=replacements,
                delete_ids=delete_ids,
                expected_generation=expected_generation,
            )
        return True

    # ── Stage: Summarize ────────────────────────────────────────────

    async def _execute_summarize(self, scope: CompactionScope) -> CompactionStageResult:
        """Summarize stage: generate summaries and trim oversized FieldValue lists.

        When a role field exceeds ``max_values_per_field`` (default 20):
        - Sort by ``weight * observation`` descending
        - Mark low-score entries as ``status="deprecated"`` for later Prune
        """
        t0 = time.monotonic()
        expected_generation, functions = self._function_snapshot()
        max_values = self._config.compaction.field_max_values
        processed = 0
        trimmed = 0

        for func in functions:
            trimmed_this = False
            for role in ("trigger", "condition", "action", "benefit"):
                values: list[FieldValue] = getattr(func, role, [])
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

        if trimmed:
            if self._apply_compaction(functions, [], expected_generation):
                pass
            else:
                self._replace_functions(functions)

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
        expected_generation, functions = self._function_snapshot()
        conf_thresh = self._config.compaction.prune_confidence_threshold
        max_age_days = self._config.compaction.prune_max_age_days
        min_access = self._config.compaction.prune_min_access_count
        review_ttl = self._config.compaction.needs_review_ttl_days

        removed = 0
        delete_ids: list[str] = []
        fields_trimmed = 0
        processed = len(functions)
        now = datetime.now(UTC)

        for func in functions:
            should_delete = False

            # Low confidence
            if func.confidence < conf_thresh:
                should_delete = True

            # Stale and rarely accessed
            if not should_delete:
                updated: str | datetime | None = func.updated_at
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
                review_until: str | datetime | None = func.needs_review_until
                if isinstance(review_until, str):
                    try:
                        review_until = datetime.fromisoformat(review_until)
                    except (ValueError, TypeError):
                        review_until = None
                if review_until is not None and now > _ensure_aware(review_until):
                    should_delete = True
                elif review_until is None:
                    # No expiry set -- use TTL from creation
                    created: str | datetime | None = func.created_at
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
                    values: list[FieldValue] = getattr(func, role, [])
                    before = len(values)
                    kept = [fv for fv in values if fv.status != "deprecated"]
                    if len(kept) < before:
                        setattr(func, role, kept)
                        removed += before - len(kept)
                        fields_trimmed += before - len(kept)

            if should_delete:
                delete_ids.append(func.id)
                removed += 1

        # Whole-Function deletes persist via store.delete(); in-place
        # FieldValue trimming needs an explicit flush on JSON backends.
        if delete_ids or fields_trimmed:
            if self._apply_compaction(
                [function for function in functions if function.id not in delete_ids],
                delete_ids,
                expected_generation,
            ):
                pass
            else:
                for identifier in delete_ids:
                    self._store.delete(identifier)
                if fields_trimmed:
                    self._replace_functions(functions)

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
        threshold are serialised (full Function body) to JSON files and
        then soft-deleted.
        """
        t0 = time.monotonic()
        archive_dir = Path.home() / ".memplex" / "archive"
        try:
            _ensure_directory_tree_durable(archive_dir)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning("Failed to prepare durable archive directory: %s", exc)
            return CompactionStageResult(
                stage="archive", processed=0, removed=0, merged=0,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        expected_generation, functions = self._function_snapshot()
        max_age_days = self._config.compaction.prune_max_age_days
        now = datetime.now(UTC)
        archived = 0
        delete_ids: list[str] = []

        for func in functions:
            # Only archive very old, very rarely accessed memories
            updated: str | datetime | None = func.updated_at
            if isinstance(updated, str):
                try:
                    updated = datetime.fromisoformat(updated)
                except (ValueError, TypeError):
                    updated = None
            if updated is None:
                continue

            age_days = (now - _ensure_aware(updated)).days
            if age_days > max_age_days and func.access_count == 0:
                # Write the FULL Function body (all role fields, attributes,
                # source paragraphs, ...) so the archive is restorable.
                archive_file = archive_dir / f"{func.id}.json"
                tmp = archive_file.with_name(f".{archive_file.name}.unwritten")
                try:
                    import json
                    payload = func.to_dict()
                    payload["archived_at"] = now.isoformat()
                    payload["original_updated_at"] = str(updated)
                    tmp = archive_file.with_name(f".{archive_file.name}.{uuid.uuid4().hex}.tmp")
                    with open(tmp, "w", encoding="utf-8") as fh:  # noqa: ASYNC230 - runs on the worker thread, not the event loop
                        json.dump(
                            payload,
                            fh,
                            ensure_ascii=False,
                            allow_nan=False,
                            indent=2,
                        )
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, archive_file)
                    _fsync_directory(archive_dir)
                    delete_ids.append(func.id)
                    archived += 1
                except Exception as exc:  # noqa: BLE001 - logged degradation path
                    tmp.unlink(missing_ok=True)
                    logger.warning("Failed to archive %s: %s", func.id, exc)

        if delete_ids:
            if self._apply_compaction([], delete_ids, expected_generation):
                pass
            else:
                for identifier in delete_ids:
                    self._store.delete(identifier)
        elapsed = int((time.monotonic() - t0) * 1000)
        return CompactionStageResult(
            stage="archive",
            processed=len(functions),
            removed=archived,
            merged=0,
            duration_ms=elapsed,
        )
