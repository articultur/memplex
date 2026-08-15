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
from typing import TYPE_CHECKING, Any, List, Optional

from memplex.auth import (
    AuthorizationContext,
    MemoryNotFoundError,
    bind_node_identity,
    local_development_context,
)

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
            except Exception as exc:
                logger.debug("typed-node lookup via %s failed for %s: %s", getter_name, node_id, exc)
                node = None
            if node is not None:
                return node
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


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
        self, context: Optional[AuthorizationContext]
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

    def feedback_store_for(self, context: AuthorizationContext) -> "FeedbackStore":
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
    def identity_value(node: Any, field_name: str, namespace_key: str) -> Optional[str]:
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

    def is_node_visible(self, node: Any, context: AuthorizationContext) -> bool:
        """Return whether *node* is in the authenticated caller's ACL scope.

        An identity-less historic node is visible only through the explicit
        local-development compatibility context.  Every explicit tenant
        context is fail-closed before applying workspace, user, or session
        visibility.
        """
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
                return True
            # Cross-agent grant (service.share_with): the owner explicitly
            # shared this node with the calling agent, overriding the
            # user-private default within the same tenant.
            return self._agent_has_grant(node, context)
        if visibility == "workspace":
            return workspace_id == context.workspace_id
        if visibility == "session":
            provenance = getattr(node, "provenance", {}) or {}
            if not isinstance(provenance, dict):
                provenance = {}
            source_agent = (
                provenance.get("agent_id")
                or namespace.get("memplex_source_agent")
                or namespace.get("memplex_agent")
            )
            return (
                workspace_id == context.workspace_id
                and subject_id == context.principal.subject_id
                and getattr(node, "origin_session", None) == context.session_id
                and source_agent == context.agent_id
            )
        return False

    def visible_node(self, memory_id: str, context: AuthorizationContext) -> Any:
        """Load one node and hide inaccessible identifiers from callers."""
        try:
            node = self.typed_lookup_for(context).get(memory_id)
        except Exception as exc:
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
        self, results: List["SearchResult"], context: AuthorizationContext
    ) -> List["SearchResult"]:
        """Drop inaccessible search candidates before any ranking side effect."""
        kept: List["SearchResult"] = []
        for result in results:
            node = self.visible_node(result.func_id, context)
            if node is not None:
                kept.append(result)
        return kept

    @staticmethod
    def bind_extracted_identity(
        extracted: "ExtractedData",
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
