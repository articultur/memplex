"""Synthetic, explicit inputs for the source validity V1 tests."""

from datetime import UTC, datetime, timedelta

from memplex.source_codec import semantic_digest
from memplex.source_contract import (
    ALGORITHM_VERSION,
    DerivationEnvelope,
    SourceClass,
    SourceHead,
    SourceKey,
    SourceKind,
    SourceRef,
    SourceSnapshot,
    SourceState,
    SourceToken,
)

AUTHORITY_ID = "11111111-1111-4111-8111-111111111111"
INCARNATION = "22222222-2222-4222-8222-222222222222"
PUBLICATION_ID = "fact_derived_v1_33333333333343338333333333333333"
TARGET_INCARNATION = "44444444-4444-4444-8444-444444444444"
REQUEST_ID = "55555555-5555-4555-8555-555555555555"


def source_snapshot(
    name: str, *, kind: SourceKind = SourceKind.RAW_PARAGRAPH, revision: int = 1
) -> SourceSnapshot:
    payload: dict[str, object] = {
        "id": name,
        "raw_text": f"source {name}",
        "tenant_id": "t1",
        "workspace_id": "w1",
    }
    ref = SourceRef(
        SourceKey(AUTHORITY_ID, "t1", "w1", kind, name),
        SourceToken(INCARNATION, revision, 1),
        1,
        semantic_digest(payload, input_context={}, root_refs=()),
    )
    return SourceSnapshot(
        SourceHead(ref, SourceState.LIVE, SourceClass.DIRECT_INPUT, name, 1, {}, ()), payload
    )


def fact_envelope(refs: tuple[SourceRef, ...]) -> DerivationEnvelope:
    return DerivationEnvelope(
        1,
        "factual_capture",
        ALGORITHM_VERSION,
        "d" * 64,
        PUBLICATION_ID,
        TARGET_INCARNATION,
        1,
        AUTHORITY_ID,
        1,
        refs,
        (),
        refs,
        "b" * 64,
        1,
    )


class FakeClock:
    def __init__(self) -> None:
        self._utc = datetime(2026, 10, 11, tzinfo=UTC)
        self._monotonic = 1_000_000_000

    def utc_now(self) -> datetime:
        return self._utc

    def monotonic_ns(self) -> int:
        return self._monotonic

    def advance_ms(self, ms: int) -> None:
        if type(ms) is not int or ms < 0:
            raise ValueError("advance requires nonnegative integer milliseconds")
        self._utc += timedelta(milliseconds=ms)
        self._monotonic += ms * 1_000_000


class FakeReadSession:
    """One stable synthetic authority view with observable source I/O."""

    def __init__(self, snapshots=(), *, clock=None):
        from collections import Counter

        from memplex.source_contract import AuthorityScope, AuthorityState, PolicySnapshot

        self.clock = clock or FakeClock()
        self.snapshots = {snapshot.head.ref.key: snapshot for snapshot in snapshots}
        self.targets = {}
        self.publications = {}
        self.reads = Counter()
        self.failures = set()
        self.read_ms = 0
        self.authority_state = AuthorityState(
            AuthorityScope(AUTHORITY_ID, "t1", "w1"), SourceState.LIVE, 1,
            PolicySnapshot(1, (), frozenset({ALGORITHM_VERSION})), 1,
        )

    def authority(self):
        return self.authority_state

    def resolve_id(self, memory_id):
        from memplex.source_contract import SourceContractError

        keys = [key for key in self.snapshots if key.logical_id == memory_id]
        if len(keys) > 1:
            raise SourceContractError()
        return keys[0] if keys else None

    def source(self, key):
        self.reads[key] += 1
        self.clock.advance_ms(self.read_ms)
        if key in self.failures:
            raise OSError("synthetic source read failure")
        return self.snapshots.get(key)

    def target(self, derivation_key):
        return self.targets.get(derivation_key)

    def publication(self, publication_id):
        return self.publications.get(publication_id)

    def utc_now(self):
        return self.clock.utc_now()

    def monotonic_ns(self):
        return self.clock.monotonic_ns()


def validation_snapshot(name, *, refs=(), kind=SourceKind.RAW_PARAGRAPH,
                        source_class=SourceClass.DIRECT_INPUT, payload=None,
                        revision=1, auth_revision=1, state=SourceState.LIVE):
    """Build registered synthetic data with real canonical semantic digests."""
    values = {"id": name, "tenant_id": "t1", "workspace_id": "w1",
              "owner_subject_id": "alice", "visibility": "workspace"}
    if kind is SourceKind.RAW_PARAGRAPH:
        values["raw_text"] = f"source {name}"
    else:
        values.update(memory_type=kind.value, name=name, subject=name,
                      predicate="is", value="synthetic", description="synthetic")
    values.update(payload or {})
    ref = SourceRef(SourceKey(AUTHORITY_ID, "t1", "w1", kind, name),
                    SourceToken(INCARNATION, revision, auth_revision), 1,
                    semantic_digest(values, input_context={}, root_refs=refs))
    return SourceSnapshot(SourceHead(ref, state, source_class, name, 1, {}, refs), values)


def derived_snapshot(session, name, refs):
    """Register a carrier, immutable publication, and current target head."""
    import hashlib
    from dataclasses import replace
    from uuid import NAMESPACE_DNS, uuid5

    from memplex.source_codec import encode_envelope, output_digest
    from memplex.source_contract import PublicationRecord, PublicationState, TargetHead, TargetToken

    # UUID version bits are fixed to 4 for deterministic synthetic opaque IDs.
    raw_uuid = str(uuid5(NAMESPACE_DNS, name))
    publication_id = "fact_derived_v1_" + (raw_uuid[:14] + "4" + raw_uuid[15:]).replace("-", "")
    base = validation_snapshot(publication_id, kind=SourceKind.FACT)
    envelope = replace(fact_envelope(refs), publication_id=publication_id,
                       derivation_key=hashlib.sha256(name.encode()).hexdigest(),
                       output_digest=output_digest(base.payload))
    payload = dict(base.payload)
    payload["namespace"] = {"memplex_derivation_contract": encode_envelope(envelope)}
    snapshot = validation_snapshot(publication_id, kind=SourceKind.FACT,
                                   source_class=SourceClass.DERIVED_V1, payload=payload)
    token = TargetToken(TARGET_INCARNATION, 1, publication_id)
    session.targets[envelope.derivation_key] = TargetHead(envelope.derivation_key, token, SourceState.LIVE, 1)
    session.publications[publication_id] = PublicationRecord(
        publication_id, envelope.derivation_key, token, PublicationState.ACTIVE,
        hashlib.sha256(encode_envelope(envelope).encode()).hexdigest(), envelope.output_digest,
        refs, ALGORITHM_VERSION, REQUEST_ID, "a"*64, "b"*64, 1, 1,
    )
    session.snapshots[snapshot.head.ref.key] = snapshot
    return snapshot
