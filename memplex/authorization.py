"""Request authorization and ACL visibility for the memory service.

This module owns the tenancy / workspace / user / session visibility rules
that scope every memory read and write. It was extracted from
``MemplexService`` so the service orchestrates memory I/O while this
collaborator is the single source of truth for *who may see what*.

The gate is constructed once with the deployment profile and the base
stores; per-request scoped facades (``store_for`` / ``feedback_store_for``)
are built from each authenticated ``AuthorizationContext`` because keeping
the current principal on a shared store would let concurrent requests
overwrite one another.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from memplex.auth import (
    AuthorizationContext,
    MemoryNotFoundError,
    bind_node_identity,
    local_development_context,
)
from memplex.models.source import SourceType

if TYPE_CHECKING:
    from memplex.models import ExtractedData, SearchResult
    from memplex.storage.feedback import FeedbackStore

logger = logging.getLogger(__name__)


class _TypedNodeLookup:
    """Store facade whose ``get`` also resolves Fact/Preference nodes.

    ``MemoryStore.get`` only covers Functions; Fact/Preference nodes live
    behind the optional typed interfaces (``get_fact`` / ``get_preference``).
    The injection guard (``filter_and_wrap`` / ``wrap_for_context``) takes
    a store-like object with ``get`` -- wrapping the real store in this
    facade keeps typed memories recallable into LLM context instead of
    being silently dropped as unresolvable. Every other attribute is
    delegated unchanged.
    """

    def __init__(self, store: Any) -> None:
        self._store = store

    def get(self, node_id: str) -> Any:
        node = self._store.get(node_id)
        if node is not None:
            return node
        for getter_name in ("get_fact", "get_preference", "get_observation"):
            getter = getattr(self._store, getter_name, None)
            if not callable(getter):
                continue
            try:
                node = getter(node_id)
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.debug("typed-node lookup via %s failed for %s: %s", getter_name, node_id, exc)
                node = None
            if node is not None:
                return node
        # ADR-013 Stage 2 raw layer: paragraph rows are dict-native, not
        # MemoryNode instances; resolve them from the raw store so the
        # ACL filter keeps raw-layer retrieval hits instead of silently
        # dropping them as unresolvable ids.
        paragraphs = getattr(self._store, "_paragraphs", None)
        if isinstance(paragraphs, dict):
            row = paragraphs.get(node_id)
            if row is not None:
                return _RawParagraphView(row)
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class _RawParagraphView:
    """ACL-visible facade over one raw-paragraph row (dict-native).

    Carries the identity/visibility fields ``is_node_visible`` reads.
    Only identity actually persisted in the row is projected. Historic raw
    payloads may lack tenant/workspace identity; the canonical gate limits
    those records to its explicit local-development compatibility boundary.
    """

    __slots__ = (
        "id",
        "memory_type",
        "name",
        "namespace",
        "origin_session",
        "owner_subject",
        "owner_subject_id",
        "provenance",
        "raw_text",
        "source_type",
        "tenant_id",
        "trust_tier",
        "visibility",
        "workspace_id",
    )

    def __init__(self, row: dict) -> None:
        # Project only persisted fields. In particular, a request principal or
        # mapping key cannot fill missing ACL identity or a missing row ID.
        self.id = row.get("id", "")
        self.memory_type = "paragraph"
        self.name = row.get("name", "")
        self.source_type = row.get("source_type") or SourceType.WIKI
        self.tenant_id = row.get("tenant_id")
        self.workspace_id = row.get("workspace_id")
        self.owner_subject = row.get("owner_subject")
        self.owner_subject_id = row.get("owner_subject_id") or self.owner_subject
        self.origin_session = row.get("origin_session")
        self.provenance = dict(row.get("provenance") or {})
        self.visibility = row.get("visibility") or "workspace"
        self.namespace = dict(row.get("namespace") or {})
        self.trust_tier = int(row.get("trust_tier", 3))
        self.raw_text = row.get("raw_text", "")

    def to_dict(self) -> dict[str, Any]:
        """Expose all projected fields to the existing bounded safety scan."""
        return {field: getattr(self, field) for field in self.__slots__}


class AuthorizationGate:
    """Encapsulates request authorization and ACL visibility filtering.

    Holds the deployment profile and base stores; per-request facades are
    derived from each authenticated context so the principal never leaks
    across concurrent requests.
    """

    def __init__(self, config: Any, store_provider: Any, feedback_provider: Any) -> None:
        self._config = config
        # Stores are resolved lazily via zero-arg providers so the gate always
        # reads the service's *current* store attributes (tests and request
        # scopes monkeypatch ``service.store``), never a stale snapshot.
        self._store_provider = store_provider
        self._feedback_provider = feedback_provider

    # ── Profile / context resolution ───────────────────────────────

    def is_production(self) -> bool:
        """Whether this service is running under the production contract."""
        return (
            str(getattr(self._config.deployment, "profile", "development"))
            .strip()
            .lower()
            == "production"
        )

    def require_authorization(
        self, context: AuthorizationContext | None
    ) -> AuthorizationContext:
        """Require adapter-bound identity outside the local development profile."""
        if context is not None:
            if not isinstance(context, AuthorizationContext):
                raise TypeError("authorization context must be an AuthorizationContext")
            return context
        profile = str(getattr(self._config.deployment, "profile", "development")).strip().lower()
        if profile == "production":
            raise PermissionError("authorization context is required in production")
        return local_development_context()

    # ── Request-scoped storage facades ─────────────────────────────

    def store_for(self, context: AuthorizationContext) -> Any:
        """Return an immutable request-scoped storage facade when supported.

        PostgreSQL stores enforce tenant predicates and RLS settings through
        ``authorized(context)``.  The facade is intentionally allocated per
        service call: keeping the current principal on a shared store would
        let concurrent requests overwrite one another.  Lite stores retain
        their development-compatible API because they expose no such facade.
        """
        authorize = getattr(self._store_provider(), "authorized", None)
        return authorize(context) if callable(authorize) else self._store_provider()

    def feedback_store_for(self, context: AuthorizationContext) -> FeedbackStore:
        """Return the request-scoped feedback facade for production calls.

        Historic Lite feedback files may contain records without tenant
        columns.  Development preserves their read compatibility and relies
        on the related memory's ACL check; production always uses the facade
        and its tenant-first backend predicates.
        """
        if not self.is_production():
            return self._feedback_provider()
        feedback_store = self._feedback_provider()
        authorize = getattr(feedback_store, "authorized", None)
        return authorize(context) if callable(authorize) else feedback_store

    def typed_lookup_for(self, context: AuthorizationContext) -> _TypedNodeLookup:
        """Build a typed lookup over the same request-scoped storage facade."""
        return _TypedNodeLookup(self.store_for(context))

    # ── Visibility rules ───────────────────────────────────────────

    @staticmethod
    def identity_value(node: Any, field_name: str, namespace_key: str) -> str | None:
        """Resolve a node identity field, accepting the stable namespace copy.

        Identity is persisted both on ``MemoryNode`` and in its namespace so
        existing serializer paths can retain it.  The typed field wins; the
        namespace is only a compatibility projection.
        """
        value = getattr(node, field_name, None)
        if value is None or not str(value).strip():
            namespace = getattr(node, "namespace", {}) or {}
            if isinstance(namespace, dict):
                value = namespace.get(namespace_key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @staticmethod
    def is_local_development_context(context: AuthorizationContext) -> bool:
        """Whether *context* is the explicit compatibility trust boundary."""
        principal = context.principal
        return (
            principal.tenant_id == "local"
            and principal.subject_id == "local-development"
            and "local-development" in principal.roles
            and context.workspace_id == "local-development"
            and context.provenance.get("trust_boundary") == "local-development"
        )

    @staticmethod
    def _agent_has_grant(node: Any, context: AuthorizationContext) -> bool:
        """Whether the calling agent holds an explicit cross-agent grant.

        Grants are stored in the node namespace under ``memplex_grants`` as
        a comma-separated agent-id list (written by
        :meth:`MemplexService.share_with`). Fail-closed: malformed grant
        data never grants access.
        """
        namespace = getattr(node, "namespace", {}) or {}
        if not isinstance(namespace, dict):
            return False
        raw = namespace.get("memplex_grants", "")
        if not raw:
            return False
        caller = context.agent_id or context.principal.subject_id
        return bool(caller) and caller in [
            part.strip() for part in str(raw).split(",") if part.strip()
        ]

    def is_node_visible(
        self, node: Any, context: AuthorizationContext, *,
        source_lookup: Callable[[str], Any] | None = None,
        _source_path: frozenset[str] = frozenset(),
    ) -> bool:
        """Apply the canonical own ACL and existing default lineage lookup."""
        if not self._is_node_in_scope(node, context):
            return False
        if source_lookup is None and self.identity_value(node, "tenant_id", "memplex_tenant_id") is None:
            # Keep the explicit identity-less legacy compatibility default.
            # M1 snapshot evaluation always checks its declared dependencies.
            return True
        return self._sources_still_visible(
            node, context, source_lookup=source_lookup, _source_path=_source_path,
        )

    def _is_node_in_scope(self, node: Any, context: AuthorizationContext) -> bool:
        """Single own-node ACL decision, shared by both lineage evaluators."""
        tenant_id = self.identity_value(node, "tenant_id", "memplex_tenant_id")
        if tenant_id is None:
            return self.is_local_development_context(context)
        if tenant_id != context.principal.tenant_id:
            return False

        namespace = getattr(node, "namespace", {}) or {}
        if not isinstance(namespace, dict):
            namespace = {}
        visibility = getattr(node, "visibility", None) or namespace.get("memplex_visibility")
        visibility = str(visibility or "workspace").strip().lower()
        subject_id = self.identity_value(node, "owner_subject_id", "memplex_subject_id")
        if subject_id is None:
            owner = getattr(node, "owner", None)
            subject_id = str(owner).strip() if owner is not None and str(owner).strip() else None
        workspace_id = self.identity_value(node, "workspace_id", "memplex_workspace_id")

        if visibility == "user":
            if subject_id == context.principal.subject_id:
                allowed = True
            else:
                # Cross-agent grant (service.share_with): the owner
                # explicitly shared this node with the calling agent,
                # overriding the user-private default within the tenant.
                allowed = self._agent_has_grant(node, context)
        elif visibility == "workspace":
            allowed = bool(workspace_id and context.workspace_id) and workspace_id == context.workspace_id
        elif visibility == "session":
            provenance = getattr(node, "provenance", {}) or {}
            if not isinstance(provenance, dict):
                provenance = {}
            source_agent = (
                provenance.get("agent_id")
                or namespace.get("memplex_source_agent")
                or namespace.get("memplex_agent")
            )
            source_session = getattr(node, "origin_session", None)
            allowed = (
                all((
                    workspace_id, context.workspace_id,
                    subject_id, context.principal.subject_id,
                    source_session, context.session_id,
                    source_agent, context.agent_id,
                ))
                and workspace_id == context.workspace_id
                and subject_id == context.principal.subject_id
                and source_session == context.session_id
                and source_agent == context.agent_id
            )
        else:
            allowed = False
        return allowed

    # ── Derived-record ACL lineage (ADR: derived never wider than sources) ──

    # Lower rank = more restrictive. Unknown values rank as most
    # restrictive so a novel visibility can never silently widen a
    # derived record (fail-closed).
    _VISIBILITY_RESTRICTIVENESS: ClassVar[dict[str, int]] = {
        "user": 0,
        "session": 1,
        "workspace": 2,
    }
    _SOURCE_REFS_KEY = "memplex_source_refs"
    _DERIVATION_KEY = "memplex_derivation"

    @classmethod
    def bind_derivation_lineage(
        cls,
        node: Any,
        source_nodes: list[Any],
        *,
        derivation_version: str = "v1",
    ) -> None:
        """Stamp a derived node with its source lineage and clamp visibility.

        The derived record's visibility becomes the MOST restrictive among
        its sources (never wider), and ``memplex_source_refs`` records the
        dependency so :meth:`is_node_visible` re-checks every source at
        read time -- revoking or deleting a source hides the derivation
        (fail-closed: a missing source counts as revoked).
        """
        if not source_nodes:
            raise ValueError("derivation lineage requires at least one source")
        ids: list[str] = []
        ranks: list[int] = []
        for source in source_nodes:
            source_id = getattr(source, "id", None)
            if not source_id:
                raise ValueError("derivation source is missing its id")
            ids.append(str(source_id))
            visibility = str(
                getattr(source, "visibility", None)
                or (getattr(source, "namespace", {}) or {}).get(
                    "memplex_visibility"
                )
                or ""
            ).strip().lower()
            ranks.append(
                cls._VISIBILITY_RESTRICTIVENESS.get(visibility, -1)
            )
        namespace = getattr(node, "namespace", None)
        if not isinstance(namespace, dict):
            namespace = {}
            node.namespace = namespace
        # Lite durability requires str->str namespace mappings; the
        # ref list is stored comma-joined (memory ids never contain
        # commas -- they are system-generated slugs).
        namespace[cls._SOURCE_REFS_KEY] = ",".join(ids)
        namespace[cls._DERIVATION_KEY] = derivation_version
        # Clamp: the derived visibility is the name whose rank equals the
        # most restrictive source rank; an unknown source visibility
        # (-1) fails closed to "user".
        most_restrictive = min(ranks)
        if most_restrictive < 0:
            node.visibility = "user"
        else:
            for name, rank in cls._VISIBILITY_RESTRICTIVENESS.items():
                if rank == most_restrictive:
                    node.visibility = name
                    break

    @classmethod
    def _source_ids(cls, node: Any) -> tuple[str, ...]:
        """Canonical declared lineage, with stable duplicate-edge removal."""
        namespace = getattr(node, "namespace", {}) or {}
        if not isinstance(namespace, dict):
            return ()
        source_refs = namespace.get(cls._SOURCE_REFS_KEY)
        if not source_refs:
            return ()
        return tuple(dict.fromkeys(part for part in str(source_refs).split(",") if part))

    def _sources_still_visible(
        self, node: Any, context: AuthorizationContext, *,
        source_lookup: Callable[[str], Any] | None = None,
        _source_path: frozenset[str] = frozenset(),
    ) -> bool:
        """Lineage gate: every source must still be visible to the caller.

        Only runs for nodes carrying ``memplex_source_refs``; a source
        that is deleted, revoked, or no longer visible hides the derived
        record entirely (fail-closed).
        """
        source_ids = self._source_ids(node)
        if not source_ids:
            return True
        node_id = str(getattr(node, "id", "") or "")
        if node_id in _source_path:
            return False
        source_path = _source_path | {node_id}
        lookup = source_lookup or self.typed_lookup_for(context).get
        visibility = _SnapshotAuthorization(
            self, context, lookup, lambda: "lookup_error",
            _legacy_source_path=source_path,
            _legacy_identityless=source_lookup is None,
        )
        for source_id in source_ids:
            try:
                source = lookup(str(source_id))
            except Exception as exc:  # noqa: BLE001 - lineage is fail-closed
                logger.debug(
                    "lineage source lookup failed for %s: %s", source_id, exc
                )
                return False
            if source is None:
                return False
            # A custom legacy lookup may return aliases or ID-less nodes.
            # Such graphs cannot safely share ID-keyed verdicts. Preserve
            # their original path-sensitive recursion rather than widen ACLs.
            verdict = (False, "legacy_alias")
            if getattr(source, "id", None) == source_id:
                verdict = visibility.check(source)
            allowed = verdict[0]
            if verdict[1] == "legacy_alias":
                allowed = self.is_node_visible(
                    source, context, source_lookup=source_lookup, _source_path=source_path,
                )
            if not allowed:
                return False
        return True

    def visible_node(self, memory_id: str, context: AuthorizationContext) -> Any:
        """Load one node and hide inaccessible identifiers from callers."""
        try:
            node = self.typed_lookup_for(context).get(memory_id)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("authorized node lookup failed for %s: %s", memory_id, exc)
            return None
        if node is None or not self.is_node_visible(node, context):
            return None
        return node

    def require_visible_node(self, memory_id: str, context: AuthorizationContext) -> Any:
        """Return a visible node or raise the uniform opaque mutation error."""
        node = self.visible_node(memory_id, context)
        if node is None:
            raise MemoryNotFoundError("Memory not found")
        return node

    def filter_authorized_results(
        self, results: list[SearchResult], context: AuthorizationContext
    ) -> list[SearchResult]:
        """Drop inaccessible search candidates before any ranking side effect."""
        kept: list[SearchResult] = []
        for result in results:
            node = self.visible_node(result.func_id, context)
            if node is not None:
                kept.append(result)
        return kept

    @staticmethod
    def bind_extracted_identity(
        extracted: ExtractedData,
        context: AuthorizationContext,
        *,
        visibility: str = "workspace",
    ) -> None:
        """Stamp every extraction product before any store operation begins.

        Also projects each node's typed ``domain`` into its namespace so
        the domain-scoped recall filter (agent-domain binding) can match
        without backend-specific typed-field reads.
        """
        seen: set[int] = set()
        for nodes in (
            extracted.functions,
            extracted.facts,
            extracted.preferences,
            getattr(extracted.graph, "nodes", []),
        ):
            for node in nodes:
                if id(node) in seen:
                    continue
                seen.add(id(node))
                bind_node_identity(node, context, visibility=visibility)
                # After bind_node_identity (which rewrites node.namespace),
                # project the typed domain into the namespace for the
                # domain-scoped recall filter.
                domain = getattr(node, "domain", None)
                if domain:
                    namespace = getattr(node, "namespace", None)
                    if isinstance(namespace, dict):
                        namespace["domain"] = str(domain)
                    else:
                        node.namespace = {"domain": str(domain)}


class _SnapshotAuthorization:
    """Request-local iterative lineage evaluation with completed verdicts.

    The service supplies a bounded, scoped committed lookup. Canonical own
    ACLs and source declarations remain on AuthorizationGate. Only existence
    and ACL propagate through ancestors; no transitive safety/expiry policy
    is introduced. A completed verdict is independent of an active DFS path.
    """

    def __init__(
        self,
        gate: AuthorizationGate,
        context: AuthorizationContext,
        lookup: Callable[[str], Any],
        lookup_failure: Callable[[], str],
        *,
        _legacy_source_path: frozenset[str] | None = None,
        _legacy_identityless: bool = False,
    ) -> None:
        self._gate = gate
        self._context = context
        self._lookup = lookup
        self._lookup_failure = lookup_failure
        # None keeps strict committed-snapshot semantics. Ordinary callers
        # retain their existing identity-less and inherited-path behavior.
        self._legacy_source_path = _legacy_source_path
        self._legacy_identityless = _legacy_identityless
        self._verdicts: dict[str, tuple[bool, str]] = {}

    def check(self, node: Any) -> tuple[bool, str]:
        """Evaluate each reachable source once, without Python recursion."""
        root_id = node.id
        if root_id in self._verdicts:
            return self._verdicts[root_id]
        stack: list[tuple[Any, tuple[str, ...] | None, int]] = [(node, None, 0)]
        active: set[str] = set(self._legacy_source_path or ())

        def complete(node_id: str, verdict: tuple[bool, str]) -> None:
            self._verdicts[node_id] = verdict
            active.discard(node_id)
            stack.pop()

        while stack:
            current, sources, position = stack[-1]
            current_id = current.id
            try:
                if sources is None:
                    if not self._gate._is_node_in_scope(current, self._context):
                        complete(current_id, (False, "denied"))
                        continue
                    sources = self._gate._source_ids(current)
                    if self._legacy_identityless and self._gate.identity_value(
                        current, "tenant_id", "memplex_tenant_id",
                    ) is None:
                        sources = ()
                    if sources and current_id in active:
                        complete(current_id, (False, "denied"))
                        continue
                    active.add(current_id)
                    stack[-1] = current, sources, position
                if position >= len(sources):
                    complete(current_id, (True, ""))
                    continue
                source_id = sources[position]
                verdict = self._verdicts.get(source_id)
                if verdict is not None:
                    if not verdict[0]:
                        complete(current_id, verdict)
                    else:
                        stack[-1] = current, sources, position + 1
                elif source_id in active and self._legacy_source_path is None:
                    complete(current_id, (False, "denied"))
                else:
                    source = self._lookup(source_id)
                    if source is None:
                        self._verdicts[source_id] = (
                            False, self._lookup_failure() or "missing",
                        )
                    else:
                        # Leave the parent's edge pending until this source's
                        # completed verdict can be consumed on the next visit.
                        if self._legacy_source_path is not None and getattr(source, "id", None) != source_id:
                            return False, "legacy_alias"
                        stack.append((source, None, 0))
            except Exception:  # noqa: BLE001 - an uncertain subtree fails closed and is memoized
                complete(current_id, (False, "lookup_error"))
        return self._verdicts[root_id]
