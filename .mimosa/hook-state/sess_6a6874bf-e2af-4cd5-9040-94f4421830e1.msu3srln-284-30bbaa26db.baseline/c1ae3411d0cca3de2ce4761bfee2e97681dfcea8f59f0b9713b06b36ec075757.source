"""Authenticated identity primitives for the Memplex service boundary.

The objects in this module represent identity established by an adapter, not
claims supplied in a memory payload.  ``bind_node_identity`` is deliberately
the only helper that projects that trusted identity onto a memory node.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, FrozenSet, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from memplex.models import MemoryNode


class IdentityClaimError(ValueError):
    """Raised when a payload attempts to contradict authenticated identity."""


class MemoryNotFoundError(LookupError):
    """Raised when a requested memory is absent or outside the caller's scope.

    The shared error deliberately makes an inaccessible identifier
    indistinguishable from an absent one at every mutating service boundary.
    """


class PrincipalRegistryError(ValueError):
    """Raised when the server-trusted principal registry is malformed."""


def _required_identifier(value: str, field_name: str) -> str:
    """Return a canonical non-empty identifier or raise a precise error."""
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"{field_name} must be a non-empty string")
    return normalized


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated tenant subject, independent of any memory payload."""

    tenant_id: str
    subject_id: str
    roles: FrozenSet[str] = field(default_factory=frozenset)
    authentication_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _required_identifier(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "subject_id", _required_identifier(self.subject_id, "subject_id"))
        object.__setattr__(
            self,
            "roles",
            frozenset(
                role.strip()
                for role in self.roles
                if isinstance(role, str) and role.strip()
            ),
        )
        if self.authentication_id is not None:
            object.__setattr__(
                self,
                "authentication_id",
                _required_identifier(self.authentication_id, "authentication_id"),
            )


@dataclass(frozen=True, slots=True)
class AuthorizationContext:
    """Trusted request context used to scope every persisted memory."""

    principal: Principal
    workspace_id: str
    agent_id: str = ""
    session_id: str = ""
    request_id: str = ""
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.principal, Principal):
            raise TypeError("principal must be a Principal")
        object.__setattr__(self, "workspace_id", _required_identifier(self.workspace_id, "workspace_id"))
        for field_name in ("agent_id", "session_id", "request_id"):
            value = getattr(self, field_name)
            if value:
                object.__setattr__(self, field_name, _required_identifier(value, field_name))
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a mapping")
        object.__setattr__(
            self,
            "provenance",
            MappingProxyType({str(key): str(value) for key, value in self.provenance.items()}),
        )


def local_development_context() -> AuthorizationContext:
    """Return the explicit, auditable identity used only in development."""
    return AuthorizationContext(
        principal=Principal(
            tenant_id="local",
            subject_id="local-development",
            roles=frozenset({"local-development"}),
            authentication_id="local-development",
        ),
        workspace_id="local-development",
        agent_id="memplex",
        session_id="local-development",
        request_id="local-development",
        provenance={"trust_boundary": "local-development"},
    )


@dataclass(frozen=True, slots=True)
class PrincipalCredential:
    """A server-trusted mapping from one token digest to one principal.

    The registry deliberately retains only a SHA-256 token digest.  Raw
    credentials arrive at the HTTP boundary, are digested once, and never
    become config, persistence, logs, or memory metadata.
    """

    credential_id: str
    token_sha256: str
    tenant_id: str
    subject_id: str
    workspace_id: str
    agent_id: str = ""
    roles: FrozenSet[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        for field_name in (
            "credential_id",
            "token_sha256",
            "tenant_id",
            "subject_id",
            "workspace_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_identifier(getattr(self, field_name), field_name),
            )
        digest = self.token_sha256.lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise PrincipalRegistryError("token_sha256 must be a SHA-256 hex digest")
        object.__setattr__(self, "token_sha256", digest)
        if self.agent_id:
            object.__setattr__(self, "agent_id", _required_identifier(self.agent_id, "agent_id"))
        object.__setattr__(
            self,
            "roles",
            frozenset(role.strip() for role in self.roles if isinstance(role, str) and role.strip()),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PrincipalCredential":
        """Validate one opaque server-side principal-registry entry."""
        if not isinstance(raw, Mapping):
            raise PrincipalRegistryError("principal registry entries must be objects")
        roles = raw.get("roles", ())
        if not isinstance(roles, Sequence) or isinstance(roles, (str, bytes)):
            raise PrincipalRegistryError("principal roles must be a list of strings")
        try:
            return cls(
                credential_id=raw["credential_id"],
                token_sha256=raw["token_sha256"],
                tenant_id=raw["tenant_id"],
                subject_id=raw["subject_id"],
                workspace_id=raw["workspace_id"],
                agent_id=raw.get("agent_id", ""),
                roles=frozenset(roles),
            )
        except KeyError as exc:
            raise PrincipalRegistryError(f"principal registry entry missing {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            raise PrincipalRegistryError(f"invalid principal registry entry: {exc}") from exc


@dataclass(frozen=True, slots=True)
class PrincipalRegistry:
    """Trusted credential-to-principal resolver used by network adapters."""

    credentials: tuple[PrincipalCredential, ...]

    def __post_init__(self) -> None:
        if not self.credentials:
            raise PrincipalRegistryError("principal registry must contain at least one credential")
        identifiers = [item.credential_id for item in self.credentials]
        digests = [item.token_sha256 for item in self.credentials]
        if len(identifiers) != len(set(identifiers)):
            raise PrincipalRegistryError("principal registry credential_id values must be unique")
        if len(digests) != len(set(digests)):
            raise PrincipalRegistryError("principal registry token_sha256 values must be unique")

    @classmethod
    def from_environment(cls) -> Optional["PrincipalRegistry"]:
        """Load ``MEMPLEX_PRINCIPALS_JSON``, or return ``None`` when absent.

        An explicitly configured empty, malformed, or non-list registry is a
        startup error rather than a silent fallback to shared-secret mode.
        """
        raw = os.environ.get("MEMPLEX_PRINCIPALS_JSON")
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PrincipalRegistryError("MEMPLEX_PRINCIPALS_JSON must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise PrincipalRegistryError("MEMPLEX_PRINCIPALS_JSON must be a JSON list")
        return cls(tuple(PrincipalCredential.from_mapping(item) for item in parsed))

    def authenticate(
        self,
        token: str,
        *,
        request_id: str = "",
        session_id: str = "",
        provenance: Optional[Mapping[str, str]] = None,
    ) -> Optional[AuthorizationContext]:
        """Resolve *token* using constant-time SHA-256 digest comparison.

        Every registry digest is compared before selecting a match, avoiding
        early-return information about where a credential is stored.  The
        returned context contains only established server-side identity.
        """
        if not isinstance(token, str) or not token:
            return None
        presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched: Optional[PrincipalCredential] = None
        for credential in self.credentials:
            is_match = hmac.compare_digest(presented, credential.token_sha256)
            if is_match:
                matched = credential
        if matched is None:
            return None
        return AuthorizationContext(
            principal=Principal(
                tenant_id=matched.tenant_id,
                subject_id=matched.subject_id,
                roles=matched.roles,
                authentication_id=matched.credential_id,
            ),
            workspace_id=matched.workspace_id,
            agent_id=matched.agent_id,
            session_id=session_id,
            request_id=request_id,
            provenance=dict(provenance or {}),
        )


def resolve_environment_authorization(
    *,
    agent_id: Optional[str],
    session_id: str = "",
    request_id: str = "",
    provenance: Optional[Mapping[str, str]] = None,
    require_registry: bool = False,
) -> Optional[AuthorizationContext]:
    """Resolve one host identity from the process principal registry.

    ``MEMPLEX_PRINCIPALS_JSON`` is authoritative whenever present. Its
    credential must come from ``MEMPLEX_PRINCIPAL_TOKEN``; missing or invalid
    tokens never fall back to legacy shared secrets or a local-process
    principal. When a current ``agent_id`` is supplied, a credential-bound
    host may only be used by that exact adapter; an empty credential host is
    the explicit wildcard form and is projected to the current host. Passing
    ``None`` performs credential validation without claiming a host, for
    transport configuration that necessarily runs before a host runtime.

    When no registry is configured, callers may either receive ``None`` and
    apply a narrowly scoped development fallback, or set ``require_registry``
    to fail closed before constructing a service/runtime.
    """

    current_agent = (
        _required_identifier(agent_id, "agent_id") if agent_id is not None else None
    )
    try:
        registry = PrincipalRegistry.from_environment()
    except PrincipalRegistryError as exc:
        raise PermissionError(f"Invalid principal registry: {exc}") from exc
    if registry is None:
        if require_registry:
            raise PermissionError(
                "a principal registry (MEMPLEX_PRINCIPALS_JSON) is required"
            )
        return None

    token = os.environ.get("MEMPLEX_PRINCIPAL_TOKEN", "")
    if not token:
        raise PermissionError(
            "MEMPLEX_PRINCIPAL_TOKEN is required when a principal registry is configured"
        )
    supplied_provenance = {
        "identity_source": "principal-registry",
        **dict(provenance or {}),
    }
    context = registry.authenticate(
        token,
        session_id=session_id,
        request_id=request_id,
        provenance=supplied_provenance,
    )
    if context is None:
        raise PermissionError(
            "MEMPLEX_PRINCIPAL_TOKEN is not accepted by the principal registry"
        )
    if current_agent is None:
        return context
    if context.agent_id and context.agent_id != current_agent:
        raise PermissionError(
            f"principal credential is bound to agent {context.agent_id!r}, "
            f"not current agent {current_agent!r}"
        )
    return AuthorizationContext(
        principal=context.principal,
        workspace_id=context.workspace_id,
        agent_id=current_agent,
        session_id=session_id,
        request_id=request_id,
        provenance=supplied_provenance,
    )


def _claim_conflicts(value: object, expected: str) -> bool:
    """Whether a non-empty payload claim contradicts a trusted value."""
    if value is None:
        return False
    supplied = str(value).strip()
    return bool(supplied) and supplied != expected


def bind_node_identity(
    node: "MemoryNode",
    context: AuthorizationContext,
    *,
    visibility: str = "workspace",
    reject_conflicts: bool = True,
) -> "MemoryNode":
    """Atomically project *context* onto ``node`` before persistence.

    Payload identity fields are only accepted when absent or equal to the
    authenticated context.  All checks complete before any node mutation so a
    rejected forged claim leaves the in-memory node unchanged.
    """
    if not isinstance(context, AuthorizationContext):
        raise TypeError("context must be an AuthorizationContext")
    if not isinstance(visibility, str) or not (visibility := visibility.strip()):
        raise ValueError("visibility must be a non-empty string")

    principal = context.principal
    namespace = getattr(node, "namespace", {}) or {}
    if not isinstance(namespace, Mapping):
        raise TypeError("memory namespace must be a mapping")
    next_namespace = dict(namespace)

    expected_claims = {
        "tenant_id": principal.tenant_id,
        "owner_subject_id": principal.subject_id,
        "owner": principal.subject_id,
        "workspace_id": context.workspace_id,
        "memplex_tenant_id": principal.tenant_id,
        "memplex_subject_id": principal.subject_id,
        "memplex_workspace_id": context.workspace_id,
    }
    conflicts = [
        field_name
        for field_name, expected in expected_claims.items()
        if _claim_conflicts(
            next_namespace.get(field_name)
            if field_name.startswith("memplex_")
            else getattr(node, field_name, None),
            expected,
        )
    ]
    if reject_conflicts and conflicts:
        raise IdentityClaimError(
            "Invalid memory identity claims: " + ", ".join(sorted(conflicts))
        )

    next_namespace.update(
        {
            "memplex_tenant_id": principal.tenant_id,
            "memplex_subject_id": principal.subject_id,
            "memplex_workspace_id": context.workspace_id,
        }
    )
    next_provenance = dict(context.provenance)
    next_provenance.update(
        {
            "agent_id": context.agent_id,
            "authentication_id": principal.authentication_id or "",
            "request_id": context.request_id,
            "session_id": context.session_id,
        }
    )

    node.tenant_id = principal.tenant_id
    node.owner_subject_id = principal.subject_id
    node.owner = principal.subject_id
    node.workspace_id = context.workspace_id
    node.visibility = visibility
    node.origin_session = context.session_id or None
    node.namespace = next_namespace
    node.provenance = next_provenance
    return node
