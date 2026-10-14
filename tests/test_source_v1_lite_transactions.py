"""Synthetic CAS, deadline, queue and durable-decision tests for Lite V1."""
from __future__ import annotations

import copy
import json
import multiprocessing
import threading
import time
from contextlib import ExitStack
from dataclasses import replace

import pytest

from memplex.models import Fact
from memplex.source_codec import decode_ledger
from memplex.source_contract import (
    ExpectedAbsent,
    SourceBusyError,
    SourceClass,
    SourceContractError,
    SourceKey,
    SourceKind,
    SourceMutation,
    SourceOperation,
    SourceState,
)
from memplex.storage.lite.durability import LiteDurability, LiteStorageIntegrityError
from memplex.storage.lite.single_writer import SingleWriterQueue
from memplex.storage.lite.source_versions import LiteSourceVersionStore
from memplex.storage.source_versions import _RESOURCE_ISSUER, _source_write_scope
from tests.test_source_v1_lite_format import CONTEXT, HOOKS, SCOPE, fence, new_bank


def command(name="source", *, expected=ExpectedAbsent.ABSENT,
            operation=SourceOperation.CREATE, value="one", payload=None):
    if payload is None and operation is not SourceOperation.DELETE:
        payload = Fact(id=name, subject="synthetic", predicate="is", object_=value,
                       tenant_id="t1", workspace_id="w1", owner_subject_id="alice",
                       visibility="workspace").to_dict()
    return SourceMutation(SourceKey(SCOPE.authority_id, "t1", "w1", SourceKind.FACT, name),
                          expected, operation, payload, SourceClass.DIRECT_INPUT, {}, ())


def mutate(backend, cmd):
    return backend.mutate_source(CONTEXT, cmd, deadline_ns=time.monotonic_ns() + 2_000_000_000)


def authoritative(store):
    return store._durability.load_authoritative()


@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_source_compare_and_write_one_decision(tmp_path, monkeypatch, single_writer):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", single_writer)
    store, backend = new_bank(tmp_path)
    decisions = []
    monkeypatch.setattr(store._durability, "after_journal_rename_and_parent_dir_fsync",
                        lambda: decisions.append("durable"))
    first = mutate(backend, command())
    assert decisions == ["durable"]
    decisions.clear()
    second = mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                     value="two"))
    assert decisions == ["durable"]
    with fence(backend) as session:
        committed = session.source(second.ref.key)
        assert committed.head.ref.token.content_revision == 2
        assert committed.payload["object"] == "two"
        assert session.authority().commit_seq == second.commit_seq == 3
    old = authoritative(store)
    decisions.clear()
    with pytest.raises(SourceContractError) as stale_write_error:
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                value="stale"))
    assert stale_write_error.value.code == "source_changed"
    assert decisions == []
    assert authoritative(store) == old


@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_recreate_changes_incarnation(tmp_path, monkeypatch, single_writer):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", single_writer)
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    deleted = mutate(backend, command(expected=first.ref.token, operation=SourceOperation.DELETE))
    assert deleted.state is SourceState.DELETED
    old = authoritative(store)
    assert old.memory["facts"] == []
    for cmd in [command(), command(expected=deleted.ref.token, operation=SourceOperation.REPLACE)]:
        with pytest.raises(SourceContractError):
            mutate(backend, cmd)
        assert authoritative(store) == old
    recreated = mutate(backend, command(expected=deleted.ref.token, operation=SourceOperation.RECREATE))
    assert recreated.ref.token.incarnation != deleted.ref.token.incarnation
    assert recreated.ref.token.content_revision == recreated.ref.token.auth_revision == 1
    with pytest.raises(SourceContractError):
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE))


def test_access_only_and_acl_changes_have_independent_revisions(tmp_path):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    values = dict(command().payload)
    values.update(access_count=4, last_accessed_at="2026-10-12T00:00:00+00:00")
    second = mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                     payload=values))
    assert second.ref.token == first.ref.token
    values["visibility"] = "user"
    third = mutate(backend, command(expected=second.ref.token, operation=SourceOperation.REPLACE,
                                    payload=values))
    assert third.ref.token.content_revision == 1
    assert third.ref.token.auth_revision == 2
    assert decode_ledger(authoritative(store).memory["source_validity"]).sources[third.ref.key].ref == third.ref


@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_lock_deadline_and_deferred_commit(tmp_path, monkeypatch, single_writer):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", single_writer)
    store, backend = new_bank(tmp_path)
    with store.deferred_commit():
        with pytest.raises(SourceBusyError), fence(backend):
            pass
        with pytest.raises(SourceBusyError):
            mutate(backend, command())
    first = mutate(backend, command())
    assert first.ref.token.content_revision == 1
    with pytest.raises(SourceBusyError), store._durability.writer_lock_until(time.monotonic_ns() - 1):
        pass


def test_same_token_thread_race_has_one_winner(tmp_path):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    start = threading.Barrier(3)
    results = []

    def writer(value):
        start.wait(timeout=10)
        try:
            result = mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                             value=value))
        except SourceContractError as exc:
            result = exc.code
        results.append(result)

    workers = [threading.Thread(target=writer, args=(value,)) for value in ("left", "right")]
    try:
        for worker in workers:
            worker.start()
        start.wait(timeout=10)
    finally:
        for worker in workers:
            if worker.ident is not None:
                worker.join(5)
        start.abort()
        for worker in workers:
            if worker.ident is not None:
                worker.join(5)
        assert all(not worker.is_alive() for worker in workers)
    assert sum(item == "source_changed" for item in results) == 1
    assert authoritative(store).generation == 3


def _child_reader(path, changelog, ready, proceed, result):
    try:
        backend = LiteSourceVersionStore(LiteDurability(path, changelog))
        backend._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
        with fence(backend) as session:
            initial = session.source(command().key).payload["object"]
        ready.set()
        assert proceed.wait(10)
        with fence(backend) as session:
            current = session.source(command().key).payload["object"]
        result.put((initial, current))
    except BaseException as exc:  # noqa: BLE001 - return child failures to the parent assertion
        result.put(("error", repr(exc)))
        ready.set()
    finally:
        result.close()
        result.join_thread()


def test_reader_after_commit_refreshes_cross_process(tmp_path):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    context = multiprocessing.get_context("spawn")
    ready, proceed, result = context.Event(), context.Event(), context.Queue()
    child = context.Process(target=_child_reader,
                            args=(store._path, store._changelog._path, ready, proceed, result))
    child.start()
    try:
        assert ready.wait(10)
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE, value="two"))
        proceed.set()
        assert result.get(timeout=10) == ("one", "two")
    finally:
        proceed.set()
        exitcode = _reap(child)
        result.close()
        result.join_thread()
    assert exitcode == 0


def test_canceled_queue_work_never_runs_after_caller_failure():
    writer = SingleWriterQueue()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    side_effects = []

    def blocker():
        started.set()
        assert release.wait(5)

    def occupy():
        writer.submit(blocker)
        finished.set()

    thread = threading.Thread(target=occupy)
    thread.start()
    try:
        assert started.wait(5)
        with pytest.raises(SourceBusyError):
            writer.submit_until(lambda: side_effects.append("published"), time.monotonic_ns() + 10_000_000)
    finally:
        release.set()
        thread.join(5)
        writer.drain()
        writer.close()
    assert not thread.is_alive()
    assert writer._thread is None or not writer._thread.is_alive()
    assert finished.is_set()
    assert side_effects == []


@pytest.mark.parametrize("boundary,new", [
    ("before_journal_durable_publish", False),
    ("after_journal_rename_and_parent_dir_fsync", True),
    ("after_memory_replace", True), ("after_changelog_replace", True),
    ("after_journal_unlink", True), ("after_final_parent_dir_fsync", True),
])
def test_fault_boundary_leaves_whole_source_pair(tmp_path, monkeypatch, boundary, new):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    old = copy.deepcopy(authoritative(store).memory["sync"])

    def fail():
        raise OSError("synthetic fault " + boundary)

    with monkeypatch.context() as fault:
        fault.setattr(store._durability, boundary, fail)
        with pytest.raises(OSError, match="synthetic fault"):
            mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE, value="two"))
    fresh = LiteDurability(store._path, store._changelog._path).load_authoritative()
    row = fresh.memory["facts"][0]
    head = decode_ledger(fresh.memory["source_validity"]).sources[first.ref.key]
    assert row["object"] == ("two" if new else "one")
    assert head.ref.token.content_revision == (2 if new else 1)
    assert fresh.memory["sync"] == old
    assert json.loads(store._path.read_text())["generation"] == json.loads(store._changelog._path.read_text())["generation"]


def test_self_dependency_is_rejected_before_durable_decision(tmp_path):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    old = authoritative(store)
    cyclic = replace(command(expected=first.ref.token, operation=SourceOperation.REPLACE),
                     root_refs=(first.ref,))
    with pytest.raises(SourceContractError):
        mutate(backend, cyclic)
    assert authoritative(store) == old


def raw_command(name="raw", *, physical="physical", expected=ExpectedAbsent.ABSENT,
                operation=SourceOperation.CREATE, text="evidence"):
    values = {"id": physical, "raw_text": text, "trust_tier": 1,
              "created_at": "2026-10-11T00:00:00+00:00", "source": {},
              "tenant_id": "t1", "workspace_id": "w1", "owner_subject_id": "alice",
              "visibility": "workspace", "namespace": {}}
    return SourceMutation(SourceKey(SCOPE.authority_id, "t1", "w1", SourceKind.RAW_PARAGRAPH, name),
                          expected, operation, None if operation is SourceOperation.DELETE else values,
                          SourceClass.DIRECT_INPUT, {}, ())


def test_raw_physical_replacement_and_delete_keep_one_logical_chain(tmp_path):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, raw_command())
    second = mutate(backend, raw_command(physical="replacement", expected=first.ref.token,
                                         operation=SourceOperation.REPLACE))
    assert second.ref.token.content_revision == 2
    with fence(backend) as session:
        assert session.resolve_id("physical") is None
        assert session.resolve_id("replacement") == first.ref.key
        assert session.source(first.ref.key).payload["id"] == "raw"
    mutate(backend, raw_command(expected=second.ref.token, operation=SourceOperation.DELETE))
    pair = authoritative(store)
    assert pair.memory["paragraphs"] == []
    tombstone = decode_ledger(pair.memory["source_validity"]).sources[first.ref.key]
    assert tombstone.payload_locator == "" and not tombstone.input_context and not tombstone.root_refs


@pytest.mark.parametrize("deleted", [False, True])
def test_cross_kind_id_cannot_reuse_live_or_deleted_handle(tmp_path, deleted):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    if deleted:
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.DELETE))
    before = authoritative(store)
    with pytest.raises(SourceContractError):
        mutate(backend, raw_command("source"))
    assert authoritative(store) == before


@pytest.mark.parametrize("field", ["id", "tenant_id", "workspace_id", "owner_subject_id", "visibility"])
def test_missing_persisted_identity_is_never_filled_from_request(tmp_path, field):
    store, backend = new_bank(tmp_path)
    values = dict(command().payload)
    values.pop(field)
    before = authoritative(store)
    with pytest.raises(SourceContractError):
        mutate(backend, command(payload=values))
    assert authoritative(store) == before


def test_existing_persisted_acl_is_checked_before_replacement_acl(tmp_path):
    from memplex.storage.source_versions import SourceValidationHooks
    store, backend = new_bank(tmp_path)
    values = dict(command().payload)
    values["owner_subject_id"] = "bob"
    first = mutate(backend, command(payload=values))
    hooks = SourceValidationHooks(
        lambda snapshot, context: snapshot.payload.get("owner_subject_id") == context.principal.subject_id,
        lambda snapshot: True,
    )
    backend._install_hooks(hooks, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    before = authoritative(store)
    with pytest.raises(SourceContractError) as rejected:
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE))
    assert rejected.value.code == "denied"
    assert authoritative(store) == before


@pytest.mark.parametrize("field", ["content_revision", "auth_revision", "commit_seq"])
def test_revision_overflow_fails_closed_without_decision(tmp_path, field):
    from memplex.source_codec import encode_ledger
    from memplex.source_contract import MAX_REVISION
    from tests.test_source_v1_lite_format import write_fixture
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    pair = authoritative(store)
    ledger = decode_ledger(pair.memory["source_validity"])
    head = ledger.sources[first.ref.key]
    if field == "commit_seq":
        ledger = replace(ledger, authority=replace(ledger.authority, commit_seq=MAX_REVISION))
    else:
        head = replace(head, ref=replace(head.ref, token=replace(head.ref.token, **{field: MAX_REVISION})))
        ledger = replace(ledger, sources={first.ref.key: head})
    pair.memory["source_validity"] = encode_ledger(ledger)
    write_fixture(store, pair)
    values = dict(command(value="changed").payload)
    values["visibility"] = "user"
    before = authoritative(store)
    with pytest.raises(SourceContractError) as rejected:
        mutate(backend, command(expected=head.ref.token, operation=SourceOperation.REPLACE, payload=values))
    assert rejected.value.code == "quarantined"
    assert authoritative(store) == before


def test_command_and_read_payloads_are_detached(tmp_path):
    _, backend = new_bank(tmp_path)
    values = dict(command().payload)
    values["namespace"] = {"note": "before"}
    cmd = command(payload=values)
    values["namespace"]["note"] = "caller mutation"
    first = mutate(backend, cmd)
    with fence(backend) as session:
        snapshot = session.source(first.ref.key)
        assert snapshot.payload["namespace"]["note"] == "before"
        with pytest.raises(TypeError):
            snapshot.payload["namespace"]["note"] = "forged"
    mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE, value="new"))
    assert snapshot.payload["object"] == "one"


def test_sync_required_rejection_and_atomic_body_free_bookkeeping(tmp_path):
    from memplex.source_codec import encode_ledger
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.source_versions import _issue_admission
    from memplex.sync_repository import SyncCapturePolicy
    from tests.test_source_v1_lite_format import POLICY, write_fixture
    store, backend = new_bank(tmp_path / "off")
    pair = authoritative(store)
    pair.memory["sync"]["tenant_binding"] = "t1"
    pair.memory["source_validity"] = encode_ledger(decode_ledger(pair.memory["source_validity"]))
    write_fixture(store, pair)
    before = authoritative(store)
    mutate(backend, command(value="private-body-do-not-log"))
    after = authoritative(store)
    assert after.memory["sync"] == before.memory["sync"]
    assert after.changelog[:-1] == before.changelog
    assert len(after.changelog) == len(before.changelog) + 1
    assert "private-body-do-not-log" not in json.dumps(after.changelog)
    required = LiteMemoryStore(tmp_path / "required" / "memory.json",
                               sync_capture_policy=SyncCapturePolicy("required", "node"))
    writer = required.source_versions()
    exact = required._durability.load_authoritative()
    with pytest.raises(SourceContractError) as rejected:
        writer.initialize_authority(SCOPE, POLICY,
            admission=_issue_admission(required._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER))
    assert rejected.value.code == "unsupported_capability"
    assert required._durability.load_authoritative() == exact
    with pytest.raises(SourceContractError):
        mutate(writer, command())


def _process_cas(backend, token, start, results, value, single_writer):
    import os
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = single_writer
    try:
        start.wait(timeout=10)
        receipt = mutate(backend, command(expected=token, operation=SourceOperation.REPLACE, value=value))
        results.put(("success", receipt.commit_seq))
    except SourceContractError as exc:
        results.put(("error", exc.code.value))
    finally:
        _close_native(backend._owner)
        results.close()
        results.join_thread()


def _fork_native_cas(path, single_writer, report):
    import os
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = "0"
    # This spawned coordinator has no queue threads when it forks the exact
    # admitted resource. The ordinary V3 legacy reopen stays prohibited.
    store, backend = new_bank(path)
    ctx = multiprocessing.get_context("fork")
    start, result = ctx.Barrier(3), ctx.Queue()
    children = []
    try:
        first = mutate(backend, command())
        children = [ctx.Process(target=_process_cas,
                                args=(backend, first.ref.token, start, result, value, single_writer))
                    for value in ("left", "right")]
        for child in children:
            child.start()
        start.wait(timeout=10)
        outcomes = [result.get(timeout=10), result.get(timeout=10)]
        report.put((outcomes, authoritative(store).generation))
    finally:
        start.abort()
        exits = [_reap(child) for child in children if child.pid is not None]
        result.close()
        result.join_thread()
        _close_native(store)
        report.close()
        report.join_thread()
        assert all(exitcode == 0 for exitcode in exits)


@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_native_cross_process_cas_has_one_winner(tmp_path, single_writer):
    ctx = multiprocessing.get_context("spawn")
    report = ctx.Queue()
    coordinator = ctx.Process(target=_fork_native_cas, args=(tmp_path, single_writer, report))
    coordinator.start()
    try:
        outcomes, generation = report.get(timeout=20)
    finally:
        exitcode = _reap(coordinator)
        report.close()
        report.join_thread()
    assert exitcode == 0
    assert sorted(outcomes) == [("error", "source_changed"), ("success", 3)]
    assert generation == 3
    peer = LiteSourceVersionStore(LiteDurability(tmp_path / "memory.json", tmp_path / "changelog.json"))
    peer._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    with fence(peer) as session:
        assert session.source(command().key).head.ref.token.content_revision == 2


def _hold_process_lock(path, changelog, held, release):
    with LiteDurability(path, changelog).writer_lock():
        held.set()
        assert release.wait(10)


def test_flock_contention_respects_native_deadline(tmp_path):
    store, backend = new_bank(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_process_lock,
                        args=(store._path, store._changelog._path, held, release))
    child.start()
    try:
        assert held.wait(10)
        with pytest.raises(SourceBusyError), backend.read_fence(
            CONTEXT, deadline_ns=time.monotonic_ns() + 10_000_000
        ):
            pass
    finally:
        release.set()
        exitcode = _reap(child)
    assert exitcode == 0
    with fence(backend) as session:
        assert session.authority().commit_seq == 1


def test_peer_instance_rejects_same_process_pending_deferred_state(tmp_path):
    store, _ = new_bank(tmp_path)
    peer = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    peer._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    with store.deferred_commit(), pytest.raises(SourceBusyError), fence(peer):
        pass
    with fence(peer) as session:
        assert session.authority().commit_seq == 1


def test_deadline_before_decision_does_not_publish(tmp_path, monkeypatch):
    import memplex.storage.lite.source_versions as implementation
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    before = authoritative(store)
    now = [time.monotonic_ns()]
    monkeypatch.setattr(implementation.time, "monotonic_ns", lambda: now[0])
    monkeypatch.setattr(store._durability, "before_journal_durable_publish",
                        lambda: now.__setitem__(0, now[0] + 100_000_000))
    with pytest.raises(SourceBusyError):
        backend.mutate_source(CONTEXT, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                              value="late"), deadline_ns=now[0] + 50_000_000)
    assert authoritative(store) == before
    assert not store._durability._journal_path.exists()


def test_deadline_after_decision_finishes_and_returns_real_receipt(tmp_path, monkeypatch):
    import memplex.storage.lite.source_versions as implementation
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    now = [time.monotonic_ns()]
    monkeypatch.setattr(implementation.time, "monotonic_ns", lambda: now[0])
    monkeypatch.setattr(store._durability, "after_journal_rename_and_parent_dir_fsync",
                        lambda: now.__setitem__(0, now[0] + 100_000_000))
    receipt = backend.mutate_source(CONTEXT, command(expected=first.ref.token, operation=SourceOperation.REPLACE,
                                                   value="committed"), deadline_ns=now[0] + 50_000_000)
    assert receipt.ref.token.content_revision == 2
    assert authoritative(store).memory["facts"][0]["object"] == "committed"


def test_read_after_source_change_rejects_existing_derived_root(tmp_path):
    from memplex.source_contract import ValidationLimits
    from memplex.source_validation import validate_sources
    from tests.test_source_v1_lite_format import derived_pair, write_fixture
    store, backend = new_bank(tmp_path)
    pair, derived_key = derived_pair(store)
    write_fixture(store, pair)
    ledger = decode_ledger(pair.memory["source_validity"])
    source = next(head for key, head in ledger.sources.items() if key.kind is SourceKind.RAW_PARAGRAPH)
    old_derived_id = derived_key.logical_id
    with fence(backend) as session:
        before = validate_sources(session, [old_derived_id], authorization=CONTEXT,
                                  own_acl=HOOKS.own_acl, safe_for_model=HOOKS.safe_for_model,
                                  limits=ValidationLimits(), deadline_ns=time.monotonic_ns() + 250_000_000)
        assert old_derived_id in before.accepted
    cmd = raw_command("logical_1", physical="physical_1", expected=source.ref.token,
                      operation=SourceOperation.REPLACE, text="changed evidence")
    mutate(backend, cmd)
    with fence(backend) as session:
        read_after_commit = validate_sources(session, [old_derived_id], authorization=CONTEXT,
                                             own_acl=HOOKS.own_acl, safe_for_model=HOOKS.safe_for_model,
                                             limits=ValidationLimits(), deadline_ns=time.monotonic_ns() + 250_000_000)
        assert old_derived_id not in read_after_commit.accepted
        assert read_after_commit.rejected[old_derived_id] == "source_changed"


def test_native_canceled_source_job_cannot_publish_later(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", "1")
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    exact = (store._path.read_bytes(), store._changelog._path.read_bytes())
    held, release = threading.Event(), threading.Event()
    writer = store._durability._single_writer

    def occupy():
        held.set()
        assert release.wait(5)

    thread = threading.Thread(target=lambda: writer.submit(occupy))
    thread.start()
    try:
        assert held.wait(5)
        with pytest.raises(SourceBusyError):
            backend.mutate_source(CONTEXT,
                command(expected=first.ref.token, operation=SourceOperation.REPLACE, value="late"),
                deadline_ns=time.monotonic_ns() + 10_000_000)
    finally:
        release.set()
        thread.join(5)
        writer.drain()
    assert not thread.is_alive()
    assert (store._path.read_bytes(), store._changelog._path.read_bytes()) == exact


def test_reader_waits_for_one_payload_head_and_receipt_decision(tmp_path, monkeypatch):
    store, backend = new_bank(tmp_path)
    first = mutate(backend, command())
    held, release, done = threading.Event(), threading.Event(), threading.Event()
    receipts, errors = [], []

    def before_decision():
        held.set()
        assert release.wait(5)

    def writer():
        try:
            receipts.append(mutate(backend, command(expected=first.ref.token,
                                                    operation=SourceOperation.REPLACE, value="two")))
        except Exception as exc:  # noqa: BLE001 - preserve thread failure for assertion
            errors.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(store._durability, "before_journal_durable_publish", before_decision)
    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert held.wait(5)
        assert not done.is_set() and not receipts
        with pytest.raises(SourceBusyError), backend.read_fence(
            CONTEXT, deadline_ns=time.monotonic_ns() + 10_000_000
        ):
            pass
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert len(receipts) == 1
    with fence(backend) as session:
        source = session.source(first.ref.key)
        assert source.head.ref == receipts[0].ref
        assert source.payload["object"] == "two"


def test_dependency_change_and_lifecycle_revocation_keep_correct_versions(tmp_path):
    from memplex.storage.source_versions import SourceValidationHooks
    store, backend = new_bank(tmp_path)
    raw = mutate(backend, raw_command())
    cmd = replace(command(), source_class=SourceClass.CAPTURED_INPUT, root_refs=(raw.ref,))
    captured = mutate(backend, cmd)
    invalid = replace(cmd, key=replace(cmd.key, logical_id="other"),
                      payload={**dict(cmd.payload), "id": "other"},
                      root_refs=(replace(raw.ref, token=replace(raw.ref.token, content_revision=2)),))
    before = authoritative(store)
    with pytest.raises(SourceContractError):
        mutate(backend, invalid)
    assert authoritative(store) == before
    hooks = SourceValidationHooks(HOOKS.own_acl, lambda snapshot: snapshot.payload.get("trust_tier") == 1)
    backend._install_hooks(hooks, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    values = dict(cmd.payload)
    values["namespace"] = {"premise_superseded": "1"}
    revoked = mutate(backend, replace(cmd, expected=captured.ref.token,
                                      operation=SourceOperation.REPLACE, payload=values))
    assert revoked.ref.token.content_revision == 2
    assert revoked.ref.token.auth_revision == 1


@pytest.fixture(autouse=True)
def owned_native_banks(monkeypatch):
    """Every bank created by a test belongs to that test, including failures."""
    import sys
    stores = []
    original = new_bank

    def tracked(path):
        store, backend = original(path)
        stores.append(store)
        return store, backend

    monkeypatch.setattr(sys.modules[__name__], "new_bank", tracked)
    try:
        yield
    finally:
        # ExitStack still closes every bank if any individual cleanup fails.
        with ExitStack() as cleanup:
            for store in stores:
                cleanup.callback(_close_native, store)


def _reap(child, *, grace_seconds=10):
    """Always reap owned workers, including assertion and timeout paths."""
    child.join(grace_seconds)
    if child.is_alive():
        child.terminate()
        child.join(5)
    if child.is_alive():
        child.kill()
        child.join(5)
    assert not child.is_alive()
    exitcode = child.exitcode
    child.close()
    return exitcode


def _close_native(store):
    writer = getattr(store._durability, "_single_writer", None)
    if writer is not None:
        writer.close()
        assert writer._thread is None or not writer._thread.is_alive()


def _peer_deferred_write(backend, token, go, result, operation, single_writer):
    import os
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = single_writer
    try:
        assert go.wait(10)
        receipt = mutate(backend, command(expected=token, operation=operation, value="peer"))
        result.send(("committed", receipt.commit_seq, receipt.state.value))
    except BaseException as exc:  # noqa: BLE001 - surface child failure to parent
        result.send(("error", repr(exc)))
    finally:
        _close_native(backend._owner)
        result.close()


def _fresh_after_parent(path, changelog, go, result):
    try:
        assert go.wait(10)
        peer = LiteSourceVersionStore(LiteDurability(path, changelog))
        peer._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
        with fence(peer) as session:
            result.send(("read", session.authority().commit_seq))
    except BaseException as exc:  # noqa: BLE001 - surface child failure to parent
        result.send(("error", type(exc).__name__))
    finally:
        result.close()


def _deferred_scenario(path, single_writer, operation, inherited, report):
    import os
    # Fork a native admitted capability before any writer thread exists.
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = "0"
    store, backend = new_bank(path)
    ctx = multiprocessing.get_context("fork")
    go = ctx.Event()
    receive, send = ctx.Pipe(duplex=False)
    child = None
    try:
        if inherited:
            with store.deferred_commit():
                child = ctx.Process(target=_fresh_after_parent,
                                    args=(store._path, store._changelog._path, go, send))
                child.start()
                send.close()
            go.set()
            assert receive.poll(10)
            report.send(receive.recv())
        else:
            first = mutate(backend, command())
            child = ctx.Process(target=_peer_deferred_write,
                                args=(backend, first.ref.token, go, send, operation, single_writer))
            child.start()
            send.close()
            os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = single_writer
            decisions = []
            store._durability.before_journal_durable_publish = lambda: decisions.append("decision")
            try:
                with store.deferred_commit():
                    go.set()
                    assert receive.poll(10)
                    receipt = receive.recv()
                    assert receipt[0] == "committed", receipt
                    committed = (store._path.read_bytes(), store._changelog._path.read_bytes())
            except LiteStorageIntegrityError:
                rejected = True
            else:
                rejected = False
            current = (store._path.read_bytes(), store._changelog._path.read_bytes())
            peer = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
            peer._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
            with fence(peer) as session:
                head = session.source(first.ref.key).head
                body = (session.source(first.ref.key).payload["object"]
                        if head.state is SourceState.LIVE else None)
                report.send((rejected, current == committed, decisions, session.authority().commit_seq,
                             head.ref.token.content_revision, head.state.value, body))
    except BaseException as exc:  # noqa: BLE001 - surface coordinator failure
        report.send(("error", repr(exc)))
    finally:
        go.set()
        receive.close()
        send.close()
        if child is not None and child.pid is not None:
            assert _reap(child) == 0
        _close_native(store)
        report.close()


def _run_deferred_scenario(path, single_writer="0", operation=SourceOperation.REPLACE, inherited=False):
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_deferred_scenario,
                        args=(path, single_writer, operation, inherited, send))
    child.start()
    send.close()
    try:
        assert receive.poll(20)
        return receive.recv()
    finally:
        receive.close()
        assert _reap(child) == 0


@pytest.mark.parametrize("single_writer", ["0", "1"])
@pytest.mark.parametrize("operation", [SourceOperation.REPLACE, SourceOperation.DELETE])
def test_peer_deferred_exit_cannot_rollback_source(tmp_path, single_writer, operation):
    outcome = _run_deferred_scenario(tmp_path, single_writer, operation)
    state = "live" if operation is SourceOperation.REPLACE else "deleted"
    body = "peer" if operation is SourceOperation.REPLACE else None
    assert outcome == (True, True, [], 3, 2, state, body)


def test_forked_fresh_reader_ignores_foreign_pending_scope(tmp_path):
    assert _run_deferred_scenario(tmp_path, inherited=True) == ("read", 1)


def _regressive_pair(store, backend, case):
    from uuid import uuid4

    from memplex.source_codec import encode_ledger
    from memplex.storage.lite.durability import LitePair

    first = mutate(backend, command())
    original = authoritative(store)
    if case == "deleted_same_incarnation":
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.DELETE))
    elif case == "auth_revision":
        payload = dict(command().payload)
        payload["visibility"] = "user"
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE, payload=payload))
    else:
        mutate(backend, command(expected=first.ref.token, operation=SourceOperation.REPLACE, value="two"))
    base = authoritative(store)
    before = decode_ledger(base.memory["source_validity"])
    memory = copy.deepcopy(original.memory)
    ledger = decode_ledger(memory["source_validity"])
    head = replace(ledger.sources[first.ref.key], last_commit_seq=before.authority.commit_seq + 1)
    authority = replace(before.authority, commit_seq=before.authority.commit_seq + 1)
    if case == "head_removed":
        sources = {}
        memory["facts"] = []
    else:
        if case == "live_incarnation":
            head = replace(head, ref=replace(head.ref, token=replace(head.ref.token, incarnation=str(uuid4()))))
        elif case == "deleted_same_incarnation":
            head = replace(head, ref=replace(head.ref, token=replace(head.ref.token, content_revision=3)))
        elif case == "authority_sequence":
            authority = ledger.authority
            head = ledger.sources[first.ref.key]
        sources = {first.ref.key: head}
    memory["source_validity"] = encode_ledger(replace(ledger, authority=authority, sources=sources))
    return base, LitePair(memory, base.changelog, base.generation + 1, "regressive-candidate")


@pytest.mark.parametrize("route", ["commit", "recovery"])
@pytest.mark.parametrize("case", ["authority_sequence", "content_revision", "auth_revision",
                                  "head_removed", "live_incarnation", "deleted_same_incarnation"])
def test_regressive_v3_pair_is_rejected_before_io(tmp_path, route, case, monkeypatch):
    from memplex.storage.lite.durability import _pair_record
    from memplex.storage.lite.source_versions import validate_source_memory

    store, backend = new_bank(tmp_path)
    base, target = _regressive_pair(store, backend, case)
    # These candidates have individually valid schema, payload, digest and ACL.
    # The cross-decision transition is what must fail closed.
    validate_source_memory(target.memory)
    exact = (store._path.read_bytes(), store._changelog._path.read_bytes())
    decisions = []
    monkeypatch.setattr(store._durability, "before_journal_durable_publish", lambda: decisions.append(1))
    if route == "commit":
        with store._durability.writer_lock(), _source_write_scope(store._durability, issuer=_RESOURCE_ISSUER), pytest.raises(LiteStorageIntegrityError):
            store._durability.commit_locked(base, target)
        assert not store._durability._journal_path.exists()
    else:
        journal = {"format_version": 3, "base_record": _pair_record(base), "base_memory": base.memory,
                   "base_changelog": base.changelog, "target_record": _pair_record(target),
                   "target": {"memory": target.memory, "changelog": target.changelog,
                              "generation": target.generation, "transaction_id": target.transaction_id}}
        store._durability._journal_path.write_text(json.dumps(journal))
        frozen_journal = store._durability._journal_path.read_bytes()
        with pytest.raises(LiteStorageIntegrityError, match="invalid Lite durable journal"):
            LiteDurability(store._path, store._changelog._path).load_authoritative()
        assert store._durability._journal_path.read_bytes() == frozen_journal
    assert decisions == []
    assert (store._path.read_bytes(), store._changelog._path.read_bytes()) == exact


def test_operation_only_v3_journal_replays_idempotently(tmp_path, monkeypatch):
    from memplex.storage.lite.durability import LitePair

    store, backend = new_bank(tmp_path)
    mutate(backend, command())
    base = authoritative(store)
    target = LitePair(base.memory, base.changelog, base.generation + 1, "operation-only")

    def crash():
        raise OSError("owned post-decision crash")

    monkeypatch.setattr(store._durability, "after_journal_rename_and_parent_dir_fsync", crash)
    with store._durability.writer_lock(), _source_write_scope(store._durability, issuer=_RESOURCE_ISSUER), pytest.raises(OSError, match="owned post-decision crash"):
        store._durability.commit_locked(base, target)
    fresh = LiteDurability(store._path, store._changelog._path)
    assert fresh.load_authoritative() == target
    assert fresh.load_authoritative() == target
    assert not fresh._journal_path.exists()


def test_owned_process_cleanup_reaps_after_failed_assertion(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_process_lock,
                        args=(tmp_path / "memory.json", tmp_path / "changelog.json", held, release))
    child.start()
    try:
        assert held.wait(10)
        with pytest.raises(AssertionError, match="owned test failure"):
            try:
                raise AssertionError("owned test failure")
            finally:
                release.set()
                exitcode = _reap(child)
        assert exitcode == 0
        assert child not in multiprocessing.active_children()
    finally:
        # A terminated process may have held the Event condition lock.
        # Never touch that synchronization object after forced termination.
        if not child._closed:
            release.set()
            _reap(child)


def test_owned_process_cleanup_terminates_unresponsive_worker(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_hold_process_lock,
                        args=(tmp_path / "memory.json", tmp_path / "changelog.json", held, release))
    child.start()
    try:
        assert held.wait(10)
        assert _reap(child, grace_seconds=0) < 0
        assert child not in multiprocessing.active_children()
    finally:
        # A terminated process may have held the Event condition lock.
        # Never touch that synchronization object after forced termination.
        if not child._closed:
            release.set()
            _reap(child)


def _check_owned_scope_pending(store, backend, label):
    peer = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    peer._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    with pytest.raises(SourceBusyError), fence(peer):
        pass
    with pytest.raises(SourceBusyError):
        mutate(backend, command(name=label))


def _inherited_scope_child(store, backend, foreign, parent_finished, result, overlap, single_writer):
    import os
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = single_writer
    locals_ = []
    try:
        assert os.read(parent_finished, 1) == b"1"
        if overlap:
            for _ in range(2):
                scope = store.deferred_commit()
                scope.__enter__()
                locals_.append(scope)
        foreign.__exit__(None, None, None)
        if not overlap:
            with fence(backend) as session:
                assert session.authority().commit_seq == 1
            for _ in range(2):
                scope = store.deferred_commit()
                scope.__enter__()
                locals_.append(scope)
        _check_owned_scope_pending(store, backend, "nested")
        locals_.pop().__exit__(None, None, None)
        _check_owned_scope_pending(store, backend, "outer")
        locals_.pop().__exit__(None, None, None)
        with fence(backend) as session:
            assert session.authority().commit_seq == 1
        receipt = mutate(backend, command(name="released"))
        assert receipt.commit_seq == 2
        os.write(result, b"balanced")
    except BaseException as exc:  # noqa: BLE001 - preserve child assertion to coordinator
        os.write(result, (type(exc).__name__ + ": " + str(exc)).encode())
    finally:
        with ExitStack() as cleanup:
            cleanup.callback(os.close, parent_finished)
            cleanup.callback(os.close, result)
            cleanup.callback(_close_native, store)
            for scope in locals_:
                cleanup.callback(scope.__exit__, None, None, None)


def _fork_scope_ownership(path, overlap, single_writer, report):
    import os
    import select
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = "0"
    store, backend = new_bank(path)
    foreign = store.deferred_commit()
    parent_finished, finish = os.pipe()
    result, send = os.pipe()
    descriptors = {parent_finished, finish, result, send}
    pid = None
    entered = False

    def close_owned(descriptor):
        os.close(descriptor)
        descriptors.remove(descriptor)

    try:
        foreign.__enter__()
        entered = True
        pid = os.fork()
        if pid == 0:
            close_owned(finish)
            close_owned(result)
            try:
                _inherited_scope_child(store, backend, foreign, parent_finished, send, overlap, single_writer)
            finally:
                os._exit(0)
        close_owned(parent_finished)
        close_owned(send)
        entered = False
        foreign.__exit__(None, None, None)
        os.write(finish, b"1")
        close_owned(finish)
        assert select.select([result], [], [], 15)[0], "owned child did not report"
        report.send(os.read(result, 8192).decode())
    finally:
        # Register all cleanup before assertions/waits so failure cannot leave
        # pipe endpoints, the admitted bank, or its writer owned by the test.
        with ExitStack() as cleanup:
            cleanup.callback(report.close)
            cleanup.callback(_close_native, store)
            for descriptor in descriptors:
                cleanup.callback(os.close, descriptor)
            if entered:
                cleanup.callback(foreign.__exit__, None, None, None)
            if pid:
                deadline = time.monotonic() + 5
                while True:
                    reaped, status = os.waitpid(pid, os.WNOHANG)
                    if reaped:
                        break
                    if time.monotonic() >= deadline:
                        os.kill(pid, 9)
                        _, status = os.waitpid(pid, 0)
                        break
                    time.sleep(.01)
                assert status == 0


@pytest.mark.parametrize("single_writer", ["0", "1"])
@pytest.mark.parametrize("overlap", [False, True], ids=["foreign-first", "foreign-during-local"])
def test_inherited_scope_release_preserves_local_ownership(tmp_path, single_writer, overlap):
    ctx = multiprocessing.get_context("spawn")
    receive, send = ctx.Pipe(duplex=False)
    coordinator = ctx.Process(target=_fork_scope_ownership,
                              args=(tmp_path, overlap, single_writer, send))
    coordinator.start()
    send.close()
    try:
        assert receive.poll(25)
        assert receive.recv() == "balanced"
    finally:
        receive.close()
        assert _reap(coordinator) == 0
