"""Durable-pair contract for the development-only Lite store."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import uuid
from copy import deepcopy
from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest

from memplex.models import (
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    Observation,
    Preference,
    SourceDocument,
    SourceType,
)
from memplex.storage import create_store
from memplex.storage.lite import durability as durability_module
from memplex.storage.lite.durability import LitePair, LiteStorageIntegrityError, _pair_record
from memplex.storage.lite.store import LiteMemoryStore
from memplex.sync_protocol import (
    SyncBatchResult,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncReceipt,
    SyncScope,
    SyncVersion,
)


@pytest.fixture(autouse=True)
def _isolate_factory_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """让 factory 回退路径始终位于 pytest 临时目录，绝不触及用户默认库。"""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated-home"))


def _source() -> SourceDocument:
    return SourceDocument(type="test", source_type=SourceType.WIKI)


def _function(identifier: str) -> Function:
    return Function(id=identifier, name=identifier, name_normalized=identifier)


def _tenant_function(identifier: str) -> Function:
    node = _function(identifier)
    node.tenant_id = "tenant-a"
    return node


def _valid_nonempty_sync_state() -> dict:
    occurred_at = datetime(2026, 8, 11, tzinfo=UTC)
    event_id = str(uuid.UUID(int=101))
    batch_id = str(uuid.UUID(int=102))
    snapshot_id = str(uuid.UUID(int=103))
    entity_key = SyncEntityKey.node("fn")
    version = str(SyncVersion.create(occurred_at, "origin-a", event_id))
    event = SyncEvent(
        protocol_version=1,
        event_id=event_id,
        origin_node_id="origin-a",
        node_type=SyncNodeType.FUNCTION,
        entity_key=entity_key,
        operation=SyncOperation.UPSERT,
        version=version,
        scope=SyncScope("tenant-a", "owner-a", None, "user", None, None),
        payload={"id": "fn"},
    )
    request_digest = "0" * 64
    response = SyncBatchResult(
        batch_id,
        request_digest,
        "accepted",
        (SyncReceipt(event_id, "accepted"),),
    )
    return {
        "tenant_binding": "tenant-a",
        "next_stream_seq": 2,
        "retention_floor": 0,
        "compacted_through": 0,
        "outbox": [
            {
                "stream_seq": 1,
                "event_id": event_id,
                "origin_node_id": "origin-a",
                "node_type": "function",
                "entity_key": str(entity_key),
                "operation": "upsert",
                "version_key": version,
                "payload": {"id": "fn"},
                "tenant_id": "tenant-a",
                "visibility": "user",
                "owner_subject_id": "owner-a",
                "workspace_id": None,
                "agent_id": None,
                "session_id": None,
                "created_at": occurred_at.isoformat(),
            }
        ],
        "entity_versions": [
            {
                "node_type": "function",
                "entity_key": str(entity_key),
                "version_key": version,
                "deleted": False,
                "event_id": event_id,
                "last_stream_seq": 1,
            }
        ],
        "targets": [
            {
                "target_id": "target-a",
                "remote_node_id": "remote-a",
                "bootstrap_seq": 0,
                "enabled": True,
            }
        ],
        "deliveries": [
            {
                "target_id": "target-a",
                "stream_seq": 1,
                "state": "pending",
                "attempt_count": 0,
                "next_attempt_at": occurred_at.isoformat(),
                "lease_owner": None,
                "lease_until": None,
                "last_error_code": None,
            }
        ],
        "inbox": [
            {
                "origin_node_id": "origin-a",
                "event_id": event_id,
                "outcome": "accepted",
                "applied_stream_seq": 1,
            }
        ],
        "batches": [
            {
                "batch_id": batch_id,
                "request_sha256": request_digest,
                "response": response.to_dict(),
                "created_at": occurred_at.isoformat(),
            }
        ],
        "cursors": [
            {
                "remote_id": "remote-a",
                "consumer_id": "consumer-a",
                "after_seq": 1,
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ],
        "inbound_cursors": [],
        "snapshots": [
            {
                "snapshot_id": snapshot_id,
                "remote_id": "remote-a",
                "consumer_id": "consumer-a",
                "request_id": "request-a",
                "resume_seq": 1,
                "expires_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
            }
        ],
        "snapshot_items": [
            {
                "snapshot_id": snapshot_id,
                "node_type": "function",
                "entity_key": str(entity_key),
                "event": event.to_dict(),
            }
        ],
    }


def _journal(base: LitePair, target: LitePair) -> dict:
    return {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }


_G002_IDENTITY_FIELDS = (
    "tenant_id",
    "owner_subject_id",
    "workspace_id",
    "visibility",
    "provenance",
)


def _as_g002_historical_node(raw: dict) -> dict:
    """只构造已知的 G002 前五个 identity 字段缺失形态。"""
    historical = deepcopy(raw)
    for field in _G002_IDENTITY_FIELDS:
        historical.pop(field)
    if historical["memory_type"] == "fact":
        historical["object_"] = historical.pop("object")
    return historical


def _write_g002_mixed_function_collection(path: Path) -> tuple[dict, dict]:
    """将四种旧节点放回历史 ``functions`` collection，并重算 pair digest。"""
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = path.with_name("changelog.json")
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    payload = memory["payload"]
    mixed = [
        _as_g002_historical_node(raw)
        for collection in ("functions", "facts", "preferences", "observations")
        for raw in payload[collection]
    ]
    payload["functions"] = mixed
    payload["facts"] = []
    payload["preferences"] = []
    payload["observations"] = []
    payload["schema_version"] = 1
    payload.pop("sync", None)
    changelog["peer_digest"] = durability_module._digest(payload)
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    return memory, changelog


def _set_g002_schema_version_one(path: Path) -> None:
    """将历史 fixture 明确标记为 schema_version=1，以进入独立 G002 兼容分支。"""
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = path.with_name("changelog.json")
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["schema_version"] = 1
    memory["payload"].pop("sync", None)
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")


def _make_g002_mixed_fixture(
    tmp_path: Path, *, counts: tuple[int, int, int, int] = (1, 1, 1, 1)
) -> Path:
    """创建缩小的 904/57/5/5 mixed-node 等价矩阵。"""
    path = tmp_path / "memory.json"
    writer = LiteMemoryStore(path)
    function_count, fact_count, preference_count, observation_count = counts
    for index in range(function_count):
        suffix = "" if index == 0 else f"-{index}"
        function = _function(f"legacy-function{suffix}")
        function.action = [FieldValue(desc=f"historic mixed function {index}")]
        writer.add(function, _source())
    for index in range(fact_count):
        writer.add_fact(
            Fact(id=f"legacy-fact-{index}", name="fact", subject="a", predicate="is", object_="b")
        )
    for index in range(preference_count):
        writer.add_preference(
            Preference(id=f"legacy-preference-{index}", name="pref", aspect="style", preference="brief")
        )
    for index in range(observation_count):
        writer.add_observation(
            Observation(id=f"legacy-observation-{index}", name="obs", event="seen", context="test")
        )
    writer.merge(
        GraphData(
            nodes=[],
            edges=[
                GraphEdge(
                    source=f"legacy-function{'-' + str(index) if index else ''}",
                    target=f"legacy-target-{index}",
                    edge_type="REFERENCES",
                )
                for index in range(function_count)
            ],
        )
    )
    return path


def _preopen_then_add(root: str, identifier: str, ready, start, result) -> None:
    store = LiteMemoryStore(Path(root) / "memory.json")
    ready.set()
    start.wait(10)
    try:
        store.add(_function(identifier), _source())
        result.put("ok")
    except Exception as exc:  # pragma: no cover - asserted in parent  # noqa: BLE001 - broad catch with explicit fallback handling
        result.put(f"error:{exc}")


def _preopen_action(root: str, action: str, ready, start, result) -> None:
    store = LiteMemoryStore(Path(root) / "memory.json")
    ready.set()
    start.wait(10)
    try:
        if action == "add":
            store.add(_function("add"), _source())
        elif action == "merge":
            store.merge(GraphData(nodes=[_function("merge")], edges=[]))
        elif action == "access":
            store.increment_access("counter")
        elif action == "clear":
            store.clear()
        elif action == "typed-fact":
            store.add_fact(Fact(id="fact", name="fact", subject="s", predicate="is", object_="o"))
        elif action == "typed-pref":
            store.add_preference(Preference(id="pref", name="pref", aspect="a", preference="p"))
        else:  # pragma: no cover - test wiring
            raise AssertionError(action)
        result.put("ok")
    except Exception as exc:  # pragma: no cover - asserted in parent  # noqa: BLE001 - broad catch with explicit fallback handling
        result.put(f"error:{exc}")


def _run_two_actions(tmp_path: Path, first: str, second: str) -> list[str]:
    ctx = multiprocessing.get_context("spawn")
    ready_a, ready_b, start = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(target=_preopen_action, args=(str(tmp_path), action, ready, start, results))
        for action, ready in ((first, ready_a), (second, ready_b))
    ]
    for worker in workers:
        worker.start()
    assert ready_a.wait(10) and ready_b.wait(10)
    start.set()
    for worker in workers:
        worker.join(15)
        assert worker.exitcode == 0
    return sorted(results.get(timeout=2) for _ in workers)


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("before_journal_durable_publish", {"old"}),
        ("after_journal_rename_and_parent_dir_fsync", {"old", "new"}),
        ("after_memory_replace", {"old", "new"}),
        ("after_changelog_replace", {"old", "new"}),
        ("after_journal_unlink", {"old", "new"}),
        ("after_final_parent_dir_fsync", {"old", "new"}),
    ],
)
def test_lite_fault_recovery_obeys_durable_decision_point(tmp_path: Path, monkeypatch, fault, expected):
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("old"), _source())

    def cut() -> None:
        raise OSError("cut")

    monkeypatch.setattr(store._durability, fault, cut)
    with pytest.raises(OSError, match="cut"):
        store.add(_function("new"), _source())
    assert {item.id for item in LiteMemoryStore(path).list_functions()} == expected


def test_generation_mismatch_without_journal_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    doc = json.loads(path.read_text())
    doc["generation"] += 1
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="generation"):
        LiteMemoryStore(path)


def test_custom_store_sidecars_are_isolated(tmp_path: Path) -> None:
    left = LiteMemoryStore(tmp_path / "a.json")
    right = LiteMemoryStore(tmp_path / "b.json")
    left.add(_function("a"), _source())
    right.add(_function("b"), _source())
    assert (tmp_path / "a.changelog.json").exists()
    assert (tmp_path / "b.changelog.json").exists()
    assert {item.id for item in LiteMemoryStore(tmp_path / "a.json").list_functions()} == {"a"}


def test_complete_legacy_pair_is_adopted_immediately(tmp_path: Path) -> None:
    (tmp_path / "memory.json").write_text(
        json.dumps({"schema_version": 1, "functions": [], "edges": []}), encoding="utf-8"
    )
    (tmp_path / "changelog.json").write_text("[]", encoding="utf-8")
    LiteMemoryStore(tmp_path / "memory.json")
    memory = json.loads((tmp_path / "memory.json").read_text(encoding="utf-8"))
    changelog = json.loads((tmp_path / "changelog.json").read_text(encoding="utf-8"))
    assert memory["format_version"] == changelog["format_version"] == 2
    assert memory["generation"] == changelog["generation"] == 1


def test_empty_pair_uses_schema_v2_with_empty_sync(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    pair = store._durability.load_authoritative()
    sync = pair.memory["sync"]
    assert pair.memory["schema_version"] == 2
    assert sync == {
        "tenant_binding": None,
        "next_stream_seq": 1,
        "retention_floor": 0,
        "compacted_through": 0,
        "outbox": [],
        "entity_versions": [],
        "targets": [],
        "deliveries": [],
        "inbox": [],
        "batches": [],
        "cursors": [],
        "inbound_cursors": [],
        "snapshots": [],
        "snapshot_items": [],
    }


def test_nonempty_legacy_pair_is_canonicalized_before_v2_adoption(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    changelog_path = tmp_path / "changelog.json"
    path.write_text(
        json.dumps(
            {
                "functions": [{"id": "legacy", "name": "legacy"}],
                "edges": [{"source": "legacy", "target": "fact", "edge_type": "REFERENCES"}],
                "facts": [{"id": "fact", "name": "fact", "subject": "a", "predicate": "is", "object_": "b"}],
                "preferences": [{"id": "pref", "name": "pref", "aspect": "style", "preference": "brief"}],
                "observations": [{"id": "obs", "name": "obs", "event": "seen", "context": "test"}],
            }
        ),
        encoding="utf-8",
    )
    changelog_path.write_text(
        json.dumps(
            [{
                "func_id": "legacy", "timestamp": "2026-08-10T00:00:00+00:00",
                "event_type": "created", "description": "legacy", "source": "", "actor": "system",
            }]
        ),
        encoding="utf-8",
    )

    store = LiteMemoryStore(path)
    memory = json.loads(path.read_text(encoding="utf-8"))
    assert memory["payload"]["schema_version"] == 2
    assert memory["payload"]["sync"] == {
        "tenant_binding": None,
        "next_stream_seq": 1,
        "retention_floor": 0,
        "compacted_through": 0,
        "outbox": [],
        "entity_versions": [],
        "targets": [],
        "deliveries": [],
        "inbox": [],
        "batches": [],
        "cursors": [],
        "inbound_cursors": [],
        "snapshots": [],
        "snapshot_items": [],
    }
    assert memory["payload"]["facts"][0]["object"] == "b"
    assert "object_" not in memory["payload"]["facts"][0]
    assert {item.id for item in store.list_functions()} == {"legacy"}
    assert {item.id for item in store.list_facts()} == {"fact"}
    assert {item.id for item in store.list_preferences()} == {"pref"}
    assert {item.id for item in store.list_observations()} == {"obs"}
    assert len(memory["payload"]["edges"]) == 1
    assert LiteMemoryStore(path).get_timeline("legacy")[0].description == "legacy"


def test_schema_v1_envelope_is_canonicalized_to_schema_v2_with_empty_sync(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory_doc = json.loads(path.read_text(encoding="utf-8"))
    prior_generation = memory_doc["generation"]
    changelog_doc = json.loads((tmp_path / "changelog.json").read_text(encoding="utf-8"))
    legacy_payload = deepcopy(memory_doc["payload"])
    legacy_payload["schema_version"] = 1
    legacy_payload.pop("sync", None)
    memory_doc["payload"] = legacy_payload
    memory_doc["peer_digest"] = durability_module._digest(changelog_doc["payload"])
    changelog_doc["peer_digest"] = durability_module._digest(memory_doc["payload"])
    path.write_text(json.dumps(memory_doc), encoding="utf-8")
    (tmp_path / "changelog.json").write_text(json.dumps(changelog_doc), encoding="utf-8")

    LiteMemoryStore(path)
    upgraded = json.loads(path.read_text(encoding="utf-8"))
    assert upgraded["generation"] == prior_generation + 1
    assert upgraded["payload"]["schema_version"] == 2
    assert upgraded["payload"]["sync"] == {
        "tenant_binding": None,
        "next_stream_seq": 1,
        "retention_floor": 0,
        "compacted_through": 0,
        "outbox": [],
        "entity_versions": [],
        "targets": [],
        "deliveries": [],
        "inbox": [],
        "batches": [],
        "cursors": [],
        "inbound_cursors": [],
        "snapshots": [],
        "snapshot_items": [],
    }


def test_recognized_g002_enveloped_v1_is_canonicalized_under_journal_with_peer_fts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.json"
    writer = LiteMemoryStore(path)
    function = _function("legacy")
    function.action = [FieldValue(desc="historic action")]
    writer.add(function, _source())
    writer.add_fact(Fact(id="fact", name="fact", subject="a", predicate="is", object_="b"))
    writer.add_preference(Preference(id="pref", name="pref", aspect="style", preference="brief"))
    writer.add_observation(Observation(id="obs", name="obs", event="seen", context="test"))
    writer.merge(
        GraphData(
            nodes=[],
            edges=[GraphEdge(source="legacy", target=f"edge-{index}", edge_type="REFERENCES") for index in range(128)],
        )
    )
    preopened = LiteMemoryStore(path)
    _set_g002_schema_version_one(path)
    before = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    for raw_function in before["payload"]["functions"]:
        for field in ("tenant_id", "owner_subject_id", "workspace_id", "visibility", "provenance"):
            raw_function.pop(field)
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    changelog["peer_digest"] = durability_module._digest(before["payload"])
    path.write_text(json.dumps(before), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")

    migrated = LiteMemoryStore(path)
    after = json.loads(path.read_text(encoding="utf-8"))
    raw_function = after["payload"]["functions"][0]
    assert after["generation"] == before["generation"] + 1
    assert raw_function["tenant_id"] is None
    assert raw_function["owner_subject_id"] is None
    assert raw_function["workspace_id"] is None
    assert raw_function["visibility"] is None
    assert raw_function["provenance"] == {}
    assert len(after["payload"]["edges"]) == 128
    assert {item.id for item in migrated.list_facts()} == {"fact"}
    assert {item.id for item in migrated.list_preferences()} == {"pref"}
    assert {item.id for item in migrated.list_observations()} == {"obs"}
    assert migrated.get_timeline("legacy")
    assert {item.func_id for item in preopened.fts_search("historic")} == {"legacy"}


def test_recognized_g002_enveloped_v1_mixed_nodes_are_split_and_canonicalized(
    tmp_path: Path,
) -> None:
    path = _make_g002_mixed_fixture(tmp_path)
    preopened = LiteMemoryStore(path)
    _write_g002_mixed_function_collection(path)
    historical = json.loads(path.read_text(encoding="utf-8"))
    generation = historical["generation"]

    migrated = LiteMemoryStore(path)
    after = json.loads(path.read_text(encoding="utf-8"))

    assert after["generation"] == generation + 1
    assert [node["memory_type"] for node in after["payload"]["functions"]] == ["function"]
    assert [node["memory_type"] for node in after["payload"]["facts"]] == ["fact"]
    assert [node["memory_type"] for node in after["payload"]["preferences"]] == ["preference"]
    assert [node["memory_type"] for node in after["payload"]["observations"]] == ["observation"]
    assert "object_" not in after["payload"]["facts"][0]
    assert len(after["payload"]["edges"]) == 1
    assert {node.id for node in migrated.list_functions()} == {"legacy-function"}
    assert {node.id for node in migrated.list_facts()} == {"legacy-fact-0"}
    assert {node.id for node in migrated.list_preferences()} == {"legacy-preference-0"}
    assert {node.id for node in migrated.list_observations()} == {"legacy-observation-0"}
    assert {result.func_id for result in preopened.fts_search("historic mixed")} == {"legacy-function"}


def test_g002_mixed_functions_small_904_57_5_5_equivalent_matrix(tmp_path: Path) -> None:
    """缩小矩阵确保 splitter 不会只处理每种 memory_type 的首条记录。"""
    counts = (4, 3, 2, 2)
    path = _make_g002_mixed_fixture(tmp_path, counts=counts)
    _write_g002_mixed_function_collection(path)

    migrated = LiteMemoryStore(path)
    after = json.loads(path.read_text(encoding="utf-8"))["payload"]

    assert len(after["functions"]) == len(migrated.list_functions()) == counts[0]
    assert len(after["facts"]) == len(migrated.list_facts()) == counts[1]
    assert len(after["preferences"]) == len(migrated.list_preferences()) == counts[2]
    assert len(after["observations"]) == len(migrated.list_observations()) == counts[3]
    assert len(after["edges"]) == counts[0]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["functions"][1].__setitem__("memory_type", "unknown"),
        lambda payload: payload["functions"][1].__setitem__("memory_type", True),
        lambda payload: payload["functions"][1].pop("memory_type"),
        lambda payload: payload["functions"][1].__setitem__("tenant_id", None),
        lambda payload: payload["functions"][0].update(
            {
                "tenant_id": None,
                "owner_subject_id": None,
                "workspace_id": None,
                "visibility": None,
                "provenance": {},
            }
        ),
        lambda payload: payload["functions"][1].__setitem__("future_field", None),
        lambda payload: payload["functions"][1].__setitem__(
            "object", payload["functions"][1].pop("object_")
        ),
    ],
)
def test_unrecognized_g002_mixed_functions_fail_closed_without_adoption(
    tmp_path: Path, mutate
) -> None:
    path = _make_g002_mixed_fixture(tmp_path)
    _write_g002_mixed_function_collection(path)
    changelog_path = tmp_path / "changelog.json"
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    mutate(memory["payload"])
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(tmp_path))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize("duplicate_memory_type", ["function", "fact", "preference", "observation"])
def test_g002_mixed_functions_reject_duplicate_ids_after_collection_split(
    tmp_path: Path, duplicate_memory_type: str
) -> None:
    path = _make_g002_mixed_fixture(tmp_path)
    _write_g002_mixed_function_collection(path)
    changelog_path = tmp_path / "changelog.json"
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    payload = memory["payload"]
    raw = next(node for node in payload["functions"] if node["memory_type"] == duplicate_memory_type)
    payload["functions"].append(deepcopy(raw))
    changelog["peer_digest"] = durability_module._digest(payload)
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(tmp_path))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_g002_mixed_functions_reject_duplicate_edge_key_before_adoption(tmp_path: Path) -> None:
    path = _make_g002_mixed_fixture(tmp_path)
    _write_g002_mixed_function_collection(path)
    changelog_path = tmp_path / "changelog.json"
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["edges"].append(deepcopy(memory["payload"]["edges"][0]))
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(tmp_path))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda function: function.pop("name"),
        lambda function: function.__setitem__("provenance", True),
        lambda function: function.__setitem__("future_identity", None),
    ],
)
def test_unrecognized_enveloped_g002_lookalike_is_not_migrated(tmp_path: Path, mutate) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("legacy"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    function = memory["payload"]["functions"][0]
    for field in ("tenant_id", "owner_subject_id", "workspace_id", "visibility", "provenance"):
        function.pop(field)
    mutate(function)
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize("different", [False, True])
def test_duplicate_current_envelope_function_is_fail_closed_without_overwrite(
    tmp_path: Path, different: bool
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    duplicate = deepcopy(memory["payload"]["functions"][0])
    if different:
        duplicate["name"] = "different payload"
    memory["payload"]["functions"].append(duplicate)
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_duplicate_historical_function_and_journal_base_target_fail_before_adoption(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("legacy"), _source())
    base = store._durability.load_authoritative()
    historical_memory = deepcopy(base.memory)
    for function in historical_memory["functions"]:
        for field in ("tenant_id", "owner_subject_id", "workspace_id", "visibility", "provenance"):
            function.pop(field)
    historical_memory["functions"].append(deepcopy(historical_memory["functions"][0]))
    target = LitePair(historical_memory, deepcopy(base.changelog), base.generation + 1, "duplicate-target")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    changelog_path = tmp_path / "changelog.json"
    memory_before, changelog_before, journal_before = path.read_bytes(), changelog_path.read_bytes(), journal_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.read_bytes() == journal_before


def test_public_merge_rejects_duplicate_node_and_edge_batch_before_commit(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("root"), _source())
    memory_before, changelog_before = path.read_bytes(), (tmp_path / "changelog.json").read_bytes()
    duplicate_node = _function("dup")
    with pytest.raises(ValueError, match="duplicate"):
        store.merge(GraphData(nodes=[duplicate_node, deepcopy(duplicate_node)], edges=[]))
    duplicate_edge = GraphEdge(source="root", target="target", edge_type="REFERENCES")
    with pytest.raises(ValueError, match="duplicate"):
        store.merge(GraphData(nodes=[], edges=[duplicate_edge, deepcopy(duplicate_edge)]))

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before


@pytest.mark.parametrize("collection", ["facts", "preferences", "observations", "edges"])
def test_duplicate_typed_or_edge_identity_in_envelope_is_fail_closed(tmp_path: Path, collection: str) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("root"), _source())
    store.add_fact(Fact(id="fact", name="fact", subject="a", predicate="is", object_="b"))
    store.add_preference(Preference(id="pref", name="pref", aspect="style", preference="brief"))
    store.add_observation(Observation(id="obs", name="obs", event="seen", context="test"))
    store.merge(GraphData(nodes=[], edges=[GraphEdge(source="root", target="target", edge_type="REFERENCES")]))
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"][collection].append(deepcopy(memory["payload"][collection][0]))
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_schema_v2_empty_sync_state_is_required_by_envelope_validator(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["sync"].pop("next_stream_seq")
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)


def test_schema_v2_sync_items_reject_invalid_keys_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["sync"]["outbox"] = [
        {
            "stream_seq": 1,
            "event_id": "evt",
            "origin_node_id": "origin",
            "node_type": "function",
            "entity_key": "legacy",
            "operation": "upsert",
            "version_key": uuid.uuid4().hex,
            "payload": {},
            "visibility": "user",
            "owner_subject_id": None,
            "workspace_id": None,
            "agent_id": None,
            "session_id": None,
            "created_at": "2026-08-11T00:00:00+00:00",
        },
        {
            "stream_seq": 1,
            "event_id": "evt",
            "origin_node_id": "origin",
            "node_type": "function",
            "entity_key": "legacy",
            "operation": "upsert",
            "version_key": uuid.uuid4().hex,
            "payload": {},
            "visibility": "user",
            "owner_subject_id": None,
            "workspace_id": None,
            "agent_id": None,
            "session_id": None,
            "created_at": "2026-08-11T00:00:00+00:00",
        },
    ]
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)


def _write_sync_state(path: Path, sync_state: dict) -> tuple[bytes, bytes]:
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = path.with_name("changelog.json")
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["sync"] = sync_state
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    return path.read_bytes(), changelog_path.read_bytes()


def test_schema_v2_accepts_complete_canonical_nonempty_sync_state(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_tenant_function("seed"), _source())
    _write_sync_state(path, _valid_nonempty_sync_state())

    reopened = LiteMemoryStore(path)

    assert reopened._durability.load_authoritative().memory["sync"] == _valid_nonempty_sync_state()


def test_schema_v2_without_inbound_cursor_namespace_upgrades_atomically_and_keeps_old_pin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_tenant_function("seed"), _source())
    historical = _valid_nonempty_sync_state()
    historical.pop("inbound_cursors")
    _write_sync_state(path, historical)
    before = json.loads(path.read_text(encoding="utf-8"))

    reopened = LiteMemoryStore(path)
    upgraded = reopened._durability.load_authoritative()

    assert upgraded.generation == before["generation"] + 1
    assert upgraded.memory["sync"]["inbound_cursors"] == []
    # Direction was not encoded historically.  Keeping the ambiguous row as
    # an outbound retention pin is conservative and prevents data loss.
    assert upgraded.memory["sync"]["cursors"] == historical["cursors"]


def test_schema_v2_rejects_business_state_outside_sync_tenant_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_tenant_function("seed"), _source())
    sync_state = _valid_nonempty_sync_state()
    memory_before, changelog_before = _write_sync_state(path, sync_state)
    memory = json.loads(memory_before)
    changelog = json.loads(changelog_before)
    memory["payload"]["functions"][0]["tenant_id"] = "tenant-b"
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    (tmp_path / "changelog.json").write_text(json.dumps(changelog), encoding="utf-8")
    corrupted_memory = path.read_bytes()
    corrupted_changelog = (tmp_path / "changelog.json").read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == corrupted_memory
    assert (tmp_path / "changelog.json").read_bytes() == corrupted_changelog


@pytest.mark.parametrize(
    "mutate",
    [
        lambda sync: sync["outbox"][0].__setitem__("stream_seq", True),
        lambda sync: sync["outbox"][0].__setitem__("origin_node_id", True),
        lambda sync: sync["outbox"][0].__setitem__("node_type", True),
        lambda sync: sync["outbox"][0].__setitem__("entity_key", "node:v1:Zm5"),
        lambda sync: sync["outbox"][0].__setitem__("operation", True),
        lambda sync: sync["outbox"][0].__setitem__("tenant_id", True),
        lambda sync: sync.__setitem__("tenant_binding", None),
        lambda sync: sync.__setitem__("tenant_binding", "tenant-b"),
        lambda sync: sync["outbox"][0].__setitem__("created_at", "2026-08-11T00:00:00"),
        lambda sync: sync.__setitem__("next_stream_seq", 1),
        lambda sync: sync["entity_versions"][0].__setitem__("deleted", 1),
        lambda sync: sync["entity_versions"][0].__setitem__("event_id", str(uuid.UUID(int=999))),
        lambda sync: sync["targets"][0].__setitem__("remote_node_id", True),
        lambda sync: sync["targets"][0].__setitem__("future", True),
        lambda sync: sync["deliveries"][0].__setitem__("attempt_count", True),
        lambda sync: sync["deliveries"][0].__setitem__("target_id", "missing"),
        lambda sync: sync["deliveries"][0].__setitem__("lease_owner", "owner"),
        lambda sync: sync["inbox"][0].__setitem__("event_id", "not-a-uuid"),
        lambda sync: sync["batches"][0]["response"].__setitem__("outcome", True),
        lambda sync: sync["cursors"][0].__setitem__("after_seq", True),
        lambda sync: sync.__setitem__("next_stream_seq", 3),
        lambda sync: sync["snapshots"][0].__setitem__("expires_at", "2026-08-12"),
        lambda sync: sync["snapshot_items"][0].__setitem__(
            "snapshot_id", str(uuid.UUID(int=999))
        ),
        lambda sync: sync["snapshot_items"][0]["event"].__setitem__("future", True),
        lambda sync: sync["snapshot_items"][0]["event"]["scope"].__setitem__(
            "tenant_id", "tenant-b"
        ),
    ],
)
def test_schema_v2_rejects_weak_or_cross_collection_sync_state_before_publish(
    tmp_path: Path,
    mutate,
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_tenant_function("seed"), _source())
    sync_state = _valid_nonempty_sync_state()
    mutate(sync_state)
    memory_before, changelog_before = _write_sync_state(path, sync_state)

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("schema_version", True),
        lambda payload: payload.__setitem__("schema_version", 0),
        lambda payload: payload.__setitem__("schema_version", 999),
        lambda payload: payload.pop("schema_version"),
        lambda payload: payload.pop("sync"),
        lambda payload: payload.__setitem__("future_collection", []),
        lambda payload: payload.pop("functions"),
        lambda payload: payload.__setitem__("edges", {}),
    ],
)
def test_envelope_memory_payload_schema_and_collections_fail_closed(
    tmp_path: Path, mutate
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    mutate(memory["payload"])
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize("optional", ["observations", "facts", "preferences"])
def test_v1_envelope_allows_only_documented_optional_collections(tmp_path: Path, optional: str) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory["payload"]["schema_version"] = 1
    memory["payload"].pop("sync")
    memory["payload"].pop(optional)
    changelog["peer_digest"] = durability_module._digest(memory["payload"])
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")

    assert {item.id for item in LiteMemoryStore(path).list_functions()} == {"one"}


def test_legacy_schema_missing_is_only_allowed_for_exact_historical_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    changelog_path = tmp_path / "changelog.json"
    path.write_text(json.dumps({"functions": [], "edges": [], "unknown": []}), encoding="utf-8")
    changelog_path.write_text("[]", encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize(
    ("side", "mutate"),
    [
        ("memory", lambda document: document.__setitem__("future_control", True)),
        ("memory", lambda document: document.pop("peer_digest")),
        ("changelog", lambda document: document.__setitem__("future_control", True)),
        ("changelog", lambda document: document.pop("transaction_id")),
    ],
)
def test_envelope_top_level_requires_exact_key_set(tmp_path: Path, side: str, mutate) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    changelog_path = tmp_path / "changelog.json"
    memory = json.loads(path.read_text(encoding="utf-8"))
    changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
    mutate(memory if side == "memory" else changelog)
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda journal: journal.__setitem__("future_control", True),
        lambda journal: journal.pop("base_changelog"),
        lambda journal: journal["target"].__setitem__("future_control", True),
        lambda journal: journal["target"].pop("transaction_id"),
    ],
)
def test_journal_and_target_require_exact_key_sets_before_forward(tmp_path: Path, mutate) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(deepcopy(base.memory), deepcopy(base.changelog), base.generation + 1, "key-test")
    journal = _journal(base, target)
    mutate(journal)
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()
    journal_before = journal_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.read_bytes() == journal_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda memory: memory["functions"][0].__setitem__("memory_type", True),
        lambda memory: memory["functions"][0].__setitem__("source_type", "future-source"),
        lambda memory: memory["functions"][0].__setitem__("source_paragraphs", "xy"),
        lambda memory: memory["functions"][0].__setitem__("provenance", [["k", "v"]]),
        lambda memory: memory["functions"][0].__setitem__("attributes", [["k", "v"]]),
        lambda memory: memory["functions"][0].__setitem__("cross_references", {}),
        lambda memory: memory["functions"][0].__setitem__("priority_from_source", True),
        lambda memory: memory["functions"][0]["action"][0].__setitem__("sources", "xy"),
        lambda memory: memory["facts"][0].__setitem__("memory_type", "function"),
        lambda memory: memory["facts"][0].__setitem__("namespace", [["k", "v"]]),
        lambda memory: memory["preferences"][0].__setitem__("source_type", True),
        lambda memory: memory["observations"][0].__setitem__("memory_type", "fact"),
        lambda memory: memory["observations"][0].__setitem__("category", "bogus"),
        lambda memory: memory["edges"][0].__setitem__("evidence", [True]),
    ],
)
def test_raw_model_discriminators_and_collections_validate_before_from_dict(
    tmp_path: Path, mutate
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    function = _function("function")
    function.action = [FieldValue(desc="action")]
    store.add(function, _source())
    store.add_fact(Fact(id="fact", name="fact", subject="a", predicate="is", object_="b"))
    store.add_preference(Preference(id="pref", name="pref", aspect="style", preference="brief"))
    store.add_observation(Observation(id="obs", name="obs", event="seen", context="test"))
    store.merge(GraphData(nodes=[], edges=[GraphEdge(source="function", target="fact", edge_type="REFERENCES")]))
    base = store._durability.load_authoritative()
    target_memory = deepcopy(base.memory)
    mutate(target_memory)
    target = LitePair(target_memory, deepcopy(base.changelog), base.generation + 1, "raw-schema")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.exists()


def test_public_mutation_validates_model_discriminators_before_commit(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    good = _function("good")
    good.source_type = SourceType.CODE
    store.add(good, _source())
    assert LiteMemoryStore(path).get("good").source_type is SourceType.CODE
    memory_before = path.read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()
    bad = _function("bad")
    bad.memory_type = "fact"
    bad.source_type = "future-source"

    with pytest.raises(LiteStorageIntegrityError):
        store.add(bad, _source())

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before


def test_provenance_nested_members_fail_closed_for_journal_target_base_and_pair(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    pristine = store._durability.load_authoritative()
    changelog_path = tmp_path / "changelog.json"
    for location in ("target", "base", "pair"):
        target = LitePair(deepcopy(pristine.memory), deepcopy(pristine.changelog), pristine.generation + 1, f"prov-{location}")
        if location == "pair":
            memory = json.loads(path.read_text(encoding="utf-8"))
            changelog = json.loads(changelog_path.read_text(encoding="utf-8"))
            memory["payload"]["functions"][0]["provenance"] = {"actor": True}
            changelog["peer_digest"] = durability_module._digest(memory["payload"])
            path.write_text(json.dumps(memory), encoding="utf-8")
            changelog_path.write_text(json.dumps(changelog), encoding="utf-8")
            memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()
            with pytest.raises(LiteStorageIntegrityError):
                LiteMemoryStore(path)
            with pytest.raises(LiteStorageIntegrityError):
                create_store("lite", path=str(path.parent))
            assert path.read_bytes() == memory_before
            assert changelog_path.read_bytes() == changelog_before
            break
        journal = _journal(pristine, target)
        if location == "target":
            journal["target"]["memory"]["functions"][0]["provenance"] = {"actor": True}
            bad_target = LitePair(
                journal["target"]["memory"], journal["target"]["changelog"],
                target.generation, target.transaction_id,
            )
            journal["target_record"] = _pair_record(bad_target)
        else:
            journal["base_memory"]["functions"][0]["provenance"] = {"actor": True}
            bad_base = LitePair(
                journal["base_memory"], journal["base_changelog"],
                pristine.generation, pristine.transaction_id,
            )
            journal["base_record"] = _pair_record(bad_base)
        journal_path = tmp_path / "memory.journal.json"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        memory_before, changelog_before, journal_before = (
            path.read_bytes(), changelog_path.read_bytes(), journal_path.read_bytes()
        )
        with pytest.raises(LiteStorageIntegrityError):
            LiteMemoryStore(path)
        assert path.read_bytes() == memory_before
        assert changelog_path.read_bytes() == changelog_before
        assert journal_path.read_bytes() == journal_before
        journal_path.unlink()


@pytest.mark.parametrize("provenance", [{"actor": True}, {1: "actor"}])
def test_public_mutation_rejects_non_string_provenance_member_before_decision(
    tmp_path: Path, provenance
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("good"), _source())
    memory_before, changelog_before = path.read_bytes(), (tmp_path / "changelog.json").read_bytes()
    bad = _function("bad")
    bad.provenance = provenance

    with pytest.raises(LiteStorageIntegrityError):
        store.add(bad, _source())

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before


@pytest.mark.parametrize(
    "memory",
    [
        {
            "schema_version": 1,
            "functions": [
                {
                    "id": "bad",
                    "name": "bad",
                    "action": [{"desc": "bad", "created_at": "not-a-datetime"}],
                }
            ],
            "edges": [],
        },
        {
            "schema_version": 1,
            "functions": [{"id": "root", "name": "root"}],
            "edges": [
                {
                    "source": "root",
                    "target": "root",
                    "edge_type": "REFERENCES",
                    "created_at": "not-a-datetime",
                }
            ],
        },
    ],
)
def test_invalid_legacy_pair_is_never_upgraded_before_model_validation(tmp_path: Path, memory) -> None:
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(memory), encoding="utf-8")
    changelog_path = tmp_path / "changelog.json"
    changelog_path.write_text("[]", encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_before = changelog_path.read_bytes()
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))
    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_peer_commit_is_observed_by_preopened_reader_and_fts(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    old = LiteMemoryStore(path)
    peer = LiteMemoryStore(path)
    peer.add(_function("peer"), _source())
    assert old.get("peer") is not None
    assert {item.func_id for item in old.fts_search("peer")} == {"peer"}


def test_returned_object_and_input_are_detached(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    incoming = _function("one")
    store.add(incoming, _source())
    incoming.name = "forged-input"
    returned = store.get("one")
    assert returned is not None
    returned.name = "forged-read"
    assert LiteMemoryStore(tmp_path / "memory.json").get("one").name == "one"


def test_nested_input_and_all_public_read_outputs_are_detached(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    source = _source()
    function = _function("root")
    function.action = [FieldValue(desc="act", sources=["source"], source_method="test")]
    function.attributes = {"nested": {"keep": True}}
    store.add(function, source)
    function.action[0].sources.append("forged")
    function.attributes["nested"]["keep"] = False
    child = _function("child")
    edge = GraphEdge(source="root", target="child", edge_type="REFERENCES", evidence=["proof"])
    graph = GraphData(nodes=[child], edges=[edge])
    store.merge(graph)
    child.name = "forged-child"
    edge.evidence.append("forged")
    observation = Observation(id="obs", name="obs", event="event", context="ctx")
    fact = Fact(id="fact", name="fact", subject="s", predicate="is", object_="o")
    preference = Preference(id="pref", name="pref", aspect="a", preference="p")
    store.add_observation(observation)
    store.add_fact(fact)
    store.add_preference(preference)
    observation.context = "forged"
    fact.object_ = "forged"
    preference.preference = "forged"

    snapshot = store.get_graph()
    snapshot.nodes[0].attributes["nested"]["keep"] = False
    snapshot.edges[0].evidence.append("forged-read")
    neighbors = store.get_neighbors("root")
    neighbors[0].name = "forged-neighbor"
    store.get_fact("fact").object_ = "forged-read"
    store.get_preference("pref").preference = "forged-read"
    store.list_observations()[0].context = "forged-read"
    store.get_timeline("root")[0].description = "forged-read"

    reopened = LiteMemoryStore(tmp_path / "memory.json")
    root = reopened.get("root")
    assert root.action[0].sources == ["source"]
    assert root.attributes == {"nested": {"keep": True}}
    assert reopened.get("child").name == "child"
    assert reopened.get_graph().edges[0].evidence == ["proof"]
    assert reopened.get_fact("fact").object_ == "o"
    assert reopened.get_preference("pref").preference == "p"
    assert reopened.list_observations()[0].context == "ctx"
    assert reopened.get_timeline("root")[0].description != "forged-read"


def test_mixed_annotation_is_one_pair_commit_or_zero_commit(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    store.add(_function("func"), _source())
    store.add_fact(Fact(id="fact", name="fact", subject="s", predicate="is", object_="o"))
    store.add_preference(Preference(id="pref", name="pref", aspect="a", preference="p"))
    before = store.generation
    updated = store.annotate_nodes(
        ["func", "fact", "pref"], attributes={"memplex_tag": "yes", "plain": "function-only"}, needs_review=True
    )
    assert {item.id for item in updated} == {"func", "fact", "pref"}
    assert store.generation == before + 1
    assert store.get("func").attributes["plain"] == "function-only"
    assert store.get_fact("fact").namespace == {"memplex_tag": "yes"}
    before = store.generation
    with pytest.raises(KeyError):
        store.annotate_nodes(["func", "missing"], needs_review=False)
    assert store.generation == before
    assert store.get("func").needs_review is True


def test_two_preopened_processes_preserve_both_adds(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    ready_a, ready_b, start = ctx.Event(), ctx.Event(), ctx.Event()
    results = ctx.Queue()
    workers = [
        ctx.Process(target=_preopen_then_add, args=(str(tmp_path), identifier, ready, start, results))
        for identifier, ready in (("a", ready_a), ("b", ready_b))
    ]
    for worker in workers:
        worker.start()
    assert ready_a.wait(10) and ready_b.wait(10)
    start.set()
    for worker in workers:
        worker.join(15)
    assert [results.get(timeout=2) for _ in workers] == ["ok", "ok"]
    assert {item.id for item in LiteMemoryStore(tmp_path / "memory.json").list_functions()} == {"a", "b"}


def test_two_preopened_processes_add_and_merge_preserve_both_nodes(tmp_path: Path) -> None:
    assert _run_two_actions(tmp_path, "add", "merge") == ["ok", "ok"]
    assert {item.id for item in LiteMemoryStore(tmp_path / "memory.json").list_functions()} == {
        "add",
        "merge",
    }


def test_two_preopened_processes_access_is_exactly_additive(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    store.add(_function("counter"), _source())
    assert _run_two_actions(tmp_path, "access", "access") == ["ok", "ok"]
    assert LiteMemoryStore(tmp_path / "memory.json").get("counter").access_count == 2


def test_two_preopened_processes_clear_and_add_is_a_legal_serial_order(tmp_path: Path) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    store.add(_function("old"), _source())
    assert _run_two_actions(tmp_path, "clear", "add") == ["ok", "ok"]
    ids = {item.id for item in LiteMemoryStore(tmp_path / "memory.json").list_functions()}
    assert ids in (set(), {"add"})


def test_two_preopened_processes_typed_writes_preserve_both_nodes(tmp_path: Path) -> None:
    assert _run_two_actions(tmp_path, "typed-fact", "typed-pref") == ["ok", "ok"]
    reopened = LiteMemoryStore(tmp_path / "memory.json")
    assert reopened.get_fact("fact") is not None
    assert reopened.get_preference("pref") is not None


def test_compaction_snapshot_generation_rejects_peer_write_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    compactor = LiteMemoryStore(path)
    compactor.add(_function("snapshot"), _source())
    generation, functions = compactor.compaction_snapshot()
    peer = LiteMemoryStore(path)
    peer.add(_function("peer"), _source())
    with pytest.raises(LiteStorageIntegrityError, match="stale"):
        compactor.apply_compaction(
            replacements=functions,
            delete_ids=[],
            expected_generation=generation,
        )
    assert LiteMemoryStore(path).get("peer") is not None


def test_single_sided_pair_and_corrupt_journal_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    (tmp_path / "changelog.json").unlink()
    with pytest.raises(LiteStorageIntegrityError, match="incomplete"):
        LiteMemoryStore(path)

    store = LiteMemoryStore(tmp_path / "other.json")
    store.add(_function("two"), _source())
    (tmp_path / "other.journal.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="invalid"):
        LiteMemoryStore(tmp_path / "other.json")


def test_stale_foreign_and_tampered_journals_cannot_replace_pair(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("live"), _source())
    base = LitePair({"schema_version": 1, "functions": [], "edges": []}, [], 0, "base")
    target = LitePair({"schema_version": 1, "functions": [], "edges": []}, [], 1, "target")
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="stale or foreign"):
        LiteMemoryStore(path)
    (tmp_path / "memory.journal.json").unlink()
    assert store.get("live") is not None

    journal["target_record"]["cross_digest"] = "tampered"
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="invalid"):
        LiteMemoryStore(path)


def test_valid_journal_cannot_overwrite_tampered_single_side_payload(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target_memory = dict(base.memory)
    target_memory["functions"] = list(base.memory["functions"]) + [_function("target").to_dict()]
    target = LitePair(target_memory, base.changelog, base.generation + 1, "target")
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    memory_doc = json.loads(path.read_text(encoding="utf-8"))
    memory_doc["payload"]["functions"][0]["name"] = "tampered"
    path.write_text(json.dumps(memory_doc), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="stale or foreign"):
        LiteMemoryStore(path)


@pytest.mark.parametrize("version", [None, True, False, 0, 1, 3, 999])
def test_journal_format_version_requires_exact_current_int_and_preserves_pair(tmp_path: Path, version) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(base.memory, base.changelog, base.generation + 1, "target")
    journal = {
        "format_version": version,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("format_version", True),
        ("format_version", 999),
        ("generation", True),
        ("generation", -1),
        ("generation", "1"),
        ("transaction_id", True),
        ("transaction_id", ""),
        ("transaction_id", 1),
    ],
)
def test_envelope_metadata_rejects_weak_types_and_future_versions(
    tmp_path: Path, field: str, bad_value
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("one"), _source())
    memory_doc = json.loads(path.read_text(encoding="utf-8"))
    changelog_doc = json.loads((tmp_path / "changelog.json").read_text(encoding="utf-8"))
    memory_doc[field] = bad_value
    path.write_text(json.dumps(memory_doc), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    # The unchanged peer side never makes a weakly typed memory envelope
    # authoritative, and a fresh valid pair remains recoverable once this
    # deliberate corruption is removed by an operator.
    assert changelog_doc["format_version"] == 2


@pytest.mark.parametrize(
    "mutate",
    [
        lambda journal: journal["target_record"].__setitem__("generation", True),
        lambda journal: journal["target_record"].__setitem__("generation", 1.0),
        lambda journal: journal["target_record"].__setitem__("extra", "x"),
        lambda journal: journal["target_record"].pop("cross_digest"),
        lambda journal: journal["base_record"].__setitem__("memory_digest", "not-a-digest"),
    ],
)
def test_journal_record_schema_rejects_bool_numeric_equivalence_and_key_drift(
    tmp_path: Path, mutate
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(base.memory, base.changelog, base.generation + 1, "target")
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    mutate(journal)
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_journal_side_matching_rejects_json_numeric_equivalence_payload_tamper(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(base.memory, base.changelog, base.generation + 1, "target")
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    memory_doc = json.loads(path.read_text(encoding="utf-8"))
    memory_doc["payload"]["functions"][0]["access_count"] = 0.0
    path.write_text(json.dumps(memory_doc), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError, match="stale or foreign"):
        LiteMemoryStore(path)


@pytest.mark.parametrize("generation", [True, -1, 0, 1, 3, 999])
def test_journal_transition_requires_exact_next_generation(tmp_path: Path, generation) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(base.memory, base.changelog, generation, "next")
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    memory_before = path.read_bytes()
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    assert path.read_bytes() == memory_before


def test_journal_transition_rejects_reused_transaction_id(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("base"), _source())
    base = store._durability.load_authoritative()
    target = LitePair(base.memory, base.changelog, base.generation + 1, base.transaction_id)
    journal = {
        "format_version": 2,
        "base_record": _pair_record(base),
        "base_memory": base.memory,
        "base_changelog": base.changelog,
        "target": {
            "memory": target.memory,
            "changelog": target.changelog,
            "generation": target.generation,
            "transaction_id": target.transaction_id,
        },
        "target_record": _pair_record(target),
    }
    (tmp_path / "memory.journal.json").write_text(json.dumps(journal), encoding="utf-8")
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)


def test_invalid_model_target_is_rejected_before_journal_and_restores_base(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("old"), _source())
    memory_before = path.read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()
    candidate = _function("bad")
    candidate.action = [FieldValue(desc="bad", created_at="not-a-datetime")]
    with pytest.raises(LiteStorageIntegrityError):
        store.add(candidate, _source())
    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before
    assert {node.id for node in store.list_functions()} == {"old"}
    assert {node.id for node in LiteMemoryStore(path).list_functions()} == {"old"}


@pytest.mark.parametrize("bad_number", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_target_numbers_are_rejected_before_durable_decision(
    tmp_path: Path, bad_number: float
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("old"), _source())
    memory_before = path.read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()
    candidate = _function("nonfinite")
    candidate.action = [FieldValue(desc="bad number", weight=bad_number)]

    with pytest.raises(LiteStorageIntegrityError):
        store.add(candidate, _source())

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before


def test_invalid_graph_edge_target_is_rejected_before_journal(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("root"), _source())
    memory_before = path.read_bytes()
    edge = GraphEdge(source="root", target="child", edge_type="REFERENCES", created_at="not-a-datetime")
    with pytest.raises(LiteStorageIntegrityError):
        store.merge(GraphData(nodes=[_function("child")], edges=[edge]))
    assert path.read_bytes() == memory_before
    assert store.get("child") is None


def test_invalid_authoritative_envelope_is_integrity_error_and_factory_never_falls_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("good"), _source())
    memory_doc = json.loads(path.read_text(encoding="utf-8"))
    changelog_path = tmp_path / "changelog.json"
    changelog_doc = json.loads(changelog_path.read_text(encoding="utf-8"))
    memory_doc["payload"]["functions"][0]["action"] = [
        {"desc": "bad", "created_at": "not-a-datetime"}
    ]
    # Keep the cross-digest envelope structurally valid: the failure must be
    # semantic model validation, not a weaker envelope mismatch.
    changelog_doc["peer_digest"] = durability_module._digest(memory_doc["payload"])
    path.write_text(json.dumps(memory_doc), encoding="utf-8")
    changelog_path.write_text(json.dumps(changelog_doc), encoding="utf-8")
    memory_before, changelog_before = path.read_bytes(), changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_factory_explicit_lite_path_never_falls_back_after_constructor_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """显式 Lite 路径的任意构造异常必须原样终止，不能触及默认库。"""
    import memplex.storage as storage_module

    calls: list[Path | None] = []

    class SentinelStore:
        def __init__(self, path: Path | None = None) -> None:
            calls.append(path)
            if path is None:
                raise AssertionError("不得访问默认 ~/.memplex")
            raise OSError("模拟配置目录不可用")

    monkeypatch.setattr(storage_module, "LiteMemoryStore", SentinelStore)
    configured_file = tmp_path / "memory.json"

    with pytest.raises(OSError, match="模拟配置目录不可用"):
        storage_module.create_store("lite", path=str(configured_file))

    assert calls == [configured_file / "memory.json"]


def test_factory_integrity_error_uses_configured_directory_without_default_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """损坏的已配置 pair 必须原样抛完整性错误，且只能打开该临时目录。"""
    import memplex.storage as storage_module

    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("good"), _source())
    path.write_text("{not-json", encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()

    original_store = storage_module.LiteMemoryStore
    calls: list[Path | None] = []

    def guarded_store(path: Path | None = None) -> LiteMemoryStore:
        calls.append(path)
        if path is None:
            raise AssertionError("完整性失败不得访问默认 ~/.memplex")
        return original_store(path)

    monkeypatch.setattr(storage_module, "LiteMemoryStore", guarded_store)

    with pytest.raises(LiteStorageIntegrityError):
        storage_module.create_store("lite", path=str(tmp_path))

    assert calls == [tmp_path / "memory.json"]
    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before


def test_semantically_invalid_journal_target_never_forwards_or_unlinks_journal(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("good"), _source())
    base = store._durability.load_authoritative()
    target_memory = deepcopy(base.memory)
    target_memory["functions"][0]["action"] = [
        {"desc": "bad", "created_at": "not-a-datetime"}
    ]
    target = LitePair(target_memory, deepcopy(base.changelog), base.generation + 1, "semantic-bad")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()
    journal_before = journal_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.read_bytes() == journal_before


@pytest.mark.parametrize("kind", ["fact", "preference", "observation"])
def test_malformed_typed_journal_target_never_forwards(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    if kind == "fact":
        store.add_fact(Fact(id="fact", name="fact", subject="a", predicate="is", object_="b"))
        collection = "facts"
    elif kind == "preference":
        store.add_preference(Preference(id="pref", name="pref", aspect="style", preference="brief"))
        collection = "preferences"
    else:
        store.add_observation(Observation(id="obs", name="obs", event="seen", context="test"))
        collection = "observations"
    base = store._durability.load_authoritative()
    target_memory = deepcopy(base.memory)
    # ``MemoryNode._base_from_dict`` must construct provenance as a mapping.
    target_memory[collection][0]["provenance"] = "not-a-mapping"
    target = LitePair(target_memory, deepcopy(base.changelog), base.generation + 1, f"bad-{kind}")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.__setitem__("timestamp", True),
        lambda event: event.__setitem__("timestamp", 1),
        lambda event: event.__setitem__("timestamp", "not-an-iso-date"),
        lambda event: event.__setitem__("func_id", True),
        lambda event: event.__setitem__("event_type", 1),
        lambda event: event.__setitem__("description", False),
        lambda event: event.pop("source"),
        lambda event: event.__setitem__("extra", "forbidden"),
    ],
)
def test_changelog_journal_target_requires_exact_event_schema_before_forward(
    tmp_path: Path, mutate
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("good"), _source())
    base = store._durability.load_authoritative()
    target_changelog = deepcopy(base.changelog)
    target_changelog.append(
        {
            "func_id": "good",
            "timestamp": "2026-08-10T00:00:00+00:00",
            "event_type": "updated",
            "description": "bad event",
            "source": "test",
            "actor": "system",
        }
    )
    mutate(target_changelog[-1])
    target = LitePair(deepcopy(base.memory), target_changelog, base.generation + 1, "bad-event")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_path = tmp_path / "changelog.json"
    changelog_before = changelog_path.read_bytes()
    journal_before = journal_path.read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)
    with pytest.raises(LiteStorageIntegrityError):
        create_store("lite", path=str(path.parent))

    assert path.read_bytes() == memory_before
    assert changelog_path.read_bytes() == changelog_before
    assert journal_path.read_bytes() == journal_before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda memory: memory["functions"][0].__setitem__("name", True),
        lambda memory: memory["functions"][0]["action"][0].__setitem__("desc", True),
        lambda memory: memory["facts"][0].__setitem__("subject", True),
        lambda memory: memory["preferences"][0].__setitem__("aspect", True),
        lambda memory: memory["observations"][0].__setitem__("event", True),
        lambda memory: memory["edges"][0].__setitem__("source", True),
    ],
)
def test_read_or_search_critical_weak_model_types_fail_before_journal_forward(
    tmp_path: Path, mutate
) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    function = _function("function")
    function.action = [FieldValue(desc="action")]
    store.add(function, _source())
    store.add_fact(Fact(id="fact", name="fact", subject="a", predicate="is", object_="b"))
    store.add_preference(Preference(id="pref", name="pref", aspect="style", preference="brief"))
    store.add_observation(Observation(id="obs", name="obs", event="seen", context="test"))
    store.merge(GraphData(nodes=[], edges=[GraphEdge(source="function", target="fact", edge_type="REFERENCES")]))
    base = store._durability.load_authoritative()
    target_memory = deepcopy(base.memory)
    mutate(target_memory)
    target = LitePair(target_memory, deepcopy(base.changelog), base.generation + 1, "weak-model")
    journal_path = tmp_path / "memory.journal.json"
    journal_path.write_text(json.dumps(_journal(base, target)), encoding="utf-8")
    memory_before = path.read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()

    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(path)

    assert path.read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before
    assert journal_path.exists()


def _fts_rows(path: Path) -> set[tuple[str, str, str]]:
    conn = sqlite3.connect(path)
    try:
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute("SELECT func_id, name, body FROM memplex_fts")
        }
    finally:
        conn.close()


@pytest.mark.parametrize("operation", ["delete", "clear", "replace"])
def test_fts_sidecar_converges_after_peer_removal_or_replacement(tmp_path: Path, operation: str) -> None:
    path = tmp_path / "memory.json"
    old = LiteMemoryStore(path)
    peer = LiteMemoryStore(path)
    peer.add(_function("dead"), _source())
    assert {item.func_id for item in old.fts_search("dead")} == {"dead"}
    fts_path = tmp_path / "memory.json.fts5.db"
    assert {row[0] for row in _fts_rows(fts_path)} == {"dead"}
    if operation == "delete":
        peer.delete("dead")
    elif operation == "clear":
        peer.clear()
    else:
        replacement = peer.get("dead")
        replacement.name = "live"
        replacement.name_normalized = "live"
        peer.replace_function(replacement)
    # A new process has no in-memory FTS signatures.  It must reconcile a
    # stale persistent sidecar even when the current pair is completely empty.
    fresh = LiteMemoryStore(path)
    fresh.fts_search("live" if operation == "replace" else "dead")
    rows = _fts_rows(fts_path)
    ids = {row[0] for row in rows}
    assert "dead" not in ids or operation == "replace"
    if operation == "replace":
        assert ids == {"dead"}
        assert {row[1] for row in rows} == {"live"}
        assert all("dead" not in row[2] for row in rows)
    else:
        assert ids == set()


def test_corrupt_fts_sidecar_is_rebuilt_from_committed_pair(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    store = LiteMemoryStore(path)
    store.add(_function("healthy"), _source())
    store.fts_search("healthy")
    fts_path = tmp_path / "memory.json.fts5.db"
    fts_path.write_bytes(b"not sqlite")
    assert {item.func_id for item in store.fts_search("healthy")} == {"healthy"}
    assert {row[0] for row in _fts_rows(fts_path)} == {"healthy"}


def test_post_decision_failure_recovers_same_instance_for_read_then_write(tmp_path: Path, monkeypatch) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    store.add(_function("old"), _source())

    def cut() -> None:
        raise OSError("cut")

    monkeypatch.setattr(store._durability, "after_memory_replace", cut)
    with pytest.raises(OSError, match="cut"):
        store.add(_function("new"), _source())
    assert store.get("new") is not None
    monkeypatch.setattr(store._durability, "after_memory_replace", lambda: None)
    store.add(_function("later"), _source())
    assert {node.id for node in store.list_functions()} == {"old", "new", "later"}


def test_ambiguous_journal_directory_fsync_poison_current_instance(tmp_path: Path, monkeypatch) -> None:
    store = LiteMemoryStore(tmp_path / "memory.json")
    store.add(_function("old"), _source())
    monkeypatch.setattr(durability_module, "_fsync_dir", lambda _path: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError, match="fsync"):
        store.add(_function("new"), _source())
    with pytest.raises(LiteStorageIntegrityError, match="poisoned"):
        store.get("old")


def test_factory_propagates_lite_integrity_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "memplex.storage.lite.durability._load_fcntl",
        lambda: (_ for _ in ()).throw(ImportError("missing")),
    )
    with pytest.raises(LiteStorageIntegrityError, match="POSIX flock"):
        create_store("lite", path=str(tmp_path))
