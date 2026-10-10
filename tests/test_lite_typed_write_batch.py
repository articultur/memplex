"""Synchronous typed batches isolate invalid items and acknowledge real disk commits."""

from copy import deepcopy
from threading import Event, Thread

import pytest

from memplex.storage.lite.durability import LiteStorageIntegrityError
from memplex.storage.lite.single_writer import SingleWriterQueue
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.typed_batch import (
    TypedBatchInput,
    TypedWriteOperation,
    TypedWritePlan,
)
from memplex.sync_repository import SyncCapturePolicy
from tests.helpers.typed_batch import literal_plan, make_fact, make_preference

_WAIT_SECONDS = 5


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", "1")
    monkeypatch.delenv("MEMPLEX_LITE_TYPED_BATCH", raising=False)
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_AUTHORITY", raising=False)
    monkeypatch.delenv("MEMPLEX_LITE_SQLITE_SHADOW", raising=False)
    opened = []

    def open_store(**kwargs):
        store = LiteMemoryStore(tmp_path / "memory.json", **kwargs)
        opened.append(store)
        return store

    yield open_store
    for store in opened:
        writer = getattr(store._durability, "_single_writer", None)
        if writer is not None:
            writer.close(drain=False)


@pytest.fixture
def store(stores):
    return stores()


@pytest.fixture
def durable_calls(store, monkeypatch):
    """Observe actual commits, including all validation, journal and fsync I/O."""
    calls = []
    original = store._durability.commit_locked

    def record(base, target, **kwargs):
        calls.append((base, target))
        return original(base, target, **kwargs)

    monkeypatch.setattr(store._durability, "commit_locked", record)
    return calls


def test_multiple_typed_items_use_one_durable_decision(store, stores, durable_calls):
    before = store.generation
    inputs = TypedBatchInput((make_fact("a"), make_fact("b")), (make_preference("p"),))

    result = store.run_typed_write_batch(inputs, literal_plan)

    assert result.supported
    assert result.successful_input_indices == (0, 1, 2)
    assert result.rejected == ()
    assert len(durable_calls) == 1
    assert result.committed_generation == before + 1
    assert result.committed_generation == store._committed_pair.generation
    assert result.transaction_id == store._committed_pair.transaction_id
    assert result.transaction_id == durable_calls[0][1].transaction_id
    reopened = stores()
    assert [fact.id for fact in reopened.list_facts()] == ["a", "b"]
    assert reopened.get_preference("p").preference == "dark"
    assert reopened.generation == before + 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_", 123),
        ("confidence", float("nan")),
        ("valid_from", float("inf")),
        ("provenance", {1: "coercible-key"}),
        ("source_paragraphs", ("not-a-list",)),
        ("knowledge_tier", "unknown"),
        ("id", []),
    ],
)
def test_invalid_middle_item_isolated(store, stores, durable_calls, field, value):
    bad = make_fact("bad")
    setattr(bad, field, value)
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"), bad, make_fact("c")), ()), literal_plan,
    )
    assert result.successful_input_indices == (0, 2)
    assert len(result.rejected) == 1
    assert result.rejected[0].operation_index == 1
    assert result.rejected[0].input_index == 1
    assert result.rejected[0].code == "invalid_typed_write"
    reopened = stores()
    assert reopened.get_fact("bad") is None
    assert [fact.id for fact in reopened.list_facts()] == ["a", "c"]
    assert len(durable_calls) == 1


@pytest.mark.parametrize("all_invalid", [False, True], ids=["empty", "all-invalid"])
def test_empty_or_all_invalid_has_no_commit(store, durable_calls, all_invalid):
    bad = make_fact("bad")
    bad.object_ = object()
    original_facts, original_preferences = store._facts, store._preferences
    original_events = store._changelog._events
    result = store.run_typed_write_batch(
        TypedBatchInput((bad,) if all_invalid else (), ()), literal_plan,
    )
    assert result.supported
    assert result.committed_generation is None
    assert result.transaction_id is None
    assert result.successful_input_indices == ()
    assert len(result.rejected) == int(all_invalid)
    assert durable_calls == []
    assert store._facts is original_facts
    assert store._preferences is original_preferences
    assert store._changelog._events is original_events


def test_repeated_id_keeps_order_and_events(store, stores, durable_calls):
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("same", "first"), make_fact("same", "last")), ()), literal_plan,
    )
    reopened = stores()
    assert reopened.get_fact("same").object_ == "last"
    timeline = reopened.get_timeline("same")
    assert len(timeline) == 2
    assert [event.event_type for event in timeline] == ["updated", "created"]
    assert result.successful_input_indices == (0, 1)
    assert len(durable_calls) == 1


def test_input_detached_before_queue_wait(store, stores, durable_calls, monkeypatch):
    writer = SingleWriterQueue()
    store._durability._single_writer = writer
    queued = Event()
    proceed = Event()
    original_submit = writer.submit
    results, errors, submissions = [], [], []
    fact, preference = make_fact("a", "before"), make_preference("p", "before")
    fact.provenance = {"origin": "before"}
    inputs = TypedBatchInput((fact,), (preference,))

    def delayed_submit(fn):
        submissions.append(fn)
        queued.set()
        assert proceed.wait(_WAIT_SECONDS)
        return original_submit(fn)

    monkeypatch.setattr(writer, "submit", delayed_submit)

    def run():
        try:
            results.append(store.run_typed_write_batch(inputs, literal_plan))
        except BaseException as exc:  # noqa: BLE001 - surfaced on test thread
            errors.append(exc)

    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert queued.wait(_WAIT_SECONDS)
        fact.object_ = "after"
        fact.provenance["origin"] = "after"
        preference.preference = "after"
    finally:
        proceed.set()
        thread.join(_WAIT_SECONDS)
    assert not thread.is_alive()
    assert not errors
    assert len(submissions) == 1
    assert results[0].successful_input_indices == (0, 1)
    assert len(durable_calls) == 1
    reopened = stores()
    assert reopened.get_fact("a").object_ == "before"
    assert reopened.get_fact("a").provenance == {"origin": "before"}
    assert reopened.get_preference("p").preference == "before"


def test_standalone_writes_still_commit_individually(store, durable_calls):
    store.add_fact(make_fact("a"))
    store.add_fact(make_fact("b"))
    store.add_preference(make_preference("p"))
    assert len(durable_calls) == 3
    assert store.generation == 3


def test_snapshot_limits_existing_facts_to_1000(store, durable_calls):
    # Seed through the pre-existing bulk fixture path, with non-sorted IDs.
    with store.deferred_commit():
        for index in range(1002):
            store.add_fact(make_fact(f"fact-{1001 - index}"))
    expected_ids = [fact.id for fact in store.list_facts()]
    durable_calls.clear()
    observed = []

    def planner(snapshot, inputs):
        observed.append(snapshot)
        return literal_plan(snapshot, inputs)

    store.run_typed_write_batch(TypedBatchInput((), ()), planner)
    assert len(observed[0].facts) == 1000
    assert [fact.id for fact in observed[0].facts] == expected_ids
    assert observed[0].facts[0].id == "fact-1001"
    assert observed[0].facts[-1].id == "fact-2"
    assert observed[0].generation == store.generation
    assert durable_calls == []


@pytest.mark.parametrize("flag", ["0", "false", "False"])
def test_disabled_batch_does_not_plan_or_refresh(store, stores, durable_calls, monkeypatch, flag):
    peer = stores()
    peer.add_fact(make_fact("peer"))
    before = store._committed_pair
    monkeypatch.setenv("MEMPLEX_LITE_TYPED_BATCH", flag)

    def forbidden(*args):
        pytest.fail("Unsupported batch must not invoke planner or refresh business state")

    result = store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), forbidden)
    assert not result.supported
    assert result.committed_generation is None
    assert result.transaction_id is None
    assert result.successful_input_indices == result.committed_superseded_ids == ()
    assert result.rejected == result.fact_valid_from == ()
    assert store._facts == {}
    assert store._committed_pair is before
    assert durable_calls == []


@pytest.mark.parametrize("mode", ["read", "rw"])
def test_non_json_authority_is_unsupported(store, durable_calls, monkeypatch, mode):
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", mode)
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
    )
    assert not result.supported
    assert durable_calls == []


def test_required_sync_is_unsupported(stores):
    store = stores(sync_capture_policy=SyncCapturePolicy("required", "local"))
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
    )
    assert not result.supported
    assert store._facts == {}


def test_subclass_is_unsupported(tmp_path):
    class CustomLite(LiteMemoryStore):
        pass

    store = CustomLite(tmp_path / "memory.json")
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
    )
    assert not result.supported
    assert store._facts == {}


@pytest.mark.parametrize(
    "method", ["add_fact", "add_preference", "list_facts", "_commit_current_state", "_prepare_typed_write"],
)
def test_instance_method_override_is_unsupported(store, durable_calls, monkeypatch, method):
    monkeypatch.setattr(store, method, lambda *args: pytest.fail("override called"), raising=False)
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
    )
    assert not result.supported
    assert durable_calls == []


def test_eligibility_is_rechecked_after_queue_admission(store, durable_calls, monkeypatch):
    writer = SingleWriterQueue()
    store._durability._single_writer = writer
    original_submit = writer.submit

    def disable_then_submit(fn):
        monkeypatch.setenv("MEMPLEX_LITE_TYPED_BATCH", "0")
        return original_submit(fn)

    monkeypatch.setattr(writer, "submit", disable_then_submit)
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
    )
    assert not result.supported
    assert durable_calls == []


def test_existing_deferred_scope_is_unsupported(store, durable_calls):
    with store.deferred_commit():
        store.add_fact(make_fact("pending"))
        before_facts = store._facts
        result = store.run_typed_write_batch(
            TypedBatchInput((make_fact("a"),), ()), lambda *args: pytest.fail("planner called"),
        )
        assert not result.supported
        assert store._facts is before_facts
        assert durable_calls == []
    assert store.get_fact("pending") is not None
    assert store.get_fact("a") is None


def test_planner_receives_detached_nodes(store, stores, durable_calls):
    store.add_fact(make_fact("old", "original"))
    incoming = make_fact("new", "original")
    durable_calls.clear()

    def planner(snapshot, inputs):
        snapshot.facts[0].object_ = "speculative"
        inputs.facts[0].object_ = "planned"
        return literal_plan(snapshot, inputs)

    store.run_typed_write_batch(TypedBatchInput((incoming,), ()), planner)
    assert incoming.object_ == "original"
    assert incoming.created_at is None
    reopened = stores()
    assert reopened.get_fact("old").object_ == "original"
    assert reopened.get_fact("new").object_ == "planned"
    assert reopened.get_fact("new").created_at
    assert reopened.get_fact("new").updated_at
    assert len(durable_calls) == 1


def test_superseded_ids_and_timestamp_supplements_only_acknowledge_commits(store, stores, durable_calls):
    store.add_fact(make_fact("old"))
    durable_calls.clear()
    stamp = "2026-10-08T09:00:00+00:00"

    def planner(snapshot, inputs):
        old = snapshot.facts[0]
        old.invalid_at = stamp
        inputs.facts[0].valid_from = stamp
        bad = make_fact("bad")
        bad.object_ = 7
        return TypedWritePlan(
            (
                TypedWriteOperation(old, None, "supersede"),
                TypedWriteOperation(inputs.facts[0], 0, "input"),
                TypedWriteOperation(bad, 1, "input"),
            ),
            ((0, stamp), (1, stamp)),
        )

    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("new"), make_fact("bad")), ()), planner,
    )
    assert result.committed_superseded_ids == ("old",)
    assert result.successful_input_indices == (0,)
    assert result.fact_valid_from == ((0, stamp),)
    assert stores().get_fact("old").invalid_at == stamp
    assert len(durable_calls) == 1


@pytest.mark.parametrize("error", [ValueError("planner failed"), KeyboardInterrupt("planner interrupted")])
def test_planner_errors_propagate_without_commit(store, durable_calls, error):
    def planner(snapshot, inputs):
        raise error

    with pytest.raises(type(error), match=str(error)):
        store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), planner)
    assert durable_calls == []
    assert store.get_fact("a") is None


@pytest.mark.parametrize("plan", [None, TypedWritePlan((object(),), ()), TypedWritePlan((TypedWriteOperation(make_fact("a"), None, "input"),), ())])
def test_malformed_plan_is_not_an_item_rejection(store, durable_calls, plan):
    with pytest.raises((TypeError, ValueError)):
        store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), lambda *args: plan)
    assert durable_calls == []
    assert store.get_fact("a") is None


@pytest.mark.parametrize("hook,committed", [("before_journal_durable_publish", False), ("after_memory_replace", True)])
@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_final_failure_recovers_authority_and_propagates(store, stores, durable_calls, monkeypatch, hook, committed, error_type):
    peer = stores()
    peer.add_fact(make_fact("peer"))

    def fail():
        raise error_type("batch failure")

    monkeypatch.setattr(store._durability, hook, fail)
    with pytest.raises(error_type, match="batch failure"):
        store.run_typed_write_batch(TypedBatchInput((make_fact("new"),), ()), literal_plan)
    # Resident state, before any public read can repair it, follows disk authority.
    assert "peer" in store._facts
    assert ("new" in store._facts) is committed
    assert store._committed_pair is None
    assert store._pair_fingerprint is None
    assert store._committed_record is None
    reopened = stores()
    assert reopened.get_fact("peer") is not None
    assert (reopened.get_fact("new") is not None) is committed
    assert len(durable_calls) == 1


def test_recovery_failure_invalidates_proof_and_preserves_original_error(store, durable_calls, monkeypatch):
    def unavailable():
        raise LiteStorageIntegrityError("recovery unavailable")

    def fail():
        monkeypatch.setattr(store._durability, "_load_authoritative_locked", unavailable)
        raise KeyboardInterrupt("original interruption")

    monkeypatch.setattr(store._durability, "before_journal_durable_publish", fail)
    with pytest.raises(KeyboardInterrupt, match="original interruption"):
        store.run_typed_write_batch(TypedBatchInput((make_fact("new"),), ()), literal_plan)
    assert store._committed_pair is store._pair_fingerprint is store._committed_record is None
    with pytest.raises(LiteStorageIntegrityError):
        store.get_fact("new")
    assert len(durable_calls) == 1


def test_existing_sync_state_is_preserved_without_capture(store, stores, durable_calls):
    store.sync_register_target("remote")
    before = deepcopy(store._sync_state)
    durable_calls.clear()
    store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), literal_plan)
    assert store._sync_state == before
    assert stores()._sync_state == before
    assert len(durable_calls) == 1


def test_batch_also_commits_once_without_queue(store, stores, durable_calls, monkeypatch):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", "0")
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"), make_fact("b")), (make_preference("p"),)), literal_plan,
    )
    assert result.successful_input_indices == (0, 1, 2)
    assert len(durable_calls) == 1
    assert stores().get_preference("p") is not None
    assert getattr(store._durability, "_single_writer", None) is None


def test_json_authority_with_sqlite_shadow_remains_eligible(store, stores, durable_calls, monkeypatch):
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_SHADOW", "1")
    result = store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), literal_plan)
    assert result.supported
    assert len(durable_calls) == 1
    assert stores().get_fact("a") is not None


def test_invalid_preference_isolated(store, stores, durable_calls):
    invalid = make_preference("bad")
    invalid.subject_id = 123
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"),), (invalid, make_preference("p"))), literal_plan,
    )
    assert result.successful_input_indices == (0, 2)
    assert result.rejected[0].memory_id == "bad"
    assert result.rejected[0].input_index == 1
    assert stores().get_preference("bad") is None
    assert len(durable_calls) == 1


def test_operation_copy_failure_isolated(store, stores, durable_calls):
    from memplex.models import Fact

    class UncopyableFact(Fact):
        def __deepcopy__(self, memo):
            raise ValueError("uncopyable operation")

    def planner(snapshot, inputs):
        return TypedWritePlan((
            TypedWriteOperation(inputs.facts[0], 0, "input"),
            TypedWriteOperation(UncopyableFact(id="bad"), 1, "input"),
            TypedWriteOperation(inputs.facts[2], 2, "input"),
        ), ())

    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"), make_fact("bad"), make_fact("c")), ()), planner,
    )
    assert result.successful_input_indices == (0, 2)
    assert result.rejected[0].code == "invalid_typed_write"
    assert stores().get_fact("bad") is None
    assert len(durable_calls) == 1


def test_event_preparation_failure_isolated(store, stores, durable_calls, monkeypatch):
    from memplex.storage.changelog import ChangelogStore

    original = ChangelogStore._deserialize_event

    def reject_bad_event(data):
        if data["func_id"] == "bad":
            raise ValueError("invalid generated event")
        return original(data)

    monkeypatch.setattr(ChangelogStore, "_deserialize_event", staticmethod(reject_bad_event))
    result = store.run_typed_write_batch(
        TypedBatchInput((make_fact("a"), make_fact("bad"), make_fact("c")), ()), literal_plan,
    )
    assert result.successful_input_indices == (0, 2)
    assert result.rejected[0].memory_id == "bad"
    reopened = stores()
    assert reopened.get_fact("bad") is None
    assert reopened.get_timeline("bad") == []
    assert len(durable_calls) == 1


def test_staging_copies_whole_containers_once(store, stores, durable_calls):
    store.add_fact(make_fact("old"))
    store.add_preference(make_preference("old-p"))
    durable_calls.clear()
    copies = {"facts": 0, "preferences": 0, "changelog": 0}

    class CountedDict(dict):
        def __init__(self, values, label):
            super().__init__(values)
            self.label = label

        def copy(self):
            copies[self.label] += 1
            return super().copy()

    class CountedEvents(list):
        def copy(self):
            copies["changelog"] += 1
            return super().copy()

    store._facts = CountedDict(store._facts, "facts")
    store._preferences = CountedDict(store._preferences, "preferences")
    store._changelog._events = CountedEvents(store._changelog._events)
    original_fact = store._facts["old"]
    original_preference = store._preferences["old-p"]
    original_event = store._changelog._events[0]
    result = store.run_typed_write_batch(
        TypedBatchInput(tuple(make_fact(f"new-{index}") for index in range(12)), (make_preference("p"),)),
        literal_plan,
    )
    assert len(result.successful_input_indices) == 13
    assert copies == {"facts": 1, "preferences": 1, "changelog": 1}
    assert store._facts["old"] is original_fact
    assert store._preferences["old-p"] is original_preference
    assert store._changelog._events[0] is original_event
    assert len(durable_calls) == 1
    assert len(stores().list_facts()) == 13


def test_poisoned_failure_clears_proof_without_recovering(store, durable_calls, monkeypatch):
    def forbidden_recovery():
        pytest.fail("Poisoned instance must not recover inside the batch")

    def interrupt_and_poison():
        store._durability._poisoned = True
        monkeypatch.setattr(store._durability, "_load_authoritative_locked", forbidden_recovery)
        raise KeyboardInterrupt("ambiguous decision")

    monkeypatch.setattr(store._durability, "before_journal_durable_publish", interrupt_and_poison)
    with pytest.raises(KeyboardInterrupt, match="ambiguous decision"):
        store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), literal_plan)
    assert store._committed_pair is store._pair_fingerprint is store._committed_record is None
    with pytest.raises(LiteStorageIntegrityError, match="poisoned"):
        store.get_fact("a")
    assert len(durable_calls) == 1


@pytest.mark.parametrize(
    "plan",
    [
        TypedWritePlan((TypedWriteOperation(make_preference("p"), None, "supersede"),), ()),
        TypedWritePlan((TypedWriteOperation(make_fact("a"), True, "input"),), ()),
        TypedWritePlan((TypedWriteOperation(make_fact("a"), 2, "input"),), ()),
        TypedWritePlan((TypedWriteOperation(make_fact("a"), 0, "other"),), ()),
        TypedWritePlan((), ((1, "stamp"),)),
    ],
)
def test_malformed_operation_metadata_propagates(store, durable_calls, plan):
    with pytest.raises((TypeError, ValueError)):
        store.run_typed_write_batch(TypedBatchInput((make_fact("a"),), ()), lambda *args: plan)
    assert durable_calls == []


@pytest.mark.parametrize("queue_mode", ["1", "0"], ids=["queue-on", "queue-off"])
def test_failed_caller_cannot_erase_later_batch_commit_proof(
    store, stores, durable_calls, monkeypatch, queue_mode,
):
    """A's unlocked exception propagation must not invalidate B's publication."""
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", queue_mode)
    a_unlocked = Event()
    b_published = Event()
    a_returned = Event()
    a_errors, b_errors, b_results = [], [], []
    a_failure = ValueError("batch A planner failed")
    original_inner = LiteMemoryStore._run_typed_write_batch_locked
    original_publish = LiteMemoryStore._publish_committed_locally

    def failing_planner(snapshot, inputs):
        raise a_failure

    def delay_a_propagation(self, inputs, planner):
        try:
            return original_inner(self, inputs, planner)
        except ValueError as exc:
            if self is store and exc is a_failure:
                # The real decorator has completed all locked recovery and
                # released the lock. With the queue enabled, submit has also
                # returned A's exception to this caller thread.
                a_unlocked.set()
                assert b_published.wait(_WAIT_SECONDS), "B never reached real publication"
            raise

    def pause_b_after_publication(self, committed):
        original_publish(self, committed)
        if self is store:
            # B is durable and has bound its actual proof, but has not yet
            # read that proof to build TypedBatchResult. It still owns the lock.
            b_published.set()
            assert a_returned.wait(_WAIT_SECONDS), "A did not finish exception propagation"

    monkeypatch.setattr(LiteMemoryStore, "_run_typed_write_batch_locked", delay_a_propagation)
    monkeypatch.setattr(LiteMemoryStore, "_publish_committed_locally", pause_b_after_publication)

    def run_a():
        try:
            store.run_typed_write_batch(TypedBatchInput((make_fact("batch-a"),), ()), failing_planner)
        except BaseException as exc:  # noqa: BLE001 - surfaced on test thread
            a_errors.append(exc)
        finally:
            a_returned.set()

    def run_b():
        try:
            b_results.append(store.run_typed_write_batch(
                TypedBatchInput((make_fact("batch-b"),), ()), literal_plan,
            ))
        except BaseException as exc:  # noqa: BLE001 - surfaced on test thread
            b_errors.append(exc)

    thread_a = Thread(target=run_a, daemon=True)
    thread_b = Thread(target=run_b, daemon=True)
    thread_a.start()
    try:
        assert a_unlocked.wait(_WAIT_SECONDS), "A did not release the decorated writer boundary"
        thread_b.start()
        thread_a.join(_WAIT_SECONDS)
        thread_b.join(_WAIT_SECONDS)
    finally:
        # Bound teardown even if an assertion or a future regression breaks
        # the intended schedule; never strand either worker on a test barrier.
        b_published.set()
        a_returned.set()
        thread_a.join(_WAIT_SECONDS)
        if thread_b.ident is not None:
            thread_b.join(_WAIT_SECONDS)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert a_errors == [a_failure]
    assert len(durable_calls) == 1
    reopened = stores()
    assert reopened.get_fact("batch-a") is None
    assert reopened.get_fact("batch-b") is not None
    assert reopened.generation == 1
    assert not b_errors, f"B committed on disk but failed to acknowledge: {b_errors}"
    assert len(b_results) == 1
    result = b_results[0]
    assert result.successful_input_indices == (0,)
    assert result.committed_generation == 1
    assert result.transaction_id == durable_calls[0][1].transaction_id
    assert result.transaction_id == store._committed_pair.transaction_id
    assert store._committed_record is not None
    assert store._pair_fingerprint is not None
    assert store._durability._last_commit_target_record is not None


@pytest.mark.parametrize("queue_mode", ["1", "0"], ids=["queue-on", "queue-off"])
def test_uncopyable_caller_input_aborts_admission_without_side_effects(
    store, stores, durable_calls, monkeypatch, queue_mode,
):
    """Admission cannot enqueue uncopyable input or replay its valid peers."""
    from memplex.models import Fact

    class UncopyableFact(Fact):
        def __deepcopy__(self, memo):
            raise ValueError("caller input cannot be detached")

    store.add_fact(make_fact("existing"))
    store.add_preference(make_preference("existing-p"))
    durable_calls.clear()
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", queue_mode)
    writer = store._durability._single_writer
    original_submit = writer.submit
    submissions, planner_calls = [], []
    state_before = (store._facts, store._preferences, store._changelog._events, store._sync_state)
    values_before = deepcopy(state_before)
    proof_before = (
        store._committed_pair, store._committed_record, store._pair_fingerprint,
        store._durability._last_commit_target_record,
    )
    generation_before = store._generation
    valid_before, valid_after = make_fact("valid-before"), make_fact("valid-after")
    preference = make_preference("valid-preference")
    inputs = TypedBatchInput(
        (valid_before, UncopyableFact(id="uncopyable"), valid_after), (preference,),
    )

    def observe_submit(fn):
        submissions.append(fn)
        return original_submit(fn)

    def observe_planner(snapshot, admitted):
        planner_calls.append(admitted)
        return literal_plan(snapshot, admitted)

    monkeypatch.setattr(writer, "submit", observe_submit)
    with pytest.raises(ValueError, match="caller input cannot be detached"):
        store.run_typed_write_batch(inputs, observe_planner)
    assert submissions == []
    assert planner_calls == []
    assert durable_calls == []
    state_after = (store._facts, store._preferences, store._changelog._events, store._sync_state)
    proof_after = (
        store._committed_pair, store._committed_record, store._pair_fingerprint,
        store._durability._last_commit_target_record,
    )
    assert all(before is after for before, after in zip(state_before, state_after, strict=True))
    assert state_after == values_before
    assert all(before is after for before, after in zip(proof_before, proof_after, strict=True))
    assert store._generation == generation_before
    assert valid_before.created_at is valid_after.created_at is preference.created_at is None
    reopened = stores()
    assert [fact.id for fact in reopened.list_facts()] == ["existing"]
    assert [node.id for node in reopened.list_preferences()] == ["existing-p"]
    assert reopened.generation == generation_before
