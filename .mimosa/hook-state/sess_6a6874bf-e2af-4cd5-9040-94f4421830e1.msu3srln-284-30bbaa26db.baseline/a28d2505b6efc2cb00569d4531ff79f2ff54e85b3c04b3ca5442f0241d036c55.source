"""受信 ingress 网关：在接触数据库前冻结 Task 1 协议字节。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from memplex.sync_protocol import SyncBatch
from memplex.sync_repository import SyncBatchRejected


class _RawNumber(str):
    pass


_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidatedIngressBatch:
    """仅 strict validator 能创建的不可变入站 envelope。"""

    batch: SyncBatch
    canonical_bytes: bytes
    request_digest: str

    def __init__(self, token: object, batch: SyncBatch) -> None:
        if token is not _TOKEN:
            raise TypeError("ValidatedIngressBatch is validator-owned")
        object.__setattr__(self, "batch", batch)
        object.__setattr__(self, "canonical_bytes", batch.canonical_bytes)
        object.__setattr__(self, "request_digest", batch.request_digest)


def validate_ingress_batch(raw: bytes, request_sha256: str) -> ValidatedIngressBatch:
    """严格验证 UTF-8、exact JSON object 与 Task 1 canonical JCS。"""
    if type(raw) is not bytes or type(request_sha256) is not str:
        raise SyncBatchRejected("invalid ingress request")
    if hashlib.sha256(raw).hexdigest() != request_sha256:
        raise SyncBatchRejected("invalid ingress request")
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys,
            parse_int=_RawNumber, parse_float=_RawNumber,
        )
        value = _normalise_protocol_numbers(value)
        batch = SyncBatch.from_dict(value)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SyncBatchRejected("invalid ingress request") from exc
    if batch.canonical_bytes != raw:
        raise SyncBatchRejected("invalid ingress request")
    return ValidatedIngressBatch(_TOKEN, batch)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _normalise_protocol_numbers(value: Any, *, in_payload: bool = False) -> Any:
    if isinstance(value, _RawNumber):
        if in_payload:
            if any(marker in value for marker in (".", "e", "E")):
                return float(value)
            integer = int(value)
            return float(value) if abs(integer) > 2**53 - 1 else integer
        return int(value)
    if isinstance(value, list):
        return [_normalise_protocol_numbers(item, in_payload=in_payload) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalise_protocol_numbers(item, in_payload=in_payload or key == "payload")
            for key, item in value.items()
        }
    return value
