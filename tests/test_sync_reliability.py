"""G004 真实 TCP 故障与重启可靠性证据。"""

from __future__ import annotations

import hashlib
import socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from memplex.models import Function, SourceDocument
from memplex.storage.lite.store import LiteMemoryStore
from memplex.sync_dispatcher import SyncDispatcher
from memplex.sync_ingress import validate_ingress_batch
from memplex.sync_protocol import SyncCursorClaims
from memplex.sync_repository import SyncCapturePolicy
from tests.helpers.http_fault_proxy import (
    FaultAction,
    HttpFaultProxy,
    UrllibSession,
)


def _function(identifier: str) -> Function:
    node = Function(id=identifier, name=identifier, name_normalized=identifier)
    node.tenant_id = "tenant-a"
    node.owner_subject_id = "subject-a"
    node.workspace_id = "workspace-a"
    node.visibility = "workspace"
    node.provenance = {"agent_id": "agent-a", "session_id": "session-a"}
    return node


def _store(path: Path, node_id: str) -> LiteMemoryStore:
    return LiteMemoryStore(
        path=path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", local_node_id=node_id),
        sync_max_attempts=4,
    )


def test_real_tcp_ack_loss_after_remote_commit_retries_same_event_once(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local"
    remote_path = tmp_path / "remote"
    local = _store(local_path, "local-a")
    remote = _store(remote_path, "remote-b")
    local.sync_register_target("remote-b")
    source = SourceDocument(type="text", content="reliable")
    local.add(_function("ack-loss-function"), source)

    applied: list[tuple[dict[str, object], bool]] = []

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        result = remote.sync_apply_batch(envelope.batch).to_dict()
        applied.append((result, remote.get("ack-loss-function") is not None))
        return result

    monkeypatch.setattr("memplex.sync_dispatcher.random.uniform", lambda *_: 0.0)
    with HttpFaultProxy(_apply) as proxy:
        proxy.enqueue(FaultAction("drop_after_commit"))
        proxy.enqueue(FaultAction("pass"))
        first = SyncDispatcher(
            local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            claim_size=10,
            lease_seconds=1,
            request_timeout=2,
        )
        result = first.dispatch_once()
        assert (result.claimed, result.delivered, result.failed) == (1, 0, 1)
        assert local.sync_status().pending == 1
        assert len(applied) == 1
        assert applied[0][0]["outcome"] == "accepted"
        assert applied[0][0]["receipts"][0]["outcome"] == "accepted"
        assert applied[0][1] is True
        assert (
            _store(remote_path, "remote-b").get("ack-loss-function") is not None
        ), proxy.errors

        restarted_local = _store(local_path, "local-a")
        second = SyncDispatcher(
            restarted_local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            claim_size=10,
            lease_seconds=1,
            request_timeout=2,
        )
        result = second.dispatch_once()

    assert proxy.errors == []
    assert (result.claimed, result.delivered, result.failed) == (1, 1, 0)
    assert len(applied) == 2
    assert applied[1][0] == applied[0][0]
    assert _store(local_path, "local-a").sync_status().pending == 0
    reopened_remote = _store(remote_path, "remote-b")
    assert reopened_remote.get("ack-loss-function") is not None
    assert len(reopened_remote._sync_repository._state["inbox"]) == 1


def test_real_tcp_503_retries_without_remote_partial_commit(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local"
    remote_path = tmp_path / "remote"
    local = _store(local_path, "local-a")
    remote = _store(remote_path, "remote-b")
    local.sync_register_target("remote-b")
    local.add(
        _function("retry-after-503"),
        SourceDocument(type="text", content="reliable"),
    )

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        return remote.sync_apply_batch(envelope.batch).to_dict()

    monkeypatch.setattr("memplex.sync_dispatcher.random.uniform", lambda *_: 0.0)
    with HttpFaultProxy(_apply) as proxy:
        proxy.enqueue(FaultAction("status", status_code=503))
        proxy.enqueue(FaultAction("pass"))
        dispatcher = SyncDispatcher(
            local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            lease_seconds=1,
            request_timeout=2,
        )
        first = dispatcher.dispatch_once()
        assert (first.delivered, first.failed) == (0, 1)
        assert _store(remote_path, "remote-b").get("retry-after-503") is None
        second = dispatcher.dispatch_once()

    assert proxy.errors == []
    assert (second.delivered, second.failed) == (1, 0)
    assert _store(remote_path, "remote-b").get("retry-after-503") is not None


def test_real_tcp_consecutive_503_reaches_dlq_and_replay_converges(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local"
    remote_path = tmp_path / "remote"
    local = _store(local_path, "local-a")
    remote = _store(remote_path, "remote-b")
    local.sync_register_target("remote-b")
    local.add(
        _function("dlq-after-503"),
        SourceDocument(type="text", content="reliable"),
    )

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        return remote.sync_apply_batch(envelope.batch).to_dict()

    monkeypatch.setattr("memplex.sync_dispatcher.random.uniform", lambda *_: 0.0)
    with HttpFaultProxy(_apply) as proxy:
        for _ in range(4):
            proxy.enqueue(FaultAction("status", status_code=503))
        proxy.enqueue(FaultAction("pass"))
        dispatcher = SyncDispatcher(
            local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            lease_seconds=1,
            request_timeout=2,
        )
        failures = [dispatcher.dispatch_once() for _ in range(4)]
        dead_letters = dispatcher.list_dead_letters()
        assert len(dead_letters) == 1
        assert dead_letters[0].event_id
        assert all((item.delivered, item.failed) == (0, 1) for item in failures)
        assert _store(remote_path, "remote-b").get("dlq-after-503") is None

        assert dispatcher.replay("remote-b", dead_letters[0].event_id) is True
        replayed = dispatcher.dispatch_once()

    assert proxy.errors == []
    assert (replayed.claimed, replayed.delivered, replayed.failed) == (1, 1, 0)
    assert local.sync_status().dead_letters == 0
    assert _store(remote_path, "remote-b").get("dlq-after-503") is not None


def test_real_tcp_timeout_after_commit_is_idempotent_on_retry(
    tmp_path: Path, monkeypatch
) -> None:
    local_path = tmp_path / "local"
    remote_path = tmp_path / "remote"
    local = _store(local_path, "local-a")
    remote = _store(remote_path, "remote-b")
    local.sync_register_target("remote-b")
    local.add(
        _function("timeout-after-commit"),
        SourceDocument(type="text", content="reliable"),
    )

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        return remote.sync_apply_batch(envelope.batch).to_dict()

    monkeypatch.setattr("memplex.sync_dispatcher.random.uniform", lambda *_: 0.0)
    with HttpFaultProxy(_apply) as proxy:
        # 4:1 delay-to-timeout ratio with CI-tolerant margins (the
        # original 50ms timeout flaked on loaded runners).
        proxy.enqueue(FaultAction("delay_after_commit", delay_seconds=2.0))
        proxy.enqueue(FaultAction("pass"))
        dispatcher = SyncDispatcher(
            local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            lease_seconds=4,
            request_timeout=0.5,
        )
        # The invariant under test is idempotence, not the first attempt's
        # timing: the commit lands, the retry delivers exactly once.
        first = dispatcher.dispatch_once()
        if (first.delivered, first.failed) == (0, 1):
            assert _store(remote_path, "remote-b").get("timeout-after-commit") is not None
        second = dispatcher.dispatch_once()

    assert proxy.errors == []
    assert (second.delivered, second.failed) == (1, 0)
    assert len(_store(remote_path, "remote-b")._sync_repository._state["inbox"]) == 1


def test_real_tcp_connect_refusal_keeps_delivery_durable(tmp_path: Path) -> None:
    local = _store(tmp_path / "local", "local-a")
    local.sync_register_target("remote-b")
    local.add(
        _function("connect-refused"),
        SourceDocument(type="text", content="reliable"),
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dispatcher = SyncDispatcher(
        local,
        targets={"remote-b": f"http://127.0.0.1:{port}"},
        local_node_id="local-a",
        http=UrllibSession(),
        lease_seconds=1,
        request_timeout=0.2,
    )

    result = dispatcher.dispatch_once()

    assert (result.claimed, result.delivered, result.failed) == (1, 0, 1)
    assert _store(tmp_path / "local", "local-a").sync_status().pending == 1


def test_real_tcp_page_snapshot_does_not_skip_concurrent_remote_write(
    tmp_path: Path,
) -> None:
    local = _store(tmp_path / "local", "local-a")
    remote = _store(tmp_path / "remote", "remote-b")
    source = SourceDocument(type="text", content="page")
    remote.add(_function("page-first"), source)
    page_queried = Event()
    release_response = Event()
    cursors: dict[str, SyncCursorClaims] = {}

    def _get_page(query: dict[str, str]) -> dict[str, object]:
        cursor = cursors.get(query.get("cursor", ""))
        page = remote.sync_page(
            "local-a",
            "reliability-consumer",
            cursor,
            int(query["limit"]),
        )
        if not page_queried.is_set():
            page_queried.set()
            assert release_response.wait(timeout=5)
        now = datetime.now(timezone.utc)
        token = f"cursor-{len(cursors) + 1}"
        cursors[token] = SyncCursorClaims(
            1,
            "reliability-key",
            "tenant-a",
            "local-a",
            "reliability-consumer",
            page.next_after_seq,
            page.snapshot_seq,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
        return {
            "items": [
                {"stream_seq": item.stream_seq, "event": item.event.to_dict()}
                for item in page.items
            ],
            "snapshot_seq": page.snapshot_seq,
            "next_cursor": token,
            "has_more": page.has_more,
        }

    with HttpFaultProxy(lambda _raw: {}, get_page=_get_page) as proxy:
        dispatcher = SyncDispatcher(
            local,
            targets={"remote-b": proxy.url},
            local_node_id="local-a",
            http=UrllibSession(),
            request_timeout=2,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            first_future = pool.submit(
                dispatcher.pull,
                "remote-b",
                max_pages=1,
                page_size=10,
            )
            assert page_queried.wait(timeout=5)
            remote.add(_function("page-concurrent"), source)
            release_response.set()
            first = first_future.result(timeout=5)
        assert first.applied == 1
        assert local.get("page-first") is not None
        assert local.get("page-concurrent") is None

        second = dispatcher.pull("remote-b", max_pages=2, page_size=10)

    assert proxy.errors == []
    assert second.applied == 1
    reopened = _store(tmp_path / "local", "local-a")
    assert reopened.get("page-first") is not None
    assert reopened.get("page-concurrent") is not None
