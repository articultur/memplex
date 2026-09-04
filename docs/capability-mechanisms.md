# Capability mechanisms

This is the canonical human-readable map from Memplex capabilities to their
implementation mechanisms, module boundaries, focused tests, and known limits.
The machine-readable companion is [`capabilities.json`](capabilities.json).

The map is deliberately repository-static. A code or test reference means the
mechanism is present at the cited lines; it does not mean the test passed in the
current checkout, a deployment supplied runtime evidence, or Memplex is
industrial-ready. Evidence level 2 in the manifest means implementation and
focused automated checks are identifiable in the repository, with no stronger
current-run claim.

## Module boundaries

Host and transport adapters live in `memplex/adapters/`. They call
`MemplexService`, the orchestration facade, which delegates to authorization,
retrieval, storage, sync, background work, and operations collaborators rather
than owning persistence itself. Domain and storage modules may not import the
adapter layer; import-linter enforces that hexagonal direction.

The service constructor creates its `AuthorizationGate` from lazy store
providers and initializes `RuntimeLifecycle` before backend runtime
composition. `_initialize_runtime()` then constructs storage, sync, typed
lookup, embedding, reranking, feedback, and extraction collaborators. This is
composition wiring, not evidence that the facade itself owns those bounded
mechanisms.

Several compatibility boundaries are intentionally non-ideal but load-bearing:
migration helpers depend on ordered re-exports from `runner.py`, PostgreSQL
resources resolve monkeypatchable names through the live `pool` module, Lite
and PostgreSQL sync implementations stay in a 17-method lockstep contract, and
the shared host adapter cluster is part of every G008 contract digest. The
detailed invariants remain in [Architecture](architecture.md).

- Implementation: [`AuthorizationGate` and `RuntimeLifecycle` wiring](../memplex/service.py#L145-L184)
  and [`MemplexService` runtime composition](../memplex/service.py#L292-L369)
- Contract tests: [split-module import order and facades](../tests/test_dependency_boundaries.py#L19-L66),
  [extracted authorization gate](../tests/test_authorization_gate.py#L46-L63),
  and [extracted runtime lifecycle](../tests/test_operations.py#L61-L77)
- Limit: `MemplexService` remains a large facade, and compatibility re-exports
  constrain otherwise attractive module moves.

## Typed memory model

`MemoryNode` is the common envelope for Function, Fact, Preference, and
Observation. It carries tenant, owner, workspace, visibility, provenance,
version, timestamps, namespace, and knowledge-tier data. Each concrete type
adds its own payload; Fact additionally carries its business-time interval.

- Implementation: [`MemoryNode` fields](../memplex/models/memory.py#L42-L73),
  [`Function`](../memplex/models/memory.py#L154-L169),
  [`Fact`](../memplex/models/memory.py#L255-L270),
  [`Preference`](../memplex/models/memory.py#L315-L347), and
  [`Observation`](../memplex/models/memory.py#L351-L390)
- Contract tests: [type hierarchy and factory](../tests/test_models.py#L372-L407)
- Limit: model and serialization coverage does not alone prove every backend
  preserves every field under failure and migration conditions.

## Capture write path

Host runtimes call the service write boundary after a response. Ordinary
`write()` strips explicit private blocks, optionally augments factual content,
runs the extractor, binds authenticated identity before persistence, scans the
extracted Function/Fact/Preference nodes, persists Fact and Preference nodes,
then merges Functions and graph edges. Observation is not part of ordinary
`write()` extraction or persistence: it uses the separate `add_observation()`
service path, which independently requires authorization, binds trusted
identity, scans the Observation, and calls the scoped store API. Development-only
indexing and compaction are scheduled after ordinary primary persistence and
remain best effort.

- Implementation: [ordinary Function/Fact/Preference write pipeline](../memplex/service.py#L1291-L1408)
  and [authorized Observation path](../memplex/service.py#L1689-L1707)
- Contract tests: [host capture then recall](../tests/test_agent_runtime.py#L26-L46)
  and [basic service writes](../tests/test_service.py#L83-L102),
  [Lite Observation persistence and restart](../tests/test_storage.py#L604-L628),
  and [categorized Observation capture plus failure isolation](../tests/test_observation_pipeline.py#L33-L105)
- Limit: post-write background work is not part of the durable write commit.
  Observation capture is a distinct authorized `add_observation()` path and
  its best-effort host hook failure handling must not be conflated with the
  ordinary Function/Fact/Preference write transaction.

## Recall retrieval path

Recall resolves an authorization context and scoped store, detects intent,
selects RAG, wiki, and/or graph paths, and divides one global candidate budget
across them. Results are merged and deduplicated, authorization/namespace/owner
filters are applied, two reranking stages and the injection filter run, `top_k`
is enforced, access counts are updated, and the final set is clipped to a token
budget. With `explain=True`, path failures, filters, and budgets are represented
in a retrieval trace. The final weighted rerank combines six dimensions: raw
relevance, semantic similarity, recency decay, source authority, frequency, and
per-memory `confidence` — an extraction-quality belief strength persisted on
the node, clamped to [0, 1] and neutral at 0.5 when absent so a missing score
never dominates by absence.

- Implementation: [parallel path budget](../memplex/service.py#L731-L780),
  [token budget](../memplex/service.py#L783-L814),
  [complete query implementation](../memplex/service.py#L817-L1067),
  [RAG/wiki paths](../memplex/retrieval/multi_path.py#L79-L118), and
  [six-dimension weighted rerank](../memplex/retrieval/reranker.py#L61-L105)
- Contract tests: [service query and shared budget](../tests/test_service.py#L108-L167)
  and [RAG/wiki behavior](../tests/test_multi_path.py#L120-L172)
- Limit: repository tests do not establish real-dataset recall, latency, or
  ranking quality.

## Temporal facts

Facts use two time axes. `valid_from` and `invalid_at` describe when a claim is
true in business time; inherited `created_at` and `updated_at` describe when
Memplex learned or changed the row. A contradictory fact supersedes rather than
deletes the previous row, and `list_facts(as_of=...)` exposes the retained
point-in-time history.

- Implementation: [Fact interval fields](../memplex/models/memory.py#L255-L270),
  [interval filtering and supersession](../memplex/temporal.py#L39-L98),
  [service write-path supersession](../memplex/service.py#L1450-L1483), and
  [`as_of` filtering](../memplex/service.py#L1641-L1646)
- Contract tests: [half-open interval and supersession](../tests/test_temporal_facts.py#L42-L69)
  and [service history](../tests/test_temporal_facts.py#L87-L137)
- Limit: this is bi-temporal fact history, not general temporal inference over
  arbitrary graph paths.

## Bounded graph expansion

Graph retrieval is vector- or FTS-seeded and deliberately bounded. It chooses
at most three seeds, reserves part of `top_k` for them, divides the remaining
budget among those seeds, and requests relation-type neighbors with
`max_hops=1` and an explicit limit. It deduplicates neighbors and never falls
back to an unbounded read.

- Implementation: [bounded hop-limited graph search](../memplex/retrieval/multi_path.py#L120-L215)
- Contract tests: [one-hop behavior](../tests/test_multi_path.py#L178-L205)
  and [edge filters plus hard budgets](../tests/test_multi_path.py#L256-L305)
- Limit: this is bounded one- or two-hop expansion (retrieval.graph_max_hops). It must not be described as unrestricted
  traversal or general multi-hop reasoning.

## Principal tenant authorization

Adapters construct an immutable authorization context from a principal plus
workspace, session, agent, credential, and request provenance. The write path
binds those trusted fields before persistence and rejects conflicting payload
claims. Each production service call derives a request-scoped PostgreSQL facade
so concurrent principals do not share mutable authorization state; visibility
then checks tenant before workspace, user, or session scope.

- Implementation: [authorization gate and scoped facade](../memplex/authorization.py#L70-L122)
- Contract tests: [identity binding and forged claims](../tests/test_authorization_context.py#L33-L100)
  and [fail-closed visibility](../tests/test_authorization_gate.py#L57-L105)
- Limit: Lite is a local development store, not a defense against another
  process, root, or direct disk access. Production tenant isolation also
  depends on correctly configured PostgreSQL predicates and RLS.

## Sync convergence

The durable sync path validates tenant-bound, monotonic pages before mutation.
Its repository contract covers snapshots, atomic apply, durable outbox/inbox,
target registration, leases, acknowledgements, retries, dead letters, replay,
compaction, and status. Lite and PostgreSQL implement the same Protocol and ABC,
while the dispatcher can recover persisted delivery state after restart.

The older `SyncableStore` is a separate development compatibility path: it
writes locally, pushes asynchronously on a bounded in-memory queue, logs remote
failure, and may drop the newest task when full. It is explicitly best effort
and must not be used as evidence of durable convergence.

- Implementation: [incoming-page validation](../memplex/sync_repository.py#L74-L133),
  [durable repository operations](../memplex/sync_repository.py#L170-L235),
  [legacy local-first semantics](../memplex/sync.py#L223-L253), and
  [legacy queue drop boundary](../memplex/sync.py#L498-L516)
- Contract tests: [backend lockstep](../tests/test_sync_repository_contract.py#L148-L176),
  [duplicate acknowledgement recovery](../tests/test_sync_dispatcher.py#L232-L306),
  and [restart recovery](../tests/test_sync_dispatcher.py#L506-L541)
- Limit: durable convergence still requires an appropriate real transport,
  correctly operated durable backends, and current failure-mode evidence.

## Backup restore

A backup artifact contains a canonical HMAC-signed manifest with backend,
schema, migration, payload digest, payload size, and tool/server metadata. The
writer publishes through a private temporary directory and the verifier pins
the artifact directory while checking its exact shape, signature, and payload
digest. Lite exposes a development-only pair restore; PostgreSQL has a separate
dump/restore implementation and drill gate.

- Implementation: [signed manifest](../memplex/backup.py#L100-L232),
  [artifact writer and atomic publication](../memplex/backup.py#L803-L919), and
  [verification entry point](../memplex/backup.py#L1022-L1031)
- Contract tests: [publish/verify](../tests/test_backup.py#L133-L175),
  [tamper rejection](../tests/test_backup.py#L308-L328), and
  [Lite restore boundary](../tests/test_backup.py#L370-L418)
- Limit: repository-static evidence is not a current PostgreSQL restore drill
  and does not establish a deployment's RPO or RTO.

## Operations observability

The operations surface models monotonic runtime state and request admission,
emits bounded low-cardinality Prometheus metrics, drains in-flight requests,
hashes shipped alert rules, and builds a signed report tied to deployment,
source, artifact, target identity, observation window, SLOs, and drain result.
The report closes its gate only when all sample, duration, target, and shutdown
conditions hold.

- Implementation: [signed report schema and gate](../memplex/operations.py#L167-L235),
  [evidence construction](../memplex/operations.py#L599-L660), and
  [atomic report output](../memplex/operations.py#L667-L705)
- Contract tests: [runtime lifecycle](../tests/test_operations.py#L61-L77),
  [bounded metrics and drain](../tests/test_operations.py#L138-L223), and
  [window/sample gates](../tests/test_operations_evidence.py#L227-L290)
- Limit: the machinery does not claim a current observation window, production
  load, successful drain, or valid signed report exists.

## Reproducible supply chain

The release builder fixes locale, timezone, hash seed, source epoch, offline
mode, and npm behavior; then it normalizes wheel, sdist, and npm archives. It
emits a CycloneDX SBOM, checksum document, and canonical release manifest.
Focused tests compare outputs built under different paths, locales, and umasks
and inspect normalized archive metadata.

- Implementation: [deterministic release builder](../scripts/build_release_artifacts.py#L200-L296)
- Contract tests: [cross-environment byte equality and archive normalization](../tests/test_reproducible_release.py#L66-L112)
- Limit: this does not prove current public registry artifacts match the
  checkout, nor does it make benchmark outputs immutable or independently
  reproducible.

## Four host lifecycle

Claude Code, Codex, OpenClaw, and Hermes use host-specific launchers and
installers around the shared capture/recall runtime. The installer registry
dispatches install and uninstall for the four supported hosts, rolls back
multi-host mutations to their pre-operation snapshots on failure, and reports
managed-install drift together with a redacted host-local runtime status. That
status records failed operations atomically, clears only a matching successful
operation, and fails closed when its sidecar cannot be trusted.

Separately, G008 binds each proof to the exact CLI, isolated root, host adapter
and shared-runtime digest, required lifecycle nodes, JUnit digest, source,
artifact, deployment, and target. Its evidence schema requires all four hosts
in the fixed order; repository-static coverage does not establish that such an
external evidence artifact exists for the current checkout.

- Implementation: [transactional host install and uninstall dispatch](../memplex/adapters/agent_installer.py#L67-L166),
  [install plus runtime status projection](../memplex/adapters/agent_installer.py#L343-L363),
  [redacted fail-closed runtime status](../memplex/adapters/runtime_status.py#L38-L151),
  [host contract digests and required nodes](../memplex/host_lifecycle.py#L211-L244),
  and [per-host proof plus four-host evidence](../memplex/host_lifecycle.py#L282-L394)
- Contract tests: [registry dispatch, rollback, restoration, and four-host reinstall](../tests/test_agent_installer_registry.py#L43-L459),
  [redacted status and operation-specific recovery](../tests/test_runtime_status.py#L91-L190),
  [concurrent status preservation](../tests/test_runtime_status.py#L193-L241),
  [fail-closed status reads](../tests/test_runtime_status.py#L271-L320),
  [adapter mutation coverage](../tests/test_host_lifecycle_evidence.py#L112-L175),
  and [shared workspace matrix](../tests/test_agent_host_matrix.py#L602-L655)
- Limit: repository tests and evidence schemas are not a current run against
  four real installed hosts. External signed lifecycle evidence remains a
  separate requirement.
