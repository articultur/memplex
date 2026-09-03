"""LiteMemoryStore -- in-memory + JSON persistence backend.

Data paths::

    ~/.memplex/memory.json      Functions + graph edges
    ~/.memplex/changelog.json   Changelog events (via ChangelogStore)
    ~/.memplex/memory.json.fts5.db  Local SQLite FTS5 sidecar index

All data is held in memory and flushed to JSON on every write.
Atomic replacement (write-to-temp + rename) guards against partial writes.

Single-thread assumption: optimistic lock is skipped.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import math
import sqlite3
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from memplex.backup import (
    BackupArtifactWriter,
    BackupConfigurationError,
    BackupIntegrityError,
    BackupManifest,
    load_verified_backup_manifest,
    open_verified_backup_artifact,
)
from memplex.models import (
    OBSERVATION_CATEGORIES,
    BatchResult,
    ChangelogEvent,
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MergeResult,
    Observation,
    Preference,
    SearchFilters,
    SearchResult,
    SourceDocument,
    SourceType,
    validate_belongs_to_edges,
    validate_domain,
    validate_func_id,
)
from memplex.storage._messages import (
    _BACKUP_ARTIFACT_INVALID,
    _DUPLICATE_FACT_ID,
    _DUPLICATE_OBSERVATION_ID,
    _DUPLICATE_PREFERENCE_ID,
    _GRAPH_NODES_MUST_BE_FUNCTIONS,
    _INVALID_LITE_FIELD,
    _ONLY_FUNCTION_NODES,
)
from memplex.storage.changelog import ChangelogStore
from memplex.storage.lite import durability as durability_module
from memplex.storage.lite.durability import LiteDurability, LitePair, LiteStorageIntegrityError
from memplex.storage.lite.search_index import SQLiteFTSIndex, local_bm25_search
from memplex.sync_protocol import (
    SyncApplyResult,
    SyncBatch,
    SyncBatchResult,
    SyncCursorClaims,
    SyncDelivery,
    SyncNodeType,
    SyncOperation,
    SyncPage,
    SyncReceipt,
    SyncSnapshotPage,
    SyncStatus,
)
from memplex.sync_repository import SyncCapturePolicy, SyncDeadLetterEntry

logger = logging.getLogger(__name__)

_SOURCE_TYPE_VALUES = {item.value for item in SourceType}
_BASE_NODE_KEYS = {
    "id", "memory_type", "name", "domain", "confidence", "source_type",
    "owner", "tenant_id", "owner_subject_id", "workspace_id", "visibility",
    "provenance", "version", "created_at", "updated_at", "origin_session",
    "access_count", "last_accessed_at", "source_paragraphs", "needs_review",
    "needs_review_until", "content_hash", "namespace", "knowledge_tier",
}
_FUNCTION_KEYS = _BASE_NODE_KEYS | {
    "name_normalized", "trigger", "condition", "action", "benefit", "attributes",
    "cross_references", "priority_from_source", "source_authority",
}
_FACT_KEYS = _BASE_NODE_KEYS | {"subject", "predicate", "object", "valid_until", "valid_from", "invalid_at"}
_FACT_LEGACY_KEYS = _FACT_KEYS | {"object_"}
_PREFERENCE_KEYS = _BASE_NODE_KEYS | {"aspect", "preference", "subject_id"}
_OBSERVATION_KEYS = _BASE_NODE_KEYS | {"event", "context", "observed_at", "actor", "category"}
_FIELD_VALUE_KEYS = {"desc", "sources", "source_method", "weight", "observation", "created_at", "status"}
_EDGE_KEYS = {"source", "target", "edge_type", "weight", "evidence", "created_at"}
_G002_MISSING_IDENTITY_KEYS = {
    "tenant_id", "owner_subject_id", "workspace_id", "visibility", "provenance",
}


def _g002_historical_keys(memory_type: str) -> set[str] | None:
    """Return the one permitted pre-G002 schema for a historical node.

    G002 stored every durable node in ``functions``.  This compatibility
    branch intentionally accepts no general partial schema: every known node
    retains its complete then-current shape, with only the five later G002
    identity fields absent.  Fact's Python-keyword spelling was already part
    of the older raw format, so it alone uses ``object_`` here.
    """
    if memory_type == "function":
        return _FUNCTION_KEYS - _G002_MISSING_IDENTITY_KEYS
    if memory_type == "fact":
        return (_FACT_LEGACY_KEYS - {"object"}) - _G002_MISSING_IDENTITY_KEYS
    if memory_type == "preference":
        return _PREFERENCE_KEYS - _G002_MISSING_IDENTITY_KEYS
    if memory_type == "observation":
        return _OBSERVATION_KEYS - _G002_MISSING_IDENTITY_KEYS
    return None


def _validate_g002_historical_node_keys(raw: dict, memory_type: str) -> None:
    expected = _g002_historical_keys(memory_type)
    if expected is None or set(raw) != expected:
        raise ValueError(f"invalid Lite recognized G002 {memory_type} schema")


def _with_writer_lock(method: Callable[..., Any]) -> Callable[..., Any]:
    """Keep reload, COW mutation, and journal decision in one flock scope."""
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._durability.writer_lock():
            try:
                return method(self, *args, **kwargs)
            except BaseException:
                # A mutation may have escaped mid-way, leaving resident state
                # ahead of the durable pair.  Invalidate the cached pair /
                # fingerprint so the next reload re-reads from disk instead
                # of building on the speculative state.
                self._pair_fingerprint = None
                self._committed_pair = None
                self._committed_record = None
                raise

    wrapped.__name__ = method.__name__
    wrapped.__doc__ = method.__doc__
    return wrapped


# ── Serialization helpers ────────────────────────────────────────────


def _json_serializer(obj: Any) -> Any:
    """Default serializer for ``json.dumps``."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, SourceType):
        return obj.value
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Function / FieldValue / Observation serialization converged on the model
# standard ``to_dict``/``from_dict`` (memplex.models); only GraphEdge keeps a
# local serializer because the model class has no standard methods yet.


def _serialize_edge(edge: GraphEdge) -> dict:
    return {
        "source": edge.source,
        "target": edge.target,
        "edge_type": edge.edge_type,
        "weight": edge.weight,
        "evidence": edge.evidence,
        "created_at": (
            edge.created_at.isoformat()
            if isinstance(edge.created_at, datetime)
            else edge.created_at
        ),
    }


def _deserialize_edge(d: dict) -> GraphEdge:
    created_at = d.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    return GraphEdge(
        source=d["source"],
        target=d["target"],
        edge_type=d["edge_type"],
        weight=d.get("weight", 1.0),
        evidence=d.get("evidence", []),
        created_at=created_at,
    )


def _require_exact_string(value: Any, label: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if type(value) is not str:
        raise ValueError(_INVALID_LITE_FIELD.format(field=label))


def _require_finite_number(value: Any, label: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(_INVALID_LITE_FIELD.format(field=label))


def _validate_node_for_read(node: Any) -> None:
    """Reject weakly typed payloads before a read/search can consume them."""
    for name in ("id", "name"):
        _require_exact_string(getattr(node, name), f"node {name}")
    _require_exact_string(getattr(node, "domain", None), "node domain", optional=True)
    _require_finite_number(getattr(node, "confidence"), "node confidence")
    if not isinstance(getattr(node, "source_type"), SourceType):
        raise ValueError("invalid Lite node source_type")
    for name in (
        "owner", "tenant_id", "owner_subject_id", "workspace_id", "visibility",
        "created_at", "updated_at", "origin_session", "last_accessed_at",
        "needs_review_until", "content_hash",
    ):
        _require_exact_string(getattr(node, name, None), f"node {name}", optional=True)
    if type(getattr(node, "version")) is not int or getattr(node, "version") < 0:
        raise ValueError("invalid Lite node version")
    if type(getattr(node, "access_count")) is not int or getattr(node, "access_count") < 0:
        raise ValueError("invalid Lite node access_count")
    if type(getattr(node, "needs_review")) is not bool:
        raise ValueError("invalid Lite node needs_review")
    if type(getattr(node, "source_paragraphs")) is not list or any(
        type(value) is not str for value in node.source_paragraphs
    ):
        raise ValueError("invalid Lite node source_paragraphs")
    if type(getattr(node, "provenance")) is not dict or type(getattr(node, "namespace")) is not dict:
        raise ValueError("invalid Lite node mappings")


def _validate_function_for_read(func: Function) -> None:
    _validate_node_for_read(func)
    _require_exact_string(func.name_normalized, "Function name_normalized")
    for values in (func.trigger, func.condition, func.action, func.benefit):
        if type(values) is not list:
            raise ValueError("invalid Lite Function field values")
        for value in values:
            _require_exact_string(value.desc, "FieldValue desc")
            _require_exact_string(value.source_method, "FieldValue source_method")
            _require_exact_string(value.status, "FieldValue status")
            if type(value.sources) is not list or any(type(source) is not str for source in value.sources):
                raise ValueError("invalid Lite FieldValue sources")
            _require_finite_number(value.weight, "FieldValue weight")
            if value.observation is not None:
                _require_finite_number(value.observation, "FieldValue observation")
            if value.created_at is not None and not isinstance(value.created_at, datetime):
                raise ValueError("invalid Lite FieldValue created_at")


def _validate_edge_for_read(edge: GraphEdge) -> None:
    for name in ("source", "target", "edge_type"):
        _require_exact_string(getattr(edge, name), f"GraphEdge {name}")
    _require_finite_number(edge.weight, "GraphEdge weight")
    if type(edge.evidence) is not list or any(type(item) is not str for item in edge.evidence):
        raise ValueError("invalid Lite GraphEdge evidence")
    if edge.created_at is not None and not isinstance(edge.created_at, datetime):
        raise ValueError("invalid Lite GraphEdge created_at")


def _validate_raw_node_discriminators(raw: Any, expected_memory_type: str, *, legacy: bool) -> None:
    """Validate raw node schema before tolerant model ``from_dict`` can coerce it."""
    if type(raw) is not dict:
        raise ValueError("invalid Lite raw memory node")
    if "memory_type" not in raw:
        if not legacy:
            raise ValueError("missing Lite memory_type")
    elif type(raw["memory_type"]) is not str or raw["memory_type"] != expected_memory_type:
        raise ValueError("invalid Lite memory_type")
    if "source_type" not in raw:
        if not legacy:
            raise ValueError("missing Lite source_type")
    else:
        source_type = raw["source_type"]
        if not (
            isinstance(source_type, SourceType)
            or (type(source_type) is str and source_type in {item.value for item in SourceType})
        ):
            raise ValueError("invalid Lite source_type")
    for name in ("id", "name"):
        if name in raw and type(raw[name]) is not str:
            raise ValueError(_INVALID_LITE_FIELD.format(field=name))
    for name in (
        "domain", "owner", "tenant_id", "owner_subject_id", "workspace_id", "visibility",
        "created_at", "updated_at", "origin_session", "last_accessed_at",
        "needs_review_until", "content_hash",
    ):
        if name in raw and raw[name] is not None and type(raw[name]) is not str:
            raise ValueError(_INVALID_LITE_FIELD.format(field=name))
    for name in ("confidence",):
        if name in raw:
            _require_finite_number(raw[name], f"raw node {name}")
    for name in ("version", "access_count"):
        if name in raw and (type(raw[name]) is not int or raw[name] < 0):
            raise ValueError(_INVALID_LITE_FIELD.format(field=name))
    if "needs_review" in raw and type(raw["needs_review"]) is not bool:
        raise ValueError("invalid Lite needs_review")
    if "source_paragraphs" in raw and (
        type(raw["source_paragraphs"]) is not list
        or any(type(item) is not str for item in raw["source_paragraphs"])
    ):
        raise ValueError("invalid Lite source_paragraphs")
    for name in ("provenance", "namespace"):
        if name in raw and (
            type(raw[name]) is not dict
            or any(type(key) is not str or type(value) is not str for key, value in raw[name].items())
        ):
            raise ValueError(_INVALID_LITE_FIELD.format(field=name))


def _validate_raw_keys(raw: dict, allowed: set[str], *, legacy: bool, label: str) -> None:
    if not set(raw) <= allowed:
        raise ValueError(f"invalid Lite {label} keys")
    if not legacy and set(raw) != allowed:
        raise ValueError(f"incomplete Lite {label} schema")


def _is_recognized_g002_enveloped_v1(memory: Any) -> bool:
    """Identify the sole supported pre-G002 mixed-node collection shape."""
    if type(memory) is not dict or type(memory.get("schema_version")) is not int or memory["schema_version"] != 1:
        return False
    functions = memory.get("functions")
    if type(functions) is not list or not functions:
        return False
    for node in functions:
        if type(node) is not dict or type(node.get("memory_type")) is not str:
            return False
        expected = _g002_historical_keys(node["memory_type"])
        if expected is None or set(node) != expected:
            return False
    return True


def _validate_json_value(value: Any, label: str) -> None:
    """Limit generic metadata to finite, canonical JSON-compatible values."""
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(_INVALID_LITE_FIELD.format(field=label))
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, label)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json_value(item, label)
        return
    raise ValueError(_INVALID_LITE_FIELD.format(field=label))


def _validate_raw_field_value(raw: Any, *, legacy: bool) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite FieldValue")
    _validate_raw_keys(raw, _FIELD_VALUE_KEYS, legacy=legacy, label="FieldValue")
    if "desc" in raw and type(raw["desc"]) is not str:
        raise ValueError("invalid Lite FieldValue desc")
    if "sources" in raw and (type(raw["sources"]) is not list or any(
        type(item) is not str for item in raw["sources"]
    )):
        raise ValueError("invalid Lite FieldValue strings")
    if "source_method" in raw and type(raw["source_method"]) is not str:
        raise ValueError("invalid Lite FieldValue source_method")
    if "weight" in raw:
        _require_finite_number(raw["weight"], "raw FieldValue weight")
    if raw.get("observation") is not None:
        _require_finite_number(raw["observation"], "raw FieldValue observation")
    if raw.get("created_at") is not None and type(raw["created_at"]) is not str:
        raise ValueError("invalid Lite FieldValue created_at")
    if "status" in raw and (type(raw["status"]) is not str or raw["status"] not in {"active", "deprecated", "disputed"}):
        raise ValueError("invalid Lite FieldValue status")


def _validate_raw_function(raw: Any, *, legacy: bool, g002_historical: bool = False) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite Function")
    if g002_historical:
        _validate_g002_historical_node_keys(raw, "function")
    else:
        _validate_raw_keys(raw, _FUNCTION_KEYS, legacy=legacy, label="Function")
    _validate_raw_node_discriminators(raw, "function", legacy=legacy)
    for name in ("trigger", "condition", "action", "benefit"):
        if name in raw:
            if type(raw[name]) is not list:
                raise ValueError(f"invalid Lite Function {name}")
            for value in raw[name]:
                _validate_raw_field_value(value, legacy=legacy)
    if "attributes" in raw:
        if type(raw["attributes"]) is not dict or any(type(key) is not str for key in raw["attributes"]):
            raise ValueError("invalid Lite Function attributes")
        _validate_json_value(raw["attributes"], "Function attributes")
    if "cross_references" in raw and (
        type(raw["cross_references"]) is not list
        or any(type(item) is not dict for item in raw["cross_references"])
    ):
        raise ValueError("invalid Lite Function cross_references")
    if "cross_references" in raw:
        for reference in raw["cross_references"]:
            _validate_json_value(reference, "Function cross_references")
    for name in ("priority_from_source", "source_authority"):
        if name in raw and raw[name] is not None and type(raw[name]) is not str:
            raise ValueError(f"invalid Lite Function {name}")


def _validate_raw_observation(raw: Any, *, legacy: bool, g002_historical: bool = False) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite Observation")
    if g002_historical:
        _validate_g002_historical_node_keys(raw, "observation")
    else:
        _validate_raw_keys(raw, _OBSERVATION_KEYS, legacy=legacy, label="Observation")
    _validate_raw_node_discriminators(raw, "observation", legacy=legacy)
    for name in ("event", "context", "actor"):
        if name in raw and type(raw[name]) is not str:
            raise ValueError(f"invalid Lite Observation {name}")
    if "observed_at" in raw and raw["observed_at"] is not None and type(raw["observed_at"]) is not str:
        raise ValueError("invalid Lite Observation observed_at")
    if "category" in raw and (
        type(raw["category"]) is not str or raw["category"] not in OBSERVATION_CATEGORIES
    ):
        raise ValueError("invalid Lite Observation category")


def _validate_raw_fact(raw: Any, *, legacy: bool, g002_historical: bool = False) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite Fact")
    if g002_historical:
        _validate_g002_historical_node_keys(raw, "fact")
    else:
        _validate_raw_keys(raw, _FACT_LEGACY_KEYS if legacy else _FACT_KEYS, legacy=legacy, label="Fact")
    _validate_raw_node_discriminators(raw, "fact", legacy=legacy)
    for name in ("subject", "predicate"):
        if name in raw and type(raw[name]) is not str:
            raise ValueError(f"invalid Lite Fact {name}")
    object_keys = [name for name in ("object", "object_") if name in raw]
    if (
        (g002_historical and object_keys != ["object_"])
        or (not g002_historical and not legacy and object_keys != ["object"])
        or (not g002_historical and legacy and len(object_keys) > 1)
    ):
        raise ValueError("invalid Lite Fact object key")
    if object_keys and type(raw[object_keys[0]]) is not str:
        raise ValueError("invalid Lite Fact object")
    if "valid_until" in raw and raw["valid_until"] is not None and type(raw["valid_until"]) is not str:
        raise ValueError("invalid Lite Fact valid_until")


def _validate_raw_preference(raw: Any, *, legacy: bool, g002_historical: bool = False) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite Preference")
    if g002_historical:
        _validate_g002_historical_node_keys(raw, "preference")
    else:
        _validate_raw_keys(raw, _PREFERENCE_KEYS, legacy=legacy, label="Preference")
    _validate_raw_node_discriminators(raw, "preference", legacy=legacy)
    for name in ("aspect", "preference"):
        if name in raw and type(raw[name]) is not str:
            raise ValueError(f"invalid Lite Preference {name}")
    if "subject_id" in raw and raw["subject_id"] is not None and type(raw["subject_id"]) is not str:
        raise ValueError("invalid Lite Preference subject_id")


def _validate_raw_edge(raw: Any, *, legacy: bool) -> None:
    if type(raw) is not dict:
        raise ValueError("invalid Lite GraphEdge")
    _validate_raw_keys(raw, _EDGE_KEYS, legacy=legacy, label="GraphEdge")
    if any(name in raw and type(raw[name]) is not str for name in ("source", "target", "edge_type")):
        raise ValueError("invalid Lite GraphEdge endpoints")
    if "weight" in raw:
        _require_finite_number(raw["weight"], "raw GraphEdge weight")
    if "evidence" in raw and (type(raw["evidence"]) is not list or any(type(item) is not str for item in raw["evidence"])):
        raise ValueError("invalid Lite GraphEdge evidence")
    if raw.get("created_at") is not None and type(raw["created_at"]) is not str:
        raise ValueError("invalid Lite GraphEdge created_at")


# ── Merge helpers ────────────────────────────────────────────────────


def _merge_field_values(
    existing: List[FieldValue],
    incoming: List[FieldValue],
) -> List[FieldValue]:
    """Merge incoming FieldValues into existing.  Duplicates (by desc) are
    skipped; weight and observation are taken from the newer entry.
    """
    seen = {fv.desc for fv in existing}
    merged = list(existing)
    for fv in incoming:
        if fv.desc not in seen:
            merged.append(fv)
            seen.add(fv.desc)
    # Enforce the model-level cap (Function.MAX_VALUES_PER_FIELD): existing
    # values win, overflow from newer incoming values is dropped.
    return merged[: Function.MAX_VALUES_PER_FIELD]


def _normalize_name(name: str) -> str:
    """Produce a normalised form for dedup matching."""
    return name.strip().lower()


# ── LiteMemoryStore ──────────────────────────────────────────────────


class LiteMemoryStore:
    """InMemory + JSON persistence backend.

    Parameters
    ----------
    path:
        Root JSON file path.  Defaults to ``~/.memplex/memory.json``.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        *,
        sync_capture_policy: SyncCapturePolicy | None = None,
        sync_max_pending_events: int = 100000,
        sync_max_attempts: int = 8,
        sync_snapshot_ttl_seconds: int = 900,
        sync_max_snapshot_items: int = 1000000,
        sync_max_active_snapshots_per_tenant: int = 2,
        sync_max_active_snapshots_per_remote: int = 1,
        sync_consumer_ttl_seconds: int = 86400,
        sync_retention_min_seconds: int = 86400,
        deployment_profile: str = "development",
    ) -> None:
        if deployment_profile not in {"development", "production"}:
            raise BackupConfigurationError("lite_backup_profile_invalid")
        self._deployment_profile = deployment_profile
        self._path = path or Path("~/.memplex/memory.json").expanduser()
        self._functions: Dict[str, Function] = {}
        self._name_index: Dict[str, str] = {}  # name_normalized -> func_id
        self._edges: List[GraphEdge] = []
        self._edges_by_node: Dict[str, Dict[str, List[GraphEdge]]] = {}
        self._observations: List[Observation] = []
        self._facts: Dict[str, Fact] = {}
        self._preferences: Dict[str, Preference] = {}
        # (mtime_ns, size) fingerprint of both pair files, set after each
        # successful publish so unchanged reads skip the O(N) reload.
        self._pair_fingerprint: Optional[tuple] = None
        # Raw pair exactly as last published (== the durable on-disk state at
        # that moment).  Lets the commit path reuse the authoritative base
        # without a second full disk read while the flock is held.
        self._committed_pair: Optional[LitePair] = None
        # Digest record of ``_committed_pair`` when it came from a local
        # commit (None after any disk reload); reused as the next commit's
        # journal base_record to skip re-encoding the base digests.
        self._committed_record: Optional[dict[str, Any]] = None
        self._sync_state: dict[str, Any] = durability_module._empty_sync_state()
        self._generation = 0
        changelog_path = (
            self._path.parent / "changelog.json"
            if self._path.name == "memory.json"
            else self._path.with_name(f"{self._path.stem}.changelog.json")
        )
        self._durability = LiteDurability(self._path, changelog_path)
        self._changelog = ChangelogStore(path=changelog_path, managed=True)
        self._fts_index = SQLiteFTSIndex(
            path=self._path.with_name(f"{self._path.name}.fts5.db"),
            functions=self._functions,
            text_factory=self._function_to_search_text,
        )
        self._durability.set_semantic_validator(self._decode_pair)
        self._load()
        from memplex.storage.lite.sync_repository import LiteSyncRepository

        self._sync_repository = LiteSyncRepository(
            self,
            capture_policy=sync_capture_policy or SyncCapturePolicy("off"),
            max_pending_events=sync_max_pending_events,
            max_attempts=sync_max_attempts,
            snapshot_ttl_seconds=sync_snapshot_ttl_seconds,
            max_snapshot_items=sync_max_snapshot_items,
            max_active_snapshots_per_tenant=sync_max_active_snapshots_per_tenant,
            max_active_snapshots_per_remote=sync_max_active_snapshots_per_remote,
            consumer_ttl_seconds=sync_consumer_ttl_seconds,
            retention_min_seconds=sync_retention_min_seconds,
        )

    def _require_development_backup(self) -> None:
        if self._deployment_profile != "development":
            raise BackupConfigurationError("lite_backup_development_only")

    @staticmethod
    def _lite_tar_member(name: str, payload: bytes) -> tuple[tarfile.TarInfo, bytes]:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        member.mode = 0o600
        member.uid = 0
        member.gid = 0
        member.uname = ""
        member.gname = ""
        member.mtime = 0
        return member, payload

    def create_backup(
        self,
        destination: Path,
        signing_key: bytes,
        key_id: str,
        *,
        max_bytes: int = 64 * 1024**3,
    ) -> BackupManifest:
        """Publish one signed snapshot of an authoritative Lite pair."""
        self._require_development_backup()
        destination_path = Path(destination)
        with self._durability.writer_lock():
            pair = self._durability._load_authoritative_locked()
            memory_doc, changelog_doc = self._durability._envelopes(pair)
            memory_bytes = durability_module._canonical_json(memory_doc)
            changelog_bytes = durability_module._canonical_json(changelog_doc)
            destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            backup_id = str(uuid.uuid4())
            with tempfile.TemporaryDirectory(
                prefix=".lite-backup-", dir=destination_path
            ) as capture_directory:
                payload = Path(capture_directory) / "payload.dump"
                with payload.open("xb") as raw:
                    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                        for member, contents in (
                            self._lite_tar_member("memory.json", memory_bytes),
                            self._lite_tar_member("changelog.json", changelog_bytes),
                        ):
                            archive.addfile(member, io.BytesIO(contents))
                writer = BackupArtifactWriter(
                    destination_path,
                    key=signing_key,
                    key_id=key_id,
                    max_bytes=max_bytes,
                )
                artifact = writer.publish(
                    manifest_fields={
                        "format_version": 1,
                        "backup_id": backup_id,
                        "created_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "backend": "lite",
                        "database": "local",
                        "schema": "memory",
                        "migration_version": pair.generation,
                        "payload_name": "payload.dump",
                        "pg_dump_version": "not-applicable",
                        "server_version": "lite-v2",
                        "consistency": "lite_pair_generation",
                    },
                    payload_source=payload,
                )
        manifest = load_verified_backup_manifest(artifact, signing_key)
        if manifest.backup_id != backup_id:
            raise BackupIntegrityError("backup_publish_outcome_unknown")
        return manifest

    @staticmethod
    def _decode_backup_json(payload: bytes) -> Any:
        def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                result[key] = value
            return result

        try:
            return json.loads(payload.decode("utf-8"), object_pairs_hook=_pairs)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID) from exc

    def restore_backup(self, artifact: Path, signing_key: bytes) -> None:
        """Replace the current Lite pair from one verified development snapshot."""
        self._require_development_backup()
        artifact_path = Path(artifact)
        try:
            with open_verified_backup_artifact(artifact_path, signing_key) as opened:
                manifest = opened.manifest
                if (
                    manifest.backend != "lite"
                    or manifest.consistency != "lite_pair_generation"
                ):
                    raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                with tempfile.TemporaryDirectory(prefix=".lite-restore-") as restore_dir:
                    payload = Path(restore_dir) / "payload.dump"
                    opened.copy_payload_to(payload)
                    with tarfile.open(payload, mode="r:") as archive:
                        members = archive.getmembers()
                        if [member.name for member in members] != [
                            "memory.json",
                            "changelog.json",
                        ]:
                            raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                        if any(
                            not member.isfile() or member.size < 0 for member in members
                        ):
                            raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                        extracted = []
                        for member in members:
                            source = archive.extractfile(member)
                            if source is None:
                                raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                            data = source.read(member.size + 1)
                            if len(data) != member.size:
                                raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID)
                            extracted.append(self._decode_backup_json(data))
        except BackupIntegrityError:
            raise
        except (OSError, tarfile.TarError) as exc:
            raise BackupIntegrityError(_BACKUP_ARTIFACT_INVALID) from exc

        restored = self._durability._decode_pair(extracted[0], extracted[1])
        self._durability._validate_semantics(restored)
        with self._durability.writer_lock():
            base = self._durability._load_authoritative_locked()
            target = LitePair(
                memory=copy.deepcopy(restored.memory),
                changelog=copy.deepcopy(restored.changelog),
                generation=base.generation + 1,
                transaction_id=uuid.uuid4().hex,
            )
            self._decode_pair(target)
            # The flock has been held continuously since ``base`` was read.
            committed = self._durability.commit_locked(base, target, base_verified=True)
            self._publish_pair(committed)
            self._fts_index.rebuild()

    # ── Public: Durable sync repository ────────────────────────────

    @property
    def schema_version(self) -> int:
        return 2

    def sync_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims | None,
        limit: int,
    ) -> SyncPage:
        return self._sync_repository.sync_page(remote_id, consumer_id, cursor, limit)

    def sync_create_snapshot(
        self,
        remote_id: str,
        consumer_id: str,
        request_id: str,
        limit: int,
    ) -> SyncSnapshotPage:
        return self._sync_repository.sync_create_snapshot(
            remote_id, consumer_id, request_id, limit
        )

    def sync_snapshot_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims,
        limit: int,
    ) -> SyncSnapshotPage:
        return self._sync_repository.sync_snapshot_page(
            remote_id, consumer_id, cursor, limit
        )

    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult:
        return self._sync_repository.sync_apply_batch(batch)

    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult:
        return self._sync_repository.sync_apply_page(remote_id, page)

    def sync_register_target(self, target_id: str, *, bootstrap: str = "future") -> None:
        self._sync_repository.sync_register_target(target_id, bootstrap=bootstrap)

    def sync_claim(
        self,
        target_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[SyncDelivery]:
        return self._sync_repository.sync_claim(
            target_id, limit=limit, lease_seconds=lease_seconds
        )

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None:
        self._sync_repository.sync_ack(delivery, receipt)

    def sync_ack_batch(
        self,
        deliveries: list[SyncDelivery],
        receipts: tuple[SyncReceipt, ...],
    ) -> None:
        self._sync_repository.sync_ack_batch(deliveries, receipts)

    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None:
        self._sync_repository.sync_fail(delivery, error_code, now)

    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None:
        self._sync_repository.sync_dead_letter(delivery, error_code, now)

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool:
        return self._sync_repository.sync_replay_dead_letter(target_id, event_id)

    def sync_list_dead_letters(self, *, limit: int) -> list[SyncDeadLetterEntry]:
        return self._sync_repository.sync_list_dead_letters(limit=limit)

    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None:
        self._sync_repository.sync_set_target_enabled(target_id, enabled)

    def sync_compact(self, now: datetime, *, limit: int) -> int:
        return self._sync_repository.sync_compact(now, limit=limit)

    def sync_status(self) -> SyncStatus:
        return self._sync_repository.sync_status()

    def sync_dispatch_status(self) -> SyncStatus:
        return self._sync_repository.sync_dispatch_status()

    def _commit_sync_changes(
        self,
        *,
        nodes: list[tuple[Any, SyncNodeType, SyncOperation]] | None = None,
        edges: list[tuple[GraphEdge, Function, SyncOperation]] | None = None,
    ) -> None:
        """Capture business changes and publish one pair or restore the base."""
        try:
            for edge, scope_node, operation in edges or []:
                if operation is SyncOperation.TOMBSTONE:
                    self._sync_repository.capture_edge(
                        edge, scope_node=scope_node, operation=operation
                    )
            for node, node_type, operation in nodes or []:
                if operation is SyncOperation.TOMBSTONE:
                    self._sync_repository.capture_node(
                        node, node_type=node_type, operation=operation
                    )
            for node, node_type, operation in nodes or []:
                if operation is SyncOperation.UPSERT:
                    self._sync_repository.capture_node(
                        node, node_type=node_type, operation=operation
                    )
            for edge, scope_node, operation in edges or []:
                if operation is SyncOperation.UPSERT:
                    self._sync_repository.capture_edge(
                        edge, scope_node=scope_node, operation=operation
                    )
            self._commit_current_state()
        except BaseException:
            try:
                self._reload_for_mutation(force=True)
            except BaseException:
                pass
            raise

    # ── Public: Write ───────────────────────────────────────────────

    def add(self, func: Function, source: SourceDocument) -> None:
        """Add under one cross-process critical section (no lost update)."""
        with self._durability.writer_lock():
            try:
                self._add_unlocked(func, source)
            except BaseException:
                # See _with_writer_lock: never let a speculative mutation
                # survive behind the fingerprint short-circuit.
                self._pair_fingerprint = None
                self._committed_pair = None
                self._committed_record = None
                raise

    def _add_unlocked(self, func: Function, source: SourceDocument) -> None:
        self._reload_for_mutation()
        func, source = copy.deepcopy(func), copy.deepcopy(source)
        self._validate_resident_graph()
        if not isinstance(func, Function):
            raise ValueError(_ONLY_FUNCTION_NODES.format(backend="LiteMemoryStore"))
        # Dataclasses are mutable: revalidate here so a caller cannot mutate
        # an otherwise valid Function into GraphBuilder's virtual namespace.
        validate_func_id(func.id)
        validate_domain(func.domain)
        norm = _normalize_name(func.name_normalized or func.name)
        existing_id = self._name_index.get(norm)

        if existing_id and existing_id in self._functions:
            existing = self._functions[existing_id]
            # Merge FieldValues
            existing.trigger = _merge_field_values(existing.trigger, func.trigger)
            existing.condition = _merge_field_values(existing.condition, func.condition)
            existing.action = _merge_field_values(existing.action, func.action)
            existing.benefit = _merge_field_values(existing.benefit, func.benefit)
            # Merge source paragraphs
            for sp in func.source_paragraphs:
                if sp not in existing.source_paragraphs:
                    existing.source_paragraphs.append(sp)
            existing.updated_at = datetime.now(timezone.utc).isoformat()
            existing.version += 1
            # Carry lifecycle fields from the incoming node (promote /
            # share_with re-add): the merge above intentionally keeps the
            # existing FieldValues, but tier, visibility, provenance, and
            # namespace grants must survive or promotion silently no-ops
            # (S3 fix).
            if func.knowledge_tier is not None:
                existing.knowledge_tier = func.knowledge_tier
            if func.visibility is not None:
                existing.visibility = func.visibility
            if func.provenance:
                merged_provenance = dict(existing.provenance or {})
                merged_provenance.update(func.provenance)
                existing.provenance = merged_provenance
            if func.namespace:
                merged_namespace = dict(existing.namespace or {})
                merged_namespace.update(func.namespace)
                existing.namespace = merged_namespace

            self._changelog.append(
                ChangelogEvent(
                    func_id=existing.id,
                    timestamp=datetime.now(),
                    event_type="updated",
                    description="Merged fields from source",
                    source=getattr(source, "source_path", None) or getattr(source, "url", "") or "",
                    actor="system",
                )
            )
        else:
            self._functions[func.id] = func
            self._name_index[norm] = func.id

            self._changelog.append(
                ChangelogEvent(
                    func_id=func.id,
                    timestamp=datetime.now(),
                    event_type="created",
                    description=f"Created function: {func.name}",
                    source=getattr(source, "source_path", None) or getattr(source, "url", "") or "",
                    actor="system",
                )
            )

        persisted = self._functions[existing_id] if existing_id else self._functions[func.id]
        self._commit_sync_changes(
            nodes=[(persisted, SyncNodeType.FUNCTION, SyncOperation.UPSERT)]
        )

    def add_batch(
        self,
        funcs: List[Function],
        sources: List[SourceDocument],
    ) -> BatchResult:
        result = BatchResult(total=len(funcs))
        for func, src in zip(funcs, sources):
            try:
                self.add(func, src)
                result.succeeded += 1
            except Exception as exc:
                result.failed_items.append(
                    {
                        "func_id": func.id,
                        "name": func.name,
                        "error": str(exc),
                    }
                )
        return result

    @_with_writer_lock
    def add_observation(self, observation: Observation) -> None:
        self._reload_for_mutation()
        observation = copy.deepcopy(observation)
        self._validate_resident_graph()
        self._observations.append(observation)
        self._commit_sync_changes(
            nodes=[(observation, SyncNodeType.OBSERVATION, SyncOperation.UPSERT)]
        )

    # ── Fact / Preference (optional MemoryStore extensions) ─────────

    @_with_writer_lock
    def add_fact(self, fact: Fact) -> None:
        """Persist a Fact (upsert by id); records a changelog entry.

        LWW sync semantics: an incoming ``updated_at`` (source timestamp)
        is preserved -- only a missing one is filled with now, matching
        the Function write path.
        """
        self._reload_for_mutation()
        fact = copy.deepcopy(fact)
        self._validate_resident_graph()
        event_type = "updated" if fact.id in self._facts else "created"
        now = datetime.now(timezone.utc).isoformat()
        if not fact.created_at:
            fact.created_at = now
        if not fact.updated_at:
            fact.updated_at = now
        self._facts[fact.id] = fact
        self._changelog.append(
            ChangelogEvent(
                func_id=fact.id,
                timestamp=datetime.now(),
                event_type=event_type,
                description=f"{event_type.capitalize()} fact: {fact.name or fact.subject}",
                source="",
                actor="system",
            )
        )
        self._commit_sync_changes(
            nodes=[(fact, SyncNodeType.FACT, SyncOperation.UPSERT)]
        )

    @_with_writer_lock
    def add_preference(self, preference: Preference) -> None:
        """Persist a Preference (upsert by id); records a changelog entry.

        LWW sync semantics: an incoming ``updated_at`` (source timestamp)
        is preserved -- only a missing one is filled with now, matching
        the Function write path.
        """
        self._reload_for_mutation()
        preference = copy.deepcopy(preference)
        self._validate_resident_graph()
        event_type = "updated" if preference.id in self._preferences else "created"
        now = datetime.now(timezone.utc).isoformat()
        if not preference.created_at:
            preference.created_at = now
        if not preference.updated_at:
            preference.updated_at = now
        self._preferences[preference.id] = preference
        self._changelog.append(
            ChangelogEvent(
                func_id=preference.id,
                timestamp=datetime.now(),
                event_type=event_type,
                description=(
                    f"{event_type.capitalize()} preference: "
                    f"{preference.name or preference.aspect}"
                ),
                source="",
                actor="system",
            )
        )
        self._commit_sync_changes(
            nodes=[(preference, SyncNodeType.PREFERENCE, SyncOperation.UPSERT)]
        )

    @_with_writer_lock
    def get_fact(self, fact_id: str) -> Optional[Fact]:
        self._refresh_for_read()
        return copy.deepcopy(self._facts.get(fact_id))

    @_with_writer_lock
    def get_preference(self, preference_id: str) -> Optional[Preference]:
        self._refresh_for_read()
        return copy.deepcopy(self._preferences.get(preference_id))

    @_with_writer_lock
    def list_facts(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Fact]:
        self._refresh_for_read()
        facts = list(self._facts.values())
        if owner is not None:
            facts = [f for f in facts if f.owner == owner]
        return copy.deepcopy(facts[offset : offset + limit])

    @_with_writer_lock
    def list_preferences(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Preference]:
        self._refresh_for_read()
        prefs = list(self._preferences.values())
        if owner is not None:
            prefs = [p for p in prefs if p.owner == owner]
        return copy.deepcopy(prefs[offset : offset + limit])

    @_with_writer_lock
    def list_observations(
        self,
        offset: int = 0,
        limit: int = 1000,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> List[Observation]:
        """Paginated Observation listing, optionally filtered by category/owner."""
        self._refresh_for_read()
        obs = list(self._observations)
        if category is not None:
            obs = [o for o in obs if o.category == category]
        if owner is not None:
            obs = [o for o in obs if o.owner == owner]
        return copy.deepcopy(obs[offset : offset + limit])

    @_with_writer_lock
    def get_observation(self, observation_id: str) -> Optional[Observation]:
        self._refresh_for_read()
        return copy.deepcopy(
            next((item for item in self._observations if item.id == observation_id), None)
        )

    @_with_writer_lock
    def delete_fact(self, fact_id: str) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        if fact_id in self._facts:
            fact = self._facts.pop(fact_id)
            self._commit_sync_changes(
                nodes=[(fact, SyncNodeType.FACT, SyncOperation.TOMBSTONE)]
            )

    @_with_writer_lock
    def delete_preference(self, preference_id: str) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        if preference_id in self._preferences:
            preference = self._preferences.pop(preference_id)
            self._commit_sync_changes(
                nodes=[
                    (preference, SyncNodeType.PREFERENCE, SyncOperation.TOMBSTONE)
                ]
            )

    @_with_writer_lock
    def delete_observation(self, observation_id: str) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        observation = next(
            (item for item in self._observations if item.id == observation_id),
            None,
        )
        if observation is None:
            return
        self._observations = [
            item for item in self._observations if item.id != observation_id
        ]
        self._commit_sync_changes(
            nodes=[
                (observation, SyncNodeType.OBSERVATION, SyncOperation.TOMBSTONE)
            ]
        )

    @_with_writer_lock
    def increment_access(self, func_id: str) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        func = self._functions.get(func_id)
        if func is None:
            return
        func.access_count += 1
        func.last_accessed_at = datetime.now(timezone.utc).isoformat()
        self._commit_sync_changes(
            nodes=[(func, SyncNodeType.FUNCTION, SyncOperation.UPSERT)]
        )

    @_with_writer_lock
    def increment_access_batch(self, func_ids: List[str]) -> None:
        """Update access_count for many funcs with a SINGLE persistence pass.

        Overrides the base default (which would call increment_access N
        times -> N full JSON rewrites). Critical for query latency: a
        query returning K results used to trigger K full-store writes;
        now it triggers one.
        """
        self._reload_for_mutation()
        self._validate_resident_graph()
        now = datetime.now(timezone.utc).isoformat()
        touched: list[Function] = []
        for func_id in func_ids:
            func = self._functions.get(func_id)
            if func is None:
                continue
            func.access_count += 1
            func.last_accessed_at = now
            touched.append(func)
        if touched:
            self._commit_sync_changes(
                nodes=[
                    (func, SyncNodeType.FUNCTION, SyncOperation.UPSERT)
                    for func in touched
                ]
            )

    # ── Public: Retrieval ───────────────────────────────────────────

    @_with_writer_lock
    def vector_search(self, text: str, top_k: int = 5) -> List[SearchResult]:
        """Local SQLite FTS5/BM25 + trigram search over Function text."""
        self._refresh_for_read()
        return self._search_with_fallback(text, top_k=top_k)

    @_with_writer_lock
    def fts_search(self, text: str, top_k: int = 10) -> List[SearchResult]:
        """Local full-text search using FTS5 BM25, phrase, and trigram matching.

        Covers Functions (FTS5 sidecar / local fallback) plus Fact and
        Preference content (local BM25, merged by relevance).
        """
        self._refresh_for_read()
        return self._search_with_fallback(text, top_k=top_k)

    @_with_writer_lock
    def filter(self, filters: SearchFilters) -> List[Function]:
        self._refresh_for_read()
        results: List[Function] = []
        for func in self._functions.values():
            if not self._matches_filter(func, filters):
                continue
            results.append(func)
        return copy.deepcopy(results)

    # ── Public: Read ────────────────────────────────────────────────

    @_with_writer_lock
    def get(self, func_id: str) -> Optional[Function]:
        self._refresh_for_read()
        return copy.deepcopy(self._functions.get(func_id))

    def _index_edge(self, edge: GraphEdge) -> None:
        node_ids = (edge.source,) if edge.source == edge.target else (edge.source, edge.target)
        for node_id in node_ids:
            self._edges_by_node.setdefault(node_id, {}).setdefault(edge.edge_type, []).append(
                edge
            )

    def _rebuild_edge_index(self) -> None:
        self._edges_by_node.clear()
        for edge in self._edges:
            self._index_edge(edge)

    @_with_writer_lock
    def get_neighbors(
        self,
        func_id: str,
        edge_types: Optional[List[str]] = None,
        max_hops: int = 1,
        limit: Optional[int] = None,
    ) -> List[Function]:
        self._refresh_for_read()
        if max_hops < 1 or (limit is not None and limit <= 0):
            return []

        # BFS over the maintained adjacency index. Query-time traversal never
        # scans the global edge list and stops as soon as the storage-level
        # candidate limit is reached.
        visited: set = {func_id}
        current_level = [func_id]
        neighbor_ids: List[str] = []

        for _ in range(max_hops):
            next_level: List[str] = []
            for fid in current_level:
                buckets = self._edges_by_node.get(fid, {})
                selected_types = edge_types if edge_types else list(buckets)
                for edge_type in selected_types:
                    for edge in buckets.get(edge_type, []):
                        neighbor_id = edge.target if edge.source == fid else edge.source
                        if neighbor_id in visited:
                            continue
                        visited.add(neighbor_id)
                        next_level.append(neighbor_id)
                        neighbor_ids.append(neighbor_id)
                        if limit is not None and len(neighbor_ids) >= limit:
                            return copy.deepcopy([
                                self._functions[nid]
                                for nid in neighbor_ids
                                if nid in self._functions
                            ])
            current_level = next_level
            if not current_level:
                break

        return copy.deepcopy([self._functions[fid] for fid in neighbor_ids if fid in self._functions])

    @_with_writer_lock
    def get_graph(self, func_ids: Optional[List[str]] = None) -> GraphData:
        self._refresh_for_read()
        if func_ids is None:
            nodes = list(self._functions.values())
            edges = list(self._edges)
        else:
            id_set = set(func_ids)
            nodes = [self._functions[fid] for fid in func_ids if fid in self._functions]
            edges = [e for e in self._edges if e.source in id_set or e.target in id_set]
        return copy.deepcopy(GraphData(nodes=nodes, edges=edges))

    @_with_writer_lock
    def get_timeline(self, func_id: str, limit: int = 20) -> List[ChangelogEvent]:
        self._refresh_for_read()
        return self._changelog.get_timeline(func_id, limit)

    @_with_writer_lock
    def count_functions(self) -> int:
        self._refresh_for_read()
        return len(self._functions)

    @_with_writer_lock
    def list_functions(
        self,
        offset: int = 0,
        limit: int = 1000,
        owner: Optional[str] = None,
    ) -> List[Function]:
        self._refresh_for_read()
        funcs = list(self._functions.values())
        if owner is not None:
            funcs = [f for f in funcs if f.owner == owner]
        return copy.deepcopy(funcs[offset : offset + limit])

    @_with_writer_lock
    def list_changes_since(
        self, since: Optional[str] = None, limit: int = 100000
    ) -> List[Function]:
        """Incremental sync query: filter by updated_at at the dict level.

        Avoids serializing all Functions when only a few changed since the
        last pull. Overrides the base default for the lite in-memory store.
        """
        self._refresh_for_read()
        if since is None:
            return copy.deepcopy(list(self._functions.values())[:limit])
        return copy.deepcopy([f for f in self._functions.values() if (f.updated_at or "") > since][:limit])

    # ── Public: Delete / Merge / Clear ──────────────────────────────

    @_with_writer_lock
    def delete(self, func_id: str) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        removed = self._functions.pop(func_id, None)
        if removed is None:
            return
        # Remove from name index
        to_remove = [norm for norm, fid in self._name_index.items() if fid == func_id]
        for norm in to_remove:
            del self._name_index[norm]
        # Remove edges referencing this function
        incident_edges = [
            edge
            for edge in self._edges
            if edge.source == func_id or edge.target == func_id
        ]
        self._edges = [e for e in self._edges if e not in incident_edges]
        self._rebuild_edge_index()
        edge_changes: list[tuple[GraphEdge, Function, SyncOperation]] = []
        for edge in incident_edges:
            scope_node = removed if edge.source == func_id else self._functions.get(edge.source)
            if scope_node is None:
                scope_node = removed
            edge_changes.append((edge, scope_node, SyncOperation.TOMBSTONE))
        self._commit_sync_changes(
            edges=edge_changes,
            nodes=[(removed, SyncNodeType.FUNCTION, SyncOperation.TOMBSTONE)],
        )

    @_with_writer_lock
    def merge(self, sub_graph: GraphData) -> MergeResult:
        # Preflight the complete node set before touching functions, indexes,
        # or edges.  GraphData is a public dataclass and may contain duck
        # objects or Functions whose IDs were mutated after construction.
        self._reload_for_mutation()
        sub_graph = copy.deepcopy(sub_graph)
        self._validate_resident_graph()
        nodes = list(sub_graph.nodes)
        seen_node_ids: set[str] = set()
        for node in nodes:
            if not isinstance(node, Function):
                raise ValueError(_GRAPH_NODES_MUST_BE_FUNCTIONS.format(backend="LiteMemoryStore"))
            validate_func_id(node.id)
            validate_domain(node.domain)
            if node.id in seen_node_ids:
                raise ValueError("LiteMemoryStore merge contains duplicate Function id")
            seen_node_ids.add(node.id)
        seen_edge_keys: set[tuple[str, str, str]] = set()
        for edge in sub_graph.edges:
            key = (edge.source, edge.target, edge.edge_type)
            if key in seen_edge_keys:
                raise ValueError("LiteMemoryStore merge contains duplicate GraphEdge key")
            seen_edge_keys.add(key)
        candidate_functions = dict(self._functions)
        candidate_functions.update({node.id: node for node in nodes})
        validate_belongs_to_edges(candidate_functions.values(), sub_graph.edges)

        result = MergeResult(merged=True)
        # Merge nodes
        for node in nodes:
            func_id = node.id
            if func_id in self._functions:
                existing = self._functions[func_id]
                existing.trigger = _merge_field_values(existing.trigger, node.trigger)
                existing.condition = _merge_field_values(existing.condition, node.condition)
                existing.action = _merge_field_values(existing.action, node.action)
                existing.benefit = _merge_field_values(existing.benefit, node.benefit)
                existing.updated_at = datetime.now(timezone.utc).isoformat()
                existing.version += 1
                result.updated_functions += 1
            else:
                self._functions[func_id] = node
                norm = _normalize_name(node.name_normalized or node.name)
                if norm:
                    self._name_index[norm] = func_id
                result.new_functions += 1

        # Merge edges (skip duplicates)
        existing_edge_keys = {(e.source, e.target, e.edge_type) for e in self._edges}
        added_edges: list[GraphEdge] = []
        for edge in sub_graph.edges:
            key = (edge.source, edge.target, edge.edge_type)
            if key not in existing_edge_keys:
                self._edges.append(edge)
                self._index_edge(edge)
                existing_edge_keys.add(key)
                added_edges.append(edge)
                result.new_edges += 1

        self._commit_sync_changes(
            edges=[
                (
                    edge,
                    self._functions[edge.source],
                    SyncOperation.UPSERT,
                )
                for edge in added_edges
            ],
            nodes=[
                (
                    self._functions[node.id],
                    SyncNodeType.FUNCTION,
                    SyncOperation.UPSERT,
                )
                for node in nodes
            ],
        )
        return result

    @_with_writer_lock
    def clear(self) -> None:
        self._reload_for_mutation()
        self._validate_resident_graph()
        functions = list(self._functions.values())
        facts = list(self._facts.values())
        preferences = list(self._preferences.values())
        observations = list(self._observations)
        edges = list(self._edges)
        function_by_id = {node.id: node for node in functions}
        self._functions.clear()
        self._name_index.clear()
        self._edges.clear()
        self._edges_by_node.clear()
        self._observations.clear()
        self._facts.clear()
        self._preferences.clear()
        self._changelog.clear()
        self._commit_sync_changes(
            edges=[
                (edge, function_by_id[edge.source], SyncOperation.TOMBSTONE)
                for edge in edges
            ],
            nodes=[
                *((node, SyncNodeType.FUNCTION, SyncOperation.TOMBSTONE) for node in functions),
                *((node, SyncNodeType.FACT, SyncOperation.TOMBSTONE) for node in facts),
                *((node, SyncNodeType.PREFERENCE, SyncOperation.TOMBSTONE) for node in preferences),
                *((node, SyncNodeType.OBSERVATION, SyncOperation.TOMBSTONE) for node in observations),
            ],
        )

    @_with_writer_lock
    def replace_function(self, func: Function) -> None:
        """Replace one Function through the durable mutation boundary.

        Unlike ``add()``, this is deliberately not a name-based merge.  It is
        the only service/maintenance API for controlled metadata or field
        replacement of an existing record.
        """
        self._reload_for_mutation()
        func = copy.deepcopy(func)
        if func.id not in self._functions:
            raise KeyError(func.id)
        validate_func_id(func.id)
        validate_domain(func.domain)
        old = self._functions[func.id]
        for norm, identifier in list(self._name_index.items()):
            if identifier == old.id:
                del self._name_index[norm]
        self._functions[func.id] = func
        norm = _normalize_name(func.name_normalized or func.name)
        if norm:
            self._name_index[norm] = func.id
        self._commit_sync_changes(
            nodes=[(func, SyncNodeType.FUNCTION, SyncOperation.UPSERT)]
        )

    @_with_writer_lock
    def annotate(self, memory_ids: List[str], *, attributes: Optional[Dict[str, Any]] = None,
                 needs_review: Optional[bool] = None) -> List[Function]:
        """Atomically apply operator metadata without exposing live objects."""
        self._reload_for_mutation()
        result: List[Function] = []
        for memory_id in memory_ids:
            func = self._functions.get(memory_id)
            if func is None:
                continue
            if attributes:
                func.attributes.update(copy.deepcopy(attributes))
            if needs_review is not None:
                func.needs_review = needs_review
            result.append(copy.deepcopy(func))
        if result:
            self._commit_sync_changes(
                nodes=[
                    (func, SyncNodeType.FUNCTION, SyncOperation.UPSERT)
                    for func in result
                ]
            )
        return result

    @_with_writer_lock
    def annotate_nodes(
        self,
        memory_ids: List[str],
        *,
        attributes: Optional[Dict[str, Any]] = None,
        needs_review: Optional[bool] = None,
    ) -> list[Any]:
        """Atomically annotate a mixed Function/Fact/Preference batch."""
        self._reload_for_mutation()
        nodes: list[Any] = []
        # Validate the entire batch before mutating one node.  Service has
        # already performed authorization; this closes partial local commits.
        for memory_id in memory_ids:
            node = self._functions.get(memory_id) or self._facts.get(memory_id) or self._preferences.get(memory_id)
            if node is None:
                raise KeyError(memory_id)
            nodes.append(node)
        for node in nodes:
            if isinstance(node, Function):
                if attributes:
                    node.attributes.update(copy.deepcopy(attributes))
            elif attributes:
                node.namespace.update(
                    {
                        str(key): str(value)
                        for key, value in attributes.items()
                        if str(key).startswith("memplex_") and value is not None
                    }
                )
            if needs_review is not None:
                node.needs_review = needs_review
        if nodes:
            self._commit_sync_changes(
                nodes=[
                    (
                        node,
                        SyncNodeType.FUNCTION
                        if isinstance(node, Function)
                        else SyncNodeType.FACT
                        if isinstance(node, Fact)
                        else SyncNodeType.PREFERENCE,
                        SyncOperation.UPSERT,
                    )
                    for node in nodes
                ]
            )
        return copy.deepcopy(nodes)

    @_with_writer_lock
    def apply_compaction(
        self,
        *,
        replacements: List[Function],
        delete_ids: List[str],
        expected_generation: Optional[int] = None,
    ) -> None:
        """Apply all Lite compaction effects as exactly one pair commit."""
        self._reload_for_mutation()
        if expected_generation is not None and self._generation != expected_generation:
            raise LiteStorageIntegrityError("Lite compaction snapshot is stale; retry from authoritative generation")
        overlap = {item.id for item in replacements}.intersection(delete_ids)
        if overlap:
            raise LiteStorageIntegrityError(
                "Lite compaction replacements and deletes overlap: " + ", ".join(sorted(overlap))
            )
        deleted_functions: list[Function] = []
        deleted_edges: list[GraphEdge] = []
        for identifier in delete_ids:
            deleted = self._functions.pop(identifier, None)
            if deleted is not None:
                deleted_functions.append(deleted)
            for norm, current in list(self._name_index.items()):
                if current == identifier:
                    del self._name_index[norm]
        deleted_edges = [
            edge
            for edge in self._edges
            if edge.source in delete_ids or edge.target in delete_ids
        ]
        self._edges = [edge for edge in self._edges if edge not in deleted_edges]
        for func in replacements:
            candidate = copy.deepcopy(func)
            self._functions[candidate.id] = candidate
            norm = _normalize_name(candidate.name_normalized or candidate.name)
            if norm:
                self._name_index[norm] = candidate.id
        self._rebuild_edge_index()
        self._commit_sync_changes(
            edges=[
                (
                    edge,
                    next(
                        (
                            node
                            for node in deleted_functions
                            if node.id in {edge.source, edge.target}
                        ),
                    ),
                    SyncOperation.TOMBSTONE,
                )
                for edge in deleted_edges
            ],
            nodes=[
                *((node, SyncNodeType.FUNCTION, SyncOperation.TOMBSTONE) for node in deleted_functions),
                *((node, SyncNodeType.FUNCTION, SyncOperation.UPSERT) for node in replacements),
            ],
        )

    @_with_writer_lock
    def rebuild_search_index(self) -> None:
        """Rebuild FTS only after a verified published snapshot."""
        self._load()
        self._fts_index.rebuild()

    # ── Persistence ─────────────────────────────────────────────────

    def _raw_memory(self) -> dict[str, Any]:
        """Serialize the complete resident state, never a partial sidecar."""
        return {
            "schema_version": 2,
            "functions": [f.to_dict() for f in self._functions.values()],
            "edges": [_serialize_edge(e) for e in self._edges],
            "observations": [o.to_dict() for o in self._observations],
            # Fact/Preference keys added under the same schema_version:
            # readers tolerate their absence (older files) via .get(..., []).
            "facts": [f.to_dict() for f in self._facts.values()],
            "preferences": [p.to_dict() for p in self._preferences.values()],
            "sync": copy.deepcopy(self._sync_state),
        }

    def _raw_changelog(self) -> list[dict[str, Any]]:
        return [self._changelog._serialize_event(event) for event in self._changelog.snapshot()]

    def _commit_current_state(self) -> None:
        """Commit one complete pair.

        Public mutators call this only after all in-memory validation.  The
        authoritative base is the last published pair: the flock is held
        continuously from reload to commit, so a fingerprint-verified
        resident base cannot be stale and the O(N) disk reload is skipped.
        A failed pre-decision commit republishes that pair and a
        post-decision failure is recovered/republished before its exception
        escapes.
        """
        self._validate_resident_graph()
        try:
            with self._durability.writer_lock():
                base = self._committed_pair
                base_verified = base is not None and self._pair_files_unchanged()
                if base is None or not base_verified:
                    base = self._durability._load_authoritative_locked()
                    base_verified = False
                target = LitePair(
                    memory=self._raw_memory(),
                    changelog=self._raw_changelog(),
                    generation=base.generation + 1,
                    transaction_id=uuid.uuid4().hex,
                )
                try:
                    # Validate the raw serializers before JSON can coerce a
                    # non-string mapping key (for example provenance {1: x})
                    # into a different durable key.
                    self._decode_pair(target)
                    # Full JSON round trip is the type boundary for the
                    # mutable dataclasses before a durable decision is made.
                    target = LitePair(
                        memory=json.loads(json.dumps(target.memory, default=_json_serializer)),
                        changelog=json.loads(json.dumps(target.changelog, default=_json_serializer)),
                        generation=target.generation,
                        transaction_id=target.transaction_id,
                    )
                    # JSON syntax is not sufficient: this is the same full
                    # model/changelog deserialization used by publication and
                    # must succeed before a journal can become durable.
                    self._decode_pair(target)
                except Exception as exc:
                    self._publish_pair(base)
                    raise LiteStorageIntegrityError(
                        "invalid Lite target payload before durable decision"
                    ) from exc
                try:
                    # Canonical-byte validation (NaN/Infinity rejection)
                    # happens inside commit_locked via the digest encodes,
                    # still strictly before any journal can be published.
                    committed = self._durability.commit_locked(
                        base,
                        target,
                        base_verified=base_verified,
                        base_record=self._committed_record if base_verified else None,
                        # ``target`` just passed this store's ``_decode_pair``
                        # — the same callable registered as the durability
                        # semantic validator — so the in-commit revalidation
                        # would be a third identical full decode.
                        target_validated=True,
                    )
                except Exception:
                    # Never retain a speculative state after a failed write.
                    self._publish_pair(self._durability._load_authoritative_locked())
                    raise
                self._publish_committed_locally(committed)
        except PermissionError as exc:
            raise PermissionError(
                f"Cannot write to storage path {self._path.parent}. "
                f"Set MEMPLEX_STORAGE_PATH to a writable directory. "
                f"Original error: {exc}"
            ) from exc

    def _pair_files_unchanged(self) -> bool:
        """Stat-based check: both pair files unchanged since the last publish."""
        if self._pair_fingerprint is None:
            return False
        try:
            memory_stat = self._path.stat()
            changelog_stat = self._path.with_name("changelog.json").stat()
        except OSError:
            return False  # stat failure → caller falls back to the authoritative load
        current = (
            memory_stat.st_mtime_ns,
            memory_stat.st_size,
            changelog_stat.st_mtime_ns,
            changelog_stat.st_size,
        )
        return current == self._pair_fingerprint

    def _reload_for_mutation(self, *, force: bool = False) -> None:
        """Refresh a pre-opened instance before it derives a write target.

        Short-circuits when both files are unchanged since the last publish
        (same fingerprint contract as ``_refresh_for_read``): the resident
        state is already the authoritative base, so the O(N) reload is
        skipped.  ``force=True`` is used by rollback paths that must discard
        a speculative resident state even when the files are unchanged.
        """
        if not force and self._committed_pair is not None and self._pair_files_unchanged():
            return
        with self._durability.writer_lock():
            self._publish_pair(self._durability._load_authoritative_locked())

    def _refresh_for_read(self) -> None:
        """Observe a peer's committed pair before returning a public value.

        Short-circuits when both files are unchanged since the last
        publish (stat-based fingerprint), so a scope=ALL query that
        triggers 6-10 read calls performs 1 full load instead of 6-10.
        """
        if self._pair_files_unchanged():
            return
        with self._durability.writer_lock():
            self._publish_pair(self._durability._load_authoritative_locked())

    def _canonical_historical_pair(
        self,
        decoded: tuple[
            Dict[str, Function],
            Dict[str, str],
            List[Observation],
            Dict[str, Fact],
            Dict[str, Preference],
            List[GraphEdge],
            List[ChangelogEvent],
            Dict[str, Any],
        ],
        generation: int,
        transaction_id: str,
    ) -> LitePair:
        """Serialize a recognized historical decode into the complete v2 shape."""
        (
            functions,
            _name_index,
            observations,
            facts,
            preferences,
            edges,
            changelog,
            _sync_state,
        ) = decoded
        return LitePair(
            memory={
                "schema_version": 2,
                "functions": [function.to_dict() for function in functions.values()],
                "edges": [_serialize_edge(edge) for edge in edges],
                "observations": [observation.to_dict() for observation in observations],
                "facts": [fact.to_dict() for fact in facts.values()],
                "preferences": [preference.to_dict() for preference in preferences.values()],
                "sync": durability_module._empty_sync_state(),
            },
            changelog=[self._changelog._serialize_event(event) for event in changelog],
            generation=generation,
            transaction_id=transaction_id,
        )

    def _load(self) -> None:
        try:
            with self._durability.writer_lock():
                pair = self._durability._load_authoritative_locked()
                # A legacy raw pair is only upgraded after the exact same model
                # decode that guards all normal publication has succeeded.
                decoded = self._decode_pair(pair)
                schema_version = pair.memory.get("schema_version")
                needs_inbound_cursor_upgrade = (
                    schema_version == 2
                    and type(pair.memory.get("sync")) is dict
                    and "inbound_cursors" not in pair.memory["sync"]
                )
                if (
                    pair.transaction_id == "legacy"
                    or schema_version == 1
                    or _is_recognized_g002_enveloped_v1(pair.memory)
                ):
                    target = self._canonical_historical_pair(
                        decoded, pair.generation + 1, uuid.uuid4().hex
                    )
                    self._decode_pair(target)
                    pair = self._durability.commit_locked(pair, target, base_verified=True)
                elif needs_inbound_cursor_upgrade:
                    # Cursor direction was not persisted by early v2 pairs.
                    # Existing rows are deliberately retained as outbound
                    # retention pins (the only lossless interpretation); a
                    # new, empty inbound namespace is added atomically.
                    target_memory = copy.deepcopy(pair.memory)
                    target_memory["sync"]["inbound_cursors"] = []
                    target = LitePair(
                        memory=target_memory,
                        changelog=copy.deepcopy(pair.changelog),
                        generation=pair.generation + 1,
                        transaction_id=uuid.uuid4().hex,
                    )
                    self._decode_pair(target)
                    pair = self._durability.commit_locked(pair, target, base_verified=True)
                self._publish_pair(pair)
        except LiteStorageIntegrityError:
            raise
        except Exception as exc:
            raise LiteStorageIntegrityError("invalid Lite authoritative payload") from exc


    @staticmethod
    def _decode_typed_collections(raw: dict, legacy: bool) -> tuple[List[Observation], Dict[str, Fact], Dict[str, Preference]]:
        """Decode + validate Observation/Fact/Preference collections in isolation."""
        loaded_observations: List[Observation] = []
        loaded_facts: Dict[str, Fact] = {}
        loaded_preferences: Dict[str, Preference] = {}
        for od in raw.get("observations", []):
            _validate_raw_observation(od, legacy=legacy)
            observation = Observation.from_dict(od)
            _validate_node_for_read(observation)
            for name in ("event", "context", "actor", "category"):
                _require_exact_string(getattr(observation, name), f"Observation {name}")
            _require_exact_string(observation.observed_at, "Observation observed_at", optional=True)
            if any(existing.id == observation.id for existing in loaded_observations):
                raise ValueError(_DUPLICATE_OBSERVATION_ID)
            loaded_observations.append(observation)

        # Older files (schema_version 1 without these keys) load as empty.
        for fd in raw.get("facts", []):
            _validate_raw_fact(fd, legacy=legacy)
            fact = Fact.from_dict(fd)
            _validate_node_for_read(fact)
            for name in ("subject", "predicate", "object_"):
                _require_exact_string(getattr(fact, name), f"Fact {name}")
            _require_exact_string(fact.valid_until, "Fact valid_until", optional=True)
            if fact.id in loaded_facts:
                raise ValueError(_DUPLICATE_FACT_ID)
            loaded_facts[fact.id] = fact

        for pd in raw.get("preferences", []):
            _validate_raw_preference(pd, legacy=legacy)
            pref = Preference.from_dict(pd)
            _validate_node_for_read(pref)
            for name in ("aspect", "preference"):
                _require_exact_string(getattr(pref, name), f"Preference {name}")
            _require_exact_string(pref.subject_id, "Preference subject_id", optional=True)
            if pref.id in loaded_preferences:
                raise ValueError(_DUPLICATE_PREFERENCE_ID)
            loaded_preferences[pref.id] = pref
        return loaded_observations, loaded_facts, loaded_preferences

    def _decode_pair(
        self, pair: LitePair
    ) -> tuple[
        Dict[str, Function],
        Dict[str, str],
        List[Observation],
        Dict[str, Fact],
        Dict[str, Preference],
        List[GraphEdge],
        List[ChangelogEvent],
        Dict[str, Any],
    ]:
        """Fully deserialize a pair without touching published resident state."""
        raw = pair.memory

        # Decode into detached collections first.  A corrupted or reserved
        # Function ID must not leave a half-loaded in-memory graph (including
        # an edge/index referring to data that was rejected later).
        loaded_functions: Dict[str, Function] = {}
        loaded_name_index: Dict[str, str] = {}
        loaded_edges: List[GraphEdge] = []
        loaded_edge_keys: set[tuple[str, str, str]] = set()
        legacy = pair.transaction_id == "legacy"
        g002_historical = not legacy and _is_recognized_g002_enveloped_v1(raw)
        loaded_observations: List[Observation] = []
        loaded_facts: Dict[str, Fact] = {}
        loaded_preferences: Dict[str, Preference] = {}

        for fd in raw.get("functions", []):
            memory_type = fd.get("memory_type") if type(fd) is dict else None
            if g002_historical and memory_type == "fact":
                _validate_raw_fact(fd, legacy=False, g002_historical=True)
                fact = Fact.from_dict(fd)
                _validate_node_for_read(fact)
                for name in ("subject", "predicate", "object_"):
                    _require_exact_string(getattr(fact, name), f"Fact {name}")
                _require_exact_string(fact.valid_until, "Fact valid_until", optional=True)
                if fact.id in loaded_facts:
                    raise ValueError(_DUPLICATE_FACT_ID)
                loaded_facts[fact.id] = fact
                continue
            if g002_historical and memory_type == "preference":
                _validate_raw_preference(fd, legacy=False, g002_historical=True)
                pref = Preference.from_dict(fd)
                _validate_node_for_read(pref)
                for name in ("aspect", "preference"):
                    _require_exact_string(getattr(pref, name), f"Preference {name}")
                _require_exact_string(pref.subject_id, "Preference subject_id", optional=True)
                if pref.id in loaded_preferences:
                    raise ValueError(_DUPLICATE_PREFERENCE_ID)
                loaded_preferences[pref.id] = pref
                continue
            if g002_historical and memory_type == "observation":
                _validate_raw_observation(fd, legacy=False, g002_historical=True)
                observation = Observation.from_dict(fd)
                _validate_node_for_read(observation)
                for name in ("event", "context", "actor", "category"):
                    _require_exact_string(getattr(observation, name), f"Observation {name}")
                _require_exact_string(observation.observed_at, "Observation observed_at", optional=True)
                if any(existing.id == observation.id for existing in loaded_observations):
                    raise ValueError(_DUPLICATE_OBSERVATION_ID)
                loaded_observations.append(observation)
                continue

            _validate_raw_function(fd, legacy=legacy, g002_historical=g002_historical)
            func = Function.from_dict(fd)
            validate_func_id(func.id)
            validate_domain(func.domain)
            _validate_function_for_read(func)
            if func.id in loaded_functions:
                raise ValueError("duplicate Lite Function id")
            loaded_functions[func.id] = func
            norm = _normalize_name(func.name_normalized or func.name)
            if norm:
                loaded_name_index[norm] = func.id

        for ed in raw.get("edges", []):
            _validate_raw_edge(ed, legacy=legacy)
            edge = _deserialize_edge(ed)
            _validate_edge_for_read(edge)
            edge_key = (edge.source, edge.target, edge.edge_type)
            if edge_key in loaded_edge_keys:
                raise ValueError("duplicate Lite GraphEdge key")
            loaded_edges.append(edge)
            loaded_edge_keys.add(edge_key)

        validate_belongs_to_edges(loaded_functions.values(), loaded_edges)
        dec_obs, dec_facts, dec_prefs = self._decode_typed_collections(raw, legacy)
        for observation in dec_obs:
            if any(existing.id == observation.id for existing in loaded_observations):
                raise ValueError(_DUPLICATE_OBSERVATION_ID)
            loaded_observations.append(observation)
        for fact_id, fact in dec_facts.items():
            if fact_id in loaded_facts:
                raise ValueError(_DUPLICATE_FACT_ID)
            loaded_facts[fact_id] = fact
        for pref_id, pref in dec_prefs.items():
            if pref_id in loaded_preferences:
                raise ValueError(_DUPLICATE_PREFERENCE_ID)
            loaded_preferences[pref_id] = pref

        loaded_changelog = [
            self._changelog._deserialize_event(event) for event in pair.changelog
        ]
        loaded_sync_state = copy.deepcopy(
            raw.get("sync", durability_module._empty_sync_state())
        )
        loaded_sync_state.setdefault("inbound_cursors", [])
        tenant_binding = loaded_sync_state["tenant_binding"]
        if tenant_binding is not None:
            loaded_nodes = (
                *loaded_functions.values(),
                *loaded_facts.values(),
                *loaded_preferences.values(),
                *loaded_observations,
            )
            if any(node.tenant_id != tenant_binding for node in loaded_nodes):
                raise ValueError("Lite sync tenant binding conflicts with business state")
        return (
            loaded_functions,
            loaded_name_index,
            loaded_observations,
            loaded_facts,
            loaded_preferences,
            loaded_edges,
            loaded_changelog,
            loaded_sync_state,
        )

    def _publish_pair(self, pair: LitePair) -> None:
        """Publish only a fully detached, already-valid decoded pair."""
        (
            loaded_functions,
            loaded_name_index,
            loaded_observations,
            loaded_facts,
            loaded_preferences,
            loaded_edges,
            loaded_changelog,
            loaded_sync_state,
        ) = self._decode_pair(pair)
        self._functions = loaded_functions
        self._name_index = loaded_name_index
        self._observations = loaded_observations
        self._facts = loaded_facts
        self._preferences = loaded_preferences
        self._edges = loaded_edges
        self._rebuild_edge_index()
        self._changelog.replace(loaded_changelog)
        self._sync_state = loaded_sync_state
        # The index keeps a reference to the old dict; rebind it whenever a
        # peer commit, recovery, or local commit publishes a new snapshot.
        self._fts_index._functions = self._functions
        # Only invalidate the FTS signature when the generation actually
        # advanced (a write); a read-observation of the same generation
        # keeps the incremental index warm.
        if pair.generation != getattr(self, "_generation", 0):
            self._fts_index._signature = None
        self._generation = pair.generation
        self._refresh_fingerprint()
        # Every published pair is the durable on-disk state at this moment;
        # the commit path reuses it as the authoritative base while the
        # fingerprint confirms the files are untouched.  The digest record
        # is only valid for a locally committed pair, so it is cleared here
        # and rebound by the commit path after a successful commit.
        self._committed_pair = pair
        self._committed_record = None

    def _refresh_fingerprint(self) -> None:
        """Record the (mtime, size) fingerprint so unchanged reads skip reload."""
        try:
            memory_stat = self._path.stat()
            changelog_stat = self._path.with_name("changelog.json").stat()
            self._pair_fingerprint = (
                memory_stat.st_mtime_ns,
                memory_stat.st_size,
                changelog_stat.st_mtime_ns,
                changelog_stat.st_size,
            )
        except OSError:
            self._pair_fingerprint = None

    def _publish_committed_locally(self, committed: LitePair) -> None:
        """Bind commit results without re-decoding the freshly written pair.

        The resident models are exactly what ``_raw_memory`` /
        ``_raw_changelog`` serialized into ``committed``, and that payload
        passed the full decode validation twice before the durable decision,
        so re-decoding it here would rebuild equivalent objects at O(N) cost
        per write.  Only the commit metadata and caches need updating.
        """
        self._rebuild_edge_index()
        # The index keeps a reference to the functions dict; the resident
        # dict identity is unchanged, but keep the rebind for parity with
        # ``_publish_pair``.
        self._fts_index._functions = self._functions
        # A commit always advances the generation: drop the FTS signature so
        # the next read rebuilds the incremental index once.
        self._fts_index._signature = None
        self._generation = committed.generation
        self._refresh_fingerprint()
        self._committed_pair = committed
        # The record of the pair just committed becomes the next commit's
        # journal base_record without re-encoding the base digests.
        self._committed_record = self._durability._last_commit_target_record

    @property
    def generation(self) -> int:
        """Generation of the last verified published pair."""
        self._refresh_for_read()
        return self._generation

    @_with_writer_lock
    def compaction_snapshot(self) -> tuple[int, List[Function]]:
        """Return one detached function snapshot bound to its generation."""
        self._refresh_for_read()
        return self._generation, copy.deepcopy(list(self._functions.values()))

    def _validate_resident_graph(self) -> None:
        """Reject mutable Function/domain or virtual-edge drift before saving."""
        validate_belongs_to_edges(self._functions.values(), self._edges)

    # ── Internal helpers ────────────────────────────────────────────

    def _search_with_fallback(self, text: str, top_k: int) -> List[SearchResult]:
        """Search with SQLite FTS5 first, then pure-Python local search.

        Facts and preferences are always searched with the pure-Python
        BM25 path (they have no FTS5 sidecar rows) and merged with the
        Function hits by relevance score.
        """
        try:
            results = self._sqlite_fts_search(text, top_k=top_k)
        except sqlite3.Error as exc:
            logger.debug("SQLite FTS5 search unavailable: %s", exc)
            results = []

        if not results:
            results = self._local_search(text, top_k=top_k)

        extra = self._search_facts_preferences(text, top_k=top_k)
        if not extra:
            return results
        merged = results + extra
        merged.sort(key=lambda r: r.relevance_score, reverse=True)
        return merged[:top_k]

    def _search_facts_preferences(self, text: str, top_k: int) -> List[SearchResult]:
        """Pure-Python BM25 over Fact/Preference content (no sidecar index).

        Mirrors the Function local-search scoring (``score / (score + 1)``)
        so results merge cleanly with Function hits.
        """
        nodes: Dict[str, Any] = {**self._facts, **self._preferences}
        if not nodes:
            return []
        ranked = local_bm25_search(
            text=text,
            functions=nodes,
            text_factory=self._fact_pref_to_search_text,
            top_k=top_k,
        )
        results: List[SearchResult] = []
        for node, node_text, score in ranked:
            relevance = score / (score + 1.0)
            results.append(
                SearchResult(
                    func_id=node.id,
                    name=node.name or node_text[:50],
                    domain=node.domain or "",
                    relevance_score=relevance,
                    summary=node_text,
                    source_type=node.source_type,
                    created_at=node.created_at,
                    updated_at=node.updated_at,
                    origin=node.origin_session or "",
                )
            )
        return results

    @staticmethod
    def _fact_pref_to_search_text(node: Any) -> str:
        """Flatten a Fact/Preference into searchable text."""
        if isinstance(node, Fact):
            parts = [node.name, node.domain or "", node.subject, node.predicate, node.object_]
        else:
            parts = [node.name, node.domain or "", node.aspect, node.preference]
        return " ".join(p for p in parts if p)

    def _sqlite_fts_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Search the SQLite FTS5 sidecar using bm25() plus trigram overlap."""
        ranked = self._fts_index.search(text, top_k=top_k)
        results: List[SearchResult] = []
        for func_id, score in ranked:
            func = self._functions.get(func_id)
            if func is None:
                continue
            func_text = self._function_to_search_text(func)
            relevance = score / (score + 1.0)
            results.append(
                SearchResult(
                    func_id=func.id,
                    name=func.name,
                    domain=func.domain or "",
                    relevance_score=relevance,
                    summary=func_text,
                    source_type=func.source_type,
                    created_at=func.created_at,
                    updated_at=func.updated_at,
                    origin=func.origin_session or "",
                )
            )
        return results

    def _local_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Search Functions with local BM25 and fuzzy character overlap."""
        ranked = local_bm25_search(
            text=text,
            functions=self._functions,
            text_factory=self._function_to_search_text,
            top_k=top_k,
        )
        results: List[SearchResult] = []
        for func, func_text, score in ranked:
            relevance = score / (score + 1.0)
            results.append(
                SearchResult(
                    func_id=func.id,
                    name=func.name,
                    domain=func.domain or "",
                    relevance_score=relevance,
                    summary=func_text,
                    source_type=func.source_type,
                    created_at=func.created_at,
                    updated_at=func.updated_at,
                    origin=func.origin_session or "",
                )
            )
        return results

    @staticmethod
    def _function_to_search_text(func: Function) -> str:
        parts = [func.name, func.domain or ""]
        for fv in func.trigger:
            parts.append(fv.desc)
        for fv in func.action:
            parts.append(fv.desc)
        for fv in func.benefit:
            parts.append(fv.desc)
        return " ".join(parts)

    @staticmethod
    def _matches_filter(func: Function, filters: SearchFilters) -> bool:
        if filters.domain and func.domain not in filters.domain:
            return False
        if filters.source_type and func.source_type not in filters.source_type:
            return False
        if filters.confidence_min is not None:
            if func.confidence < filters.confidence_min:
                return False
        if filters.owner is not None and func.owner != filters.owner:
            return False
        if filters.needs_review is not None:
            if func.needs_review != filters.needs_review:
                return False
        # Datetime filters: compare ISO strings lexicographically
        if filters.updated_after is not None:
            after = (
                filters.updated_after.isoformat()
                if hasattr(filters.updated_after, "isoformat")
                else str(filters.updated_after)
            )
            if func.updated_at and func.updated_at < after:
                return False
        if filters.updated_before is not None:
            before = (
                filters.updated_before.isoformat()
                if hasattr(filters.updated_before, "isoformat")
                else str(filters.updated_before)
            )
            if func.updated_at and func.updated_at > before:
                return False
        return True
