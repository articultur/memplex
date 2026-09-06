# ADR-010: Bounded heuristic graph edges

**Status**: Accepted (2026-09-06)

## Context
The heuristic edge families — `ASSOCIATED_WITH` (same domain) and
`DEPENDS_ON` (name-substring reference) — had no per-node bound. On
mixed corpora whose texts share common terms, every new function linked
to a growing prefix of the corpus: 3,000 seeded documents produced
1.65M edges and an 11G resident set, with write cost following the
quadratic edge count. The graph's own size, not any validation cost,
was the capacity ceiling.

## Decision
Cap both families per function (`graph.depends_on_max_edges` /
`graph.associated_with_max_edges`, default 20 each), mirroring the
existing `semantic_similar_max_edges` precedent. Selection is
deterministic: DEPENDS_ON keeps the longest matched names first (a
longer name is a more specific reference); ASSOCIATED_WITH keeps the
lowest ids. SEMANTIC_SIMILAR edges (embedding-ranked) and BELONGS_TO
(domain membership, exactly one per function) are unaffected.

## Consequences
- Graph size grows linearly (5,000-doc mixed corpus: 103K edges, 65s
  total seeding at ~13ms/document amortized), unlocking the
  hundred-thousand-document evaluation class.
- Beyond the cap, same-domain association and generic-term references
  are no longer materialized; retrieval that relied on those long
  tails falls back to search scoring. This is the deliberate trade:
  BELONGS_TO already carries domain membership, and an unranked
  complete subgraph adds density, not signal.
- The caps are configuration, so deployments with denser graphs as a
  hard requirement can raise them with eyes open.
