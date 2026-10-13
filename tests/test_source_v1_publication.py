"""Atomic source publication; synthetic model output, real backend boundaries."""
from __future__ import annotations

import hashlib
import multiprocessing
import os
import threading
import time
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from memplex.models import Fact
from memplex.source_codec import _canonical, decode_ledger, output_digest
from memplex.source_contract import (
    ALGORITHM_VERSION,
    CARRIER_KEY,
    PUBLICATION_PREFIX,
    CaptureManifest,
    DerivationEnvelope,
    ExpectedAbsent,
    PublicationState,
    PublishCommand,
    SourceContractError,
    SourceOperation,
    SourceState,
)
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceValidationHooks,
    _issue_admission,
)
from tests.test_source_v1_pg_transactions import (
    CONTEXT,
    NATIVE,
    POLICY,
    SCOPE,
    _command,
    _fence,
    _mutate,
    source_backend,
)

PG = pytest.param("postgres", marks=pytest.mark.skipif(
    not os.environ.get("MEMPLEX_TEST_POSTGRES_DSN"), reason="native publication proof requires external PostgreSQL"))


def deadline():
    return time.monotonic_ns() + 2_000_000_000


@pytest.fixture(params=["lite", PG])
def publication_backend(request, tmp_path):
    if request.param == "postgres":
        store, backend, resources, trace, dsn, _ = request.getfixturevalue("source_backend")
        return SimpleNamespace(kind="postgres", store=store, backend=backend, resources=resources, trace=trace, dsn=dsn)
    store = LiteMemoryStore(tmp_path / "memory.json")
    backend = store.source_versions()
    hooks = SourceValidationHooks(
        lambda snapshot, context: snapshot.payload.get("tenant_id") == context.principal.tenant_id,
        lambda snapshot: snapshot.payload.get("unsafe") is not True,
    )
    admission = _issue_admission(store._durability, SCOPE, hooks, issuer=_RESOURCE_ISSUER)
    backend.initialize_authority(SCOPE, POLICY, admission=admission)
    return SimpleNamespace(kind="lite", store=store, backend=backend, trace=[])


def publish_command(backend, source, *, expected=ExpectedAbsent.ABSENT, value="derived", key="d" * 64):
    with _fence(backend) as session:
        authority = session.authority()
    # Fixed synthetic enhancer output; no model call or automatic retry.
    output = Fact(id="candidate", subject="item", predicate="is", object_=value,
                  trust_tier=1, tenant_id=SCOPE.tenant_id, workspace_id=SCOPE.workspace_id,
                  owner_subject_id="alice", visibility="workspace").to_dict()
    refs = (source.ref,)
    manifest = CaptureManifest(str(uuid.uuid4()), SCOPE.authority_id, authority.policy.revision,
                               authority.commit_seq, refs, {}, hashlib.sha256(_canonical(refs).encode()).hexdigest(),
                               ALGORITHM_VERSION)
    envelope = DerivationEnvelope(1, "factual_capture", ALGORITHM_VERSION, key,
                                  PUBLICATION_PREFIX + uuid.uuid4().hex, str(uuid.uuid4()), 1,
                                  SCOPE.authority_id, authority.policy.revision, refs, (), refs,
                                  output_digest(output), 1)
    return PublishCommand(str(uuid.uuid4()), key, expected, manifest, envelope, output)


def publish(backend, command):
    return backend.publish_if_current(CONTEXT, command, deadline_ns=deadline())


def snapshot(harness):
    if harness.kind == "lite":
        return harness.store._durability.load_authoritative()
    with _fence(harness.backend) as session:
        result = [session.authority()]
        for table in ("memplex_source_heads", "memplex_derivation_records", "memplex_facts"):
            session._execute(f"SELECT row_to_json(t) FROM {table} t ORDER BY row_to_json(t)::text")
            result.append(session._cursor.fetchall())
        return result


def reject_unchanged(harness, command, code):
    before = snapshot(harness)
    with pytest.raises(SourceContractError) as caught:
        publish(harness.backend, command)
    assert caught.value.code == code
    assert snapshot(harness) == before


def test_worker_source_revision_fence(publication_backend):
    h = publication_backend
    first = _mutate(h.backend, _command())
    old_job = publish_command(h.backend, first)
    second = _mutate(h.backend, _command(value="two", expected=first.ref.token, operation=SourceOperation.REPLACE))
    reject_unchanged(h, old_job, "source_changed")
    receipt = publish(h.backend, publish_command(h.backend, second))
    with _fence(h.backend) as session:
        assert session.target(old_job.derivation_key).token == receipt.target
        assert session.source(session.resolve_id(receipt.publication_id)).payload["object"] == "derived"


def test_target_aba_and_late_worker(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    first = publish(h.backend, publish_command(h.backend, source))
    late = publish_command(h.backend, source, expected=first.target)
    second = publish(h.backend, publish_command(h.backend, source, expected=first.target, value="newer"))
    reject_unchanged(h, late, "publication_conflict")
    deleted = h.backend.delete_target(CONTEXT, late.derivation_key, second.target, deadline_ns=deadline())
    assert deleted.token.generation == second.target.generation + 1
    reject_unchanged(h, publish_command(h.backend, source, expected=deleted.token), "publication_conflict")
    fresh = publish_command(h.backend, source, expected=deleted.token, value="recreated")
    recreated = h.backend.recreate_target(CONTEXT, fresh, deleted.token, deadline_ns=deadline())
    assert recreated.target.generation == first.target.generation == 1
    assert recreated.target.incarnation != first.target.incarnation
    reject_unchanged(h, late, "publication_conflict")
    with _fence(h.backend) as session:
        assert session.target(late.derivation_key).token == recreated.target


def test_same_request_id_is_exactly_idempotent(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    first = publish(h.backend, command)
    before = snapshot(h)
    second = publish(h.backend, command)
    assert second == replace(first, replayed=True)
    assert snapshot(h) == before
    changed_output = {**command.output, "object": "changed"}
    changed = replace(command, output=changed_output,
                      envelope=replace(command.envelope, output_digest=output_digest(changed_output)))
    reject_unchanged(h, changed, "publication_conflict")
    reject_unchanged(h, replace(command, manifest=replace(command.manifest, input_parameters={"role": "other"})),
                     "publication_conflict")
    h.backend.delete_target(CONTEXT, command.derivation_key, first.target, deadline_ns=deadline())
    after_delete = snapshot(h)
    assert publish(h.backend, command) == replace(first, replayed=True)
    assert snapshot(h) == after_delete


def test_supersede_removes_body_keeps_metadata(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    first = publish(h.backend, publish_command(h.backend, source, value="secret-old-body"))
    with _fence(h.backend) as session:
        old = session.source(session.resolve_id(first.publication_id))
    second = publish(h.backend, publish_command(h.backend, source, expected=first.target, value="new-body"))
    with _fence(h.backend) as session:
        assert session.target("d" * 64).token == second.target
        record = session.publication(first.publication_id)
        old_payload = session.source(old.head.ref.key).payload
        assert record.state is PublicationState.SUPERSEDED
        assert not old_payload
        assert "object" not in _canonical(record)
        assert "secret-old-body" not in _canonical(record)
        assert old.payload["object"] == "secret-old-body"  # caller cache remains, current authority does not
    assert "secret-old-body" not in _canonical(snapshot(h).memory if h.kind == "lite" else snapshot(h))


def test_target_discovered_after_model_cannot_rebase_old_job(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    old_job = publish_command(h.backend, source)
    first = publish(h.backend, publish_command(h.backend, source))
    # Exact source token is unchanged. Re-reading target cannot move the model's observation point.
    rebased = replace(old_job, expected_target=first.target)
    reject_unchanged(h, rebased, "publication_conflict")


def test_publication_validation_rejects_without_durable_decision(publication_backend, monkeypatch):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    decisions = []
    if h.kind == "lite":
        monkeypatch.setattr(h.store._durability, "after_journal_rename_and_parent_dir_fsync", lambda: decisions.append(1))
    else:
        h.trace.clear()
    reject_unchanged(h, replace(command, output={**command.output, "object": "digest mismatch"}), "malformed")
    if h.kind == "lite":
        assert decisions == []
    else:
        assert not any(item[0] == "start" and item[2].startswith(("INSERT", "UPDATE", "DELETE")) for item in h.trace)


@pytest.mark.parametrize("field,value,code", [
    ("trust_tier", 3, "unsupported_semantics"), ("unsafe", True, "unsafe"),
    ("valid_until", "2000-01-01T00:00:00+00:00", "expired"),
])
def test_invalid_output_has_no_publication(publication_backend, field, value, code):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    output = {**command.output, field: value}
    reject_unchanged(h, replace(command, output=output, envelope=replace(command.envelope, output_digest=output_digest(output))), code)


def test_source_auth_and_policy_checked_in_publication_fence(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    old_policy = replace(command, manifest=replace(command.manifest, policy_revision=2),
                         envelope=replace(command.envelope, policy_revision=2))
    reject_unchanged(h, old_policy, "auth_changed")
    changed = _command(expected=source.ref.token, operation=SourceOperation.REPLACE)
    changed = replace(changed, payload={**changed.payload, "visibility": "user"})
    _mutate(h.backend, changed)
    reject_unchanged(h, command, "auth_changed")


@pytest.mark.parametrize("boundary,new", [("before_journal_durable_publish", False),
                                          ("after_journal_rename_and_parent_dir_fsync", True),
                                          ("after_memory_replace", True)])
def test_lite_publication_crash_recovery(publication_backend, monkeypatch, boundary, new):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite durable-pair crash point")
    source = _mutate(h.backend, _command())
    first = publish(h.backend, publish_command(h.backend, source))
    command = publish_command(h.backend, source, expected=first.target, value="after-crash")

    def crash():
        raise OSError("owned publication crash")

    with monkeypatch.context() as patch:
        patch.setattr(h.store._durability, boundary, crash)
        with pytest.raises(OSError, match="owned publication crash"):
            publish(h.backend, command)
    recovered = h.store._durability.load_authoritative()
    ledger = decode_ledger(recovered.memory["source_validity"])
    assert len(ledger.publications) == (2 if new else 1)
    receipt = publish(h.backend, command)
    assert receipt.replayed is new
    assert ledger.targets[command.derivation_key].token.current_publication == (receipt.publication_id if new else first.publication_id)


@NATIVE
@pytest.mark.parametrize("sql_prefix", ["INSERT INTO memplex_facts", "INSERT INTO memplex_derivation_records", "UPDATE memplex_source_heads"])
def test_native_postgres_publication_rollback(request, monkeypatch, sql_prefix):
    if not os.environ.get("MEMPLEX_TEST_POSTGRES_DSN"):
        pytest.skip("native publication rollback requires external PostgreSQL")
    from memplex.storage.postgres_sources import _PostgresReadSession

    store, backend, _, trace, _, _ = request.getfixturevalue("source_backend")
    h = SimpleNamespace(kind="postgres", store=store, backend=backend, trace=trace)
    source = _mutate(backend, _command())
    first = publish(backend, publish_command(backend, source))
    command = publish_command(backend, source, expected=first.target, value="rollback")
    before = snapshot(h)
    execute = _PostgresReadSession._execute

    def fail_after_insert(session, sql, parameters=()):
        execute(session, sql, parameters)
        if sql.startswith(sql_prefix):
            raise RuntimeError("owned publication rollback")

    trace.clear()
    with monkeypatch.context() as patch:
        patch.setattr(_PostgresReadSession, "_execute", fail_after_insert)
        with pytest.raises(RuntimeError, match="owned publication rollback"):
            publish(backend, command)
    assert any(item[0] == "rollback" for item in trace)
    assert snapshot(h) == before
    assert not publish(backend, command).replayed


@pytest.mark.parametrize("kind", ["fact", "preference", "observation", "function"])
@pytest.mark.parametrize("reserved", ["prefix", "carrier"])
def test_ordinary_lite_writer_rejects_reserved_publication(tmp_path, kind, reserved):
    from memplex.models import Function, Observation, Preference, SourceDocument

    store = LiteMemoryStore(tmp_path / "legacy.json")
    nodes = {"fact": Fact(subject="a", predicate="is", object_="b"),
             "preference": Preference(aspect="a", preference="b"),
             "observation": Observation(event="event"), "function": Function(id="func_test", name="function")}
    node = nodes[kind]
    if reserved == "prefix":
        node.id = PUBLICATION_PREFIX + uuid.uuid4().hex
    else:
        node.namespace[CARRIER_KEY] = "{}"
    before = store._durability.load_authoritative()
    with pytest.raises(SourceContractError) as caught:
        if kind == "function":
            store.add(node, SourceDocument(type="text"))
        else:
            getattr(store, "add_" + kind)(node)
    assert caught.value.code == "unsupported_semantics"
    assert store._durability.load_authoritative() == before


@pytest.mark.parametrize("reserved", ["prefix", "carrier"])
def test_postgres_ordinary_guard_precedes_identity_and_sql(monkeypatch, reserved):
    from memplex.storage.postgres import PostgresMemoryStore

    node = Fact(subject="a", predicate="is", object_="b")
    if reserved == "prefix":
        node.id = PUBLICATION_PREFIX + uuid.uuid4().hex
    else:
        node.namespace[CARRIER_KEY] = "{}"
    store = object.__new__(PostgresMemoryStore)
    monkeypatch.setattr(store, "_authorization_context", lambda: pytest.fail("ordinary writer reached identity/SQL"))
    with pytest.raises(SourceContractError) as caught:
        store._write_identity(node)
    assert caught.value.code == "unsupported_semantics"


def test_publication_id_and_incarnation_are_backend_assigned(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    receipt = publish(h.backend, command)
    assert receipt.publication_id != command.envelope.publication_id
    assert receipt.target.incarnation != command.envelope.target_incarnation
    assert uuid.UUID(receipt.publication_id.removeprefix(PUBLICATION_PREFIX)).version == 4
    with _fence(h.backend) as session:
        record = session.publication(receipt.publication_id)
        assert record.request_digest and record.manifest_digest
        assert record.commit_seq == receipt.commit_seq <= session.authority().commit_seq


@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_two_workers_same_target_only_one_commit(publication_backend, monkeypatch, single_writer):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", single_writer)
    h = publication_backend
    source = _mutate(h.backend, _command())
    first = publish(h.backend, publish_command(h.backend, source))
    commands = [publish_command(h.backend, source, expected=first.target, value=x) for x in ("left", "right")]
    barrier = threading.Barrier(3)
    results = []

    def worker(command):
        barrier.wait(timeout=10)
        try:
            results.append(publish(h.backend, command))
        except SourceContractError as exc:
            results.append(exc.code)

    workers = [threading.Thread(target=worker, args=(command,)) for command in commands]
    try:
        for thread in workers:
            thread.start()
        barrier.wait(timeout=10)
    finally:
        for thread in workers:
            if thread.ident is not None:
                thread.join(10)
        barrier.abort()
        assert all(not thread.is_alive() for thread in workers)
    assert len(results) == 2
    assert sum(result == "publication_conflict" for result in results) == 1
    with _fence(h.backend) as session:
        target = session.target("d" * 64)
        assert target.token in [result.target for result in results if not isinstance(result, str)]
        assert target.token.generation == 2


def test_recreate_requires_fresh_manifest_and_new_request(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    receipt = publish(h.backend, command)
    old_job = publish_command(h.backend, source, expected=receipt.target)
    deleted = h.backend.delete_target(CONTEXT, command.derivation_key, receipt.target, deadline_ns=deadline())
    before = snapshot(h)
    for candidate in (replace(old_job, expected_target=deleted.token),
                      replace(publish_command(h.backend, source, expected=deleted.token), request_id=command.request_id)):
        with pytest.raises(SourceContractError) as caught:
            h.backend.recreate_target(CONTEXT, candidate, deleted.token, deadline_ns=deadline())
        assert caught.value.code == "publication_conflict"
        assert snapshot(h) == before


def test_exact_retry_race_returns_one_committed_id(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    barrier = threading.Barrier(3)
    results = []

    def worker():
        barrier.wait(timeout=10)
        results.append(publish(h.backend, command))

    workers = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in workers:
            thread.start()
        barrier.wait(timeout=10)
    finally:
        for thread in workers:
            if thread.ident is not None:
                thread.join(10)
        barrier.abort()
        assert all(not thread.is_alive() for thread in workers)
    assert len(results) == 2
    assert results[0].publication_id == results[1].publication_id
    assert sorted(result.replayed for result in results) == [False, True]
    assert {result.commit_seq for result in results} == {source.commit_seq + 1}


def test_success_is_one_committed_decision(publication_backend, monkeypatch):
    h = publication_backend
    source = _mutate(h.backend, _command())
    command = publish_command(h.backend, source)
    decisions = []
    if h.kind == "lite":
        monkeypatch.setattr(h.store._durability, "after_journal_rename_and_parent_dir_fsync", lambda: decisions.append(1))
    h.trace.clear()
    receipt = publish(h.backend, command)
    if h.kind == "lite":
        assert decisions == [1]
    else:
        import re

        # The real deadline cursor prefixes the statement in the same message.
        # Anchor to that fixed prefix; quoted/commented DML is not a write.
        dml = re.compile(r"^(?:SET LOCAL lock_timeout = '\d+ms'; "
                         r"SET LOCAL statement_timeout = '\d+ms'; )?\s*(?:INSERT|UPDATE|DELETE)\s")
        commits = [(index, item) for index, item in enumerate(h.trace) if item[0] == "commit"]
        assert len(commits) == 1
        writes = [item for item in h.trace if item[0] == "start" and dml.match(item[2])]
        finished = [(index, item) for index, item in enumerate(h.trace) if item[0] == "finish" and dml.match(item[2])]
        assert writes and [item[1:] for item in writes] == [item[1:] for _, item in finished]
        assert {item[1] for item in h.trace} == {commits[0][1][1]}
        assert all(index < commits[0][0] for index, _ in finished)
    with _fence(h.backend) as session:
        assert session.publication(receipt.publication_id).commit_seq == receipt.commit_seq
        assert session.target(command.derivation_key).token == receipt.target
        assert session.source(session.resolve_id(receipt.publication_id)).payload["object"] == "derived"


def test_stale_upstream_publication_cannot_publish_downstream(publication_backend):
    h = publication_backend
    source = _mutate(h.backend, _command())
    upstream = publish(h.backend, publish_command(h.backend, source))
    with _fence(h.backend) as session:
        cached = session.source(session.resolve_id(upstream.publication_id))
    downstream = publish_command(h.backend, SimpleNamespace(ref=cached.head.ref), key="e" * 64)
    second = publish(h.backend, publish_command(h.backend, source, expected=upstream.target, value="new"))
    reject_unchanged(h, downstream, "source_changed")
    # A cached old body plus its matching old carrier still cannot certify it.
    from memplex.source_contract import ValidationLimits
    from memplex.source_validation import validate_sources

    with _fence(h.backend) as session:
        class CachedRead:
            def __getattr__(self, name):
                return getattr(session, name)

            def source(self, key):
                return cached if key == cached.head.ref.key else session.source(key)

        result = validate_sources(CachedRead(), [upstream.publication_id, second.publication_id],
                                  authorization=CONTEXT, own_acl=lambda snapshot, context: True,
                                  safe_for_model=lambda snapshot: True,
                                  limits=ValidationLimits(), deadline_ns=deadline())
        assert result.rejected[upstream.publication_id] == "not_current_publication"
        assert second.publication_id in result.accepted


def test_reserved_id_in_typed_batch_is_rejected_before_staging(tmp_path):
    from memplex.storage.typed_batch import TypedWriteOperation

    store = LiteMemoryStore(tmp_path / "memory.json")
    node = Fact(id=PUBLICATION_PREFIX + uuid.uuid4().hex, subject="a", predicate="is", object_="b")
    before = store._durability.load_authoritative()
    with pytest.raises(SourceContractError):
        store._prepare_typed_write(TypedWriteOperation(node, 0, "input"), exists=False)
    assert store._durability.load_authoritative() == before



def _crash_publisher(backend, command, boundary, single_writer):
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = single_writer
    setattr(backend._owner._durability, boundary, lambda: os._exit(73))
    publish(backend, command)
    os._exit(74)


def _late_publisher(backend, command, ready, proceed, result):
    from tests.test_source_v1_lite_transactions import _close_native

    try:
        ready.set()
        assert proceed.wait(10)
        try:
            receipt = publish(backend, command)
            result.put(receipt.publication_id)
        except SourceContractError as exc:
            result.put(exc.code)
    finally:
        _close_native(backend._owner)
        result.close()
        result.join_thread()


def _fork_publication_scenario(path, boundary, single_writer, report):
    from tests.test_source_v1_lite_transactions import _close_native, _reap

    # As in the reviewed source-CAS test, a thread-free spawned coordinator
    # forks its exact admitted resource. Legacy V3 reopen remains prohibited.
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = "0"
    store = LiteMemoryStore(path / "memory.json")
    backend = store.source_versions()
    hooks = SourceValidationHooks(lambda snapshot, context: True, lambda snapshot: True)
    backend.initialize_authority(SCOPE, POLICY, admission=_issue_admission(store._durability, SCOPE, hooks, issuer=_RESOURCE_ISSUER))
    context = multiprocessing.get_context("fork")
    child = None
    result = None
    try:
        source = _mutate(backend, _command())
        command = publish_command(backend, source)
        if boundary:
            child = context.Process(target=_crash_publisher, args=(backend, command, boundary, single_writer))
            child.start()
            child_exit = _reap(child)
            child = None
            assert child_exit == 73
            ledger = decode_ledger(store._durability.load_authoritative().memory["source_validity"])
            receipt = publish(backend, command)
            with _fence(backend) as session:
                assert session.target(command.derivation_key).token == receipt.target
            report.put((child_exit, len(ledger.publications), receipt.replayed))
        else:
            first = publish(backend, command)
            command = publish_command(backend, source, expected=first.target)
            ready, proceed, result = context.Event(), context.Event(), context.Queue()
            child = context.Process(target=_late_publisher, args=(backend, command, ready, proceed, result))
            child.start()
            assert ready.wait(10)
            newer = publish(backend, publish_command(backend, source, expected=first.target, value="newer"))
            proceed.set()
            outcome = result.get(timeout=10)
            child_exit = _reap(child)
            child = None
            with _fence(backend) as session:
                assert session.target(command.derivation_key).token == newer.target
            report.put((child_exit, outcome))
    finally:
        if child is not None and child.pid is not None:
            _reap(child)
        if result is not None:
            result.close()
            result.join_thread()
        _close_native(store)
        report.close()
        report.join_thread()


@pytest.mark.parametrize("boundary,committed", [("before_journal_durable_publish", False),
                                               ("after_journal_rename_and_parent_dir_fsync", True)])
@pytest.mark.parametrize("single_writer", ["0", "1"])
def test_process_exit_preserves_atomic_publication(tmp_path, boundary, committed, single_writer):
    from tests.test_source_v1_lite_transactions import _reap

    context = multiprocessing.get_context("spawn")
    report = context.Queue()
    coordinator = context.Process(target=_fork_publication_scenario, args=(tmp_path, boundary, single_writer, report))
    coordinator.start()
    try:
        assert report.get(timeout=20) == (73, int(committed), committed)
    finally:
        exitcode = _reap(coordinator)
        report.close()
        report.join_thread()
    assert exitcode == 0


def test_late_worker_rejected_across_processes(tmp_path):
    from tests.test_source_v1_lite_transactions import _reap

    context = multiprocessing.get_context("spawn")
    report = context.Queue()
    coordinator = context.Process(target=_fork_publication_scenario, args=(tmp_path, None, "0", report))
    coordinator.start()
    try:
        assert report.get(timeout=20) == (0, "publication_conflict")
    finally:
        exitcode = _reap(coordinator)
        report.close()
        report.join_thread()
    assert exitcode == 0


@NATIVE
def test_native_commit_exception_returns_no_publication_receipt(request, monkeypatch):
    import psycopg2

    store, backend, resources, _, dsn, _ = request.getfixturevalue("source_backend")
    source = _mutate(backend, _command())
    first = publish(backend, publish_command(backend, source))
    command = publish_command(backend, source, expected=first.target, value="uncommitted")
    connection_class = type(store._pool_manager._pool._pool[0])

    def fail_commit(connection):
        raise RuntimeError("owned native publication commit failure")

    resources._test_expected_fault = True
    with monkeypatch.context() as patch:
        patch.setattr(connection_class, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="publication commit failure"):
            publish(backend, command)
    assert store._pool_manager.business_lease_count == 0
    connection = psycopg2.connect(dsn)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT data->>'object' FROM memplex_facts WHERE id=%s", (first.publication_id,))
            assert cursor.fetchone() == ("derived",)
            cursor.execute("SELECT count(*) FROM memplex_derivation_records WHERE record_kind='publication'")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT data->>'state' FROM memplex_derivation_records WHERE record_key=%s", (first.publication_id,))
            assert cursor.fetchone() == ("active",)
            cursor.execute("SELECT commit_seq FROM memplex_source_authorities")
            assert cursor.fetchone() == (first.commit_seq,)
    finally:
        connection.close()


@pytest.fixture
def publication_rls_backend(request, monkeypatch):
    """Actual non-bypass PostgreSQL role; controlled fixture bootstrap only."""
    import psycopg2
    from psycopg2 import sql

    from memplex.source_contract import PolicyEntry

    store, backend, _, _, dsn, _ = request.getfixturevalue("source_backend")
    peer = replace(CONTEXT, principal=replace(CONTEXT.principal, subject_id="bob", authentication_id="credential-bob"),
                   session_id="session-bob")
    policy = replace(POLICY, entries=(*POLICY.entries, PolicyEntry(
        peer.principal.authentication_id, SCOPE.tenant_id, peer.principal.subject_id,
        SCOPE.workspace_id, peer.agent_id, peer.principal.roles)))
    connection = psycopg2.connect(dsn)
    role = "publication_writer_" + uuid.uuid4().hex
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute("UPDATE memplex_source_authorities SET policy=%s::jsonb WHERE authority_id=%s",
                           (_canonical(policy), SCOPE.authority_id))
            cursor.execute("SELECT current_schema()")
            schema = cursor.fetchone()[0]
            cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON memplex_source_authorities, memplex_source_heads, "
                                   "memplex_derivation_records TO {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON memplex_facts TO {}").format(sql.Identifier(role)))
        bind = store._bind_transaction_scope

        def app_role(cursor, context):
            bind(cursor, context)
            cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            cursor.execute("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")
            assert cursor.fetchone() == (role, False, False)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_bind_transaction_scope", app_role)
            yield SimpleNamespace(store=store, backend=backend, peer=peer, kind="postgres")
    finally:
        connection.rollback()
        with connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        connection.close()


@NATIVE
@pytest.mark.parametrize("operation", ["supersede", "delete", "recreate"])
@pytest.mark.parametrize("actor,visibility", [("renewed", "workspace"), ("renewed", "user"), ("peer", "workspace")])
def test_native_publication_effective_rls(publication_rls_backend, actor, visibility, operation):
    h = publication_rls_backend
    source = _mutate(h.backend, _command())
    original = publish_command(h.backend, source)
    output = {**original.output, "visibility": visibility}
    original = replace(original, output=output, envelope=replace(original.envelope, output_digest=output_digest(output)))
    first = publish(h.backend, original)
    with _fence(h.backend) as session:
        old_record = session.publication(first.publication_id)
        old_key = session.resolve_id(first.publication_id)
    context = replace(CONTEXT, session_id="session-alice-renewed") if actor == "renewed" else h.peer
    backend = h.store.authorized(context).source_versions()
    token = first.target
    if operation == "recreate":
        token = h.backend.delete_target(CONTEXT, original.derivation_key, first.target, deadline_ns=deadline()).token
    if operation == "delete":
        result = backend.delete_target(context, original.derivation_key, token, deadline_ns=deadline())
        assert result.state is SourceState.DELETED and result.token.generation == token.generation + 1
    else:
        command = publish_command(h.backend, source, expected=token, value="new owner-bound output")
        output = {**command.output, "visibility": visibility}
        command = replace(command, output=output, envelope=replace(command.envelope, output_digest=output_digest(output)))
        if operation == "recreate":
            result = backend.recreate_target(context, command, token, deadline_ns=deadline())
            assert result.target.incarnation != token.incarnation and result.target.generation == 1
        else:
            result = backend.publish_if_current(context, command, deadline_ns=deadline())
            assert result.target.generation == token.generation + 1
        with _fence(backend, context) as session:
            current = session.source(session.resolve_id(result.publication_id))
            assert current.payload["owner_subject_id"] == context.principal.subject_id
            assert current.payload["provenance"]["session_id"] == context.session_id
            assert current.payload["provenance"]["authentication_id"] == context.principal.authentication_id
            assert session.target(command.derivation_key).token == result.target
    with _fence(h.backend) as session:
        expected_state = PublicationState.SUPERSEDED if operation == "supersede" else PublicationState.DELETED
        assert session.publication(first.publication_id) == replace(old_record, state=expected_state)
        assert session.source(old_key).payload == {}
        session._execute("SELECT visibility FROM memplex_source_heads WHERE logical_id=%s", (first.publication_id,))
        assert session._cursor.fetchone() == (visibility,)
        session._execute("SELECT visibility FROM memplex_derivation_records WHERE record_key=%s", (first.publication_id,))
        assert session._cursor.fetchone() == (visibility,)
    before = snapshot(h)
    assert publish(h.backend, original) == replace(first, replayed=True)
    with pytest.raises(SourceContractError) as changed_caller:
        backend.publish_if_current(context, original, deadline_ns=deadline())
    assert changed_caller.value.code == "publication_conflict"
    assert snapshot(h) == before


@NATIVE
@pytest.mark.parametrize("visibility", ["user", "session"])
def test_native_publication_target_visibility(publication_rls_backend, visibility):
    h = publication_rls_backend
    source = _mutate(h.backend, _command())
    original = publish_command(h.backend, source)
    output = {**original.output, "visibility": visibility}
    original = replace(original, output=output, envelope=replace(original.envelope, output_digest=output_digest(output)))
    first = publish(h.backend, original)
    second = publish(h.backend, publish_command(h.backend, source, expected=first.target))
    peer_backend = h.store.authorized(h.peer).source_versions()
    with _fence(peer_backend, h.peer) as session:
        assert session.target(original.derivation_key).token == second.target
        assert session.source(session.resolve_id(second.publication_id)).payload["visibility"] == "workspace"
        assert session.publication(first.publication_id) is None
        assert session.resolve_id(first.publication_id) is None
    with _fence(h.backend) as session:
        assert session.publication(first.publication_id).state is PublicationState.SUPERSEDED
        session._execute("SELECT visibility FROM memplex_derivation_records WHERE record_key=%s", (first.publication_id,))
        assert session._cursor.fetchone() == (visibility,)
    # A newly visible workspace target is usable by an admitted workspace peer.
    peer_backend.delete_target(h.peer, original.derivation_key, second.target, deadline_ns=deadline())


@NATIVE
@pytest.mark.parametrize("denial", ["user", "session", "unadmitted", "tenant", "workspace"])
def test_native_publication_rls_denial(publication_rls_backend, denial):
    h = publication_rls_backend
    source = _mutate(h.backend, _command())
    original = publish_command(h.backend, source)
    visibility = denial if denial in {"user", "session"} else "workspace"
    output = {**original.output, "visibility": visibility}
    original = replace(original, output=output, envelope=replace(original.envelope, output_digest=output_digest(output)))
    first = publish(h.backend, original)
    context = h.peer
    if denial == "session":
        context = replace(CONTEXT, session_id="session-alice-renewed")
    elif denial == "unadmitted":
        context = replace(context, principal=replace(context.principal, authentication_id="unadmitted-credential"))
    elif denial == "tenant":
        context = replace(context, principal=replace(context.principal, tenant_id="other-tenant"))
    elif denial == "workspace":
        context = replace(context, workspace_id="other-workspace")
    before = snapshot(h)
    command = publish_command(h.backend, source, expected=first.target)
    backend = h.store.authorized(context).source_versions()
    with pytest.raises(SourceContractError):
        backend.publish_if_current(context, command, deadline_ns=deadline())
    assert snapshot(h) == before
    with pytest.raises(SourceContractError):
        backend.delete_target(context, original.derivation_key, first.target, deadline_ns=deadline())
    assert snapshot(h) == before


@pytest.mark.parametrize("operation", ["target", "retire"])
def test_record_update_rebinds_current_identity(operation):
    """Local mutation protocol regression; actual RLS is tested natively above."""
    import json
    import re

    from memplex.source_codec import _decode_value
    from memplex.source_contract import (
        PublicationRecord,
        SourceClass,
        SourceHead,
        SourceKey,
        SourceKind,
        SourceRef,
        SourceSnapshot,
        SourceToken,
        TargetHead,
        TargetToken,
    )
    from memplex.storage.postgres_sources import PostgresSourceVersionStore

    target = TargetHead("d" * 64, TargetToken(str(uuid.uuid4()), 2, PUBLICATION_PREFIX + uuid.uuid4().hex), SourceState.LIVE, 4)
    identity = (SCOPE.tenant_id, "alice", SCOPE.workspace_id, "workspace", CONTEXT.agent_id, "renewed-session")
    columns = ("tenant_id", "owner_subject", "workspace", "visibility", "source_agent", "source_session")
    original = dict(zip(columns, (SCOPE.tenant_id, "alice", SCOPE.workspace_id, "user", CONTEXT.agent_id, CONTEXT.session_id)))
    rows = {table: dict(original) for table in ("memplex_source_heads", "memplex_derivation_records")}
    deleted = []

    class Session:
        _scope = SCOPE

        def __init__(self):
            self.result = None
            self._cursor = SimpleNamespace(fetchone=lambda: self.result)

        def _execute(self, statement, parameters):
            if statement.startswith("DELETE FROM memplex_facts"):
                deleted.append(parameters[0])
                self.result = (parameters[0],)
                return
            row = rows[statement.split()[1]]
            assignments = statement.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            values = iter(parameters)
            for name, expression in re.findall(r"(\w+)\s*=\s*(%s(?:::jsonb)?|'[^']*')", assignments):
                value = next(values) if expression.startswith("%s") else expression[1:-1]
                row[name] = json.loads(value) if name in {"data", "head_data"} else value
            # The frozen WITH CHECK binds the resulting row to this writer.
            if (row["owner_subject"], row["source_agent"], row["source_session"]) != (identity[1], identity[4], identity[5]):
                raise PermissionError("resulting row violates frozen writer binding")
            self.result = (parameters[-1],)

    backend = object.__new__(PostgresSourceVersionStore)
    session = Session()
    if operation == "target":
        backend._store_record(session, "target", target.derivation_key, target, identity)
        assert tuple(rows["memplex_derivation_records"][column] for column in columns) == identity
        assert rows["memplex_derivation_records"]["data"]["token"]["generation"] == 2
    else:
        publication_id = target.token.current_publication
        ref = SourceRef(SourceKey(SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id, SourceKind.FACT, publication_id),
                        SourceToken(str(uuid.uuid4()), 1, 1), 1, "e" * 64)
        payload = {"id": publication_id, "tenant_id": SCOPE.tenant_id, "owner_subject_id": "alice",
                   "workspace_id": SCOPE.workspace_id, "visibility": "user", "agent_id": CONTEXT.agent_id,
                   "session_id": CONTEXT.session_id}
        old = SourceSnapshot(SourceHead(ref, SourceState.LIVE, SourceClass.DERIVED_V1, "body", 4, {}, ()), payload)
        record = PublicationRecord(publication_id, target.derivation_key, target.token, PublicationState.ACTIVE,
                                   "a" * 64, "b" * 64, (ref,), ALGORITHM_VERSION, str(uuid.uuid4()), "c" * 64, "d" * 64, 2, 4)
        result = backend._retire_publication(session, replace(CONTEXT, session_id="renewed-session"),
                                             old, record, 5, PublicationState.SUPERSEDED)
        historical = (*identity[:3], "user", *identity[4:])
        assert result == historical and deleted == [publication_id]
        for row in rows.values():
            assert tuple(row[column] for column in columns) == historical
        assert rows["memplex_source_heads"]["state"] == "deleted"
        assert rows["memplex_source_heads"]["payload_locator"] == ""
        assert rows["memplex_source_heads"]["head_data"] == {"input_context": {}, "root_refs": []}
        assert rows["memplex_source_heads"]["content_revision"] == 2
        data = dict(rows["memplex_derivation_records"]["data"])
        assert data.pop("contract_version") == 1
        assert _decode_value(PublicationRecord, data) == replace(record, state=PublicationState.SUPERSEDED)
