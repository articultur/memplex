# ADR-011: Layered pre-durable validation in the lite write path

**Status**: Accepted (2026-09-06)

## Context
Every lite mutation paid the whole-state cost chain: serialize →
double full decode-validate → canonical encode → fsync, twice more
under `_validate_resident_graph`'s whole-graph BELONGS_TO audit. At 600
seeded documents a single write cost 4.5s (72x the 100-document rate);
profiling attributed 62% to the repeated full decodes.

## Decision
Validate in layers, each scoped to what could actually have changed:

1. **Mutation boundary** (always, O(delta)): every incoming node and
   edge passes the full read-side validation — including
   provenance/namespace key typing, so a `{1: "actor"}` mapping is
   rejected before JSON could coerce it into a different durable key.
2. **Incremental resident audit** (per mutation, O(delta)): a resident
   id→domain index plus a queue of BELONGS_TO edges pending
   revalidation; domain changes and deletions requeue affected
   out-edges.
3. **Periodic full audits** (every 32nd commit, every reload, every
   publish): the original whole-graph contract and the double
   full-decode audit, so a serializer regression cannot outlive an
   audit window.
4. **Batching** (`deferred_commit()`): mutators validate per mutation;
   one durable pair per outermost scope. Failed mutations under a
   batch keep the consistent prefix; the final commit revalidates
   everything fail-closed.

## Consequences
- 600-doc baseline 958s → ~7s across the batching + layering
  campaigns; per-write cost tracks the delta, not the corpus.
- The fail-closed posture is preserved end to end: loads are always
  full decodes, publishes re-derive the incremental index from decoded
  collections, and the periodic audits re-run the exact original
  contracts.
- The audit interval (32) trades worst-case regression detection
  latency for throughput; it is a named constant, not behavior.
