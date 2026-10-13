"""Pure re-admission against the policy in the current authority transaction."""
from __future__ import annotations

import hashlib

from memplex.auth import AuthorizationContext
from memplex.source_codec import _canonical
from memplex.source_contract import PolicySnapshot


def admit_principal(context: AuthorizationContext, policy: PolicySnapshot) -> bool:
    """Require the exact credential, identity, scope, agent and role set."""
    if type(context) is not AuthorizationContext or type(policy) is not PolicySnapshot:
        return False
    principal = context.principal
    actual = (principal.authentication_id, principal.tenant_id, principal.subject_id,
              context.workspace_id, context.agent_id, principal.roles)
    return any(actual == (entry.authentication_id, entry.tenant_id, entry.subject_id,
                          entry.workspace_id, entry.agent_id, entry.roles) for entry in policy.entries)


def policy_digest(policy: PolicySnapshot) -> str:
    """Hash only the frozen public identity mapping, never authentication secrets."""
    return hashlib.sha256(_canonical(policy).encode("utf-8")).hexdigest()
