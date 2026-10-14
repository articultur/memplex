"""Native source admission and permanently quarantined controlled restores.

Configuration and backup metadata never issue a capability. The private
factories below are called only by audited native resource construction.
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from memplex.config import MemplexConfig, SourceContractConfig, postgres_dsn_identity
from memplex.source_codec import decode_ledger
from memplex.source_contract import AuthorityScope, RejectionCode, SourceContractError, SourceState
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceCapability,
    SourceValidationHooks,
    _ResourceSeal,
)

_MARKER_PREFIX = "memplex_source_v1:"


def _deny() -> NoReturn:
    raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)


def _scope(config: SourceContractConfig) -> AuthorityScope:
    config.validate()
    if config.mode != "v1":
        _deny()
    return AuthorityScope(cast(str, config.authority_id), cast(str, config.tenant_id), cast(str, config.workspace_id))


def _marker_path(durability: Any) -> Path:
    return durability._memory_path.with_name(durability._memory_path.name + ".source-admission.json")


def _lite_identity(durability: Any) -> str:
    return str(durability._memory_path.resolve())


def _read_lite_marker(durability: Any) -> dict[str, Any] | None:
    path = _marker_path(durability)
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SourceContractError(RejectionCode.QUARANTINED) from exc
    if (type(value) is not dict or type(value.get("version")) is not int or value.get("version") != 1
            or value.get("storage_identity") != _lite_identity(durability)
            or value.get("state") not in {"initializing", "live", "quarantined"}):
        raise SourceContractError(RejectionCode.QUARANTINED)
    return value


def _write_lite_marker(durability: Any, marker: dict[str, Any], *, exclusive: bool) -> None:
    from memplex.storage.lite.durability import _fsync_dir, _replace_and_fsync

    path = _marker_path(durability)
    if exclusive:
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(json.dumps(marker, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_dir(path.parent)
    else:
        _replace_and_fsync(path, marker)
    if _read_lite_marker(durability) != marker:
        raise SourceContractError(RejectionCode.QUARANTINED)


def _lite_marker(durability: Any, scope: AuthorityScope, state: str) -> dict[str, Any]:
    return {"version": 1, "storage_identity": _lite_identity(durability),
            "scope": asdict(scope), "state": state}


def _check_lite_live(durability: Any, scope: AuthorityScope | None, pair: Any) -> None:
    if scope is None:
        _deny()
    marker = _read_lite_marker(durability)
    if marker != _lite_marker(durability, scope, "live"):
        raise SourceContractError(RejectionCode.QUARANTINED)
    if pair.memory.get("schema_version") != 3:
        _deny()
    authority = decode_ledger(pair.memory["source_validity"]).authority
    if authority.scope != scope or authority.state is not SourceState.LIVE:
        raise SourceContractError(RejectionCode.QUARANTINED)


def _admit_lite(store: Any, config: SourceContractConfig, *, issuer: object) -> SourceCapability:
    from memplex.storage.lite.durability import _empty_sync_state
    from memplex.storage.lite.store import LiteMemoryStore

    scope = _scope(config)
    if (issuer is not _RESOURCE_ISSUER or type(store) is not LiteMemoryStore
            or os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "")):
        _deny()
    backend: Any = store.source_versions()
    if (backend is None or backend._hooks is None or backend._scope != scope
            or store._sync_repository._capture_policy.mode != "off"):
        _deny()
    with store._durability.writer_lock():
        pair = store._durability._load_authoritative_locked()
        _check_lite_live(store._durability, scope, pair)
        if pair.memory.get("sync") != _empty_sync_state():
            _deny()
    capability = SourceCapability(issuer=issuer, resource=store, backend="lite_json",
                                  scope=scope, contract_version=1, state=SourceState.LIVE)
    cast(Any, store)._source_capability = capability
    return capability


def assert_v1_admission(config: MemplexConfig, capability: SourceCapability) -> None:
    """Check the seal, current native binding and exact requested authority."""
    if (type(config) is not MemplexConfig or type(config.source_contract) is not SourceContractConfig
            or config.sync.enabled is not False or os.environ.get("MEMPLEX_REMOTE_URL", "")
            or type(capability) is not SourceCapability
            or capability._issuer is not _RESOURCE_ISSUER or capability.scope != _scope(config.source_contract)
            or capability.contract_version != 1 or capability.state is not SourceState.LIVE):
        _deny()
    store: Any = capability._resource
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.postgres import PostgresMemoryStore

    if getattr(store, "_source_capability", None) is not capability:
        _deny()
    if type(store) is LiteMemoryStore and capability.backend == "lite_json":
        if (config.storage.backend != "lite" or Path(config.storage.path).expanduser().resolve() != store._path.parent.resolve()
                or os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
                or store._sync_repository._capture_policy.mode != "off"):
            _deny()
        store._refresh_for_read()
        _check_lite_live(store._durability, capability.scope, store._committed_pair)
    elif type(store) is PostgresMemoryStore and capability.backend == "postgres":
        backend = cast(Any, store.source_versions())
        if (config.storage.backend != "postgres" or backend is None
                or postgres_dsn_identity(config.storage.path) != postgres_dsn_identity(backend._resource.dsn)
                or backend._scope != capability.scope or backend._hooks is None
                or backend._resource._source_capacity is None
                or backend._resource.ready_pool is not store._ready_pool):
            _deny()
    else:
        _deny()


def _audited_hooks() -> SourceValidationHooks:
    from memplex.authorization import is_own_node_visible, source_authorization_node
    from memplex.llm.injection_guard import IndirectInjectionGuard

    return SourceValidationHooks(
        lambda snapshot, context: is_own_node_visible(source_authorization_node(snapshot), context),
        lambda snapshot: not IndirectInjectionGuard.is_suspected(source_authorization_node(snapshot)),
    )


class _LiteOpenAuthority(_ResourceSeal):
    __slots__ = ("hooks", "scope")
    scope: AuthorityScope
    hooks: SourceValidationHooks

    def __init__(self, path: Path, scope: AuthorityScope, *, issuer: object) -> None:
        super().__init__(issuer=issuer, resource=path.resolve())
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "hooks", _audited_hooks())


def _open_lite(path: Path, config: SourceContractConfig, *, issuer: object,
               deployment_profile: str = "development") -> Any:
    from memplex.storage.lite.store import LiteMemoryStore

    authority = _LiteOpenAuthority(path, _scope(config), issuer=issuer)
    store = LiteMemoryStore(path, deployment_profile=deployment_profile, _source_open=authority)
    _admit_lite(store, config, issuer=issuer)
    return store


def reject_legacy_writer(store: Any) -> None:
    """Reject an unadapted producer before model or filesystem side effects."""
    from memplex.storage.lite.store import LiteMemoryStore
    from memplex.storage.postgres import PostgresMemoryStore, _AuthorizedPostgresStore
    from memplex.sync import SyncableStore, _AuthorizedSyncableStore

    seen: set[int] = set()
    while type(store) in {_AuthorizedPostgresStore, SyncableStore, _AuthorizedSyncableStore}:
        if id(store) in seen:
            _deny()
        seen.add(id(store))
        if type(store) is _AuthorizedPostgresStore:
            if type(store._store) is not PostgresMemoryStore:
                _deny()
            store = store._store
        elif type(store) is _AuthorizedSyncableStore:
            if type(store._store) is not SyncableStore:
                _deny()
            store = store._store
        else:
            store = store._local

    if type(store) is LiteMemoryStore:
        marker = _read_lite_marker(store._durability)
        pair = store._durability.load_authoritative()
        if marker is not None or pair.memory.get("schema_version") == 3:
            _deny()
    elif type(store) is PostgresMemoryStore:
        if store.source_versions() is not None:
            _deny()
        # An older, unregistered resource must observe the global marker too.
        from memplex.auth import local_development_context
        from memplex.storage.postgres_resources import _source_schema_marker
        with closing(store._pool_manager.read_cursor(store._bind_transaction_scope, local_development_context())) as cursor:
            if _source_schema_marker(cursor, store._ready_pool.target.schema) is not None:
                _deny()


@dataclass(frozen=True, slots=True)
class RestoreTarget:
    backend: Literal["lite_json", "postgres"]
    scope: AuthorityScope
    storage_identity: str

    def __post_init__(self) -> None:
        if (self.backend not in {"lite_json", "postgres"} or type(self.scope) is not AuthorityScope
                or type(self.storage_identity) is not str or not self.storage_identity):
            _deny()


@dataclass(frozen=True, slots=True)
class RestoreQuarantine:
    restore_id: str
    scope: AuthorityScope
    state: Literal["quarantined"] = "quarantined"


class RestoreAuthority(_ResourceSeal):
    __slots__ = ("receipt", "target")
    target: RestoreTarget
    receipt: RestoreQuarantine

    def __init__(self, *, issuer: object = None, resource: object = None,
                 target: RestoreTarget | None = None) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if type(target) is not RestoreTarget:
            _deny()
        assert target is not None
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "receipt", RestoreQuarantine(str(uuid.uuid4()), target.scope))


def _lite_restore_authority(store: Any, scope: AuthorityScope, *, issuer: object) -> RestoreAuthority:
    from memplex.storage.lite.store import LiteMemoryStore

    if issuer is not _RESOURCE_ISSUER or type(store) is not LiteMemoryStore:
        _deny()
    return RestoreAuthority(issuer=issuer, resource=store,
                            target=RestoreTarget("lite_json", scope, _lite_identity(store._durability)))


def _check_restore(authority: RestoreAuthority, resource: Any) -> dict[str, Any]:
    if (type(authority) is not RestoreAuthority or authority._issuer is not _RESOURCE_ISSUER
            or authority._resource is not resource):
        _deny()
    resource = cast(Any, resource)
    marker = _lite_marker(resource._durability, authority.target.scope, "quarantined")
    marker["restore_id"] = authority.receipt.restore_id
    if _read_lite_marker(resource._durability) != marker:
        raise SourceContractError(RejectionCode.QUARANTINED)
    return marker


def begin_v1_restore(target: RestoreTarget, *, authority: RestoreAuthority) -> RestoreQuarantine:
    if (type(authority) is not RestoreAuthority or authority._issuer is not _RESOURCE_ISSUER
            or type(target) is not RestoreTarget or target != authority.target):
        _deny()
    if target.backend == "postgres":
        cast(Any, authority._resource)._begin_source_restore(authority)
        return authority.receipt
    store: Any = authority._resource
    from memplex.storage.lite.durability import _empty_sync_state
    from memplex.storage.lite.store import LiteMemoryStore

    if type(store) is not LiteMemoryStore or target.storage_identity != _lite_identity(store._durability):
        _deny()
    with store._durability.writer_lock():
        base = store._durability._load_authoritative_locked()
        sqlite = store._path.parent / "shadow_v2.sqlite3"
        if (os.environ.get("MEMPLEX_LITE_SQLITE_AUTHORITY", "")
                or any(p.exists() for p in (sqlite, sqlite.with_name(sqlite.name + "-wal"),
                                             sqlite.with_name(sqlite.name + "-shm")))
                or _read_lite_marker(store._durability) is not None
                or base.generation != 0 or base.transaction_id != "empty" or base.changelog
                or any(base.memory.get(n) for n in ("facts", "functions", "preferences", "observations", "paragraphs", "edges"))
                or base.memory.get("sync") != _empty_sync_state()
                or store._sync_repository._capture_policy.mode != "off"):
            _deny()
        marker = _lite_marker(store._durability, target.scope, "quarantined")
        marker["restore_id"] = authority.receipt.restore_id
        _write_lite_marker(store._durability, marker, exclusive=True)
        _check_restore(authority, store)
    return authority.receipt
