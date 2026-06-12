---
name: mem-explore
description: Explore Memplex knowledge graph. Use when user asks "what do we know about X?", "explore memories", "show related concepts", or wants to browse the knowledge base.
---

# Memory Explore

Browse and explore the Memplex knowledge graph to understand related concepts and connections.

## When to Use

Use when users want to explore stored knowledge:

- "What do we know about X?"
- "Show me everything related to Y"
- "Explore the knowledge graph"
- "What memories are in the database?"

## Workflow

### Broad Exploration

Search with a general query to discover what's stored:

```
memory_search(query="authentication", top_k=20)
```

### Drill Down

Pick interesting results and fetch details:

```
memory_get(memory_id="func_abc123")
```

### Check Health and Stats

Use CLI to see overall state:

```bash
# Service health
memplex health

# Statistics (total memories, types breakdown, graph edges)
memplex stats
```

### Review Pending Conflicts

Check if any memories need resolution:

```
memory_pending_reviews(limit=20)
```

Resolve conflicts:

```
memory_resolve(memory_id="func_abc123", field_role="action", action="accept")
```

## Understanding Memory Structure

Each memory has metadata:

- **version**: Incremented on merge/update
- **confidence**: 0.0-1.0 quality score
- **access_count**: How often this memory has been retrieved
- **domain**: Category (security, testing, devops, etc.)
- **source_type**: Where it came from (wiki, file, url, text)

## Graph Relationships

Memplex auto-detects three edge types:

| Edge | Meaning |
|------|---------|
| REFERENCES | Memory A references Memory B |
| DEPENDS_ON | Memory A depends on Memory B |
| CONFLICTS_WITH | Memory A contradicts Memory B |

These are discovered during write operations via term mapping, reference linking, entity alignment, and domain classification.
