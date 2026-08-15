# ADR-009: MemplexService remains an orchestration facade

**Status**: Accepted (2026-08-15)

## Context
`MemplexService` is 2543 lines / 69 methods. The evaluation flagged it
as a god object.

## Decision
After two extraction waves (auth → `authorization.py`, injection →
`injection_guard.py`, plus temporal/improve/sleep_time/working_memory
leaf modules), the remaining methods are thin delegations or
orchestrations over those collaborators. Extracting the feedback or
health surfaces would require threading 5+ collaborators through new
classes for negative structural gain (the methods already delegate).

The boundary rule going forward: **new capabilities land in leaf
modules; the service only wires them.** The service's size is the size
of its *orchestration surface*, not its logic.

## Consequences
- Kept as a facade; complexity stays at the call sites, not in bodies.
- The two documented noqa functions (sync registrar 44, pool probe 30)
  remain the only true complexity debt.
