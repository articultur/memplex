"""Shared schema constants and data classes for the migration cluster.

This module is deliberately dependency-free (stdlib only) so ``runner`` and
the four split-out modules (``catalogue_checks``, ``acl_verification``,
``ledger_state``, ``catalogue_snapshot``) can all import the same names from
here in any order.  ``runner`` re-exports everything via its own
``from ..._constants import ...`` so existing ``from ...runner import X``
paths keep resolving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

_LEDGER_TABLE: Final[str] = "memplex_schema_migrations"
_CAPABILITIES_TABLE: Final[str] = "memplex_schema_capabilities"
_MAX_VECTOR_DIM: Final[int] = 16_000
_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_facts",
    "memplex_preferences",
    "memplex_changelog",
)
_SYNC_TABLES: Final[tuple[str, ...]] = (
    "memplex_sync_outbox",
    "memplex_sync_entity_versions",
    "memplex_sync_inbox",
    "memplex_sync_batches",
    "memplex_sync_targets",
    "memplex_sync_deliveries",
    "memplex_sync_cursors",
    "memplex_sync_stream_state",
    "memplex_sync_local_identity",
    "memplex_sync_ingress_principals",
    "memplex_sync_snapshots",
    "memplex_sync_snapshot_items",
)
_TASK_TABLES: Final[tuple[str, ...]] = ("memplex_background_tasks",)
_LEGACY_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_changelog",
)
_MANAGED_TABLES: Final[tuple[str, ...]] = (
    *_CORE_TABLES,
    *_SYNC_TABLES,
    *_TASK_TABLES,
    "feedback",
    _CAPABILITIES_TABLE,
    _LEDGER_TABLE,
)
_APPLICATION_ACL_TABLES: Final[tuple[str, ...]] = (
    *_CORE_TABLES,
    "feedback",
    *_SYNC_TABLES,
    *_TASK_TABLES,
)
_APPLICATION_ACL: Final[dict[str, frozenset[str]]] = {
    "memplex_functions": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_edges": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_observations": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_facts": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_preferences": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_changelog": frozenset({"SELECT", "INSERT", "DELETE"}),
    "feedback": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_outbox": frozenset({"SELECT", "INSERT", "DELETE"}),
    "memplex_sync_entity_versions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_inbox": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_batches": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_targets": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_deliveries": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_cursors": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_stream_state": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_local_identity": frozenset(),
    "memplex_sync_ingress_principals": frozenset(),
    "memplex_sync_snapshots": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_snapshot_items": frozenset({"SELECT", "INSERT", "DELETE"}),
    "memplex_background_tasks": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
}
_APPLICATION_ACL_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
)
_SYNC_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_configure_sync_local_identity",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_configure_sync_ingress_principal",
    "memplex_sync_apply_inbound",
    "memplex_sync_require_canonical_entity_key",
    "memplex_sync_require_canonical_version",
    "memplex_sync_encode_string_array",
)
_ACL_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "owner_subject",
    "workspace",
    "visibility",
    "source_agent",
    "source_session",
)

# ``pg_class`` is the authoritative managed-object namespace. PostgreSQL
# represents primary-key backing indexes and BIGSERIAL sequences there too, so
# scanning only ordinary relations would let a memplex-named object hide behind
# an ignored relkind.
_KNOWN_MEMPLEX_RELATION_KINDS: Final[dict[str, str]] = {
    **{name: "r" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    **{name: "r" for name in _SYNC_TABLES},
    **{name: "r" for name in _TASK_TABLES},
    **{f"{name}_pkey": "i" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    "memplex_changelog_id_seq": "S",
    "memplex_sync_local_identity_pkey": "i",
    "memplex_sync_local_identity_node_id_key": "i",
    "memplex_sync_ingress_principals_pkey": "i",
    "memplex_functions_tenant_updated_idx": "i",
    "memplex_functions_tenant_idx": "i",
    "memplex_edges_tenant_source_type_target_idx": "i",
    "memplex_edges_tenant_target_type_source_idx": "i",
    "memplex_edges_tenant_idx": "i",
    "memplex_edges_tenant_target_function_idx": "i",
    "memplex_observations_tenant_idx": "i",
    "memplex_facts_tenant_idx": "i",
    "memplex_preferences_tenant_idx": "i",
    "memplex_changelog_tenant_idx": "i",
    "memplex_functions_workspace_normalized_name_key": "i",
    "memplex_functions_user_normalized_name_key": "i",
    "memplex_functions_session_normalized_name_key": "i",
    **{f"{name}_pkey": "i" for name in _SYNC_TABLES},
    "memplex_sync_targets_tenant_id_remote_node_id_key": "i",
    "memplex_sync_snapshots_tenant_id_remote_id_consumer_id_requ_key": "i",
    "memplex_sync_outbox_tenant_id_origin_node_id_event_id_key": "i",
    "memplex_sync_outbox_stream_seq_seq": "S",
    "memplex_sync_outbox_tenant_stream_idx": "i",
    "memplex_sync_deliveries_claim_idx": "i",
    "memplex_sync_deliveries_retention_idx": "i",
    "memplex_sync_cursors_tenant_after_idx": "i",
    "memplex_sync_snapshots_expiry_idx": "i",
    "memplex_background_tasks_pkey": "i",
    "memplex_background_tasks_due_idx": "i",
    "memplex_background_tasks_lease_idx": "i",
}

_CORE_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "966aa3ee5c0224eafbba062f2bfee28f4a34b0f0f86f9ee23b72e6f62d08de0d",
    "3f1fe3be69457e86e5c3f595d60141abcd127e987d5a7d01eab84f871c0d4493",
)
_FEEDBACK_CURRENT_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "5b5ddc963fccdcb941c7eccbcb646b053ef079d7bf47272964fc184891152025",
    "f2c2439d4d159bdc189dc2cd46d116b69799483dd879af36d05f9b03ee98e679",
)
_FEEDBACK_RUNTIME_V1_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "dfa495e062435c70a0cbd2e3b971cdce89abc48a19b7a215897ad7b7fe03e5c2",
    "8c8dd0553aab40d8e7c042fd759fa97ded54317596a291407d6a864fda2cb5c6",
)
_SEARCH_TSV_GENERATION_DIGEST: Final[str] = (
    "542630b9761cfc19062f8e4c97e4b75cdfabc0f715170d779c5d33c0ec6968d7"
)


class MigrationIntegrityError(RuntimeError):
    """Raised when versioned SQL resources cannot be safely trusted."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL resource discovered from the installed package."""

    version: int
    name: str
    sql_bytes: bytes
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Read-only migration status returned by the Task 2 runner state machine."""

    current_version: int
    known_version: int
    pending: tuple[Migration, ...]
    state: Literal["ready", "upgrade_required", "blocked"]


@dataclass(frozen=True, slots=True)
class SchemaVariantFeatures:
    """Structured features of one recognised post-G002 schema variant.

    This is the authoritative classification; the variant *name* is a derived
    display string (see ``display_name``) consumed by status output, digests,
    and legacy adoption-baseline mapping only.
    """

    layout: str
    feedback_v1: bool = False
    current: bool = False
    has_edge_integrity: bool = False
    has_reliable_sync: bool = False
    has_background_tasks: bool = False
    vector_dim: int | None = None

    def display_name(self) -> str:
        """Render the canonical variant name for status output and digests."""
        if self.feedback_v1:
            name = "post_g002_runtime_v1_feedback_v1"
        else:
            name = f"post_g002_{self.layout}"
            if self.current:
                if self.has_edge_integrity:
                    name = f"{name}_edge_integrity"
                name = f"{name}_current"
        if self.has_reliable_sync:
            name = f"{name}_reliable_sync_v5"
        if self.has_background_tasks:
            name = f"{name}_background_tasks_v6"
        if self.vector_dim is not None:
            name = f"{name}_vector_{self.vector_dim}"
        return name


@dataclass(frozen=True, slots=True)
class SchemaFingerprint:
    """Canonical, catalogue-derived classification of the current schema."""

    kind: Literal["empty", "pre_g002_3_2_7", "post_g002_current", "unknown"]
    digest: str
    variant: str = ""
    features: SchemaVariantFeatures | None = None


@dataclass(frozen=True, slots=True)
class ApplicationAclContract:
    """Data-only authorization allowance for one exact application role."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("application ACL role must be an exact non-empty str")


@dataclass(frozen=True, slots=True)
class IngressAclContract:
    """Exact relation-less LOGIN principal allowed to submit inbound batches."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("ingress ACL role must be an exact non-empty str")


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    version: int
    name: str
    checksum: str
    execution_mode: str
    baseline_fingerprint: str | None
