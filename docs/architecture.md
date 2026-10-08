# Architecture

Memplex is a multi-agent long-term memory layer: recall before a turn,
capture after the turn, compact old context. This document is the module map
for maintainers — what lives where, which boundaries are load-bearing, and
the import contracts that must not be broken silently.

## Layer map

```
adapters/            Host + transport boundary (one port per agent platform)
  agent_installer.py   Install/uninstall orchestrator + per-host installers
  install_transaction.py  Path enumeration + snapshot/rollback machinery ¹
  agent_assets.py      Embedded OpenClaw extension JS + Hermes plugin ¹
  cli.py / http_api.py / mcp_server.py   Human / HTTP / MCP transports
  codex_plugin / claude_skill / openclaw_plugin / hermes_memory_provider
  agent_runtime.py     Shared recall/capture runtime used by every host
service.py           MemplexService: orchestration facade over collaborators
capture_identity.py  Capture-only canonical scope keys, typed rekeying and raw-source identity
query_pipeline.py    QueryPipeline: 6-stage read-side query execution (service delegate)
authorization.py     AuthorizationGate: tenant/workspace/user/session ACL ¹
context.py           Current-source assembly and bounded candidate-only prefetch cache (leaf)
serialization.py     Layer-neutral dataclass→JSON serializer (leaf) ¹
temporal.py          Bi-temporal fact validity (supersede/as_of) ⁴
improve.py           Proactive fact maintenance (dedupe/expire/reindex) ⁴
premise_resolution.py B1 maintenance pass: LLM inference-level stale-supersession stamps ⁴
consolidation.py     F3 offline consolidation: episodic→sustained promotion + TTL forgetting ⁴
sleep_time.py        Idle-time maintenance + inference precompute daemon ⁴
working_memory.py    TTL hot-context tier (per-tenant scoped) ⁴
sync_crypto.py       Shared-key AES-GCM sync payload encryption ⁴
llm/
  injection_guard.py  InjectionScanCounter + drop_injection_suspected ¹
storage/
  base.py             MemoryStore interface
  lite/               Development JSON-pair backend — in-memory model + journaled JSON persistence, with a SQLite FTS5 sidecar for search (store, durability, sync_repository); production must use postgres. sqlite_v2.py adds the ADR-012 Phase A shadow writer (opt-in MEMPLEX_LITE_SQLITE_SHADOW=1): mirrors every durable commit into shadow_v2.sqlite3 next to the pair, log-only failures, gated by scripts/lite_v2_diff.py 100%-equal
  postgres.py         PostgreSQL business store (request-scoped ACL facade)
  postgres_sync.py    PostgreSQL sync repository
  postgres_backup.py  Backup/restore
  pool.py             Connection pools + ReadyPostgresPool seal ²
  postgres_resources.py  Service-owned storage resources ²
  migrations/
    runner.py           Migration plan/apply + PostgresMigrationRunner ³
    _constants.py       Shared schema constants + migration data classes ³
    catalogue_snapshot.py  Whole-catalogue snapshot reader (8 domain fns) ³
    catalogue_checks.py    Pure schema-verification helpers ³
    acl_verification.py    Least-privilege ACL contract verifiers ³
    ledger_state.py        Observed-state ledger + plan-from-state ³
sync_repository.py   SyncRepository Protocol + AbstractSyncRepository ABC
sync.py / sync_protocol.py / sync_dispatcher.py / sync_ingress.py
product.py           Evidence-gated readiness (G002–G009), fail-closed
host_lifecycle.py    G008 host-contract digests (see below)
```

`¹ ² ³ ⁴` mark the split groups (⁴ = post-S-wave leaf modules) described under [Split modules](#split-modules-and-their-re-export-contracts).

### Captured conversation identity

New conversation captures extract user and assistant paragraphs separately in
one write. Host identity and provenance remain trusted fields; they are not
prepended as text for the rule-based fact extractor. The structured Observation
is retained independently, and model-facing reads remain low-trust and subject
to current-source authorization.

`capture_identity.py` gives new typed captures versioned scoped IDs. User scope
uses tenant/owner, workspace scope additionally uses workspace, and session
scope also uses host/session. Function physical IDs and normalized-name keys
add the complete writer identity, because PostgreSQL Function merges retain
the original writer columns. Their opaque keys preserve display names. Raw
paragraph source hints include the complete writer identity, including session,
to avoid reusing another writer's immutable PostgreSQL raw-row ACL columns.

When either Fact is a new capture, supersession requires complete matching
canonical scope, including corrections through ordinary writes. Compaction
partitions new captured Functions by scope before exact or semantic matching;
unmarked Functions keep their existing behavior in a separate partition.
Incomplete captured scope is isolated, and editable namespace metadata cannot
disable these guards. Existing data is not migrated or re-extracted.

PostgreSQL cold retrieval combines Functions and Facts in its lexical search
leg, with explicit per-table ACL predicates and forced RLS. Fact text uses
subject, predicate and object; final assembly still re-reads committed sources
and enforces current visibility, validity and injection filtering. The vector
leg remains Function-only. No raw-paragraph, Observation or Preference fallback
is added. Fact lexical vectors are computed at query time without a schema
change: candidate/result limits do not bound the underlying database scan cost,
and large-corpus latency has not been established by the correctness tests.

### Top-level quick reference

One line per top-level module (`ls memplex/*.py memplex/*/`); the layer map
above stays canonical for the split-group annotations:

```
__init__.py           Package root: CoreEngine/MemplexService re-exports + CLI main shim
__main__.py           `python -m memplex` entry point
adapters/             Host + transport boundary (detailed map above)
auth.py               Authenticated identity primitives for the service boundary
authorization.py      AuthorizationGate: tenant/workspace/user/session ACL ¹
backup.py             Strict backup manifests + disaster-recovery data contracts
capacity_chaos.py     G009 capacity/soak/chaos signed machine evidence
capture_identity.py  Capture identity shared by foreground writes and compaction partitioning
compaction.py         CompactionPipeline: 5-stage memory compression
config.py             Configuration load/validate (MEMPLEX_* env > config.yaml > defaults)
context.py            Bounded source-ID context assembly; no adapters, service or storage imports
core/                 Pure computation layer (CoreEngine, extractors, hooks)
factual_capture.py    Opt-in evidence namespace/materialization, committed raw + typed lineage
factual_lineage.py    Factual-only semantic source snapshots and current-source safety
host_lifecycle.py     G008 host-contract digests (detailed below)
improve.py            Proactive fact maintenance (dedupe/expire/reindex) ⁴
intent.py             Memory-type + query-scope intent heuristics (pure, dependency-free)
llm/                  LLM provider layer (providers, fallback chain, enhancer, injection_guard ¹)
logging_config.py     Structured logging configuration
models/               Typed data models (memory, graph, search, task, feedback)
operations.py         Operations status + signed SLO evidence
operations_assets/    Packaged G006 operator assets (admin console, alert rules)
_plugin/              Packaged host-plugin assets (hooks, scripts, skills)
privacy.py            Privacy helpers shared across write paths
processing/           Association/merging/graph-building pipeline
product.py            Evidence-gated readiness (G002–G009), fail-closed
query_explainer.py    Product-facing retrieval trace translator
query_pipeline.py     QueryPipeline: 6-stage read-side query path delegated from service.query
readiness_evidence.py G003/G004 fail-closed signed deployment evidence
release.py            Fail-closed release metadata + artifact contracts
retrieval/            Search and ranking (embedding, multi-path, reranker, dedup)
serialization.py      Layer-neutral dataclass→JSON serializer (leaf) ¹
service.py            MemplexService orchestration facade
premise_resolution.py B1 maintenance pass: LLM inference-level stale-supersession stamps ⁴
consolidation.py      F3 offline consolidation: episodic→sustained promotion + TTL forgetting ⁴
sleep_time.py         Idle-time maintenance + inference precompute daemon ⁴
storage/              MemoryStore interface + lite/postgres backends + migrations (map above)
sync.py               Local-cache + remote push/pull multi-node sharing
sync_crypto.py        Shared-key AES-GCM sync payload encryption ⁴
sync_dispatcher.py    Bounded dispatcher for durable sync deliveries
sync_ingress.py       Trusted ingress gateway freezing protocol bytes pre-DB
sync_protocol.py      G004 v1 pure data protocol + canonical codec
sync_repository.py    SyncRepository Protocol + AbstractSyncRepository ABC
task_repository.py    Durable background-task repository contract
temporal.py           Bi-temporal fact validity (supersede/as_of) ⁴
wiki/                 Wiki layer: compile, generate, search, lint
worker.py             BackgroundWorker async task processor
working_memory.py     TTL hot-context tier (per-tenant scoped) ⁴
```

## Mechanism map

[Capability mechanisms](capability-mechanisms.md) is the canonical map from
user-visible capabilities to implementation boundaries, focused tests, and
honest limitations. [`capabilities.json`](capabilities.json) provides the same
stable capability IDs and line-ranged evidence for machines.

| Area | Canonical mechanism |
|---|---|
| Model and write/read loop | [Typed model](capability-mechanisms.md#typed-memory-model), [capture](capability-mechanisms.md#capture-write-path), [recall](capability-mechanisms.md#recall-retrieval-path) |
| Time, graph, and access | [Bi-temporal facts](capability-mechanisms.md#temporal-facts), [bounded one-hop expansion](capability-mechanisms.md#bounded-graph-expansion), [principal/tenant authorization](capability-mechanisms.md#principal-tenant-authorization) |
| Durability and operations | [Durable versus legacy sync](capability-mechanisms.md#sync-convergence), [backup/restore](capability-mechanisms.md#backup-restore), [operations](capability-mechanisms.md#operations-observability) |
| Delivery surfaces | [Reproducible supply chain](capability-mechanisms.md#reproducible-supply-chain), [four-host lifecycle](capability-mechanisms.md#four-host-lifecycle) |

## Split modules and their re-export contracts

Several large files were split into cohesive sub-modules. **Every split
keeps the original import path working** through an end-of-file re-export in
the parent module. External code and tests must keep importing from the
parent (`from memplex.storage.migrations.runner import _matches_post_core`
still works).

### 1. Service collaborators (`authorization.py`, `llm/injection_guard.py`, `query_pipeline.py`)

`MemplexService` delegates authorization and injection-scan state to
single-purpose collaborators and keeps thin one-line wrappers for API
stability. The gate resolves stores lazily via providers so tests that
monkeypatch `service.store` are honoured.

The opt-in factual write path delegates bounded validation/provider orchestration
to `llm/factual_capture.py`, committed evidence materialization to
`factual_capture.py`, and read-time semantic source checks to
`factual_lineage.py`. Generated content cannot enter raw storage or supersede
source assertions. The existing authorization evaluator applies the additional
snapshot check only to `factual_capture_v1` derivations; both direct and context
reads use the same check. Optional new Lite raw identity metadata and existing
PostgreSQL ACL columns provide source proof without adopting legacy raw rows.
See [the factual capture contract](capability-mechanisms.md#opt-in-evidence-linked-factual-capture)
for outcomes, limits, dates, fallback and deadline semantics.

`service.query()` itself delegates the six-stage read path to
`query_pipeline.QueryPipeline`: the service resolves the request-scoped
authorization context and store, then builds a fresh pipeline per call from
its **current** attributes — tests that monkeypatch `service._detect_scope`
or `service._retriever` keep working unchanged.

`service.assemble_context()` resolves candidate IDs through the scoped committed
reader and the canonical authorization gate, including current source lineage.
The request-local iterative evaluator memoizes completed ACL verdicts and failure
reasons, sharing one canonical own-node policy with ordinary gate callers.
`context.py` receives that snapshot plus a pure runtime predicate, projects only
current text, reuses the injection guard against the same snapshot, and budgets
the complete wrapped string. Candidate and additional lineage reads are each
bounded to 500 unique IDs. PostgreSQL raw context reads project the actual SQL
identity columns and payload in one scoped query; ordinary raw CRUD keeps its
payload-only shape. Failed resolution never restores a cached summary;
internal diagnostics contain reason counts only.

Working memory remains disabled by default. Scoped hot references hold IDs,
TTL, pin and insertion order only; the legacy standalone string container does
not supply runtime context. Pin suspends only hot-candidate TTL expiration;
unpin restarts TTL from the current time. Pin cannot bypass authorization,
source deletion/revocation, Fact validity, safety filtering, capacity or the
final budget. Legacy entries and references share `max_entries`; if every
entry is pinned at capacity, a new reference is rejected without undoing the
successful durable write.

The runtime collects scoped hot IDs (bounded by `inject_limit`) followed by
ordinary retrieval IDs, preserving retrieval provenance for duplicate IDs.
The combined candidate sequence is capped at 500 and enters this final boundary
once for live recall. Legacy namespace migration runs through the controlled
read path before assembly; the namespace/domain callback itself never writes.
Legacy working-memory strings are not model context. Private ranked candidate
collection is separate from public `search_memories`: only retrieval order and
scores survive into public results. Public search always uses final assembly,
regardless of `explain`; the leaf returns frozen `ContextFragment` projections
alongside the aggregate `ContextAssembly`. Each fragment's current name, domain,
source type and complete protective wrapper come from the same accepted source
snapshot and budget decision. Rejected sources never enter fragments or public
trace IDs. No extra source lookup, old-summary fallback or wrapper parsing is
used to rebuild public results.

MCP `memory_search` retains its existing response fields and adds
`token_budget_scope: "wrapped_memory_fragments_only"`. The `summary` field now
contains the complete current protective wrapper rather than an unwrapped
retrieval/compiled-page summary; names/domains are current source fields (a
missing current name can remain empty). Result `est_tokens` estimates that
complete fragment. Top-level `tokens_used` estimates all accepted fragments
joined with two newlines, including wrappers and separators. `total` counts
accepted unique IDs. The requested memory budget does **not** cap the surrounding
serialized JSON transport envelope, query/trace diagnostics, or repeated result
metadata. Their additional size must be budgeted by the host; actual serialized
text overhead is measured separately in the MCP regression evidence. No count is
claimed to be a model tokenizer count.

The advertised generic CLI `query` recall command uses the same assembly and
`project_context_results` / public-trace projection as runtime search. CLI
identity remains the trusted adapter-established context; no host identity or
namespace is invented. CLI `top_k` is capped at the existing 500-candidate
ceiling before calling the raw producer. Retrieval order and scores are
unchanged. JSON and human
output retain whole accepted fragments and the existing fields (CLI `scope`
remains the result domain), plus `token_budget_scope`. CLI current recall uses
the existing upper model ceiling of 32000; `--max-tokens 0` returns empty
context, matching the shared assembler, not unlimited model context. Ranked
candidate collection does not spend this budget on old summaries; it is applied
once to current complete fragments. Runtime candidate collection follows the
same single-budget rule; its existing candidate/result caps remain. More
candidates can reach bounded final resolution when stale summaries would have
spent the budget early; earlier measurements do not quantify this final cost.
Raw `service.query(max_tokens=0)` retains its
unlimited candidate-producer meaning.

MCP structured reads (`memory_get`, `memory_facts`, `memory_observations`,
`memory_pending_reviews`, and request-bound scope preview) resolve candidate IDs
through current committed canonical tenant/own/source-lineage ACL and safety,
then apply the existing pure host restriction. The actual accepted objects,
never earlier resident/list payloads, supply serialized memory fields.
Search-to-detail `memory_get` is current recall and suppresses expired Facts;
not-found responses do not echo rejected IDs. Explicit `as_of` /
`include_invalidated` Fact history and pending-review inspection keep their
intentional temporal semantics; they never bypass commitment or authorization.
Pending batches are neutral. Storage failures never fall back to resident
bodies; Observation discovery preserves a neutral backend-error response.
Collection discovery and result limits are bounded at 1000 candidates;
committed resolution retains the 500 candidate / 500 extra source bound per
batch. Preview projects only accepted current nodes before counts and samples.
Unbound operator `scope_preview` preserves its existing inspection behavior.
The CLI scope-preview example in getting-started is operator inspection; no
CLI scope-preview invocation is generated by the agent assets or packaged
skills. The agent-integration scope-preview promise refers to the MCP route.

Generic Python service query, HTTP `GET /memories`, and `corpus_recall` remain
ranked candidate/inspection producers, not finalized model context. The HTTP
route exposes raw `QueryResult` and the corpus CLI exposes canonical-corpus
source-path diagnostics. Their documented callers do not establish another
advertised model-consumption route. A model consumer must use current assembly
or the advertised CLI/runtime/MCP recall boundary; raw results must not be
claimed to have its final temporal, wrapper or public-trace guarantees.

`RecalledContext.total` counts actually injected unique IDs. `tokens_used` and
`est_tokens` are the same character estimate of the complete wrapped string:
`len(context) // 4 + 1` for nonempty context, otherwise zero. This is not a model
tokenizer. No filtered placeholder can bypass the final budget.

Runtime stamping restores only previously proven hot references whose controlled
annotation succeeded and whose scoped committed identity can be read again.
An active deferred batch publishes nothing speculative; after commit ordinary
retrieval works, but no hot reference is published by a batch-finalization
callback. A later successful write can publish references normally.

Explicit `AuthorizationContext` objects are retained without projecting a
missing or transport-generic agent onto the selected host. Such callers must
supply correctly host-bound authorization for session-restricted access.
The trusted environment registry still resolves wildcard credentials onto the
selected host. Explicit local-development remains development-only; a
`local-process-*` tenant never acquires that compatibility identity.
Each `MemplexService` owns a `ContextCandidateCache`: at most 64 FIFO entries,
each a tuple of at most 500 `ContextCandidate` ID/origin pairs. It retains no
rendered `RecalledContext`, `ContextAssembly.fragments`, body or ACL copy.
Replacing a key preserves its original FIFO age; `pop` consumes it. The frozen
`ContextCacheKey` preserves the actual namespace, tenant, subject, workspace,
agent, session and normalized query, including absent/empty optional identities.
Input traversal is bounded before locking; the lock protects only local cache
operations and is never held during source I/O or assembly. Historic rendered
or otherwise incompatible entries are misses. Older duck-typed service objects
receive the same service-local cache from runtime initialization, never a global
fallback; real services always construct it themselves.

Automatic recall checks `auto_recall` before popping. A hit retains
`source="prefetch"` but revalidates current scoped working-memory references
(TTL, pin, capacity and current inject limit), filters stale hot provenance, then
uses the same current-source assembly as live recall. A valid retrieval origin
for the same ID remains eligible. Explicit prefetch immediately renders current
output while caching only candidates. Successful local mutations invalidate
matching candidate entries after backend mutation calls return; a failed sibling
cannot undo successful-prefix invalidation. Direct backend failures do not clear
unchanged entries. A successful staged call in a deferred batch may conservatively
evict candidates even if outer finalization later fails: this costs an extra
retrieval, never proves a commit, and never permits staged text in context.
No transaction-finalization callback is added. There is no cross-service
broadcast: current source resolution is the correctness boundary.
The legacy `zero_latency_prefetch` capability key/value/default remains for host
compatibility; it means candidate-prefetch enablement and makes no zero-latency
or no-validation guarantee. Full M1 backend/host acceptance remains separate.

#### Lite current-source read cost and backend evidence

Lite's Observation lookup is a derived, first-ID-wins resident map rebuilt at
both existing publication points: detached pair load/recovery and successful
local commit. Source resolution preserves Function → Fact → Preference →
Observation → raw-paragraph precedence. Every final read still holds the writer
lock, refuses an active deferred batch and refreshes the authoritative pair
before consulting the map. Local/peer JSON and rw commits, inbound sync,
clear/restore and recovery after failed finalization all use those publication
boundaries. The map is not persisted and is never independent commit proof.
The ordinary Observation getter retains its historical resident scan while a
batch is pending; final context remains empty during that batch.

Stable committed reads and lazy lineage/getter lookups no longer scan the whole
Observation table per candidate. This costs O(number of Observations) extra
resident references and one O(number of Observations) map rebuild per published
state, including successful rw delta writes. Ordinary retrieval also records access
counts through an existing durable write, so it pays this publication cost;
prefetch preparation pays it too, while a consume hit only revalidates.
Peer refresh still performs the
existing full authoritative reload; lexical retrieval and other existing
whole-corpus work are not made O(candidate count) by this change. Candidate and
lineage limits remain 500 each. Counted-work tests measure actual visited rows
rather than disguising full-table scans as bounded requested-ID counts.

Ordinary authorization before hot publication, query ranking, namespace
filtering and model assembly now reuses the same iterative lineage evaluator.
For canonical ID-equal source graphs, each evaluation memoizes completed ACL
verdicts, so shared ancestry takes graph work rather than expanding every path,
and valid in-bound chains do not depend on Python recursion depth. Separate
pipeline gates still perform separate evaluations; this is not a single-pass
whole-query claim. Memos are evaluation-local and never survive a later call.
The own-node ACL, source declarations, default scoped typed lookup, identity-less
local-development early success and explicit-lookup behavior remain unchanged;
no ancestor safety or expiry policy is added. Legacy custom/noncanonical lookup
results with aliases or missing IDs retain the prior path-sensitive recursive
fallback in either default or explicit lookup mode, including its depth/work
limits. Strict final committed readers continue to reject mismatched IDs with
no such fallback and retain the separate 500-candidate/+500-lineage limits.

The local synthetic cost evidence uses identical fixed inputs with model,
embedding and network execution disabled, reports monotonic p50/p95/p99,
read/resolve/scan operations and RSS, and separates prefetch preparation from
hit latency. It also measures publication-map rebuilding and full writes
separately. These warm, instrumented local samples are not a production latency
SLA or a model-quality/SOTA comparison.

Event-ordered two-service regression cases start live/prefetch recall only after
peer replacement or deletion returns, with an authorized owner positive control.
JSON and rw share this contract; real PostgreSQL cases use the existing isolated
function/migration fixtures and separate application/migration identities. Merely
collecting those cases cannot satisfy the mandatory real PostgreSQL+pgvector
gate. Four-host cases exercise Codex, Claude Code, OpenClaw and Hermes shared
runtime output, not an actual installation certificate. The seven-file G008
contract set is unchanged; runtime byte changes still invalidate old proofs.

Adapters report the SSE subscriber count through the public
`memplex.service.register_sse_subscriber_count_provider(fn)` registration
point (never by writing the private module global); the health surface
fails closed to `0` when no provider is registered or the provider raises.

### 2. Storage resources (`storage/postgres_resources.py`)

`PostgresStorageResources` / `PostgresSyncStorageResources` moved out of
`pool.py`. The test suite patches `pool.PostgresPoolManager` and
`pool._new_migration_runner`; the moved code therefore resolves those names
through the **live pool module** (`import ... pool as _pool`, then
`_pool.X` at call time). Never convert those to direct `from pool import X`
bindings — that would silently break every test patch.

### 3. Migration clusters (`storage/migrations/*`)

`runner.py` (was 3815 lines) now delegates to four sub-modules plus one
shared constants module:

- `_constants.py` — every schema constant, the application ACL matrix, and
  the migration data classes (`Migration`, `MigrationPlan`,
  `SchemaFingerprint`, `SchemaVariantFeatures`, ACL contracts,
  `_LedgerEntry`). Stdlib-only, no internal imports.
- `catalogue_snapshot.py` — `_catalog_snapshot` decomposed into
  `_read_schema_and_relations` / `_snapshot_table(s)` /
  `_read_capabilities` / `_read_extensions` / `_read_changelog_sequence` /
  `_read_sync_functions` + a thin orchestrator.
- `catalogue_checks.py` — 48 pure verification helpers.
- `acl_verification.py` — the three ACL contract verifiers.
- `ledger_state.py` — ledger read/validate/plan functions.

All shared names live in `_constants.py` and every cluster module imports
them from there, so **no import order is load-bearing** any more.
`runner.py` still re-exports the split-out functions (and binds the shared
names via its own top-level `_constants` import) so existing
`from ...runner import X` paths and `runner.X` monkeypatches keep
resolving; the re-export blocks must stay below the code that references
the re-exported bare names. `SchemaFingerprint.features` carries the
structured variant classification (`SchemaVariantFeatures`); the variant
string is a derived display name consumed by status output, digests, and
legacy adoption-baseline mapping only. The business-pool readiness probe
(`storage/pool.py`) derives its privilege matrix from
`_constants._APPLICATION_ACL` — the single source of truth for the
application role's least-privilege grants.

### 4. Shared G008 adapter runtime

The shared-runtime digest is the exact seven-file set
`adapters/{agent_installer,install_transaction,agent_assets,agent_runtime,managed_identity,runtime_status,_shared}.py`.
Every file is included in every host's G008 contract digest via
`host_lifecycle._contract_files()`; any byte drift in any one invalidates all
four host proofs. Install-path/rollback machinery and embedded plugin assets
live in `install_transaction.py` and `agent_assets.py`, while installer-owned
helpers are imported lazily to keep loading one-directional. Adding, removing,
or renaming a file in this seven-file cluster requires updating **both**
`host_lifecycle._contract_files()` and the mutation manifest in
`tests/test_host_lifecycle_evidence.py`.

## Machine-enforced gates (CI)

- **Hexagonal contract** (`import-linter`, `lint-imports` in CI): the domain
  and storage layers listed in `pyproject.toml [tool.importlinter]` may never
  import `memplex.adapters`. `memplex.serialization.py` exists so shared
  serializers do not force domain→adapter imports.
- **Complexity freeze** (ruff `C901`, max 25): any function whose complexity
  exceeds 25 fails CI. The one remaining known-debt hot spot carries an
  inline `# noqa: C901  documented known debt` marker (the ACL access probe
  in `storage/pool.py`); adding a new >25-complexity function — or a new
  noqa marker without the "documented known debt" justification — fails
  review.
- **Typed boundary** (mypy): the file list in `[tool.mypy] files` is itself
  pinned by `tests/test_release_workflows.py` — extend both together.

## Known oversized files and split roadmap

Size debt is tracked explicitly here so it stays visible between review
waves. Line counts are `wc -l` at the time of writing; re-measure before
quoting them in a review.

| File | Lines | Status / next slice |
|---|---|---|
| `adapters/cli.py` | ~2375 | Largest remaining adapter. Candidate: per-command-group registrars (same pattern as the http_api wave-3/4 registrar splits). |
| `adapters/http_api.py` | ~2370 | Route registrars are now one helper per endpoint (`_register_memory_*_route`, `_register_sync_v1_*_route`, legacy sync pair); remaining bulk is endpoint bodies. Next slice: extract the legacy sync push/changes payloads (`_legacy_sync_v1_push` ≈ complexity 20). |
| `service.py` | ~2275 | Query path extracted to `query_pipeline.py` (6-stage `QueryPipeline`, ~440 lines); `query()` is now a thin delegate. Next slices by independence: sync lifecycle block (`sync_status`/`drain_sync`/`pull_sync`), then the health/status block (`health`/`runtime_status`/`operations_metrics_status`/`readiness_status`/`_sync_health`). |
| `storage/lite/store.py` | ~2204 | Lite backend monolith. Candidate: split the FTS5 search-index sidecar and the COW/journal durability machinery out of the store class. |
| `storage/postgres.py` | ~2158 | Postgres business store. Candidate: split the request-scoped ACL facade from the pool-backed CRUD core. |
| `storage/pool.py` | ~1896 | Connection pools + readiness seal; holds the last `noqa: C901` (`_probe_application_access`). Candidate: move the readiness probe/ACL matrix verification into its own module reading `_constants._APPLICATION_ACL`. |

Split priority: `cli.py` and `service.py` first (both sit on the
user-facing orchestration path and accrete per-feature methods fastest),
then the two storage monoliths, then `pool.py`. Every split must keep the
original import paths working (re-export contract, see above) and pass the
full lite + real-PostgreSQL gates with zero test edits.

## Sync repository lockstep contract

`LiteSyncRepository` and `PostgresSyncRepository` must expose the same 17
atomic sync operations. Both inherit `AbstractSyncRepository`
(`sync_repository.py`), so dropping or renaming a method fails at
instantiation. `tests/test_sync_repository_contract.py` pins the method set
and signature equality against the `SyncRepository` Protocol in CI.

## Invariants worth knowing before editing

- **Fail-closed authorization**: identity-less nodes are visible only via
  the exact local-development context; unknown visibility is invisible;
  `MemoryNotFoundError` makes "no access" indistinguishable from "absent".
- **Token discipline**: the registry stores SHA-256 digests only; raw
  `MEMPLEX_PRINCIPAL_TOKEN` is hashed once and never logged or persisted.
- **Unauthenticated HTTP is loopback-only** (`_is_loopback_peer`), refused
  when proxy headers are present, and fail-closed outside development.
- **Evidence gating**: `readiness --strict` reports `ready/industrial` only
  with valid HMAC-signed, version-bound, ≤15-minute-old external evidence
  per gate. No evidence ⇒ `blocked`, never a downgrade to warning.
- **Reproducible builds**: release builds twice and byte-compares; any
  nondeterminism (paths, umask, mtimes) is a release blocker.

## Testing topology

- `tests/` (lite suite): CI matrix ubuntu/macos × py3.11–3.13, coverage
  gate ≥68% (actual ~80%).
- Real-PostgreSQL suites (`test_postgres_integration.py`,
  `test_postgres_backup_integration.py`, `test_sync_postgres_integration.py`)
  run against a real database: locally via the self-contained `pgserver`
  (`uv sync --extra pgtest`), in CI via the `test-postgres` job's pgvector
  service container. The CI job deliberately runs a curated contract slice
  (see `tests/test_release_workflows.py` for the pinned list); the deep
  387-test suite is the pre-release local gate.
