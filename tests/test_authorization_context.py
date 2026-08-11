"""Principal and authorization-context production contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from memplex.config import MemplexConfig
from memplex.models import Fact, Function, Observation, Preference
from memplex.service import MemplexService


def _authorization():
    from memplex.auth import AuthorizationContext, Principal

    principal = Principal(
        tenant_id="tenant-a",
        subject_id="alice",
        roles=frozenset({"member"}),
        authentication_id="credential-alice",
    )
    return AuthorizationContext(
        principal=principal,
        workspace_id="workspace-shared",
        agent_id="codex",
        session_id="session-a",
        request_id="request-a",
        provenance={"launcher": "managed-codex"},
    )


def test_principal_and_authorization_context_reject_empty_identity() -> None:
    from memplex.auth import AuthorizationContext, Principal

    with pytest.raises(ValueError, match="tenant_id"):
        Principal(tenant_id="", subject_id="alice")
    with pytest.raises(ValueError, match="subject_id"):
        Principal(tenant_id="tenant-a", subject_id="")

    principal = Principal(tenant_id="tenant-a", subject_id="alice")
    with pytest.raises(ValueError, match="workspace_id"):
        AuthorizationContext(principal=principal, workspace_id="")


@pytest.mark.parametrize(
    "node",
    [
        Function(id="auth-function", name="function"),
        Fact(id="auth-fact", name="fact"),
        Preference(id="auth-preference", name="preference"),
        Observation(id="auth-observation", name="observation"),
    ],
)
def test_binding_stamps_every_memory_type_before_persistence(node) -> None:
    from memplex.auth import bind_node_identity

    context = _authorization()
    bind_node_identity(node, context, visibility="workspace")

    assert node.tenant_id == "tenant-a"
    assert node.owner_subject_id == "alice"
    assert node.owner == "alice"
    assert node.workspace_id == "workspace-shared"
    assert node.visibility == "workspace"
    assert node.origin_session == "session-a"
    assert node.provenance == {
        "agent_id": "codex",
        "authentication_id": "credential-alice",
        "launcher": "managed-codex",
        "request_id": "request-a",
        "session_id": "session-a",
    }
    assert node.namespace["memplex_tenant_id"] == "tenant-a"
    assert node.namespace["memplex_subject_id"] == "alice"
    assert node.namespace["memplex_workspace_id"] == "workspace-shared"

    restored = type(node).from_dict(node.to_dict())
    assert restored.tenant_id == node.tenant_id
    assert restored.owner_subject_id == node.owner_subject_id
    assert restored.workspace_id == node.workspace_id
    assert restored.visibility == node.visibility
    assert restored.provenance == node.provenance


def test_untrusted_identity_claims_cannot_override_authenticated_context() -> None:
    from memplex.auth import IdentityClaimError, bind_node_identity

    node = Function(
        id="forged-function",
        name="forged",
        tenant_id="tenant-victim",
        owner_subject_id="victim",
        workspace_id="victim-workspace",
        owner="victim",
        namespace={"memplex_tenant_id": "tenant-victim"},
    )

    with pytest.raises(IdentityClaimError, match="Invalid memory identity claims"):
        bind_node_identity(node, _authorization(), reject_conflicts=True)

    assert node.tenant_id == "tenant-victim"
    assert node.owner_subject_id == "victim"


def test_production_service_requires_bound_authorization_context() -> None:
    service = object.__new__(MemplexService)
    config = MemplexConfig()
    config.deployment.profile = "production"
    service._config = config

    with pytest.raises(PermissionError, match="authorization context"):
        service._require_authorization(None)

    context = _authorization()
    assert service._require_authorization(context) is context


def test_development_service_uses_explicit_local_context() -> None:
    service = object.__new__(MemplexService)
    config = MemplexConfig()
    config.deployment.profile = "development"
    service._config = config
    service.store = SimpleNamespace()

    context = service._require_authorization(None)

    assert context.principal.tenant_id == "local"
    assert context.principal.subject_id
    assert context.workspace_id
    assert context.provenance["trust_boundary"] == "local-development"
