# ADR-005: Cross-agent grants are read-only; promotion requires the owner

**Status**: Accepted (2026-08-15, after V1 security fix)

## Context
`share_with(memory_id, agent_id)` lets an owner open a user-private
memory to a named peer. The initial implementation let any
**grant holder** call `promote()` on the granted memory — a read-only
grant could widen the memory to workspace visibility, leaking the
owner's private content to everyone (High severity, found by the
independent security evaluation).

## Decision
Grants confer **read access only**. `promote()` requires the memory
owner (or local development). A grant holder attempting promotion gets
either `MemoryNotFoundError` (if the ACL hides it) or `PermissionError`
(if the owner check fires) — never a success.

## Consequences
- The grant model is now asymmetric by design: owner = full control,
  grant holder = read-only.
- `share_with` rejects comma/whitespace in agent_id (V2 fix: the
  comma-joined namespace store would split one id into many).
- Function-node grants survive via the `store.add` merge path carrying
  namespace changes (S3 fix).
