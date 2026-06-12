"""Memplex Claude Skill Adapter -- generates SKILL.md and hook scripts.

Generates files that integrate Memplex into Claude Code's skill system:

- ``SKILL.md``: skill description with YAML frontmatter and trigger conditions
- ``hook.sh``: PostToolUse hook script that auto-collects observations

Usage::

    from memplex.adapters.claude_skill import generate_skill_md, generate_hook_sh

    skill_content = generate_skill_md()
    hook_content = generate_hook_sh()
"""

from __future__ import annotations

import textwrap
from typing import Optional

_SKILL_MD_TEMPLATE = textwrap.dedent("""\
    ---
    name: memplex
    description: Search and manage Memplex persistent memory. Use when user asks to "search memory", "recall", "remember this", "save", "lookup", or needs knowledge from previous sessions.
    ---

    # Memplex Memory Skill

    Persistent knowledge graph for multi-agent workflows. Store, query, and
    manage knowledge that persists across sessions.

    ## When to Use

    Activate when the user:
    - Asks to find or recall information from past sessions
    - Provides content and asks to "remember" or "save" it
    - Wants to review, correct, or update existing memories
    - Uses keywords: "memplex", "memory", "remember", "recall", "lookup"

    ## 3-Layer Retrieval (ALWAYS Follow)

    **NEVER fetch full details without filtering first.**

    ### Step 1: Search -- Get Index with IDs

    Use the `memory_search` MCP tool:

    ```
    memory_search(query="search text", top_k=10)
    ```

    Returns: IDs, names, relevance scores (~50-100 tokens/result)

    ### Step 2: Filter -- Review Results

    Pick relevant IDs from search results. Discard the rest.

    ### Step 3: Fetch -- Get Full Details for Filtered IDs

    ```
    memory_get(memory_id="func_abc123")
    ```

    Returns: Complete memory with all fields (~500-1000 tokens)

    ## Write Memory

    ```
    memory_add(content="text to remember", source_type="text")
    ```

    ## Feedback and Maintenance

    ```
    memory_feedback(memory_id="...", role="trigger", index=0, verdict="correct")
    memory_pending_reviews(limit=20)
    memory_health()
    ```

    ## CLI Commands

    ```bash
    memplex query "search text"       # Search memories
    memplex write --text "content"    # Write memory
    memplex get <memory_id>           # Get details
    memplex health                    # Health check
    memplex stats                     # Statistics
    memplex compact --scope project   # Run compaction
    ```
""")


def generate_skill_md(output_path: Optional[str] = None) -> str:
    content = _SKILL_MD_TEMPLATE.strip() + "\n"

    if output_path is not None:
        import os

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

    return content


_HOOK_SH_TEMPLATE = textwrap.dedent("""\
    #!/usr/bin/env bash
    # Memplex PostToolUse Hook -- auto-collect observations
    #
    # Register in plugin/hooks/hooks.json.
    #
    # Environment variables provided by Claude Code:
    #   MEMPLEX_TOOL_NAME    - name of the tool that was called
    #   MEMPLEX_SESSION_ID   - current session identifier

    set -euo pipefail

    # Rate limit: skip if last observation was less than 30 seconds ago
    RATE_FILE="/tmp/.memplex_last_obs_${MEMPLEX_SESSION_ID:-default}"
    if [ -f "$RATE_FILE" ]; then
        LAST=$(cat "$RATE_FILE" 2>/dev/null || echo 0)
        NOW=$(date +%s)
        DIFF=$((NOW - LAST))
        if [ "$DIFF" -lt 30 ]; then
            exit 0
        fi
    fi

    TOOL_NAME="${MEMPLEX_TOOL_NAME:-unknown}"
    TOOL_INPUT="${MEMPLEX_TOOL_INPUT:-}"

    # Skip low-value tools
    case "$TOOL_NAME" in
        Read|read) exit 0 ;;
    esac

    # Build observation text from tool input
    OBS_TEXT="[$TOOL_NAME] $TOOL_INPUT"

    # Truncate to reasonable length
    if [ ${#OBS_TEXT} -gt 500 ]; then
        OBS_TEXT="${OBS_TEXT:0:500}..."
    fi

    # Store observation via CLI
    if command -v memplex &>/dev/null; then
        memplex write --text "$OBS_TEXT" --output json 2>/dev/null || true
    fi

    # Update rate limit timestamp
    date +%s > "$RATE_FILE"
""")


def generate_hook_sh(output_path: Optional[str] = None) -> str:
    content = _HOOK_SH_TEMPLATE.strip() + "\n"

    if output_path is not None:
        import os
        import stat

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)

        st = os.stat(output_path)
        os.chmod(output_path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return content
