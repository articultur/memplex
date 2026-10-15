"""Task 14 native fixtures and owned event barriers; no production callbacks."""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from types import SimpleNamespace

import pytest

from memplex.config import SourceContractConfig
from memplex.models import QueryScope, SearchResult
from memplex.source_codec import _decode_value, _wire
from memplex.source_contract import PublishCommand, SourceContractError, SourceMutation
from memplex.storage.source_versions import _RESOURCE_ISSUER, _issue_admission
from tests.test_source_v1_pg_transactions import CONTEXT, POLICY, SCOPE, _command
from tests.test_source_v1_pg_transactions import source_backend as source_backend  # noqa: PLC0414
from tests.test_source_v1_publication import publish_command

SEAMS = (
    "after_candidate_read", "after_source_snapshot", "before_final_clock",
    "after_linearization", "before_publish_compare", "after_durable_decision",
)
OLD = "T14_OLD_BODY_CITATION_METADATA"
CONTROL = "T14_INDEPENDENT_CONTROL"
CONFIG = SourceContractConfig("v1", SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
NATIVE = pytest.mark.skipif(not os.environ.get("MEMPLEX_TEST_POSTGRES_DSN"),
                           reason="T14 native PostgreSQL evidence requires the authenticated CI database")


def deadline():
    return time.monotonic_ns() + 250_000_000


def preparation_deadline():
    """Existing two-second write setup budget; never used by measured operations."""
    return time.monotonic_ns() + 2_000_000_000


def prepare_mutation(bank, command):
    return bank.backend.mutate_source(CONTEXT, command, deadline_ns=preparation_deadline())


def prepare_publication(bank, command):
    return bank.backend.publish_if_current(CONTEXT, command, deadline_ns=preparation_deadline())


def close_lite(owner):
    """Discover a late-created queue only through the original exact owner."""
    writer = getattr(owner, "_single_writer", None)
    if writer is not None:
        writer.close(drain=True)
        assert writer._thread is None or not writer._thread.is_alive()


def make_lite(path, cleanup, *, initialize=False):
    from memplex.source_compatibility import _admit_lite, _audited_hooks, _LiteOpenAuthority
    from memplex.storage.lite.store import LiteMemoryStore

    seal = None if initialize else _LiteOpenAuthority(path, SCOPE, issuer=_RESOURCE_ISSUER)
    store = LiteMemoryStore(path, _source_open=seal)
    owner = store._durability
    cleanup(close_lite, owner)  # Before bootstrap, admission, or any lazy submission.
    backend = store.source_versions()
    if initialize:
        admission = _issue_admission(owner, SCOPE, _audited_hooks(), issuer=_RESOURCE_ISSUER)
        backend.initialize_authority(SCOPE, POLICY, admission=admission)
    _admit_lite(store, CONFIG, issuer=_RESOURCE_ISSUER)
    return SimpleNamespace(kind="lite", store=store, backend=backend, resources=None, trace=[],
                           spec={"kind": "lite", "path": str(path),
                                 "single_writer": os.environ["MEMPLEX_LITE_SINGLE_WRITER"]})


def open_peer(spec, cleanup):
    if spec["kind"] == "lite":
        from pathlib import Path
        os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = spec["single_writer"]
        return make_lite(Path(spec["path"]), cleanup)
    import psycopg2
    from psycopg2.extensions import connection

    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.pool import PostgresStorageResources
    from memplex.storage.postgres import PostgresMemoryStore

    class OwnedConnection(connection):
        """Only this resource's native connections accept commit fault interception."""

    resources = PostgresStorageResources._for_source_validity(spec["dsn"], issuer=_RESOURCE_ISSUER, capacity=2)
    cleanup(close_pg, resources)
    connect = psycopg2.connect
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(psycopg2, "connect", lambda *args, **kwargs: connect(
            *args, **dict(kwargs, connection_factory=OwnedConnection)))
        resources.ensure_ready(VectorCapabilityRequest(0, "disabled"), "development")
    store = PostgresMemoryStore(spec["dsn"], ready_pool=resources.ready_pool, require_authorization=True)
    resources._source_startup(CONFIG, store, issuer=_RESOURCE_ISSUER)
    return SimpleNamespace(kind="postgres", store=store, backend=store.authorized(CONTEXT).source_versions(),
                           resources=resources, trace=[], spec=spec)


def close_pg(resources):
    if getattr(resources, "_test_expected_fault", False):
        with pytest.raises(RuntimeError, match="faulted"):
            resources.close()
    else:
        resources.close()


@pytest.fixture
def t14_bank(request, tmp_path, monkeypatch):
    setting = request.param
    if setting == "postgres":
        store, backend, resources, trace, dsn, _ = request.getfixturevalue("source_backend")
        return SimpleNamespace(kind="postgres", store=store, backend=backend, resources=resources,
                               trace=trace, spec={"kind": "postgres", "dsn": dsn})
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", setting[-1])
    return make_lite(tmp_path / "memory.json", lambda fn, *args: request.addfinalizer(lambda: fn(*args)),
                     initialize=True)


def mutate(bank, command):
    return bank.backend.mutate_source(CONTEXT, command, deadline_ns=deadline())


def publish(bank, command):
    return bank.backend.publish_if_current(CONTEXT, command, deadline_ns=deadline())


def fence(bank):
    return bank.backend.read_fence(CONTEXT, deadline_ns=deadline())


def seed(bank, *, chain=False):
    source = prepare_mutation(bank, _command(value="source before change"))
    control = prepare_mutation(bank, _command(name="u", value=CONTROL))
    middle = None
    if chain:
        middle = prepare_publication(bank, publish_command(bank.backend, source, value="middle", key="a" * 64))
        with fence(bank) as session:
            upstream = session.source(session.resolve_id(middle.publication_id))
        command = publish_command(bank.backend, SimpleNamespace(ref=upstream.head.ref), value=OLD)
    else:
        command = publish_command(bank.backend, source, value=OLD)
    derived = prepare_publication(bank, command)
    return SimpleNamespace(source=source, control=control, middle=middle, derived=derived, command=command)


def service_for(bank, path, cleanup):
    from tests.test_factual_capture import service

    request = SimpleNamespace(addfinalizer=lambda fn: cleanup(fn))
    svc = service(path, _request=request)
    original = svc.store
    cleanup(setattr, svc, "store", original)
    svc.store = bank.store
    svc._config.source_contract = copy.deepcopy(CONFIG)
    svc._config.storage.backend = "lite" if bank.kind == "lite" else "postgres"
    svc._config.storage.path = str(bank.store._path.parent) if bank.kind == "lite" else bank.spec["dsn"]
    svc._embedding_service = svc._reranker = svc._cross_reranker = svc._llm = None
    svc._detect_scope = lambda text: QueryScope.ALL
    # No background rebuild/model is started or required by these reads.
    svc._config.llm.factual_capture = False
    svc._config.wiki.enabled = svc._config.sleep_time.enabled = False
    return svc


def nominate(monkeypatch, ids, barrier=None):
    from memplex.query_pipeline import QueryPipeline

    candidates = [SearchResult(key, OLD, OLD, 1 - i / 10, OLD,
                               graph_context={"old": OLD}, origin=OLD) for i, key in enumerate(ids)]

    def retrieve(*args):
        fixed = copy.deepcopy(candidates)
        if barrier is not None:
            barrier.arrive()
        return [fixed]

    monkeypatch.setattr(QueryPipeline, "_parallel_scope_search", retrieve)


def read(svc):
    result = svc.query("T14 source lookup", authorization=CONTEXT, explain=True)
    return {"ids": [row.func_id for row in result.results],
            "text": json.dumps(asdict(result), default=str)}


def assert_read(result, derived, *, accepted):
    assert (derived in result["ids"]) is accepted
    assert "u" in result["ids"] and CONTROL in result["text"]
    if not accepted:
        assert OLD not in result["text"]


@dataclass
class Barrier:
    name: str
    entered: object
    release: object

    @classmethod
    def create(cls, name, cleanup):
        assert name in SEAMS
        context = multiprocessing.get_context("spawn")
        gate = cls(name, context.Event(), context.Event())
        cleanup(gate.release.set)
        return gate

    def arrive(self, end=None):
        self.entered.set()
        remaining = 5 if end is None else max(0, (end - time.monotonic_ns()) / 1_000_000_000)
        assert self.release.wait(remaining), "barrier not released: " + self.name

    def reached(self):
        assert self.entered.wait(5), "barrier not reached: " + self.name


def thread_call(cleanup, fn, *, name="t14-owned-operation", releases=()):
    result = SimpleNamespace(value=None, error=None, done=threading.Event())

    def run():
        try:
            result.value = fn()
        except BaseException as exc:  # noqa: BLE001 - propagate exact owned worker failure
            result.error = exc
        finally:
            result.done.set()

    worker = threading.Thread(target=run, name=name)

    def finish():
        if worker.ident is not None:
            worker.join(5)
            assert not worker.is_alive(), "owned T14 thread did not terminate"

    def terminal_cleanup():
        for release in releases:
            release.set()
        finish()

    cleanup(terminal_cleanup)
    result.worker, result.finish = worker, finish
    worker.start()
    return result


def completed(result):
    result.finish()
    if result.error is not None:
        raise result.error
    return result.value


def reap(process):
    if process.pid is None:
        return
    process.join(5)
    if process.is_alive():
        process.terminate()
        process.join(2)
    if process.is_alive():
        process.kill()
        process.join(2)
    assert not process.is_alive()


def start_process(cleanup, target, *args, releases=()):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    cleanup(parent.close)
    cleanup(child.close)
    process = context.Process(target=target, args=(child, *args))
    def terminal_cleanup():
        for release in releases:
            release.set()
        reap(process)

    cleanup(terminal_cleanup)
    process.start()
    child.close()
    return process, parent


def receive(pipe, expected=None, *, end=None):
    remaining = 10 if end is None else max(0, (end - time.monotonic_ns()) / 1_000_000_000)
    assert pipe.poll(remaining), "owned child sent no terminal/readiness proof"
    value = pipe.recv()
    assert value.get("state") != "error", value
    if expected is not None:
        assert value["state"] == expected, value
    return value


def authoritative(bank):
    """Whole native state, outside a prior final read; synthetic data only."""
    if bank.kind == "lite":
        pair = bank.store._durability.load_authoritative()
        return asdict(pair)
    with fence(bank) as session:
        result = {}
        for table in ("memplex_source_authorities", "memplex_source_heads", "memplex_derivation_records", "memplex_facts"):
            session._execute(f"SELECT row_to_json(t) FROM {table} t ORDER BY row_to_json(t)::text")
            result[table] = session._cursor.fetchall()
        return result


def session_type(bank):
    if bank.kind == "lite":
        from memplex.storage.lite.source_versions import _LiteReadSession
        return _LiteReadSession
    from memplex.storage.postgres_sources import _PostgresReadSession
    return _PostgresReadSession


def lock_waiter(bank, cleanup):
    """Pre-open a read-only observer; no product pool capacity is expanded."""
    if bank.kind == "lite":
        return None
    import psycopg2
    connection = psycopg2.connect(bank.spec["dsn"])
    cleanup(connection.close)
    connection.autocommit = True
    return connection


def assert_pg_waiter(bank, observer, writer_pid, end):
    from memplex.storage.postgres_sources import _source_lock_key
    key = _source_lock_key(bank.store._ready_pool.target, SCOPE.authority_id)
    with closing(observer.cursor()) as cursor:
        while time.monotonic_ns() < end:
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' "
                           "AND NOT granted AND classid=%s::oid AND objid=%s::oid AND objsubid=1)",
                           (writer_pid, (key >> 32) & 0xffffffff, key & 0xffffffff))
            if cursor.fetchone()[0]:
                return
    pytest.fail("the exact PostgreSQL writer was never observed waiting on the authority lock")


def command_wire(command):
    return _wire(command)


def decode_command(value, *, publication=False):
    return _decode_value(PublishCommand if publication else SourceMutation, value)


def child_failure(pipe, exc):
    import traceback
    pipe.send({"state": "error", "type": type(exc).__name__,
               "code": str(exc.code) if isinstance(exc, SourceContractError) else None,
               "frames": [{"file": os.path.basename(frame.filename), "function": frame.name, "line": frame.lineno}
                          for frame in traceback.extract_tb(exc.__traceback__)]})
