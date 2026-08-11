"""Bounded dispatcher for durable sync deliveries.

The repository is the queue.  This module never keeps an application write in
an in-memory future list: it leases durable rows, sends one canonical batch per
target, and records ACK/failure back through the same repository boundary.
"""

from __future__ import annotations

import json
import logging
import math
import queue
import random
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from memplex.sync_protocol import (
    SyncApplyResult,
    SyncBatch,
    SyncBatchResult,
    SyncDelivery,
    SyncDrainResult,
    SyncEvent,
    SyncPage,
    SyncReceipt,
    SyncStatus,
    SyncStreamItem,
)
from memplex.sync_repository import SyncCursorExpired

_BATCH_NAMESPACE = uuid.UUID("a9d2e586-6a74-4f68-a45b-86db880a4368")
_MAX_PROTOCOL_BYTES = 4 * 1024 * 1024
_MAX_PULL_PAGES = 1000
_MAX_PAGE_SIZE = 1000
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """One bounded dispatch pass."""

    claimed: int
    delivered: int
    failed: int

    def __post_init__(self) -> None:
        for name in ("claimed", "delivered", "failed"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")


@dataclass(frozen=True, slots=True)
class PullResult:
    """One bounded pull cycle."""

    pages: int
    applied: int
    duplicate: int
    conflict: int
    cursor_advanced: int

    def __post_init__(self) -> None:
        for name in (
            "pages",
            "applied",
            "duplicate",
            "conflict",
            "cursor_advanced",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative exact int")


class SyncDispatcher:
    """Dispatch locally-originated durable deliveries to explicit peers."""

    def __init__(
        self,
        repository: Any,
        *,
        targets: Mapping[str, str],
        local_node_id: str,
        http: Any | None = None,
        headers: Mapping[str, str] | None = None,
        claim_size: int = 100,
        max_in_flight: int = 4,
        per_target_in_flight: int = 1,
        lease_seconds: int = 30,
        request_timeout: float = 10.0,
        poll_interval: float = 0.1,
        max_response_bytes: int = _MAX_PROTOCOL_BYTES,
    ) -> None:
        if type(local_node_id) is not str or not local_node_id:
            raise ValueError("local_node_id must be a non-empty exact str")
        if not isinstance(targets, Mapping):
            raise TypeError("targets must be a mapping")
        detached_targets: dict[str, str] = {}
        for target_id, url in targets.items():
            if type(target_id) is not str or not target_id:
                raise ValueError("target ids must be non-empty exact strings")
            if target_id == local_node_id:
                raise ValueError("target id must differ from local_node_id")
            if type(url) is not str or not url:
                raise ValueError("target URLs must be non-empty exact strings")
            detached_targets[target_id] = url.rstrip("/")
        if len(set(detached_targets.values())) != len(detached_targets):
            raise ValueError("one transport URL cannot identify multiple targets")
        for name, value in (
            ("claim_size", claim_size),
            ("max_in_flight", max_in_flight),
            ("per_target_in_flight", per_target_in_flight),
            ("lease_seconds", lease_seconds),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive exact int")
        if per_target_in_flight > max_in_flight:
            raise ValueError("per_target_in_flight cannot exceed max_in_flight")
        if type(max_response_bytes) is not int:
            raise TypeError("max_response_bytes must be an exact int")
        if not 1 <= max_response_bytes <= _MAX_PROTOCOL_BYTES:
            raise ValueError(
                "max_response_bytes must be between 1 and the 4MiB protocol cap"
            )
        for name, value in (
            ("request_timeout", request_timeout),
            ("poll_interval", poll_interval),
        ):
            if type(value) not in {int, float} or not math.isfinite(float(value)):
                raise TypeError(f"{name} must be a finite number")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        detached_headers: dict[str, str] = {}
        if headers is not None:
            if not isinstance(headers, Mapping):
                raise TypeError("headers must be a mapping")
            for name, value in headers.items():
                if type(name) is not str or not name or type(value) is not str:
                    raise ValueError("headers must contain exact string pairs")
                detached_headers[name] = value

        self._repository = repository
        self._targets = detached_targets
        self._local_node_id = local_node_id
        self._http = http
        self._headers = detached_headers
        self._pull_cursors: dict[str, str] = {}
        self._claim_size = claim_size
        self._max_in_flight = max_in_flight
        self._per_target_in_flight = per_target_in_flight
        self._lease_seconds = lease_seconds
        self._request_timeout = float(request_timeout)
        self._poll_interval = float(poll_interval)
        self._max_response_bytes = max_response_bytes
        self._stop_event = threading.Event()
        self._work_queue: queue.Queue[
            tuple[str, str, list[SyncDelivery], datetime]
        ] = queue.Queue(maxsize=max_in_flight)
        self._state_condition = threading.Condition()
        self._active_targets: dict[str, int] = {}
        self._active_batches = 0
        self._workers: list[threading.Thread] = []
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()

    def _client(self) -> Any:
        if self._http is None:
            import requests

            self._http = requests.Session()
        return self._http

    def _batch(self, target_id: str, deliveries: list[SyncDelivery]) -> SyncBatch:
        identity = "\x00".join(
            (self._local_node_id, target_id, *(item.event.event_id for item in deliveries))
        )
        batch_id = str(uuid.uuid5(_BATCH_NAMESPACE, identity))
        return SyncBatch(
            1,
            batch_id,
            self._local_node_id,
            tuple(item.event for item in deliveries),
        )

    def _response_json(self, response: object) -> object:
        iter_content = getattr(response, "iter_content", None)
        if not callable(iter_content):
            raise ValueError("remote response body is unavailable")
        content = bytearray()
        for chunk in iter_content(chunk_size=64 * 1024):
            if not isinstance(chunk, bytes):
                raise ValueError("remote response body is invalid")
            if len(content) + len(chunk) > self._max_response_bytes:
                raise ValueError(
                    "remote response body exceeds the configured limit"
                )
            content.extend(chunk)
        try:
            return json.loads(bytes(content).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("remote response body is invalid JSON") from exc

    @staticmethod
    def _close_response(response: object) -> None:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.warning("sync_remote_response_close_failed")

    @staticmethod
    def _parse_result(value: object) -> SyncBatchResult:
        if type(value) is not dict or set(value) != {
            "batch_id",
            "request_digest",
            "outcome",
            "receipts",
        }:
            raise ValueError("invalid remote batch result")
        receipts = value["receipts"]
        if type(receipts) is not list:
            raise ValueError("invalid remote batch receipts")
        parsed_receipts: list[SyncReceipt] = []
        for item in receipts:
            if type(item) is not dict or set(item) != {"event_id", "outcome"}:
                raise ValueError("invalid remote batch receipt")
            parsed_receipts.append(SyncReceipt(item["event_id"], item["outcome"]))
        return SyncBatchResult(
            value["batch_id"],
            value["request_digest"],
            value["outcome"],
            tuple(parsed_receipts),
        )

    @staticmethod
    def _validate_result(
        batch: SyncBatch,
        deliveries: list[SyncDelivery],
        result: SyncBatchResult,
    ) -> None:
        if result.batch_id != batch.batch_id or result.request_digest != batch.request_digest:
            raise ValueError("remote batch identity mismatch")
        if len(result.receipts) != len(deliveries):
            raise ValueError("remote receipt cardinality mismatch")
        by_event_id = {item.event.event_id: item for item in deliveries}
        if len(by_event_id) != len(deliveries):
            raise ValueError("delivery identity is duplicated")
        receipt_ids = {receipt.event_id for receipt in result.receipts}
        if receipt_ids != set(by_event_id):
            raise ValueError("remote receipt identity mismatch")

    def _ack_result(
        self,
        deliveries: list[SyncDelivery],
        result: SyncBatchResult,
    ) -> int:
        ack_batch = getattr(self._repository, "sync_ack_batch", None)
        if not callable(ack_batch):
            raise TypeError("sync repository must expose atomic sync_ack_batch")
        ack_batch(deliveries, result.receipts)
        return len(deliveries)

    def _fail(
        self,
        deliveries: list[SyncDelivery],
        error_code: str,
        now: datetime,
        *,
        terminal: bool,
    ) -> None:
        method_name = "sync_dead_letter" if terminal else "sync_fail"
        method = getattr(self._repository, method_name, None)
        if not callable(method):
            method = self._repository.sync_fail
        for delivery in deliveries:
            failure_time = now
            if not terminal:
                cap = min(60.0, 2.0 ** max(delivery.attempt - 1, 0))
                jitter = random.uniform(0.0, cap)
                # Repositories freeze the durable next-attempt policy as
                # ``failure_time + cap``.  Shift the supplied reference time
                # so the persisted result is full-jitter in ``[now, now+cap]``.
                failure_time = now - timedelta(seconds=cap - jitter)
            method(delivery, error_code, failure_time)

    def _send(
        self,
        target_id: str,
        target_url: str,
        deliveries: list[SyncDelivery],
        now: datetime,
    ) -> tuple[int, int]:
        batch = self._batch(target_id, deliveries)
        headers = dict(self._headers)
        headers["Content-Type"] = "application/json"
        try:
            response = self._client().post(
                f"{target_url}/sync/v1/batches",
                data=batch.canonical_bytes,
                headers=headers,
                timeout=self._request_timeout,
                stream=True,
            )
        except Exception:
            self._fail(deliveries, "transport_unavailable", now, terminal=False)
            return 0, len(deliveries)

        try:
            status_code = getattr(response, "status_code", None)
            if type(status_code) is not int:
                self._fail(deliveries, "remote_protocol_error", now, terminal=True)
                return 0, len(deliveries)
            if status_code == 429:
                self._fail(deliveries, "remote_backpressure", now, terminal=False)
                return 0, len(deliveries)
            if status_code >= 500:
                self._fail(deliveries, "remote_unavailable", now, terminal=False)
                return 0, len(deliveries)
            if status_code >= 400:
                code = (
                    "remote_identity_rejected"
                    if status_code in {401, 403}
                    else "remote_batch_conflict"
                    if status_code == 409
                    else "remote_batch_rejected"
                )
                self._fail(deliveries, code, now, terminal=True)
                return 0, len(deliveries)
            if status_code < 200 or status_code >= 300:
                self._fail(deliveries, "remote_protocol_error", now, terminal=True)
                return 0, len(deliveries)
            try:
                result = self._parse_result(self._response_json(response))
                self._validate_result(batch, deliveries, result)
            except Exception:
                self._fail(
                    deliveries,
                    "remote_protocol_error",
                    now,
                    terminal=True,
                )
                return 0, len(deliveries)
            delivered = self._ack_result(deliveries, result)
        finally:
            self._close_response(response)
        return delivered, 0

    def dispatch_once(self, now: datetime | None = None) -> DispatchResult:
        if now is None:
            now = datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise TypeError("now must be an aware datetime")
        now = now.astimezone(timezone.utc)
        claimed = delivered = failed = 0
        scheduled_batches = 0
        for target_id, target_url in self._targets.items():
            if scheduled_batches >= self._max_in_flight:
                break
            deliveries = self._repository.sync_claim(
                target_id,
                limit=self._claim_size,
                lease_seconds=self._lease_seconds,
            )
            if not deliveries:
                continue
            scheduled_batches += 1
            claimed += len(deliveries)
            sent, rejected = self._send(
                target_id, target_url, deliveries, now
            )
            delivered += sent
            failed += rejected
        return DispatchResult(claimed, delivered, failed)

    def status(self) -> SyncStatus:
        return self._repository.sync_status()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def _dispatch_status(self) -> SyncStatus:
        method = getattr(self._repository, "sync_dispatch_status", None)
        return method() if callable(method) else self.status()

    def replay(self, target_id: str, event_id: str) -> bool:
        if target_id not in self._targets:
            raise ValueError("target is not configured")
        return self._repository.sync_replay_dead_letter(target_id, event_id)

    def list_dead_letters(self, *, limit: int = 100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an exact int between 1 and 1000")
        return self._repository.sync_list_dead_letters(limit=limit)

    @staticmethod
    def _parse_page(value: object) -> tuple[SyncPage, str]:
        if type(value) is not dict or set(value) != {
            "items",
            "snapshot_seq",
            "next_cursor",
            "has_more",
        }:
            raise ValueError("invalid remote sync page")
        raw_items = value["items"]
        if type(raw_items) is not list:
            raise ValueError("invalid remote sync items")
        items: list[SyncStreamItem] = []
        for raw_item in raw_items:
            if type(raw_item) is not dict or set(raw_item) != {
                "stream_seq",
                "event",
            }:
                raise ValueError("invalid remote sync item")
            items.append(
                SyncStreamItem(
                    raw_item["stream_seq"],
                    SyncEvent.from_dict(raw_item["event"]),
                )
            )
        snapshot_seq = value["snapshot_seq"]
        has_more = value["has_more"]
        if type(snapshot_seq) is not int or snapshot_seq < 0:
            raise ValueError("invalid remote snapshot sequence")
        if type(has_more) is not bool:
            raise ValueError("invalid remote page continuation")
        next_cursor = value["next_cursor"]
        if type(next_cursor) is not str or not next_cursor:
            raise ValueError("invalid remote cursor")
        next_after_seq = items[-1].stream_seq if has_more else snapshot_seq
        return SyncPage(
            tuple(items), snapshot_seq, next_after_seq, has_more
        ), next_cursor

    def pull(
        self,
        target_id: str,
        *,
        max_pages: int,
        page_size: int,
    ) -> PullResult:
        if target_id not in self._targets:
            raise ValueError("target is not configured")
        for name, value in (("max_pages", max_pages), ("page_size", page_size)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive exact int")
        if max_pages > _MAX_PULL_PAGES:
            raise ValueError("max_pages exceeds the hard limit of 1000")
        if page_size > _MAX_PAGE_SIZE:
            raise ValueError("page_size exceeds the hard limit of 1000")
        cursor = self._pull_cursors.get(target_id)
        pages = applied = duplicate = conflict = cursor_advanced = 0
        for _ in range(max_pages):
            params: dict[str, object] = {"limit": page_size}
            if cursor is not None:
                params["cursor"] = cursor
            try:
                response = self._client().get(
                    f"{self._targets[target_id]}/sync/v1/changes",
                    params=params,
                    headers=dict(self._headers),
                    timeout=self._request_timeout,
                    stream=True,
                )
            except Exception as exc:
                raise RuntimeError("sync_pull_unavailable") from exc
            try:
                status_code = getattr(response, "status_code", None)
                if status_code in {400, 409}:
                    self._pull_cursors.pop(target_id, None)
                    raise SyncCursorExpired("cursor_expired")
                if type(status_code) is not int or not 200 <= status_code < 300:
                    raise RuntimeError("sync_pull_unavailable")
                page, next_cursor = self._parse_page(
                    self._response_json(response)
                )
                if len(page.items) > page_size:
                    raise ValueError("remote page exceeds the requested limit")
                result = self._repository.sync_apply_page(target_id, page)
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("sync_pull_invalid_page") from exc
            finally:
                self._close_response(response)
            if type(result) is not SyncApplyResult:
                raise RuntimeError("sync_pull_invalid_apply_result")
            cursor = next_cursor
            self._pull_cursors[target_id] = cursor
            pages += 1
            applied += result.applied
            duplicate += result.duplicate
            conflict += result.conflict
            cursor_advanced = max(cursor_advanced, result.cursor_advanced)
            if not page.items and not page.has_more:
                break
        return PullResult(
            pages, applied, duplicate, conflict, cursor_advanced
        )

    @staticmethod
    def _deadline_seconds(deadline: float) -> float:
        if type(deadline) not in {int, float} or not math.isfinite(float(deadline)):
            raise TypeError("deadline must be a finite number of seconds")
        if deadline < 0:
            raise ValueError("deadline must be non-negative")
        return float(deadline)

    def drain(self, deadline: float) -> SyncDrainResult:
        expires = time.monotonic() + self._deadline_seconds(deadline)
        while True:
            background_running = self._thread is not None and self._thread.is_alive()
            dispatch = (
                DispatchResult(0, 0, 0)
                if background_running
                else self.dispatch_once()
            )
            actionable = self._dispatch_status()
            overall = self.status()
            with self._state_condition:
                active_batches = self._active_batches
            if (
                actionable.pending == 0
                and actionable.leased == 0
                and active_batches == 0
            ):
                return SyncDrainResult(
                    True,
                    overall.delivered,
                    overall.pending,
                    overall.leased,
                    overall.dead_letters,
                    False,
                )
            remaining = expires - time.monotonic()
            if remaining <= 0:
                return SyncDrainResult(
                    False,
                    overall.delivered,
                    overall.pending,
                    overall.leased,
                    overall.dead_letters,
                    True,
                )
            if dispatch.claimed == 0:
                time.sleep(min(self._poll_interval, remaining))

    def _reserve_target(self, target_id: str) -> bool:
        with self._state_condition:
            if self._active_batches >= self._max_in_flight:
                return False
            target_active = self._active_targets.get(target_id, 0)
            if target_active >= self._per_target_in_flight:
                return False
            self._active_batches += 1
            self._active_targets[target_id] = target_active + 1
            return True

    def _release_target(self, target_id: str) -> None:
        with self._state_condition:
            target_active = self._active_targets.get(target_id, 0)
            if target_active <= 1:
                self._active_targets.pop(target_id, None)
            else:
                self._active_targets[target_id] = target_active - 1
            self._active_batches -= 1
            self._state_condition.notify_all()

    def _schedule_once(self) -> int:
        scheduled = 0
        now = datetime.now(timezone.utc)
        for target_id, target_url in self._targets.items():
            with self._state_condition:
                if self._stop_event.is_set() or not self._reserve_target(target_id):
                    continue
            try:
                deliveries = self._repository.sync_claim(
                    target_id,
                    limit=self._claim_size,
                    lease_seconds=self._lease_seconds,
                )
                if not deliveries:
                    self._release_target(target_id)
                    continue
                with self._state_condition:
                    if self._stop_event.is_set():
                        defer_for_shutdown = True
                    else:
                        defer_for_shutdown = False
                        self._work_queue.put_nowait(
                            (target_id, target_url, deliveries, now)
                        )
                        scheduled += 1
                if defer_for_shutdown:
                    self._fail(
                        deliveries,
                        "shutdown_deferred",
                        now,
                        terminal=False,
                    )
                    self._release_target(target_id)
            except BaseException:
                self._release_target(target_id)
                raise
        return scheduled

    def _run_worker(self) -> None:
        while not self._stop_event.is_set() or not self._work_queue.empty():
            try:
                target_id, target_url, deliveries, now = self._work_queue.get(
                    timeout=self._poll_interval
                )
            except queue.Empty:
                continue
            try:
                if self._stop_event.is_set():
                    self._fail(
                        deliveries,
                        "shutdown_deferred",
                        now,
                        terminal=False,
                    )
                else:
                    self._send(target_id, target_url, deliveries, now)
            except Exception:
                logger.warning("sync_dispatch_worker_failed")
            finally:
                self._release_target(target_id)
                self._work_queue.task_done()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                scheduled = self._schedule_once()
            except Exception:
                logger.warning("sync_dispatch_scheduler_failed")
                scheduled = 0
            if scheduled == 0:
                with self._state_condition:
                    self._state_condition.wait(timeout=self._poll_interval)

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._workers = [
                threading.Thread(
                    target=self._run_worker,
                    name=f"memplex-sync-worker-{index}",
                    daemon=True,
                )
                for index in range(self._max_in_flight)
            ]
            for worker in self._workers:
                worker.start()
            self._thread = threading.Thread(
                target=self._run,
                name="memplex-sync-scheduler",
                daemon=True,
            )
            self._thread.start()

    def _release_queued_work(self) -> None:
        while True:
            try:
                target_id, _target_url, deliveries, now = (
                    self._work_queue.get_nowait()
                )
            except queue.Empty:
                return
            try:
                self._fail(
                    deliveries,
                    "shutdown_deferred",
                    now,
                    terminal=False,
                )
            except Exception:
                logger.warning("sync_dispatch_shutdown_release_failed")
            finally:
                self._release_target(target_id)
                self._work_queue.task_done()

    def stop(self, deadline: float) -> SyncDrainResult:
        seconds = self._deadline_seconds(deadline)
        expires = time.monotonic() + seconds
        result = self.drain(max(0.0, expires - time.monotonic()))
        with self._state_condition:
            self._stop_event.set()
            self._state_condition.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, expires - time.monotonic()))
        self._release_queued_work()
        for worker in self._workers:
            worker.join(timeout=max(0.0, expires - time.monotonic()))
        status = self.status()
        actionable = self._dispatch_status()
        with self._state_condition:
            final_empty = (
                actionable.pending == 0
                and actionable.leased == 0
                and self._active_batches == 0
            )
        drained = result.drained and final_empty
        return SyncDrainResult(
            drained,
            status.delivered,
            status.pending,
            status.leased,
            status.dead_letters,
            result.deadline_exceeded or not drained,
        )
