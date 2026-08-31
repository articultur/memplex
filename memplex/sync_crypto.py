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

Key rotation
------------
Every new envelope carries a ``kid`` (key id) field — a keyed digest of the
raw key (SHA-256, domain-separated, truncated to 64 bits) that identifies
the key without revealing it. Decryption selects the key by ``kid``:

- envelopes **with** a ``kid`` are opened only by the matching key;
- legacy envelopes **without** a ``kid`` (written before this field
  existed) are tried against the current key first, then the previous key,
  so the wire format stays backward compatible.

``MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS`` optionally holds the retired key
(same 32-byte urlsafe-base64 format). Rotation procedure: set the new key
in ``MEMPLEX_SYNC_ENCRYPTION_KEY`` and move the old one into
``..._PREVIOUS`` — encryption always uses the current key, decryption still
opens envelopes written under the previous one. This mirrors the
``previous_keys`` semantics of the sync cursor codec (sync_protocol.py).

Decoded keys and derived AEAD instances are cached per process, keyed by
the raw environment values, so a key change (including rotation)
invalidates the cache automatically.

Associated data (design note)
-----------------------------
``associated_data`` stays ``None`` for now. The envelope is already
self-describing (marker + version + kid), and binding AD to an outer
context (path, tenant) would change the AEAD verification input for every
envelope in flight — a wire-format break for zero confidentiality gain,
since payload binding is already handled inside the encrypted JSON. If a
future envelope version needs AD, it must gate on a new ``v`` value.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_ENV_KEY = "MEMPLEX_SYNC_ENCRYPTION_KEY"
_ENV_KEY_PREVIOUS = "MEMPLEX_SYNC_ENCRYPTION_KEY_PREVIOUS"
_ENVELOPE_MARKER = "memplex_encrypted"
_ENVELOPE_VERSION = 1
_ENVELOPE_KID = "kid"


class SyncCryptoError(ValueError):
    """Raised when an encrypted sync envelope is malformed or undecryptable."""


def _decode_env_key(encoded: str, env_name: str) -> bytes:
    """Decode one 32-byte urlsafe-base64 key, fail-closed on bad material."""
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SyncCryptoError(f"{env_name} is not valid urlsafe base64") from exc
    if len(raw) != 32:
        raise SyncCryptoError(
            f"{env_name} must decode to exactly 32 bytes (AES-256), got {len(raw)}"
        )
    return raw


def _derive_kid(raw: bytes) -> str:
    """Stable key id: domain-separated SHA-256 over the raw key, 64 bits."""
    digest = hashlib.sha256(b"memplex-sync-kid-v1:" + raw).digest()
    return base64.urlsafe_b64encode(digest[:8]).decode("ascii").rstrip("=")


def _aesgcm(key: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise SyncCryptoError(
            "MEMPLEX_SYNC_ENCRYPTION_KEY is set but the 'cryptography' package "
            "is not installed; install with: pip install 'memplex[sync-crypto]'"
        ) from exc
    return AESGCM(key)


class _KeyRing:
    """Decoded key material and derived AEAD instances for one env snapshot.

    Encryption always uses the current key; decryption can fall back to the
    previous key (rotation), selected by the envelope ``kid``.
    """

    __slots__ = ("current_aead", "current_kid", "by_kid")

    def __init__(self, current: bytes, previous: Optional[bytes]) -> None:
        self.current_aead = _aesgcm(current)
        self.current_kid = _derive_kid(current)
        # Insertion order matters: legacy (kid-less) envelopes try the
        # current key first, then the previous one.
        by_kid: Dict[str, Any] = {self.current_kid: self.current_aead}
        if previous is not None:
            by_kid.setdefault(_derive_kid(previous), _aesgcm(previous))
        self.by_kid = by_kid


# Process-local cache keyed by the raw env values, so changing either
# variable (e.g. rotation) automatically misses the cache and rebuilds.
_ring_cache: Dict[Tuple[Optional[str], Optional[str]], _KeyRing] = {}


def _get_ring() -> Optional[_KeyRing]:
    """Return the key ring for the current env, or None when encryption is off.

    Fail-closed on malformed key material (current or previous). Results are
    cached per env snapshot so repeated encrypt/decrypt calls skip the
    base64 decode and AEAD setup.
    """
    current_encoded = os.environ.get(_ENV_KEY)
    if not current_encoded:
        return None
    previous_encoded = os.environ.get(_ENV_KEY_PREVIOUS)
    cache_key = (current_encoded, previous_encoded)
    ring = _ring_cache.get(cache_key)
    if ring is None:
        ring = _KeyRing(
            _decode_env_key(current_encoded, _ENV_KEY),
            _decode_env_key(previous_encoded, _ENV_KEY_PREVIOUS)
            if previous_encoded
            else None,
        )
        if len(_ring_cache) >= 8:  # bound growth across rotations/tests
            _ring_cache.clear()
        _ring_cache[cache_key] = ring
    return ring


def is_enabled() -> bool:
    """Whether payload encryption is switched on (env var present, non-empty).

    Pure presence check — never validates key material, never raises. A
    malformed key surfaces as :class:`SyncCryptoError` at first use.
    """
    return bool(os.environ.get(_ENV_KEY))


def is_configured() -> bool:
    """Backward-compatible alias for :func:`is_enabled`."""
    return is_enabled()


def encrypt_json_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Encrypt one JSON-serialisable sync payload into an opaque envelope.

    Returns ``{"memplex_encrypted": 1, "v": 1, "kid": <key id>,
    "n": <nonce b64>, "c": <ct b64>}``.
    When no key is configured the payload is returned unchanged (opt-in).
    """
    ring = _get_ring()
    if ring is None:
        return payload
    nonce = os.urandom(12)
    plaintext = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ciphertext = ring.current_aead.encrypt(nonce, plaintext, associated_data=None)
    return {
        _ENVELOPE_MARKER: _ENVELOPE_VERSION,
        "v": _ENVELOPE_VERSION,
        _ENVELOPE_KID: ring.current_kid,
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


def _decode_envelope_fields(envelope: Dict[str, Any], what: str) -> Tuple[bytes, bytes]:
    """Extract and base64-decode nonce + ciphertext, fail-closed."""
    try:
        nonce = base64.urlsafe_b64decode(envelope["n"].encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(envelope["c"].encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SyncCryptoError(f"{what} has invalid encoding") from exc
    if len(nonce) != 12:
        raise SyncCryptoError(f"{what} has invalid nonce length")
    return nonce, ciphertext


def _open_envelope(ring: _KeyRing, envelope: Dict[str, Any], nonce: bytes, ciphertext: bytes) -> bytes:
    """AEAD-open with kid-based key selection, fail-closed.

    Every failure — unknown kid, wrong key, tamper — collapses to one
    generic authentication error so the response cannot leak *why* the
    envelope was rejected.
    """
    kid = envelope.get(_ENVELOPE_KID)
    if kid is None:
        candidates = tuple(ring.by_kid.values())  # legacy: current, then previous
    elif isinstance(kid, str) and kid in ring.by_kid:
        candidates = (ring.by_kid[kid],)
    else:
        raise SyncCryptoError("encrypted sync envelope failed authentication")
    for aead in candidates:
        try:
            return aead.decrypt(nonce, ciphertext, associated_data=None)
        except Exception:  # InvalidTag covers tamper + wrong key
            continue
    raise SyncCryptoError("encrypted sync envelope failed authentication")


def decrypt_json_payload(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Decrypt an envelope back into the original JSON payload.

    Fail-closed: any tamper, wrong key, or malformed field raises
    :class:`SyncCryptoError` (callers map it to a 4xx, never a passthrough).
    """
    if not is_encrypted_envelope(envelope):
        raise SyncCryptoError("not an encrypted sync envelope")
    ring = _get_ring()
    if ring is None:
        raise SyncCryptoError("encrypted payload received but no key is configured")
    nonce, ciphertext = _decode_envelope_fields(envelope, "encrypted sync envelope")
    plaintext = _open_envelope(ring, envelope, nonce, ciphertext)
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SyncCryptoError("encrypted sync envelope decrypted to invalid JSON") from exc
    if not isinstance(payload, dict):
        raise SyncCryptoError("encrypted sync envelope payload must be an object")
    return payload


def encrypt_bytes(raw: bytes) -> bytes:
    """Encrypt raw canonical bytes (v1 ingress bodies) into envelope bytes."""
    ring = _get_ring()
    if ring is None:
        return raw
    nonce = os.urandom(12)
    ciphertext = ring.current_aead.encrypt(nonce, raw, associated_data=None)
    envelope = {
        _ENVELOPE_MARKER: _ENVELOPE_VERSION,
        "v": _ENVELOPE_VERSION,
        _ENVELOPE_KID: ring.current_kid,
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
    ring = _get_ring()
    if ring is None:
        raise SyncCryptoError("encrypted payload received but no key is configured")
    nonce, ciphertext = _decode_envelope_fields(envelope, "encrypted sync envelope")
    return _open_envelope(ring, envelope, nonce, ciphertext)
