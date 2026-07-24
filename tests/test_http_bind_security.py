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

import pytest  # noqa: E402

pytest.importorskip("fastapi")

from memplex.adapters.http_api import _check_bind_security  # noqa: E402


def _clear_bind_env(monkeypatch):
    """Strip every variable _check_bind_security reads so each test is isolated."""
    for var in ("MEMPLEX_HOST", "MEMPLEX_API_KEY", "MEMPLEX_BEARER_TOKEN"):
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
