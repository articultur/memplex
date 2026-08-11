"""G004 v1 可靠同步的纯数据协议与 canonical codec。

这里刻意没有存储、线程、HTTP 或重试逻辑。所有边界都在入站数据进入
repository 前完成，以便 PostgreSQL、Lite 与 HTTP 使用同一份确定性定义。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import uuid
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import total_ordering
from types import MappingProxyType
from typing import Final, Mapping

from memplex.sync_repository import SyncBatchRejected, SyncCursorExpired

_PROTOCOL_VERSION: Final = 1
_MAX_ENTITY_ID_BYTES: Final = 256
_MAX_ENTITY_KEY_BYTES: Final = 1200
_MAX_BATCH_EVENTS: Final = 1000
_MAX_BATCH_BYTES: Final = 4 * 1024 * 1024
_MAX_JAVASCRIPT_SAFE_INTEGER: Final = 2**53 - 1
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_B64URL_RE: Final = re.compile(r"^[A-Za-z0-9_-]+$")
_VERSION_TIME_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


class SyncNodeType(str, Enum):
    FUNCTION = "function"
    FACT = "fact"
    PREFERENCE = "preference"
    OBSERVATION = "observation"
    EDGE = "edge"


class SyncOperation(str, Enum):
    UPSERT = "upsert"
    TOMBSTONE = "tombstone"


@dataclass(frozen=True, slots=True)
class SyncScope:
    """每个同步事件必须携带的 durable RLS identity。"""

    tenant_id: str
    owner_subject_id: str
    workspace_id: str | None
    visibility: str
    agent_id: str | None
    session_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _require_exact_string(self.tenant_id, "tenant_id"))
        object.__setattr__(
            self,
            "owner_subject_id",
            _require_exact_string(self.owner_subject_id, "owner_subject_id"),
        )
        visibility = _require_exact_string(self.visibility, "visibility")
        if visibility not in {"user", "workspace", "session"}:
            raise ValueError("visibility must be user, workspace, or session")
        for field_name in ("workspace_id", "agent_id", "session_id"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _require_exact_string(value, field_name),
                )
        if visibility in {"workspace", "session"} and self.workspace_id is None:
            raise ValueError(f"{visibility} visibility requires workspace_id")
        if visibility == "session" and (self.agent_id is None or self.session_id is None):
            raise ValueError("session visibility requires agent_id and session_id")

    @classmethod
    def from_dict(cls, value: object) -> SyncScope:
        data = _exact_dict(
            value,
            {
                "tenant_id",
                "owner_subject_id",
                "workspace_id",
                "visibility",
                "agent_id",
                "session_id",
            },
            "scope",
        )
        return cls(
            data["tenant_id"],
            data["owner_subject_id"],
            data["workspace_id"],
            data["visibility"],
            data["agent_id"],
            data["session_id"],
        )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tenant_id": self.tenant_id,
            "owner_subject_id": self.owner_subject_id,
            "workspace_id": self.workspace_id,
            "visibility": self.visibility,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
        }


def _require_exact_string(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    _assert_unicode_scalar_string(value, name)
    if not allow_empty and not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _require_exact_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an exact int")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _require_uuid(value: object, name: str) -> str:
    text = _require_exact_string(value, name)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != text:
        raise ValueError(f"{name} must be a canonical UUID")
    return text


def _canonical_json_bytes(value: object) -> bytes:
    """Encode the supported RFC 8785/JCS subset in one deterministic form."""
    _assert_json_value(value)
    return _serialize_jcs(value).encode("utf-8")


def _assert_json_value(value: object) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if abs(value) > _MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ValueError("canonical JSON rejects integers outside JavaScript safe range")
        return
    if type(value) is str:
        _assert_unicode_scalar_string(value, "JSON string")
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects non-finite floats")
        return
    if type(value) is list:
        for item in value:
            _assert_json_value(item)
        return
    if type(value) is dict:
        for key, item in value.items():
            _require_exact_string(key, "JSON object key")
            _assert_unicode_scalar_string(key, "JSON object key")
            _assert_json_value(item)
        return
    raise TypeError("value is not canonical JSON data")


def _assert_unicode_scalar_string(value: str, name: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} contains U+0000")
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError(f"{name} contains a lone UTF-16 surrogate")


def _serialize_jcs(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        if abs(value) > _MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ValueError("canonical JSON rejects integers outside JavaScript safe range")
        return str(value)
    if type(value) is float:
        return _serialize_jcs_float(value)
    if type(value) is str:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    if type(value) is list:
        return "[" + ",".join(_serialize_jcs(item) for item in value) + "]"
    if type(value) is dict:
        keys = sorted(value, key=lambda item: item.encode("utf-16be"))
        return "{" + ",".join(
            _serialize_jcs(key) + ":" + _serialize_jcs(value[key]) for key in keys
        ) + "}"
    raise TypeError("value is not canonical JSON data")


def _serialize_jcs_float(value: float) -> str:
    """ECMAScript-compatible shortest decimal form for finite binary64 values."""
    if not math.isfinite(value):
        raise ValueError("canonical JSON rejects non-finite floats")
    if value == 0:
        return "0"
    rendered = repr(value)
    if "e" not in rendered and "E" not in rendered:
        return rendered[:-2] if rendered.endswith(".0") else rendered
    mantissa, exponent_text = rendered.lower().split("e", maxsplit=1)
    exponent = int(exponent_text)
    magnitude = abs(value)
    if 1e-6 <= magnitude < 1e21:
        sign = "-" if mantissa.startswith("-") else ""
        digits = mantissa.lstrip("-").replace(".", "")
        decimal_position = 1 + exponent
        if decimal_position <= 0:
            return sign + "0." + "0" * (-decimal_position) + digits
        if decimal_position >= len(digits):
            return sign + digits + "0" * (decimal_position - len(digits))
        return sign + digits[:decimal_position] + "." + digits[decimal_position:]
    if mantissa.endswith(".0"):
        mantissa = mantissa[:-2]
    exponent_sign = "+" if exponent >= 0 else ""
    return f"{mantissa}e{exponent_sign}{exponent}"


def _freeze_json(value: object) -> object:
    """Recursively snapshot a validated payload without leaving mutable aliases."""
    if value is None or type(value) in (bool, float, str):
        return value
    if type(value) is int:
        if abs(value) > _MAX_JAVASCRIPT_SAFE_INTEGER:
            raise ValueError("canonical JSON rejects integers outside JavaScript safe range")
        return value
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    raise TypeError("value is not canonical JSON data")


def _thaw_json(value: object) -> object:
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    if isinstance(value, MappingABC):
        return {key: _thaw_json(item) for key, item in value.items()}
    raise TypeError("frozen JSON value has invalid shape")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: object, name: str) -> bytes:
    text = _require_exact_string(value, name)
    if not _B64URL_RE.fullmatch(text):
        raise ValueError(f"{name} is not unpadded base64url")
    try:
        return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise ValueError(f"{name} is not base64url") from exc


def _canonical_time(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise TypeError(f"{name} must be an aware datetime")
    return value.astimezone(timezone.utc)


def _time_to_wire(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _time_from_wire(value: object, name: str) -> datetime:
    text = _require_exact_string(value, name)
    if not _VERSION_TIME_RE.fullmatch(text):
        raise ValueError(f"{name} must use canonical UTC microseconds")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
@total_ordering
class SyncVersion:
    """按 UTC 时间、origin、event id 全序排列的不可变版本键。"""

    occurred_at: datetime
    origin_node_id: str
    event_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _canonical_time(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "origin_node_id", _require_exact_string(self.origin_node_id, "origin_node_id"))
        object.__setattr__(self, "event_id", _require_uuid(self.event_id, "event_id"))

    @classmethod
    def create(cls, occurred_at: datetime, origin_node_id: str, event_id: str) -> SyncVersion:
        return cls(occurred_at, origin_node_id, event_id)

    @classmethod
    def parse(cls, value: object) -> SyncVersion:
        text = _require_exact_string(value, "version")
        parts = text.split(":", maxsplit=1)
        if len(parts) != 2 or parts[0] != "v1":
            raise ValueError("version has invalid shape")
        try:
            decoded = json.loads(_b64url_decode(parts[1], "version payload").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("version payload is not canonical JSON") from exc
        if type(decoded) is not list or len(decoded) != 3:
            raise ValueError("version payload has invalid shape")
        parsed = cls(_time_from_wire(decoded[0], "version timestamp"), decoded[1], decoded[2])
        if str(parsed) != text:
            raise ValueError("version is not canonical")
        return parsed

    def __str__(self) -> str:
        payload = _canonical_json_bytes([_time_to_wire(self.occurred_at), self.origin_node_id, self.event_id])
        return f"v1:{_b64url_encode(payload)}"

    def _order_key(self) -> tuple[datetime, str, str]:
        return self.occurred_at, self.origin_node_id, self.event_id

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SyncVersion):
            return NotImplemented
        return self._order_key() < other._order_key()


@dataclass(frozen=True, slots=True)
class SyncEntityKey:
    """所有 durable node / edge identity 的唯一 canonical codec。"""

    kind: str
    node_id: str | None = None
    source_id: str | None = None
    target_id: str | None = None
    edge_type: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "node":
            node_id = _validate_entity_id(self.node_id, "node_id")
            if any(value is not None for value in (self.source_id, self.target_id, self.edge_type)):
                raise ValueError("node entity key cannot include edge fields")
            object.__setattr__(self, "node_id", node_id)
        elif self.kind == "edge":
            if self.node_id is not None:
                raise ValueError("edge entity key cannot include node_id")
            object.__setattr__(self, "source_id", _validate_entity_id(self.source_id, "source_id"))
            object.__setattr__(self, "target_id", _validate_entity_id(self.target_id, "target_id"))
            object.__setattr__(self, "edge_type", _validate_entity_id(self.edge_type, "edge_type"))
        else:
            raise ValueError("unknown entity key kind")
        if len(str(self).encode("ascii")) > _MAX_ENTITY_KEY_BYTES:
            raise ValueError("entity key exceeds maximum size")

    @classmethod
    def node(cls, node_id: str) -> SyncEntityKey:
        return cls("node", node_id=node_id)

    @classmethod
    def edge(cls, source_id: str, target_id: str, edge_type: str) -> SyncEntityKey:
        return cls("edge", source_id=source_id, target_id=target_id, edge_type=edge_type)

    @property
    def edge_parts(self) -> tuple[str, str, str] | None:
        if self.kind != "edge":
            return None
        return self.source_id, self.target_id, self.edge_type  # type: ignore[return-value]

    @classmethod
    def parse(cls, value: object) -> SyncEntityKey:
        text = _require_exact_string(value, "entity_key")
        if len(text.encode("ascii")) > _MAX_ENTITY_KEY_BYTES:
            raise ValueError("entity key exceeds maximum size")
        parts = text.split(":")
        if len(parts) != 3 or parts[1] != "v1":
            raise ValueError("unknown entity key version")
        raw = _b64url_decode(parts[2], "entity_key payload")
        if parts[0] == "node":
            try:
                node_id = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("node id is not UTF-8") from exc
            parsed = cls.node(node_id)
        elif parts[0] == "edge":
            try:
                raw_value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("edge payload is not canonical JSON") from exc
            if type(raw_value) is not list or len(raw_value) != 3:
                raise ValueError("edge payload must be a three-element list")
            parsed = cls.edge(raw_value[0], raw_value[1], raw_value[2])
        else:
            raise ValueError("unknown entity key kind")
        if str(parsed) != text:
            raise ValueError("entity key is not canonical")
        return parsed

    def __str__(self) -> str:
        if self.kind == "node":
            return f"node:v1:{_b64url_encode(self.node_id.encode('utf-8'))}"  # type: ignore[union-attr]
        raw = _canonical_json_bytes([self.source_id, self.target_id, self.edge_type])
        return f"edge:v1:{_b64url_encode(raw)}"


def _validate_entity_id(value: object, name: str) -> str:
    text = _require_exact_string(value, name)
    if len(text.encode("utf-8")) > _MAX_ENTITY_ID_BYTES:
        raise ValueError(f"{name} exceeds maximum size")
    return text


@dataclass(frozen=True, slots=True)
class SyncEvent:
    protocol_version: int
    event_id: str
    origin_node_id: str
    node_type: SyncNodeType
    entity_key: SyncEntityKey
    operation: SyncOperation
    version: str
    scope: SyncScope
    payload: dict[str, object] | None

    def __post_init__(self) -> None:
        if self.protocol_version != _PROTOCOL_VERSION or type(self.protocol_version) is not int:
            raise ValueError("unsupported protocol_version")
        object.__setattr__(self, "event_id", _require_uuid(self.event_id, "event_id"))
        object.__setattr__(self, "origin_node_id", _require_exact_string(self.origin_node_id, "origin_node_id"))
        if type(self.node_type) is not SyncNodeType or type(self.operation) is not SyncOperation:
            raise TypeError("node_type and operation must be protocol enums")
        if type(self.entity_key) is not SyncEntityKey:
            raise TypeError("entity_key must be SyncEntityKey")
        if type(self.scope) is not SyncScope:
            raise TypeError("scope must be SyncScope")
        version = SyncVersion.parse(self.version)
        if version.origin_node_id != self.origin_node_id or version.event_id != self.event_id:
            raise ValueError("version must bind event id and origin node")
        object.__setattr__(self, "version", str(version))
        is_edge = self.node_type is SyncNodeType.EDGE
        if is_edge != (self.entity_key.kind == "edge"):
            raise ValueError("node_type and entity_key kind must agree")
        if self.operation is SyncOperation.UPSERT and type(self.payload) is not dict:
            raise TypeError("upsert payload must be an exact dict")
        if self.operation is SyncOperation.TOMBSTONE and self.payload is not None:
            raise ValueError("tombstone payload must be None")
        if self.payload is not None:
            if type(self.payload) is not dict:
                raise TypeError("payload must be an exact dict or None")
            _assert_json_value(self.payload)
            object.__setattr__(self, "payload", _freeze_json(self.payload))

    @classmethod
    def from_dict(cls, value: object) -> SyncEvent:
        data = _exact_dict(value, {"protocol_version", "event_id", "origin_node_id", "node_type", "entity_key", "operation", "version", "scope", "payload"}, "event")
        try:
            node_type = SyncNodeType(data["node_type"])
            operation = SyncOperation(data["operation"])
        except (TypeError, ValueError) as exc:
            raise ValueError("event uses an unknown enum value") from exc
        return cls(
            data["protocol_version"], data["event_id"], data["origin_node_id"], node_type,
            SyncEntityKey.parse(data["entity_key"]), operation, data["version"],
            SyncScope.from_dict(data["scope"]), data["payload"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "event_id": self.event_id,
            "origin_node_id": self.origin_node_id,
            "node_type": self.node_type.value,
            "entity_key": str(self.entity_key),
            "operation": self.operation.value,
            "version": self.version,
            "scope": self.scope.to_dict(),
            "payload": None if self.payload is None else _thaw_json(self.payload),
        }


def _exact_dict(value: object, expected: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError(f"{name} must be an exact dict")
    actual = set(value)
    if actual != expected or any(type(key) is not str for key in value):
        raise ValueError(f"{name} fields do not match protocol")
    return value


@dataclass(frozen=True, slots=True)
class SyncBatch:
    protocol_version: int
    batch_id: str
    origin_node_id: str
    events: tuple[SyncEvent, ...]
    _canonical_bytes: bytes = field(init=False, repr=False, compare=False)
    _request_digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.protocol_version != _PROTOCOL_VERSION or type(self.protocol_version) is not int:
            raise SyncBatchRejected("unsupported protocol_version")
        object.__setattr__(self, "batch_id", _require_uuid(self.batch_id, "batch_id"))
        object.__setattr__(self, "origin_node_id", _require_exact_string(self.origin_node_id, "origin_node_id"))
        if type(self.events) is not tuple or not self.events:
            raise SyncBatchRejected("events must be a non-empty tuple")
        if len(self.events) > _MAX_BATCH_EVENTS:
            raise SyncBatchRejected("batch exceeds event limit")
        ids: set[str] = set()
        identities: set[tuple[str, str]] = set()
        tenant_id: str | None = None
        for event in self.events:
            if type(event) is not SyncEvent:
                raise TypeError("batch events must be SyncEvent")
            if event.origin_node_id != self.origin_node_id:
                raise SyncBatchRejected("batch origin must match every event")
            if tenant_id is None:
                tenant_id = event.scope.tenant_id
            elif event.scope.tenant_id != tenant_id:
                raise SyncBatchRejected("batch events must share one tenant scope")
            identity = (str(event.entity_key), event.version)
            if event.event_id in ids or identity in identities:
                raise SyncBatchRejected("batch has duplicate event identity")
            ids.add(event.event_id)
            identities.add(identity)
        canonical_bytes = _canonical_json_bytes(self.to_dict())
        if len(canonical_bytes) > _MAX_BATCH_BYTES:
            raise SyncBatchRejected("batch exceeds byte limit")
        object.__setattr__(self, "_canonical_bytes", canonical_bytes)
        object.__setattr__(self, "_request_digest", hashlib.sha256(canonical_bytes).hexdigest())

    @classmethod
    def from_dict(cls, value: object) -> SyncBatch:
        data = _exact_dict(value, {"protocol_version", "batch_id", "origin_node_id", "events"}, "batch")
        if type(data["events"]) is not list:
            raise TypeError("batch events must be a list")
        return cls(data["protocol_version"], data["batch_id"], data["origin_node_id"], tuple(SyncEvent.from_dict(item) for item in data["events"]))

    @property
    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes

    @property
    def request_digest(self) -> str:
        return self._request_digest

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "batch_id": self.batch_id,
            "origin_node_id": self.origin_node_id,
            "events": [event.to_dict() for event in self.events],
        }


@dataclass(frozen=True, slots=True)
class SyncStreamItem:
    """一个不可跳过的全局 stream sequence 与其事件。"""

    stream_seq: int
    event: SyncEvent

    def __post_init__(self) -> None:
        _require_exact_int(self.stream_seq, "stream_seq", minimum=1)
        if type(self.event) is not SyncEvent:
            raise TypeError("stream item event must be SyncEvent")


@dataclass(frozen=True, slots=True)
@total_ordering
class SyncSnapshotAnchor:
    """snapshot keyset page 的唯一已签名位置。"""

    node_type: SyncNodeType
    entity_key: SyncEntityKey

    def __post_init__(self) -> None:
        if type(self.node_type) is not SyncNodeType or type(self.entity_key) is not SyncEntityKey:
            raise TypeError("snapshot anchor must contain protocol types")
        if (self.node_type is SyncNodeType.EDGE) != (self.entity_key.kind == "edge"):
            raise ValueError("snapshot anchor node type and key kind must agree")

    @classmethod
    def from_event(cls, event: SyncEvent) -> SyncSnapshotAnchor:
        if type(event) is not SyncEvent:
            raise TypeError("snapshot event must be SyncEvent")
        return cls(event.node_type, event.entity_key)

    @classmethod
    def from_dict(cls, value: object) -> SyncSnapshotAnchor:
        data = _exact_dict(value, {"node_type", "entity_key"}, "snapshot anchor")
        try:
            node_type = SyncNodeType(data["node_type"])
        except (TypeError, ValueError) as exc:
            raise ValueError("snapshot anchor uses unknown node type") from exc
        return cls(node_type, SyncEntityKey.parse(data["entity_key"]))

    def to_dict(self) -> dict[str, str]:
        return {"node_type": self.node_type.value, "entity_key": str(self.entity_key)}

    def _order_key(self) -> tuple[str, str]:
        return self.node_type.value, str(self.entity_key)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SyncSnapshotAnchor):
            return NotImplemented
        return self._order_key() < other._order_key()


@dataclass(frozen=True, slots=True)
class SyncCursorClaims:
    version: int
    key_id: str
    tenant_binding: str
    remote_binding: str
    consumer_binding: str
    after_seq: int
    snapshot_seq: int
    snapshot_id: str | None
    snapshot_after: SyncSnapshotAnchor | None
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if self.version != _PROTOCOL_VERSION or type(self.version) is not int:
            raise ValueError("unsupported cursor version")
        for name in ("key_id", "tenant_binding", "remote_binding", "consumer_binding"):
            object.__setattr__(self, name, _require_exact_string(getattr(self, name), name))
        _require_exact_int(self.after_seq, "after_seq")
        _require_exact_int(self.snapshot_seq, "snapshot_seq")
        if self.after_seq > self.snapshot_seq:
            raise ValueError("after_seq cannot exceed snapshot_seq")
        if self.snapshot_id is not None:
            object.__setattr__(self, "snapshot_id", _require_exact_string(self.snapshot_id, "snapshot_id"))
        if (self.snapshot_id is None) != (self.snapshot_after is None):
            raise ValueError("snapshot cursor id and anchor must be both present or absent")
        if self.snapshot_after is not None and type(self.snapshot_after) is not SyncSnapshotAnchor:
            raise TypeError("snapshot_after must be SyncSnapshotAnchor or None")
        issued_at = _canonical_time(self.issued_at, "issued_at")
        expires_at = _canonical_time(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("expires_at must follow issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "key_id": self.key_id,
            "tenant_binding": self.tenant_binding,
            "remote_binding": self.remote_binding,
            "consumer_binding": self.consumer_binding,
            "after_seq": self.after_seq,
            "snapshot_seq": self.snapshot_seq,
            "snapshot_id": self.snapshot_id,
            "snapshot_after": None if self.snapshot_after is None else self.snapshot_after.to_dict(),
            "issued_at": _time_to_wire(self.issued_at),
            "expires_at": _time_to_wire(self.expires_at),
        }

    @classmethod
    def from_dict(cls, value: object) -> SyncCursorClaims:
        data = _exact_dict(value, {"version", "key_id", "tenant_binding", "remote_binding", "consumer_binding", "after_seq", "snapshot_seq", "snapshot_id", "snapshot_after", "issued_at", "expires_at"}, "cursor claims")
        snapshot_after = data["snapshot_after"]
        return cls(
            data["version"], data["key_id"], data["tenant_binding"], data["remote_binding"],
            data["consumer_binding"], data["after_seq"], data["snapshot_seq"], data["snapshot_id"],
            None if snapshot_after is None else SyncSnapshotAnchor.from_dict(snapshot_after),
            _time_from_wire(data["issued_at"], "issued_at"), _time_from_wire(data["expires_at"], "expires_at"),
        )


class SyncCursorCodec:
    """HMAC-SHA256 cursor signer; all decode faults collapse to invalid_cursor。"""

    def __init__(self, active_key_id: str, active_secret: str, previous_keys: Mapping[str, str] | None = None) -> None:
        self._active_key_id = _require_exact_string(active_key_id, "active_key_id")
        self._active_secret = _validate_secret(active_secret, "active_secret")
        if previous_keys is not None and not isinstance(previous_keys, Mapping):
            raise TypeError("previous_keys must be a mapping")
        keys: dict[str, bytes] = {self._active_key_id: self._active_secret}
        for key_id, secret in (previous_keys or {}).items():
            key_id = _require_exact_string(key_id, "previous key id")
            if key_id == self._active_key_id:
                raise ValueError("previous keys cannot replace active key")
            keys[key_id] = _validate_secret(secret, "previous secret")
        self._keys = keys

    def encode(self, claims: SyncCursorClaims) -> str:
        if type(claims) is not SyncCursorClaims:
            raise TypeError("claims must be SyncCursorClaims")
        if claims.key_id != self._active_key_id:
            claims = SyncCursorClaims(
                claims.version, self._active_key_id, claims.tenant_binding, claims.remote_binding,
                claims.consumer_binding, claims.after_seq, claims.snapshot_seq, claims.snapshot_id,
                claims.snapshot_after,
                claims.issued_at, claims.expires_at,
            )
        payload = _canonical_json_bytes(claims.to_dict())
        signature = hmac.new(self._active_secret, payload, hashlib.sha256).digest()
        return f"{_b64url_encode(payload)}.{_b64url_encode(signature)}"

    def decode(
        self,
        token: object,
        *,
        tenant_binding: str,
        remote_binding: str,
        consumer_binding: str,
        now: datetime | None = None,
    ) -> SyncCursorClaims:
        try:
            text = _require_exact_string(token, "cursor")
            parts = text.split(".")
            if len(parts) != 2:
                raise ValueError("invalid cursor")
            payload = _b64url_decode(parts[0], "cursor payload")
            signature = _b64url_decode(parts[1], "cursor signature")
            raw = json.loads(payload.decode("utf-8"))
            claims = SyncCursorClaims.from_dict(raw)
            if _canonical_json_bytes(claims.to_dict()) != payload:
                raise ValueError("noncanonical cursor")
            secret = self._keys.get(claims.key_id)
            if secret is None or not hmac.compare_digest(signature, hmac.new(secret, payload, hashlib.sha256).digest()):
                raise ValueError("invalid signature")
            if claims.tenant_binding != _require_exact_string(tenant_binding, "tenant_binding"):
                raise ValueError("binding mismatch")
            if claims.remote_binding != _require_exact_string(remote_binding, "remote_binding"):
                raise ValueError("binding mismatch")
            if claims.consumer_binding != _require_exact_string(consumer_binding, "consumer_binding"):
                raise ValueError("binding mismatch")
            current = _canonical_time(now or datetime.now(timezone.utc), "now")
            if current >= claims.expires_at:
                raise ValueError("cursor expired")
            return claims
        except SyncCursorExpired:
            raise
        except Exception as exc:
            raise SyncCursorExpired("invalid_cursor") from exc


def _validate_secret(value: object, name: str) -> bytes:
    text = _require_exact_string(value, name)
    raw = text.encode("utf-8")
    if len(raw) < 32:
        raise ValueError(f"{name} must be at least 32 bytes")
    return raw


@dataclass(frozen=True, slots=True)
class SyncReceipt:
    event_id: str
    outcome: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _require_uuid(self.event_id, "event_id"))
        outcome = _require_exact_string(self.outcome, "outcome")
        if outcome not in {"accepted", "duplicate", "rejected_conflict"}:
            raise ValueError("unknown receipt outcome")

    def to_dict(self) -> dict[str, str]:
        return {"event_id": self.event_id, "outcome": self.outcome}


@dataclass(frozen=True, slots=True)
class SyncBatchResult:
    batch_id: str
    request_digest: str
    outcome: str
    receipts: tuple[SyncReceipt, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "batch_id", _require_uuid(self.batch_id, "batch_id"))
        digest = _require_exact_string(self.request_digest, "request_digest")
        if not _DIGEST_RE.fullmatch(digest):
            raise ValueError("request_digest must be SHA-256 hex")
        outcome = _require_exact_string(self.outcome, "outcome")
        if outcome not in {"accepted", "duplicate"}:
            raise ValueError("unknown batch outcome")
        if type(self.receipts) is not tuple or not all(type(item) is SyncReceipt for item in self.receipts):
            raise TypeError("receipts must be a tuple of SyncReceipt")

    def to_dict(self) -> dict[str, object]:
        return {"batch_id": self.batch_id, "request_digest": self.request_digest, "outcome": self.outcome, "receipts": [receipt.to_dict() for receipt in self.receipts]}


@dataclass(frozen=True, slots=True)
class SyncPage:
    items: tuple[SyncStreamItem, ...]
    snapshot_seq: int
    next_after_seq: int
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or not all(type(item) is SyncStreamItem for item in self.items):
            raise TypeError("items must be a tuple of SyncStreamItem")
        _require_exact_int(self.snapshot_seq, "snapshot_seq")
        _require_exact_int(self.next_after_seq, "next_after_seq")
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be bool")
        previous_seq = 0
        for item in self.items:
            if item.stream_seq <= previous_seq or item.stream_seq > self.snapshot_seq:
                raise ValueError("stream items must be strictly ordered within snapshot")
            previous_seq = item.stream_seq
        if self.has_more:
            if not self.items or self.next_after_seq != self.items[-1].stream_seq:
                raise ValueError("has_more page must continue from its final item")
        elif self.next_after_seq != self.snapshot_seq:
            raise ValueError("complete page must advance cursor to snapshot sequence")


@dataclass(frozen=True, slots=True)
class SyncSnapshotPage:
    events: tuple[SyncEvent, ...]
    snapshot_id: str
    next_anchor: SyncSnapshotAnchor | None
    resume_seq: int
    has_more: bool

    def __post_init__(self) -> None:
        if type(self.events) is not tuple or not all(type(event) is SyncEvent for event in self.events):
            raise TypeError("events must be a tuple of SyncEvent")
        object.__setattr__(
            self,
            "snapshot_id",
            _require_exact_string(self.snapshot_id, "snapshot_id"),
        )
        _require_exact_int(self.resume_seq, "resume_seq")
        if type(self.has_more) is not bool:
            raise TypeError("has_more must be bool")
        if self.next_anchor is not None and type(self.next_anchor) is not SyncSnapshotAnchor:
            raise TypeError("next_anchor must be SyncSnapshotAnchor or None")
        anchors = tuple(SyncSnapshotAnchor.from_event(event) for event in self.events)
        if any(left >= right for left, right in zip(anchors, anchors[1:])):
            raise ValueError("snapshot events must be strictly canonical ordered")
        if self.has_more:
            if not anchors or self.next_anchor != anchors[-1]:
                raise ValueError("has_more snapshot must continue from its final anchor")
        elif self.next_anchor is not None:
            raise ValueError("complete snapshot page cannot return a next anchor")


@dataclass(frozen=True, slots=True)
class SyncDelivery:
    target_id: str
    event: SyncEvent
    attempt: int
    lease_id: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "target_id", _require_exact_string(self.target_id, "target_id"))
        if type(self.event) is not SyncEvent:
            raise TypeError("event must be SyncEvent")
        _require_exact_int(self.attempt, "attempt", minimum=1)
        object.__setattr__(self, "lease_id", _require_uuid(self.lease_id, "lease_id"))
        object.__setattr__(self, "lease_expires_at", _canonical_time(self.lease_expires_at, "lease_expires_at"))


@dataclass(frozen=True, slots=True)
class SyncApplyResult:
    applied: int
    duplicate: int
    conflict: int
    cursor_advanced: int

    def __post_init__(self) -> None:
        for name in ("applied", "duplicate", "conflict", "cursor_advanced"):
            _require_exact_int(getattr(self, name), name)

    def to_dict(self) -> dict[str, int]:
        return {"applied": self.applied, "duplicate": self.duplicate, "conflict": self.conflict, "cursor_advanced": self.cursor_advanced}


@dataclass(frozen=True, slots=True)
class SyncStatus:
    pending: int
    leased: int
    delivered: int
    disabled_targets: int
    dead_letters: int

    def __post_init__(self) -> None:
        for name in ("pending", "leased", "delivered", "disabled_targets", "dead_letters"):
            _require_exact_int(getattr(self, name), name)

    def to_dict(self) -> dict[str, int]:
        return {"pending": self.pending, "leased": self.leased, "delivered": self.delivered, "disabled_targets": self.disabled_targets, "dead_letters": self.dead_letters}


@dataclass(frozen=True, slots=True)
class SyncDrainResult:
    drained: bool
    delivered: int
    pending: int
    leased: int
    dead_letters: int
    deadline_exceeded: bool

    def __post_init__(self) -> None:
        if type(self.drained) is not bool or type(self.deadline_exceeded) is not bool:
            raise TypeError("drain flags must be bool")
        for name in ("delivered", "pending", "leased", "dead_letters"):
            _require_exact_int(getattr(self, name), name)

    def to_dict(self) -> dict[str, object]:
        return {"drained": self.drained, "delivered": self.delivered, "pending": self.pending, "leased": self.leased, "dead_letters": self.dead_letters, "deadline_exceeded": self.deadline_exceeded}
