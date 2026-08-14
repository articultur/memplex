from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from memplex.models import Function, SourceDocument, SourceType
from memplex.storage import create_store
from memplex.storage.lite.store import LiteMemoryStore
from memplex.sync_dispatcher import DispatchResult, PullResult, SyncDispatcher
from memplex.sync_protocol import (
    SyncApplyResult,
    SyncBatch,
    SyncDelivery,
    SyncDrainResult,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncReceipt,
    SyncScope,
    SyncStatus,
    SyncVersion,
)
from memplex.sync_repository import SyncCapturePolicy, SyncDeadLetterEntry


def _event(identifier: str, *, origin: str = "node-local") -> SyncEvent:
    event_id = str(uuid.uuid4())
    occurred_at = datetime(2026, 8, 11, tzinfo=timezone.utc)
    return SyncEvent(
        1,
        event_id,
        origin,
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(identifier),
        SyncOperation.UPSERT,
        str(SyncVersion.create(occurred_at, origin, event_id)),
        SyncScope(
            "tenant-a",
            "subject-a",
            "workspace-a",
            "workspace",
            "agent-a",
            "session-a",
        ),
        {"id": identifier},
    )


class _Repository:
    def __init__(self, events: list[SyncEvent], *, max_attempts: int = 3) -> None:
        self.rows = [
            {"event": event, "state": "pending", "attempt": 0}
            for event in events
        ]
        self.max_attempts = max_attempts

    def sync_claim(self, target_id: str, *, limit: int, lease_seconds: int):
        del lease_seconds
        claimed = []
        for row in self.rows:
            if len(claimed) >= limit:
                break
            if row["state"] != "pending" or row["event"].origin_node_id != "node-local":
                continue
            row["state"] = "leased"
            row["attempt"] += 1
            claimed.append(
                SyncDelivery(
                    target_id,
                    row["event"],
                    row["attempt"],
                    str(uuid.uuid4()),
                    datetime.now(timezone.utc) + timedelta(seconds=30),
                )
            )
        return claimed

    def _row(self, delivery: SyncDelivery):
        return next(
            row
            for row in self.rows
            if row["event"].event_id == delivery.event.event_id
        )

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None:
        assert receipt.event_id == delivery.event.event_id
        self._row(delivery)["state"] = "delivered"

    def sync_ack_batch(self, deliveries, receipts) -> None:
        assert {item.event.event_id for item in deliveries} == {
            item.event_id for item in receipts
        }
        for delivery in deliveries:
            self._row(delivery)["state"] = "delivered"

    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None:
        del error_code, now
        row = self._row(delivery)
        row["state"] = (
            "dead_letter" if row["attempt"] >= self.max_attempts else "pending"
        )

    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None:
        del error_code, now
        self._row(delivery)["state"] = "dead_letter"

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool:
        del target_id
        row = next(
            (row for row in self.rows if row["event"].event_id == event_id),
            None,
        )
        if row is None or row["state"] != "dead_letter":
            return False
        row.update(state="pending", attempt=0)
        return True

    def sync_list_dead_letters(self, *, limit: int):
        return [
            SyncDeadLetterEntry(
                "remote-a",
                row["event"].event_id,
                row["attempt"],
                "remote_batch_rejected",
            )
            for row in self.rows
            if row["state"] == "dead_letter"
        ][:limit]

    def sync_dispatch_status(self) -> SyncStatus:
        local_rows = [
            row for row in self.rows if row["event"].origin_node_id == "node-local"
        ]
        return SyncStatus(
            pending=sum(row["state"] == "pending" for row in local_rows),
            leased=sum(row["state"] == "leased" for row in local_rows),
            delivered=sum(row["state"] == "delivered" for row in local_rows),
            disabled_targets=0,
            dead_letters=sum(row["state"] == "dead_letter" for row in local_rows),
        )

    def sync_status(self) -> SyncStatus:
        return SyncStatus(
            pending=sum(row["state"] == "pending" for row in self.rows),
            leased=sum(row["state"] == "leased" for row in self.rows),
            delivered=sum(row["state"] == "delivered" for row in self.rows),
            disabled_targets=0,
            dead_letters=sum(row["state"] == "dead_letter" for row in self.rows),
        )


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.content = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def json(self):
        return self._body

    def iter_content(self, chunk_size):
        del chunk_size
        yield self.content

    def close(self):
        return None


class _AcceptingHttp:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []

    def post(self, url, *, data, headers, timeout, stream):
        del timeout
        assert stream is True
        self.requests.append((url, data, headers))
        batch = SyncBatch.from_dict(json.loads(data))
        return _Response(
            200,
            {
                "batch_id": batch.batch_id,
                "request_digest": batch.request_digest,
                "outcome": "accepted",
                "receipts": [
                    {"event_id": event.event_id, "outcome": "accepted"}
                    for event in batch.events
                ],
            },
        )


def test_dispatch_once_posts_one_canonical_batch_and_acks_every_delivery() -> None:
    events = [_event("a"), _event("b")]
    repository = _Repository(events)
    http = _AcceptingHttp()
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=http,
        headers={"Authorization": "Bearer secret"},
        claim_size=100,
        max_in_flight=4,
        per_target_in_flight=1,
        lease_seconds=30,
    )

    result = dispatcher.dispatch_once(datetime.now(timezone.utc))

    assert result == DispatchResult(claimed=2, delivered=2, failed=0)
    assert repository.sync_dispatch_status().delivered == 2
    assert len(http.requests) == 1
    url, raw, headers = http.requests[0]
    assert url == "https://remote.example/sync/v1/batches"
    batch = SyncBatch.from_dict(json.loads(raw))
    assert raw == batch.canonical_bytes
    assert batch.origin_node_id == "node-local"
    assert headers["Authorization"] == "Bearer secret"
    assert headers["Content-Type"] == "application/json"


def test_ack_loss_retries_the_same_batch_and_remote_duplicate_is_acked() -> None:
    repository = _Repository([_event("ack-loss")])

    class AckLossHttp(_AcceptingHttp):
        def __init__(self) -> None:
            super().__init__()
            self.remote_event_ids: set[str] = set()

        def post(self, url, *, data, headers, timeout, stream):
            response = super().post(
                url,
                data=data,
                headers=headers,
                timeout=timeout,
                stream=stream,
            )
            batch = SyncBatch.from_dict(json.loads(data))
            if not self.remote_event_ids:
                self.remote_event_ids.update(event.event_id for event in batch.events)
                raise TimeoutError("response lost after commit")
            response._body["outcome"] = "duplicate"
            response._body["receipts"] = [
                {"event_id": event.event_id, "outcome": "duplicate"}
                for event in batch.events
            ]
            return response

    http = AckLossHttp()
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=http,
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    )

    first = dispatcher.dispatch_once(datetime.now(timezone.utc))
    second = dispatcher.dispatch_once(datetime.now(timezone.utc))

    assert first.failed == 1
    assert second.delivered == 1
    assert http.requests[0][1] == http.requests[1][1]
    assert len(http.remote_event_ids) == 1
    assert repository.sync_dispatch_status().delivered == 1


def test_local_ack_failure_preserves_lease_for_durable_retry() -> None:
    class FailingAckRepository(_Repository):
        def sync_ack_batch(self, deliveries, receipts) -> None:
            del deliveries, receipts
            raise RuntimeError("database unavailable after remote commit")

    repository = FailingAckRepository([_event("ack-db-failure")])
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=_AcceptingHttp(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        dispatcher.dispatch_once()

    assert repository.sync_dispatch_status() == SyncStatus(0, 1, 0, 0, 0)


def test_terminal_http_rejection_moves_delivery_to_dlq_and_replay_is_durable() -> None:
    event = _event("bad")
    repository = _Repository([event], max_attempts=1)

    class RejectingHttp:
        def post(self, url, *, data, headers, timeout, stream):
            del url, data, headers, timeout, stream
            return _Response(422, {"detail": "private remote detail"})

    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=RejectingHttp(),
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    )

    result = dispatcher.dispatch_once(datetime.now(timezone.utc))

    assert result.failed == 1
    assert repository.sync_dispatch_status().dead_letters == 1
    assert dispatcher.list_dead_letters() == [
        SyncDeadLetterEntry(
            "remote-a", event.event_id, 1, "remote_batch_rejected"
        )
    ]
    assert dispatcher.replay("remote-a", event.event_id) is True
    assert repository.sync_dispatch_status().pending == 1


def test_dispatch_rejects_oversized_remote_response_before_json_decode() -> None:
    event = _event("oversized-response")
    repository = _Repository([event])

    class OversizedHttp:
        def post(self, url, *, data, headers, timeout, stream):
            del url, data, headers, timeout
            assert stream is True
            response = _Response(200, {"ignored": True})
            response.content = b"x" * 65
            return response

    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=OversizedHttp(),
        max_response_bytes=64,
    )

    result = dispatcher.dispatch_once()

    assert result == DispatchResult(claimed=1, delivered=0, failed=1)
    assert repository.sync_dispatch_status().dead_letters == 1


def test_dispatch_reads_success_response_through_bounded_stream() -> None:
    event = _event("streamed-response")
    repository = _Repository([event])

    class StreamingResponse:
        status_code = 200

        def __init__(self, body: dict[str, object]) -> None:
            self._raw = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            self.closed = False

        def iter_content(self, chunk_size):
            assert chunk_size <= 64 * 1024
            midpoint = max(1, len(self._raw) // 2)
            yield self._raw[:midpoint]
            yield self._raw[midpoint:]

        def close(self):
            self.closed = True

    class StreamingHttp:
        def __init__(self) -> None:
            self.response = None

        def post(self, url, *, data, headers, timeout, stream):
            del url, headers, timeout
            assert stream is True
            batch = SyncBatch.from_dict(json.loads(data))
            self.response = StreamingResponse(
                {
                    "batch_id": batch.batch_id,
                    "request_digest": batch.request_digest,
                    "outcome": "accepted",
                    "receipts": [
                        {
                            "event_id": item.event_id,
                            "outcome": "accepted",
                        }
                        for item in batch.events
                    ],
                }
            )
            return self.response

    http = StreamingHttp()
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=http,
    )

    result = dispatcher.dispatch_once()

    assert result == DispatchResult(claimed=1, delivered=1, failed=0)
    assert http.response.closed is True


def test_pull_rejects_oversized_response_and_hard_bounds() -> None:
    class PullRepository(_Repository):
        def sync_apply_page(self, remote_id, page):
            raise AssertionError((remote_id, page))

    class OversizedHttp:
        def get(self, url, *, params, headers, timeout, stream):
            del url, params, headers, timeout
            assert stream is True
            response = _Response(200, {"ignored": True})
            response.content = b"x" * 65
            return response

    dispatcher = SyncDispatcher(
        PullRepository([]),
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=OversizedHttp(),
        max_response_bytes=64,
    )

    with pytest.raises(RuntimeError, match="sync_pull_invalid_page"):
        dispatcher.pull("remote-a", max_pages=1, page_size=1)
    with pytest.raises(ValueError, match="max_pages"):
        dispatcher.pull("remote-a", max_pages=1001, page_size=1)
    with pytest.raises(ValueError, match="page_size"):
        dispatcher.pull("remote-a", max_pages=1, page_size=1001)


def test_pull_rejects_page_larger_than_requested_before_apply() -> None:
    first = _event("over-page-a", origin="remote-a")
    second = _event("over-page-b", origin="remote-a")

    class PullRepository(_Repository):
        def sync_apply_page(self, remote_id, page):
            raise AssertionError((remote_id, page))

    class PullHttp:
        def get(self, url, *, params, headers, timeout, stream):
            del url, headers, timeout
            assert params["limit"] == 1
            assert stream is True
            return _Response(
                200,
                {
                    "items": [
                        {"stream_seq": 1, "event": first.to_dict()},
                        {"stream_seq": 2, "event": second.to_dict()},
                    ],
                    "snapshot_seq": 2,
                    "next_cursor": "signed-over-page",
                    "has_more": False,
                },
            )

    dispatcher = SyncDispatcher(
        PullRepository([]),
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=PullHttp(),
    )

    with pytest.raises(RuntimeError, match="sync_pull_invalid_page"):
        dispatcher.pull("remote-a", max_pages=1, page_size=1)


def test_mixed_origin_pending_rows_do_not_block_dispatcher_drain() -> None:
    repository = _Repository([_event("remote", origin="remote-source")])
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=_AcceptingHttp(),
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    )

    result = dispatcher.drain(0.1)

    assert result.drained is True
    assert result.pending == 1
    assert result.deadline_exceeded is False


def test_restart_recovers_durable_delivery_without_in_memory_future(tmp_path) -> None:
    def store() -> LiteMemoryStore:
        return LiteMemoryStore(
            path=tmp_path / "memory.json",
            sync_capture_policy=SyncCapturePolicy("required", "node-local"),
        )

    first = store()
    first.sync_register_target("remote-a")
    first.add(
        Function(
            id="restart-event",
            name="restart-event",
            name_normalized="restart-event",
            tenant_id="tenant-a",
            owner="subject-a",
            owner_subject_id="subject-a",
            workspace_id="workspace-a",
            visibility="workspace",
            provenance={"agent_id": "agent-a", "session_id": "session-a"},
        ),
        SourceDocument(type="text", source_type=SourceType.WIKI),
    )

    class Offline:
        def post(self, url, *, data, headers, timeout, stream):
            del url, data, headers, timeout, stream
            raise TimeoutError("offline")

    failed = SyncDispatcher(
        first,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=Offline(),
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    ).dispatch_once()
    assert failed.failed == 1
    assert store().sync_dispatch_status().pending == 1

    # The durable repository owns retry timing; no in-memory Future is needed
    # to recover this row after a process restart.
    time.sleep(1.05)
    reopened = store()
    recovered = SyncDispatcher(
        reopened,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=_AcceptingHttp(),
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    ).dispatch_once()

    assert recovered.delivered == 1
    assert store().sync_dispatch_status().delivered == 1


def test_pull_uses_signed_cursor_pages_and_applies_each_page_once() -> None:
    first = _event("pulled-a", origin="remote-a")
    second = _event("pulled-b", origin="remote-a")

    class PullRepository(_Repository):
        def __init__(self) -> None:
            super().__init__([])
            self.pages = []

        def sync_apply_page(self, remote_id, page):
            self.pages.append((remote_id, page))
            return SyncApplyResult(len(page.items), 0, 0, page.next_after_seq)

    class PullHttp(_AcceptingHttp):
        def __init__(self) -> None:
            super().__init__()
            self.cursors = []

        def get(self, url, *, params, headers, timeout, stream):
            del url, headers, timeout
            assert stream is True
            cursor = params.get("cursor")
            self.cursors.append(cursor)
            if cursor is None:
                return _Response(
                    200,
                    {
                        "items": [{"stream_seq": 1, "event": first.to_dict()}],
                        "snapshot_seq": 2,
                        "next_cursor": "signed-1",
                        "has_more": True,
                    },
                )
            if cursor == "signed-1":
                return _Response(
                    200,
                    {
                        "items": [{"stream_seq": 2, "event": second.to_dict()}],
                        "snapshot_seq": 2,
                        "next_cursor": "signed-2",
                        "has_more": False,
                    },
                )
            return _Response(
                200,
                {
                    "items": [],
                    "snapshot_seq": 2,
                    "next_cursor": "signed-3",
                    "has_more": False,
                },
            )

    repository = PullRepository()
    http = PullHttp()
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=http,
        claim_size=10,
        max_in_flight=1,
        per_target_in_flight=1,
        lease_seconds=30,
    )

    result = dispatcher.pull("remote-a", max_pages=5, page_size=10)

    assert result == PullResult(
        pages=3, applied=2, duplicate=0, conflict=0, cursor_advanced=2
    )
    assert http.cursors == [None, "signed-1", "signed-2"]
    assert len(repository.pages) == 3
    assert all(remote_id == "remote-a" for remote_id, _ in repository.pages)


def test_background_workers_enforce_global_batch_cap_and_drain() -> None:
    target_events = {
        target: _event(target) for target in ("remote-a", "remote-b", "remote-c")
    }

    class TargetRepository:
        def __init__(self) -> None:
            self.states = {target: "pending" for target in target_events}
            self.attempts = {target: 0 for target in target_events}
            self.lock = threading.Lock()

        def sync_claim(self, target_id, *, limit, lease_seconds):
            del limit, lease_seconds
            with self.lock:
                if self.states[target_id] != "pending":
                    return []
                self.states[target_id] = "leased"
                self.attempts[target_id] += 1
                return [
                    SyncDelivery(
                        target_id,
                        target_events[target_id],
                        self.attempts[target_id],
                        str(uuid.uuid4()),
                        datetime.now(timezone.utc) + timedelta(seconds=30),
                    )
                ]

        def sync_ack(self, delivery, receipt):
            del receipt
            with self.lock:
                self.states[delivery.target_id] = "delivered"

        def sync_ack_batch(self, deliveries, receipts):
            del receipts
            with self.lock:
                for delivery in deliveries:
                    self.states[delivery.target_id] = "delivered"

        def sync_fail(self, delivery, error_code, now):
            del error_code, now
            with self.lock:
                self.states[delivery.target_id] = "pending"

        def sync_dead_letter(self, delivery, error_code, now):
            del error_code, now
            with self.lock:
                self.states[delivery.target_id] = "dead_letter"

        def sync_replay_dead_letter(self, target_id, event_id):
            del event_id
            with self.lock:
                if self.states[target_id] != "dead_letter":
                    return False
                self.states[target_id] = "pending"
                return True

        def sync_status(self):
            with self.lock:
                states = tuple(self.states.values())
            return SyncStatus(
                states.count("pending"),
                states.count("leased"),
                states.count("delivered"),
                0,
                states.count("dead_letter"),
            )

        sync_dispatch_status = sync_status

    class BlockingHttp(_AcceptingHttp):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()
            self.active = 0
            self.max_active = 0
            self.two_started = threading.Event()
            self.release = threading.Event()

        def post(self, url, *, data, headers, timeout, stream):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                if self.active == 2:
                    self.two_started.set()
            assert self.release.wait(timeout=timeout)
            try:
                return super().post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    stream=stream,
                )
            finally:
                with self.lock:
                    self.active -= 1

    repository = TargetRepository()
    http = BlockingHttp()
    dispatcher = SyncDispatcher(
        repository,
        targets={
            "remote-a": "https://a.example",
            "remote-b": "https://b.example",
            "remote-c": "https://c.example",
        },
        local_node_id="node-local",
        http=http,
        claim_size=10,
        max_in_flight=2,
        per_target_in_flight=1,
        lease_seconds=30,
        request_timeout=2,
        poll_interval=0.01,
    )

    dispatcher.start()
    assert http.two_started.wait(timeout=2)
    assert http.max_active == 2
    http.release.set()
    result = dispatcher.stop(2)

    assert result.drained is True
    assert repository.sync_status().delivered == 3
    assert http.max_active == 2


def test_stop_uses_one_absolute_deadline_for_all_thread_joins() -> None:
    dispatcher = SyncDispatcher(
        _Repository([]),
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=_AcceptingHttp(),
        poll_interval=0.01,
    )
    dispatcher.drain = lambda deadline: SyncDrainResult(
        False, 0, 1, 0, 0, True
    )

    class SlowJoin:
        # Record the timeout each join receives instead of sleeping it:
    # the invariant under test is that every join shares ONE absolute
    # deadline (each timeout <= the budget, never cumulative), which a
    # wall-clock bound cannot prove reliably on shared macOS runners.
        timeouts: list[float] = []

        def join(self, timeout):
            SlowJoin.timeouts.append(timeout)

    dispatcher._thread = SlowJoin()
    dispatcher._workers = [SlowJoin(), SlowJoin()]

    started = time.monotonic()
    result = dispatcher.stop(0.05)
    elapsed = time.monotonic() - started

    assert result.deadline_exceeded is True
    # Three joins, each bounded by the one 0.05s budget (not 3x0.05 serial).
    assert len(SlowJoin.timeouts) == 3
    assert all(0 < t <= 0.05 for t in SlowJoin.timeouts)
    assert elapsed < 1.0  # generous runaway guard only


def test_stop_fence_rechecks_work_claimed_after_drain_snapshot() -> None:
    allow_claim = threading.Event()
    send_started = threading.Event()
    release_send = threading.Event()

    class GatedRepository(_Repository):
        def sync_claim(self, target_id, *, limit, lease_seconds):
            assert allow_claim.wait(timeout=2)
            return super().sync_claim(
                target_id,
                limit=limit,
                lease_seconds=lease_seconds,
            )

    class BlockingHttp(_AcceptingHttp):
        def post(self, url, *, data, headers, timeout, stream):
            send_started.set()
            assert release_send.wait(timeout=2)
            return super().post(
                url,
                data=data,
                headers=headers,
                timeout=timeout,
                stream=stream,
            )

    repository = GatedRepository([_event("claimed-after-drain-snapshot")])
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=BlockingHttp(),
        poll_interval=0.01,
    )

    def stale_drain_snapshot(_deadline):
        allow_claim.set()
        assert send_started.wait(timeout=2)
        return SyncDrainResult(True, 0, 0, 0, 0, False)

    dispatcher.drain = stale_drain_snapshot
    dispatcher.start()
    try:
        result = dispatcher.stop(0.01)

        assert result.drained is False
        assert result.leased == 1
        assert result.deadline_exceeded is True
    finally:
        release_send.set()
        for worker in dispatcher._workers:
            worker.join(timeout=2)

    assert repository.sync_status().delivered == 1


def test_stop_deadline_releases_queued_unissued_leases() -> None:
    repository = _Repository([_event("queued-at-stop")])
    dispatcher = SyncDispatcher(
        repository,
        targets={"remote-a": "https://remote.example"},
        local_node_id="node-local",
        http=_AcceptingHttp(),
        poll_interval=0.01,
    )
    deliveries = repository.sync_claim(
        "remote-a", limit=1, lease_seconds=30
    )
    assert dispatcher._reserve_target("remote-a") is True
    dispatcher._work_queue.put_nowait(
        (
            "remote-a",
            "https://remote.example",
            deliveries,
            datetime.now(timezone.utc),
        )
    )

    result = dispatcher.stop(0.01)

    assert result.deadline_exceeded is True
    assert repository.sync_dispatch_status() == SyncStatus(1, 0, 0, 0, 0)
    assert dispatcher._active_batches == 0
    assert dispatcher._work_queue.empty()


def test_durable_capture_bypasses_legacy_future_wrapper(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MEMPLEX_REMOTE_URL", "https://legacy.example")

    store = create_store(
        "lite",
        path=str(tmp_path),
        sync_capture_policy=SyncCapturePolicy("required", "node-local"),
    )

    assert type(store) is LiteMemoryStore
    assert not hasattr(store, "_push_futures")
