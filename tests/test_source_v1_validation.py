"""Pure V1 proofs, fair work budgets, and legacy ACL compatibility."""

from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from memplex.auth import AuthorizationContext, Principal, local_development_context
from memplex.authorization import AuthorizationGate, is_own_node_visible, source_authorization_node
from memplex.source_codec import encode_envelope, semantic_digest
from memplex.source_contract import (
    CARRIER_KEY,
    RejectionCode,
    SourceClass,
    SourceHead,
    SourceKind,
    SourceRef,
    SourceSnapshot,
    SourceState,
    SourceToken,
    ValidationLimits,
)
from memplex.source_validation import finalize_at, validate_sources
from tests.source_v1_support import (
    FakeReadSession,
    derived_snapshot,
    validation_snapshot,
)

CONTEXT = AuthorizationContext(
    Principal("t1", "alice", authentication_id="auth-alice"), "w1", agent_id="ag", session_id="s1"
)


def own_acl(snapshot, context):
    return is_own_node_visible(source_authorization_node(snapshot), context)


def run(
    session,
    roots,
    *,
    limits=None,
    authorization=CONTEXT,
    own=own_acl,
    safe=lambda snapshot: True,
    deadline=None,
    allow=False,
):
    return validate_sources(
        session,
        roots,
        authorization=authorization,
        own_acl=own,
        safe_for_model=safe,
        limits=limits or ValidationLimits(),
        deadline_ns=deadline or session.monotonic_ns() + 1_000_000_000,
        allow_captured_inputs=allow,
    )


def root_id(snapshot):
    return snapshot.head.ref.key.logical_id


def add(session, snapshot):
    session.snapshots[snapshot.head.ref.key] = snapshot
    return snapshot


def chain(session, length, *, prefix="chain"):
    node = add(session, validation_snapshot(f"{prefix}_{length - 1}"))
    for index in reversed(range(length - 1)):
        node = add(session, validation_snapshot(f"{prefix}_{index}", refs=(node.head.ref,)))
    return node


def wide(session, size, *, prefix="wide"):
    leaves = [add(session, validation_snapshot(f"{prefix}_leaf_{i}")) for i in range(size - 3)]
    middle = [
        add(
            session,
            validation_snapshot(
                f"{prefix}_mid_{i}",
                refs=tuple(item.head.ref for item in leaves[i * 63 : (i + 1) * 63]),
            ),
        )
        for i in range(2)
    ]
    return add(session, validation_snapshot(prefix, refs=tuple(item.head.ref for item in middle)))


@pytest.mark.parametrize(
    "change,code",
    [
        ("content", "source_changed"),
        ("auth", "auth_changed"),
        ("incarnation", "source_changed"),
        ("digest", "source_changed"),
    ],
)
def test_changed_ref_and_independent_control(change, code):
    s, u = validation_snapshot("s"), validation_snapshot("u")
    session = FakeReadSession([s, u])
    m = derived_snapshot(session, "m", (s.head.ref,))
    d = derived_snapshot(session, "d", (m.head.ref,))
    token = s.head.ref.token
    if change == "content":
        token = replace(token, content_revision=2)
    elif change == "auth":
        token = replace(token, auth_revision=2)
    elif change == "incarnation":
        token = replace(token, incarnation="99999999-9999-4999-8999-999999999999")
    current_ref = replace(
        s.head.ref,
        token=token,
        semantic_digest="f" * 64 if change == "digest" else s.head.ref.semantic_digest,
    )
    add(session, replace(s, head=replace(s.head, ref=current_ref)))
    result = run(session, [root_id(d), "u"])
    assert "u" in result.accepted
    assert result.rejected[root_id(d)] == code
    assert session.reads[s.head.ref.key] == 1


def test_shared_ancestor_is_not_cycle():
    shared = validation_snapshot("shared")
    session = FakeReadSession([shared])
    left = add(session, validation_snapshot("left", refs=(shared.head.ref,)))
    right = add(session, validation_snapshot("right", refs=(shared.head.ref,)))
    d = add(session, validation_snapshot("d", refs=(left.head.ref, right.head.ref)))
    result = run(session, ["d", "left", "right"])
    assert result.rejected == {}
    assert set(result.accepted) == {"d", "left", "right"}
    assert set(result.dependency_refs["d"]) == {
        d.head.ref,
        left.head.ref,
        right.head.ref,
        shared.head.ref,
    }
    assert session.reads == Counter(dict.fromkeys(session.snapshots, 1))


def test_cycle_and_missing_fail_per_root():
    u = validation_snapshot("u")
    a, b = validation_snapshot("a"), validation_snapshot("b")
    # Deliberately forged cyclic metadata avoids solving recursive digest
    # fixed points. Cycle detection precedes exit-time integrity checking.
    a = replace(a, head=replace(a.head, root_refs=(b.head.ref,)))
    b = replace(b, head=replace(b.head, root_refs=(a.head.ref,)))
    missing = validation_snapshot("absent")
    d = validation_snapshot("d", refs=(missing.head.ref,))
    session = FakeReadSession([a, b, d, u])
    result = run(session, ["a", "d", "u"])
    assert result.rejected["a"] == "cycle"
    assert result.rejected["d"] == "missing"
    assert set(result.accepted) == {"u"}


@pytest.mark.parametrize("size,accepted", [(128, True), (129, False)])
@pytest.mark.parametrize("reverse", [False, True])
def test_fair_budget_counts_cached_closure(size, accepted, reverse):
    session = FakeReadSession([validation_snapshot("u")])
    root = wide(session, size)
    ids = ["wide", "wide_mid_0", "wide_mid_1", "u"]
    if reverse:
        ids.reverse()
    result = run(session, ids)
    assert "u" in result.accepted
    assert ("wide" in result.accepted) is accepted
    if not accepted:
        assert result.rejected["wide"] == "budget_exhausted"
    assert max(session.reads.values()) == 1
    assert len(result.dependency_refs.get("wide", ())) == (size if accepted else 0)
    assert root.head.ref.key in result.sources


@pytest.mark.parametrize("depth,accepted", [(16, True), (17, False)])
def test_depth_boundary_and_independent_control(depth, accepted):
    session = FakeReadSession([validation_snapshot("u")])
    root = chain(session, depth)
    result = run(session, [root_id(root), "u"])
    assert "u" in result.accepted
    assert (root_id(root) in result.accepted) is accepted
    if not accepted:
        assert result.rejected[root_id(root)] == "budget_exhausted"


def test_cached_shared_tail_still_counts_actual_depth():
    session = FakeReadSession([validation_snapshot("u")])
    tail = chain(session, 16)
    too_deep = add(session, validation_snapshot("deep", refs=(tail.head.ref,)))
    result = run(session, [root_id(tail), root_id(too_deep), "u"])
    assert set(result.accepted) == {root_id(tail), "u"}
    assert result.rejected["deep"] == "budget_exhausted"


@pytest.mark.parametrize("reads", [5000, 5001])
def test_unique_read_boundary(reads):
    session = FakeReadSession()
    roots = []
    for i in range(50):
        roots.append(root_id(wide(session, 100 + (i == 0 and reads == 5001), prefix=f"r{i}")))
    result = run(session, roots)
    assert sum(session.reads.values()) == 5000
    assert len(result.accepted) == (50 if reads == 5000 else 49)
    assert set(result.rejected.values()) == (
        set() if reads == 5000 else {RejectionCode.BUDGET_EXHAUSTED}
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_small_control_survives_large_request_budget_in_any_rank(reverse):
    session = FakeReadSession([validation_snapshot("u")])
    roots = [root_id(wide(session, 101, prefix=f"r{i}")) for i in range(50)]
    roots = (["u"] + roots) if reverse else (roots + ["u"])
    result = run(session, roots)
    assert "u" in result.accepted
    assert sum(session.reads.values()) == 5000
    assert result.rejected


@pytest.mark.parametrize("count", [500, 501])
def test_root_count_boundary(count):
    session = FakeReadSession([validation_snapshot(f"r{i}") for i in range(count)])
    result = run(session, [f"r{i}" for i in range(count)])
    assert len(result.accepted) == (500 if count == 500 else 0)
    assert len(result.rejected) == (0 if count == 500 else 501)
    assert sum(session.reads.values()) == (500 if count == 500 else 0)


@pytest.mark.parametrize(
    "change", ["generation", "incarnation", "publication", "deleted", "missing"]
)
def test_current_publication_required_recursively(change):
    s, u = validation_snapshot("s"), validation_snapshot("u")
    session = FakeReadSession([s, u])
    m = derived_snapshot(session, "m", (s.head.ref,))
    d = derived_snapshot(session, "d", (m.head.ref,))
    publication = session.publications[root_id(m)]
    target = session.targets[publication.target_key]
    if change == "missing":
        del session.targets[publication.target_key]
    else:
        token = target.token
        if change == "generation":
            token = replace(token, generation=2)
        elif change == "incarnation":
            token = replace(token, incarnation="99999999-9999-4999-8999-999999999999")
        elif change == "publication":
            token = replace(
                token, current_publication="fact_derived_v1_99999999999949998999999999999999"
            )
        session.targets[publication.target_key] = replace(
            target,
            token=token,
            state=SourceState.DELETED if change == "deleted" else SourceState.LIVE,
        )
    result = run(session, [root_id(d), root_id(m), "u"])
    assert set(result.accepted) == {"u"}
    assert set(result.rejected.values()) == {RejectionCode.NOT_CURRENT_PUBLICATION}


@pytest.mark.parametrize(
    "change,code",
    [
        ("record_missing", "missing"),
        ("record_digest", "malformed"),
        ("record_output", "malformed"),
        ("record_state", "not_current_publication"),
        ("policy", "auth_changed"),
        ("algorithm", "unsupported_capability"),
        ("carrier_missing", "malformed"),
        ("output", "malformed"),
    ],
)
def test_publication_proof_must_match_authority(change, code):
    from memplex.source_contract import PublicationState

    s = validation_snapshot("s")
    session = FakeReadSession([s, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (s.head.ref,))
    record = session.publications[root_id(d)]
    if change == "record_missing":
        del session.publications[root_id(d)]
    elif change == "record_digest":
        session.publications[root_id(d)] = replace(record, envelope_digest="f" * 64)
    elif change == "record_output":
        session.publications[root_id(d)] = replace(record, output_digest="f" * 64)
    elif change == "record_state":
        session.publications[root_id(d)] = replace(record, state=PublicationState.SUPERSEDED)
    elif change in {"policy", "algorithm"}:
        policy = session.authority_state.policy
        session.authority_state = replace(
            session.authority_state,
            policy=replace(
                policy,
                revision=2 if change == "policy" else 1,
                algorithm_versions=frozenset()
                if change == "algorithm"
                else policy.algorithm_versions,
            ),
        )
    else:
        payload = dict(d.payload)
        if change == "carrier_missing":
            payload.pop("namespace")
        else:
            payload["value"] = "tampered output"
        head = replace(
            d.head,
            ref=replace(
                d.head.ref,
                semantic_digest=semantic_digest(
                    payload, input_context=d.head.input_context, root_refs=d.head.root_refs
                ),
            ),
        )
        add(session, SourceSnapshot(head, payload))
    result = run(session, [root_id(d), "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected[root_id(d)] == code


@pytest.mark.parametrize("kind", [SourceKind.FUNCTION, SourceKind.FACT, SourceKind.PREFERENCE])
def test_captured_typed_is_internal_and_raw_closure_is_required(kind):
    s = validation_snapshot("s")
    typed = validation_snapshot(
        "typed", kind=kind, refs=(s.head.ref,), source_class=SourceClass.CAPTURED_INPUT
    )
    session = FakeReadSession([s, typed, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (typed.head.ref,))
    result = run(session, ["typed", root_id(d), "u"])
    assert "typed" not in result.accepted
    assert result.rejected["typed"] == "captured_input_only"
    assert root_id(d) in result.accepted
    assert "typed" in run(session, ["typed"], allow=True).accepted
    del session.snapshots[s.head.ref.key]
    assert run(session, [root_id(d), "u"]).rejected[root_id(d)] == "missing"


def test_captured_typed_without_raw_receipt_is_rejected():
    typed = validation_snapshot(
        "typed", kind=SourceKind.FACT, source_class=SourceClass.CAPTURED_INPUT
    )
    other = validation_snapshot("other", kind=SourceKind.FACT)
    wrong = validation_snapshot(
        "wrong",
        kind=SourceKind.FACT,
        refs=(other.head.ref,),
        source_class=SourceClass.CAPTURED_INPUT,
    )
    session = FakeReadSession([typed, other, wrong, validation_snapshot("u")])
    result = run(session, ["typed", "wrong", "u"], allow=True)
    assert set(result.accepted) == {"u"}
    assert set(result.rejected.values()) == {RejectionCode.INVALID_UPSTREAM}


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("tenant_id", None, "denied"),
        ("workspace_id", None, "denied"),
        ("tenant_id", "other", "denied"),
        ("workspace_id", "other", "denied"),
        ("id", "other", "malformed"),
        ("memory_type", "preference", "malformed"),
    ],
)
def test_persisted_identity_checked_before_acl(field, value, code):
    snapshot = validation_snapshot("x", kind=SourceKind.FACT, payload={field: value})
    calls = []
    result = run(FakeReadSession([snapshot]), ["x"], own=lambda *args: calls.append(args) or True)
    assert result.rejected["x"] == code
    assert calls == []


def test_v1_does_not_inherit_identityless_local_allow():
    x = validation_snapshot("x", payload={"tenant_id": None, "workspace_id": None})
    session = FakeReadSession([x])
    result = run(session, ["x"], authorization=local_development_context(), own=lambda *args: True)
    assert result.accepted == {}
    assert result.rejected["x"] == "denied"


@pytest.mark.parametrize(
    "state,code", [(SourceState.DELETED, "deleted"), (SourceState.QUARANTINED, "quarantined")]
)
def test_source_state_is_fail_closed(state, code):
    x = validation_snapshot("x")
    head = replace(x.head, state=state, payload_locator="" if state is SourceState.DELETED else "x")
    result = run(FakeReadSession([replace(x, head=head), validation_snapshot("u")]), ["x", "u"])
    assert "u" in result.accepted
    assert result.rejected["x"] == code


def test_lookup_errors_and_unsafe_are_local():
    x, y, u = (validation_snapshot(name) for name in ["x", "y", "u"])
    session = FakeReadSession([x, y, u])
    session.failures.add(x.head.ref.key)
    result = run(session, ["x", "y", "u"], safe=lambda snapshot: root_id(snapshot) != "y")
    assert set(result.accepted) == {"u"}
    assert result.rejected == {"x": "lookup_error", "y": "unsafe"}


def test_ambiguous_cross_kind_id_fails_closed():
    session = FakeReadSession(
        [
            validation_snapshot("same"),
            validation_snapshot("same", kind=SourceKind.FACT),
            validation_snapshot("u"),
        ]
    )
    result = run(session, ["same", "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected["same"] == "malformed"


@pytest.mark.parametrize("field", ["valid_until", "expires_at", "invalid_at"])
def test_finalize_uses_one_L_for_root_and_all_dependencies(field):
    at = datetime(2026, 10, 12, tzinfo=UTC)
    s = validation_snapshot("s", payload={field: at.isoformat()})
    session = FakeReadSession([s, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (s.head.ref,))
    result = run(session, [root_id(d), "s", "u"])
    reads = dict(session.reads)
    before = finalize_at(result, at=at - timedelta(microseconds=1))
    expired_at_L = finalize_at(result, at=at)
    assert set(before.accepted) == {root_id(d), "s", "u"}
    assert set(expired_at_L.accepted) == {"u"}
    assert expired_at_L.dropped == {"expired": 2}
    assert session.reads == reads


def test_future_and_historical_are_not_current():
    at = datetime(2026, 10, 12, tzinfo=UTC)
    future = validation_snapshot(
        "future", payload={"valid_from": (at + timedelta(seconds=1)).isoformat()}
    )
    historical = validation_snapshot("historical", payload={"context_historical": True})
    session = FakeReadSession([future, historical, validation_snapshot("u")])
    result = run(session, ["future", "historical", "u"])
    final = finalize_at(result, at=at)
    assert set(final.accepted) == {"u"}
    assert final.dropped == {"invalid_upstream": 2}


@pytest.mark.parametrize("deadline_ms", [1, 250])
def test_deadline_preserves_completed_control_and_uses_monotonic(deadline_ms):
    session = FakeReadSession([validation_snapshot("u")])
    d = chain(session, 16)
    session.read_ms = 1 if deadline_ms == 1 else 20
    deadline = session.monotonic_ns() + deadline_ms * 1_000_000
    result = run(session, ["u", root_id(d)], deadline=deadline)
    if deadline_ms == 1:
        assert result.accepted == {}
    else:
        assert "u" in result.accepted
    assert result.rejected[root_id(d)] == "budget_exhausted"


def test_250ms_fence_limit_overrides_later_caller_deadline():
    session = FakeReadSession([validation_snapshot("u")])
    root = chain(session, 16)
    session.read_ms = 20
    result = run(session, [root_id(root), "u"])
    assert "u" in result.accepted
    assert result.rejected[root_id(root)] == "budget_exhausted"
    assert session.monotonic_ns() <= 1_260_000_000


@pytest.mark.parametrize("field", ["tenant_id", "workspace_id", "owner_subject_id"])
def test_converter_never_fills_identity_from_source_key(field):
    x = validation_snapshot("x", kind=SourceKind.FACT, payload={field: None})
    node = source_authorization_node(x)
    assert hasattr(node, field)
    assert getattr(node, field) is None


@pytest.mark.parametrize("kind", list(SourceKind))
def test_converter_returns_typed_or_raw_persisted_projection(kind):
    x = validation_snapshot(
        "x", kind=kind, payload={"provenance": {"agent_id": "ag"}, "origin_session": "s1"}
    )
    node = source_authorization_node(x)
    assert node.id == "x"
    assert node.tenant_id == "t1"
    assert node.workspace_id == "w1"
    assert node.provenance == {"agent_id": "ag"}
    assert node.origin_session == "s1"
    assert node.memory_type == ("paragraph" if kind is SourceKind.RAW_PARAGRAPH else kind.value)


@pytest.mark.parametrize(
    "values,context,allowed",
    [
        ({}, CONTEXT, True),
        ({"tenant_id": "other"}, CONTEXT, False),
        ({"workspace_id": "other"}, CONTEXT, False),
        ({"visibility": "user"}, CONTEXT, True),
        ({"visibility": "user", "owner_subject_id": "bob"}, CONTEXT, False),
        (
            {
                "visibility": "user",
                "owner_subject_id": "bob",
                "namespace": {"memplex_grants": "ag"},
            },
            CONTEXT,
            True,
        ),
        (
            {
                "visibility": "user",
                "owner_subject_id": "bob",
                "namespace": {"memplex_grants": "other"},
            },
            CONTEXT,
            False,
        ),
        (
            {"visibility": "session", "origin_session": "s1", "provenance": {"agent_id": "ag"}},
            CONTEXT,
            True,
        ),
        (
            {"visibility": "session", "origin_session": "s2", "provenance": {"agent_id": "ag"}},
            CONTEXT,
            False,
        ),
        ({"visibility": "session", "origin_session": "s1", "provenance": {}}, CONTEXT, False),
        ({"visibility": "unknown"}, CONTEXT, False),
        ({"tenant_id": None, "workspace_id": None}, local_development_context(), True),
        ({"tenant_id": None, "workspace_id": None}, CONTEXT, False),
    ],
)
def test_pure_acl_and_existing_entry_preserve_rules(values, context, allowed):
    payload = dict(validation_snapshot("x", payload=values).payload)
    for field in ("namespace", "provenance"):
        if field in payload:
            payload[field] = dict(payload[field])
    node = SimpleNamespace(**payload)
    gate = AuthorizationGate(SimpleNamespace(), lambda: None, lambda: None)
    assert is_own_node_visible(node, context) is allowed
    assert gate._is_node_in_scope(node, context) is allowed
    assert gate.is_node_visible(node, context) is allowed


def test_existing_entry_preserves_instance_helper_override(monkeypatch):
    gate = AuthorizationGate(SimpleNamespace(), lambda: None, lambda: None)
    node = SimpleNamespace(**validation_snapshot("x").payload)
    monkeypatch.setattr(gate, "identity_value", lambda *args: "other")
    assert gate._is_node_in_scope(node, CONTEXT) is False


@pytest.mark.parametrize("kind", [SourceKind.RAW_PARAGRAPH, SourceKind.FACT])
@pytest.mark.parametrize(
    "payload,allowed",
    [
        (
            {
                "visibility": "user",
                "owner_subject_id": "bob",
                "namespace": {"memplex_grants": "ag"},
            },
            True,
        ),
        (
            {
                "visibility": "user",
                "owner_subject_id": "bob",
                "namespace": {"memplex_grants": "other"},
            },
            False,
        ),
        ({"visibility": "session", "origin_session": "s1", "provenance": {"agent_id": "ag"}}, True),
        (
            {"visibility": "session", "origin_session": "s2", "provenance": {"agent_id": "ag"}},
            False,
        ),
    ],
)
def test_validator_uses_persisted_grants_and_origin(kind, payload, allowed):
    x = validation_snapshot("x", kind=kind, payload=payload)
    result = run(FakeReadSession([x, validation_snapshot("u")]), ["x", "u"])
    assert "u" in result.accepted
    assert ("x" in result.accepted) is allowed
    if not allowed:
        assert result.rejected["x"] == "denied"


def test_cross_scope_dependency_is_rejected_before_read():
    from memplex.source_contract import SourceKey

    x = validation_snapshot("private", payload={"tenant_id": "other"})
    x = replace(
        x,
        head=replace(
            x.head, ref=replace(x.head.ref, key=replace(x.head.ref.key, tenant_id="other"))
        ),
    )
    session = FakeReadSession([x, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (x.head.ref,))
    result = run(session, [root_id(d), "u"])
    assert "u" in result.accepted
    assert result.rejected[root_id(d)] == "unsupported_semantics"
    assert session.reads[x.head.ref.key] == 0


@pytest.mark.parametrize("boundary", ["exact", "too_small"])
def test_envelope_byte_limit_is_inclusive(boundary):
    x = validation_snapshot("x")
    session = FakeReadSession([x, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (x.head.ref,))
    size = len(d.payload["namespace"][CARRIER_KEY].encode())
    result = run(
        session,
        [root_id(d), "u"],
        limits=ValidationLimits(max_envelope_bytes=size - (boundary == "too_small")),
    )
    assert "u" in result.accepted
    assert (root_id(d) in result.accepted) is (boundary == "exact")
    if boundary == "too_small":
        assert result.rejected[root_id(d)] == "budget_exhausted"


def test_direct_reference_limit_is_not_silently_truncated():
    x, y = validation_snapshot("x"), validation_snapshot("y")
    session = FakeReadSession([x, y, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (x.head.ref, y.head.ref))
    result = run(session, [root_id(d), "u"], limits=ValidationLimits(max_direct_refs=1))
    assert set(result.accepted) == {"u"}
    assert result.rejected[root_id(d)] == "budget_exhausted"


def test_same_root_second_ref_checks_expected_vector_even_after_cached_visit():
    shared = validation_snapshot("shared")
    left = validation_snapshot("left", refs=(shared.head.ref,))
    stale = replace(shared.head.ref, token=replace(shared.head.ref.token, auth_revision=2))
    right = validation_snapshot("right", refs=(stale,))
    root = validation_snapshot("root", refs=(left.head.ref, right.head.ref))
    session = FakeReadSession([shared, left, right, root, validation_snapshot("u")])
    result = run(session, ["root", "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected["root"] == "auth_changed"
    assert session.reads[shared.head.ref.key] == 1


def test_legacy_unverified_cannot_be_promoted_to_v1_dependency():
    x = validation_snapshot("x", source_class=SourceClass.LEGACY_UNVERIFIED)
    session = FakeReadSession([x, validation_snapshot("u")])
    d = derived_snapshot(session, "d", (x.head.ref,))
    result = run(session, [root_id(d), "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected[root_id(d)] == "invalid_upstream"


@pytest.mark.parametrize(
    "field", ["memplex_tenant_id", "memplex_workspace_id", "memplex_subject_id"]
)
def test_namespace_identity_cannot_override_persisted_identity(field):
    x = validation_snapshot("x", payload={"namespace": {field: "other"}})
    calls = []
    result = run(FakeReadSession([x]), ["x"], own=lambda *args: calls.append(args) or True)
    assert result.rejected["x"] == "denied"
    assert calls == []


def test_digest_corruption_and_hook_failure_do_not_clear_control():
    x = validation_snapshot("x")
    x = replace(x, head=replace(x.head, ref=replace(x.head.ref, semantic_digest="f" * 64)))
    y = validation_snapshot("y")

    def safe(snapshot):
        if root_id(snapshot) == "y":
            raise OSError("synthetic safety hook failure")
        return True

    result = run(FakeReadSession([x, y, validation_snapshot("u")]), ["x", "y", "u"], safe=safe)
    assert set(result.accepted) == {"u"}
    assert result.rejected == {"x": "malformed", "y": "lookup_error"}


def test_utc_rollback_does_not_extend_monotonic_budget():
    session = FakeReadSession([validation_snapshot("u")])
    root = chain(session, 16)
    session.read_ms = 20
    read = session.source

    def rolling_back(key):
        snapshot = read(key)
        session.clock._utc -= timedelta(days=1)
        return snapshot

    session.source = rolling_back
    result = run(session, [root_id(root), "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected[root_id(root)] == "budget_exhausted"


@pytest.mark.parametrize("state", [SourceState.DELETED, SourceState.QUARANTINED])
def test_invalid_authority_is_not_a_partial_success(state):
    session = FakeReadSession([validation_snapshot("u")])
    session.authority_state = replace(session.authority_state, state=state)
    result = run(session, ["u"])
    assert result.accepted == {}
    assert result.rejected["u"] == "quarantined"
    assert session.reads == {}


@pytest.mark.parametrize("kind", list(SourceKind))
@pytest.mark.parametrize("marked", [False, True])
def test_premise_superseded_is_rejected_for_root_and_dependent_with_control(kind, marked):
    marker = "replacement_id" if marked else ""
    payload = (
        {"premise_superseded": marker}
        if kind is SourceKind.RAW_PARAGRAPH
        else {"namespace": {"premise_superseded": marker}}
    )
    s = validation_snapshot("s", kind=kind, payload=payload)
    session = FakeReadSession([s, validation_snapshot("u")])
    d = derived_snapshot(session, "lifecycle_d", (s.head.ref,))
    result = run(session, [root_id(d), "s", "u"])
    expected = {"u"} if marked else {root_id(d), "s", "u"}
    assert set(result.accepted) == expected
    assert result.rejected == (
        {root_id(d): "invalid_upstream", "s": "invalid_upstream"} if marked else {}
    )
    before = dict(session.reads)
    final = finalize_at(result, at=session.utc_now())
    assert set(final.accepted) == expected
    assert final.dropped == ({"invalid_upstream": 2} if marked else {})
    assert session.reads == before


@pytest.mark.parametrize("kind", [SourceKind.RAW_PARAGRAPH, SourceKind.FACT])
@pytest.mark.parametrize("marker", ["context_historical", "historical", "superseded"])
def test_non_time_lifecycle_fails_proof_before_finalization(kind, marker):
    s = validation_snapshot("s", kind=kind, payload={marker: True})
    session = FakeReadSession([s, validation_snapshot("u")])
    d = derived_snapshot(session, "historical_d", (s.head.ref,))
    result = run(session, [root_id(d), "s", "u"])
    assert set(result.accepted) == {"u"}
    assert result.rejected == {root_id(d): "invalid_upstream", "s": "invalid_upstream"}
    final = finalize_at(result, at=session.utc_now())
    assert set(final.accepted) == {"u"}
    assert final.dropped == {"invalid_upstream": 2}


@pytest.mark.parametrize("kind", [SourceKind.FUNCTION, SourceKind.FACT, SourceKind.PREFERENCE])
@pytest.mark.parametrize("marked", [False, True])
def test_captured_internal_premise_superseded_is_not_certified(kind, marked):
    raw = validation_snapshot("raw")
    s = validation_snapshot(
        "s",
        kind=kind,
        source_class=SourceClass.CAPTURED_INPUT,
        refs=(raw.head.ref,),
        payload={"namespace": {"premise_superseded": "replacement" if marked else ""}},
    )
    session = FakeReadSession([raw, s, validation_snapshot("u")])
    d = derived_snapshot(session, "captured_d", (s.head.ref,))
    result = run(session, ["s", root_id(d), "u"], allow=True)
    assert set(result.accepted) == ({"u"} if marked else {"s", root_id(d), "u"})
    final = finalize_at(result, at=session.utc_now())
    assert set(final.accepted) == set(result.accepted)


@pytest.mark.parametrize(
    "failure", ["wrong_key", "snapshot_subclass", "ordinary_object", "contract_error"]
)
@pytest.mark.parametrize("u_first", [False, True])
def test_malformed_source_cache_preserves_control_and_cached_rejection(failure, u_first):
    from memplex.source_contract import SourceContractError

    class SnapshotSubclass(SourceSnapshot):
        pass

    x, y, u = (validation_snapshot(name) for name in ("x", "y", "u"))
    session = FakeReadSession([x, u])
    d = derived_snapshot(session, "cache_d", (x.head.ref,))
    source = session.source

    def malformed_read(key):
        result = source(key)
        if key != x.head.ref.key:
            return result
        if failure == "wrong_key":
            return y
        if failure == "snapshot_subclass":
            return SnapshotSubclass(x.head, x.payload)
        if failure == "contract_error":
            raise SourceContractError()
        return object()

    session.source = malformed_read
    roots = ["u", "x", root_id(d)] if u_first else ["x", root_id(d), "u"]
    result = run(session, roots)
    assert set(result.accepted) == {"u"}
    assert result.rejected == {"x": "malformed", root_id(d): "malformed"}
    assert session.reads[x.head.ref.key] == 1
    assert set(result.sources) == {u.head.ref.key, d.head.ref.key}
    final = finalize_at(result, at=session.utc_now())
    assert set(final.accepted) == {"u"}
    assert final.dropped == {"malformed": 2}


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("id", "physical_paragraph_2", "malformed"),
        ("id", None, "malformed"),
        ("tenant_id", None, "denied"),
    ],
)
def test_canonical_raw_handle_survives_distinct_physical_locator_replacement(field, value, code):
    # The trusted backend must verify physical row ID/scope and receipt/head
    # binding before this canonical detached projection. This fake proves only
    # the pure seam; it does not normalize or implement any physical backend.
    raw = validation_snapshot("logical_handle")
    raw = replace(raw, head=replace(raw.head, payload_locator="physical_paragraph_1"))
    session = FakeReadSession([raw, validation_snapshot("u")])
    d = derived_snapshot(session, "locator_d", (raw.head.ref,))
    initial = run(session, ["logical_handle", root_id(d), "u"])
    assert set(initial.accepted) == {"logical_handle", root_id(d), "u"}
    replacement = validation_snapshot(
        "logical_handle", revision=2, payload={"raw_text": "current replacement text"}
    )
    replacement = replace(
        replacement, head=replace(replacement.head, payload_locator="physical_paragraph_2")
    )
    add(session, replacement)
    result = run(session, ["logical_handle", root_id(d), "u"])
    assert set(result.accepted) == {"logical_handle", "u"}
    assert result.accepted["logical_handle"].head.payload_locator == "physical_paragraph_2"
    assert result.rejected[root_id(d)] == "source_changed"
    unnormalized = dict(replacement.payload)
    unnormalized[field] = value
    add(session, SourceSnapshot(replacement.head, unnormalized))
    calls = []
    bad = run(session, ["logical_handle", "u"], own=lambda *args: calls.append(args) or True)
    assert bad.rejected["logical_handle"] == code
    assert len(calls) == 1 and root_id(calls[0][0]) == "u"
