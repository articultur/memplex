# Architecture Decision Records

Numbered by the session wave that produced them. Format: context →
decision → consequences.

| # | Decision |
|---|----------|
| [001](001-evidence-gated-readiness.md) | Fail-closed signed-evidence industrial readiness |
| [002](002-split-module-re-export-contracts.md) | Split modules via end-of-file re-exports (migrations cluster shares names via `_constants.py`) |
| [003](003-sync-lockstep-abc.md) | AbstractSyncRepository ABC for dual-backend lockstep |
| [004](004-one-store-two-lifecycles.md) | One storage engine, two lifecycles (memory vs knowledge) |
| [005](005-cross-agent-grants-readonly.md) | Grants are read-only; promotion requires the owner |
| [006](006-shared-key-sync-encryption.md) | Shared-key sync payload encryption (not server-blind E2E) |
| [007](007-bi-temporal-supersede.md) | Bi-temporal fact validity (supersede, never delete) |
| [008](008-complexity-freeze-gate.md) | C901 freeze-gate with per-function noqa |
| [010](010-bounded-heuristic-graph-edges.md) | Bounded heuristic graph edges (per-function caps, linear graphs) |
| [011](011-layered-pre-durable-validation.md) | Layered pre-durable validation in the lite write path |
| [012](012-lite-sqlite-v2-authoritative-store.md) | Lite SQLite v2 authoritative store (shadow → rw authority → JSON as export; Status: Phase A landed, Phase B gated on shadow-exposure + acceptance thresholds) |
| [013](013-provenance-trust-tier-and-raw-layer.md) | Provenance trust tiers + raw-text authoritative layer (Status: Stages 1/2/2B landed — tier fields, merge-takes-min, lite raw paragraphs; PG paragraphs deferred to the 012 Phase-B window; Stage 3 peer-mesh pending) |
