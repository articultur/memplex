"""Current authority policy dominates stale process authentication."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from memplex.auth import PrincipalCredential, PrincipalRegistry
from memplex.source_contract import SourceContractError
from tests.test_source_v1_pg_transactions import CONTEXT, POLICY, SCOPE, _command, _fence, _mutate
from tests.test_source_v1_pg_transactions import (
    source_backend as source_backend,  # noqa: PLC0414 - pytest fixture registration
)
from tests.test_source_v1_publication import (
    publication_backend as publication_backend,  # noqa: PLC0414 - pytest fixture registration
)


def registry():
    return PrincipalRegistry((PrincipalCredential("credential-alice", hashlib.sha256(b"test-token").hexdigest(),
                              SCOPE.tenant_id, "alice", SCOPE.workspace_id, "source-agent", frozenset({"member"})),))


def policy_authority(h):
    from memplex.storage.source_versions import _RESOURCE_ISSUER, _issue_policy_authority

    resource = h.store._durability if h.kind == "lite" else h.resources
    return _issue_policy_authority(resource, CONTEXT, issuer=_RESOURCE_ISSUER)


def test_stale_process_registry_cannot_override_policy(publication_backend):
    h = publication_backend
    stale = registry()
    context = stale.authenticate("test-token")
    assert context is not None
    _mutate(h.backend, _command())
    policy = replace(POLICY, revision=2, entries=())
    assert h.backend.replace_policy(1, policy, policy_authority(h)) == policy
    assert stale.authenticate("test-token") == context
    with pytest.raises(SourceContractError, match="denied"), _fence(h.backend, context):
        pass
    with pytest.raises(SourceContractError, match="denied"):
        _mutate(h.backend, _command(name="new"), context)


@pytest.mark.parametrize("field,value", [("authentication_id", "revoked"), ("tenant_id", "other"),
    ("subject_id", "other"), ("roles", frozenset({"admin"})), ("workspace_id", "other"), ("agent_id", "other")])
def test_admit_principal_rechecks_every_dimension(field, value):
    from memplex.source_policy import admit_principal, policy_digest

    assert admit_principal(CONTEXT, POLICY)
    changed = (replace(CONTEXT, **{field: value}) if field in {"workspace_id", "agent_id"}
               else replace(CONTEXT, principal=replace(CONTEXT.principal, **{field: value})))
    assert not admit_principal(changed, POLICY)
    assert policy_digest(POLICY) != policy_digest(replace(POLICY, revision=2))
    assert admit_principal(replace(CONTEXT, provenance={"roles": "admin"}), POLICY)


def test_policy_commit_precedes_new_read(publication_backend):
    h = publication_backend
    current = registry()
    replacement = PrincipalRegistry((replace(current.credentials[0], roles=frozenset({"reader"})),))
    from memplex.config import _replace_principal_registry

    state = type("Runtime", (), {"principal_registry": current})()
    policy = _replace_principal_registry(state, replacement, backend=h.backend,
                                        expected_revision=1, authority=policy_authority(h),
                                        algorithm_versions=POLICY.algorithm_versions)
    assert state.principal_registry is replacement
    assert policy.revision == 2
    with pytest.raises(SourceContractError, match="denied"), _fence(h.backend):
        pass
    new_context = replacement.authenticate("test-token")
    with _fence(h.backend, new_context) as session:
        assert session.authority().policy == policy
    with pytest.raises(SourceContractError):
        _replace_principal_registry(state, current, backend=h.backend, expected_revision=1,
                                    authority=policy_authority(h), algorithm_versions=POLICY.algorithm_versions)
    assert state.principal_registry is replacement


def test_policy_update_requires_sealed_resource(publication_backend):
    h = publication_backend
    with pytest.raises(SourceContractError):
        h.backend.replace_policy(1, replace(POLICY, revision=2), object())
    with _fence(h.backend) as session:
        assert session.authority().policy == POLICY


@pytest.mark.parametrize("expected", [True, 0, 2])
def test_policy_cas_rejects_invalid_or_stale_revision(publication_backend, expected):
    h = publication_backend
    with pytest.raises(SourceContractError):
        h.backend.replace_policy(expected, replace(POLICY, revision=2), policy_authority(h))
    with _fence(h.backend) as session:
        assert session.authority().policy == POLICY


def test_policy_contains_no_authentication_secret():
    from memplex.source_codec import _canonical
    from memplex.source_policy import policy_digest

    policy = registry().policy_snapshot(1, algorithm_versions=POLICY.algorithm_versions)
    encoded = _canonical(policy)
    assert "test-token" not in encoded
    assert registry().credentials[0].token_sha256 not in encoded
    assert policy == POLICY
    assert len(policy_digest(policy)) == 64


def test_policy_authority_is_bound_non_wire_and_not_a_role(publication_backend):
    import pickle

    from memplex.storage.source_versions import (
        _RESOURCE_ISSUER,
        PolicyUpdateAuthority,
        _issue_policy_authority,
    )

    h = publication_backend
    for invalid in [object(), {}, _issue_policy_authority(object(), CONTEXT, issuer=_RESOURCE_ISSUER)]:
        with pytest.raises(SourceContractError):
            h.backend.replace_policy(1, replace(POLICY, revision=2), invalid)
    with pytest.raises(SourceContractError):
        PolicyUpdateAuthority(resource=h.store, context=CONTEXT)
    with pytest.raises(TypeError):
        pickle.dumps(policy_authority(h))


def test_changed_roles_deny_old_context(publication_backend):
    h = publication_backend
    new = replace(POLICY, revision=2, entries=(replace(POLICY.entries[0], roles=frozenset({"reader"})),))
    h.backend.replace_policy(1, new, policy_authority(h))
    with pytest.raises(SourceContractError, match="denied"), _fence(h.backend):
        pass
    with _fence(h.backend, replace(CONTEXT, principal=replace(CONTEXT.principal, roles=frozenset({"reader"})))) as session:
        assert session.authority().policy == new


def test_registry_not_installed_if_policy_commit_fails(publication_backend, monkeypatch):
    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite durable decision failure injection")
    from memplex.config import _replace_principal_registry

    old = registry()
    runtime = type("Runtime", (), {"principal_registry": old})()
    replacement = PrincipalRegistry((replace(old.credentials[0], roles=frozenset({"reader"})),))
    def crash():
        raise OSError("before durable decision")
    monkeypatch.setattr(h.store._durability, "before_journal_durable_publish", crash)
    with pytest.raises(OSError):
        _replace_principal_registry(runtime, replacement, backend=h.backend, expected_revision=1,
                                    authority=policy_authority(h), algorithm_versions=POLICY.algorithm_versions)
    assert runtime.principal_registry is old
    with _fence(h.backend) as session:
        assert session.authority().policy == POLICY


def _stale_policy_process(path, ready, proceed, result):
    import time

    from memplex.storage.lite.durability import LiteDurability
    from memplex.storage.lite.source_versions import LiteSourceVersionStore
    from memplex.storage.source_versions import _RESOURCE_ISSUER, SourceValidationHooks

    stale = registry()
    context = stale.authenticate("test-token")
    backend = LiteSourceVersionStore(LiteDurability(path, path.with_name("changelog.json")))
    backend._install_hooks(SourceValidationHooks(lambda snapshot, auth: True, lambda snapshot: True),
                           scope=SCOPE, issuer=_RESOURCE_ISSUER)
    try:
        ready.send("old registry loaded")
        assert proceed.recv() == "read after commit"
        assert stale.authenticate("test-token") == context
        try:
            with backend.read_fence(context, deadline_ns=time.monotonic_ns() + 250_000_000):
                result.send("incorrectly admitted")
        except SourceContractError as exc:
            result.send(exc.code.value)
    finally:
        ready.close()
        proceed.close()
        result.close()


def test_policy_commit_precedes_read_in_stale_process(publication_backend):
    import multiprocessing

    h = publication_backend
    if h.kind != "lite":
        pytest.skip("Lite process/fresh-file fence proof; PG native policy/fence tests use service transactions")
    ctx = multiprocessing.get_context("spawn")
    ready_read, ready_write = ctx.Pipe(duplex=False)
    go_read, go_write = ctx.Pipe(duplex=False)
    result_read, result_write = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_stale_policy_process,
                        args=(h.store._path, ready_write, go_read, result_write))
    child.start()
    ready_write.close()
    go_read.close()
    result_write.close()
    try:
        assert ready_read.poll(10)
        assert ready_read.recv() == "old registry loaded"
        h.backend.replace_policy(1, replace(POLICY, revision=2, entries=()), policy_authority(h))
        go_write.send("read after commit")
        assert result_read.poll(10)
        assert result_read.recv() == "denied"
        child.join(10)
        assert child.exitcode == 0
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)
        if child.is_alive():
            child.kill()
            child.join(5)
        child.close()
        ready_read.close()
        go_write.close()
        result_read.close()
