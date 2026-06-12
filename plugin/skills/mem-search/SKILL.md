---
name: mem-search
description: Search Memplex persistent memory. Use when user asks "did we already solve this?", "how did we do X?", "recall", "lookup", or needs work from previous sessions.
---

# Memory Search

Search Memplex knowledge graph across all sessions. Simple workflow: search → filter → fetch.

## When to Use

Use when users ask about PREVIOUS sessions or stored knowledge (not current conversation):

- "Did we already fix this?"
- "How did we solve X last time?"
- "What do we know about Y?"
- "Recall the steps for Z"

## 3-Layer Workflow (ALWAYS Follow)

**NEVER fetch full details without filtering first. 10x token savings.**

### Step 1: Search -- Get Index with IDs

Use the `memory_search` MCP tool:

```
memory_search(query="authentication", top_k=20)
```

**Returns:** Table with IDs, names, relevance scores, domains (~50-100 tokens/result)

```
| ID | Name | Relevance | Domain |
|----|------|-----------|--------|
| func_abc123 | JWT Auth Flow | 0.92 | security |
| func_def456 | Token Refresh | 0.85 | security |
```

**Parameters:**

- `query` (string) - Natural language search query
- `top_k` (number) - Max results, default 10

### Step 2: Filter -- Review Results

Review names and relevance scores from Step 1. Pick relevant IDs. Discard the rest.

### Step 3: Fetch -- Get Full Details ONLY for Filtered IDs

Use the `memory_get` MCP tool:

```
memory_get(memory_id="func_abc123")
```

**Returns:** Complete memory object with trigger, condition, action, benefit fields, plus metadata

## Examples

**Find recent knowledge about a topic:**

```
memory_search(query="authentication", top_k=20)
```

**Get details for specific memories:**

```
memory_get(memory_id="func_abc123")
memory_get(memory_id="func_def456")
```

**Submit feedback on a memory:**

```
memory_feedback(memory_id="func_abc123", role="trigger", index=0, verdict="correct")
memory_feedback(memory_id="func_abc123", role="action", index=1, verdict="wrong")
```

## Why This Workflow?

- **Search index:** ~50-100 tokens per result
- **Full memory:** ~500-1000 tokens each
- **10x token savings** by filtering before fetching
