"""T14 complete native decisions, terminal cancellation and quarantined restart."""
from __future__ import annotations

import json
import os
import time
from contextlib import ExitStack, closing
from dataclasses import asdict, replace

import pytest

from memplex.source_contract import SourceBusyError, SourceContractError, SourceOperation
from memplex.storage.lite.durability import LitePair
from tests.source_v1_concurrency_support import (
    CONFIG,
    CONTEXT,
    NATIVE,
    POLICY,
    SCOPE,
    Barrier,
    assert_read,
    authoritative,
    child_failure,
    close_lite,
    close_pg,
    command_wire,
    completed,
    deadline,
    decode_command,
    fence,
    mutate,
    nominate,
    open_peer,
    preparation_deadline,
    prepare_mutation,
    prepare_publication,
    publish,
    read,
    receive,
    seed,
    service_for,
    session_type,
    start_process,
    thread_call,
)
from tests.source_v1_concurrency_support import (
    t14_bank as t14_bank,  # noqa: PLC0414 - fixture registration
)
from tests.test_source_v1_pg_transactions import _command
from tests.test_source_v1_pg_transactions import (
    source_backend as source_backend,  # noqa: PLC0414 - fixture registration
)
from tests.test_source_v1_policy import policy_authority
from tests.test_source_v1_publication import publish_command

NEW = "T14_RECOVERED_NEW_PUBLICATION"
TABLES = ("memplex_source_authorities", "memplex_source_heads", "memplex_derivation_records", "memplex_facts")


def _seed_recovery(bank):
    dead = prepare_mutation(bank, _command(name="retained-tombstone"))
    target = prepare_publication(bank, publish_command(bank.backend, dead, key="b" * 64))
    deleted = bank.backend.delete_target(CONTEXT, "b" * 64, target.target, deadline_ns=preparation_deadline())
    dead = prepare_mutation(bank, _command(name="retained-tombstone", expected=dead.ref.token, operation=SourceOperation.DELETE))
    bank.backend.replace_policy(1, replace(POLICY, revision=2), policy_authority(bank))
    data = seed(bank)
    data.dead_source, data.dead_target = dead, deleted
    return data


def _pending(bank, data, operation):
    if operation == "source":
        return _command(value=NEW, expected=data.source.ref.token, operation=SourceOperation.REPLACE)
    return publish_command(bank.backend, data.source, expected=data.derived.target, value=NEW)


def _perform(bank, command, operation, end=None):
    method = bank.backend.mutate_source if operation == "source" else bank.backend.publish_if_current
    return method(CONTEXT, command, deadline_ns=deadline() if end is None else end)


def _native_rows(connection):
    with closing(connection.cursor()) as cursor:
        result = {}
        for table in TABLES:
            cursor.execute(f"SELECT row_to_json(t) FROM {table} t ORDER BY row_to_json(t)::text")
            result[table] = cursor.fetchall()
        return result


def _capture_target(bank, patch, proof):
    if bank.kind == "lite":
        owner = bank.store._durability
        original = owner.commit_locked

        def capture(base, target, **kwargs):
            # Source-bound: commit/validators must not mutate this owned graph.
            # LitePair is only shallowly frozen; re-audit if that path changes.
            assert "proposed" not in proof
            proof["proposed"] = target
            return original(base, target, **kwargs)

        patch.setattr(owner, "commit_locked", capture)


def _materialize_target(proof):
    """Detach the complete Lite target only after the operation is terminal."""
    target = proof["proposed"]
    assert type(target) is LitePair
    proof["proposed"] = asdict(target)


def _crash_child(pipe, spec, wire, operation, phase):
    try:
        with ExitStack() as stack, pytest.MonkeyPatch.context() as patch:
            bank = open_peer(spec, stack.callback)
            command = decode_command(wire, publication=operation == "publication")
            proof = {"phase": phase, "pid": os.getpid(), "operation": operation}
            _capture_target(bank, patch, proof)

            def crash():
                pipe.send({"state": "fault", "proof": proof})
                os._exit(73)

            if bank.kind == "lite":
                patch.setattr(bank.store._durability, phase, crash)
            elif phase == "after_payload_before_head":
                cls = session_type(bank)
                original = cls._execute

                def partial(session, sql, parameters=()):
                    result = original(session, sql, parameters)
                    if sql.startswith(("UPDATE memplex_facts SET data=", "INSERT INTO memplex_facts ")):
                        proof["backend_pid"] = session._cursor.connection.get_backend_pid()
                        proof["payload_sql_completed"] = True
                        crash()
                    return result

                patch.setattr(cls, "_execute", partial)
            else:
                cls = type(bank.store._pool_manager._pool._pool[0])
                original = cls.commit

                def committed(connection):
                    proof["proposed"] = _native_rows(connection)
                    original(connection)
                    proof["backend_pid"] = connection.get_backend_pid()
                    proof["after_durable_decision"] = True
                    crash()

                patch.setattr(cls, "commit", committed)
            pipe.send({"state": "ready", "pid": os.getpid()})
            assert pipe.recv() == "start"
            _perform(bank, command, operation)
            pipe.send({"state": "unexpected_receipt"})
    except BaseException as exc:  # noqa: BLE001 - child failure must not masquerade as an expected exit
        child_failure(pipe, exc)
    finally:
        pipe.close()


def _ambiguous(bank, command, operation, patch):
    proof = {"phase": "acknowledgment_loss", "pid": os.getpid()}
    _capture_target(bank, patch, proof)
    if bank.kind == "lite":
        from memplex.storage.lite import durability
        owner = bank.store._durability
        original = durability._fsync_dir

        def uncertain(path):
            if owner._journal_path.exists():
                proof["decision_unknown"] = True
                raise OSError("T14 journal parent fsync outcome unknown")
            return original(path)

        patch.setattr(durability, "_fsync_dir", uncertain)
        with pytest.raises(OSError, match="outcome unknown"):
            _perform(bank, command, operation)
        assert owner._poisoned
    else:
        cls = type(bank.store._pool_manager._pool._pool[0])
        original = cls.commit

        def lost(connection):
            proof["proposed"] = _native_rows(connection)
            original(connection)
            proof["after_durable_decision"] = True
            proof["backend_pid"] = connection.get_backend_pid()
            raise RuntimeError("T14 successful commit acknowledgment lost")

        bank.resources._test_expected_fault = True
        patch.setattr(cls, "commit", lost)
        with pytest.raises(RuntimeError, match="acknowledgment lost"):
            _perform(bank, command, operation)
        assert bank.store._pool_manager._pool.closed
        assert bank.store._pool_manager.business_lease_count == 0
    proof["success_receipt"] = False
    return proof


def _check_recovered(bank, data, operation, committed, path, patch, stack):
    with fence(bank) as session:
        assert session.authority().policy.revision == 2
        assert session.source(data.dead_source.ref.key).head == data.dead_source_head
        assert session.target("b" * 64) == data.dead_target
        source = session.source(data.source.ref.key)
        target = session.target(data.command.derivation_key)
        if operation == "source":
            assert source.payload["object"] == (NEW if committed else "source before change")
            assert source.head.ref.token.content_revision == data.source.ref.token.content_revision + int(committed)
            assert target.token == data.derived.target
        else:
            assert source.head.ref == data.source.ref
            assert target.token.generation == data.derived.target.generation + int(committed)
        current_id = target.token.current_publication
        record = session.publication(data.derived.publication_id)
        assert record.state.value == ("superseded" if operation == "publication" and committed else "active")
        old = session.source(session.resolve_id(data.derived.publication_id))
        if operation == "publication" and committed:
            assert old.payload is None or not old.payload
    svc = service_for(bank, path, stack.callback)
    nominate(patch, [data.derived.publication_id, current_id, "u"] if current_id != data.derived.publication_id
             else [current_id, "u"])
    result = read(svc)
    assert_read(result, data.derived.publication_id, accepted=not committed)
    if operation == "publication" and committed:
        assert current_id in result["ids"] and NEW in result["text"]
    return current_id


def _crash_cases():
    phases = ("before_journal_durable_publish", "after_journal_rename_and_parent_dir_fsync",
              "after_memory_replace", "after_changelog_replace", "acknowledgment_loss")
    cases = [pytest.param(setting, operation, phase, id=f"{setting}-{operation}-{phase}")
             for setting in ("lite0", "lite1") for operation in ("source", "publication") for phase in phases]
    cases += [pytest.param("postgres", operation, phase, marks=NATIVE, id=f"pg-{operation}-{phase}")
              for operation in ("source", "publication")
              for phase in ("after_payload_before_head", "after_durable_decision", "acknowledgment_loss")]
    return cases


@pytest.mark.parametrize("t14_bank,operation,phase", _crash_cases(), indirect=["t14_bank"])
def test_payload_and_ledger_crash_atomicity(t14_bank, operation, phase, tmp_path, monkeypatch, record_property):
    bank = t14_bank
    data = _seed_recovery(bank)
    with fence(bank) as session:
        data.dead_source_head = session.source(data.dead_source.ref.key).head
    pending = _pending(bank, data, operation)
    old = authoritative(bank)
    with ExitStack() as stack:
        if phase == "acknowledgment_loss":
            with monkeypatch.context() as fault:
                proof = _ambiguous(bank, pending, operation, fault)
        else:
            child, pipe = start_process(stack.callback, _crash_child, bank.spec, command_wire(pending), operation, phase)
            receive(pipe, "ready")
            pipe.send("start")
            proof = receive(pipe, "fault")["proof"]
            child.join(5)
            assert child.exitcode == 73
            proof["exitcode"] = child.exitcode
        if bank.kind == "lite":
            _materialize_target(proof)
        fresh = open_peer(bank.spec, stack.callback)
        actual = authoritative(fresh)
        if phase in {"before_journal_durable_publish", "after_payload_before_head"}:
            assert actual == old
            committed = False
        elif bank.kind == "lite" and phase == "acknowledgment_loss":
            assert actual in (old, proof["proposed"])
            committed = actual == proof["proposed"]
        else:
            assert actual == proof["proposed"]
            committed = True
        current_id = _check_recovered(fresh, data, operation, committed, tmp_path / "cold-service", monkeypatch, stack)
        if operation == "publication" and committed:
            receipt = publish(fresh, pending)
            assert receipt.replayed and receipt.publication_id == current_id
            assert authoritative(fresh) == actual
        proof.pop("proposed", None)
        proof.update(complete_new=committed, complete_old=not committed, policy_revision=2)
        record_property("recovery", json.dumps(proof, sort_keys=True))


def _timeout_cases():
    cases = [("lite0", "lock"), ("lite1", "lock"), ("lite1", "queue")]
    return [pytest.param(setting, boundary, operation, id=f"{setting}-{boundary}-{operation}")
            for setting, boundary in cases for operation in ("source", "publication")] + [
        pytest.param("postgres", boundary, operation, marks=NATIVE, id=f"pg-{boundary}-{operation}")
        for boundary in ("capacity", "lock") for operation in ("source", "publication")]


def _hold_blocker(bank, boundary, stack):
    gate = Barrier.create("before_publish_compare", stack.callback)
    if bank.kind == "lite":
        def block():
            if boundary == "queue":
                bank.store._durability._single_writer.submit(gate.arrive)
            else:
                with bank.store._durability.writer_lock():
                    gate.arrive()
        worker = thread_call(stack.callback, block, releases=(gate.release,))
        gate.reached()
        return lambda: (gate.release.set(), completed(worker))
    if boundary == "capacity":
        held = ExitStack()
        stack.callback(held.close)
        for _ in range(2):
            held.enter_context(bank.store._pool_manager.transaction(bank.store._bind_transaction_scope, CONTEXT))
        assert bank.store._pool_manager.business_lease_count == 2
        return held.close
    import psycopg2

    from memplex.storage.postgres_sources import _source_lock_key
    connection = psycopg2.connect(bank.spec["dsn"])
    stack.callback(connection.close)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", (_source_lock_key(bank.store._ready_pool.target, SCOPE.authority_id),))
    return connection.rollback


@pytest.mark.parametrize("t14_bank,boundary,operation", _timeout_cases(), indirect=["t14_bank"])
def test_timeout_does_not_commit_later(t14_bank, boundary, operation, tmp_path, monkeypatch, record_property):
    bank = t14_bank
    data = _seed_recovery(bank)
    pending = _pending(bank, data, operation)
    before = authoritative(bank)
    decisions = []
    with ExitStack() as stack, monkeypatch.context() as patch:
        unblock = _hold_blocker(bank, boundary, stack)
        if bank.kind == "lite":
            patch.setattr(bank.store._durability, "after_journal_rename_and_parent_dir_fsync",
                          lambda: decisions.append("durable"))
        else:
            bank.trace.clear()
        with pytest.raises(SourceBusyError):
            _perform(bank, pending, operation, time.monotonic_ns() + 30_000_000)
        # The blocker is held until the API has returned its terminal cancellation.
        unblock()
        if bank.kind == "lite" and getattr(bank.store._durability, "_single_writer", None):
            bank.store._durability._single_writer.drain()
        assert decisions == []
        if bank.kind == "postgres":
            assert not any(row[0] == "finish" and row[2].startswith(("UPDATE ", "INSERT ", "DELETE "))
                           for row in bank.trace)
        assert authoritative(bank) == before
        fresh = open_peer(bank.spec, stack.callback)
        assert authoritative(fresh) == before
        svc = service_for(fresh, tmp_path / "cold-service", stack.callback)
        nominate(patch, [data.derived.publication_id, "u"])
        assert_read(read(svc), data.derived.publication_id, accepted=True)
    record_property("terminal_timeout", json.dumps({"backend": bank.kind, "boundary": boundary,
                    "operation": operation, "deadline_ms": 30, "late_decisions": 0}))


def _restart_child(pipe, spec):
    from memplex.service import MemplexService
    from memplex.storage.lite.durability import LiteDurability, LiteStorageIntegrityError
    from memplex.storage.pool import PostgresStorageResources
    from tests.test_source_v1_compatibility import legacy_config

    try:
        pipe.send({"state": "ready", "pid": os.getpid()})
        assert pipe.recv() == "start"
        rejected = []
        for mode in ("off", "v1"):
            with ExitStack() as stack, pytest.MonkeyPatch.context() as patch:
                lite_init, pg_init = LiteDurability.__init__, PostgresStorageResources.__init__

                def owned_lite(owner, *args, _init=lite_init, **kwargs):
                    _init(owner, *args, **kwargs)
                    stack.callback(close_lite, owner)

                def owned_pg(owner, *args, _init=pg_init, **kwargs):
                    _init(owner, *args, **kwargs)
                    stack.callback(close_pg, owner)

                patch.setattr(LiteDurability, "__init__", owned_lite)
                patch.setattr(PostgresStorageResources, "__init__", owned_pg)
                from pathlib import Path
                config = legacy_config(Path(spec["path"]).parent if spec["kind"] == "lite" else Path("unused"))
                if spec["kind"] == "postgres":
                    config.storage.backend = "postgres"
                    config.storage.path = spec["dsn"]
                else:
                    patch.setenv("MEMPLEX_LITE_SINGLE_WRITER", spec["single_writer"])
                if mode == "v1":
                    config.source_contract = CONFIG
                try:
                    service = MemplexService(config)
                except (SourceContractError, LiteStorageIntegrityError) as exc:
                    if isinstance(exc, SourceContractError):
                        assert exc.code == "quarantined"
                    elif mode == "v1":
                        assert str(exc) == "invalid Lite authoritative payload"
                        assert isinstance(exc.__cause__, SourceContractError)
                        assert exc.__cause__.code == "quarantined"
                    else:
                        assert str(exc) == "Lite V3 requires a trusted source read session"
                    rejected.append(mode)
                else:
                    stack.callback(service.stop)
                    pytest.fail("quarantined backup obtained public admission")
        pipe.send({"state": "rejected", "modes": rejected, "pid": os.getpid()})
    except BaseException as exc:  # noqa: BLE001 - unexpected startup failures are not quarantine proof
        child_failure(pipe, exc)
    finally:
        pipe.close()


def _advance_after_backup(bank, data):
    deleted = prepare_mutation(bank, _command(expected=data.source.ref.token, operation=SourceOperation.DELETE))
    recreated = prepare_mutation(bank, _command(value="source before change", expected=deleted.ref.token,
                                      operation=SourceOperation.RECREATE))
    assert recreated.ref.token.incarnation != data.source.ref.token.incarnation
    updated = prepare_publication(bank, publish_command(bank.backend, recreated, expected=data.derived.target, value=NEW))
    bank.backend.delete_target(CONTEXT, data.command.derivation_key, updated.target, deadline_ns=preparation_deadline())
    bank.backend.replace_policy(2, replace(POLICY, revision=3), policy_authority(bank))
    with fence(bank) as session:
        return {"authority": command_wire(session.authority()), "source": command_wire(session.source(recreated.ref.key).head),
                "target": command_wire(session.target(data.command.derivation_key))}


def _restore_lite(bank, artifact, key, phase, path, stack, patch):
    from memplex.source_compatibility import (
        _lite_restore_authority,
        _read_lite_marker,
        begin_v1_restore,
    )
    from memplex.storage.lite.durability import LiteDurability
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.source_versions import _RESOURCE_ISSUER

    target = LiteMemoryStore(path / "memory.json")
    owner = target._durability
    stack.callback(close_lite, owner)
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    receipt = begin_v1_restore(authority.target, authority=authority)
    marker = _read_lite_marker(owner)
    assert marker["state"] == "quarantined" and marker["restore_id"] == receipt.restore_id
    if phase != "after_quarantine":
        def interrupt():
            assert _read_lite_marker(owner) == marker
            raise RuntimeError("T14 interrupted after durable restore")

        if phase == "after_import":
            patch.setattr(owner, "after_journal_rename_and_parent_dir_fsync", interrupt)
            with pytest.raises(RuntimeError, match="after durable restore"):
                target.restore_backup(artifact, key, authority=authority)
        else:
            target.restore_backup(artifact, key, authority=authority)
    close_lite(owner)
    assert _read_lite_marker(owner) == marker
    recovered = LiteDurability(target._path, target._changelog._path).load_authoritative()
    return ({"kind": "lite", "path": str(target._path), "single_writer": bank.spec["single_writer"]},
            marker, asdict(recovered))


def _pg_marker(spec):
    import psycopg2

    from memplex.storage.postgres_resources import _source_schema_marker
    with closing(psycopg2.connect(spec["dsn"])) as connection, connection.cursor() as cursor:
        return _source_schema_marker(cursor, spec["schema"])


def _restore_pg(bank, executor, artifact, key, phase, patch):
    import re

    import psycopg2
    from psycopg2 import sql

    from memplex.backup import BackupIntegrityError
    from memplex.source_compatibility import begin_v1_restore
    from memplex.storage.source_versions import _RESOURCE_ISSUER

    target = bank.resources.ready_pool.target
    dsn, schema = bank.spec["dsn"], target.schema
    # Exactly the randomly named function fixture, not a user schema or a glob.
    assert re.fullmatch(r"g003_function_[0-9a-f]{32}", schema)
    assert psycopg2.extensions.parse_dsn(dsn)["options"] == "-c search_path=" + schema
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_schema(), current_database(), nspowner=(SELECT oid FROM pg_roles "
                       "WHERE rolname=current_user) FROM pg_namespace WHERE nspname=%s", (schema,))
        assert cursor.fetchone() == (schema, target.database, True)
    bank.resources.close()
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    # Q is the NEW schema's comment, durable before import. No further DROP occurs.
    authority = executor._restore_authority(dsn, SCOPE, issuer=_RESOURCE_ISSUER)
    begin_v1_restore(authority.target, authority=authority)
    spec = {"kind": "postgres", "dsn": dsn, "schema": schema}
    marker = _pg_marker(spec)
    assert marker["state"] == "quarantined"
    if phase != "after_quarantine":
        original = executor._restore_list

        def imported(*args):
            before = _pg_marker(spec)
            assert before["restore_id"] == marker["restore_id"] and before["state"] == "quarantined"
            original(*args)
            raise RuntimeError("T14 interrupted after native import")

        if phase == "after_import":
            patch.setattr(executor, "_restore_list", imported)
            with pytest.raises(BackupIntegrityError, match="quarantined"):
                executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key,
                                 target_schema=schema, authority=authority)
        else:
            assert executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key,
                                    target_schema=schema, authority=authority).restored
    marker = _pg_marker(spec)
    with closing(psycopg2.connect(dsn)) as connection:
        restored = _native_rows(connection) if phase != "after_quarantine" else None
    return spec, marker, restored


@pytest.mark.parametrize("t14_bank", ["lite0", "lite1", pytest.param("postgres", marks=NATIVE)], indirect=True)
@pytest.mark.parametrize("phase", ["after_quarantine", "after_import", "success"])
def test_restore_quarantine_survives_restart(t14_bank, phase, tmp_path, monkeypatch, record_property):
    bank = t14_bank
    data = _seed_recovery(bank)
    old = authoritative(bank)
    if bank.kind == "lite":
        key = bytes(range(32))
        manifest = bank.store.create_backup(tmp_path / "backup", key, "t14-synthetic")
        artifact, executor = tmp_path / "backup" / manifest.backup_id, None
    else:
        from tests.test_source_v1_restore import _pg_artifact
        executor, artifact, key = _pg_artifact(bank.resources, bank.spec["dsn"], tmp_path)
    newer = _advance_after_backup(bank, data)
    record_property("before_restore_newer_state", json.dumps(newer, sort_keys=True))
    with ExitStack() as stack, monkeypatch.context() as patch:
        if bank.kind == "lite":
            spec, marker, restored = _restore_lite(bank, artifact, key, phase, tmp_path / "restored", stack, patch)
        else:
            spec, marker, restored = _restore_pg(bank, executor, artifact, key, phase, patch)
        if phase != "after_quarantine":
            if bank.kind == "lite":
                assert restored["memory"] == old["memory"] and restored["changelog"] == old["changelog"]
            else:
                assert restored == old
        terminal_end = time.monotonic_ns() + 10_000_000_000
        child, pipe = start_process(stack.callback, _restart_child, spec)
        receive(pipe, "ready", end=terminal_end)
        pipe.send("start")
        result = receive(pipe, "rejected", end=terminal_end)
        child.join(5)
        assert child.exitcode == 0 and result["modes"] == ["off", "v1"]
        if bank.kind == "lite":
            from pathlib import Path
            assert json.loads(Path(spec["path"] + ".source-admission.json").read_bytes()) == marker
        else:
            assert _pg_marker(spec) == marker
        record_property("quarantined_restart", json.dumps({"backend": bank.kind, "phase": phase,
                        "marker": marker, "fresh_process": result}, sort_keys=True))
