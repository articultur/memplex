"""Pure typed-write plans preserve the existing temporal/capture rules."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from datetime import datetime
from importlib import import_module

import pytest

from memplex import temporal
from memplex.capture_identity import capture_scopes_match
from memplex.models import Fact, Preference

STAMP = "2026-10-08T09:00:00+00:00"


@pytest.fixture(autouse=True)
def _fixed_temporal_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromisoformat(STAMP)
            return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)

    monkeypatch.setattr(temporal, "datetime", FixedDatetime)


def _fact(**overrides) -> Fact:
    values = {
        "id": "fact-old",
        "subject": "database",
        "predicate": "is",
        "object_": "mysql",
        "tenant_id": "tenant",
        "owner_subject_id": "alice",
        "workspace_id": "workspace",
        "visibility": "workspace",
    }
    values.update(overrides)
    return Fact(**values)


def _interfaces():
    # Import within the test call so RED is an interface-missing failure,
    # rather than a dependency or test-collection error.
    from memplex.service import _plan_typed_writes

    return import_module("memplex.storage.typed_batch"), _plan_typed_writes


def test_plan_keeps_supersession_fact_preference_order() -> None:
    contract, planner = _interfaces()
    snapshot = contract.TypedBatchSnapshot(generation=7, facts=(_fact(),))
    inputs = contract.TypedBatchInput(
        facts=(
            _fact(id="fact-new-1", object_="postgres"),
            _fact(id="fact-new-2", object_="sqlite"),
        ),
        preferences=(Preference(id="pref-dark", aspect="theme", preference="dark"),),
    )

    plan = planner(snapshot, inputs, supersede=True)

    assert [op.purpose for op in plan.operations] == ["supersede", "input", "input", "input"]
    assert [op.input_index for op in plan.operations] == [None, 0, 1, 2]
    assert [op.node.id for op in plan.operations] == [
        "fact-old", "fact-new-1", "fact-new-2", "pref-dark",
    ]
    assert snapshot.facts[0].invalid_at is None
    assert inputs.facts[0].valid_from is None
    assert inputs.facts[1].valid_from is None
    assert plan.operations[0].node.invalid_at == STAMP
    assert plan.fact_valid_from == ((0, STAMP), (1, STAMP))


@pytest.mark.parametrize(
    ("old_values", "new_values", "supersede"),
    [
        ({}, {}, True),
        ({}, {}, False),
        ({}, {"id": "fact-old"}, True),
        (
            {"id": "fact_capture_v1_old"},
            {"id": "fact_capture_v1_new", "workspace_id": "other-workspace"},
            True,
        ),
        ({}, {"provenance": {"capture_role": "assistant"}}, False),
        (
            {},
            {"object_": "mysql", "provenance": {"capture_input": "factual_capture_v1"}},
            True,
        ),
        ({}, {"valid_from": "2026-09-01T00:00:00+00:00"}, True),
        ({"invalid_at": "2026-09-01T00:00:00+00:00"}, {}, True),
        ({"valid_until": "2026-10-01T00:00:00+00:00"}, {}, True),
        ({"valid_from": "2026-11-01T00:00:00+00:00"}, {}, True),
        ({}, {"object_": "mysql"}, True),
        ({}, {"valid_from": ""}, True),
        ({"subject": "cache"}, {}, True),
    ],
    ids=[
        "ordinary-contradiction", "supersede-disabled", "same-id", "cross-capture-scope",
        "assistant-caller-disables-supersession", "factual-capture-same-object",
        "existing-valid-from", "already-invalid", "expired", "not-yet-valid",
        "ordinary-same-object-still-supersedes", "empty-valid-from", "different-slot",
    ],
)
def test_plan_preserves_existing_temporal_rules(old_values, new_values, supersede) -> None:
    contract, planner = _interfaces()
    old = _fact(**old_values)
    new = _fact(**({"id": "fact-new", "object_": "postgres"} | new_values))
    snapshot = contract.TypedBatchSnapshot(generation=11, facts=(old,))
    inputs = contract.TypedBatchInput(facts=(new,), preferences=())
    original_old, original_new = deepcopy(old), deepcopy(new)
    expected_old, expected_new = deepcopy(old), deepcopy(new)
    expected_superseded = []
    supplements = ()
    if supersede:
        if not expected_new.valid_from:
            expected_new.valid_from = temporal.now_iso()
            supplements = ((0, expected_new.valid_from),)
        eligible = (
            fact for fact in (expected_old,)
            if capture_scopes_match(expected_new, fact)
            and not (
                expected_new.provenance.get("capture_input") == "factual_capture_v1"
                and expected_new.object_ == fact.object_
            )
        )
        expected_superseded = temporal.supersede_contradicted(expected_new, eligible)

    plan = planner(snapshot, inputs, supersede=supersede)

    assert [op.node for op in plan.operations] == [*expected_superseded, expected_new]
    assert [op.purpose for op in plan.operations] == [
        *("supersede" for _ in expected_superseded), "input",
    ]
    assert [op.input_index for op in plan.operations] == [
        *(None for _ in expected_superseded), 0,
    ]
    assert plan.fact_valid_from == supplements
    assert old == original_old
    assert new == original_new


def test_plan_nodes_are_deeply_detached() -> None:
    contract, planner = _interfaces()
    old = _fact(provenance={"old": "evidence"}, source_paragraphs=["raw-old"])
    new = _fact(
        id="fact-new", object_="postgres", provenance={"new": "evidence"},
        source_paragraphs=["raw-new"], namespace={"tenant": "tenant"},
    )
    preference = Preference(
        id="pref-dark", provenance={"preference": "evidence"}, source_paragraphs=["raw-pref"],
    )
    snapshot = contract.TypedBatchSnapshot(generation=5, facts=(old,))
    inputs = contract.TypedBatchInput(facts=(new,), preferences=(preference,))

    plan = planner(snapshot, inputs, supersede=True)

    for operation, original in zip(plan.operations, (old, new, preference), strict=True):
        assert operation.node is not original
        operation.node.provenance["planned"] = "changed"
        operation.node.source_paragraphs.append("planned")
        assert "planned" not in original.provenance
        assert "planned" not in original.source_paragraphs
        original.provenance["caller"] = "changed"
        original.source_paragraphs.append("caller")
        assert "caller" not in operation.node.provenance
        assert "caller" not in operation.node.source_paragraphs
    plan.operations[1].node.namespace["tenant"] = "planned"
    assert new.namespace == {"tenant": "tenant"}


def test_plan_preserves_duplicate_ids_and_does_not_supersede_incoming_facts() -> None:
    contract, planner = _interfaces()
    inputs = contract.TypedBatchInput(
        facts=(_fact(id="same", object_="mysql"), _fact(id="same", object_="postgres")),
        preferences=(
            Preference(id="same", preference="first"), Preference(id="same", preference="last"),
        ),
    )

    plan = planner(contract.TypedBatchSnapshot(generation=1, facts=()), inputs, supersede=True)

    assert [op.input_index for op in plan.operations] == [0, 1, 2, 3]
    assert [op.purpose for op in plan.operations] == ["input"] * 4
    assert [op.node.id for op in plan.operations] == ["same"] * 4
    assert [op.node.object_ for op in plan.operations[:2]] == ["mysql", "postgres"]
    assert [op.node.preference for op in plan.operations[2:]] == ["first", "last"]


@pytest.mark.parametrize("preference_count", [0, 2], ids=["empty", "preferences-only"])
def test_plan_handles_no_incoming_facts(preference_count: int) -> None:
    contract, planner = _interfaces()
    preferences = tuple(Preference(id=f"pref-{index}") for index in range(preference_count))
    inputs = contract.TypedBatchInput(facts=(), preferences=preferences)

    plan = planner(
        contract.TypedBatchSnapshot(generation=0, facts=(_fact(),)), inputs, supersede=True,
    )

    assert [op.node for op in plan.operations] == list(preferences)
    assert [op.input_index for op in plan.operations] == list(range(preference_count))
    assert [op.purpose for op in plan.operations] == ["input"] * preference_count
    assert plan.fact_valid_from == ()


@pytest.mark.parametrize(
    ("name", "values"),
    [
        ("TypedBatchInput", {"facts": (), "preferences": ()}),
        ("TypedBatchSnapshot", {"generation": 0, "facts": ()}),
        ("TypedWriteOperation", {"node": Fact(id="input"), "input_index": 0, "purpose": "input"}),
        ("TypedWritePlan", {"operations": (), "fact_valid_from": ()}),
        (
            "TypedWriteRejection",
            {"operation_index": 0, "input_index": None, "memory_id": "old", "code": "rejected"},
        ),
        (
            "TypedBatchResult",
            {
                "supported": False, "committed_generation": None, "transaction_id": None,
                "successful_input_indices": (), "committed_superseded_ids": (),
                "rejected": (), "fact_valid_from": (),
            },
        ),
    ],
)
def test_typed_batch_values_have_exact_frozen_fields(name, values) -> None:
    contract, _planner = _interfaces()
    value = getattr(contract, name)(**values)

    assert [field.name for field in fields(value)] == list(values)
    with pytest.raises(FrozenInstanceError):
        setattr(value, next(iter(values)), None)


@pytest.mark.parametrize("factual_capture", [False, True], ids=["ordinary", "factual-capture"])
def test_shared_supersession_candidates_preserve_order_and_capture_rules(factual_capture) -> None:
    from memplex.service import _supersession_candidates

    new = _fact(
        id="fact_capture_v1_new", object_="postgres",
        provenance={"capture_input": "factual_capture_v1"} if factual_capture else {},
    )
    same = _fact(id="fact_capture_v1_same", object_="postgres")
    changed = _fact(id="fact_capture_v1_changed", object_="mysql")
    outside = _fact(id="fact_capture_v1_outside", workspace_id="other-workspace")
    originals = deepcopy((same, changed, outside))

    candidates = list(_supersession_candidates(new, (outside, same, changed, same)))

    expected = [changed] if factual_capture else [same, changed, same]
    assert candidates == expected
    assert all(candidate is original for candidate, original in zip(candidates, expected, strict=True))
    assert (same, changed, outside) == originals
    assert new.valid_from is None


def test_shared_supersession_candidates_short_circuit_outside_capture_scope() -> None:
    from memplex.service import _supersession_candidates

    # The old policy never reads provenance when the scope already rejects
    # the candidate. Deliberately malformed metadata makes that observable.
    new = _fact(id="fact_capture_v1_new", provenance=None)
    outside = _fact(id="fact_capture_v1_outside", workspace_id="other-workspace")

    assert list(_supersession_candidates(new, (outside,))) == []


def test_shared_supersession_candidates_keep_filter_evaluation_lazy() -> None:
    from memplex.service import _supersession_candidates

    visited = []
    old = _fact()

    def existing():
        visited.append(old.id)
        yield old

    candidates = _supersession_candidates(_fact(id="fact-new"), existing())
    assert visited == []
    assert list(candidates) == [old]
    assert visited == [old.id]


@pytest.mark.parametrize("factual_capture", [False, True], ids=["ordinary", "factual-capture"])
def test_planner_matches_legacy_supersession_in_native_store(tmp_path, factual_capture) -> None:
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    contract, planner = _interfaces()
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    service = MemplexService(config=config)
    try:
        for old in (
            _fact(id="fact_capture_v1_same", object_="postgres"),
            _fact(id="fact_capture_v1_changed", object_="mysql"),
            _fact(id="fact_capture_v1_outside", workspace_id="other-workspace"),
        ):
            service.store.add_fact(old)
        existing = tuple(service.store.list_facts())
        new = _fact(
            id="fact_capture_v1_new", object_="postgres",
            provenance={"capture_input": "factual_capture_v1"} if factual_capture else {},
        )
        plan = planner(
            contract.TypedBatchSnapshot(generation=0, facts=existing),
            contract.TypedBatchInput(facts=(new,), preferences=()),
            supersede=True,
        )

        service._supersede_contradicted_facts_batch((new,), service.store)

        superseded = [operation.node for operation in plan.operations if operation.purpose == "supersede"]
        expected_ids = ["fact_capture_v1_changed"] if factual_capture else [
            "fact_capture_v1_same", "fact_capture_v1_changed",
        ]
        assert [fact.id for fact in superseded] == expected_ids
        expected = {fact.id: fact.invalid_at for fact in existing}
        expected.update({fact.id: fact.invalid_at for fact in superseded})
        assert {fact.id: fact.invalid_at for fact in service.store.list_facts()} == expected
        assert new.valid_from == STAMP
        assert plan.fact_valid_from == ((0, STAMP),)
    finally:
        service.stop()
