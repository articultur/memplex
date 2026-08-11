"""受信 ingress gateway 必须在任何数据库调用前冻结 Task 1 字节。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from memplex.sync_ingress import ValidatedIngressBatch, validate_ingress_batch
from memplex.sync_protocol import (
    SyncBatch,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncScope,
    SyncVersion,
)
from memplex.sync_repository import SyncBatchRejected


def _batch(*, payload: dict[str, object] | None = None) -> SyncBatch:
    event_id = "123e4567-e89b-42d3-a456-426614174901"
    origin = "remote-gateway"
    event = SyncEvent(
        1, event_id, origin, SyncNodeType.FUNCTION, SyncEntityKey.node("gateway-fn"),
        SyncOperation.UPSERT,
        str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), origin, event_id)),
        SyncScope("tenant-gateway", "owner-gateway", "workspace-gateway", "user", None, None),
        {"value": -4.288043741161912e17} if payload is None else payload,
    )
    return SyncBatch(1, "123e4567-e89b-42d3-a456-426614174902", origin, (event,))


def test_validator_returns_opaque_frozen_envelope_for_frozen_task1_bytes() -> None:
    batch = _batch(payload={"binary64": -4.288043741161912e17, "unicode": "雪"})
    envelope = validate_ingress_batch(batch.canonical_bytes, batch.request_digest)
    assert envelope.batch == batch
    assert envelope.canonical_bytes == batch.canonical_bytes
    assert envelope.request_digest == batch.request_digest
    with pytest.raises(TypeError):
        ValidatedIngressBatch(object(), batch)


@pytest.mark.parametrize("mutate", [
    lambda batch: json.dumps(batch.to_dict(), indent=2).encode(),
    lambda batch: json.dumps(batch.to_dict(), separators=(",", ":"), sort_keys=False).encode(),
    lambda batch: batch.canonical_bytes.replace(b"-428804374116191200", b"-4.288043741161912e17"),
    lambda batch: batch.canonical_bytes.replace(b'"entity_key":"node:v1:Z2F0ZXdheS1mbg"', b'"entity_key":"node:v1:Z2F0ZXdheS1mbh"'),
    lambda batch: batch.canonical_bytes.replace(b'"version":"v1:', b'"version":"v1:='),
    lambda batch: b'{"x":1,"x":1}',
    lambda batch: b'\xff',
    lambda batch: batch.canonical_bytes.replace(b'"value"', b'"nul"').replace(b'-428804374116191200', b'"\\u0000"'),
])
def test_validator_rejects_noncanonical_input(mutate) -> None:
    batch = _batch()
    raw = mutate(batch)
    with pytest.raises(SyncBatchRejected, match="invalid ingress request"):
        validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())


def test_validator_rejects_digest_mismatch() -> None:
    batch = _batch()
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(batch.canonical_bytes, "0" * 64)


def test_validate_ingress_batch_does_not_expose_request_content() -> None:
    with pytest.raises(SyncBatchRejected) as error:
        validate_ingress_batch(b'{"secret":"do-not-leak"}', hashlib.sha256(b'{"secret":"do-not-leak"}').hexdigest())
    assert "do-not-leak" not in str(error.value)


@pytest.mark.parametrize("number", [
    float(2**53), 0.0, 1e-7, 1e-6, 1e20, 1e21, -4.288043741161912e17,
])
def test_validator_accepts_task1_binary64_wire_numbers(number: float) -> None:
    batch = _batch(payload={"number": number})
    assert validate_ingress_batch(batch.canonical_bytes, batch.request_digest).canonical_bytes == batch.canonical_bytes


@pytest.mark.parametrize("raw_number", [b"9007199254740993", b"-0"])
def test_validator_rejects_noncanonical_binary64_wire_numbers(raw_number: bytes) -> None:
    batch = _batch(payload={"number": float(2**53)})
    raw = batch.canonical_bytes.replace(b"9007199254740992", raw_number)
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
