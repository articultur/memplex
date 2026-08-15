# ADR-004: One storage engine, two lifecycles (memory vs knowledge)

**Status**: Accepted (2026-08)

## Context
"Do we need separate memory and knowledge-base systems?" The differences
(provenance, temporality, trust, churn, lifecycle) are real but the
storage shape (nodes, graph, retrieval, tenancy) is identical.

## Decision
One store, distinguished by `knowledge_tier` (None = plain personal
memory; personal/domain/team = curated knowledge). Promotion via
`service.promote()` is provenance-stamped and version-bumped; team tier
widens visibility to the workspace. The capture pipeline (memory) and
the curation pipeline (knowledge) are separate write paths into the same
node model.

## Consequences
- No duplicated infrastructure; retrieval, ACL, and sync serve both.
- S4 fix: the runtime read-filter needed a dedicated team-tier branch
  (workspace + knowledge_tier=team, no per-user pinning) for cross-user
  team recall to actually work at the adapter level.
- Typed fields (domain, knowledge_tier) are projected into namespace
  metadata at write AND filter time — the dual projection is the price
  of serving both the typed model and the metadata-filter model.
