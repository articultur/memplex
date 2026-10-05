"""Committed-only source reads for the final context assembly boundary."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Barrier, Event

import pytest

from memplex.auth import AuthorizationContext, Principal
from memplex.models import (
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    Observation,
    Paragraph,
    Preference,
    SourceDocument,
    domain_node_id,
)
from memplex.models.paragraph import persisted_paragraph_id
from memplex.storage.base import MemoryStore
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.postgres import PostgresMemoryStore


@pytest.fixture(params=["", "rw"], ids=["lite-default", "lite-rw"])
def store(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", request.param)
    return LiteMemoryStore(tmp_path / "memory.json")


def _function(node_id, text="committed content"):
    return Function(
        id=node_id, name=node_id, name_normalized=node_id,
        trigger=[FieldValue(desc=text, sources=["source"])],
    )


def _source():
    return SourceDocument(type="test")


def _context(subject="alice", tenant="tenant-a"):
    return AuthorizationContext(
        Principal(tenant_id=tenant, subject_id=subject),
        workspace_id="workspace", agent_id="agent", session_id=f"session-{subject}",
    )


def test_context_read_base_extension_fails_closed():
    # Optional capability must not reinterpret the ordinary getter as committed.
    assert MemoryStore.read_context_nodes(object(), ["unknown"]) == {}


def test_context_read_excludes_pending_batch(store, monkeypatch):
    committed_id, pending_id = "committed", "pending"
    store.add(_function(committed_id), _source())
    original_refresh = store._refresh_for_read
    refresh_called = Event()

    def observe_refresh():
        refresh_called.set()
        original_refresh()

    monkeypatch.setattr(store, "_refresh_for_read", observe_refresh)
    with store.deferred_commit():
        store.add(_function(pending_id), _source())
        with store.deferred_commit():
            assert store.read_context_nodes([pending_id, committed_id]) == {}
        assert store.read_context_nodes([pending_id]) == {}
        assert not refresh_called.is_set()
        assert pending_id in store._functions
    assert store.read_context_nodes([pending_id])[pending_id].id == pending_id
    assert refresh_called.is_set()
    assert store.read_context_nodes([committed_id])[committed_id].id == committed_id


def test_context_read_keeps_successful_prefix_on_batch_exception(store):
    with pytest.raises(ValueError, match="failed extraction"), store.deferred_commit():
        store.add(_function("prefix"), _source())
        assert store.read_context_nodes(["prefix"]) == {}
        raise ValueError("failed extraction")
    assert store.read_context_nodes(["prefix"])["prefix"].id == "prefix"


def test_context_read_refreshes_peer_commit(store, monkeypatch):
    peer = LiteMemoryStore(store._path)
    commit_finished = Event()
    refresh_observed = Event()
    original_refresh = store._refresh_for_read

    def observe_refresh():
        assert commit_finished.is_set()
        original_refresh()
        refresh_observed.set()

    monkeypatch.setattr(store, "_refresh_for_read", observe_refresh)

    def commit_peer():
        peer.add(_function("peer", "current peer content"), _source())
        commit_finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        write = pool.submit(commit_peer)
        assert commit_finished.wait(timeout=5)
        node = store.read_context_nodes(["peer"])["peer"]
        write.result(timeout=5)
    assert node.trigger[0].desc == "current peer content"
    assert refresh_observed.is_set()


def test_context_read_observes_peer_update_and_delete(store):
    store.add_fact(Fact(id="fact", subject="version", predicate="is", object_="old"))
    peer = LiteMemoryStore(store._path)
    peer.add_fact(Fact(id="fact", subject="version", predicate="is", object_="new"))
    assert store.read_context_nodes(["fact"])["fact"].object_ == "new"
    peer.delete_fact("fact")
    assert store.read_context_nodes(["fact"]) == {}


def test_context_read_copies_nodes(store):
    function = _function("function")
    fact = Fact(id="fact", subject="setting", predicate="is", object_="on")
    preference = Preference(id="preference", aspect="theme", preference="dark")
    observation = Observation(id="observation", event="deploy", context="stable")
    paragraph = Paragraph(id="paragraph", source="test.md", section="1", raw_text="raw body")
    paragraph_id = persisted_paragraph_id("source", paragraph.id, paragraph.raw_text)
    store.add(function, _source())
    store.add_fact(fact)
    store.add_preference(preference)
    store.add_observation(observation)
    store.persist_paragraphs([paragraph], trust_tier=3, source_hint="source")
    ids = [function.id, fact.id, preference.id, observation.id, paragraph_id]

    nodes = store.read_context_nodes([*ids, "missing", function.id])
    assert set(nodes) == set(ids)
    nodes[function.id].trigger[0].desc = "tampered"
    nodes[function.id].trigger[0].sources.append("tampered")
    nodes[fact.id].provenance["tampered"] = "yes"
    nodes[preference.id].preference = "tampered"
    nodes[observation.id].context = "tampered"
    nodes[paragraph_id]["raw_text"] = "tampered"

    current = store.read_context_nodes(ids)
    assert current[function.id].trigger[0].desc == "committed content"
    assert current[function.id].trigger[0].sources == ["source"]
    assert current[fact.id].provenance == {}
    assert current[preference.id].preference == "dark"
    assert current[observation.id].context == "stable"
    assert current[paragraph_id]["raw_text"] == "raw body"


def test_context_read_deduplicates_before_refresh(store, monkeypatch):
    store.add(_function("deduplicated"), _source())
    refresh_count = 0
    original_refresh = store._refresh_for_read

    def count_refresh():
        nonlocal refresh_count
        refresh_count += 1
        original_refresh()

    monkeypatch.setattr(store, "_refresh_for_read", count_refresh)
    assert list(store.read_context_nodes(["deduplicated", "deduplicated"])) == ["deduplicated"]
    assert refresh_count == 1


def _postgres_without_database(monkeypatch):
    # Isolate facade/context behavior. Real RLS is covered in PG integration.
    store = object.__new__(PostgresMemoryStore)
    store._require_authorization = True
    for name in ("get", "get_fact", "get_preference", "get_observation", "_read_context_paragraph"):
        monkeypatch.setattr(store, name, lambda _node_id: None)
    return store


@pytest.mark.parametrize("bob_tenant", ["tenant-a", "tenant-b"])
def test_context_read_postgres_facade_keeps_concurrent_principal(monkeypatch, bob_tenant):
    store = _postgres_without_database(monkeypatch)
    gate = Barrier(2)

    def read_function(node_id):
        context = store._authorization_context()
        gate.wait(timeout=5)
        assert store._authorization_context() is context
        return _function(node_id, f"{context.principal.tenant_id}:{context.principal.subject_id}")

    monkeypatch.setattr(store, "get", read_function)
    alice = store.authorized(_context())
    bob = store.authorized(_context("bob", bob_tenant))
    with ThreadPoolExecutor(max_workers=2) as pool:
        alice_read = pool.submit(alice.read_context_nodes, ["shared", "shared"])
        bob_read = pool.submit(bob.read_context_nodes, ["shared"])
        assert alice_read.result(timeout=5)["shared"].trigger[0].desc == "tenant-a:alice"
        assert bob_read.result(timeout=5)["shared"].trigger[0].desc == f"{bob_tenant}:bob"
    with pytest.raises(PermissionError, match="authorization context"):
        store.read_context_nodes(["shared"])


def test_context_read_postgres_facade_resets_scope_after_exception(monkeypatch):
    store = _postgres_without_database(monkeypatch)

    def fail_read(_node_id):
        assert store._authorization_context().principal.subject_id == "alice"
        raise LookupError("database read failed")

    monkeypatch.setattr(store, "get", fail_read)
    with pytest.raises(LookupError, match="database read failed"):
        store.authorized(_context()).read_context_nodes(["missing"])
    with pytest.raises(PermissionError, match="authorization context"):
        store._authorization_context()
    monkeypatch.setattr(store, "get", lambda node_id: _function(node_id))
    assert store.authorized(_context("bob")).read_context_nodes(["bob"])["bob"].id == "bob"


def test_context_read_postgres_copies_typed_and_raw_results(monkeypatch):
    store = _postgres_without_database(monkeypatch)
    fact = Fact(id="fact", subject="setting", predicate="is", object_="on")
    raw = {"raw_text": "database body", "nested": {"source": "original"}}
    monkeypatch.setattr(store, "get_fact", lambda node_id: fact if node_id == fact.id else None)
    monkeypatch.setattr(store, "_read_context_paragraph", lambda node_id: raw if node_id == "raw" else None)
    facade = store.authorized(_context())
    nodes = facade.read_context_nodes(["fact", "raw", "missing"])
    assert set(nodes) == {"fact", "raw"}
    nodes["fact"].object_ = "tampered"
    nodes["raw"]["nested"]["source"] = "tampered"
    assert facade.read_context_nodes(["fact"])["fact"].object_ == "on"
    assert facade.read_context_nodes(["raw"])["raw"]["nested"]["source"] == "original"


def test_context_read_postgres_missing_raw_capability_never_uses_cache(monkeypatch):
    store = _postgres_without_database(monkeypatch)
    monkeypatch.setattr(store, "_read_context_paragraph", None)
    store._paragraphs = {"raw": {"raw_text": "untrusted cache body"}}
    assert store.authorized(_context()).read_context_nodes(["raw"]) == {}


def test_context_read_waits_for_batch_exit_commit(store, monkeypatch):
    commit_entered = Event()
    release_commit = Event()
    reader_started = Event()
    original_commit = store._commit_current_state

    def pause_final_commit():
        if store._commit_defer_depth == 0:
            commit_entered.set()
            assert release_commit.wait(timeout=5)
        original_commit()

    monkeypatch.setattr(store, "_commit_current_state", pause_final_commit)

    def write_batch():
        with store.deferred_commit():
            store.add(_function("exit-pending"), _source())

    def read_context():
        reader_started.set()
        return store.read_context_nodes(["exit-pending"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(write_batch)
        assert commit_entered.wait(timeout=5)
        reader = pool.submit(read_context)
        assert reader_started.wait(timeout=5)
        try:
            # A bounded future wait proves the read cannot finish in the
            # deliberately paused exit transition; no sleep schedules a race.
            with pytest.raises(TimeoutError):
                reader.result(timeout=0.2)
        finally:
            release_commit.set()
            writer.result(timeout=5)
        assert reader.result(timeout=5)["exit-pending"].id == "exit-pending"


@pytest.mark.parametrize("writer_mode", ["1", "0"], ids=["queued", "direct"])
def test_context_read_recovers_failed_batch_domain_validation(store, monkeypatch, writer_mode):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", writer_mode)
    original = _function("domain-function", "durable original")
    original.domain = "security"
    store.add(original, _source())
    store.merge(GraphData(nodes=[], edges=[GraphEdge(
        source=original.id, target=domain_node_id("security"), edge_type="BELONGS_TO",
    )]))
    replacement = _function(original.id, "uncommitted replacement")
    replacement.domain = "networking"

    with pytest.raises(ValueError, match="BELONGS_TO"), store.deferred_commit():
        store.replace_function(replacement)

    current = store.read_context_nodes([original.id])[original.id]
    assert current.trigger[0].desc == "durable original"
    assert current.domain == "security"
    durable = LiteMemoryStore(store._path).read_context_nodes([original.id])[original.id]
    assert durable.trigger[0].desc == "durable original"
    assert durable.domain == "security"
    store.add(_function("after-validation-failure"), _source())
    assert store.read_context_nodes(["after-validation-failure"])["after-validation-failure"].id


@pytest.mark.parametrize("writer_mode", ["1", "0"], ids=["queued", "direct"])
def test_context_read_recovers_failed_batch_target_serialization(store, monkeypatch, writer_mode):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", writer_mode)
    store.add(_function("original", "durable original"), _source())

    def fail_serialization():
        raise ValueError("target serialization failed")

    with (
        monkeypatch.context() as patch,
        pytest.raises(ValueError, match="target serialization failed"),
        store.deferred_commit(),
    ):
        store.add(_function("pending", "uncommitted serialization body"), _source())
        patch.setattr(store, "_raw_memory", fail_serialization)

    current = store.read_context_nodes(["original", "pending"])
    assert set(current) == {"original"}
    assert current["original"].trigger[0].desc == "durable original"
    assert LiteMemoryStore(store._path).read_context_nodes(["pending"]) == {}
    store.add(_function("after-serialization-failure"), _source())
    assert store.read_context_nodes(["after-serialization-failure"])["after-serialization-failure"].id


@pytest.mark.parametrize("writer_mode", ["1", "0"], ids=["queued", "direct"])
def test_context_read_fails_closed_when_failed_batch_cannot_reload(store, monkeypatch, writer_mode):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", writer_mode)
    store.add(_function("original"), _source())

    def fail_serialization():
        raise ValueError("target serialization failed")

    with (
        monkeypatch.context() as patch,
        pytest.raises(ValueError, match="target serialization failed"),
        store.deferred_commit(),
    ):
        store.add(_function("pending", "uncommitted body"), _source())
        patch.setattr(store, "_raw_memory", fail_serialization)

    def fail_reload():
        raise OSError("authoritative source unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store._durability, "_load_authoritative_locked", fail_reload)
        with pytest.raises(OSError, match="authoritative source unavailable"):
            store.read_context_nodes(["pending"])
    assert store.read_context_nodes(["pending"]) == {}
    assert store.read_context_nodes(["original"])["original"].id == "original"


@pytest.mark.parametrize("writer_mode", ["1", "0"], ids=["queued", "direct"])
def test_context_read_recovers_current_authority_after_post_decision_error(
    store, monkeypatch, writer_mode,
):
    monkeypatch.setenv("MEMPLEX_LITE_SINGLE_WRITER", writer_mode)
    store.add(_function("original"), _source())
    original_commit = store._commit_current_state

    def commit_then_fail():
        original_commit()
        raise OSError("post-decision publication interrupted")

    with (
        monkeypatch.context() as patch,
        pytest.raises(OSError, match="post-decision"),
        store.deferred_commit(),
    ):
        store.add(_function("committed-after-error", "new durable content"), _source())
        patch.setattr(store, "_commit_current_state", commit_then_fail)

    # A stale pre-commit snapshot would wrongly discard the durable outcome.
    current = store.read_context_nodes(["committed-after-error"])["committed-after-error"]
    assert current.trigger[0].desc == "new durable content"
    durable = LiteMemoryStore(store._path).read_context_nodes(["committed-after-error"])
    assert durable["committed-after-error"].trigger[0].desc == "new durable content"


class _CountedObservationList(list):
    """Count actual resident observations visited, not requested candidate IDs."""

    def __init__(self, values):
        super().__init__(values)
        self.visited = 0

    def __iter__(self):
        for item in super().__iter__():
            self.visited += 1
            yield item


@pytest.mark.parametrize("corpus_size", [64, 512])
def test_context_source_lookup_work_is_independent_of_observation_corpus(store, corpus_size):
    with store.deferred_commit():
        for index in range(corpus_size):
            store.add_observation(Observation(id=f"observation-{index}", event=f"event-{index}"))
        store.persist_paragraphs(
            [Paragraph(id="raw", source="test", section="1", raw_text="raw current")],
            trust_tier=1, source_hint="test",
        )
    raw_id = persisted_paragraph_id("test", "raw", "raw current")
    counted = _CountedObservationList(store._observations)
    store._observations = counted
    last_id = f"observation-{corpus_size - 1}"
    for _ in range(20):
        nodes = store.read_context_nodes([last_id, "missing", raw_id, last_id])
        assert nodes[last_id].event == f"event-{corpus_size - 1}"
        assert nodes[raw_id]["raw_text"] == "raw current"
        assert store.get_observation(last_id).id == last_id
        assert store.get_observation("missing") is None
    assert counted.visited == 0, "stable source reads must not rescan the resident Observation table"


def test_context_observation_lookup_tracks_local_peer_clear_and_reopen(store):
    store.add_observation(Observation(id="first", event="FIRST-COMMITTED"))
    assert store.read_context_nodes(["first"])["first"].event == "FIRST-COMMITTED"
    peer = LiteMemoryStore(store._path)
    peer.add_observation(Observation(id="second", event="SECOND-COMMITTED"))
    assert store.get_observation("second").event == "SECOND-COMMITTED"
    peer.delete_observation("first")
    assert store.read_context_nodes(["first", "second"]).keys() == {"second"}
    assert store.get_observation("first") is None
    store.delete_observation("second")
    assert peer.read_context_nodes(["second"]) == {}
    store.add_observation(Observation(id="third", event="THIRD-COMMITTED"))
    assert LiteMemoryStore(store._path).get_observation("third").event == "THIRD-COMMITTED"
    peer.clear()
    assert store.read_context_nodes(["third"]) == {}
    assert store.get_observation("third") is None


def test_context_observation_lookup_recovers_failed_finalization(store, monkeypatch):
    store.add_observation(Observation(id="committed", event="COMMITTED"))
    assert store.get_observation("committed").event == "COMMITTED"
    commit = store._commit_current_state

    def fail_finalization():
        if store._commit_defer_depth == 0:
            raise OSError("forced observation finalization failure")
        return commit()

    with monkeypatch.context() as scoped:
        scoped.setattr(store, "_commit_current_state", fail_finalization)
        with pytest.raises(OSError, match="forced observation"), store.deferred_commit():
            store.delete_observation("committed")
            store.add_observation(Observation(id="speculative", event="FAILED-TEXT"))
            # Preserve the established ordinary getter's in-batch semantics.
            assert store.get_observation("speculative").event == "FAILED-TEXT"
            assert store.get_observation("committed") is None
            assert store.read_context_nodes(["committed", "speculative"]) == {}
    assert store.read_context_nodes(["committed", "speculative"])["committed"].event == "COMMITTED"
    assert store.get_observation("speculative") is None


def test_context_observation_lookup_tracks_sync_replacement_and_tombstone(store):
    import uuid
    from datetime import UTC, datetime, timedelta

    from memplex.sync_protocol import (
        SyncBatch,
        SyncEntityKey,
        SyncEvent,
        SyncNodeType,
        SyncOperation,
        SyncScope,
        SyncVersion,
    )

    # Same-ID Observation replacement is supported by inbound sync, not add_observation.
    for index, operation in enumerate([SyncOperation.UPSERT, SyncOperation.UPSERT, SyncOperation.TOMBSTONE]):
        event_id = str(uuid.uuid4())
        node = Observation(id="synced", event=f"SYNC-CURRENT-{index}")
        event = SyncEvent(
            1, event_id, "remote-a", SyncNodeType.OBSERVATION, SyncEntityKey.node(node.id),
            operation, str(SyncVersion.create(datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index), "remote-a", event_id)),
            SyncScope("tenant-a", "alice", "workspace", "workspace", "agent", "session-alice"),
            node.to_dict() if operation is SyncOperation.UPSERT else None,
        )
        result = store.sync_apply_batch(SyncBatch(1, str(uuid.uuid4()), "remote-a", (event,)))
        assert result.receipts[0].outcome == "accepted"
        if operation is SyncOperation.TOMBSTONE:
            assert store.read_context_nodes([node.id]) == {}
            assert store.get_observation(node.id) is None
        else:
            assert store.read_context_nodes([node.id])[node.id].event == node.event
            assert store.get_observation(node.id).event == node.event


def test_context_observation_lookup_tracks_restore(store, tmp_path):
    store.add_observation(Observation(id="backup", event="BACKUP-COMMITTED"))
    key = bytes(range(32))  # Synthetic fixture key only.
    manifest = store.create_backup(tmp_path / "backups", key, "fixture-key")
    store.delete_observation("backup")
    store.add_observation(Observation(id="later", event="AFTER-BACKUP"))
    assert store.get_observation("later").event == "AFTER-BACKUP"
    store.restore_backup(tmp_path / "backups" / manifest.backup_id, key)
    assert store.read_context_nodes(["backup", "later"])["backup"].event == "BACKUP-COMMITTED"
    assert store.get_observation("later") is None


def test_context_observation_index_preserves_first_match_and_source_precedence(store):
    # Deliberately malformed resident duplicate protects the old next(...) order;
    # normal persisted decoders still reject duplicate Observation IDs.
    first = Observation(id="duplicate", event="FIRST")
    store._observations = [first, Observation(id="duplicate", event="SECOND")]
    store._rebuild_observation_index()
    assert store.get_observation("duplicate").event == "FIRST"
    store._paragraphs["duplicate"] = {"id": "duplicate", "raw_text": "RAW"}
    assert store.read_context_nodes(["duplicate"])["duplicate"].event == "FIRST"
    store._preferences["duplicate"] = Preference(id="duplicate", preference="PREFERENCE")
    assert isinstance(store.read_context_nodes(["duplicate"])["duplicate"], Preference)
    store._facts["duplicate"] = Fact(id="duplicate", object_="FACT")
    assert isinstance(store.read_context_nodes(["duplicate"])["duplicate"], Fact)
    store._functions["duplicate"] = _function("duplicate")
    assert isinstance(store.read_context_nodes(["duplicate"])["duplicate"], Function)
