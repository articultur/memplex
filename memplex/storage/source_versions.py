"""Internal source-version protocols and resource-owned, non-wire capabilities.

The issuer is deliberately private. Resource wiring is a later task; no public
constructor, namespace, HTTP claim, wrapper, or decoded ledger grants a seal.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, NoReturn, Protocol

from memplex.auth import AuthorizationContext
from memplex.source_contract import (
    AuthorityScope,
    AuthorityState,
    PolicySnapshot,
    PublicationReceipt,
    PublicationRecord,
    PublishCommand,
    RejectionCode,
    SourceContractError,
    SourceKey,
    SourceMutation,
    SourceReceipt,
    SourceSnapshot,
    SourceState,
    TargetHead,
    TargetToken,
)


class SourceReadSession(Protocol):
    """A detached source view valid only while its backend fence is held."""

    def authority(self) -> AuthorityState: ...
    def resolve_id(self, memory_id: str) -> SourceKey | None: ...
    def source(self, key: SourceKey) -> SourceSnapshot | None: ...
    def target(self, derivation_key: str) -> TargetHead | None: ...
    def publication(self, publication_id: str) -> PublicationRecord | None: ...
    def utc_now(self) -> datetime: ...
    def monotonic_ns(self) -> int: ...


class SourceVersionStore(Protocol):
    def initialize_authority(self, scope: AuthorityScope, policy: PolicySnapshot, *,
                             admission: SourceAdmission) -> AuthorityState: ...
    def read_fence(self, authorization: AuthorizationContext, *,
                   deadline_ns: int) -> AbstractContextManager[SourceReadSession]: ...
    def mutate_source(self, authorization: AuthorizationContext, command: SourceMutation, *,
                      deadline_ns: int) -> SourceReceipt: ...
    def publish_if_current(self, authorization: AuthorizationContext, command: PublishCommand, *,
                           deadline_ns: int) -> PublicationReceipt: ...
    def delete_target(self, authorization: AuthorizationContext, derivation_key: str,
                      expected: TargetToken, *, deadline_ns: int) -> TargetHead: ...
    def recreate_target(self, authorization: AuthorizationContext, command: PublishCommand,
                        expected_deleted: TargetToken, *, deadline_ns: int) -> PublicationReceipt: ...


@dataclass(frozen=True, slots=True)
class SourceValidationHooks:
    own_acl: Callable[[SourceSnapshot, AuthorizationContext], bool]
    safe_for_model: Callable[[SourceSnapshot], bool]

    def __post_init__(self) -> None:
        if not callable(self.own_acl) or not callable(self.safe_for_model):
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)


_RESOURCE_ISSUER = object()


class _ResourceSeal:
    __slots__ = ("_issuer", "_resource")
    _issuer: object
    _resource: object

    def __init__(self, *, issuer: object = None, resource: object = None) -> None:
        if issuer is not _RESOURCE_ISSUER or resource is None:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_issuer", issuer)
        object.__setattr__(self, "_resource", resource)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("source capability is read-only")

    def __reduce__(self) -> NoReturn:
        raise TypeError("source capability cannot be serialized")


class SourceAdmission(_ResourceSeal):
    __slots__ = ("_hooks", "_scope")
    _scope: AuthorityScope
    _hooks: SourceValidationHooks

    def __init__(self, *, issuer: object = None, resource: object = None,
                 scope: AuthorityScope | None = None,
                 hooks: SourceValidationHooks | None = None) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if type(scope) is not AuthorityScope or type(hooks) is not SourceValidationHooks:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_hooks", hooks)


class PolicyUpdateAuthority(_ResourceSeal):
    __slots__ = ()


class SourceCapability(_ResourceSeal):
    __slots__ = ("_backend", "_contract_version", "_scope", "_state")
    _backend: Literal["lite_json", "postgres"]
    _scope: AuthorityScope
    _contract_version: int
    _state: SourceState

    def __init__(self, *, issuer: object, resource: object,
                 backend: Literal["lite_json", "postgres"], scope: AuthorityScope,
                 contract_version: int, state: SourceState) -> None:
        super().__init__(issuer=issuer, resource=resource)
        if backend not in {"lite_json", "postgres"} or type(scope) is not AuthorityScope:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        if type(contract_version) is not int or contract_version != 1 or type(state) is not SourceState:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        object.__setattr__(self, "_backend", backend)
        object.__setattr__(self, "_scope", scope)
        object.__setattr__(self, "_contract_version", contract_version)
        object.__setattr__(self, "_state", state)

    @property
    def backend(self) -> Literal["lite_json", "postgres"]:
        return self._backend

    @property
    def scope(self) -> AuthorityScope:
        return self._scope

    @property
    def contract_version(self) -> int:
        return self._contract_version

    @property
    def state(self) -> SourceState:
        return self._state


def _issue_admission(resource: object, scope: AuthorityScope, hooks: SourceValidationHooks, *,
                     issuer: object) -> SourceAdmission:
    return SourceAdmission(issuer=issuer, resource=resource, scope=scope, hooks=hooks)


def _check_admission(admission: SourceAdmission, resource: object, scope: AuthorityScope) -> None:
    if (type(admission) is not SourceAdmission
            or admission._issuer is not _RESOURCE_ISSUER
            or admission._resource is not resource or admission._scope != scope
            or type(admission._hooks) is not SourceValidationHooks):
        raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
