"""Captured memories only compact within their trusted, canonical write scope."""

import math
import sys
import types

import pytest

from memplex.compaction import CompactionPipeline
from memplex.config import MemplexConfig
from memplex.models import CompactionScope, Fact, FieldValue, Function, Preference
from memplex.retrieval.dedup import DedupStrategy, MemoryDeduplicator
from memplex.storage.lite.store import LiteMemoryStore


class _MatchingEmbedder:
    """Different wording can intentionally describe the same semantic content."""

    def embed_batch(self, texts):
        return [[1.0, 0.0] for _ in texts]


def _capture(identifier, *, name="remember the deployment procedure", **overrides):
    fields = {
        "id": f"func_capture_v1_{identifier}",
        "name": name,
        "name_normalized": f"captured:{identifier}",
        "tenant_id": "tenant-a",
        "owner_subject_id": "owner-a",
        "workspace_id": "workspace-a",
        "visibility": "session",
        "origin_session": "session-a",
        "provenance": {"agent_id": "agent-a"},
        "trigger": [FieldValue(desc="deploy the application")],
        "updated_at": "2026-10-05T00:00:00+00:00",
        "access_count": 1,
    }
    fields.update(overrides)
    return Function(**fields)


def _ordinary(identifier, **overrides):
    return _capture(identifier, id=f"ordinary_{identifier}", **overrides)


@pytest.fixture(params=["numpy", "python", "faiss"])
def semantic_backend(request, monkeypatch):
    """Exercise real clustering code with only optional dependency loading replaced.

    FAISS is optional and unavailable in the lite environment. Its index test
    double performs the same exact inner-product search, while the production
    FAISS clustering/merge implementation is left intact.
    """
    if request.param == "python":
        monkeypatch.setitem(sys.modules, "numpy", None)
    elif request.param == "faiss":
        import numpy as np

        class _ExactInnerProductIndex:
            def __init__(self, dimensions):
                self.dimensions = dimensions
                self.vectors = None

            def add(self, vectors):
                assert vectors.shape[1] == self.dimensions
                self.vectors = vectors

            def search(self, vectors, k):
                similarities = vectors @ self.vectors.T
                indices = np.argsort(-similarities, axis=1, kind="stable")[:, :k]
                return np.take_along_axis(similarities, indices, axis=1), indices

        module = types.ModuleType("faiss")
        module.IndexFlatIP = _ExactInnerProductIndex
        monkeypatch.setitem(sys.modules, "faiss", module)
    return request.param


def _deduplicator(backend, strategy, *, chunk_threshold=20000, embedder=None):
    return MemoryDeduplicator(
        embedder or _MatchingEmbedder(),
        strategy=strategy,
        use_faiss=backend == "faiss",
        chunk_threshold=chunk_threshold,
        threshold=0.8,
    )


@pytest.mark.parametrize("strategy", list(DedupStrategy))
def test_identical_captures_preserve_every_scope_axis(strategy, semantic_backend):
    memories = [
        _capture("base"),
        _capture("tenant", tenant_id="tenant-b"),
        _capture("owner", owner_subject_id="owner-b"),
        _capture("workspace", workspace_id="workspace-b"),
        _capture("session", origin_session="session-b"),
        _capture("agent", provenance={"agent_id": "agent-b"}),
        _capture("workspace_visibility", visibility="workspace"),
        _capture("user_visibility", visibility="user"),
    ]

    result = _deduplicator(semantic_backend, strategy).deduplicate(memories)

    assert {memory.id for memory in result.deduplicated} == {memory.id for memory in memories}
    assert result.original_count == result.final_count == len(memories)
    assert result.exact_removed == result.semantic_removed == 0


@pytest.mark.parametrize("strategy", list(DedupStrategy))
@pytest.mark.parametrize(
    "invalid_scope",
    [
        {"tenant_id": None},
        {"tenant_id": ""},
        {"tenant_id": "   "},
        {"owner_subject_id": None},
        {"workspace_id": None},
        {"visibility": None},
        {"visibility": "unknown"},
        {"origin_session": None},
        {"provenance": {}},
        {"provenance": {"agent_id": ""}},
    ],
)
def test_unknown_capture_identity_fails_closed_per_record(
    strategy, semantic_backend, invalid_scope
):
    memories = [
        _capture("unknown-a", **invalid_scope),
        _capture("unknown-b", **invalid_scope),
        _capture("known"),
        _ordinary("legacy"),
    ]

    result = _deduplicator(semantic_backend, strategy).deduplicate(memories)

    assert {memory.id for memory in result.deduplicated} == {memory.id for memory in memories}
    assert result.exact_removed == result.semantic_removed == 0


@pytest.mark.parametrize("strategy", list(DedupStrategy))
def test_captured_and_ordinary_partitions_never_merge(strategy, semantic_backend):
    memories = [_capture("captured"), _ordinary("legacy")]

    result = _deduplicator(semantic_backend, strategy).deduplicate(memories)

    assert result.final_count == 2
    assert {memory.id for memory in result.deduplicated} == {memory.id for memory in memories}


@pytest.mark.parametrize("strategy", list(DedupStrategy))
@pytest.mark.parametrize("visibility", ["session", "workspace", "user"])
def test_same_scope_captures_still_deduplicate(strategy, semantic_backend, visibility):
    older = _capture("older", visibility=visibility)
    newer = _capture("newer", visibility=visibility, updated_at="2026-10-05T01:00:00+00:00")

    result = _deduplicator(semantic_backend, strategy).deduplicate([older, newer])

    assert result.final_count == 1
    assert result.deduplicated[0].id == newer.id
    assert result.exact_removed + result.semantic_removed == 1


@pytest.mark.parametrize("strategy", list(DedupStrategy))
def test_ordinary_memories_retain_legacy_cross_identity_dedup(strategy, semantic_backend):
    memories = [
        _ordinary("first"),
        _ordinary("second", tenant_id="tenant-b", owner_subject_id="owner-b"),
        _ordinary("third", tenant_id=None, visibility=None, provenance={}),
    ]

    result = _deduplicator(semantic_backend, strategy).deduplicate(memories)

    assert result.final_count == 1
    assert result.exact_removed + result.semantic_removed == 2


@pytest.mark.parametrize("strategy", list(DedupStrategy))
def test_namespace_metadata_cannot_join_or_split_capture_scope(strategy, semantic_backend):
    first = _capture("first", namespace={"user": "owner-a"})
    same_scope = _capture("same-scope", namespace={"user": "owner-b"})
    other_scope = _capture("other-scope", owner_subject_id="owner-b", namespace=first.namespace)

    result = _deduplicator(semantic_backend, strategy).deduplicate([first, same_scope, other_scope])

    assert result.final_count == 2
    assert {memory.owner_subject_id for memory in result.deduplicated} == {"owner-a", "owner-b"}


@pytest.mark.parametrize("visibility", ["user", "workspace"])
def test_non_session_scope_ignores_session_and_agent(visibility, semantic_backend):
    first = _capture("first", visibility=visibility)
    second = _capture(
        "second", visibility=visibility, origin_session="session-b", provenance={"agent_id": "agent-b"}
    )
    if visibility == "user":
        second.workspace_id = "workspace-b"

    result = _deduplicator(semantic_backend, DedupStrategy.BOTH).deduplicate([first, second])

    assert result.final_count == 1
    assert result.exact_removed == 1


@pytest.mark.parametrize("strategy", list(DedupStrategy))
@pytest.mark.parametrize("chunk_threshold", [1, 20000])
def test_partition_counters_aggregate_and_reset(strategy, semantic_backend, chunk_threshold):
    memories = []
    for factory, scope in [(_ordinary, {}), (_capture, {}), (_capture, {"owner_subject_id": "owner-b"})]:
        start = len(memories)
        memories.extend([
            factory(f"{start}-first", **scope),
            factory(f"{start}-exact", **scope),
            factory(f"{start}-semantic", name="ship the application safely", **scope),
        ])
    memories.extend([_capture("unknown-a", tenant_id=None), _capture("unknown-b", tenant_id=None)])
    deduplicator = _deduplicator(semantic_backend, strategy, chunk_threshold=chunk_threshold)

    result = deduplicator.deduplicate(memories)

    expected = {
        DedupStrategy.EXACT: (8, 3, 0),
        DedupStrategy.SEMANTIC: (5, 0, 6),
        DedupStrategy.BOTH: (5, 3, 3),
    }
    assert result.original_count == 11
    assert (result.final_count, result.exact_removed, result.semantic_removed) == expected[strategy]
    assert result.original_count - result.final_count == result.exact_removed + result.semantic_removed
    reset = deduplicator.deduplicate([_capture("single")])
    assert reset.original_count == reset.final_count == 1
    assert reset.exact_removed == reset.semantic_removed == 0


@pytest.mark.parametrize("strategy", [DedupStrategy.SEMANTIC, DedupStrategy.BOTH])
def test_singleton_capture_scopes_need_no_semantic_embedding(strategy, semantic_backend):
    class _CountingEmbedder(_MatchingEmbedder):
        def __init__(self):
            self.calls = 0

        def embed_batch(self, texts):
            self.calls += 1
            return super().embed_batch(texts)

    embedder = _CountingEmbedder()
    memories = [_capture(str(index), origin_session=f"session-{index}") for index in range(100)]
    memories.extend([_capture("unknown-a", tenant_id=None), _capture("unknown-b", tenant_id=None)])

    result = _deduplicator(semantic_backend, strategy, embedder=embedder).deduplicate(memories)

    assert result.original_count == result.final_count == 102
    assert result.exact_removed == result.semantic_removed == 0
    assert embedder.calls == 0


def test_exact_dedup_to_singleton_needs_no_semantic_embedding(semantic_backend):
    class _CountingEmbedder(_MatchingEmbedder):
        def __init__(self):
            self.calls = 0

        def embed_batch(self, texts):
            self.calls += 1
            return super().embed_batch(texts)

    embedder = _CountingEmbedder()
    result = _deduplicator(
        semantic_backend, DedupStrategy.BOTH, embedder=embedder
    ).deduplicate([_capture("first"), _capture("second")])

    assert result.original_count == 2
    assert result.final_count == result.exact_removed == 1
    assert result.semantic_removed == 0
    assert embedder.calls == 0


def test_legacy_semantic_bridge_cannot_connect_captured_owners(semantic_backend):
    class _BridgeEmbedder:
        def embed_batch(self, texts):
            vectors = {"left": [1.0, 0.0], "bridge": [math.sqrt(0.75), 0.5], "right": [0.5, math.sqrt(0.75)]}
            return [vectors[text.split()[0]] for text in texts]

    memories = [
        _capture("left", name="left", owner_subject_id="owner-a"),
        _ordinary("bridge", name="bridge"),
        _capture("right", name="right", owner_subject_id="owner-b"),
    ]

    result = _deduplicator(
        semantic_backend, DedupStrategy.SEMANTIC, embedder=_BridgeEmbedder()
    ).deduplicate(memories)

    assert result.final_count == 3
    assert result.semantic_removed == 0
    assert {memory.id for memory in result.deduplicated} == {memory.id for memory in memories}


@pytest.mark.parametrize("strategy", list(DedupStrategy))
@pytest.mark.parametrize("memory_type,prefix", [(Fact, "fact"), (Preference, "pref")])
def test_all_capture_node_prefixes_receive_isolation(strategy, semantic_backend, memory_type, prefix):
    first = memory_type(
        id=f"{prefix}_capture_v1_first",
        name="the same captured content",
        tenant_id="tenant-a",
        owner_subject_id="owner-a",
        visibility="user",
    )
    second = memory_type.from_dict({**first.to_dict(), "id": f"{prefix}_capture_v1_second", "owner_subject_id": "owner-b"})

    result = _deduplicator(semantic_backend, strategy).deduplicate([first, second])

    assert result.final_count == 2
    assert {memory.owner_subject_id for memory in result.deduplicated} == {"owner-a", "owner-b"}


@pytest.mark.asyncio
async def test_full_compaction_preserves_scoped_owners_on_disk_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path=path)
    config = MemplexConfig()
    config.compaction.dedup_use_faiss = False
    owner_a = _capture("owner-a")
    duplicate_a = _capture("owner-a-duplicate", updated_at="2026-10-05T01:00:00+00:00")
    owner_b = _capture("owner-b", owner_subject_id="owner-b")
    legacy = _ordinary("legacy")
    store.apply_compaction(replacements=[owner_a, duplicate_a, owner_b, legacy], delete_ids=[])
    pipeline = CompactionPipeline(store=store, embedding_service=_MatchingEmbedder(), config=config)

    result = await pipeline.run(CompactionScope.GLOBAL)

    assert result.skipped is False
    assert result.total_removed == 1
    assert store.get(owner_a.id) is None
    expected_ids = {duplicate_a.id, owner_b.id, legacy.id}
    assert {memory.id for memory in store.list_functions()} == expected_ids
    restarted = LiteMemoryStore(path=path)
    assert {memory.id for memory in restarted.list_functions()} == expected_ids
    assert restarted.get(owner_b.id).owner_subject_id == "owner-b"
    assert restarted.get(duplicate_a.id).origin_session == "session-a"
    rerun = await CompactionPipeline(
        store=restarted, embedding_service=_MatchingEmbedder(), config=config
    ).run(CompactionScope.GLOBAL)
    assert rerun.total_removed == 0
    assert {memory.id for memory in LiteMemoryStore(path=path).list_functions()} == expected_ids
