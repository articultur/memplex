"""Independent V1 wire oracles and fail-closed pure contract checks."""

import hashlib
import json
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import memplex.source_contract as contract
from memplex.models import Fact, Function, Observation, Preference
from memplex.source_codec import (
    decode_envelope,
    decode_ledger,
    encode_envelope,
    encode_ledger,
    envelope_digest,
    output_digest,
    semantic_digest,
)
from tests.source_v1_support import (
    AUTHORITY_ID,
    INCARNATION,
    PUBLICATION_ID,
    REQUEST_ID,
    TARGET_INCARNATION,
    FakeClock,
    fact_envelope,
    source_snapshot,
)

GOLDEN_PATH = Path(__file__).parent / "fixtures/source_v1/wire_v1.json"
GOLDEN = json.loads(GOLDEN_PATH.read_text())


def digest(payload, **kwargs):
    return semantic_digest(
        payload,
        input_context=kwargs.get("input_context", {}),
        root_refs=kwargs.get("root_refs", ()),
    )


def ledger():
    snapshot = source_snapshot("raw_来源")
    policy = contract.PolicySnapshot(
        1,
        (contract.PolicyEntry("auth", "t1", "user", "w1", "agent", frozenset({"reader"})),),
        frozenset({contract.ALGORITHM_VERSION}),
    )
    authority = contract.AuthorityState(
        contract.AuthorityScope(AUTHORITY_ID, "t1", "w1"), contract.SourceState.LIVE, 1, policy, 2
    )
    token = contract.TargetToken(TARGET_INCARNATION, 1, PUBLICATION_ID)
    target = contract.TargetHead("d" * 64, token, contract.SourceState.LIVE, 2)
    publication = contract.PublicationRecord(
        PUBLICATION_ID,
        "d" * 64,
        token,
        contract.PublicationState.ACTIVE,
        "c" * 64,
        "b" * 64,
        (snapshot.head.ref,),
        contract.ALGORITHM_VERSION,
        REQUEST_ID,
        "e" * 64,
        "f" * 64,
        1,
        2,
    )
    return contract.SourceLedger(
        authority,
        {snapshot.head.ref.key: snapshot.head},
        {target.derivation_key: target},
        {PUBLICATION_ID: publication},
    )


def test_exact_types_and_canonical_roundtrip():
    envelope = decode_envelope(GOLDEN["canonical_envelope"])
    assert json.loads(encode_envelope(envelope)) == GOLDEN["envelope"]
    assert encode_envelope(envelope) == GOLDEN["canonical_envelope"]
    assert decode_envelope(encode_envelope(envelope)) == envelope
    assert envelope_digest(envelope) == GOLDEN["envelope_sha256"]
    assert hashlib.sha256(GOLDEN_PATH.read_bytes()).hexdigest() == (
        "0961222e73ed657b164908ad3ea1da56e2949fdb099752e050c70b805cd7c98e"
    )
    assert digest(GOLDEN["digest_payload"]) == GOLDEN["semantic_sha256"]
    assert output_digest(GOLDEN["digest_payload"]) == GOLDEN["output_sha256"]


@pytest.mark.parametrize("revision", [True, False, 0, -1, 2**63, 1.0, "1", None])
def test_revisions_reject_coercion_and_out_of_range(revision):
    with pytest.raises(contract.SourceContractError):
        contract.SourceToken(INCARNATION, revision, 1)
    with pytest.raises(contract.SourceContractError):
        contract.SourceToken(INCARNATION, 1, revision)


@pytest.mark.parametrize("revision", [1, 2**63 - 1])
def test_revision_bounds_are_inclusive(revision):
    assert contract.SourceToken(INCARNATION, revision, revision).content_revision == revision


@pytest.mark.parametrize(
    "uuid",
    [
        "",
        INCARNATION.upper(),
        "22222222222242228222222222222222",
        "22222222-2222-1222-8222-222222222222",
        "22222222-2222-4222-7222-222222222222",
        123,
    ],
)
def test_opaque_uuid_is_canonical_v4(uuid):
    # The chosen all-digit UUID has no case variants; use a letter UUID below.
    if uuid == INCARNATION:
        uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa".upper()
    with pytest.raises(contract.SourceContractError):
        contract.SourceToken(uuid, 1, 1)


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": True},
        {"contract_version": 2},
        {"output_digest_version": 2},
        {"producer": "other"},
        {"algorithm_version": "unknown"},
        {"target_generation": True},
        {"target_generation": 0},
        {"policy_revision": 2**63},
        {"alternative_support_groups": [GOLDEN["envelope"]["required_premises"]]},
        {"required_premises": []},
        {"authorization_dependencies": []},
        {"output_digest": "B" * 64},
        {"derivation_key": "d" * 63},
        {"publication_id": "legacy_fact"},
        {"extra_protocol": 1},
    ],
)
def test_reserved_carrier_rejects_ambiguous_json(change):
    wire = deepcopy(GOLDEN["envelope"])
    wire.update(change)
    with pytest.raises(contract.SourceContractError):
        decode_envelope(json.dumps(wire))


@pytest.mark.parametrize("field", ["digest_version", "semantic_digest", "extra"])
def test_nested_source_ref_strict_fields(field):
    wire = deepcopy(GOLDEN["envelope"])
    for refs in (wire["required_premises"], wire["authorization_dependencies"]):
        refs[0][field] = 2 if field == "digest_version" else "wrong"
    with pytest.raises(contract.SourceContractError):
        decode_envelope(json.dumps(wire))


@pytest.mark.parametrize(
    "raw",
    [
        '{"contract_version":1,"contract_version":1}',
        GOLDEN["canonical_envelope"].replace(
            '"content_revision":1', '"content_revision":1,"content_revision":1', 1
        ),
        GOLDEN["canonical_envelope"].replace('"target_generation":1', '"target_generation":NaN'),
        GOLDEN["canonical_envelope"].replace(
            '"target_generation":1', '"target_generation":Infinity'
        ),
        GOLDEN["canonical_envelope"].replace("raw_来源", "raw_\\ud800"),
        "null",
        "[]",
        "{",
    ],
)
def test_duplicate_keys_nonfinite_unicode_and_malformed_json(raw):
    with pytest.raises(contract.SourceContractError):
        decode_envelope(raw)


@pytest.mark.parametrize("field", ["tenant_id", "workspace_id", "authority_id"])
def test_envelope_rejects_cross_scope_sources(field):
    wire = deepcopy(GOLDEN["envelope"])
    for refs in (wire["required_premises"], wire["authorization_dependencies"]):
        refs[0]["key"][field] = "other" if field != "authority_id" else INCARNATION
    if field != "authority_id":
        ref = source_snapshot("s").head.ref
        other = replace(ref, key=replace(ref.key, **{field: "other"}))
        with pytest.raises(contract.SourceContractError):
            fact_envelope((ref, other))
    else:
        with pytest.raises(contract.SourceContractError):
            decode_envelope(json.dumps(wire))


@pytest.mark.parametrize("changed_auth", [False, True])
def test_reference_sets_reject_duplicate_keys_or_mismatched_vectors(changed_auth):
    ref = source_snapshot("s").head.ref
    second = replace(ref, token=replace(ref.token, auth_revision=2)) if changed_auth else ref
    with pytest.raises(contract.SourceContractError):
        fact_envelope((ref, second))
    with pytest.raises(contract.SourceContractError):
        replace(
            fact_envelope((ref,)),
            authorization_dependencies=(replace(ref, token=replace(ref.token, auth_revision=2)),),
        )


def test_direct_refs_and_envelope_byte_limits_include_equal_boundary():
    refs = tuple(source_snapshot(f"s{i:02}").head.ref for i in range(64))
    value = fact_envelope(refs)
    assert len(decode_envelope(encode_envelope(value)).required_premises) == 64
    with pytest.raises(contract.SourceContractError):
        fact_envelope(refs + (source_snapshot("s64").head.ref,))
    base = GOLDEN["canonical_envelope"]
    boundary = base + " " * (65536 - len(base.encode()))
    assert len(boundary.encode()) == 65536
    assert encode_envelope(decode_envelope(boundary)) == base
    with pytest.raises(contract.SourceContractError):
        decode_envelope(boundary + " ")
    # UTF-8 bytes, not character count, govern Unicode envelope rejection.
    wire = deepcopy(GOLDEN["envelope"])
    for group in ("required_premises", "authorization_dependencies"):
        wire[group][0]["key"]["logical_id"] = "🫘" * 9000
    with pytest.raises(contract.SourceContractError):
        decode_envelope(json.dumps(wire, ensure_ascii=False))


def test_reference_sets_canonicalize_without_reordering_business_arrays():
    a, b = (source_snapshot(name).head.ref for name in ("a", "b"))
    assert encode_envelope(fact_envelope((a, b))) == encode_envelope(fact_envelope((b, a)))
    assert digest({"items": [1, 2]}) != digest({"items": [2, 1]})
    assert digest({"text": " a "}) != digest({"text": "a"})
    assert digest({"none": None}) != digest({})


@pytest.mark.parametrize(
    "field",
    [
        "access_count",
        "last_accessed_at",
        "created_at",
        "updated_at",
        "version",
        "observation_count",
        "last_observed_at",
        "sync_version",
        "transport_version",
        "storage_generation",
        "content_hash",
    ],
)
def test_nonsemantic_fields_do_not_invalidate(field):
    before = {"object": "stable", field: None}
    assert digest(before) == digest({**before, field: "changed"})
    assert output_digest(before) == output_digest({**before, field: "changed"})


@pytest.mark.parametrize(
    "field",
    [
        "object",
        "premise_superseded",
        "source_paragraphs",
        "needs_review",
        "unsafe",
        "valid_until",
        "trust_tier",
        "future_business",
        "namespace",
        "provenance",
    ],
)
def test_semantic_business_lifecycle_fields_are_conservatively_included(field):
    values = {
        "valid_until": "2026-10-12T01:00:00Z",
        "source_paragraphs": ["new"],
        "namespace": {"business": "changed"},
        "provenance": {"evidence": "new"},
    }
    assert digest({"object": "stable"}) != digest(
        {"object": "stable", field: values.get(field, "changed")}
    )


def test_input_context_and_direct_roots_are_part_of_semantic_digest():
    payload = {"raw_text": "same"}
    ref = source_snapshot("r").head.ref
    assert digest(payload) != digest(payload, input_context={"author_role": "assistant"})
    assert digest(payload) != digest(payload, root_refs=(ref,))
    assert digest(payload, root_refs=(ref,)) != digest(
        payload, root_refs=(replace(ref, token=replace(ref.token, content_revision=2)),)
    )


def test_output_digest_has_no_recursion_and_envelope_digest_binds_output():
    ref = source_snapshot("r").head.ref
    envelope = fact_envelope((ref,))
    payload = {"object": "claim", "namespace": {"business": "value"}}
    with_carrier = {
        **payload,
        "namespace": {"business": "value", contract.CARRIER_KEY: encode_envelope(envelope)},
        "output_digest": "a" * 64,
        "publication_id": PUBLICATION_ID,
        "target_generation": 1,
        "tenant_id": "t1",
        "provenance": {"authentication_id": "secret"},
    }
    assert output_digest(payload) == output_digest(with_carrier)
    assert digest(payload) != digest(with_carrier)
    assert envelope_digest(envelope) != envelope_digest(replace(envelope, output_digest="c" * 64))
    assert output_digest(payload) != output_digest({**payload, "object": "new claim"})


@pytest.mark.parametrize(
    "node",
    [
        Function(
            id="func_a",
            name="f",
            created_at="2026-10-11T00:00:00Z",
            updated_at="2026-10-11T00:00:00Z",
        ),
        Fact(id="fact_a", subject="s", predicate="p", object_="o"),
        Preference(id="pref_a", aspect="a", preference="p"),
        Observation(id="obs_a", event="e", context="c"),
    ],
)
def test_four_existing_models_preserve_carrier_exactly(node):
    node.namespace = {"business": "kept", contract.CARRIER_KEY: GOLDEN["canonical_envelope"]}
    node.tenant_id, node.workspace_id = "t1", "w1"
    restored = type(node).from_dict(node.to_dict())
    assert restored.namespace == node.namespace
    assert restored.tenant_id == "t1" and restored.workspace_id == "w1"
    assert (
        decode_envelope(restored.namespace[contract.CARRIER_KEY]).publication_id == PUBLICATION_ID
    )
    assert output_digest(restored.to_dict()) == output_digest(node.to_dict())


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_json_payload_rejects_nonfinite_numbers(number):
    for function in (digest, output_digest):
        with pytest.raises(contract.SourceContractError):
            function({"nested": [number]})
    with pytest.raises(contract.SourceContractError):
        replace(source_snapshot("s"), payload={"bad": number})


@pytest.mark.parametrize("value", [{1: "x"}, {"x": {"a"}}, {"x": object()}, {"x": "\ud800"}])
def test_json_payload_rejects_non_json_and_surrogates(value):
    with pytest.raises(contract.SourceContractError):
        digest(value)


@pytest.mark.parametrize(
    "value", ["yesterday", "2026-10-11T00:00:00", "2026-10-11T01:00:00+01:00", True]
)
def test_time_fields_require_valid_utc(value):
    with pytest.raises(contract.SourceContractError):
        digest({"valid_until": value})


@pytest.mark.parametrize(
    "change",
    [
        {"contract_version": 2},
        {"digest_version": 2},
        {"algorithm_version": "future"},
        {"memplex_source_contract_future": "x"},
    ],
)
def test_unknown_payload_protocol_is_rejected(change):
    with pytest.raises(contract.SourceContractError):
        digest({"raw_text": "x", **change})


def test_payload_and_mapping_dtos_are_detached_deeply_immutable():
    payload = {"list": [{"value": "before"}]}
    snapshot = replace(source_snapshot("s"), payload=payload)
    payload["list"][0]["value"] = "after"
    assert snapshot.payload["list"][0]["value"] == "before"
    with pytest.raises(TypeError):
        snapshot.payload["list"][0]["value"] = "changed"
    with pytest.raises(FrozenInstanceError):
        snapshot.head = source_snapshot("other").head
    source = source_snapshot("s")
    mutable = {source.head.ref.key: source.head}
    original = ledger()
    value = replace(original, sources=mutable)
    mutable.clear()
    assert len(value.sources) == 1
    with pytest.raises(TypeError):
        value.targets["x"] = next(iter(value.targets.values()))


def test_ledger_roundtrip_is_deterministic_and_exact():
    value = ledger()
    wire = encode_ledger(value)
    assert set(wire) == {"contract_version", "authority", "sources", "targets", "publications"}
    assert decode_ledger(wire) == value
    assert encode_ledger(decode_ledger(json.loads(json.dumps(wire)))) == wire
    wire["authority"]["policy"]["entries"][0]["roles"].append("other")
    assert value.authority.policy.entries[0].roles == frozenset({"reader"})


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "version",
        "source_extra",
        "duplicate",
        "wrong_scope",
        "wrong_key",
        "bool_commit",
        "bad_policy",
        "bad_target",
        "duplicate_request",
    ],
)
def test_ledger_strict_sets_metadata_and_relations(mutation):
    value = ledger()
    if mutation == "wrong_key":
        with pytest.raises(contract.SourceContractError):
            replace(
                value,
                sources={source_snapshot("other").head.ref.key: next(iter(value.sources.values()))},
            )
        return
    wire = deepcopy(encode_ledger(value))
    if mutation == "unknown":
        wire["extra"] = {}
    elif mutation == "version":
        wire["contract_version"] = True
    elif mutation == "source_extra":
        wire["sources"][0]["extra"] = "business cannot extend protocol DTO"
    elif mutation == "duplicate":
        wire["sources"].append(wire["sources"][0])
    elif mutation == "wrong_scope":
        wire["sources"][0]["ref"]["key"]["workspace_id"] = "other"
    elif mutation == "bool_commit":
        wire["authority"]["commit_seq"] = True
    elif mutation == "bad_policy":
        wire["authority"]["policy"]["algorithm_versions"] = ["future"]
    elif mutation == "bad_target":
        wire["targets"][0]["token"]["current_publication"] = "legacy"
    elif mutation == "duplicate_request":
        second = deepcopy(wire["publications"][0])
        second["publication_id"] = "fact_derived_v1_66666666666646668666666666666666"
        second["target_token"]["current_publication"] = second["publication_id"]
        second["state"] = "superseded"
        wire["publications"].append(second)
    with pytest.raises(contract.SourceContractError):
        decode_ledger(wire)


def test_complete_shared_dtos_validate_expected_and_receipt_invariants():
    snapshot = source_snapshot("s")
    ref = snapshot.head.ref
    manifest = contract.CaptureManifest(
        REQUEST_ID,
        AUTHORITY_ID,
        1,
        1,
        (ref,),
        {"author_role": "user"},
        "f" * 64,
        contract.ALGORITHM_VERSION,
    )
    envelope = fact_envelope((ref,))
    mutation = contract.SourceMutation(
        ref.key,
        contract.ExpectedAbsent.ABSENT,
        contract.SourceOperation.CREATE,
        snapshot.payload,
        contract.SourceClass.DIRECT_INPUT,
        {},
        (),
    )
    command = contract.PublishCommand(
        REQUEST_ID, "d" * 64, contract.ExpectedAbsent.ABSENT, manifest, envelope, {"object": "new"}
    )
    receipt = contract.PublicationReceipt(
        PUBLICATION_ID, contract.TargetToken(TARGET_INCARNATION, 1, PUBLICATION_ID), 1, False
    )
    assert mutation.expected is contract.ExpectedAbsent.ABSENT
    assert command.manifest == manifest
    assert receipt.replayed is False
    assert contract.SourceReceipt(ref, contract.SourceState.LIVE, 1).ref == ref
    bad = [
        lambda: replace(mutation, expected=None),
        lambda: replace(mutation, operation=contract.SourceOperation.REPLACE),
        lambda: replace(mutation, payload=None),
        lambda: replace(receipt, replayed=1),
        lambda: replace(manifest, policy_revision=True),
        lambda: replace(command, derivation_key="e" * 64),
        lambda: replace(snapshot.head, state="live"),
    ]
    for factory in bad:
        with pytest.raises(contract.SourceContractError):
            factory()


def test_limits_final_clock_and_validation_result_exact_types():
    limits = contract.ValidationLimits()
    assert tuple(getattr(limits, field.name) for field in fields(limits)) == (
        500,
        65536,
        64,
        128,
        16,
        5000,
        250,
    )
    for field in fields(limits):
        with pytest.raises(contract.SourceContractError):
            replace(limits, **{field.name: True})
    snapshot = source_snapshot("s")
    result = contract.ValidationResult(
        {"s": snapshot}, {}, {"s": ()}, {snapshot.head.ref.key: snapshot}
    )
    assert result.accepted["s"] == snapshot
    assert (
        contract.FinalRead(datetime(2026, 10, 11, tzinfo=UTC), result.accepted, {}).accepted
        == result.accepted
    )
    for at in (
        datetime(2026, 10, 11, tzinfo=UTC).replace(tzinfo=None),
        datetime(2026, 10, 11, tzinfo=timezone(timedelta(hours=1))),
    ):
        with pytest.raises(contract.SourceContractError):
            contract.FinalRead(at, {}, {})
    with pytest.raises(contract.SourceContractError):
        contract.ValidationResult({}, {"s": "missing"}, {}, {})
    clock = FakeClock()
    before = clock.monotonic_ns()
    clock.advance_ms(250)
    assert clock.monotonic_ns() - before == 250_000_000


@pytest.mark.parametrize(
    "namespace_key", ["memplex_tenant_id", "memplex_subject_id", "memplex_workspace_id"]
)
def test_actual_binder_namespace_identity_is_separate_from_business(namespace_key):
    before = {"object": "same", "namespace": {namespace_key: "first", "business": "same"}}
    after = {"object": "same", "namespace": {namespace_key: "second", "business": "same"}}
    assert digest(before) == digest(after)
    assert output_digest(before) == output_digest(after)
    assert digest(after) != digest(
        {**after, "namespace": {namespace_key: "second", "business": "new"}}
    )


def test_actual_bind_node_identity_request_rebind_preserves_content_digests():
    from memplex.auth import AuthorizationContext, Principal, bind_node_identity

    context = AuthorizationContext(
        Principal("t1", "owner", authentication_id="auth"),
        "w1",
        agent_id="agent",
        session_id="session",
        request_id="r1",
    )
    node = Preference(id="pref_a", subject_id="Alice", aspect="drink", preference="tea")
    bind_node_identity(node, context, visibility="private")
    before = node.to_dict()
    bind_node_identity(node, replace(context, request_id="r2"), visibility="private")
    assert digest(before) == digest(node.to_dict())
    assert output_digest(before) == output_digest(node.to_dict())
    changed = {**node.to_dict(), "provenance": {**node.provenance, "evidence": "new"}}
    assert digest(before) != digest(changed)
    assert output_digest(before) != output_digest(changed)


def test_preference_business_subject_is_separate_from_acl_owner():
    node = Preference(
        id="pref_a",
        subject_id="Alice",
        aspect="drink",
        preference="tea",
        tenant_id="t1",
        workspace_id="w1",
        owner_subject_id="owner",
        visibility="private",
    )
    before = node.to_dict()
    node.subject_id = "Bob"
    assert digest(before) != digest(node.to_dict())
    assert output_digest(before) != output_digest(node.to_dict())
    assert digest(before) == digest({**before, "owner_subject_id": "other owner"})


def test_function_observation_evidence_strength_is_semantic():
    from memplex.models import FieldValue

    field = FieldValue(desc="same action", weight=0.5, observation=0.1)
    node = Function(
        id="func_a",
        action=[field],
        created_at="2026-10-11T00:00:00Z",
        updated_at="2026-10-11T00:00:00Z",
    )
    before = node.to_dict()
    field.observation = 0.9
    assert digest(before) != digest(node.to_dict())
    assert output_digest(before) != output_digest(node.to_dict())


@pytest.mark.parametrize("role", ["trigger", "condition", "action", "benefit"])
@pytest.mark.parametrize("field", ["observation", "observation_count", "last_observed_at"])
def test_unknown_nested_business_fields_are_retained(role, field):
    before = {role: [{field: "A"}]}
    after = {role: [{field: "B"}]}
    assert digest(before) != digest(after)
    assert output_digest(before) != output_digest(after)


def test_deleted_target_advances_without_rewriting_publication_history():
    original = ledger()
    target = next(iter(original.targets.values()))
    record = next(iter(original.publications.values()))
    deleted_target = replace(
        target,
        token=replace(target.token, generation=2),
        state=contract.SourceState.DELETED,
        last_commit_seq=3,
    )
    audit = replace(record, state=contract.PublicationState.DELETED)
    value = contract.SourceLedger(
        replace(original.authority, commit_seq=3),
        original.sources,
        {deleted_target.derivation_key: deleted_target},
        {audit.publication_id: audit},
    )
    assert value.targets[target.derivation_key].token.generation == 2
    assert value.publications[record.publication_id].target_token.generation == 1
    assert decode_ledger(encode_ledger(value)) == value


@pytest.mark.parametrize("future_audit", [False, True])
def test_current_publication_rejects_live_mismatch_and_future_audit_generation(future_audit):
    original = ledger()
    target = next(iter(original.targets.values()))
    record = next(iter(original.publications.values()))
    if future_audit:
        target = replace(target, state=contract.SourceState.DELETED)
        record = replace(
            record,
            state=contract.PublicationState.DELETED,
            target_token=replace(record.target_token, generation=2),
        )
    else:
        target = replace(target, token=replace(target.token, generation=2))
    with pytest.raises(contract.SourceContractError):
        contract.SourceLedger(
            original.authority,
            original.sources,
            {target.derivation_key: target},
            {record.publication_id: record},
        )


def test_recreate_retains_old_incarnation_publication_audit():
    original = ledger()
    previous = next(iter(original.publications.values()))
    old_audit = replace(previous, state=contract.PublicationState.SUPERSEDED)
    publication_id = "fact_derived_v1_66666666666646668666666666666666"
    new_token = contract.TargetToken("77777777-7777-4777-8777-777777777777", 1, publication_id)
    new_record = replace(
        previous,
        publication_id=publication_id,
        target_token=new_token,
        request_id="88888888-8888-4888-8888-888888888888",
        observed_commit_seq=3,
        commit_seq=4,
    )
    target = contract.TargetHead("d" * 64, new_token, contract.SourceState.LIVE, 4)
    value = contract.SourceLedger(
        replace(original.authority, commit_seq=4),
        original.sources,
        {target.derivation_key: target},
        {old_audit.publication_id: old_audit, publication_id: new_record},
    )
    assert (
        value.publications[old_audit.publication_id].target_token.incarnation
        != target.token.incarnation
    )
    assert decode_ledger(encode_ledger(value)) == value
