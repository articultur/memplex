"""T14 event-proven native C/R/L orders and stale worker incarnation fences."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import ExitStack

import pytest

from memplex.source_contract import SourceContractError, SourceOperation
from tests.source_v1_concurrency_support import (
    CONTEXT,
    NATIVE,
    Barrier,
    assert_pg_waiter,
    assert_read,
    authoritative,
    child_failure,
    command_wire,
    completed,
    deadline,
    decode_command,
    fence,
    lock_waiter,
    mutate,
    nominate,
    open_peer,
    publish,
    read,
    receive,
    seed,
    service_for,
    session_type,
    start_process,
    thread_call,
)
from tests.source_v1_concurrency_support import t14_bank as t14_bank  # noqa: PLC0414
from tests.test_source_v1_pg_transactions import _command
from tests.test_source_v1_pg_transactions import source_backend as source_backend  # noqa: PLC0414
from tests.test_source_v1_publication import publish_command


def _order_cases():
    cases = []
    for setting in ("lite0", "lite1"):
        for topology in ("same", "instance", "process"):
            for order in ("C<R", "R<C<L", "L<C"):
                cases.append(pytest.param(setting, topology, order, id=f"{setting}-{topology}-{order}"))
        cases.append(pytest.param(setting, "instance", "mid_closure", id=setting + "-mid-closure"))
    for topology, order in [("same", order) for order in ("C<R", "R<C<L", "L<C", "mid_closure")] + [
            ("instance", "R<C<L"), ("process", "C<R")]:
        cases.append(pytest.param("postgres", topology, order, marks=NATIVE, id=f"pg-{topology}-{order}"))
    return cases


def _instrument_reader(bank, patch, gate, middle_id, proof, *, reader_name):
    from memplex.query_pipeline import QueryPipeline

    cls = session_type(bank)
    source, clock = cls.source, cls.utc_now
    finalize = QueryPipeline._finalize
    # PostgreSQL release checks only enforce lifetime/budget; SQL uses _execute.
    access_name = "_execute" if bank.kind == "postgres" else "_check"
    access = getattr(cls, access_name)
    active = False

    def final_projection(pipeline, result):
        nonlocal active
        if threading.current_thread().name != reader_name:
            return finalize(pipeline, result)
        active = True
        try:
            return finalize(pipeline, result)
        finally:
            active = False

    def owned():
        return active and threading.current_thread().name == reader_name

    def accessed(session, *args, **kwargs):
        if owned():
            assert "L" not in proof, "storage access after final L"
        return access(session, *args, **kwargs)

    def snapshot(session, key, *args, **kwargs):
        if owned():
            proof["business_deadline_ns"] = session._deadline_ns
            assert "L" not in proof, "source lookup after final L"
        result = source(session, key, *args, **kwargs)
        if owned():
            proof.setdefault("source_ids", []).append(key.logical_id)
            if result is not None:
                proof.setdefault("source_vectors", {})[key.logical_id] = command_wire(result.head.ref)
            if gate is not None and gate.name == "after_source_snapshot" and key.logical_id == middle_id:
                proof["after_source_snapshot"] = True
                gate.arrive(session._deadline_ns)
        return result

    def final_clock(session):
        if not owned():
            return clock(session)
        proof["before_final_clock"] = True
        proof["business_deadline_ns"] = session._deadline_ns
        at = clock(session)
        proof["L"] = at.isoformat()
        proof["after_linearization"] = True
        if bank.kind == "postgres":
            proof["reader_backend_pid"] = session._cursor.connection.get_backend_pid()
        if gate is not None and gate.name == "after_linearization":
            gate.arrive(session._deadline_ns)
        return at

    patch.setattr(cls, "source", snapshot)
    patch.setattr(cls, "utc_now", final_clock)
    patch.setattr(cls, access_name, accessed)
    patch.setattr(QueryPipeline, "_finalize", final_projection)


def _reader_child(pipe, spec, ids, gate, middle_id, path):
    try:
        with ExitStack() as stack, pytest.MonkeyPatch.context() as patch:
            bank = open_peer(spec, stack.callback)
            svc = service_for(bank, path, stack.callback)
            nominate(patch, ids, gate if gate is not None and gate.name == "after_candidate_read" else None)
            proof = {"pid": os.getpid()}
            _instrument_reader(bank, patch, gate, middle_id, proof, reader_name=threading.current_thread().name)
            pipe.send({"state": "ready", "pid": os.getpid()})
            assert pipe.recv() == "start"
            result = read(svc)
            pipe.send({"state": "read", "result": result, "proof": proof})
    except BaseException as exc:  # noqa: BLE001 - report only synthetic child failure identity
        child_failure(pipe, exc)
    finally:
        pipe.close()


def _observe_contender(bank, patch, event, proof, *, cross_process):
    if bank.kind == "postgres":
        cls = session_type(bank)
        execute = cls._execute

        def observed(session, sql, parameters=()):
            if threading.current_thread().name == "t14-writer" and "SELECT pg_advisory_xact_lock(" in sql:
                proof["writer_backend_pid"] = session._cursor.connection.get_backend_pid()
                event.set()
            return execute(session, sql, parameters)

        patch.setattr(cls, "_execute", observed)
        return
    from memplex.storage.lite import durability

    owner = bank.store._durability

    def writer_thread():
        queue = getattr(owner, "_single_writer", None)
        return (threading.current_thread().name == "t14-writer"
                or queue is not None and threading.get_ident() == queue._writer_ident)

    if cross_process:
        fcntl = durability._load_fcntl()
        flock = fcntl.flock

        def observed_flock(fd, operation):
            try:
                return flock(fd, operation)
            except BlockingIOError:
                if writer_thread() and operation == fcntl.LOCK_EX | fcntl.LOCK_NB:
                    assert os.fstat(fd).st_ino == owner._lock_path.stat().st_ino
                    proof["native_flock_contended"] = True
                    event.set()
                raise

        patch.setattr(fcntl, "flock", observed_flock)
        return
    original = durability._process_lock_for
    exact = original(owner._memory_path)

    class OwnedLock:
        def acquire(self, *args, **kwargs):
            if writer_thread() and not event.is_set():
                acquired = exact.acquire(blocking=False)
                if acquired:
                    exact.release()
                assert not acquired, "the competing thread did not encounter the actual held lock"
                proof["native_process_lock_contended"] = True
                event.set()
            return exact.acquire(*args, **kwargs)

        def release(self):
            exact.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *args):
            self.release()

    lock = OwnedLock()
    patch.setattr(durability, "_process_lock_for", lambda path: lock if path == owner._memory_path else original(path))


def _hold_pg_commit(bank, patch, gate, proof):
    if bank.kind != "postgres":
        return
    cls = type(bank.store._pool_manager._pool._pool[0])
    commit = cls.commit

    def committed(connection):
        commit(connection)
        if threading.current_thread().name == "t14-writer":
            proof["writer_backend_pid"] = connection.get_backend_pid()
            proof["after_durable_decision"] = True
            gate.arrive()

    patch.setattr(cls, "commit", committed)


def _run_order(bank, topology, order, tmp_path, patch, stack):
    data = seed(bank, chain=order == "mid_closure")
    ids = [data.derived.publication_id, "u"]
    reader_bank = open_peer(bank.spec, stack.callback) if topology == "instance" else bank
    svc = service_for(reader_bank, tmp_path / "reader-service", stack.callback)
    proof = {"pid": os.getpid(), "topology": topology, "order": order}
    seam = {"R<C<L": "after_candidate_read", "L<C": "after_linearization",
            "mid_closure": "after_source_snapshot"}.get(order)
    gate = Barrier.create(seam, stack.callback) if seam else None
    commit_gate = Barrier.create("after_durable_decision", stack.callback)
    nominate(patch, ids, gate if seam == "after_candidate_read" else None)
    _instrument_reader(reader_bank, patch, gate, (data.middle.publication_id if data.middle is not None else None), proof, reader_name="t14-reader")
    observer = lock_waiter(bank, stack.callback)
    contender = threading.Event()
    if order in {"L<C", "mid_closure"}:
        _observe_contender(bank, patch, contender, proof, cross_process=topology == "process")
    else:
        _hold_pg_commit(bank, patch, commit_gate, proof)
    command = _command(value="committed replacement", expected=data.source.ref.token, operation=SourceOperation.REPLACE)
    process = pipe = reader = writer = None
    if topology == "process":
        process, pipe = start_process(stack.callback, _reader_child, bank.spec, ids, gate,
                                      (data.middle.publication_id if data.middle is not None else None), tmp_path / "child-service",
                                      releases=(gate.release,) if gate else ())
        receive(pipe, "ready")
    if order != "C<R":
        if pipe:
            pipe.send("start")
        else:
            reader = thread_call(stack.callback, lambda: read(svc), name="t14-reader", releases=(gate.release,))
        gate.reached()
    writer = thread_call(stack.callback, lambda: mutate(bank, command), name="t14-writer",
                         releases=(commit_gate.release,) + ((gate.release,) if gate else ()))
    if order in {"L<C", "mid_closure"}:
        assert contender.wait(.2), "the writer did not enter the native lock boundary"
        if bank.kind == "postgres":
            assert_pg_waiter(bank, observer, proof["writer_backend_pid"], proof["business_deadline_ns"])
            proof["server_visible_waiter"] = True
        assert not writer.done.is_set()
        gate.release.set()
    else:
        if bank.kind == "postgres":
            commit_gate.reached()
        else:
            completed(writer)
            proof["after_durable_decision"] = True
        if order == "C<R":
            if pipe:
                pipe.send("start")
            else:
                reader = thread_call(stack.callback, lambda: read(svc), name="t14-reader")
        else:
            gate.release.set()
    if pipe:
        response = receive(pipe, "read")
        first = response["result"]
        proof.update(response["proof"])
    else:
        first = completed(reader)
    commit_gate.release.set()
    completed(writer)
    if process is not None:
        process.join(5)
        assert process.exitcode == 0
    assert_read(first, data.derived.publication_id, accepted=order in {"L<C", "mid_closure"})
    assert_read(read(svc), data.derived.publication_id, accepted=False)
    if bank.kind == "postgres":
        assert proof["reader_backend_pid"] != proof["writer_backend_pid"]
    assert proof["before_final_clock"] and proof["after_linearization"]
    if order == "mid_closure":
        assert proof["source_vectors"][data.source.ref.key.logical_id] == command_wire(data.source.ref)
    return proof


@pytest.mark.parametrize("t14_bank,topology,order", _order_cases(), indirect=["t14_bank"])
def test_source_commit_orders(t14_bank, topology, order, tmp_path, monkeypatch, record_property):
    with ExitStack() as stack, monkeypatch.context() as patch:
        proof = _run_order(t14_bank, topology, order, tmp_path, patch, stack)
    record_property("interleaving", json.dumps(proof, sort_keys=True))


def _publish_child(pipe, spec, command, gate):
    try:
        with ExitStack() as stack, pytest.MonkeyPatch.context() as patch:
            bank = open_peer(spec, stack.callback)
            pending = decode_command(command, publication=True)
            module = __import__("memplex.storage.lite.source_versions" if bank.kind == "lite"
                                else "memplex.storage.postgres_sources", fromlist=["_make_publication"])
            original = module._make_publication
            proof = {"pid": os.getpid(), "actual_compare": False}
            if bank.kind == "postgres":
                cls = session_type(bank)
                execute = cls._execute

                def observed_execute(session, sql, parameters=()):
                    proof["backend_pid"] = session._cursor.connection.get_backend_pid()
                    return execute(session, sql, parameters)

                patch.setattr(cls, "_execute", observed_execute)

            def compare(*args, **kwargs):
                proof["actual_compare"] = True
                return original(*args, **kwargs)

            patch.setattr(module, "_make_publication", compare)
            pipe.send({"state": "ready", "pid": os.getpid()})
            gate.arrive()
            try:
                receipt = publish(bank, pending)
            except SourceContractError as exc:
                pipe.send({"state": "rejected", "code": str(exc.code), "proof": proof})
            else:
                pipe.send({"state": "unexpected_receipt", "receipt": command_wire(receipt)})
    except BaseException as exc:  # noqa: BLE001 - preserve child failure separately from expected conflict
        child_failure(pipe, exc)
    finally:
        pipe.close()


def _recreate(bank, data, target):
    if target == "source":
        deleted = mutate(bank, _command(expected=data.source.ref.token, operation=SourceOperation.DELETE))
        recreated = mutate(bank, _command(value="source before change", expected=deleted.ref.token,
                                          operation=SourceOperation.RECREATE))
        assert recreated.ref.token.incarnation != data.source.ref.token.incarnation
        return recreated
    deleted = bank.backend.delete_target(CONTEXT, data.command.derivation_key, data.derived.target, deadline_ns=deadline())
    fresh = publish_command(bank.backend, data.source, expected=deleted.token, value="recreated current body")
    recreated = bank.backend.recreate_target(CONTEXT, fresh, deleted.token, deadline_ns=deadline())
    assert recreated.target.incarnation != data.derived.target.incarnation
    assert recreated.target.generation == data.derived.target.generation == 1
    return recreated


@pytest.mark.parametrize("t14_bank", ["lite0", "lite1", pytest.param("postgres", marks=NATIVE)], indirect=True)
@pytest.mark.parametrize("target", ["source", "target"])
def test_old_worker_after_delete_recreate(t14_bank, target, tmp_path, monkeypatch, record_property):
    bank = t14_bank
    data = seed(bank)
    old = publish_command(bank.backend, data.source, expected=data.derived.target)
    with ExitStack() as stack:
        gate = Barrier.create("before_publish_compare", stack.callback)
        # Spawned PG owns an independently opened fixed pool, never inherited connections.
        child, pipe = start_process(stack.callback, _publish_child, bank.spec, command_wire(old), gate,
                                    releases=(gate.release,))
        child_ready = receive(pipe, "ready")
        gate.reached()
        recreated = _recreate(bank, data, target)
        writer_pid = next((row[1] for row in reversed(bank.trace) if row[0] == "commit"), None)
        before = authoritative(bank)
        gate.release.set()
        result = receive(pipe, "rejected")
        child.join(5)
        assert child.exitcode == 0
        assert result["code"] == ("source_changed" if target == "source" else "publication_conflict")
        assert result["proof"]["actual_compare"]
        if bank.kind == "postgres":
            assert writer_pid and writer_pid != result["proof"]["backend_pid"]
        assert authoritative(bank) == before
        svc = service_for(bank, tmp_path / "service", stack.callback)
        ids = [data.derived.publication_id, "u"]
        if target == "target":
            ids.append(recreated.publication_id)
        nominate(monkeypatch, ids)
        current_read = read(svc)
        assert_read(current_read, data.derived.publication_id, accepted=False)
        if target == "target":
            assert recreated.publication_id in current_read["ids"]
            assert "recreated current body" in current_read["text"]
            with fence(bank) as session:
                assert session.target(data.command.derivation_key).token == recreated.target
        record_property("old_worker", json.dumps({"target": target, "parent_pid": os.getpid(),
                        "child_pid": child_ready["pid"], "outcome": result}, sort_keys=True))
