# ADR-007: Bi-temporal fact validity (supersede, never delete)

**Status**: Accepted (2026-08)

## Context
"Agent changed its mind" — when a fact is contradicted, LWW merge
silently overwrites, losing the belief history. Zep/Graphiti solve this
with bi-temporal edges.

## Decision
`Fact.valid_from` / `invalid_at` describe the business-time interval
the fact was TRUE. The write path supersedes contradicted same-slot
(subject+predicate) facts by stamping `invalid_at` — the row is
retained (never deleted), so `list_facts(as_of=...)` reconstructs what
was believed at any point in time.

## Consequences
- History is auditable; point-in-time queries are first-class.
- The `improve()` maintenance verb dedupes by superseding (not
  deleting) — retention is a design invariant.
- `valid_until` (shelf life) is a third time axis; `is_valid_at`
  handles all three (start, end, shelf) with a half-open interval.
