"""Memplex storage layer -- MemoryStore interface and backends."""

import logging
from typing import TYPE_CHECKING, Optional

from memplex.storage.base import MemoryStore
from memplex.storage.changelog import ChangelogStore
from memplex.storage.feedback import (
    FeedbackStore,
    LiteFeedbackStore,
    PostgresFeedbackStore,
    SQLiteFeedbackStore,
    create_feedback_store,
)
from memplex.storage.lite.durability import LiteStorageIntegrityError
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.vector import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
    create_vector_store,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from memplex.storage.feedback import PostgresFeedbackStore
    from memplex.storage.postgres import PostgresMemoryStore


def _unwrap_postgres_for_migration(
    store: object,
) -> "PostgresMemoryStore | PostgresFeedbackStore":
    """Return a local PostgreSQL backend without activating SyncableStore.

    Migration diagnostics are local database maintenance.  Looking through a
    ``SyncableStore`` must therefore be a plain attribute read rather than a
    push, pull, or service-level operation.
    """
    from memplex.storage.feedback import PostgresFeedbackStore
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.sync import SyncableStore

    local = store.local if isinstance(store, SyncableStore) else store
    if isinstance(local, (PostgresMemoryStore, PostgresFeedbackStore)):
        return local
    raise TypeError("migration diagnostics require a local PostgreSQL store")


def create_store(
    config=None,
    **kwargs,
) -> MemoryStore:
    """Factory: create a MemoryStore.

    Accepts either a MemplexConfig object or a backend string.
    Unknown backends raise ``ValueError``; ``"standard"``/``"enterprise"``
    are roadmap names that currently map to the ``lite`` backend (a
    warning is logged).
    """
    if config is not None and hasattr(config, "storage"):
        from memplex.config import validate_deployment_contract

        validate_deployment_contract(config)
        backend = config.storage.backend
        storage_path = config.storage.path
    else:
        backend = config if isinstance(config, str) else kwargs.get("backend", "lite")
        storage_path = kwargs.get("path")

    if backend == "postgres":
        # Postgres DSN comes via storage.path (or MEMPLEX_STORAGE_PATH).
        if not storage_path:
            raise ValueError("Postgres backend requires a DSN in storage.path")
        from memplex.storage.pool import validate_ready_postgres_pool
        from memplex.storage.postgres import PostgresMemoryStore
        from memplex.sync_repository import SyncCapturePolicy

        ready_pool = kwargs.get("ready_pool")
        ready_pool = validate_ready_postgres_pool(ready_pool)

        # The service enables this for the production deployment profile.
        # Keep the factory's historic standalone behaviour by making the
        # requirement opt-in for callers which do not carry a deployment
        # contract (for example local migration tools).
        store = PostgresMemoryStore(
            dsn=str(storage_path),
            require_authorization=bool(kwargs.get("require_authorization", False)),
            ready_pool=ready_pool,
            inbound_executor=kwargs.get("inbound_executor"),
            sync_capture_policy=kwargs.get("sync_capture_policy", SyncCapturePolicy("off")),
            sync_max_attempts=kwargs.get("sync_max_attempts", 8),
            sync_snapshot_ttl_seconds=kwargs.get("sync_snapshot_ttl_seconds", 900),
            sync_max_snapshot_items=kwargs.get("sync_max_snapshot_items", 1000000),
            sync_max_active_snapshots_per_tenant=kwargs.get(
                "sync_max_active_snapshots_per_tenant", 2
            ),
            sync_max_active_snapshots_per_remote=kwargs.get(
                "sync_max_active_snapshots_per_remote", 1
            ),
            sync_snapshot_create_timeout_seconds=kwargs.get(
                "sync_snapshot_create_timeout_seconds", 30
            ),
            sync_consumer_ttl_seconds=kwargs.get("sync_consumer_ttl_seconds", 86400),
            sync_retention_min_seconds=kwargs.get("sync_retention_min_seconds", 86400),
        )
        capture_policy = kwargs.get("sync_capture_policy")
        if (
            type(capture_policy) is SyncCapturePolicy
            and capture_policy.mode == "required"
        ):
            return store
        from memplex.sync import maybe_wrap_sync

        return maybe_wrap_sync(store)
    if backend in ("lite", "standard", "enterprise"):
        from pathlib import Path

        if backend != "lite":
            logger.warning(
                "Storage backend %r is not implemented yet; using the 'lite' backend.",
                backend,
            )
        path = Path(storage_path).expanduser() / "memory.json" if storage_path else None
        try:
            sync_keys = {
                "deployment_profile",
                "sync_capture_policy",
                "sync_max_pending_events",
                "sync_max_attempts",
                "sync_snapshot_ttl_seconds",
                "sync_max_snapshot_items",
                "sync_max_active_snapshots_per_tenant",
                "sync_max_active_snapshots_per_remote",
                "sync_consumer_ttl_seconds",
                "sync_retention_min_seconds",
            }
            lite_sync_kwargs = {
                key: kwargs[key] for key in sync_keys if key in kwargs
            }
            store = LiteMemoryStore(path=path, **lite_sync_kwargs)
        except LiteStorageIntegrityError:
            # A half pair or missing POSIX lock is a data-integrity failure,
            # never a reason to silently select a different directory.
            raise
        except Exception as exc:
            if storage_path is not None:
                # An explicit Lite location is an operator-selected state
                # boundary.  A constructor failure must be observable rather
                # than silently opening a second, unrelated default library.
                raise
            logger.warning(
                "Failed to create LiteMemoryStore at configured path %s (%s); "
                "falling back to the default ~/.memplex location.",
                storage_path,
                exc,
            )
            store = LiteMemoryStore()
        # Multi-node sharing: when MEMPLEX_REMOTE_URL is set, wrap the local
        # store so writes push to and reads can pull from a central server.
        # When the env var is unset, maybe_wrap_sync returns the store
        # unchanged -- zero behaviour change for single-machine users.
        from memplex.sync_repository import SyncCapturePolicy

        capture_policy = kwargs.get("sync_capture_policy")
        if (
            type(capture_policy) is SyncCapturePolicy
            and capture_policy.mode == "required"
        ):
            return store
        from memplex.sync import maybe_wrap_sync

        return maybe_wrap_sync(store)
    raise ValueError(f"Unknown storage backend: {backend!r}. Supported: 'lite', 'postgres'.")


__all__ = [
    "MemoryStore",
    "LiteMemoryStore",
    "LiteStorageIntegrityError",
    "ChangelogStore",
    "VectorStore",
    "InMemoryVectorStore",
    "ChromaVectorStore",
    "create_vector_store",
    "FeedbackStore",
    "LiteFeedbackStore",
    "SQLiteFeedbackStore",
    "PostgresFeedbackStore",
    "PostgresPoolManager",
    "PostgresStorageResources",
    "ReadyPostgresPool",
    "create_feedback_store",
    "create_store",
]

from memplex.storage.pool import (
    PostgresPoolManager,
    PostgresStorageResources,
    ReadyPostgresPool,
)
