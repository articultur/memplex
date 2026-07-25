"""Memplex storage layer -- MemoryStore interface and backends."""

from typing import Optional

from memplex.storage.base import MemoryStore
from memplex.storage.changelog import ChangelogStore
from memplex.storage.feedback import (
    FeedbackStore,
    LiteFeedbackStore,
    PostgresFeedbackStore,
    SQLiteFeedbackStore,
    create_feedback_store,
)
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.vector import (
    ChromaVectorStore,
    InMemoryVectorStore,
    VectorStore,
    create_vector_store,
)


def create_store(
    config=None,
    **kwargs,
) -> MemoryStore:
    """Factory: create a MemoryStore.

    Accepts either a MemplexConfig object or a backend string.
    Falls back to 'lite' for unsupported backends.
    """
    if config is not None and hasattr(config, "storage"):
        backend = config.storage.backend
        storage_path = config.storage.path
    else:
        backend = config if isinstance(config, str) else kwargs.get("backend", "lite")
        storage_path = kwargs.get("path")

    if backend == "postgres":
        # Postgres DSN comes via storage.path (or MEMPLEX_STORAGE_PATH).
        if not storage_path:
            raise ValueError("Postgres backend requires a DSN in storage.path")
        from memplex.storage.postgres import PostgresMemoryStore

        store = PostgresMemoryStore(dsn=str(storage_path))
        from memplex.sync import maybe_wrap_sync

        return maybe_wrap_sync(store)
    if backend in ("lite", "standard", "enterprise"):
        from pathlib import Path

        path = Path(storage_path).expanduser() / "memory.json" if storage_path else None
        try:
            store = LiteMemoryStore(path=path)
        except Exception:
            store = LiteMemoryStore()
        # Multi-node sharing: when MEMPLEX_REMOTE_URL is set, wrap the local
        # store so writes push to and reads can pull from a central server.
        # When the env var is unset, maybe_wrap_sync returns the store
        # unchanged -- zero behaviour change for single-machine users.
        from memplex.sync import maybe_wrap_sync

        return maybe_wrap_sync(store)
    raise ValueError(f"Unknown storage backend: {backend!r}. Supported: 'lite', 'postgres'.")


__all__ = [
    "MemoryStore",
    "LiteMemoryStore",
    "ChangelogStore",
    "VectorStore",
    "InMemoryVectorStore",
    "ChromaVectorStore",
    "create_vector_store",
    "FeedbackStore",
    "LiteFeedbackStore",
    "SQLiteFeedbackStore",
    "PostgresFeedbackStore",
    "create_feedback_store",
    "create_store",
]
