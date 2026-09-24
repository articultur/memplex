# ADR-013: Provenance trust tiers + raw-text authoritative layer

## Status

Accepted (Stage 1 in flight). Design detail and evidence:
[provenance-schema-design-2026-09.md](../research/provenance-schema-design-2026-09.md)
(fidelity audit [fidelity-audit-2026-09.md](../research/fidelity-audit-2026-09.md);
red-team baseline [redteam-poison-baseline](../evidence/redteam-poison-baseline/)).

## Decision

1. Every persisted memory node carries a first-class `trust_tier`:
   `user_direct=4` (user-authored interaction text) > `session_derived=3`
   (extraction/summary output; **default for untagged legacy data**) >
   `external_web=2` (url/tool-sourced content) > `agent_inferred=1`
   (hubs, cross-session conclusions).
2. Authority never amplifies on consolidation: dedup/merge survivors take
   `min` of participants; derived views (summaries, hubs, wiki) take `min`
   of their source set - the same traversal the ACL derivation lineage
   already uses.
3. Retrieval surfaces the tier and applies a configurable score penalty
   to low-trust tiers (default 0.5 for tier <= 2), so external content
   cannot out-rank the user's own history on equal relevance.
4. Raw paragraph text becomes a persistent authoritative node type
   (`paragraph`): write-path `ParagraphCollection` content is persisted
   alongside typed nodes, `source_paragraphs` ids resolve to it, and
   paragraph nodes join vector+FTS retrieval (the "answer-bearing unit"
   the benchmark harness already proved at 0.916 that the product path
   lacks at 0.797). Paragraphs do not participate in prune.
5. peer-mesh carries `trust_tier` in object payloads; conflict
   resolution adds merge-takes-min; `SyncNodeType` gains `PARAGRAPH`
   reusing existing upsert/delete operations (no new sync methods).

## Consequences

- Schema must freeze before ADR-012 Phase B flips authority; the SQLite
  v2 shadow schema gains `kind='paragraph'` and payload-level tiers.
- Acceptance: red-team ASR drops materially from 0.80 (detection layer
  caught 0/25); fidelity parity re-run targets orchestrated probe
  0.797 -> >= 0.8045.
- Storage growth: raw text roughly doubles corpus size; accepted per F3
  (native fidelity over compression), mitigated by paragraph-level
  dedup.

## Stages

- Stage 1 (this change): tier field + write-path attribution +
  merge-takes-min + retrieval penalty; red-team re-run.
- Stage 2: paragraph authoritative layer + shadow/PG schema + fidelity
  parity run.
- Stage 3: peer-mesh contract extension + hub/derived min propagation.
