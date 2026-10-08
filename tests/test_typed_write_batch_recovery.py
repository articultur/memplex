"""Typed batches acknowledge only complete durable pairs, including crash recovery.

These tests would fail if the batch acknowledged a failed decision, retained
speculative residents/proof, or recovered only one member of the durable pair.
"""

from __future__ import annotations

import faulthandler
import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from memplex.models import Fact
from memplex.storage.lite import durability as durability_module
from memplex.storage.lite.durability import LitePair, LiteStorageIntegrityError
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.typed_batch import TypedBatchInput
from tests.helpers.typed_batch import literal_plan, make_fact, make_preference

# Cover fixture setup, ordinary blocking queue calls, and cleanup. The thread
# method emits every thread's stack and exits even if interrupted teardown hangs.
pytestmark = pytest.mark.timeout(
    60, method="thread", func_only=False,
)

_CRASH_EXIT = 77
_POST_DECISION_HOOKS = (
    "after_journal_rename_and_parent_dir_fsync",
    "after_memory_replace",
    "after_changelog_replace",
    "after_journal_unlink",
    "after_final_parent_dir_fsync",
)


def test_recovery_watchdog_bounds_setup_call_and_cleanup(tmp_path):
    marks = globals().get("pytestmark", [])
    if not isinstance(marks, list):
        marks = [marks]
    watchdog = next((mark for mark in marks if mark.name == "timeout"), None)
    assert watchdog is not None, "Recovery calls and fixture cleanup need a local hard watchdog"
    assert watchdog.args == (60,)
    assert watchdog.kwargs == {"method": "thread", "func_only": False}
    # Exercise the same locked plugin/method in a disposable child. Its shorter
    # probe budget deliberately strands a real queue submission, never this runner.
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    probe = tmp_path / "test_watchdog_probe.py"
    probe.write_text(
        "import pytest\nfrom threading import Event\n"
        "from memplex.storage.lite.single_writer import SingleWriterQueue\n"
        f"pytestmark = pytest.mark.timeout(0.5, **{watchdog.kwargs!r})\n"
        "def test_stuck_queue():\n    SingleWriterQueue().submit(lambda: Event().wait())\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(tmp_path / "pytest.ini"), str(probe), "-q"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    evidence = process.stdout + process.stderr
    (tmp_path / "watchdog-stacks.log").write_text(evidence, encoding="utf-8")
    assert process.returncode == 1, evidence
    assert "Timeout" in evidence and "test_stuck_queue" in evidence
    assert "lite-single-writer" in evidence and "single_writer.py" in evidence
    assert "submit" in evidence and "_run" in evidence


def _configure(mode):
    os.environ["MEMPLEX_LITE_SINGLE_WRITER"] = mode
    for key in ("MEMPLEX_LITE_TYPED_BATCH", "MEMPLEX_LITE_SQLITE_AUTHORITY", "MEMPLEX_LITE_SQLITE_SHADOW"):
        os.environ.pop(key, None)


def _close(store):
    writer = getattr(store._durability, "_single_writer", None)
    if writer is not None:
        writer.close(drain=False)


def _seed(path):
    store = LiteMemoryStore(path)
    store.add_fact(make_fact("old", "base"))
    store.add_preference(make_preference("old-p", "base"))
    return store


def _inputs():
    return TypedBatchInput(
        (make_fact("old", "target"), make_fact("new-a"), make_fact("new-b")),
        (make_preference("old-p", "target"), make_preference("new-p")),
    )


def _pair_data(pair):
    return {
        "memory": pair.memory, "changelog": pair.changelog,
        "generation": pair.generation, "transaction_id": pair.transaction_id,
    }


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _assert_disk_pair(path, expected):
    """Check raw bytes' logical pair plus the independently recalculated proof."""
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog = json.loads(path.with_name("changelog.json").read_text(encoding="utf-8"))
    assert memory["generation"] == changelog["generation"] == expected.generation
    assert memory["transaction_id"] == changelog["transaction_id"] == expected.transaction_id
    assert memory["payload"] == expected.memory
    assert changelog["payload"] == expected.changelog
    assert memory["peer_digest"] == _digest(changelog["payload"])
    assert changelog["peer_digest"] == _digest(memory["payload"])
    assert not path.with_name("memory.journal.json").exists()
    record = {
        "generation": expected.generation, "transaction_id": expected.transaction_id,
        "memory_digest": _digest(expected.memory), "changelog_digest": _digest(expected.changelog),
    }
    return {**record, "cross_digest": _digest(record)}


def _assert_reopened(path, expected):
    reopened = LiteMemoryStore(path)
    try:
        assert reopened._committed_pair == expected
        assert {node.id for node in reopened.list_facts()} == {node["id"] for node in expected.memory["facts"]}
        assert {node.id for node in reopened.list_preferences()} == {node["id"] for node in expected.memory["preferences"]}
        record = _assert_disk_pair(path, expected)
        # Reopening validates authority but deliberately leaves the optional
        # local-commit digest cache empty; compare the pair record itself.
        assert durability_module._pair_record(reopened._committed_pair) == record
        assert reopened._pair_fingerprint is not None
        return deepcopy(reopened._committed_pair)
    finally:
        _close(reopened)


def _assert_no_proof(store):
    assert store._committed_pair is None
    assert store._committed_record is None
    assert store._pair_fingerprint is None
    assert store._durability._last_commit_target_record is None


def _assert_resident_pair(store, pair):
    assert store._raw_memory() == pair.memory
    assert store._raw_changelog() == pair.changelog


@pytest.fixture(params=["1", "0"], ids=["queue-on", "queue-off"])
def store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", request.param)
    for key in ("MEMPLEX_LITE_TYPED_BATCH", "MEMPLEX_LITE_SQLITE_AUTHORITY", "MEMPLEX_LITE_SQLITE_SHADOW"):
        monkeypatch.delenv(key, raising=False)
    memory = _seed(tmp_path / "memory.json")
    try:
        yield memory
    finally:
        _close(memory)


@pytest.fixture
def decisions(store, monkeypatch):
    calls = []
    original = store._durability.commit_locked

    def capture(base, target, **kwargs):
        calls.append((deepcopy(base), deepcopy(target)))
        return original(base, target, **kwargs)

    monkeypatch.setattr(store._durability, "commit_locked", capture)
    return calls


def _failed_batch(store, monkeypatch, hook, error):
    acknowledgements = []

    def cut():
        raise error

    monkeypatch.setattr(store._durability, hook, cut)
    with pytest.raises(type(error), match=str(error)) as raised:
        acknowledgements.append(store.run_typed_write_batch(_inputs(), literal_plan))
    assert raised.value is error
    assert acknowledgements == [], "A failed decision must never return successful input indices"
    _assert_no_proof(store)


def test_failure_before_journal_publish_keeps_previous_pair(store, monkeypatch, decisions):
    before = deepcopy(store._committed_pair)
    _failed_batch(store, monkeypatch, "before_journal_durable_publish", OSError("pre-decision cut"))
    assert len(decisions) == 1
    _assert_resident_pair(store, before)
    _assert_reopened(store._path, before)


@pytest.mark.parametrize("hook", _POST_DECISION_HOOKS)
def test_post_decision_failure_recovers_complete_batch(store, monkeypatch, decisions, hook):
    before = deepcopy(store._committed_pair)
    _failed_batch(store, monkeypatch, hook, OSError("post-decision cut"))
    assert len(decisions) == 1
    target = decisions[0][1]
    assert target.generation == before.generation + 1
    assert len(target.changelog) == len(before.changelog) + 5
    assert [(row["func_id"], row["event_type"]) for row in target.changelog[-5:]] == [
        ("old", "updated"), ("new-a", "created"), ("new-b", "created"),
        ("old-p", "updated"), ("new-p", "created"),
    ]
    _assert_resident_pair(store, target)
    _assert_reopened(store._path, target)


def test_ambiguous_directory_fsync_poisons_instance(store, monkeypatch, decisions):
    before = deepcopy(store._committed_pair)
    acknowledgements = []

    def ambiguous(_path):
        assert store._durability._journal_path.exists(), "Fault must occur after journal rename"
        raise OSError("ambiguous journal fsync")

    with monkeypatch.context() as fault:
        fault.setattr(durability_module, "_fsync_dir", ambiguous)
        with pytest.raises(OSError, match="ambiguous journal fsync"):
            acknowledgements.append(store.run_typed_write_batch(_inputs(), literal_plan))
    assert acknowledgements == []
    assert len(decisions) == 1
    assert store._durability._poisoned
    _assert_no_proof(store)
    for operation in (
        lambda: store.get_fact("old"),
        lambda: store.read_context_nodes(["old"]),
        lambda: store.add_fact(make_fact("later")),
        lambda: store.add_preference(make_preference("later-p")),
        lambda: store.run_typed_write_batch(_inputs(), literal_plan),
    ):
        with pytest.raises(LiteStorageIntegrityError, match="poisoned"):
            operation()
    reopened = LiteMemoryStore(store._path)
    try:
        recovered = deepcopy(reopened._committed_pair)
    finally:
        _close(reopened)
    assert recovered in (before, decisions[0][1]), "Ambiguity permits a complete old or new pair only"
    _assert_reopened(store._path, recovered)


def test_failed_serialization_does_not_leak_staged_nodes(store, monkeypatch, decisions):
    before = deepcopy(store._committed_pair)
    original = Fact.to_dict
    calls = 0
    acknowledgements = []

    def fail_final_serialization(self):
        nonlocal calls
        if self.id == "new-b":
            calls += 1
            if calls == 2:
                assert "new-a" in store._facts and "new-p" in store._preferences
                raise OSError("final serialization failed")
        return original(self)

    with monkeypatch.context() as fault:
        fault.setattr(Fact, "to_dict", fail_final_serialization)
        with pytest.raises(OSError, match="final serialization failed"):
            acknowledgements.append(store.run_typed_write_batch(_inputs(), literal_plan))
    assert acknowledgements == []
    assert calls == 2
    assert decisions == [], "Serialization failure must precede the durable decision"
    _assert_no_proof(store)
    _assert_resident_pair(store, before)
    assert store.read_context_nodes(["new-a", "new-b", "new-p"]) == {}
    _assert_reopened(store._path, before)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
@pytest.mark.parametrize("hook", ["before_journal_durable_publish", "after_memory_replace"])
def test_recovery_failure_clears_commit_proof(store, monkeypatch, decisions, hook, error_type):
    before = deepcopy(store._committed_pair)
    original = error_type("original interrupted decision")
    acknowledgements = []

    def unavailable():
        raise LiteStorageIntegrityError("authoritative recovery unavailable")

    def cut():
        monkeypatch.setattr(store._durability, "_load_authoritative_locked", unavailable)
        raise original

    monkeypatch.setattr(store._durability, hook, cut)
    # Existing commit-helper precedence exposes a recovery integrity error for
    # an OSError; non-ordinary interruption must propagate by identity.
    expected_error = LiteStorageIntegrityError if error_type is OSError else error_type
    with pytest.raises(expected_error) as raised:
        acknowledgements.append(store.run_typed_write_batch(_inputs(), literal_plan))
    if error_type is OSError:
        assert str(raised.value) == "authoritative recovery unavailable"
        assert raised.value.__context__ is original
    else:
        assert raised.value is original
    assert acknowledgements == []
    assert len(decisions) == 1
    _assert_no_proof(store)
    for operation in (
        lambda: store.read_context_nodes(["new-a"]),
        lambda: store.add_fact(make_fact("later")),
        lambda: store.run_typed_write_batch(_inputs(), literal_plan),
    ):
        with pytest.raises(LiteStorageIntegrityError, match="authoritative recovery unavailable"):
            operation()
        _assert_no_proof(store)
    _assert_reopened(store._path, before if hook == "before_journal_durable_publish" else decisions[0][1])


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("hook", ["before_journal_durable_publish", "after_memory_replace"])
def test_base_exception_propagates(store, monkeypatch, decisions, error_type, hook):
    before = deepcopy(store._committed_pair)
    _failed_batch(store, monkeypatch, hook, error_type("non-ordinary interruption"))
    assert len(decisions) == 1
    expected = before if hook == "before_journal_durable_publish" else decisions[0][1]
    _assert_resident_pair(store, expected)
    _assert_reopened(store._path, expected)


@pytest.mark.parametrize("hook", ["before_journal_durable_publish", "after_memory_replace"])
def test_clean_write_after_recovered_failure(store, monkeypatch, decisions, hook):
    before = deepcopy(store._committed_pair)
    with monkeypatch.context() as fault:
        _failed_batch(store, fault, hook, OSError("recoverable cut"))
    recovered = before if hook == "before_journal_durable_publish" else decisions[0][1]
    # Write directly after recovery, without a public read repairing cached proof.
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("later"),), (make_preference("later-p"),)), literal_plan,
    )
    assert result.successful_input_indices == (0, 1)
    assert result.committed_generation == recovered.generation + 1
    assert len(decisions) == 2, "No automatic replay of the failed batch"
    target = decisions[-1][1]
    assert result.transaction_id == target.transaction_id
    assert store._committed_record == _assert_disk_pair(store._path, target)
    _assert_reopened(store._path, target)


def _crash_worker(root, mode, stage):
    """Spawn entrypoint; never inherits a writer thread or held parent lock."""
    _configure(mode)
    path = Path(root) / "memory.json"
    with (Path(root) / "crash-stacks.log").open("w", encoding="utf-8") as stacks:
        faulthandler.enable(file=stacks)
        faulthandler.dump_traceback_later(25, file=stacks)
        store = LiteMemoryStore(path)
        original_commit = store._durability.commit_locked

        def record_target(base, target, **kwargs):
            (Path(root) / "target.json").write_text(json.dumps(_pair_data(target)), encoding="utf-8")
            return original_commit(base, target, **kwargs)

        def crash():
            (Path(root) / "reached.txt").write_text(stage, encoding="utf-8")
            os._exit(_CRASH_EXIT)

        store._durability.commit_locked = record_target
        if stage == "journal-rename-before-fsync":
            original_replace = durability_module.os.replace

            def cut_replace(src, dst):
                original_replace(src, dst)
                if Path(dst) == store._durability._journal_path:
                    crash()

            durability_module.os.replace = cut_replace
        elif stage == "journal-fsync-unconfirmed":
            original_fsync = durability_module._fsync_dir

            def cut_fsync(directory):
                original_fsync(directory)
                crash()

            durability_module._fsync_dir = cut_fsync
        else:
            setattr(store._durability, stage, crash)
        result = store.run_typed_write_batch(_inputs(), literal_plan)
        (Path(root) / "unexpected-ack.json").write_text(str(result), encoding="utf-8")
        raise AssertionError("Crash boundary was not reached")


@pytest.mark.parametrize("mode", ["1", "0"], ids=["queue-on", "queue-off"])
@pytest.mark.parametrize(
    "stage,expected_state",
    [
        ("before_journal_durable_publish", "old"),
        ("after_journal_rename_and_parent_dir_fsync", "new"),
        ("after_memory_replace", "new"),
        ("after_changelog_replace", "new"),
        ("after_journal_unlink", "new"),
        ("journal-rename-before-fsync", "either"),
        ("journal-fsync-unconfirmed", "either"),
    ],
)
def test_hard_exit_recovers_one_complete_pair(tmp_path, monkeypatch, mode, stage, expected_state):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", mode)
    for key in ("MEMPLEX_LITE_TYPED_BATCH", "MEMPLEX_LITE_SQLITE_AUTHORITY", "MEMPLEX_LITE_SQLITE_SHADOW"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "memory.json"
    store = _seed(path)
    before = deepcopy(store._committed_pair)
    _close(store)
    process = multiprocessing.get_context("spawn").Process(target=_crash_worker, args=(str(tmp_path), mode, stage))
    process.start()
    try:
        process.join(30)
        assert not process.is_alive(), (tmp_path / "crash-stacks.log").read_text(encoding="utf-8")
        assert process.exitcode == _CRASH_EXIT
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        if process.is_alive():
            process.kill()
            process.join(2)
        process.close()
    assert (tmp_path / "reached.txt").read_text(encoding="utf-8") == stage
    assert not (tmp_path / "unexpected-ack.json").exists()
    target = LitePair(**json.loads((tmp_path / "target.json").read_text(encoding="utf-8")))
    assert target.generation == before.generation + 1
    assert len(target.changelog) == len(before.changelog) + 5
    reopened = LiteMemoryStore(path)
    recovered = deepcopy(reopened._committed_pair)
    _close(reopened)
    allowed = {"old": (before,), "new": (target,), "either": (before, target)}[expected_state]
    assert recovered in allowed, "Crash recovery must not mix nodes, events, generations or digests"
    _assert_reopened(path, recovered)
