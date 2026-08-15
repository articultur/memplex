# Curation UI — Design (pending implementation)

The knowledge-tier operations (`promote`, `share_with`, `facts` listing)
have CLI + MCP surfaces. A human curation console is the remaining gap.

## Goals
1. Browse captured memories by tier / domain / recency
2. Review + promote to personal / domain / team with provenance display
3. Manage cross-agent grants (grant / revoke)
4. Point-in-time history view (bi-temporal `as_of`)

## Options

### A. Web console (recommended)
- `memplex server` already exposes the HTTP API; add a static admin
  page served from the same app at `/admin` (auth = same principal
  registry, loopback default).
- Stack: vanilla JS + fetch (no build step) — the repo is Python-only.
- Views: memories table (filterable) → detail drawer with tier selector
  + grant editor + history timeline.

### B. TUI (textual)
- `memplex tui` using `textual` (new dep) for terminal curation.
- Pros: no HTTP surface; cons: new runtime dep + smaller audience.

### C. MCP-first (agents curate)
- Extend the MCP tools with review workflow (list candidates →
  promote); no human UI, agents do curation.

## Recommendation
Ship **A** (web console) as the primary, reuse the existing
`service.promote/share_with/list_facts` APIs directly (they are already
HTTP-adjacent). Estimated 3-5 days: static HTML + fetch wiring + a
`/admin` route with registry auth. C remains free via the already-shipped
MCP tools.
