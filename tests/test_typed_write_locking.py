"""Native Fact writes and deferred entry share the Lite writer boundary."""

from collections.abc import Iterator
from contextlib import contextmanager
from inspect import signature
from threading import Event, Thread

import pytest

from memplex.models import Fact
from memplex.storage.lite.store import LiteMemoryStore

_WAIT_SECONDS = 5


@pytest.mark.parametrize(
    "name,parameters,annotations",
    [
        ("add_fact", ("self", "fact"), {"fact": "Fact", "return": "None"}),
        ("add_preference", ("self", "preference"), {"preference": "Preference", "return": "None"}),
        ("get_fact", ("self", "fact_id"), {"fact_id": "str", "return": "Fact | None"}),
        (
            "list_facts", ("self", "offset", "limit", "owner"),
            {"offset": "int", "limit": "int", "owner": "str | None", "return": "list[Fact]"},
        ),
    ],
)
def test_writer_decorator_preserves_public_reflection(name, parameters, annotations):
    method = getattr(LiteMemoryStore, name)
    assert tuple(signature(method).parameters) == parameters
    assert method.__annotations__ == annotations
    original = method.__wrapped__
    assert signature(method) == signature(original)
    assert method.__annotations__ == original.__annotations__
    assert method.__qualname__ == original.__qualname__
    assert method.__module__ == original.__module__
    assert method.__doc__ == original.__doc__


def test_bound_add_fact_keeps_signature_and_sync_durable_return(store):
    assert tuple(signature(store.add_fact).parameters) == ("fact",)
    assert store.add_fact.__annotations__ == {"fact": "Fact", "return": "None"}
    assert store.add_fact(Fact(id="metadata-fact", subject="alice", predicate="likes", object_="tea")) is None
    reopened = LiteMemoryStore(store._path)
    try:
        assert reopened.get_fact("metadata-fact").object_ == "tea"
    finally:
        writer = getattr(reopened._durability, "_single_writer", None)
        if writer is not None:
            writer.close(drain=False)


@pytest.fixture(params=["1", "0"], ids=["queue-on", "queue-off"])
def store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", request.param)
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
    memory = LiteMemoryStore(tmp_path / "memory.json")
    try:
        yield memory
    finally:
        writer = getattr(memory._durability, "_single_writer", None)
        if writer is not None:
            writer.close(drain=False)


def test_fact_mutation_waits_for_existing_writer_lock(store, monkeypatch):
    """Removing add_fact's writer boundary lets reload run before acquisition."""
    first_boundary = Event()
    reload_called = Event()
    lock_held = Event()
    write_requested = Event()
    finished = Event()
    errors = []
    original_lock = store._durability.writer_lock
    original_reload = store._reload_for_mutation

    @contextmanager
    def observe_lock() -> Iterator[None]:
        first_boundary.set()
        with original_lock():
            yield

    def observe_reload(*, force=False):
        reload_called.set()
        first_boundary.set()
        original_reload(force=force)

    monkeypatch.setattr(store._durability, "writer_lock", observe_lock)
    monkeypatch.setattr(store, "_reload_for_mutation", observe_reload)

    def write_fact():
        try:
            assert lock_held.wait(_WAIT_SECONDS), "Existing writer lock was not held"
            write_requested.set()
            store.add_fact(Fact(id="blocked-fact", subject="alice", predicate="likes", object_="tea"))
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=write_fact, daemon=True)
    try:
        with original_lock():
            worker.start()
            lock_held.set()
            assert write_requested.wait(_WAIT_SECONDS), "Fact write was not requested"
            # Rendezvous on either reload (the old, unguarded entry) or the
            # real lock attempt; the test does not merely sleep and hope.
            assert first_boundary.wait(_WAIT_SECONDS), "Fact write reached no boundary"
            assert not reload_called.wait(0.1), "Fact reload ran before the existing writer lock released"
            assert not finished.is_set(), "Fact write returned while another writer held the lock"
        worker.join(_WAIT_SECONDS)
        assert not worker.is_alive(), "Fact writer deadlocked after lock release"
        assert finished.is_set()
        assert reload_called.is_set()
        assert not errors, f"Fact write errors: {errors}"
        assert store.get_fact("blocked-fact").object_ == "tea"
    finally:
        worker.join(_WAIT_SECONDS)


def test_deferred_entry_waits_for_existing_writer_lock(store, monkeypatch):
    """Removing the entry lock advances deferred depth during another write."""
    first_boundary = Event()
    lock_held = Event()
    entry_requested = Event()
    entered = Event()
    allow_exit = Event()
    finished = Event()
    errors = []
    original_lock = store._durability.writer_lock

    @contextmanager
    def observe_lock() -> Iterator[None]:
        first_boundary.set()
        with original_lock():
            yield

    monkeypatch.setattr(store._durability, "writer_lock", observe_lock)

    def enter_deferred():
        try:
            assert lock_held.wait(_WAIT_SECONDS), "Existing writer lock was not held"
            entry_requested.set()
            with store.deferred_commit():
                entered.set()
                first_boundary.set()
                assert allow_exit.wait(_WAIT_SECONDS), "Deferred body was not released"
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=enter_deferred, daemon=True)
    try:
        with original_lock():
            worker.start()
            lock_held.set()
            assert entry_requested.wait(_WAIT_SECONDS), "Deferred entry was not requested"
            assert first_boundary.wait(_WAIT_SECONDS), "Deferred entry reached no boundary"
            assert not entered.is_set(), "Deferred body entered before the existing writer lock released"
            assert store._commit_defer_depth == 0
            assert not finished.is_set()
        assert entered.wait(_WAIT_SECONDS), "Deferred entry deadlocked after lock release"
        assert store._commit_defer_depth == 1
        allow_exit.set()
        worker.join(_WAIT_SECONDS)
        assert not worker.is_alive(), "Deferred exit deadlocked"
        assert finished.is_set()
        assert store._commit_defer_depth == 0
        assert not errors, f"Deferred context errors: {errors}"
    finally:
        allow_exit.set()
        worker.join(_WAIT_SECONDS)


def test_deferred_scope_releases_writer_lock_before_yield(store):
    """Holding the entry lock across yield deadlocks a separate Fact writer."""
    write_requested = Event()
    finished = Event()
    errors = []

    def write_fact():
        write_requested.set()
        try:
            store.add_fact(Fact(id="deferred-fact", subject="alice", predicate="likes", object_="tea"))
        except BaseException as exc:  # noqa: BLE001 - surfaced on the test thread
            errors.append(exc)
        finally:
            finished.set()

    worker = Thread(target=write_fact, daemon=True)
    before = store._committed_pair.generation
    try:
        with store.deferred_commit():
            worker.start()
            assert write_requested.wait(_WAIT_SECONDS)
            assert finished.wait(_WAIT_SECONDS), "Deferred yield retained the writer lock"
            assert store._commit_defer_depth == 1
            assert store._committed_pair.generation == before
            assert not errors, f"Deferred Fact write errors: {errors}"
        worker.join(_WAIT_SECONDS)
        assert not worker.is_alive()
        assert store._commit_defer_depth == 0
        assert store._committed_pair.generation == before + 1
        assert store.get_fact("deferred-fact").object_ == "tea"
    finally:
        worker.join(_WAIT_SECONDS)
