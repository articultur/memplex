# ADR-008: C901 freeze-gate with per-function noqa

**Status**: Accepted (2026-08)

## Context
After clearing 7 of 8 complexity-debt functions, the remaining debt
(two sync route registrars, pool probe, test mock cursor) needed a
mechanism that prevents new debt without blocking the existing
exceptions.

## Decision
Ruff `C901` (mccabe, max 25) via `extend-select`; exemptions are
**per-function** `# noqa: C901` on the def lines — not per-file ignores
(which would let any new >25 function inside the file pass silently).
Any new >25-complexity function anywhere fails CI.

## Consequences
- The exemption list is exactly 4 documented functions, each with a
  comment stating why.
- The gate ratchets: clearing a function's noqa in the same commit as
  its refactor is the expected workflow.
- File-level exemptions were tried first and rejected after the
  independent review flagged the enforcement gap.
