# ADR-006: Shared-key sync payload encryption (not server-blind E2E)

**Status**: Accepted (2026-08)

## Context
Self-hosted LAN deployments run plain `http://` remotes; sync payloads
travel in the clear. Mnemosyne's client-side encryption keeps the
server blind — but our central server **applies** batches (LWW ingest)
and must read plaintext.

## Decision
AES-256-GCM envelopes on `/sync/push` and `/sync/v1/batches`, key from
`MEMPLEX_SYNC_ENCRYPTION_KEY` (env-only, 32-byte, urlsafe-base64).
Fail-closed on tamper/wrong key (400, never a plaintext passthrough).
Inert when unset. **Honest scope**: this is hop/at-rest protection, not
end-to-end encryption against the server — the applying server holds
the same key.

## Consequences
- Protects payloads on no-TLS LANs and against proxy/log inspection.
- The `sync-crypto` extra adds `cryptography>=50,<52`.
- The `looks_encrypted` / `is_encrypted_envelope` parsing differential
  converges both directions to 400/422 (no bypass).
