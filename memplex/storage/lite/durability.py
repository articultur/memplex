"""Crash-consistent, single-writer persistence for the Lite backend.

The two Lite JSON files are one logical record.  This module deliberately
keeps the commit protocol here (rather than in either store) so a caller can
never make one file authoritative by itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from memplex.sync_protocol import (
    SyncBatchResult,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncReceipt,
    SyncVersion,
    _assert_json_value,
)
from memplex.sync_protocol import (
    _require_uuid as _require_uuid_strict,
)

_FORMAT_VERSION = 2
_PAIR_RECORD_KEYS = {
    "generation",
    "transaction_id",
    "memory_digest",
    "changelog_digest",
    "cross_digest",
}
_MEMORY_V2_KEYS = {
    "schema_version",
    "functions",
    "edges",
    "observations",
    "facts",
    "preferences",
    "sync",
}
_MEMORY_OPTIONAL_COLLECTION_KEYS = {"observations", "facts", "preferences"}
_MEMORY_V1_REQUIRED_KEYS = {"schema_version", "functions", "edges"}
_MEMORY_V1_ALLOWED_KEYS = _MEMORY_V1_REQUIRED_KEYS | _MEMORY_OPTIONAL_COLLECTION_KEYS
_SYNC_STATE_KEYS = {
    "tenant_binding",
    "next_stream_seq",
    "retention_floor",
    "compacted_through",
    "outbox",
    "entity_versions",
    "targets",
    "deliveries",
    "inbox",
    "batches",
    "cursors",
    "inbound_cursors",
    "snapshots",
    "snapshot_items",
}
_EMPTY_SYNC_STATE: dict[str, Any] = {
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
_SYNC_OUTBOX_KEYS = {
    "stream_seq",
    "event_id",
    "origin_node_id",
    "node_type",
    "entity_key",
    "operation",
    "version_key",
    "payload",
    "tenant_id",
    "visibility",
    "owner_subject_id",
    "workspace_id",
    "agent_id",
    "session_id",
    "created_at",
}
_SYNC_ENTITY_VERSION_KEYS = {
    "node_type",
    "entity_key",
    "version_key",
    "deleted",
    "event_id",
    "last_stream_seq",
}
_SYNC_TARGET_KEYS = {"target_id", "remote_node_id", "bootstrap_seq", "enabled"}
_SYNC_DELIVERY_KEYS = {
    "target_id",
    "stream_seq",
    "state",
    "attempt_count",
    "next_attempt_at",
    "lease_owner",
    "lease_until",
    "last_error_code",
}
_SYNC_INBOX_KEYS = {"origin_node_id", "event_id", "outcome", "applied_stream_seq"}
_SYNC_BATCH_KEYS = {"batch_id", "request_sha256", "response", "created_at"}
_SYNC_CURSOR_KEYS = {"remote_id", "consumer_id", "after_seq", "updated_at"}
_SYNC_SNAPSHOT_KEYS = {
    "snapshot_id",
    "remote_id",
    "consumer_id",
    "request_id",
    "resume_seq",
    "expires_at",
}
_SYNC_SNAPSHOT_ITEM_KEYS = {"snapshot_id", "node_type", "entity_key", "event"}
_ENVELOPE_KEYS = {"format_version", "generation", "transaction_id", "payload", "peer_digest"}
_JOURNAL_KEYS = {
    "format_version",
    "base_record",
    "base_memory",
    "base_changelog",
    "target",
    "target_record",
}
_JOURNAL_TARGET_KEYS = {"memory", "changelog", "generation", "transaction_id"}
_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class LiteStorageIntegrityError(RuntimeError):
    """A durable Lite pair is incomplete, corrupt, or cannot be locked."""


@dataclass(frozen=True)
class LitePair:
    memory: dict[str, Any]
    changelog: list[dict[str, Any]]
    generation: int
    transaction_id: str


def _canonical(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _process_lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


def _load_fcntl() -> Any:
    try:
        import fcntl

        if not callable(getattr(fcntl, "flock", None)):
            raise AttributeError("flock")
        return fcntl
    except (ImportError, AttributeError) as exc:
        raise LiteStorageIntegrityError("persistent Lite requires POSIX flock") from exc


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _pair_record(pair: LitePair) -> dict[str, Any]:
    memory_digest = _digest(pair.memory)
    changelog_digest = _digest(pair.changelog)
    return {
        "generation": pair.generation,
        "transaction_id": pair.transaction_id,
        "memory_digest": memory_digest,
        "changelog_digest": changelog_digest,
        "cross_digest": _digest(
            {
                "generation": pair.generation,
                "transaction_id": pair.transaction_id,
                "memory_digest": memory_digest,
                "changelog_digest": changelog_digest,
            }
        ),
    }


def _require_generation(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise LiteStorageIntegrityError(f"invalid Lite {label} generation")
    return value


def _require_transaction_id(value: Any, *, label: str) -> str:
    if type(value) is not str or not value:
        raise LiteStorageIntegrityError(f"invalid Lite {label} transaction_id")
    return value


def _require_str(value: Any, *, label: str) -> str:
    if type(value) is not str or not value:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    return value


def _require_uuid(value: Any, *, label: str) -> str:
    try:
        return _require_uuid_strict(_require_str(value, label=label), label)
    except (TypeError, ValueError) as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc


def _require_aware_timestamp(value: Any, *, label: str) -> datetime:
    if type(value) is not str or not value:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        when = datetime.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc
    if when.tzinfo is None or when.utcoffset() is None:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    if when.utcoffset() != timezone.utc.utcoffset(when):
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    canonical = when.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    return when.astimezone(timezone.utc)


def _require_canonical_json(value: Any, *, label: str) -> None:
    try:
        _assert_json_value(value)
    except (TypeError, ValueError) as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc


def _require_sync_version(value: Any, *, label: str, origin_node_id: str, event_id: str) -> None:
    version = _require_str(value, label=label)
    try:
        parsed = SyncVersion.parse(version)
    except (TypeError, ValueError) as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc
    if parsed.origin_node_id != origin_node_id or parsed.event_id != event_id:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")


def _require_node_type(value: Any, *, label: str) -> str:
    node_type = _require_str(value, label=label)
    try:
        SyncNodeType(node_type)
    except (TypeError, ValueError):
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    return node_type


def _require_event_operation(value: Any, *, label: str) -> str:
    operation = _require_str(value, label=label)
    try:
        SyncOperation(operation)
    except (TypeError, ValueError):
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    return operation


def _require_canonical_sync_event(value: Any, *, label: str) -> None:
    try:
        if type(value) is not dict:
            raise TypeError("not a dict")
        _require_canonical_json(value, label=label)
        event = SyncEvent.from_dict(value)
        if event.to_dict() != value:
            raise ValueError("event is not canonical")
    except Exception as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc


def _require_canonical_batch_result(
    value: Any,
    *,
    label: str,
    batch_id: str | None = None,
) -> None:
    if type(value) is not dict:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    _require_exact_keys(value, {"batch_id", "request_digest", "outcome", "receipts"}, label=label)
    response_batch_id = _require_uuid(value["batch_id"], label=f"{label} batch_id")
    if batch_id is not None and response_batch_id != batch_id:
        raise LiteStorageIntegrityError(f"invalid Lite {label} batch_id")
    digest = _require_str(value["request_digest"], label=f"{label} request_digest")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise LiteStorageIntegrityError(f"invalid Lite {label} request_digest")
    if value["outcome"] not in {"accepted", "duplicate"}:
        raise LiteStorageIntegrityError(f"invalid Lite {label} outcome")
    if type(value["receipts"]) is not list:
        raise LiteStorageIntegrityError(f"invalid Lite {label} receipts")
    parsed_receipts: list[SyncReceipt] = []
    for receipt in value["receipts"]:
        if type(receipt) is not dict:
            raise LiteStorageIntegrityError(f"invalid Lite {label} receipts")
        _require_exact_keys(receipt, {"event_id", "outcome"}, label=f"{label} receipt")
        _require_uuid(receipt["event_id"], label=f"{label} receipt event_id")
        if type(receipt["outcome"]) is not str or receipt["outcome"] not in {
            "accepted",
            "duplicate",
            "rejected_conflict",
        }:
            raise LiteStorageIntegrityError(f"invalid Lite {label} receipt outcome")
        parsed_receipts.append(SyncReceipt(receipt["event_id"], receipt["outcome"]))
    try:
        parsed = SyncBatchResult(
            response_batch_id,
            digest,
            value["outcome"],
            tuple(parsed_receipts),
        )
        if parsed.to_dict() != value:
            raise ValueError("batch result is not canonical")
    except Exception as exc:
        raise LiteStorageIntegrityError(f"invalid Lite {label}") from exc


def _require_optional_string(value: Any, *, label: str) -> None:
    if value is None:
        return
    if type(value) is not str:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")


def _require_timestamp(value: Any, *, label: str) -> None:
    if type(value) is not str or not value:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")


def _validate_sync_list_exact_items(items: Any, *, element_keys: set[str], label: str) -> list[dict[str, Any]]:
    if type(items) is not list:
        raise LiteStorageIntegrityError(f"invalid Lite {label}")
    validated: list[dict[str, Any]] = []
    for item in items:
        if type(item) is not dict:
            raise LiteStorageIntegrityError(f"invalid Lite {label} item")
        _require_exact_keys(item, element_keys, label=f"{label} item")
        validated.append(item)  # copy-by-ref keeps local behavior stable for tests.
    return validated


def _empty_sync_state() -> dict[str, Any]:
    return {key: (list(value) if isinstance(value, list) else value) for key, value in _EMPTY_SYNC_STATE.items()}



def _validate_sync_outbox_items(
    items: list,
    *,
    label: str,
    tenant_binding: Any,
    seen_event_ids: set,
    outbox_sequences: set,
    previous_stream_seq: int,
) -> int:
    """Validate outbox events in stream order; returns the last stream_seq."""
    for item in items:
        event_id = _require_uuid(item["event_id"], label="outbox event_id")
        stream_seq = _require_generation(item["stream_seq"], label="outbox stream_seq")
        if stream_seq < 1 or stream_seq <= previous_stream_seq:
            raise LiteStorageIntegrityError("invalid Lite outbox item")
        previous_stream_seq = stream_seq
        origin_node_id = _require_str(item["origin_node_id"], label="outbox origin_node_id")
        node_type = _require_node_type(item["node_type"], label="outbox node_type")
        entity_key = _require_str(item["entity_key"], label="outbox entity_key")
        try:
            parsed_entity_key = SyncEntityKey.parse(entity_key)
        except (TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite outbox entity_key") from exc
        operation = _require_event_operation(item["operation"], label="outbox operation")
        version_key = _require_str(item["version_key"], label="outbox version_key")
        _require_sync_version(
            version_key,
            label="outbox version_key",
            origin_node_id=origin_node_id,
            event_id=event_id,
        )
        tenant_id = _require_str(item["tenant_id"], label="outbox tenant_id")
        if tenant_binding is None or tenant_id != tenant_binding:
            raise LiteStorageIntegrityError("invalid Lite outbox tenant binding")
        owner_subject_id = _require_str(
            item["owner_subject_id"], label="outbox owner_subject_id"
        )
        workspace_id = item["workspace_id"]
        agent_id = item["agent_id"]
        session_id = item["session_id"]
        _require_optional_string(workspace_id, label="outbox workspace_id")
        _require_optional_string(agent_id, label="outbox agent_id")
        _require_optional_string(session_id, label="outbox session_id")
        created_at = _require_aware_timestamp(item["created_at"], label="outbox created_at")
        try:
            version_time = SyncVersion.parse(version_key).occurred_at
        except (TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite outbox version_key") from exc
        if created_at != version_time:
            raise LiteStorageIntegrityError("invalid Lite outbox created_at")
        event = {
            "protocol_version": 1,
            "event_id": event_id,
            "origin_node_id": origin_node_id,
            "node_type": node_type,
            "entity_key": str(parsed_entity_key),
            "operation": operation,
            "version": version_key,
            "scope": {
                "tenant_id": tenant_id,
                "owner_subject_id": owner_subject_id,
                "workspace_id": workspace_id,
                "visibility": item["visibility"],
                "agent_id": agent_id,
                "session_id": session_id,
            },
            "payload": item["payload"],
        }
        _require_canonical_sync_event(event, label="outbox event")
        if event_id in seen_event_ids or stream_seq in outbox_sequences:
            raise LiteStorageIntegrityError("invalid Lite outbox duplicates")
        seen_event_ids.add(event_id)
        outbox_sequences.add(stream_seq)
    return previous_stream_seq

def _validate_sync_state(payload: Any, *, label: str) -> None:
    if type(payload) is not dict:
        raise LiteStorageIntegrityError(f"invalid Lite {label} sync payload")
    # Pre-separation v2 pairs did not have ``inbound_cursors``.  Accept that
    # one exact historical shape so LiteMemoryStore can upgrade it under the
    # pair journal; every other missing or future field still fails closed.
    legacy_keys = _SYNC_STATE_KEYS - {"inbound_cursors"}
    payload_keys = set(payload)
    if payload_keys != _SYNC_STATE_KEYS and payload_keys != legacy_keys:
        raise LiteStorageIntegrityError(f"invalid Lite {label} sync payload schema")
    tenant_binding = payload["tenant_binding"]
    if tenant_binding is not None:
        _require_str(tenant_binding, label=f"{label} tenant_binding")
    _require_generation(payload["next_stream_seq"], label=f"{label} next_stream_seq")
    if payload["next_stream_seq"] < 1:
        raise LiteStorageIntegrityError(f"invalid Lite {label} next_stream_seq")
    _require_generation(payload["retention_floor"], label=f"{label} retention_floor")
    _require_generation(payload["compacted_through"], label=f"{label} compacted_through")
    if payload["compacted_through"] != payload["retention_floor"]:
        raise LiteStorageIntegrityError(f"invalid Lite {label} compacted_through")

    outbox = _validate_sync_list_exact_items(payload["outbox"], element_keys=_SYNC_OUTBOX_KEYS, label="outbox")
    entity_versions = _validate_sync_list_exact_items(payload["entity_versions"], element_keys=_SYNC_ENTITY_VERSION_KEYS, label="entity_versions")
    targets = _validate_sync_list_exact_items(payload["targets"], element_keys=_SYNC_TARGET_KEYS, label="targets")
    deliveries = _validate_sync_list_exact_items(payload["deliveries"], element_keys=_SYNC_DELIVERY_KEYS, label="deliveries")
    inbox = _validate_sync_list_exact_items(payload["inbox"], element_keys=_SYNC_INBOX_KEYS, label="inbox")
    batches = _validate_sync_list_exact_items(payload["batches"], element_keys=_SYNC_BATCH_KEYS, label="batches")
    cursors = _validate_sync_list_exact_items(payload["cursors"], element_keys=_SYNC_CURSOR_KEYS, label="cursors")
    inbound_cursors = _validate_sync_list_exact_items(
        payload.get("inbound_cursors", []),
        element_keys=_SYNC_CURSOR_KEYS,
        label="inbound cursors",
    )
    snapshots = _validate_sync_list_exact_items(payload["snapshots"], element_keys=_SYNC_SNAPSHOT_KEYS, label="snapshots")
    snapshot_items = _validate_sync_list_exact_items(payload["snapshot_items"], element_keys=_SYNC_SNAPSHOT_ITEM_KEYS, label="snapshot_items")

    seen_event_ids: set[str] = set()
    outbox_sequences: set[int] = set()
    previous_stream_seq = 0
    previous_stream_seq = _validate_sync_outbox_items(
        outbox,
        label=label,
        tenant_binding=tenant_binding,
        seen_event_ids=seen_event_ids,
        outbox_sequences=outbox_sequences,
        previous_stream_seq=previous_stream_seq,
    )

    if outbox_sequences and payload["next_stream_seq"] <= max(outbox_sequences):
        raise LiteStorageIntegrityError("invalid Lite next_stream_seq")
    expected_sequences = set(
        range(payload["retention_floor"] + 1, payload["next_stream_seq"])
    )
    if outbox_sequences != expected_sequences:
        raise LiteStorageIntegrityError("invalid Lite outbox sequence continuity")

    _validate_sync_entity_versions_items(entity_versions,
        label=label)

    target_ids = _validate_sync_targets_items(targets,
        payload=payload,
        label=label)

    _validate_sync_deliveries_items(deliveries,
        label=label,
        outbox_sequences=outbox_sequences,
        target_ids=target_ids)

    _validate_sync_inbox_items(inbox,
        label=label)

    _validate_sync_batches_items(batches,
        label=label)

    _validate_sync_cursors_items(cursors,
        payload=payload,
        label=label)

    _validate_sync_inbound_cursors_items(inbound_cursors,
        label=label)

    snapshot_ids, snapshot_requests = _validate_sync_snapshots_items(snapshots,
        payload=payload,
        label=label)

    _validate_sync_snapshot_items_items(snapshot_items,
        label=label,
        tenant_binding=tenant_binding,
        snapshot_ids=snapshot_ids,
    )


def _require_format_version(value: Any, *, label: str) -> None:
    if type(value) is not int or value != _FORMAT_VERSION:
        raise LiteStorageIntegrityError(f"unsupported or invalid Lite {label} format_version")


def _require_exact_keys(value: Any, keys: set[str], *, label: str) -> None:
    if type(value) is not dict or set(value) != keys:
        raise LiteStorageIntegrityError(f"invalid Lite {label} schema")


def _validate_memory_payload(memory: Any, *, allow_legacy_missing_schema: bool = False) -> None:
    """Accept only the frozen v2 shape or the narrow historical v1 shapes."""
    if type(memory) is not dict:
        raise LiteStorageIntegrityError("invalid Lite memory payload")
    keys = set(memory)
    if "schema_version" not in memory:
        # Pre-schema raw files may contain only the historic typed collections;
        # anything outside this fixed whitelist is never a compatibility path.
        if not (
            allow_legacy_missing_schema
            and {"functions", "edges"} <= keys
            <= ({"functions", "edges"} | _MEMORY_OPTIONAL_COLLECTION_KEYS)
        ):
            raise LiteStorageIntegrityError("missing or unknown Lite memory payload schema")
    else:
        schema_version = memory["schema_version"]
        if type(schema_version) is int and schema_version == 2:
            _require_exact_keys(memory, _MEMORY_V2_KEYS, label="memory payload")
            _validate_sync_state(memory["sync"], label="memory")
        elif type(schema_version) is int and schema_version == 1:
            if not _MEMORY_V1_REQUIRED_KEYS <= keys or not keys <= _MEMORY_V1_ALLOWED_KEYS:
                raise LiteStorageIntegrityError("invalid Lite memory payload collection keys")
        else:
            raise LiteStorageIntegrityError("unsupported or invalid Lite memory payload schema")
    collection_names = (
        _MEMORY_V2_KEYS - {"schema_version", "sync"}
        if memory.get("schema_version") == 2
        else {"functions", "edges"} | (keys & _MEMORY_OPTIONAL_COLLECTION_KEYS)
    )
    for name in collection_names:
        if type(memory[name]) is not list:
            raise LiteStorageIntegrityError(f"invalid Lite memory payload collection: {name}")


def _require_digest(value: Any, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise LiteStorageIntegrityError(f"invalid Lite {label} digest")
    return value


def _validated_pair_record(value: Any, *, label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _PAIR_RECORD_KEYS:
        raise LiteStorageIntegrityError(f"invalid Lite {label} record schema")
    return {
        "generation": _require_generation(value["generation"], label=label),
        "transaction_id": _require_transaction_id(value["transaction_id"], label=label),
        "memory_digest": _require_digest(value["memory_digest"], label=label),
        "changelog_digest": _require_digest(value["changelog_digest"], label=label),
        "cross_digest": _require_digest(value["cross_digest"], label=label),
    }


def _validate_transition(base: LitePair, target: LitePair) -> None:
    if type(base.memory) is not dict or type(base.changelog) is not list:
        raise LiteStorageIntegrityError("invalid Lite base pair")
    if type(target.memory) is not dict or type(target.changelog) is not list:
        raise LiteStorageIntegrityError("invalid Lite target pair")
    base_generation = _require_generation(base.generation, label="base")
    target_generation = _require_generation(target.generation, label="target")
    base_transaction = _require_transaction_id(base.transaction_id, label="base")
    target_transaction = _require_transaction_id(target.transaction_id, label="target")
    if target_generation != base_generation + 1 or target_transaction == base_transaction:
        raise LiteStorageIntegrityError("invalid Lite journal generation/transaction transition")


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_and_fsync(path: Path, value: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as fh:
            fh.write(_canonical_json(value))
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        # A failed write/fsync must not leak the hidden tmp file: repeated
        # retries on a full disk would otherwise accumulate them.
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _replace_and_fsync(path: Path, value: Any) -> None:
    tmp = _write_and_fsync(path, value)
    try:
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        tmp.unlink(missing_ok=True)


class LiteDurability:
    """Own the pair envelope, process lock, flock, and recovery protocol."""

    def __init__(
        self,
        memory_path: Path,
        changelog_path: Path,
        *,
        semantic_validator: Optional[Callable[[LitePair], Any]] = None,
    ) -> None:
        self._memory_path = _canonical(memory_path)
        self._changelog_path = _canonical(changelog_path)
        if self._memory_path.parent != self._changelog_path.parent:
            raise LiteStorageIntegrityError("Lite memory and changelog must share a directory")
        # ``x.json`` owns ``x.lock`` / ``x.journal.json``.  The basename is
        # intentional: two sibling custom stores never share hidden generic
        # sidecars, while the conventional memory.json remains intuitive.
        self._lock_path = self._memory_path.with_name(f"{self._memory_path.stem}.lock")
        self._journal_path = self._memory_path.with_name(
            f"{self._memory_path.stem}.journal.json"
        )
        self._local = threading.local()
        self._poisoned = False
        self._semantic_validator = semantic_validator
        # Fail at persistent construction, not module import.  This is also a
        # useful early guard for factories which historically hid this error.
        try:
            _load_fcntl()
        except (ImportError, AttributeError) as exc:
            raise LiteStorageIntegrityError("persistent Lite requires POSIX flock") from exc

    def set_semantic_validator(self, validator: Callable[[LitePair], Any]) -> None:
        """Install store-owned model decoding before authoritative I/O."""
        self._semantic_validator = validator

    def _validate_semantics(self, pair: LitePair) -> None:
        if self._semantic_validator is None:
            return
        try:
            self._semantic_validator(pair)
        except LiteStorageIntegrityError:
            raise
        except Exception as exc:
            raise LiteStorageIntegrityError("invalid Lite authoritative payload") from exc

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        if self._poisoned:
            raise LiteStorageIntegrityError("Lite instance is poisoned after ambiguous durable decision")
        depth = int(getattr(self._local, "depth", 0))
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        fcntl = _load_fcntl()
        lock = _process_lock_for(self._memory_path)
        with lock:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    self._local.depth = 1
                    yield
                finally:
                    self._local.depth = 0
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _envelopes(self, pair: LitePair) -> tuple[dict[str, Any], dict[str, Any]]:
        _validate_memory_payload(
            pair.memory, allow_legacy_missing_schema=pair.transaction_id == "legacy"
        )
        memory_digest = _digest(pair.memory)
        changelog_digest = _digest(pair.changelog)
        common = {
            "format_version": _FORMAT_VERSION,
            "generation": pair.generation,
            "transaction_id": pair.transaction_id,
        }
        return (
            {**common, "payload": pair.memory, "peer_digest": changelog_digest},
            {**common, "payload": pair.changelog, "peer_digest": memory_digest},
        )

    @staticmethod
    def _read(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise LiteStorageIntegrityError(f"invalid Lite persistence file: {path.name}") from exc

    def _decode_pair(self, memory_doc: Any, changelog_doc: Any) -> LitePair:
        # Legacy is accepted only as an exact *pair*, allowing an atomic first
        # upgrade.  A legacy memory file without changelog is ambiguous.
        memory_is_envelope = isinstance(memory_doc, dict) and "format_version" in memory_doc
        changelog_is_envelope = isinstance(changelog_doc, dict) and "format_version" in changelog_doc
        if not memory_is_envelope and not changelog_is_envelope:
            if type(changelog_doc) is not list:
                raise LiteStorageIntegrityError("legacy Lite pair has invalid shape")
            _validate_memory_payload(memory_doc, allow_legacy_missing_schema=True)
            return LitePair(copy.deepcopy(memory_doc), copy.deepcopy(changelog_doc), 0, "legacy")
        if not memory_is_envelope or not changelog_is_envelope:
            raise LiteStorageIntegrityError("Lite pair generation mismatch without journal")
        try:
            _require_exact_keys(memory_doc, _ENVELOPE_KEYS, label="memory envelope")
            _require_exact_keys(changelog_doc, _ENVELOPE_KEYS, label="changelog envelope")
            _require_format_version(memory_doc["format_version"], label="memory envelope")
            _require_format_version(changelog_doc["format_version"], label="changelog envelope")
            memory_generation = _require_generation(memory_doc["generation"], label="memory envelope")
            changelog_generation = _require_generation(changelog_doc["generation"], label="changelog envelope")
            memory_tx = _require_transaction_id(memory_doc["transaction_id"], label="memory envelope")
            changelog_tx = _require_transaction_id(changelog_doc["transaction_id"], label="changelog envelope")
            if memory_generation != changelog_generation or memory_tx != changelog_tx:
                raise LiteStorageIntegrityError("Lite pair generation mismatch without journal")
            if memory_doc["peer_digest"] != _digest(changelog_doc["payload"]) or changelog_doc["peer_digest"] != _digest(memory_doc["payload"]):
                raise LiteStorageIntegrityError("Lite pair cross-digest mismatch without journal")
            if type(changelog_doc["payload"]) is not list:
                raise TypeError("payload")
            _validate_memory_payload(memory_doc["payload"])
            return LitePair(
                copy.deepcopy(memory_doc["payload"]),
                copy.deepcopy(changelog_doc["payload"]),
                memory_generation,
                memory_tx,
            )
        except LiteStorageIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite pair envelope") from exc

    def _recover_locked(self) -> None:
        if not self._journal_path.exists():
            return
        journal = self._read(self._journal_path)
        try:
            _require_exact_keys(journal, _JOURNAL_KEYS, label="durable journal")
            _require_format_version(journal["format_version"], label="journal")
            target = journal["target"]
            _require_exact_keys(target, _JOURNAL_TARGET_KEYS, label="journal target")
            pair = LitePair(
                target["memory"],
                target["changelog"],
                _require_generation(target["generation"], label="journal target"),
                _require_transaction_id(target["transaction_id"], label="journal target"),
            )
            if type(pair.changelog) is not list:
                raise TypeError("journal target payload")
            _validate_memory_payload(pair.memory)
            expected = _validated_pair_record(journal["target_record"], label="journal target")
            if _canonical_json(expected) != _canonical_json(_pair_record(pair)):
                raise ValueError("digest")
            base_record = _validated_pair_record(journal["base_record"], label="journal base")
        except (KeyError, TypeError, ValueError, LiteStorageIntegrityError) as exc:
            raise LiteStorageIntegrityError("invalid Lite durable journal") from exc
        try:
            base_pair = LitePair(
                memory=journal["base_memory"],
                changelog=journal["base_changelog"],
                generation=_require_generation(base_record["generation"], label="journal base"),
                transaction_id=_require_transaction_id(base_record["transaction_id"], label="journal base"),
            )
        except (KeyError, TypeError, ValueError, LiteStorageIntegrityError) as exc:
            raise LiteStorageIntegrityError("invalid Lite durable journal") from exc
        if _canonical_json(_pair_record(base_pair)) != _canonical_json(base_record):
            raise LiteStorageIntegrityError("invalid Lite durable journal")
        if type(base_pair.changelog) is not list:
            raise LiteStorageIntegrityError("invalid Lite durable journal")
        _validate_memory_payload(
            base_pair.memory, allow_legacy_missing_schema=base_pair.transaction_id == "legacy"
        )
        try:
            _validate_transition(base_pair, pair)
        except LiteStorageIntegrityError as exc:
            raise LiteStorageIntegrityError("invalid Lite durable journal") from exc
        # Both journal records are authority candidates; never forward a
        # target across a semantically corrupt base side merely because the
        # target itself decodes.
        self._validate_semantics(base_pair)
        self._validate_semantics(pair)
        base_memory, base_changelog = self._envelopes(base_pair)
        memory_doc, changelog_doc = self._envelopes(pair)
        # A journal may only complete its own four side combinations:
        # base/base, target/base, base/target, target/target.  We compare
        # each side even when the other is corrupt so an unrelated document
        # cannot be overwritten merely because `_decode_pair` failed.
        legacy_base = base_record["transaction_id"] == "legacy"

        def _matches(document: Any, base_doc: Any, target_doc: Any, base_payload: Any) -> bool:
            encoded = _canonical_json(document)
            return encoded == _canonical_json(base_doc) or encoded == _canonical_json(target_doc) or (
                legacy_base and encoded == _canonical_json(base_payload)
            )

        memory_exists = self._memory_path.exists()
        changelog_exists = self._changelog_path.exists()
        initial_empty = base_record["transaction_id"] == "empty" and base_record["generation"] == 0
        if memory_exists:
            if not _matches(self._read(self._memory_path), base_memory, memory_doc, journal.get("base_memory")):
                raise LiteStorageIntegrityError("stale or foreign Lite journal cannot overwrite pair")
        elif not initial_empty:
            raise LiteStorageIntegrityError("stale or foreign Lite journal cannot overwrite pair")
        if changelog_exists:
            if not _matches(self._read(self._changelog_path), base_changelog, changelog_doc, journal.get("base_changelog")):
                raise LiteStorageIntegrityError("stale or foreign Lite journal cannot overwrite pair")
        elif not initial_empty:
            raise LiteStorageIntegrityError("stale or foreign Lite journal cannot overwrite pair")
        _replace_and_fsync(self._memory_path, memory_doc)
        _replace_and_fsync(self._changelog_path, changelog_doc)
        _fsync_dir(self._memory_path.parent)
        self._journal_path.unlink(missing_ok=True)
        _fsync_dir(self._memory_path.parent)

    def _load_authoritative_locked(self) -> LitePair:
        self._recover_locked()
        memory_exists, changelog_exists = self._memory_path.exists(), self._changelog_path.exists()
        if not memory_exists and not changelog_exists:
            return LitePair(
                {
                    "schema_version": 2,
                    "functions": [],
                    "edges": [],
                    "observations": [],
                    "facts": [],
                    "preferences": [],
                    "sync": _empty_sync_state(),
                },
                [],
                0,
                "empty",
            )
        if memory_exists != changelog_exists:
            raise LiteStorageIntegrityError("Lite memory/changelog pair is incomplete without journal")
        pair = self._decode_pair(self._read(self._memory_path), self._read(self._changelog_path))
        self._validate_semantics(pair)
        return pair

    def load_authoritative(self) -> LitePair:
        with self.writer_lock():
            return self._load_authoritative_locked()

    # Hooks are intentionally named stage boundaries for deterministic tests.
    def before_journal_durable_publish(self) -> None:
        return None

    def after_journal_rename_and_parent_dir_fsync(self) -> None:
        return None

    def after_memory_replace(self) -> None:
        return None

    def after_changelog_replace(self) -> None:
        return None

    def after_journal_unlink(self) -> None:
        return None

    def after_final_parent_dir_fsync(self) -> None:
        return None

    def commit_locked(self, base: LitePair, target: LitePair) -> LitePair:
        current = self._load_authoritative_locked()
        if current != base:
            raise LiteStorageIntegrityError("Lite authoritative state changed before commit")
        _validate_memory_payload(
            base.memory, allow_legacy_missing_schema=base.transaction_id == "legacy"
        )
        _validate_memory_payload(target.memory)
        _validate_transition(base, target)
        self._validate_semantics(target)
        memory_doc, changelog_doc = self._envelopes(target)
        journal = {
            "format_version": _FORMAT_VERSION,
            "base_record": _pair_record(base),
            "base_memory": base.memory,
            "base_changelog": base.changelog,
            "target": {"memory": target.memory, "changelog": target.changelog, "generation": target.generation, "transaction_id": target.transaction_id},
            "target_record": _pair_record(target),
        }
        self.before_journal_durable_publish()
        tmp = _write_and_fsync(self._journal_path, journal)
        renamed = False
        try:
            os.replace(tmp, self._journal_path)
            renamed = True
            _fsync_dir(self._journal_path.parent)
        except OSError:
            if renamed:
                # Directory fsync can fail after the rename reached media;
                # this instance cannot distinguish the decision outcome.
                self._poisoned = True
            raise
        finally:
            tmp.unlink(missing_ok=True)
        self.after_journal_rename_and_parent_dir_fsync()
        _replace_and_fsync(self._memory_path, memory_doc)
        self.after_memory_replace()
        _replace_and_fsync(self._changelog_path, changelog_doc)
        self.after_changelog_replace()
        _fsync_dir(self._memory_path.parent)
        self._journal_path.unlink(missing_ok=True)
        self.after_journal_unlink()
        _fsync_dir(self._memory_path.parent)
        self.after_final_parent_dir_fsync()
        return target

def _validate_sync_entity_versions_items(items: list, *,
    label: Any,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        node_type = _require_node_type(item["node_type"], label="entity version node_type")
        entity_key = _require_str(item["entity_key"], label="entity version entity_key")
        try:
            parsed_entity_key = SyncEntityKey.parse(entity_key)
        except (TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite entity version entity_key") from exc
        if (node_type == "edge") != (parsed_entity_key.kind == "edge"):
            raise LiteStorageIntegrityError("invalid Lite entity version entity_key")
        event_id = _require_uuid(item["event_id"], label="entity version event_id")
        version_key = _require_str(item["version_key"], label="entity version version_key")
        try:
            parsed_version = SyncVersion.parse(version_key)
        except (TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite entity version version_key") from exc
        if parsed_version.event_id != event_id:
            raise LiteStorageIntegrityError("invalid Lite entity version version_key")
        if type(item["deleted"]) is not bool:
            raise LiteStorageIntegrityError("invalid Lite entity version deleted")
        if type(item["last_stream_seq"]) is not int or item["last_stream_seq"] < 1:
            raise LiteStorageIntegrityError("invalid Lite entity version last_stream_seq")
        key = (node_type, entity_key)
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite entity version duplicates")
        seen.add(key)


def _validate_sync_targets_items(items: list, *,
    payload: Any,
    label: Any,
) -> set[str]:
    """Validate every item of the sync collection."""
    target_ids: set[str] = set()
    seen: set[Any] = set()
    for item in items:
        target_id = _require_str(item["target_id"], label="target target_id")
        _require_str(item["remote_node_id"], label="target remote_node_id")
        _require_generation(item["bootstrap_seq"], label="sync target bootstrap_seq")
        if item["bootstrap_seq"] >= payload["next_stream_seq"]:
            raise LiteStorageIntegrityError("invalid Lite sync target bootstrap_seq")
        if type(item["enabled"]) is not bool:
            raise LiteStorageIntegrityError("invalid Lite sync target")
        if target_id in seen:
            raise LiteStorageIntegrityError("invalid Lite sync target duplicates")
        seen.add(target_id)
        target_ids.add(target_id)
    return target_ids


def _validate_sync_deliveries_items(items: list, *,
    label: Any,
    outbox_sequences: Any,
    target_ids: set,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        if type(item["state"]) is not str or item["state"] not in {"pending", "leased", "delivered", "dead_letter"}:
            raise LiteStorageIntegrityError("invalid Lite delivery state")
        target_id = _require_str(item["target_id"], label="delivery target_id")
        if target_id not in target_ids:
            raise LiteStorageIntegrityError("invalid Lite delivery target")
        _require_generation(item["stream_seq"], label="delivery stream_seq")
        if item["stream_seq"] < 1 or item["stream_seq"] not in outbox_sequences:
            raise LiteStorageIntegrityError("invalid Lite delivery stream_seq")
        if type(item["attempt_count"]) is not int or item["attempt_count"] < 0:
            raise LiteStorageIntegrityError("invalid Lite delivery attempt_count")
        _require_optional_string(item["lease_owner"], label="delivery lease_owner")
        if item["lease_until"] is not None:
            _require_aware_timestamp(item["lease_until"], label="delivery lease_until")
        _require_optional_string(item["last_error_code"], label="delivery last_error_code")
        _require_aware_timestamp(item["next_attempt_at"], label="delivery next_attempt_at")
        if item["state"] == "leased":
            if item["lease_owner"] is None or item["lease_until"] is None:
                raise LiteStorageIntegrityError("invalid Lite leased delivery")
        elif item["lease_owner"] is not None or item["lease_until"] is not None:
            raise LiteStorageIntegrityError("invalid Lite delivery lease state")
        key = (item["target_id"], item["stream_seq"])
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite delivery duplicates")
        seen.add(key)


def _validate_sync_inbox_items(items: list, *,
    label: Any,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        _require_str(item["origin_node_id"], label="inbox origin_node_id")
        _require_uuid(item["event_id"], label="inbox event_id")
        if type(item["outcome"]) is not str or item["outcome"] not in {
            "accepted",
            "duplicate",
            "rejected_conflict",
        }:
            raise LiteStorageIntegrityError("invalid Lite inbox outcome")
        if item["applied_stream_seq"] is not None and (type(item["applied_stream_seq"]) is not int or item["applied_stream_seq"] < 0):
            raise LiteStorageIntegrityError("invalid Lite inbox applied_stream_seq")
        key = (item["origin_node_id"], item["event_id"])
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite inbox duplicates")
        seen.add(key)


def _validate_sync_batches_items(items: list, *,
    label: Any,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        batch_id = _require_uuid(item["batch_id"], label="batch batch_id")
        if type(item["request_sha256"]) is not str or len(item["request_sha256"]) != 64 or not all(ch in "0123456789abcdef" for ch in item["request_sha256"]):
            raise LiteStorageIntegrityError("invalid Lite batch request_sha256")
        _require_canonical_batch_result(
            item["response"], label="batch response", batch_id=batch_id
        )
        if item["response"]["request_digest"] != item["request_sha256"]:
            raise LiteStorageIntegrityError("invalid Lite batch response digest")
        _require_aware_timestamp(item["created_at"], label="batch created_at")
        if batch_id in seen:
            raise LiteStorageIntegrityError("invalid Lite batch duplicates")
        seen.add(batch_id)


def _validate_sync_cursors_items(items: list, *,
    payload: Any,
    label: Any,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        _require_str(item["remote_id"], label="cursor remote_id")
        _require_str(item["consumer_id"], label="cursor consumer_id")
        _require_generation(item["after_seq"], label="cursor after_seq")
        _require_aware_timestamp(item["updated_at"], label="cursor updated_at")
        if (
            item["after_seq"] < payload["retention_floor"]
            or item["after_seq"] >= payload["next_stream_seq"]
        ):
            raise LiteStorageIntegrityError("invalid Lite cursor after_seq")
        key = (item["remote_id"], item["consumer_id"])
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite cursor duplicates")
        seen.add(key)


def _validate_sync_inbound_cursors_items(items: list, *,
    label: Any,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        remote_id = _require_str(item["remote_id"], label="inbound cursor remote_id")
        consumer_id = _require_str(
            item["consumer_id"], label="inbound cursor consumer_id"
        )
        _require_generation(item["after_seq"], label="inbound cursor after_seq")
        _require_aware_timestamp(
            item["updated_at"], label="inbound cursor updated_at"
        )
        key = (remote_id, consumer_id)
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite inbound cursor duplicates")
        seen.add(key)


def _validate_sync_snapshots_items(items: list, *,
    payload: Any,
    label: Any,
) -> tuple[set, set]:
    """Validate every item of the sync collection."""
    snapshot_ids: set[str] = set()
    snapshot_requests: set[tuple[str, str, str]] = set()
    seen: set[Any] = set()
    for item in items:
        snapshot_id = _require_uuid(item["snapshot_id"], label="snapshot snapshot_id")
        remote_id = _require_str(item["remote_id"], label="snapshot remote_id")
        consumer_id = _require_str(item["consumer_id"], label="snapshot consumer_id")
        request_id = _require_str(item["request_id"], label="snapshot request_id")
        _require_generation(item["resume_seq"], label="snapshot resume_seq")
        if (
            item["resume_seq"] < payload["retention_floor"]
            or item["resume_seq"] >= payload["next_stream_seq"]
        ):
            raise LiteStorageIntegrityError("invalid Lite snapshot resume_seq")
        _require_aware_timestamp(item["expires_at"], label="snapshot expires_at")
        request_key = (remote_id, consumer_id, request_id)
        if snapshot_id in seen or request_key in snapshot_requests:
            raise LiteStorageIntegrityError("invalid Lite snapshot duplicates")
        seen.add(snapshot_id)
        snapshot_ids.add(snapshot_id)
        snapshot_requests.add(request_key)
    return snapshot_ids, snapshot_requests


def _validate_sync_snapshot_items_items(items: list, *,
    label: Any,
    tenant_binding: Any,
    snapshot_ids: set,
) -> None:
    """Validate every item of the sync collection."""
    seen: set[Any] = set()
    for item in items:
        snapshot_id = _require_uuid(item["snapshot_id"], label="snapshot item snapshot_id")
        if snapshot_id not in snapshot_ids:
            raise LiteStorageIntegrityError("invalid Lite snapshot item snapshot_id")
        node_type = _require_node_type(item["node_type"], label="snapshot item node_type")
        entity_key = _require_str(item["entity_key"], label="snapshot item entity_key")
        try:
            parsed_entity_key = SyncEntityKey.parse(entity_key)
        except (TypeError, ValueError) as exc:
            raise LiteStorageIntegrityError("invalid Lite snapshot item entity_key") from exc
        if (node_type == "edge") != (parsed_entity_key.kind == "edge"):
            raise LiteStorageIntegrityError("invalid Lite snapshot item entity_key")
        _require_canonical_sync_event(item["event"], label="snapshot item event")
        if (
            tenant_binding is None
            or item["event"]["scope"]["tenant_id"] != tenant_binding
        ):
            raise LiteStorageIntegrityError("invalid Lite snapshot item tenant binding")
        if (
            item["event"]["node_type"] != node_type
            or item["event"]["entity_key"] != entity_key
        ):
            raise LiteStorageIntegrityError("invalid Lite snapshot item event identity")
        key = (item["snapshot_id"], item["node_type"], item["entity_key"])
        if key in seen:
            raise LiteStorageIntegrityError("invalid Lite snapshot item duplicates")
        seen.add(key)

