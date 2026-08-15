# Memory vs Knowledge — One Store, Two Lifecycles

Design answer to "do we need separate memory and knowledge-base systems?"
**No: one storage engine, two lifecycles, distinguished by
`knowledge_tier`.** Building two systems would duplicate the node model,
retrieval, tenancy ACL, and sync for a difference that is fundamentally
about *provenance and lifecycle*, not storage shape.

## The real differences

| Dimension | Memory (记忆) | Knowledge (知识) |
|-----------|---------------|------------------|
| Provenance | Conversation-derived, agent-experienced | Curated — promoted through review |
| Temporality | Bi-temporal, supersede-on-contradiction (`temporal.py`) | Versioned revisions (`version` + promotion provenance) |
| Trust unit | Personal belief (`confidence` score) | Endorsement (`promoted_by` provenance stamp) |
| Churn | High write, auto-expire, compact | Low write, deliberate promote |
| Default reader | The capturing agent (user visibility) | The team (workspace visibility at `team` tier) |
| Lifecycle | capture → factualize → compact → supersede | capture → **review → promote** → maintain |

## The tier model (`knowledge_tier` field)

| Tier | Meaning | Read scope | Typical content |
|------|---------|-----------|-----------------|
| `None` (default) | Plain personal memory | Per `visibility` (user default) | Conversation facts, observations |
| `personal` | Curated personal knowledge | user | Refined personal notes |
| `domain` | Domain knowledge | user until shared; **domain-bound agents filter on it** | DB recipes, deploy conventions |
| `team` | Team knowledge | **workspace** (all member agents) | Team decisions, shared conventions |

Promotion (`service.promote(id, tier)`) is provenance-stamped
(who/when/tier), version-bumped, and idempotent. Team promotion widens
visibility to the workspace; domain promotion keeps the owner's scope and
relies on agent-domain binding for discovery.

## Team topology: how agents interconnect

1. **Same-workspace agents** see each other's workspace/team-tier knowledge
   by default (the blackboard model, with `source_agent` provenance).
2. **Cross-agent grants** (`service.share_with(id, agent_id)`) let an owner
   open one private memory to a named peer — additive, idempotent, honoured
   by the authorization gate (`memplex_grants` namespace key).
3. **Agent-domain binding** (`agent_domains.agent_domains` config) scopes a
   runtime's recall to its bound knowledge domains — every visibility
   branch is exploded with `domain` pinned, so a "database agent" sees
   database-domain knowledge and nothing else.
4. **Distributed**: tiers and grants are ordinary node data — they ride the
   existing sync (central server / P2P mesh) and RLS/ACL enforcement
   unchanged across sites.

## What deliberately stays out

- Knowledge *editing* UI / diff review flows — the promotion API is the
  machine surface; human curation tooling builds on it.
- Cross-tenant knowledge markets — grants are tenant-internal by the
  fail-closed ACL; cross-tenant sharing is a deployment topology decision,
  not a code path.
