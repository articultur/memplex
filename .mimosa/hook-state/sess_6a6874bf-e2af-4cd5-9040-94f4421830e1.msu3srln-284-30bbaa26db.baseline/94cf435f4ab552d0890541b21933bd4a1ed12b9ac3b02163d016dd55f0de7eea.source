"""Layer-neutral serialization helpers.

``dataclass_to_dict`` was originally an adapter-internal helper, but the sync
domain also needs JSON-friendly node serialization. It lives in this leaf
module so the domain layer never has to import from ``memplex.adapters``
(the import-linter contract "Domain and storage layers never import host
adapters" enforces exactly that). ``memplex.adapters._shared`` re-exports it
for import-path stability.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any


def dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses to plain JSON-serializable values.

    Handles ``Enum`` (-> .value), ``datetime`` (-> isoformat), ``list``,
    ``dict``, and ``__dataclass_fields__`` containers. Any other leaf is
    returned unchanged. This is the canonical serializer shared by all
    adapters (CLI/MCP/HTTP) and the sync domain, so Enum/datetime leaves are
    never a surprise.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {f: dataclass_to_dict(getattr(obj, f)) for f in obj.__dataclass_fields__}
    if isinstance(obj, (list, tuple)):
        return [dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    return obj
