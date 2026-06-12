---
name: mem-write
description: Store knowledge into Memplex memory. Use when user says "remember this", "save this", "memorize", "store", or provides documents/URLs/content for future reference.
---

# Memory Write

Store knowledge into Memplex for persistent cross-session retrieval.

## When to Use

Use when users want to save knowledge for later:

- "Remember this: ..."
- "Save this for later"
- "Store this document/URL"
- "Memorize these steps"

## Workflow

### Store Text Content

Use the `memory_add` MCP tool:

```
memory_add(content="Steps to deploy: 1) Run tests 2) Build 3) Push to main", source_type="text")
```

**Returns:** Extracted function IDs, graph edges created

```
{
  "functions_extracted": 2,
  "edges": 3,
  "function_ids": ["func_abc123", "func_def456"]
}
```

### Store from File

Use the CLI:

```bash
memplex write --file /path/to/document.md
```

### Store from URL

Use the CLI:

```bash
memplex write --url https://example.com/docs
```

### Review What Was Stored

After writing, use `memory_get` to verify the extracted knowledge:

```
memory_get(memory_id="func_abc123")
```

## What Gets Stored

Memplex extracts structured knowledge into 4 memory types:

| Type | Structure | Example |
|------|-----------|---------|
| Function | trigger → condition → action → benefit | "When deploying, if tests pass, run build, to ship faster" |
| Fact | subject → predicate → object | "Python uses GIL for thread safety" |
| Preference | aspect → preference | "User prefers pytest over unittest" |
| Observation | event → context | "Bug in auth module at line 42" |

## Notes

- Content is deduplicated automatically (same name → merge)
- Graph edges (REFERENCES, DEPENDS_ON, CONFLICTS_WITH) are auto-detected
- Private content wrapped in `<private>...</private>` is stripped before storage
