# Positioning and decision guide

This guide helps adopters decide whether Memplex fits their agent-memory
problem. It compares product boundaries and mechanisms, not benchmark scores.
The external comparison is a point-in-time review of official sources accessed
on **2026-08-30**. Local evidence is identified separately and does not turn
repository mechanisms into deployment claims.

## Decision summary

Memplex is best evaluated as a **multi-agent long-term-memory layer for local
agent hosts**. It connects recall-before-turn and capture-after-turn lifecycle
hooks to scoped typed memories, point-in-time fact history, optional shared
knowledge, and Lite or PostgreSQL storage. The canonical mechanism map records
the implementation and limits behind each part of that description in
[Capability mechanisms](capability-mechanisms.md).

That boundary matters. Memplex is not a managed memory platform, a general
temporal graph engine, a complete agent harness, or a workflow-persistence
framework. Mem0 OSS, Graphiti OSS, Letta Code, and LangGraph overlap with
Memplex at different layers; they are not interchangeable products or one
valid leaderboard category.

## Who should evaluate Memplex

- **Users of supported local agent hosts** who want Codex, Claude Code,
  OpenClaw, or Hermes to use a shared recall/capture model while retaining
  host-specific installation and lifecycle behavior.
- **Teams that need explicit memory boundaries** such as tenant, owner,
  workspace, visibility, provenance, and curated knowledge tiers instead of an
  undifferentiated text store.
- **Builders that need fact correction history** with retained superseded
  values and `as_of` reads, while accepting that graph retrieval is currently
  bounded to one-hop expansion.
- **Operators willing to prove their own deployment** with PostgreSQL,
  authorization, restore, operations, and host-lifecycle evidence rather than
  treating repository tests as production certification.

## Bright spots

These are repository-static design strengths, not comparative winner claims:

- The [typed memory model](capability-mechanisms.md#typed-memory-model) makes
  Function, Fact, Preference, and Observation explicit and carries scope,
  provenance, version, and knowledge-tier fields in the common envelope.
- [Temporal facts](capability-mechanisms.md#temporal-facts) retain superseded
  values and expose point-in-time reads instead of overwriting all history.
- The [recall path](capability-mechanisms.md#recall-retrieval-path) combines
  multiple retrieval paths with authorization filters, reranking, injection
  filtering, `top_k`, and a final token budget.
- [Principal and tenant authorization](capability-mechanisms.md#principal-tenant-authorization)
  uses immutable request context and request-scoped PostgreSQL facades; the
  documented limit is equally important: Lite is not a hostile-local-OS
  security boundary.
- Durable sync, signed backup manifests, operations reports, reproducible
  release artifacts, and four-host lifecycle proofs are modeled as separate
  [mechanisms with explicit evidence limits](capability-mechanisms.md#sync-convergence).
- The default local path can retain FTS5/BM25 plus trigram retrieval when
  remote model access is unavailable; see [Offline and Mainland China](../README.md#offline-and-mainland-china).

## Fit and non-fit guide

| Requirement | Decision guidance |
| --- | --- |
| Add scoped long-term memory to the supported local agent hosts | **Good fit to evaluate.** Host adapters share a recall/capture runtime while preserving host-specific lifecycle boundaries. Start with the [real-value CLI workflows](guides/real-value-cli.md). |
| Keep typed, provenance-bearing memories and point-in-time fact history | **Good fit to evaluate.** These are first-class Memplex mechanisms, with the repository-static and current-run limits documented below. |
| Share curated knowledge across agents while retaining private/workspace boundaries | **Good fit to evaluate carefully.** The model includes grants and knowledge tiers, but a production authorization claim still requires deployment-bound PostgreSQL evidence. |
| Embed a general-purpose memory SDK or consume a managed memory API | **Compare [Mem0 OSS](https://docs.mem0.ai/open-source/overview) first.** It documents library and self-hosted-server shapes; managed-platform behavior must be evaluated separately from OSS. |
| Build around an evolving temporal relationship graph | **Compare [Graphiti OSS](https://help.getzep.com/graphiti/getting-started/overview) first.** Its primary abstraction is a temporal Context Graph; do not attribute proprietary Zep capabilities to Graphiti OSS. |
| Adopt a full long-lived agent runtime with agent-owned filesystem memory | **Compare [Letta Code](https://github.com/letta-ai/letta/blob/main/README.md) first.** It supplies the agent harness and Git-backed MemFS model rather than only a host-integrated memory layer. |
| Compose checkpointed workflows and application-defined cross-thread state | **Compare [LangGraph](https://docs.langchain.com/oss/python/concepts/memory) first.** Its checkpointer and Store primitives sit at the workflow framework layer. |
| Require generic multi-hop graph reasoning | **Not an established fit.** Memplex currently documents bounded seeds plus one-hop neighbors, not unrestricted traversal or general multi-hop reasoning. |
| Require a turnkey hostile-local-machine security boundary, HA, SLO, or compliance guarantee | **Not established by this repository.** Lite has an explicit local-development boundary, and production claims require fresh deployment evidence. |

## How the mechanisms differ

Memplex routes host events through adapters into `MemplexService`, which
coordinates authorization, typed capture, multi-path recall, storage, sync,
and operations collaborators. The [architecture map](architecture.md) records
those module boundaries; the table below translates them into adoption
decisions.

| Decision surface | Memplex mechanism | What the mechanism does not prove |
| --- | --- | --- |
| Context lifecycle | Host-specific adapters call a shared before-turn recall and after-turn capture runtime. | That all four real hosts are installed and healthy in the current environment. |
| Knowledge model | Four typed nodes carry identity, scope, provenance, visibility, version, and tier fields. Observation uses a separate authorized path from ordinary Function/Fact/Preference extraction. | That every backend preserves every field under every migration or failure mode. |
| Historical truth | Facts use business-time validity plus inherited record timestamps; contradictions supersede retained rows and `as_of` reads select historical state. | General temporal inference across arbitrary graph paths. |
| Retrieval | RAG, wiki, and graph candidates share a global budget before filtering, reranking, injection filtering, and token clipping. Graph expansion is seed-bounded and one hop. | Public-dataset quality, arbitrary multi-hop reasoning, latency, or capacity. |
| Scope and sharing | Tenant, owner, workspace, visibility, grants, and knowledge tiers are explicit; production calls derive request-scoped PostgreSQL access. | Security or compliance of an unverified deployment, or protection from another process/root reading Lite data. |
| Durability and operations | Durable sync contracts, backup verification, evidence-gated operations, and host lifecycle proofs are distinct mechanisms. | Current recovery objectives, production SLOs, or real-host readiness without fresh signed evidence. |

## Comparison with adjacent open-source peers

The peers below are included only because the G006 research found maintained
official sources and a directly adjacent memory or persistence role. A blank
capability is not inferred as absent; this table describes documented product
boundaries.

A broader point-in-time industry survey recorded on 2026-09-04 — including
Cognee, MemOS, the multi-host memory layer claude-mem/Grok Mem, and host-native
memory in Codex and Claude — is retained in
[Memory landscape research](research/memory-landscape-2026-09.md). That survey
is internal research context and does not modify the boundaries or claims of
this guide.

| System | Product boundary and primary user | Memory model and lifecycle | Retrieval, scope, and integration | Version context checked |
| --- | --- | --- | --- | --- |
| **Memplex** | Host-integrated, multi-agent memory layer for users and teams operating supported local agent hosts. | Typed Function/Fact/Preference/Observation nodes; recall/capture hooks; retained fact supersession and `as_of` reads. | Multi-path retrieval with explicit budgets; tenant/owner/workspace/visibility model; Lite and PostgreSQL backends. | Local checkout declares unreleased `3.3.0`; the [README install section](../README.md#install) identifies public stable `3.2.7`. The retained benchmark artifact is from a dirty worktree and is not release evidence. |
| **Mem0 OSS** | The official [OSS overview](https://docs.mem0.ai/open-source/overview) describes an embeddable Python/Node library and a self-hosted server for application developers. | The official [memory evaluation guide](https://docs.mem0.ai/core-concepts/memory-evaluation) describes extraction and retrieval phases. This row makes no claim about Platform V3 write behavior. | The OSS overview documents configurable LLM, embedding, vector-store, and reranking components. Reviewed OSS sources do not establish a Memplex-equivalent authorization proof. | [Python SDK v2.0.19](https://github.com/mem0ai/mem0/releases/tag/v2.0.19), checked 2026-08-30. Mem0 Platform APIs and behavior are outside this OSS row. |
| **Graphiti OSS** | The official [Graphiti overview](https://help.getzep.com/graphiti/getting-started/overview) describes a temporal Context Graph framework for applications that ingest changing conversational or structured data. | Episodes incrementally produce entities and temporal relationships; invalidated facts retain historical context. The upstream [architecture paper](https://arxiv.org/abs/2501.13956) is design evidence, not an independent product ranking. | The overview documents time, full-text, semantic, and graph retrieval. It also separates local Graphiti OSS from Zep's proprietary managed Context Graph Engine and Context Lake. | [Graphiti v0.29.3](https://github.com/getzep/graphiti/releases/tag/v0.29.3), checked 2026-08-30. Zep enterprise claims do not transfer to Graphiti OSS. |
| **Letta Code** | The [Letta project README](https://github.com/letta-ai/letta/blob/main/README.md) points to Letta Code as the current stateful-agent runtime and marks the legacy V1 server as historical. | Official [MemFS documentation](https://github.com/letta-ai/letta-docs-md/blob/main/concepts/memfs/index.md) describes agent-owned, Git-backed Markdown memory; [memory configuration docs](https://github.com/letta-ai/letta-docs-md/blob/main/configuration/memory/index.md) describe durable updates and dreaming. | `system/` memory is loaded into context and other files are discovered on demand. Semantic/hybrid MemFS search requires the documented optional search tooling; shared Git-backed repositories are a separate collaboration shape. | [Letta Code v0.31.6](https://github.com/letta-ai/letta-code/releases/tag/v0.31.6), checked 2026-08-30. |
| **LangGraph memory** | The official [memory overview](https://docs.langchain.com/oss/python/concepts/memory) presents memory as workflow primitives for developers composing stateful applications. | Thread state is checkpointed; cross-thread long-term data uses an application-defined Store. The application chooses hot-path or background writes. | The [add-memory guide](https://docs.langchain.com/oss/python/langgraph/add-memory) documents Store namespaces and optional semantic search; [persistence docs](https://docs.langchain.com/oss/python/langgraph/persistence) cover resume, history, time travel, and checkpoint retention concerns. | [LangGraph 1.2.11](https://github.com/langchain-ai/langgraph/releases/tag/1.2.11), checked 2026-08-30. This row does not treat workflow persistence as an automatic memory-extraction product. |

## Apples-to-oranges caveats

1. **The product layers differ.** Feature counts cannot rank a memory
   SDK/server, temporal graph framework, agent harness, workflow framework,
   and host-integrated memory layer.
2. **OSS and managed products are separate comparison objects.** Graphiti OSS
   is not Zep's proprietary Context Lake, and Mem0 OSS is not Mem0 Platform.
3. **Shared labels do not imply shared semantics.** “Memory,” “history,”
   “temporal,” and “graph” can represent different write policies,
   invalidation rules, query APIs, and context-placement strategies.
4. **A valid quality comparison needs one frozen protocol.** It must pin
   release or commit, dataset and license, model/provider, embeddings,
   reranker, write policy, retrieval budget, hardware, token/cost accounting,
   metric definitions, raw per-query traces, and failure handling.
5. **Security and operations depend on deployment.** Documentation, static
   code, focused tests, or release notes do not prove isolation, durability,
   recovery, SLOs, or compliance for any system.

## Current local evidence

The following evidence was checked on **2026-08-30** in the current dirty
worktree. It is deliberately narrower than a release or production claim.

| Evidence | Fresh result | Valid conclusion | Explicit boundary |
| --- | --- | --- | --- |
| G003 retained artifact verifier | `{"evidence_level": "E1"}` | The four-file synthetic bundle is internally valid under the current verifier. | The bundle is aggregate-only, unsigned, unpublished, based on a dirty worktree, and contains no public-dataset or per-query raw results. |
| Retained synthetic benchmark bundle | 56 aggregate rows across seven generated synthetic dataset entries; only `retrieval` and `temporal_multihop` are marked `passed`. | The artifact can support smoke-level discussion of those generated workloads. | ACL, sync, latency/capacity, recovery, and host integration are `not_measured`; LongMemEval multi-hop has one synthetic sample scored `0.0` under the bundle's retired `answer_hit_rate` metric (the current runner reports `token_f1` as primary and the one-directional `substring_hit_rate` as an auxiliary diagnostic). |

Focused worktree tests were also run while editing this documentation, but no
provenance-rich artifact was retained. They are non-published local
verification and are intentionally excluded from the formal evidence table.

Retained bundle verification command:

```bash
.venv/bin/python scripts/run_g003_benchmark.py verify \
    --run-dir artifacts/g003-synthetic-worktree-ef9aa8f-k10
```

See the [current worktree benchmark report](current-worktree-benchmark.md) and
its [manifest](../artifacts/g003-synthetic-worktree-ef9aa8f-k10/manifest.json)
for the exact source digest, environment, configuration, datasets, coverage,
and limitations. The broader [point-in-time baseline](open-source-benchmark-baseline.md)
still marks Memplex as **not benchmark-qualified** under its stated rubric.

## Limitations and prohibited claims

Do not use this guide or the current local evidence to claim any of the
following:

- Memplex is benchmark-qualified, better, faster, more accurate, more
  token-efficient, or production-superior to a peer.
- Memplex supports true generic multi-hop reasoning. The documented graph
  mechanism is bounded one-hop expansion.
- Public LongMemEval, LoCoMo, NQ, TriviaQA, PopQA, or HotpotQA results were run.
  The retained bundle contains generated synthetic inputs only.
- The retained artifact represents a current clean SHA, released `3.2.7`, or
  the unreleased checkout as a clean build.
- The full test suite, real PostgreSQL gate, real four-host lifecycle, capacity
  gate, deployment SLO, recovery objective, or current production readiness
  passed because the focused checks above passed.
- Repository workflow files establish current Actions checks or outcomes.
- Lite protects against another local process, root, or direct disk access, or
  that the documented authorization model is a compliance certification.

A future comparative claim must publish the shared protocol, immutable raw
artifacts, exact versions, failures, and independently reproducible results.
Until then, use this page to choose the right product layer and evaluation
plan, not to declare a winner.
