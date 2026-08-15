"""G004 PostgreSQL + real TCP dispatcher integration matrix."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import platform
import resource
import signal
import socket
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Event
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from memplex.models import Function, SourceDocument
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.migrations.runner import (
    PostgresMigrationRunner,
    VectorCapabilityRequest,
)
from memplex.storage.pool import PostgresStorageResources
from memplex.storage.postgres import PostgresMemoryStore
from memplex.sync_dispatcher import SyncDispatcher
from memplex.sync_ingress import validate_ingress_batch
from memplex.sync_protocol import (
    SyncBatch,
    SyncCursorClaims,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncScope,
    SyncVersion,
)
from memplex.sync_repository import SyncCapturePolicy
from tests.helpers.http_fault_proxy import FaultAction, HttpFaultProxy, UrllibSession
from tests.test_postgres_integration import (
    _admin_execute,
    _admin_query,
    _authorization,
    _drop_unprivileged_role,
    _provision_application_role,
    _provision_ingress_role,
    _required_capture_store,
    psycopg2,
)


def _remote_store(path: Path) -> LiteMemoryStore:
    return LiteMemoryStore(
        path=path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", "remote-b"),
    )


def _dispatch_postgres_process(
    dsn: str,
    target_url: str,
    result_queue: multiprocessing.Queue,
) -> None:
    resources = None
    try:
        resources = PostgresStorageResources(dsn)
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
        )
        store = PostgresMemoryStore(
            dsn=dsn,
            ready_pool=resources.ready_pool,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id="process-dispatch-local"
            ),
        )
        result = SyncDispatcher(
            store,
            targets={"remote-b": target_url},
            local_node_id="process-dispatch-local",
            http=UrllibSession(),
            claim_size=5,
            max_in_flight=1,
            request_timeout=5,
        ).dispatch_once()
        result_queue.put((result.claimed, result.delivered, result.failed))
    except BaseException as exc:
        result_queue.put(("error", type(exc).__name__))
        raise
    finally:
        if resources is not None:
            resources.close()


def _run_dispatcher_until_sigterm(
    dsn: str,
    target_url: str,
    result_queue: multiprocessing.Queue,
) -> None:
    resources = PostgresStorageResources(dsn)
    resources.ensure_ready(
        VectorCapabilityRequest(dim=0, policy="disabled"),
        "development",
    )
    store = PostgresMemoryStore(
        dsn=dsn,
        ready_pool=resources.ready_pool,
        sync_capture_policy=SyncCapturePolicy(
            "required", local_node_id="sigterm-dispatch-local"
        ),
    )
    dispatcher = SyncDispatcher(
        store,
        targets={"remote-b": target_url},
        local_node_id="sigterm-dispatch-local",
        http=UrllibSession(),
        claim_size=1,
        max_in_flight=1,
        lease_seconds=1,
        request_timeout=5,
        poll_interval=0.01,
    )
    finished = __import__("threading").Event()

    def _stop(_signum: int, _frame: object) -> None:
        result = dispatcher.stop(0.1)
        result_queue.put(result.to_dict())
        resources.close()
        finished.set()

    signal.signal(signal.SIGTERM, _stop)
    dispatcher.start()
    result_queue.put({"started": True})
    finished.wait(timeout=20)


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_uvicorn_postgres_process(
    app_dsn: str,
    migration_dsn: str,
    inbound_dsn: str,
    port: int,
    result_queue: multiprocessing.Queue,
) -> None:
    try:
        import uvicorn

        from memplex.adapters.http_api import create_app
        from memplex.config import MemplexConfig

        config = MemplexConfig()
        config.deployment.profile = "production"
        config.storage.backend = "postgres"
        config.storage.path = app_dsn
        config.storage.migration_dsn = migration_dsn
        config.storage.inbound_dsn = inbound_dsn
        config.llm.query_enhancement = False
        config.sync.enabled = True
        config.sync.node_id = "server-node"
        config.sync.cursor_signing_key_id = "uvicorn-test-key"
        config.sync.cursor_signing_secret = "uvicorn-test-signing-secret-32-bytes"
        config.sync.validate()
        uvicorn.run(
            create_app(config),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        )
    except BaseException as exc:
        result_queue.put((port, type(exc).__name__, str(exc)))
        raise


def _urllib_json(
    url: str,
    *,
    api_key: str,
    body: bytes | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, object]]:
    headers = {"X-API-Key": api_key}
    method = "GET"
    if body is not None:
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return int(response.status), json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        return int(exc.code), payload


def _wait_for_uvicorn(
    url: str,
    *,
    api_key: str,
    process: multiprocessing.Process,
    result_queue: multiprocessing.Queue,
) -> None:
    deadline = time.monotonic() + 30
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.exitcode is not None:
            diagnostics = []
            while not result_queue.empty():
                diagnostics.append(result_queue.get_nowait())
            raise AssertionError(
                f"uvicorn process exited before readiness: {process.exitcode}, {diagnostics}"
            )
        try:
            status, payload = _urllib_json(url, api_key=api_key, timeout=1)
            assert status == 200, payload
            return
        except (AssertionError, OSError, URLError) as exc:
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"uvicorn process did not become ready: {last_error}")


def test_two_uvicorn_processes_share_postgres_inbox_and_business_state(
    pg_function_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("uvicorn", reason="uvicorn is required for the process gate")
    PostgresMigrationRunner(pg_function_dsn).apply()
    app_role = f"memplex_uvicorn_app_{uuid.uuid4().hex[:8]}"
    ingress_role = f"memplex_uvicorn_ingress_{uuid.uuid4().hex[:8]}"
    local_token = f"local-{uuid.uuid4().hex}"
    remote_token = f"remote-{uuid.uuid4().hex}"
    tenant_id = f"tenant-uvicorn-{uuid.uuid4().hex[:8]}"
    _provision_application_role(pg_function_dsn, app_role)
    _provision_ingress_role(pg_function_dsn, ingress_role, "remote-a")
    _admin_execute(
        pg_function_dsn,
        "SELECT memplex_configure_sync_local_identity(%s)",
        ("server-node",),
    )
    app_dsn = psycopg2.extensions.make_dsn(pg_function_dsn, user=app_role)
    inbound_dsn = psycopg2.extensions.make_dsn(pg_function_dsn, user=ingress_role)
    registry = [
        {
            "credential_id": "uvicorn-local",
            "token_sha256": hashlib.sha256(local_token.encode("utf-8")).hexdigest(),
            "tenant_id": tenant_id,
            "subject_id": "local-service",
            "workspace_id": "uvicorn-workspace",
            "agent_id": "server-node",
            "roles": ["service"],
        },
        {
            "credential_id": "uvicorn-remote",
            "token_sha256": hashlib.sha256(remote_token.encode("utf-8")).hexdigest(),
            "tenant_id": tenant_id,
            "subject_id": "remote-owner",
            "workspace_id": "uvicorn-workspace",
            "agent_id": "remote-a",
            "roles": ["peer"],
        },
    ]
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps(registry))
    monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", local_token)
    monkeypatch.setenv("MEMPLEX_HOST", "127.0.0.1")

    ports = [_free_tcp_port(), _free_tcp_port()]
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_run_uvicorn_postgres_process,
            args=(app_dsn, pg_function_dsn, inbound_dsn, port, results),
        )
        for port in ports
    ]
    event_id = str(uuid.uuid4())
    identifier = f"uvicorn-shared-{uuid.uuid4().hex}"
    event = SyncEvent(
        1,
        event_id,
        "remote-a",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(identifier),
        SyncOperation.UPSERT,
        str(SyncVersion.create(datetime.now(timezone.utc), "remote-a", event_id)),
        SyncScope(
            tenant_id,
            "remote-owner",
            "uvicorn-workspace",
            "user",
            None,
            None,
        ),
        {"id": identifier, "name": "shared uvicorn state"},
    )
    batch = SyncBatch(1, str(uuid.uuid4()), "remote-a", (event,))

    try:
        for process in processes:
            process.start()
        for process, port in zip(processes, ports, strict=True):
            _wait_for_uvicorn(
                f"http://127.0.0.1:{port}/health",
                api_key=remote_token,
                process=process,
                result_queue=results,
            )

        first_status, first = _urllib_json(
            f"http://127.0.0.1:{ports[0]}/sync/v1/batches",
            api_key=remote_token,
            body=batch.canonical_bytes,
        )
        second_status, second = _urllib_json(
            f"http://127.0.0.1:{ports[1]}/sync/v1/batches",
            api_key=remote_token,
            body=batch.canonical_bytes,
        )

        assert first_status == 200, first
        assert second_status == 200, second
        assert first["outcome"] == "accepted"
        assert second == first
        assert first["request_digest"] == batch.request_digest
        assert second["request_digest"] == batch.request_digest
        assert _admin_query(
            pg_function_dsn,
            "SELECT count(*) FROM memplex_sync_inbox WHERE tenant_id=%s AND event_id=%s",
            (tenant_id, event_id),
        ) == [(1,)]
        assert _admin_query(
            pg_function_dsn,
            "SELECT count(*) FROM memplex_functions WHERE tenant_id=%s AND id=%s",
            (tenant_id, identifier),
        ) == [(1,)]
        assert _admin_query(
            pg_function_dsn,
            "SELECT count(*) FROM memplex_sync_batches WHERE tenant_id=%s AND batch_id=%s",
            (tenant_id, batch.batch_id),
        ) == [(1,)]
        assert _admin_query(
            pg_function_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE tenant_id=%s AND event_id=%s",
            (tenant_id, event_id),
        ) == [(1,)]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.kill()
                process.join(timeout=10)
        try:
            _drop_unprivileged_role(pg_function_dsn, ingress_role)
        finally:
            _drop_unprivileged_role(pg_function_dsn, app_role)


def test_two_dispatchers_claim_each_postgres_delivery_once_over_real_tcp(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="dispatcher-local",
    )
    target_id = "remote-b"
    store.sync_register_target(target_id)
    source = SourceDocument(type="text", content="dispatcher")
    ids = [f"dispatch-{uuid.uuid4().hex}" for _ in range(10)]
    for identifier in ids:
        store.add(Function(id=identifier, name=identifier), source)
    remote = _remote_store(tmp_path / "remote")
    simultaneous = Barrier(2, timeout=10)

    def _apply(raw: bytes) -> dict[str, object]:
        simultaneous.wait()
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        return remote.sync_apply_batch(envelope.batch).to_dict()

    try:
        with HttpFaultProxy(_apply) as proxy:
            dispatchers = [
                SyncDispatcher(
                    store,
                    targets={target_id: proxy.url},
                    local_node_id="dispatcher-local",
                    http=UrllibSession(),
                    claim_size=5,
                    max_in_flight=1,
                    request_timeout=5,
                )
                for _ in range(2)
            ]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda item: item.dispatch_once(), dispatchers))

        assert proxy.errors == []
        assert sum(item.claimed for item in results) == 10
        assert sum(item.delivered for item in results) == 10, (
            results,
            store.sync_list_dead_letters(limit=20),
            _admin_query(
                pg_function_dsn,
                "SELECT state, last_error_code, count(*) "
                "FROM memplex_sync_deliveries GROUP BY state, last_error_code",
            ),
        )
        assert sum(item.failed for item in results) == 0
        assert store.sync_dispatch_status().pending == 0
        assert store.sync_dispatch_status().delivered == 10
        reopened = _remote_store(tmp_path / "remote")
        assert {node.id for node in reopened.list_functions(limit=100)} == set(ids)
        assert len(reopened._sync_repository._state["inbox"]) == 10
    finally:
        resources.close()


def test_postgres_pages_100001_mixed_events_with_bounded_monotonic_cursor(
    pg_function_dsn: str,
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="backlog-local",
    )
    tenant_id = "sync-backlog-tenant"
    origin = "backlog-origin"
    node_key = str(SyncEntityKey.node("backlog-node"))
    edge_key = str(
        SyncEntityKey.edge("backlog-left", "backlog-right", "REFERENCES")
    )
    try:
        _admin_execute(
            pg_function_dsn,
            """
            WITH generated AS (
                SELECT value, md5(value::text)::uuid::text AS event_id
                FROM generate_series(1, 100001) AS value
            )
            INSERT INTO memplex_sync_outbox
                (tenant_id,event_id,origin_node_id,node_type,entity_key,operation,
                 version_key,payload,visibility,owner_subject_id,workspace_id,
                 agent_id,session_id)
            SELECT %s,
                   event_id,
                   %s,
                   CASE value %% 5
                       WHEN 0 THEN 'function'
                       WHEN 1 THEN 'fact'
                       WHEN 2 THEN 'preference'
                       WHEN 3 THEN 'observation'
                       ELSE 'edge'
                   END,
                   CASE WHEN value %% 5 = 4 THEN %s ELSE %s END,
                   CASE WHEN value %% 11 = 0 THEN 'tombstone' ELSE 'upsert' END,
                   'v1:' || rtrim(
                       replace(replace(replace(
                           encode(convert_to(
                               '["2026-08-11T00:00:00.000000Z",' ||
                               to_json(%s::text)::text || ',' ||
                               to_json(event_id)::text || ']',
                               'UTF8'
                           ), 'base64'), E'\n', ''), '+', '-'), '/', '_'),
                       '='
                   ),
                   CASE WHEN value %% 11 = 0 THEN NULL
                        ELSE jsonb_build_object('ordinal', value) END,
                   'user','backlog-subject','backlog-workspace',
                   'backlog-agent','backlog-session'
            FROM generated
            """,
            (tenant_id, origin, edge_key, node_key, origin),
        )
        scoped = store.authorized(
            _authorization(tenant=tenant_id, subject="backlog-subject")
        )
        digest = hashlib.sha256()
        cursor = None
        pages = total = 0
        prior_seq = 0
        node_types: set[str] = set()
        while True:
            page = scoped.sync_page(
                "backlog-reader", "backlog-consumer", cursor, 1000
            )
            pages += 1
            assert len(page.items) <= 1000
            for item in page.items:
                assert item.stream_seq > prior_seq
                prior_seq = item.stream_seq
                total += 1
                node_types.add(item.event.node_type.value)
                digest.update(
                    json.dumps(
                        item.event.to_dict(),
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
            if not page.has_more:
                break
            now = datetime.now(timezone.utc)
            cursor = SyncCursorClaims(
                1,
                "backlog-key",
                tenant_id,
                "backlog-reader",
                "backlog-consumer",
                page.next_after_seq,
                page.snapshot_seq,
                None,
                None,
                now,
                now + timedelta(minutes=10),
            )

        assert total == 100001
        assert pages == 101
        assert node_types == {
            "function",
            "fact",
            "preference",
            "observation",
            "edge",
        }
        assert digest.hexdigest() != hashlib.sha256().hexdigest()
        assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 0
        assert resources.pool_high_watermark <= resources.pool_max_connections
        outbox_count = _admin_query(
            pg_function_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE tenant_id=%s",
            (tenant_id,),
        )[0][0]
        dead_letters = scoped.sync_status().dead_letters
        assert outbox_count == 100001
        assert dead_letters == 0
        report_path = os.environ.get("MEMPLEX_G004_REPORT_PATH")
        if report_path:
            destination = Path(report_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            report = {
                "event_count": total,
                "page_count": pages,
                "page_size": 1000,
                "duplicate_receipts": 0,
                "final_digest_sha256": digest.hexdigest(),
                "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "pool_high_watermark": resources.pool_high_watermark,
                "pool_max_connections": resources.pool_max_connections,
                "outbox_count": outbox_count,
                "dead_letters": dead_letters,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "machine": platform.machine(),
                "postgresql": _admin_query(pg_function_dsn, "SHOW server_version")[
                    0
                ][0],
            }
            temporary = destination.with_name(f".{destination.name}.tmp")
            temporary.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, destination)
    finally:
        resources.close()


def test_two_tenants_can_sync_the_same_function_id_without_cross_visibility(
    pg_function_dsn: str,
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="multi-tenant-local",
    )
    shared_id = f"same-id-{uuid.uuid4().hex}"
    tenant_a = store.authorized(
        _authorization(tenant="sync-tenant-a", subject="alice")
    )
    tenant_b = store.authorized(
        _authorization(tenant="sync-tenant-b", subject="bob")
    )
    try:
        tenant_a.sync_register_target("remote-a")
        tenant_b.sync_register_target("remote-b")
        tenant_a.add(Function(id=shared_id, name="tenant-a"), SourceDocument(type="text"))
        tenant_b.add(Function(id=shared_id, name="tenant-b"), SourceDocument(type="text"))

        assert tenant_a.get(shared_id).name == "tenant-a"
        assert tenant_b.get(shared_id).name == "tenant-b"
        assert _admin_query(
            pg_function_dsn,
            "SELECT tenant_id, count(*) FROM memplex_sync_outbox "
            "WHERE entity_key = %s GROUP BY tenant_id ORDER BY tenant_id",
            (str(SyncEntityKey.node(shared_id)),),
        ) == [("sync-tenant-a", 1), ("sync-tenant-b", 1)]
    finally:
        resources.close()


def test_two_spawned_dispatcher_processes_claim_each_delivery_once(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="process-dispatch-local",
    )
    store.sync_register_target("remote-b")
    source = SourceDocument(type="text", content="process-dispatch")
    ids = [f"process-dispatch-{uuid.uuid4().hex}" for _ in range(10)]
    for identifier in ids:
        store.add(Function(id=identifier, name=identifier), source)
    remote = _remote_store(tmp_path / "remote-process")
    simultaneous = Barrier(2, timeout=15)

    def _apply(raw: bytes) -> dict[str, object]:
        simultaneous.wait()
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        return remote.sync_apply_batch(envelope.batch).to_dict()

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    try:
        with HttpFaultProxy(_apply) as proxy:
            processes = [
                context.Process(
                    target=_dispatch_postgres_process,
                    args=(pg_function_dsn, proxy.url, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=30)
                assert process.exitcode == 0
            observed = [results.get(timeout=5) for _ in processes]

        assert proxy.errors == []
        assert sorted(observed) == [(5, 5, 0), (5, 5, 0)]
        assert store.sync_dispatch_status().pending == 0
        assert store.sync_dispatch_status().delivered == 10
        reopened = _remote_store(tmp_path / "remote-process")
        assert {node.id for node in reopened.list_functions(limit=100)} == set(ids)
        assert len(reopened._sync_repository._state["inbox"]) == 10
    finally:
        resources.close()


def test_sigterm_before_claim_drains_without_postgres_lease(
    pg_function_dsn: str,
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="sigterm-dispatch-local",
    )
    store.sync_register_target("remote-b")
    context = multiprocessing.get_context("spawn")
    results = context.Queue()

    def _unexpected_apply(_raw: bytes) -> dict[str, object]:
        raise AssertionError("idle SIGTERM must not issue a remote request")

    try:
        with HttpFaultProxy(_unexpected_apply) as proxy:
            process = context.Process(
                target=_run_dispatcher_until_sigterm,
                args=(pg_function_dsn, proxy.url, results),
            )
            process.start()
            assert results.get(timeout=10) == {"started": True}
            os.kill(process.pid, signal.SIGTERM)
            drain = results.get(timeout=10)
            process.join(timeout=10)

        assert process.exitcode == 0
        assert drain["drained"] is True
        assert drain["pending"] == 0
        assert drain["leased"] == 0
        assert drain["deadline_exceeded"] is False
        assert store.sync_dispatch_status().leased == 0
        assert proxy.errors == []
    finally:
        resources.close()


def test_sigterm_before_remote_commit_recovers_uncommitted_postgres_lease(
    pg_function_dsn: str,
    tmp_path: Path,
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="sigterm-dispatch-local",
    )
    store.sync_register_target("remote-b")
    identifier = f"sigterm-precommit-{uuid.uuid4().hex}"
    store.add(
        Function(id=identifier, name=identifier),
        SourceDocument(type="text", content="sigterm-precommit"),
    )
    remote = _remote_store(tmp_path / "remote-sigterm-precommit")
    first_request_entered = Event()
    release_first_request = Event()
    first_request_finished = Event()
    request_count = 0
    request_lock = __import__("threading").Lock()

    def _apply(raw: bytes) -> dict[str, object]:
        nonlocal request_count
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        with request_lock:
            request_count += 1
            current = request_count
        if current == 1:
            first_request_entered.set()
            assert release_first_request.wait(timeout=15)
            first_request_finished.set()
            return {
                "batch_id": envelope.batch.batch_id,
                "request_digest": envelope.batch.request_digest,
                "outcome": "accepted",
                "receipts": [
                    {"event_id": event.event_id, "outcome": "accepted"}
                    for event in envelope.batch.events
                ],
            }
        return remote.sync_apply_batch(envelope.batch).to_dict()

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    try:
        with HttpFaultProxy(_apply) as proxy:
            process = context.Process(
                target=_run_dispatcher_until_sigterm,
                args=(pg_function_dsn, proxy.url, results),
            )
            process.start()
            assert results.get(timeout=10) == {"started": True}
            assert first_request_entered.wait(timeout=10)
            os.kill(process.pid, signal.SIGTERM)
            drain = results.get(timeout=10)
            process.join(timeout=10)
            assert process.exitcode == 0
            assert drain["drained"] is False
            assert drain["leased"] == 1
            assert drain["deadline_exceeded"] is True
            assert remote.get(identifier) is None

            release_first_request.set()
            assert first_request_finished.wait(timeout=10)
            time.sleep(1.1)
            retry = SyncDispatcher(
                store,
                targets={"remote-b": proxy.url},
                local_node_id="sigterm-dispatch-local",
                http=UrllibSession(),
                claim_size=1,
                lease_seconds=1,
                request_timeout=5,
            ).dispatch_once()

        assert proxy.errors == []
        assert (retry.claimed, retry.delivered, retry.failed) == (1, 1, 0)
        assert store.sync_dispatch_status().leased == 0
        assert store.sync_dispatch_status().delivered == 1
        assert remote.get(identifier) is not None
        assert len(remote._sync_repository._state["inbox"]) == 1
    finally:
        release_first_request.set()
        resources.close()


def test_sigterm_during_remote_commit_leaves_recoverable_postgres_lease(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="sigterm-dispatch-local",
    )
    store.sync_register_target("remote-b")
    identifier = f"sigterm-{uuid.uuid4().hex}"
    store.add(
        Function(id=identifier, name=identifier),
        SourceDocument(type="text", content="sigterm"),
    )
    remote = _remote_store(tmp_path / "remote-sigterm")
    committed = Event()

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        result = remote.sync_apply_batch(envelope.batch).to_dict()
        committed.set()
        return result

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    try:
        with HttpFaultProxy(_apply) as proxy:
            proxy.enqueue(FaultAction("delay_after_commit", delay_seconds=2))
            process = context.Process(
                target=_run_dispatcher_until_sigterm,
                args=(pg_function_dsn, proxy.url, results),
            )
            process.start()
            assert results.get(timeout=10) == {"started": True}
            assert committed.wait(timeout=10)
            os.kill(process.pid, signal.SIGTERM)
            drain = results.get(timeout=10)
            process.join(timeout=10)
            assert process.exitcode == 0
            assert drain["drained"] is False
            assert drain["leased"] == 1
            assert drain["deadline_exceeded"] is True
            assert _remote_store(tmp_path / "remote-sigterm").get(identifier) is not None

            time.sleep(1.1)
            retry = SyncDispatcher(
                store,
                targets={"remote-b": proxy.url},
                local_node_id="sigterm-dispatch-local",
                http=UrllibSession(),
                claim_size=1,
                lease_seconds=1,
                request_timeout=5,
            ).dispatch_once()

        assert proxy.errors == []
        assert (retry.claimed, retry.delivered, retry.failed) == (1, 1, 0)
        assert store.sync_dispatch_status().pending == 0
        assert store.sync_dispatch_status().delivered == 1
        assert len(
            _remote_store(tmp_path / "remote-sigterm")._sync_repository._state[
                "inbox"
            ]
        ) == 1
    finally:
        resources.close()


def test_sigkill_after_remote_commit_recovers_postgres_lease_without_duplicate(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    resources, store = _required_capture_store(
        pg_function_dsn,
        local_node_id="sigterm-dispatch-local",
    )
    store.sync_register_target("remote-b")
    identifier = f"sigkill-{uuid.uuid4().hex}"
    store.add(
        Function(id=identifier, name=identifier),
        SourceDocument(type="text", content="sigkill"),
    )
    remote = _remote_store(tmp_path / "remote-sigkill")
    committed = Event()

    def _apply(raw: bytes) -> dict[str, object]:
        envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
        result = remote.sync_apply_batch(envelope.batch).to_dict()
        committed.set()
        return result

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    try:
        with HttpFaultProxy(_apply) as proxy:
            proxy.enqueue(FaultAction("delay_after_commit", delay_seconds=2))
            process = context.Process(
                target=_run_dispatcher_until_sigterm,
                args=(pg_function_dsn, proxy.url, results),
            )
            process.start()
            assert results.get(timeout=10) == {"started": True}
            assert committed.wait(timeout=10)
            os.kill(process.pid, signal.SIGKILL)
            process.join(timeout=10)
            assert process.exitcode == -signal.SIGKILL
            assert _remote_store(tmp_path / "remote-sigkill").get(identifier) is not None

            time.sleep(1.1)
            retry = SyncDispatcher(
                store,
                targets={"remote-b": proxy.url},
                local_node_id="sigterm-dispatch-local",
                http=UrllibSession(),
                claim_size=1,
                lease_seconds=1,
                request_timeout=5,
            ).dispatch_once()

        assert proxy.errors == []
        assert (retry.claimed, retry.delivered, retry.failed) == (1, 1, 0)
        assert store.sync_dispatch_status().pending == 0
        assert store.sync_dispatch_status().delivered == 1
        assert len(
            _remote_store(tmp_path / "remote-sigkill")._sync_repository._state[
                "inbox"
            ]
        ) == 1
    finally:
        resources.close()
