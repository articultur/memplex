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
    assert sync_crypto.is_enabled() is False
    assert sync_crypto.is_configured() is False  # backward-compatible alias
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


def _set_key(monkeypatch, raw: bytes, previous: bytes | None = None) -> None:
    monkeypatch.setenv("MEMPLEX_SYNC_ENCRYPTION_KEY", base64.urlsafe_b64encode(raw).decode())
    if previous is None:
        monkeypatch.delenv("MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS", raising=False)
    else:
        monkeypatch.setenv(
            "MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS", base64.urlsafe_b64encode(previous).decode()
        )


def test_new_envelope_carries_kid(key_env):
    payload = {"observations": [{"id": "o1"}]}
    envelope = sync_crypto.encrypt_json_payload(payload)
    assert isinstance(envelope["kid"], str) and envelope["kid"]
    # kid must not equal or contain the key material
    assert base64.urlsafe_b64encode(key_env).decode() != envelope["kid"]
    assert sync_crypto.decrypt_json_payload(envelope) == payload


def test_bytes_envelope_carries_kid(key_env):
    import json as _json

    encrypted = sync_crypto.encrypt_bytes(b'{"canonical":1}')
    envelope = _json.loads(encrypted.decode("utf-8"))
    assert isinstance(envelope["kid"], str) and envelope["kid"]
    assert sync_crypto.decrypt_bytes(encrypted) == b'{"canonical":1}'


def test_legacy_envelope_without_kid_decrypts(key_env):
    """Envelopes written before the kid field existed stay decryptable."""
    payload = {"legacy": True, "n": 7}
    envelope = sync_crypto.encrypt_json_payload(payload)
    del envelope["kid"]  # hand-construct the pre-kid wire format
    assert sync_crypto.is_encrypted_envelope(envelope)
    assert sync_crypto.decrypt_json_payload(envelope) == payload


def test_previous_key_rotation_decrypts(monkeypatch):
    """Rotation: old key moved to MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS."""
    old_raw, new_raw = os.urandom(32), os.urandom(32)
    _set_key(monkeypatch, old_raw)
    kid_pinned = sync_crypto.encrypt_json_payload({"era": "old", "k": 1})
    legacy = sync_crypto.encrypt_json_payload({"era": "old", "k": 2})
    del legacy["kid"]

    _set_key(monkeypatch, new_raw, previous=old_raw)
    # Old envelopes still open — kid-pinned and legacy kid-less alike.
    assert sync_crypto.decrypt_json_payload(kid_pinned) == {"era": "old", "k": 1}
    assert sync_crypto.decrypt_json_payload(legacy) == {"era": "old", "k": 2}
    # New encryption uses only the current key.
    fresh = sync_crypto.encrypt_json_payload({"era": "new"})
    assert fresh["kid"] != kid_pinned["kid"]
    assert sync_crypto.decrypt_json_payload(fresh) == {"era": "new"}
    # The previous key alone can no longer open new envelopes.
    _set_key(monkeypatch, old_raw)
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.decrypt_json_payload(fresh)


def test_unknown_kid_fails_closed_without_leaking_reason(key_env):
    envelope = sync_crypto.encrypt_json_payload({"x": 1})
    envelope["kid"] = "no-such-key-id"
    with pytest.raises(sync_crypto.SyncCryptoError) as excinfo:
        sync_crypto.decrypt_json_payload(envelope)
    message = str(excinfo.value)
    assert "authentication" in message
    assert "no-such-key-id" not in message  # never echo the attacker-supplied kid


def test_kid_pinned_envelope_not_opened_by_wrong_known_key(monkeypatch):
    """kid pins the key: no silent fallback to another known key."""
    raw_a, raw_b = os.urandom(32), os.urandom(32)
    _set_key(monkeypatch, raw_a)
    envelope = sync_crypto.encrypt_json_payload({"x": 1})
    _set_key(monkeypatch, raw_b, previous=raw_a)
    envelope["kid"] = sync_crypto.encrypt_json_payload({"y": 2})["kid"]  # kid of B
    with pytest.raises(sync_crypto.SyncCryptoError, match="authentication"):
        sync_crypto.decrypt_json_payload(envelope)


def test_is_enabled_never_raises_on_malformed_key(monkeypatch):
    monkeypatch.setenv("MEMPLEX_SYNC_ENCRYPTION_KEY", "a")  # invalid b64 padding
    assert sync_crypto.is_enabled() is True
    assert sync_crypto.is_configured() is True
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.encrypt_json_payload({"x": 1})


def test_malformed_previous_key_fails_closed(monkeypatch, key_env):
    envelope = sync_crypto.encrypt_json_payload({"x": 1})
    monkeypatch.setenv("MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS", "%%%bogus%%%")
    with pytest.raises(sync_crypto.SyncCryptoError, match="base64"):
        sync_crypto.decrypt_json_payload(envelope)


def test_key_cache_tracks_env_change(monkeypatch):
    """Key material is cached, but keyed on the env value: a key change must
    take effect immediately (no stale-key decrypts within the process)."""
    raw_a, raw_b = os.urandom(32), os.urandom(32)
    _set_key(monkeypatch, raw_a)
    env_a = sync_crypto.encrypt_json_payload({"k": "a"})
    assert sync_crypto.decrypt_json_payload(env_a) == {"k": "a"}

    _set_key(monkeypatch, raw_b)
    with pytest.raises(sync_crypto.SyncCryptoError):
        sync_crypto.decrypt_json_payload(env_a)  # stale cache would decrypt
    env_b = sync_crypto.encrypt_json_payload({"k": "b"})
    assert env_b["kid"] != env_a["kid"]
    assert sync_crypto.decrypt_json_payload(env_b) == {"k": "b"}

    # Switching back hits the cache again and works.
    _set_key(monkeypatch, raw_a)
    assert sync_crypto.decrypt_json_payload(env_a) == {"k": "a"}
