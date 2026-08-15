# ADR-001: Fail-closed signed-evidence industrial readiness

**Status**: Accepted (2026-08)

## Context
The project claims "industrial readiness" capability. Competitors
self-attest readiness; we needed a mechanism that cannot be lied about
from inside the codebase.

## Decision
`readiness --strict` reports `ready/industrial` only when every gate
(G005 backup/DR, G006 SLO, G007, G008 four-host E2E, G009 capacity chaos)
has **externally supplied, HMAC-signed evidence** bound to the installed
version with a 15-minute max age. No evidence ⇒ `blocked`; bad signature
⇒ `fail`. The two completed gates (G003 migrations, G004 sync) are
hard-coded; everything else is re-verified per call.

## Consequences
- The repo **cannot** fake readiness — the fail-closed design is the
  feature.
- S-grade production readiness requires real HA deployments + SLO
  attainment history — this is operational, not codebase work.
- Every new feature wave must be audit-checked against the question
  "does this bypass readiness?" (verified: all S-wave features are
  opt-in/default-off and add no gate bypass).
