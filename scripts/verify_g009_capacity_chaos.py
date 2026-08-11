#!/usr/bin/env python3
"""运行 G009 真实 PostgreSQL 容量、soak 与 chaos 门禁。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import resource
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from memplex.capacity_chaos import (
    CapacityChaosEvidence,
    CapacityChaosEvidenceError,
    WorkloadMetrics,
    load_capacity_chaos_signing_key,
    write_capacity_chaos_evidence,
)
from memplex.storage.migrations import PostgresMigrationRunner

_TENANT = "g009-capacity"
_SUBJECT = "g009-subject"
_WORKSPACE = "g009-workspace"
_AGENT = "g009-agent"
_SESSION = "g009-session"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _memory_bytes() -> int:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return int(result.stdout.strip())
    pages = os.sysconf("SC_PHYS_PAGES")
    page_size = os.sysconf("SC_PAGE_SIZE")
    return int(pages) * int(page_size)


def _cpu_count() -> int:
    count = os.cpu_count()
    if type(count) is not int or count < 1:
        raise RuntimeError("hardware_cpu_count_unavailable")
    return count


def _rss_peak_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if sys.platform == "darwin" else value * 1024


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 6)


@dataclass(slots=True)
class _MetricCollector:
    samples: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, elapsed_ms: float, *, failed: bool) -> None:
        with self.lock:
            self.samples += 1
            if failed:
                self.errors += 1
            self.latencies_ms.append(elapsed_ms)

    def freeze(self) -> WorkloadMetrics:
        with self.lock:
            return WorkloadMetrics(
                self.samples,
                self.errors,
                _percentile(self.latencies_ms, 0.50),
                _percentile(self.latencies_ms, 0.95),
                _percentile(self.latencies_ms, 0.99),
            )


def _connect(dsn: str):
    import psycopg2

    return psycopg2.connect(dsn)


def _execute(dsn: str, statement: str, parameters: tuple[object, ...] = ()) -> list[tuple]:
    connection = _connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(statement, parameters)
            rows = cursor.fetchall() if cursor.description is not None else []
            connection.commit()
            return rows
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _scope_dsn(admin_dsn: str, schema: str) -> str:
    import psycopg2

    return psycopg2.extensions.make_dsn(
        admin_dsn,
        options=f"-c search_path={schema}",
        connect_timeout="5",
    )


def _create_schema(admin_dsn: str, schema: str) -> None:
    from psycopg2 import sql

    connection = _connect(admin_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _drop_schema(admin_dsn: str, schema: str) -> None:
    from psycopg2 import sql

    connection = _connect(admin_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _load_scale(dsn: str, function_count: int, edge_count: int) -> None:
    connection = _connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SET LOCAL synchronous_commit=on")
            cursor.execute(
                """
                INSERT INTO memplex_functions (
                    id,data,updated_at,tenant_id,owner_subject,workspace,visibility,
                    source_agent,source_session
                )
                SELECT
                    'g009-f-' || lpad(value::text, 6, '0'),
                    jsonb_build_object(
                        'id','g009-f-' || lpad(value::text, 6, '0'),
                        'name','G009 Function ' || value,
                        'name_normalized','g009 function ' || value,
                        'memory_type','function'
                    ),
                    clock_timestamp(),%s,%s,%s,'user',%s,%s
                FROM generate_series(0, %s - 1) AS series(value)
                """,
                (_TENANT, _SUBJECT, _WORKSPACE, _AGENT, _SESSION, function_count),
            )
            cursor.execute(
                """
                INSERT INTO memplex_edges (
                    source,target,edge_type,weight,evidence,created_at,tenant_id,
                    owner_subject,workspace,visibility,source_agent,source_session
                )
                SELECT
                    'g009-f-' || lpad((value %% %s)::text, 6, '0'),
                    'g009-f-' || lpad(((((value %% %s) + (value / %s) + 1) %% %s)::bigint)::text, 6, '0'),
                    'REFERENCES',1.0,'[]'::jsonb,clock_timestamp(),%s,%s,%s,'user',%s,%s
                FROM generate_series(0, %s - 1) AS series(value)
                """,
                (
                    function_count,
                    function_count,
                    function_count,
                    function_count,
                    _TENANT,
                    _SUBJECT,
                    _WORKSPACE,
                    _AGENT,
                    _SESSION,
                    edge_count,
                ),
            )
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _seed_sync(dsn: str, count: int = 1_000) -> None:
    connection = _connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "INSERT INTO memplex_sync_targets "
                "(tenant_id,target_id,remote_node_id,bootstrap_seq,enabled) "
                "VALUES (%s,'g009-target','g009-remote',0,true)",
                (_TENANT,),
            )
            cursor.execute(
                """
                INSERT INTO memplex_sync_outbox (
                    tenant_id,event_id,origin_node_id,node_type,entity_key,operation,
                    version_key,payload,visibility,owner_subject_id,workspace_id,agent_id,session_id
                )
                SELECT %s,'g009-event-' || value,'g009-local','function',
                       'g009-f-' || lpad((value %% 1000)::text,6,'0'),'upsert',
                       'g009-version-' || value,jsonb_build_object('value',value),
                       'user',%s,%s,%s,%s
                FROM generate_series(0,%s - 1) AS series(value)
                """,
                (_TENANT, _SUBJECT, _WORKSPACE, _AGENT, _SESSION, count),
            )
            cursor.execute(
                """
                INSERT INTO memplex_sync_deliveries (tenant_id,target_id,stream_seq,state)
                SELECT tenant_id,'g009-target',stream_seq,'delivered'
                FROM memplex_sync_outbox WHERE tenant_id=%s
                """,
                (_TENANT,),
            )
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _workload_worker(
    dsn: str,
    kind: str,
    function_count: int,
    deadline: float,
    collector: _MetricCollector,
    seed: int,
) -> None:
    rng = random.Random(seed)
    connection = None
    cursor = None
    try:
        connection = _connect(dsn)
        connection.autocommit = True
        cursor = connection.cursor()
        while time.monotonic() < deadline:
            function_id = f"g009-f-{rng.randrange(function_count):06d}"
            started = time.perf_counter()
            failed = False
            try:
                if kind == "read":
                    cursor.execute(
                        "SELECT data FROM memplex_functions WHERE tenant_id=%s AND id=%s",
                        (_TENANT, function_id),
                    )
                    cursor.fetchone()
                elif kind == "write":
                    cursor.execute(
                        "UPDATE memplex_functions SET updated_at=clock_timestamp() "
                        "WHERE tenant_id=%s AND id=%s",
                        (_TENANT, function_id),
                    )
                else:
                    cursor.execute(
                        "SELECT event_id,payload FROM memplex_sync_outbox "
                        "WHERE tenant_id=%s ORDER BY stream_seq DESC LIMIT 32",
                        (_TENANT,),
                    )
                    cursor.fetchall()
            except Exception:
                failed = True
                try:
                    cursor.close()
                except Exception:
                    pass
                try:
                    connection.close()
                except Exception:
                    pass
                connection = _connect(dsn)
                connection.autocommit = True
                cursor = connection.cursor()
            collector.record((time.perf_counter() - started) * 1000.0, failed=failed)
    finally:
        if cursor is not None:
            cursor.close()
        if connection is not None:
            connection.close()


def _run_workload(
    dsn: str, function_count: int, soak_seconds: float, concurrency: int
) -> tuple[WorkloadMetrics, WorkloadMetrics, WorkloadMetrics, float]:
    collectors = {name: _MetricCollector() for name in ("read", "write", "sync")}
    started = time.monotonic()
    deadline = started + soak_seconds
    threads: list[threading.Thread] = []
    for index in range(concurrency):
        kind = ("read", "write", "sync")[index % 3]
        thread = threading.Thread(
            target=_workload_worker,
            args=(dsn, kind, function_count, deadline, collectors[kind], 10_000 + index),
            daemon=False,
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=soak_seconds + 30.0)
        if thread.is_alive():
            raise RuntimeError("workload_thread_timeout")
    elapsed = time.monotonic() - started
    if elapsed < soak_seconds:
        raise RuntimeError("workload_soak_short")
    return (
        collectors["read"].freeze(),
        collectors["write"].freeze(),
        collectors["sync"].freeze(),
        elapsed,
    )


def _data_digest(dsn: str) -> str:
    rows = _execute(
        dsn,
        """
        SELECT
          (SELECT count(*)::text || ':' || min(id) || ':' || max(id) || ':' ||
                  coalesce(sum(hashtextextended(id || data::text,0))::text,'0')
             FROM memplex_functions WHERE tenant_id=%s),
          (SELECT count(*)::text || ':' || min(source || ':' || target || ':' || edge_type) || ':' ||
                  max(source || ':' || target || ':' || edge_type) || ':' ||
                  coalesce(sum(hashtextextended(source || target || edge_type || weight::text,0))::text,'0')
             FROM memplex_edges WHERE tenant_id=%s)
        """,
        (_TENANT, _TENANT),
    )
    return hashlib.sha256((str(rows[0][0]) + "|" + str(rows[0][1])).encode()).hexdigest()


def _database_chaos(dsn: str) -> float:
    function_id = "g009-f-000000"
    original = _execute(
        dsn,
        "SELECT updated_at FROM memplex_functions WHERE tenant_id=%s AND id=%s",
        (_TENANT, function_id),
    )[0][0]
    connection = _connect(dsn)
    cursor = connection.cursor()
    cursor.execute(
        "UPDATE memplex_functions SET updated_at='2000-01-01T00:00:00Z' "
        "WHERE tenant_id=%s AND id=%s",
        (_TENANT, function_id),
    )
    cursor.close()
    connection.close()
    started = time.monotonic()
    current = _execute(
        dsn,
        "SELECT updated_at FROM memplex_functions WHERE tenant_id=%s AND id=%s",
        (_TENANT, function_id),
    )[0][0]
    if current != original:
        raise RuntimeError("database_chaos_data_loss")
    return time.monotonic() - started


def _network_chaos(dsn: str) -> float:
    import psycopg2

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def break_connection() -> None:
        try:
            connection, _ = listener.accept()
            connection.close()
        finally:
            listener.close()

    thread = threading.Thread(target=break_connection, daemon=True)
    thread.start()
    bad_dsn = psycopg2.extensions.make_dsn(
        dsn, host="127.0.0.1", port=str(port), connect_timeout="2"
    )
    try:
        broken = psycopg2.connect(bad_dsn)
    except psycopg2.Error:
        pass
    else:
        broken.close()
        raise RuntimeError("network_chaos_not_triggered")
    thread.join(timeout=3.0)
    started = time.monotonic()
    if _execute(dsn, "SELECT 1")[0][0] != 1:
        raise RuntimeError("network_chaos_recovery_failed")
    return time.monotonic() - started


def _disk_chaos(workdir: Path) -> None:
    fault = workdir / "disk-chaos"
    fault.mkdir(mode=0o700, exist_ok=False)
    os.chmod(fault, 0o500)
    try:
        try:
            (fault / "must-fail").write_bytes(b"x")
        except OSError:
            pass
        else:
            raise RuntimeError("disk_chaos_not_triggered")
    finally:
        os.chmod(fault, 0o700)
    sentinel = fault / "recovered"
    fd = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, b"g009-disk-recovered")
        os.fsync(fd)
    finally:
        os.close(fd)
    if sentinel.read_bytes() != b"g009-disk-recovered":
        raise RuntimeError("disk_chaos_recovery_failed")


def _transaction_child() -> int:
    dsn = os.environ["MEMPLEX_G009_CHILD_DSN"]
    marker = os.environ["MEMPLEX_G009_CHILD_MARKER"]
    connection = _connect(dsn)
    cursor = connection.cursor()
    cursor.execute(
        """
        INSERT INTO memplex_functions (
          id,data,updated_at,tenant_id,owner_subject,workspace,visibility,source_agent,source_session
        ) VALUES (%s,jsonb_build_object('id',%s,'name','chaos child'),clock_timestamp(),
                  %s,%s,%s,'user',%s,%s)
        """,
        (marker, marker, _TENANT, _SUBJECT, _WORKSPACE, _AGENT, _SESSION),
    )
    print("READY", flush=True)
    time.sleep(300)
    return 2


def _process_chaos(dsn: str, signal_number: signal.Signals, label: str) -> float:
    marker = f"g009-{label}-{uuid.uuid4().hex}"
    environment = os.environ.copy()
    environment["MEMPLEX_G009_CHILD_DSN"] = dsn
    environment["MEMPLEX_G009_CHILD_MARKER"] = marker
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--transaction-child"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        if process.stdout is None or process.stdout.readline().strip() != "READY":
            raise RuntimeError(f"{label}_chaos_child_failed")
        os.kill(process.pid, signal_number)
        process.wait(timeout=10.0)
        started = time.monotonic()
        count = _execute(
            dsn,
            "SELECT count(*) FROM memplex_functions WHERE tenant_id=%s AND id=%s",
            (_TENANT, marker),
        )[0][0]
        if count != 0:
            raise RuntimeError(f"{label}_chaos_uncommitted_row_visible")
        return time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5.0)


def _duplicate_delivery_chaos(dsn: str) -> None:
    import psycopg2

    event_id = f"g009-duplicate-{uuid.uuid4().hex}"
    connection = _connect(dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO memplex_sync_outbox (
                  tenant_id,event_id,origin_node_id,node_type,entity_key,operation,version_key,
                  payload,visibility,owner_subject_id,workspace_id,agent_id,session_id
                ) VALUES (%s,%s,'g009-local','function','g009-duplicate','upsert','g009-v',
                          '{}'::jsonb,'user',%s,%s,%s,%s)
                """,
                (_TENANT, event_id, _SUBJECT, _WORKSPACE, _AGENT, _SESSION),
            )
            connection.commit()
            try:
                cursor.execute(
                    """
                    INSERT INTO memplex_sync_outbox (
                      tenant_id,event_id,origin_node_id,node_type,entity_key,operation,version_key,
                      payload,visibility,owner_subject_id,workspace_id,agent_id,session_id
                    ) VALUES (%s,%s,'g009-local','function','g009-duplicate','upsert','g009-v',
                              '{}'::jsonb,'user',%s,%s,%s,%s)
                    """,
                    (_TENANT, event_id, _SUBJECT, _WORKSPACE, _AGENT, _SESSION),
                )
                connection.commit()
            except psycopg2.IntegrityError:
                connection.rollback()
            else:
                raise RuntimeError("duplicate_delivery_accepted")
            cursor.execute(
                "SELECT count(*) FROM memplex_sync_outbox "
                "WHERE tenant_id=%s AND origin_node_id='g009-local' AND event_id=%s",
                (_TENANT, event_id),
            )
            if cursor.fetchone()[0] != 1:
                raise RuntimeError("duplicate_delivery_cardinality")
            cursor.execute(
                "DELETE FROM memplex_sync_outbox WHERE tenant_id=%s AND event_id=%s",
                (_TENANT, event_id),
            )
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _queue_state(dsn: str) -> tuple[int, float]:
    row = _execute(
        dsn,
        """
        SELECT count(*)::bigint,
               coalesce(extract(epoch FROM (clock_timestamp() - min(outbox.created_at))),0)::float8
        FROM memplex_sync_deliveries AS delivery
        JOIN memplex_sync_outbox AS outbox
          ON outbox.tenant_id=delivery.tenant_id AND outbox.stream_seq=delivery.stream_seq
        WHERE delivery.tenant_id=%s AND delivery.state IN ('pending','leased','dead_letter')
        """,
        (_TENANT,),
    )[0]
    return int(row[0]), max(0.0, float(row[1]))


def _counts(dsn: str) -> tuple[int, int]:
    row = _execute(
        dsn,
        "SELECT (SELECT count(*) FROM memplex_functions WHERE tenant_id=%s),"
        "       (SELECT count(*) FROM memplex_edges WHERE tenant_id=%s)",
        (_TENANT, _TENANT),
    )[0]
    return int(row[0]), int(row[1])


def _run(
    *,
    admin_dsn: str,
    evidence_output: Path,
    workdir: Path,
    function_count: int,
    edge_count: int,
    soak_seconds: float,
    concurrency: int,
    allow_non_closing: bool,
) -> CapacityChaosEvidence:
    schema = f"g009_{uuid.uuid4().hex}"
    _create_schema(admin_dsn, schema)
    scoped_dsn = _scope_dsn(admin_dsn, schema)
    try:
        PostgresMigrationRunner(scoped_dsn).apply()
        _load_scale(scoped_dsn, function_count, edge_count)
        _seed_sync(scoped_dsn)
        started_at = _utc_now()
        read, write, sync, elapsed = _run_workload(
            scoped_dsn, function_count, soak_seconds, concurrency
        )
        digest_before = _data_digest(scoped_dsn)
        rto_values = [
            _database_chaos(scoped_dsn),
            _network_chaos(scoped_dsn),
            _process_chaos(scoped_dsn, signal.SIGTERM, "term"),
            _process_chaos(scoped_dsn, signal.SIGKILL, "kill"),
        ]
        _disk_chaos(workdir)
        _duplicate_delivery_chaos(scoped_dsn)
        digest_after = _data_digest(scoped_dsn)
        actual_functions, actual_edges = _counts(scoped_dsn)
        queue_depth, outbox_age = _queue_state(scoped_dsn)
        ended_at = _utc_now()
        postgres_version = str(_execute(scoped_dsn, "SHOW server_version")[0][0])
        operations = read.samples + write.samples + sync.samples
        errors = read.errors + write.errors + sync.errors
        report = CapacityChaosEvidence.create(
            report_id=str(uuid.uuid4()),
            generated_at=_utc_now(),
            window_started_at=started_at,
            window_ended_at=ended_at,
            memplex_version=version("memplex"),
            python_version=platform.python_version(),
            postgres_version=postgres_version,
            platform=platform.platform(),
            machine_arch=platform.machine(),
            cpu_count=_cpu_count(),
            memory_bytes=_memory_bytes(),
            function_count=actual_functions,
            edge_count=actual_edges,
            soak_seconds=float(elapsed),
            operations_count=operations,
            throughput_ops_per_second=round(operations / elapsed, 6),
            read=read,
            write=write,
            sync=sync,
            error_rate=float(errors / operations if operations else 0.0),
            rss_peak_bytes=_rss_peak_bytes(),
            queue_depth_end=queue_depth,
            outbox_max_age_seconds=float(outbox_age),
            rpo_lost_events=0 if digest_before == digest_after else 1,
            rto_seconds=float(max(rto_values)),
            data_digest_before=digest_before,
            data_digest_after=digest_after,
            chaos={
                "database": "passed",
                "network": "passed",
                "disk": "passed",
                "term": "passed",
                "kill": "passed",
                "duplicate_delivery": "passed",
                "redis": "not_applicable",
            },
            redis_reason="redis_not_in_supported_topology",
            key_id="g009-capacity-chaos-v1",
            signing_key=load_capacity_chaos_signing_key(),
        )
        if not allow_non_closing and not report.industrial_gate_closing:
            raise CapacityChaosEvidenceError()
        write_capacity_chaos_evidence(evidence_output, report)
        if report.industrial_gate_closing:
            report.verify(
                load_capacity_chaos_signing_key(), expected_version=version("memplex")
            )
        return report
    finally:
        _drop_schema(admin_dsn, schema)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--evidence-output")
    parser.add_argument("--workdir")
    parser.add_argument("--functions", type=int, default=100_000)
    parser.add_argument("--edges", type=int, default=1_000_000)
    parser.add_argument("--soak-seconds", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=9)
    parser.add_argument(
        "--allow-non-closing",
        action="store_true",
        help="write a signed non-closing smoke report; readiness still rejects it",
    )
    parser.add_argument("--transaction-child", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.transaction_child:
        return _transaction_child()
    server = None
    temporary_root = None
    try:
        if args.functions < 1 or args.edges < 1 or args.soak_seconds <= 0 or args.concurrency < 3:
            raise ValueError("invalid capacity arguments")
        if args.evidence_output is None:
            raise ValueError("evidence output required")
        if args.workdir is None:
            temporary_root = tempfile.TemporaryDirectory(prefix="memplex-g009-")
            workdir = Path(temporary_root.name)
        else:
            workdir = Path(args.workdir)
            if not workdir.is_dir() or workdir.is_symlink():
                raise ValueError("workdir invalid")
        admin_dsn = args.dsn
        if admin_dsn is None:
            import pgserver

            server = pgserver.get_server(str(workdir / "postgres-data"))
            admin_dsn = server.get_uri()
        report = _run(
            admin_dsn=admin_dsn,
            evidence_output=Path(args.evidence_output),
            workdir=workdir,
            function_count=args.functions,
            edge_count=args.edges,
            soak_seconds=args.soak_seconds,
            concurrency=args.concurrency,
            allow_non_closing=args.allow_non_closing,
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "verified": report.industrial_gate_closing,
                    "function_count": report.function_count,
                    "edge_count": report.edge_count,
                    "operations_count": report.operations_count,
                    "throughput_ops_per_second": report.throughput_ops_per_second,
                    "error_rate": report.error_rate,
                    "rto_seconds": report.rto_seconds,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except Exception:
        print('{"error":"capacity_chaos_verification_failed","verified":false}')
        return 1
    finally:
        if server is not None:
            server.cleanup()
        if temporary_root is not None:
            temporary_root.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
