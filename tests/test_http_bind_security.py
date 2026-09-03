"""Test _check_bind_security: refuse non-local bind without credentials.

Previously this function had zero direct coverage (multi-angle evaluation
security #2). It reads MEMPLEX_HOST and the credential env vars and must
raise RuntimeError on an unauthenticated non-local bind.

The ``app`` argument is accepted for signature stability but unused --
the check keys off environment variables, so tests drive it via
monkeypatch without constructing a FastAPI app.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

pytest.importorskip("fastapi")

from memplex.adapters.http_api import _check_bind_security


def _clear_bind_env(monkeypatch):
    """Strip every variable _check_bind_security reads so each test is isolated."""
    for var in (
        "MEMPLEX_HOST",
        "MEMPLEX_API_KEY",
        "MEMPLEX_BEARER_TOKEN",
        "MEMPLEX_PRINCIPALS_JSON",
        "MEMPLEX_ALLOW_UNAUTHENTICATED_UDS",
    ):
        monkeypatch.delenv(var, raising=False)


# ── Local binds: never refused ───────────────────────────────────────


def test_local_ipv4_bind_allowed_without_credentials(monkeypatch):
    _clear_bind_env(monkeypatch)
    # Default when MEMPLEX_HOST unset is 127.0.0.1 (http_api.py:91).
    _check_bind_security(None)  # must not raise


def test_localhost_name_bind_allowed(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "localhost")
    _check_bind_security(None)


def test_ipv6_loopback_bind_allowed(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "::1")
    _check_bind_security(None)


# ── Non-local binds ──────────────────────────────────────────────────


def test_non_local_bind_without_credentials_raises(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "0.0.0.0")
    with pytest.raises(RuntimeError) as exc:
        _check_bind_security(None)
    msg = str(exc.value)
    assert "0.0.0.0" in msg
    assert "MEMPLEX_API_KEY" in msg or "MEMPLEX_BEARER_TOKEN" in msg


def test_non_local_bind_with_api_key_allowed(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "0.0.0.0")
    monkeypatch.setenv("MEMPLEX_API_KEY", "some-key")
    _check_bind_security(None)  # must not raise


def test_non_local_bind_with_bearer_token_allowed(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "0.0.0.0")
    monkeypatch.setenv("MEMPLEX_BEARER_TOKEN", "some-token")
    _check_bind_security(None)


def test_non_local_bind_with_public_hostname_raises(monkeypatch):
    """A non-IP public hostname is also non-local and must be refused."""
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "memplex.example.com")
    with pytest.raises(RuntimeError):
        _check_bind_security(None)


# ── create_app integration: the guard fires at construction ─────────


def test_create_app_refuses_non_local_bind_without_credentials(monkeypatch, tmp_path):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_HOST", "0.0.0.0")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path))
    from memplex.adapters.http_api import create_app

    with pytest.raises(RuntimeError):
        create_app()


# ── Request-time defense-in-depth: _is_remote_peer ───────────────────


from memplex.adapters.http_api import _is_remote_peer


def test_is_remote_peer_recognises_local_addresses():
    assert _is_remote_peer("127.0.0.1") is False
    assert _is_remote_peer("127.255.255.254") is False  # whole 127.0.0.0/8
    assert _is_remote_peer("::1") is False
    assert _is_remote_peer(None) is False  # unix-domain socket: no peer
    assert _is_remote_peer("not-an-ip") is False  # synthetic test client


def test_is_remote_peer_flags_remote_addresses():
    assert _is_remote_peer("10.0.0.5") is True
    assert _is_remote_peer("192.168.1.1") is True
    assert _is_remote_peer("0.0.0.0") is True
    assert _is_remote_peer("8.8.8.8") is True


# ── _require_auth: open access is loopback-only at request time ───────


class _FakeRequest:
    """Minimal stand-in for starlette.Request used by _require_auth.

    Header values are passed explicitly to ``_require_auth`` as kwargs
    (mirroring how FastAPI binds them) rather than via the headers dict,
    because direct calls bypass FastAPI's Header parameter binding.
    """

    def __init__(
        self,
        peer_host,
        path="/memory",
        *,
        headers=None,
        deployment_profile="development",
    ):
        from types import SimpleNamespace

        self.url = SimpleNamespace(path=path)
        self.headers = headers or {}
        self.client = SimpleNamespace(host=peer_host, port=12345) if peer_host else None
        self.state = SimpleNamespace()
        # _require_auth reads request.app.state.principal_registry via getattr.
        self.app = SimpleNamespace(
            state=SimpleNamespace(deployment_profile=deployment_profile)
        )


def test_require_auth_open_access_permitted_for_loopback(monkeypatch):
    _clear_bind_env(monkeypatch)
    from memplex.adapters.http_api import _require_auth

    ctx = _require_auth(_FakeRequest(peer_host="127.0.0.1"))
    assert ctx.principal.tenant_id == "local"


def test_require_auth_open_access_refused_for_remote_peer(monkeypatch):
    """Even with no startup guard, a remote peer without credentials is 403."""
    _clear_bind_env(monkeypatch)
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(_FakeRequest(peer_host="203.0.113.7"))
    assert exc.value.status_code == 403


def test_require_auth_remote_peer_with_api_key_allowed(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_API_KEY", "secret-key")
    from memplex.adapters.http_api import _require_auth

    ctx = _require_auth(
        _FakeRequest(peer_host="203.0.113.7"), x_api_key="secret-key"
    )
    assert ctx.principal.tenant_id == "local"


def test_require_auth_open_access_rejects_forwarded_header_from_loopback(monkeypatch):
    """A proxy header must never turn a loopback transport into trusted open access."""
    _clear_bind_env(monkeypatch)
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(
            _FakeRequest(
                peer_host="127.0.0.1",
                headers={"Forwarded": "for=203.0.113.9"},
            )
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Authentication required"


def test_require_auth_open_access_rejects_x_forwarded_for_from_loopback(monkeypatch):
    """X-Forwarded-For is equally untrusted without configured authentication."""
    _clear_bind_env(monkeypatch)
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(
            _FakeRequest(
                peer_host="::1",
                headers={"X-Forwarded-For": "203.0.113.9"},
            )
        )
    assert exc.value.status_code == 403
    assert exc.value.detail == "Authentication required"


@pytest.mark.parametrize("peer_host", [None, "in-process-test-client"])
def test_require_auth_open_access_rejects_missing_or_non_ip_peer(monkeypatch, peer_host):
    """Only concrete loopback IP transports get the development fallback."""
    _clear_bind_env(monkeypatch)
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(_FakeRequest(peer_host=peer_host))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Authentication required"


def test_require_auth_open_access_allows_explicit_development_uds(monkeypatch):
    """UDS is a deliberate development-only exception, not an implicit trust path."""
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_ALLOW_UNAUTHENTICATED_UDS", "1")
    from memplex.adapters.http_api import _require_auth

    context = _require_auth(_FakeRequest(peer_host=None))
    assert context.principal.tenant_id == "local"


def test_require_auth_open_access_rejects_uds_opt_in_in_production(monkeypatch):
    _clear_bind_env(monkeypatch)
    monkeypatch.setenv("MEMPLEX_ALLOW_UNAUTHENTICATED_UDS", "1")
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(_FakeRequest(peer_host=None, deployment_profile="production"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Authentication required"


def test_require_auth_open_access_rejects_loopback_in_production(monkeypatch):
    _clear_bind_env(monkeypatch)
    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import _require_auth

    with pytest.raises(HTTPException) as exc:
        _require_auth(_FakeRequest(peer_host="127.0.0.1", deployment_profile="production"))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Authentication required"


def _registry_environment(monkeypatch, *, roles):
    import hashlib
    import json

    monkeypatch.setenv(
        "MEMPLEX_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "credential_id": "compact-test",
                    "token_sha256": hashlib.sha256(b"compact-test-token").hexdigest(),
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                    "roles": roles,
                }
            ]
        ),
    )


def _http_config(tmp_path, *, profile="development"):
    from memplex.config import MemplexConfig

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "store")
    config.llm.query_enhancement = False
    config.deployment.profile = profile
    return config


def test_http_compact_rejects_authenticated_non_maintenance_role(tmp_path, monkeypatch):
    """Changing the maintenance-role guard must make this endpoint reachable."""
    _clear_bind_env(monkeypatch)
    _registry_environment(monkeypatch, roles=["member"])
    from fastapi.testclient import TestClient

    from memplex.adapters.http_api import create_app

    with TestClient(create_app(_http_config(tmp_path))) as client:
        response = client.post("/compact", headers={"X-API-Key": "compact-test-token"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Forbidden"}


def test_http_compact_allows_maintenance_role_in_development(tmp_path, monkeypatch):
    """Development compaction is reachable only through the explicit maintenance role."""
    _clear_bind_env(monkeypatch)
    _registry_environment(monkeypatch, roles=["maintenance"])
    from fastapi.testclient import TestClient

    from memplex.adapters.http_api import create_app

    with TestClient(create_app(_http_config(tmp_path))) as client:
        response = client.post("/compact", headers={"X-API-Key": "compact-test-token"})

    assert response.status_code == 200, response.text


def test_production_compact_endpoint_fails_closed_before_service_access(tmp_path, monkeypatch):
    """The production route must reject before a missing service can become a 500."""
    import asyncio

    from starlette.exceptions import HTTPException

    from memplex.adapters.http_api import create_app
    from memplex.auth import AuthorizationContext, Principal

    _clear_bind_env(monkeypatch)
    _registry_environment(monkeypatch, roles=["maintenance"])
    app = create_app(_http_config(tmp_path, profile="production"))
    endpoint = next(route.endpoint for route in app.routes if route.path == "/compact")
    request = _FakeRequest(peer_host="127.0.0.1")
    request.app = app
    request.state.authorization = AuthorizationContext(
        principal=Principal(
            tenant_id="tenant-a",
            subject_id="alice",
            roles=frozenset({"maintenance"}),
        ),
        workspace_id="workspace-a",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(request))
    assert exc.value.status_code == 403
    assert exc.value.detail == "Forbidden"
