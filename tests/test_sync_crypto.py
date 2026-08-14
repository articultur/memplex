"""Tests for shared-key sync payload encryption (memplex/sync_crypto.py).

Scope honesty: this is NOT Mnemosyne-style server-blind E2E — the central
server holds the same key and decrypts before applying. These tests pin the
opt-in/fail-closed contract: unset key = byte-identical passthrough;
tamper/wrong-key/malformed = SyncCryptoError, never plaintext leakage.
"""

from __future__ import annotations

import base64
import os

import pytest

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex import sync_crypto  # noqa: E402

pytest.importorskip("cryptography", reason="sync-crypto extra not installed")


@pytest.fixture
def key_env(monkeypatch):
    raw = os.urandom(32)
    monkeypatch.setenv("MEMPLEX_SYNC_ENCRYPTION_KEY", base64.urlsafe_b64encode(raw).decode())
    yield raw
    monkeypatch.delenv("MEMPLEX_SYNC_ENCRYPTION_KEY", raising=False)


def test_unset_key_is_inert_passthrough():
    payload = {"functions": [{"id": "f1"}], "facts": []}
    assert sync_crypto.is_configured() is False
    assert sync_crypto.encrypt_json_payload(payload) is payload
    assert sync_crypto.encrypt_bytes(b"raw-bytes") == b"raw-bytes"


def test_round_trip_json_payload(key_env):
    payload = {"observations": [{"id": "o1", "context": "雪 snow"}], "n": 2**40}
    envelope = sync_crypto.encrypt_json_payload(payload)
    assert sync_crypto.is_encrypted_envelope(envelope)
    # Envelope must not leak plaintext
    serialized = str(envelope)
    assert "observations" not in serialized and "snow" not in serialized
    assert sync_crypto.decrypt_json_payload(envelope) == payload


def test_round_trip_bytes(key_env):
    raw = b'{"canonical":true,"n":12345678901234567890}'
    encrypted = sync_crypto.encrypt_bytes(raw)
    assert sync_crypto.looks_encrypted(encrypted)
    assert not sync_crypto.looks_encrypted(raw)
    assert sync_crypto.decrypt_bytes(encrypted) == raw


def test_unique_nonce_per_message(key_env):
    payload = {"x": 1}
    e1 = sync_crypto.encrypt_json_payload(payload)
    e2 = sync_crypto.encrypt_json_payload(payload)
    assert e1["n"] != e2["n"] and e1["c"] != e2["c"]


def test_tamper_fails_closed(key_env):
    envelope = sync_crypto.encrypt_json_payload({"secret": "value"})
    flipped = bytearray(base64.urlsafe_b64decode(envelope["c"]))
    flipped[-1] ^= 0xFF
    envelope["c"] = base64.urlsafe_b64encode(bytes(flipped)).decode()
    with pytest.raises(sync_crypto.SyncCryptoError, match="authentication"):
        sync_crypto.decrypt_json_payload(envelope)


def test_wrong_key_fails_closed(monkeypatch):
    payload = {"a": 1}
    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    envelope = sync_crypto.encrypt_json_payload(payload)
    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.decrypt_json_payload(envelope)


def test_malformed_envelope_rejected(key_env):
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.decrypt_json_payload({"memplex_encrypted": 1, "n": "x", "c": "y"})
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.decrypt_json_payload({"unrelated": True})


def test_bad_key_material_rejected(monkeypatch):
    import base64 as _b64

    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY",
        _b64.urlsafe_b64encode(b"only-16-bytes!!").decode(),
    )
    with pytest.raises(sync_crypto.SyncCryptoError, match="32 bytes"):
        sync_crypto.encrypt_json_payload({"x": 1})  # key loading raises on use


def test_encrypted_push_rejected_without_server_key(monkeypatch, tmp_path):
    """Server-side fail-closed: envelope arrives, no key configured → 400 path."""
    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(os.urandom(32)).decode(),
    )
    envelope = sync_crypto.encrypt_json_payload({"functions": []})
    monkeypatch.delenv("MEMPLEX_SYNC_ENCRYPTION_KEY", raising=False)
    assert sync_crypto.is_encrypted_envelope(envelope)
    with pytest.raises(sync_crypto.SyncCryptoError, match="no key"):
        sync_crypto.decrypt_json_payload(envelope)
