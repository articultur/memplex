---
name: mem-manage
description: Manage Memplex memory system. Use when user asks to "compact memories", "cleanup database", "memory stats", "run compaction", or perform maintenance operations.
---

# Memory Manage

Perform maintenance and management operations on the Memplex knowledge base.

## When to Use

Use when users want to maintain or manage memories:

- "Compact the memory database"
- "Clean up old memories"
- "Show me memory stats"
- "Run compaction"
- "Check memory health"

## Operations

### Health Check

```
memory_health()
```

Or via CLI:

```bash
memplex health
```

### Statistics

```bash
memplex stats
```

Shows total memories, breakdown by type, graph edges, storage size.

### Run Compaction

5-stage pipeline: Extract → Dedup → Summarize → Prune → Archive

```bash
# Compact project-level memories
memplex compact --scope project

# Compact all memories
memplex compact --scope global
```

Compaction automatically:
- Deduplicates memories with similar names
- Summarizes verbose field values
- Prunes low-confidence entries
- Archives stale memories

### Submit Feedback

Improve memory quality over time:

```
memory_feedback(memory_id="func_abc123", role="trigger", index=0, verdict="correct")
memory_feedback(memory_id="func_abc123", role="action", index=1, verdict="wrong", reason="Should be POST not GET")
```

### Delete Memories

```
memory_delete(memory_id="func_abc123")
```

Or via CLI:

```bash
memplex delete func_abc123
```

### Update Memory Fields

```
memory_update(memory_id="func_abc123", role="action", new_value="Use OAuth2 with PKCE flow")
```

## Notes

- Compaction is safe: it creates backups before modifying
- Feedback weights affect future retrieval relevance
- Delete is permanent -- use with care
- Stats refresh after each operation
