# ADR-003: AbstractSyncRepository ABC for dual-backend lockstep

**Status**: Accepted (2026-08)

## Context
`LiteSyncRepository` and `PostgresSyncRepository` expose the same 17
atomic sync operations, kept in lockstep by hand with no enforcement.

## Decision
Both inherit `AbstractSyncRepository` (17 `@abstractmethod`s mirroring the
`SyncRepository` Protocol). Dropping or renaming a method fails at
instantiation. A contract test pins the method set and signature equality
in CI.

## Consequences
- Backend drift is structurally impossible ( instantiation rejects it).
- Adding a sync operation = ABC + Protocol + both backends + contract
  test, all in the same commit.
- The Protocol remains for duck-typing; the ABC is the enforcement layer.
