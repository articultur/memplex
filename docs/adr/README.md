# Architecture Decision Records

Numbered by the session wave that produced them. Format: context →
decision → consequences.

| # | Decision |
|---|----------|
| [001](001-evidence-gated-readiness.md) | Fail-closed signed-evidence industrial readiness |
| [002](002-split-module-re-export-contracts.md) | Split modules via end-of-file re-exports + ordered circular imports |
| [003](003-sync-lockstep-abc.md) | AbstractSyncRepository ABC for dual-backend lockstep |
| [004](004-one-store-two-lifecycles.md) | One storage engine, two lifecycles (memory vs knowledge) |
| [005](005-cross-agent-grants-readonly.md) | Grants are read-only; promotion requires the owner |
| [006](006-shared-key-sync-encryption.md) | Shared-key sync payload encryption (not server-blind E2E) |
| [007](007-bi-temporal-supersede.md) | Bi-temporal fact validity (supersede, never delete) |
| [008](008-complexity-freeze-gate.md) | C901 freeze-gate with per-function noqa |
