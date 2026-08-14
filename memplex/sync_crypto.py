"""Shared-key payload encryption for sync transport (optional).

Threat model and honest scope
-----------------------------
The central Memplex server *applies* sync batches (LWW ingest), so — unlike
Mnemosyne's server-blind model — it must read plaintext. What this module
provides is **payload confidentiality across hops and at rest**:

- protects push/pull bodies on self-hosted LANs that run without TLS
  (plain ``http://`` remotes), where today the JSON travels in the clear;
- keeps the payload opaque to proxies, load balancers, and request logs;
- adds authenticated encryption (AES-256-GCM) with a per-message nonce and
  an envelope version tag, fail-closed on tamper or wrong key.

It is NOT end-to-end encryption against the server: the server holds the
same key (``MEMPLEX_SYNC_ENCRYPTION_KEY``) and decrypts before validation.

Key handling
------------
``MEMPLEX_SYNC_ENCRYPTION_KEY`` holds a 32-byte urlsafe-base64 key, read
from the environment only — never from config files, never logged. When
unset (the default), the module is inert and payloads pass through
unchanged, so behaviour is opt-in and zero-risk for existing deployments.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ENV_KEY = "MEMPLEX_SYNC_ENCRYPTION_KEY"
_ENVELOPE_MARKER = "memplex_encrypted"
_ENVELOPE_VERSION = 1


class SyncCryptoError(ValueError):
    """Raised when an encrypted sync envelope is malformed or undecryptable."""


def _load_key() -> Optional[bytes]:
    """Load the raw 32-byte key from the environment, or None when unset."""
    encoded = os.environ.get(_ENV_KEY)
    if not encoded:
        return None
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SyncCryptoError(f"{_ENV_KEY} is not valid urlsafe base64") from exc
    if len(raw) != 32:
        raise SyncCryptoError(
            f"{_ENV_KEY} must decode to exactly 32 bytes (AES-256), got {len(raw)}"
        )
    return raw


def is_configured() -> bool:
    """Whether payload encryption is active in this process."""
    return _load_key() is not None


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SyncCryptoError(
            "MEMPLEX_SYNC_ENCRYPTION_KEY is set but the 'cryptography' package "
            "is not installed; install with: pip install 'memplex[sync-crypto]'"
        ) from exc
    return AESGCM(key)


def encrypt_json_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Encrypt one JSON-serialisable sync payload into an opaque envelope.

    Returns ``{"memplex_encrypted": 1, "v": 1, "n": <nonce b64>, "c": <ct b64>}``.
    When no key is configured the payload is returned unchanged (opt-in).
    """
    key = _load_key()
    if key is None:
        return payload
    nonce = os.urandom(12)
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = _aesgcm(key).encrypt(nonce, plaintext, associated_data=None)
    return {
        _ENVELOPE_MARKER: _ENVELOPE_VERSION,
        "v": _ENVELOPE_VERSION,
        "n": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "c": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }


def is_encrypted_envelope(body: Any) -> bool:
    """Whether a decoded body is one of our encrypted envelopes."""
    return (
        isinstance(body, dict)
        and body.get(_ENVELOPE_MARKER) == _ENVELOPE_VERSION
        and isinstance(body.get("n"), str)
        and isinstance(body.get("c"), str)
    )


def decrypt_json_payload(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt an envelope back into the original JSON payload.

    Fail-closed: any tamper, wrong key, or malformed field raises
    :class:`SyncCryptoError` (callers map it to a 4xx, never a passthrough).
    """
    if not is_encrypted_envelope(envelope):
        raise SyncCryptoError("not an encrypted sync envelope")
    key = _load_key()
    if key is None:
        raise SyncCryptoError("encrypted payload received but no key is configured")
    try:
        nonce = base64.urlsafe_b64decode(envelope["n"].encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(envelope["c"].encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SyncCryptoError("encrypted sync envelope has invalid encoding") from exc
    if len(nonce) != 12:
        raise SyncCryptoError("encrypted sync envelope has invalid nonce length")
    try:
        plaintext = _aesgcm(key).decrypt(nonce, ciphertext, associated_data=None)
    except Exception as exc:  # InvalidTag covers tamper + wrong key
        raise SyncCryptoError("encrypted sync envelope failed authentication") from exc
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncCryptoError("encrypted sync envelope decrypted to invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SyncCryptoError("encrypted sync envelope payload must be an object")
    return payload


def encrypt_bytes(raw: bytes) -> bytes:
    """Encrypt raw canonical bytes (v1 ingress bodies) into envelope bytes."""
    key = _load_key()
    if key is None:
        return raw
    nonce = os.urandom(12)
    ciphertext = _aesgcm(key).encrypt(nonce, raw, associated_data=None)
    envelope = {
        _ENVELOPE_MARKER: _ENVELOPE_VERSION,
        "v": _ENVELOPE_VERSION,
        "n": base64.urlsafe_b64encode(nonce).decode("ascii"),
        "c": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def looks_encrypted(raw: bytes) -> bool:
    """Cheap byte-level check for an envelope in a raw request body."""
    return raw[:64].lstrip(b" \t\r\n").startswith(b'{"' + _ENVELOPE_MARKER.encode())


def decrypt_bytes(raw: bytes) -> bytes:
    """Decrypt an envelope-in-bytes back to the original raw canonical bytes."""
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncCryptoError("request body is not a JSON envelope") from exc
    if not is_encrypted_envelope(envelope):
        raise SyncCryptoError("request body is not an encrypted sync envelope")
    key = _load_key()
    if key is None:
        raise SyncCryptoError("encrypted payload received but no key is configured")
    try:
        nonce = base64.urlsafe_b64decode(envelope["n"].encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(envelope["c"].encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SyncCryptoError("encrypted sync envelope has invalid encoding") from exc
    if len(nonce) != 12:
        raise SyncCryptoError("encrypted sync envelope has invalid nonce length")
    try:
        return _aesgcm(key).decrypt(nonce, ciphertext, associated_data=None)
    except Exception as exc:
        raise SyncCryptoError("encrypted sync envelope failed authentication") from exc
