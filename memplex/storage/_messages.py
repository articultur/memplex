"""Shared error-message constants for the storage backends.

Both storage backends (``lite`` and ``postgres``) raise identical wording
in several places; keep those strings here so the two backends cannot drift
apart.  Deliberately dependency-free (stdlib only), mirroring
``migrations/_constants.py``.  Messages with a varying prefix carry a
placeholder (``{backend}`` / ``{field}``) and are formatted at the raise
site, so runtime text stays byte-identical to the former literals.
"""

from __future__ import annotations

from typing import Final

# Graph-node validation shared by both backends; the ``{backend}`` prefix is
# "LiteMemoryStore" or "PostgreSQL" at the raise site.
_ONLY_FUNCTION_NODES: Final[str] = "{backend} 只接受 Function 节点"
_GRAPH_NODES_MUST_BE_FUNCTIONS: Final[str] = "{backend} 图节点必须是 Function"

# Backup restore integrity failures: one fixed code for every cause so a
# caller can match on it without enumerating parse/verify details.
_BACKUP_ARTIFACT_INVALID: Final[str] = "backup_artifact_invalid"

# PostgreSQL write-path guard: an INSERT/UPDATE/DELETE that matched no row
# the caller is authorized to touch.
_PG_WRITE_NO_AUTHORIZED_ROW: Final[str] = (
    "PostgreSQL write did not affect an authorized row"
)

# Lite typed-collection load rejections (duplicate stable IDs).
_DUPLICATE_OBSERVATION_ID: Final[str] = "duplicate Lite Observation id"
_DUPLICATE_FACT_ID: Final[str] = "duplicate Lite Fact id"
_DUPLICATE_PREFERENCE_ID: Final[str] = "duplicate Lite Preference id"

# Lite raw-payload field validators; ``{field}`` is the rejected field label.
_INVALID_LITE_FIELD: Final[str] = "invalid Lite {field}"
