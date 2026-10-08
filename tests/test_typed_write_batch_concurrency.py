"""Deterministic, process-bounded tests of the real typed-batch writer boundary.

Removing a lock, refreshing before acquisition, restoring a stale snapshot, or
allowing deferred entry during staging must break these serial-outcome checks.
"""

from __future__ import annotations

import faulthandler
import json
import multiprocessing
import os
import signal
import threading
import time
import traceback
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pytest

from memplex.storage.lite.durability import LitePair
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.typed_batch import TypedBatchInput
from tests.helpers.typed_batch import literal_plan, make_fact, make_preference
from tests.test_typed_write_batch_recovery import (
    _assert_disk_pair,
    _assert_reopened,
    _close,
    _configure,
    _pair_data,
)

_WAIT = 10
_OUTER_LIMIT = 30


def _diagnostics(root):
    return "\n".join(f"{path.name}:\n{path.read_text(encoding='utf-8')}" for path in Path(root).glob("*.log"))


def _stop_process(process, *, owned_group=False):
    if owned_group:
        # The outer child creates its own session before spawning any peers.
        # Kill that owned process group even if the coordinator died first.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        process.join(2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.join(2)
    if process.is_alive():
        process.terminate()
        process.join(2)
    if process.is_alive():
        process.kill()
        process.join(2)


def _bounded_child(root, target, args):
    """Persist all stacks before the parent enforces its 30-second deadline."""
    os.setsid()
    (Path(root) / "owned-process-group.txt").write_text(str(os.getpid()), encoding="utf-8")
    with (Path(root) / "outer-stacks.log").open("w", encoding="utf-8") as evidence:
        faulthandler.enable(file=evidence)
        faulthandler.dump_traceback_later(24, file=evidence)
        try:
            target(Path(root), *args)
            (Path(root) / "completed.txt").write_text("passed", encoding="utf-8")
        except BaseException:
            traceback.print_exc(file=evidence)
            faulthandler.dump_traceback(file=evidence, all_threads=True)
            evidence.flush()
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()


def _run_bounded(root, target, *args):
    process = multiprocessing.get_context("spawn").Process(target=_bounded_child, args=(str(root), target, args))
    process.start()
    try:
        process.join(_OUTER_LIMIT)
        assert not process.is_alive(), f"30-second outer timeout; evidence:\n{_diagnostics(root)}"
        assert process.exitcode == 0, f"Child failed; evidence:\n{_diagnostics(root)}"
        assert (root / "completed.txt").read_text(encoding="utf-8") == "passed"
    finally:
        group_marker = root / "owned-process-group.txt"
        owned_group = group_marker.exists() and group_marker.read_text(encoding="utf-8") == str(process.pid)
        _stop_process(process, owned_group=owned_group)
        process.close()


def _join_threads(workers, errors):
    deadline = time.monotonic() + _WAIT
    for worker in workers:
        worker.join(max(0, deadline - time.monotonic()))
    assert not [worker.name for worker in workers if worker.is_alive()], "Threads did not join within 10 seconds"
    assert not errors, "\n".join(errors)


def _thread_target(operation, results, errors, name, finished):
    try:
        results[name] = operation()
    except BaseException:  # noqa: BLE001 - preserve worker failures for the parent assertion
        errors.append(traceback.format_exc())
    finally:
        finished.set()


def _observe_contender_boundary(store, mode, action, attempted):
    """Observe actual queue admission or lock attempt, never scheduler sleeps."""
    if mode == "1" and action != "deferred":
        queue = store._durability._single_writer._queue
        original_put = queue.put

        def put(job, *args, **kwargs):
            original_put(job, *args, **kwargs)
            if threading.current_thread().name == "contender":
                attempted.set()

        queue.put = put
    else:
        original_lock = store._durability.writer_lock

        @contextmanager
        def lock():
            if threading.current_thread().name == "contender":
                attempted.set()
            with original_lock():
                yield

        store._durability.writer_lock = lock


def _deferred_contender(store):
    completed, results, errors = threading.Event(), {}, []
    with store.deferred_commit():
        assert store._commit_defer_depth == 1
        nested = threading.Thread(
            target=_thread_target,
            args=(lambda: store.add_fact(make_fact("deferred-fact")), results, errors, "nested", completed),
            name="deferred-body-writer", daemon=True,
        )
        nested.start()
        _join_threads([nested], errors)
        assert completed.is_set(), "Deferred entry retained the writer lock across yield"
        assert store.read_context_nodes(["deferred-fact"]) == {}
    return "deferred committed"


def _contender_operation(store, action):
    if action == "batch":
        return store.run_typed_write_batch(
            TypedBatchInput((make_fact("second-fact"),), (make_preference("second-pref"),)), literal_plan,
        )
    if action == "fact":
        return store.add_fact(make_fact("second-fact"))
    if action == "preference":
        return store.add_preference(make_preference("second-pref"))
    if action == "read":
        return store.read_context_nodes(["seed", "batch-a", "batch-b", "batch-p"])
    assert action == "deferred"
    return _deferred_contender(store)


def _thread_matrix(root, mode, action, checkpoint):
    _configure(mode)
    store = LiteMemoryStore(root / "memory.json")
    store.add_fact(make_fact("seed"))
    before = store._committed_pair.generation
    staged, release, attempted = threading.Event(), threading.Event(), threading.Event()
    batch_done, contender_done = threading.Event(), threading.Event()
    results, errors, decisions = {}, [], []
    original_commit = store._durability.commit_locked

    def capture(base, target, **kwargs):
        decisions.append(deepcopy(target))
        return original_commit(base, target, **kwargs)

    def pause_staged_batch():
        if not staged.is_set():
            staged.set()
            assert release.wait(_WAIT), "Batch release never arrived"
            assert store._commit_defer_depth == 0, "Deferred entry changed active batch depth"

    def planner(snapshot, inputs):
        if checkpoint == "planner":
            pause_staged_batch()
        return literal_plan(snapshot, inputs)

    store._durability.commit_locked = capture
    if checkpoint == "pre-journal":
        store._durability.before_journal_durable_publish = pause_staged_batch
    batch = threading.Thread(
        target=_thread_target,
        args=(lambda: store.run_typed_write_batch(
            TypedBatchInput((make_fact("batch-a"), make_fact("batch-b")), (make_preference("batch-p"),)),
            planner,
        ), results, errors, "batch", batch_done), name="batch", daemon=True,
    )
    contender = threading.Thread(
        target=_thread_target,
        args=(lambda: _contender_operation(store, action), results, errors, "contender", contender_done),
        name="contender", daemon=True,
    )
    try:
        batch.start()
        assert staged.wait(_WAIT), f"Batch did not reach {checkpoint}"
        if checkpoint == "pre-journal":
            assert {"batch-a", "batch-b"} <= store._facts.keys()
        else:
            assert set(store._facts) == {"seed"}
        _observe_contender_boundary(store, mode, action, attempted)
        contender.start()
        assert attempted.wait(_WAIT), "Contender never reached queue/lock admission"
        assert not contender_done.is_set(), "Contender crossed an active batch writer boundary"
        assert not batch_done.is_set(), "Batch acknowledged before a durable decision"
        assert store._commit_defer_depth == 0
    finally:
        release.set()
    _join_threads([batch, contender], errors)
    result = results["batch"]
    assert result.successful_input_indices == (0, 1, 2)
    assert result.committed_generation == before + 1
    assert result.transaction_id == decisions[0].transaction_id
    if action == "batch":
        assert results["contender"].successful_input_indices == (0, 1)
        assert results["contender"].committed_generation == before + 2
        assert results["contender"].transaction_id == decisions[1].transaction_id
    if action == "read":
        assert set(results["contender"]) == {"seed", "batch-a", "batch-b", "batch-p"}
    expected_facts = {"seed", "batch-a", "batch-b"}
    expected_prefs = {"batch-p"}
    if action in {"batch", "fact"}:
        expected_facts.add("second-fact")
    if action in {"batch", "preference"}:
        expected_prefs.add("second-pref")
    if action == "deferred":
        expected_facts.add("deferred-fact")
    assert {row["id"] for row in decisions[-1].memory["facts"]} == expected_facts
    assert {row["id"] for row in decisions[-1].memory["preferences"]} == expected_prefs
    assert len(decisions) == (1 if action == "read" else 2)
    assert decisions[-1].generation == before + len(decisions)
    _assert_disk_pair(store._path, decisions[-1])
    _assert_reopened(store._path, decisions[-1])
    assert store._commit_defer_depth == 0
    _close(store)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("action", ["batch", "fact", "preference", "read", "deferred"])
@pytest.mark.parametrize("checkpoint", ["planner", "pre-journal"])
def test_batch_serializes_thread_contenders(tmp_path, mode, action, checkpoint):
    _run_bounded(tmp_path, _thread_matrix, mode, action, checkpoint)


def _seed_peer_bank(root):
    store = LiteMemoryStore(root / "memory.json")
    store.add_fact(make_fact("shared", "base"))
    store.add_fact(make_fact("victim", "base"))
    store.add_preference(make_preference("shared-p", "base"))
    before = deepcopy(store._committed_pair)
    _close(store)
    return before


def _peer_inputs(scenario, role):
    facts = [make_fact(f"{role}-fact", role)]
    prefs = [make_preference(f"{role}-pref", role)]
    if scenario == "update":
        facts.insert(0, make_fact("shared", role))
        prefs.insert(0, make_preference("shared-p", role))
    elif scenario == "explicit-upsert" and role == "batch":
        facts.insert(0, make_fact("victim", "batch"))
    return TypedBatchInput(tuple(facts), tuple(prefs))


def _install_lock_gate(store, attempted, acquired, release):
    original = store._durability.writer_lock
    paused = False

    @contextmanager
    def gate():
        nonlocal paused
        outer = not getattr(store._durability._local, "depth", 0)
        if outer:
            attempted.set()
        with original():
            if outer and not paused:
                paused = True
                acquired.set()
                assert release.wait(_WAIT), "Writer lock was never released by the test"
            yield

    store._durability.writer_lock = gate


def _peer_operation(store, root, scenario, role):
    initial = store._committed_pair.generation
    decisions, snapshots = [], []
    original_commit = store._durability.commit_locked

    def capture(base, target, **kwargs):
        decisions.append({"base": _pair_data(deepcopy(base)), "target": _pair_data(deepcopy(target))})
        return original_commit(base, target, **kwargs)

    def planner(snapshot, inputs):
        snapshots.append({"generation": snapshot.generation, "facts": [node.id for node in snapshot.facts]})
        return literal_plan(snapshot, inputs)

    store._durability.commit_locked = capture
    if role == "peer" and scenario in {"delete", "explicit-upsert"}:
        store.delete_fact("victim")
        acknowledgement = None
    else:
        inputs = _peer_inputs(scenario, role)
        result = store.run_typed_write_batch(inputs, planner)
        assert result.supported
        assert result.successful_input_indices == tuple(range(len(inputs.facts) + len(inputs.preferences)))
        acknowledgement = {"generation": result.committed_generation, "transaction_id": result.transaction_id}
    assert len(decisions) == 1
    (root / f"{role}-result.json").write_text(json.dumps({
        "initial_generation": initial, "decisions": decisions,
        "snapshots": snapshots, "acknowledgement": acknowledgement,
    }), encoding="utf-8")


def _coordinate_order(first, second, starts, attempted, acquired, releases):
    starts[first].set()
    assert acquired[first].wait(_WAIT), "First writer never acquired the actual lock"
    starts[second].set()
    assert attempted[second].wait(_WAIT), "Second writer never attempted the actual lock"
    assert not acquired[second].is_set(), "Both writers held the bank lock at once"
    releases[first].set()
    assert acquired[second].wait(_WAIT), "Second writer did not acquire the released lock"
    releases[second].set()


def _assert_serial_peer_result(root, before, scenario, first):
    roles = [first, "peer" if first == "batch" else "batch"]
    expected_facts = {"shared": "base", "victim": "base"}
    expected_prefs = {"shared-p": "base"}
    events = [(row["func_id"], row["event_type"]) for row in before.changelog]
    last_target = before
    for offset, role in enumerate(roles, 1):
        report = json.loads((root / f"{role}-result.json").read_text(encoding="utf-8"))
        assert report["initial_generation"] == before.generation, "Both writers must preopen a stale-capable snapshot"
        decision = report["decisions"][0]
        assert LitePair(**decision["base"]) == last_target
        target = LitePair(**decision["target"])
        assert target.generation == before.generation + offset
        if role == "peer" and scenario in {"delete", "explicit-upsert"}:
            expected_facts.pop("victim", None)
            assert report["acknowledgement"] is None
        else:
            assert report["snapshots"] == [{"generation": last_target.generation, "facts": list(expected_facts)}]
            inputs = _peer_inputs(scenario, role)
            for node in inputs.facts:
                events.append((node.id, "updated" if node.id in expected_facts else "created"))
                expected_facts[node.id] = node.object_
            for node in inputs.preferences:
                events.append((node.id, "updated" if node.id in expected_prefs else "created"))
                expected_prefs[node.id] = node.preference
            assert report["acknowledgement"] == {"generation": target.generation, "transaction_id": target.transaction_id}
        assert {row["id"]: row["object"] for row in target.memory["facts"]} == expected_facts
        assert {row["id"]: row["preference"] for row in target.memory["preferences"]} == expected_prefs
        assert [(row["func_id"], row["event_type"]) for row in target.changelog] == events
        last_target = target
    _assert_reopened(root / "memory.json", last_target)


def _instance_matrix(root, mode, scenario, first):
    _configure(mode)
    before = _seed_peer_bank(root)
    stores = {role: LiteMemoryStore(root / "memory.json") for role in ("batch", "peer")}
    starts = {role: threading.Event() for role in stores}
    attempted = {role: threading.Event() for role in stores}
    acquired = {role: threading.Event() for role in stores}
    releases = {role: threading.Event() for role in stores}
    results, errors, workers = {}, [], []

    def operation(role):
        assert starts[role].wait(_WAIT), "Operation start missing"
        _peer_operation(stores[role], root, scenario, role)

    for role, store in stores.items():
        _install_lock_gate(store, attempted[role], acquired[role], releases[role])
        worker = threading.Thread(
            target=_thread_target,
            args=(lambda role=role: operation(role), results, errors, role, threading.Event()),
            name=role, daemon=True,
        )
        workers.append(worker)
        worker.start()
    try:
        _coordinate_order(first, "peer" if first == "batch" else "batch", starts, attempted, acquired, releases)
    finally:
        for event in (*starts.values(), *releases.values()):
            event.set()
    _join_threads(workers, errors)
    _assert_serial_peer_result(root, before, scenario, first)
    for store in stores.values():
        _close(store)


def _peer_process(root, mode, scenario, role, ready, start, attempted, acquired, release):
    _configure(mode)
    root = Path(root)
    with (root / f"{role}-stacks.log").open("w", encoding="utf-8") as evidence:
        faulthandler.enable(file=evidence)
        # All existing individual/group cleanup paths send SIGTERM before
        # SIGKILL. Native faulthandler writes stacks before chaining termination,
        # including when the main thread is blocked on the writer queue. These
        # process-group tests already require POSIX (setsid/killpg/flock).
        faulthandler.register(signal.SIGTERM, file=evidence, all_threads=True, chain=True)
        # Also dump ahead of the earliest normal 10-second wait/join deadline.
        faulthandler.dump_traceback_later(5, file=evidence)
        try:
            store = LiteMemoryStore(root / "memory.json")
            _install_lock_gate(store, attempted, acquired, release)
            ready.set()
            assert start.wait(_WAIT), "Process operation start missing"
            _peer_operation(store, root, scenario, role)
            _close(store)
        except BaseException:
            traceback.print_exc(file=evidence)
            faulthandler.dump_traceback(file=evidence, all_threads=True)
            evidence.flush()
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.unregister(signal.SIGTERM)


def _process_matrix(root, mode, scenario, first):
    _configure(mode)
    before = _seed_peer_bank(root)
    context = multiprocessing.get_context("spawn")
    roles = ("batch", "peer")
    ready = {role: context.Event() for role in roles}
    starts = {role: context.Event() for role in roles}
    attempted = {role: context.Event() for role in roles}
    acquired = {role: context.Event() for role in roles}
    releases = {role: context.Event() for role in roles}
    processes = [context.Process(
        target=_peer_process,
        args=(str(root), mode, scenario, role, ready[role], starts[role], attempted[role], acquired[role], releases[role]),
    ) for role in roles]
    try:
        for process in processes:
            process.start()
        for event in ready.values():
            assert event.wait(_WAIT), "Both child stores must open before either write"
        _coordinate_order(first, "peer" if first == "batch" else "batch", starts, attempted, acquired, releases)
        deadline = time.monotonic() + _WAIT
        for process in processes:
            process.join(max(0, deadline - time.monotonic()))
        assert not any(process.is_alive() for process in processes), f"10-second process join timeout:\n{_diagnostics(root)}"
        assert all(process.exitcode == 0 for process in processes), _diagnostics(root)
        _assert_serial_peer_result(root, before, scenario, first)
    finally:
        for event in (*starts.values(), *releases.values()):
            event.set()
        for process in processes:
            if process.pid is not None:
                _stop_process(process)
                process.close()


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("scenario", ["add", "update", "delete", "explicit-upsert"])
@pytest.mark.parametrize("first", ["batch", "peer"], ids=["batch-first", "peer-first"])
def test_two_instances_follow_actual_lock_order(tmp_path, mode, scenario, first):
    _run_bounded(tmp_path, _instance_matrix, mode, scenario, first)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("scenario", ["add", "update", "delete", "explicit-upsert"])
@pytest.mark.parametrize("first", ["batch", "peer"], ids=["batch-first", "peer-first"])
def test_two_processes_follow_actual_lock_order(tmp_path, mode, scenario, first):
    _run_bounded(tmp_path, _process_matrix, mode, scenario, first)


def _stalled_diagnostic_peer(root, mode, ready, start, attempted, acquired, release, blocked, owned_group):
    if owned_group:
        os.setsid()

    def stalled_operation(store, _root, _scenario, _role):
        def stalled_planner(snapshot, inputs):
            blocked.set()
            threading.Event().wait()
            return literal_plan(snapshot, inputs)

        store.run_typed_write_batch(TypedBatchInput((make_fact("stalled"),), ()), stalled_planner)

    globals()["_peer_operation"] = stalled_operation
    _peer_process(root, mode, "add", "peer", ready, start, attempted, acquired, release)


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize("owned_group", [False, True], ids=["individual-cleanup", "group-cleanup"])
def test_peer_cleanup_preserves_stacks_before_diagnostic_deadline(tmp_path, mode, owned_group):
    context = multiprocessing.get_context("spawn")
    ready, start, attempted, acquired, release, blocked = (context.Event() for _ in range(6))
    process = context.Process(
        target=_stalled_diagnostic_peer,
        args=(str(tmp_path), mode, ready, start, attempted, acquired, release, blocked, owned_group),
    )
    process.start()
    try:
        assert ready.wait(_WAIT), "Diagnostic peer did not open"
        start.set()
        release.set()
        assert blocked.wait(_WAIT), "Diagnostic peer never reached its stuck planner"
        # No sleeps: cleanup is deliberately immediate, before the scheduled dump.
        _stop_process(process, owned_group=owned_group)
        assert not process.is_alive(), "Diagnostic cleanup exceeded existing bounds"
        evidence = (tmp_path / "peer-stacks.log").read_text(encoding="utf-8")
        assert "stalled_planner" in evidence, evidence
        assert "_run_typed_write_batch_locked" in evidence, evidence
        if mode == "1":
            assert "submit" in evidence and "single_writer.py" in evidence, evidence
    finally:
        _stop_process(process, owned_group=owned_group)
        process.close()
