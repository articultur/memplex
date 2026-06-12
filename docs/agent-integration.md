# Agent Integration Loop

Memplex now exposes a shared agent runtime for Codex, Claude Code, OpenClaw,
Hermes, and similar tool-using agents. The goal is a closed production-to-
consumption memory loop rather than manual memory entry.

## Shared Loop

All agent adapters should map their platform lifecycle into three operations:

1. **Recall before the model turn**
   - Call `memory_turn_begin` over MCP, or `memplex agent recall`.
   - Inject the returned `context` into the prompt as data.
   - The context is wrapped by `IndirectInjectionGuard` before injection.

2. **Capture after the model turn**
   - Call `memory_turn_end` over MCP, or `memplex agent capture`.
   - Pass the user message and assistant response.
   - Memplex writes the turn as observation input through `MemplexService`.

3. **Consolidate in the background**
   - Use existing compaction and background worker paths for dedup, summarize,
     prune, archive, vector refresh, and wiki compilation.
   - Hermes-style adapters can pass `next_prompt_hint` to `memory_turn_end` so
     Memplex prefetches context for the next turn.

## Supported Agent Profiles

The portable profile registry is implemented in
`memplex.adapters.agent_runtime`.

| Agent | Integration modes | Memory behavior |
| --- | --- | --- |
| Codex | MCP, CLI, hooks | auto recall, auto capture, background consolidation |
| Claude Code | plugin, MCP, lifecycle hooks, CLI | lifecycle prompt recall/tool observation plus MCP tools |
| OpenClaw | plugin slot, CLI, MCP | triage, recall, dream shape; memory slot config |
| Hermes | memory provider, CLI, MCP | sync after response plus zero-latency prefetch |

Discover profiles:

```bash
memplex --output json agent list
memplex --output json agent manifest --agent openclaw
```

Install or uninstall an agent host with one command:

```bash
memplex agent install --agent codex
memplex agent install --agent claude-code
memplex agent install --agent openclaw --user-id alice --project-path /path/to/project
memplex agent install --agent hermes --user-id alice --project-path /path/to/project
memplex agent install --agent all --user-id alice --project-path /path/to/project

memplex agent uninstall --agent openclaw
memplex agent uninstall --agent all
```

### Without A Checkout

Users do not need to download this repository. The hosted installer creates a
persistent Memplex Python environment and registers Memplex into a detected
local agent host. The recommended public entrypoint is the npm CLI:

```bash
npx memplex setup
```

Pass an agent when the user wants a specific host:

```bash
npx memplex setup --agent codex --project-path "$PWD"
npx memplex setup --agent claude-code --project-path "$PWD"
npx memplex setup --agent openclaw --project-path "$PWD"
npx memplex setup --agent hermes --project-path "$PWD"
npx memplex setup --agent all --project-path "$PWD"
```

Uninstall is symmetrical:

```bash
npx memplex uninstall --agent all
```

The `npx` package follows the same convention as CLIs such as
`npx shadcn@latest init` and `npx create-next-app@latest`: the package name is
the product name, the first command is the setup action, and the installer owns
the host-specific details.

Python-first users can install a persistent command and then run the same setup
verb:

```bash
uv tool install memplex==3.2.7
memplex setup
```

The raw script remains available as a no-npm fallback:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | bash
```

Auto-detection checks local config directories and commands for Codex, Claude
Code, OpenClaw, and Hermes. If the user knows the target, pass it explicitly:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent codex --project-path "$PWD"

curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent claude-code --project-path "$PWD"

curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent openclaw --project-path "$PWD"

curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent hermes --project-path "$PWD"
```

To configure all supported hosts in one transaction with the raw script:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent all --project-path "$PWD"
```

Direct `uv` is supported through the script. The installer deliberately creates
a persistent venv instead of using one-shot `uvx`, because generated agent
configs must keep importing Memplex after the install command exits:

```bash
MEMPLEX_AGENT=auto \
MEMPLEX_PACKAGE=memplex==3.2.7 \
MEMPLEX_PROJECT_PATH="$PWD" \
bash scripts/install-agent.sh
```

The installers are deliberately reversible:

- Codex gets a marker-bounded `mcp_servers.memplex` block in
  `$CODEX_HOME/config.toml` or `~/.codex/config.toml`. The generated MCP
  command uses the current Python executable, or `MEMPLEX_PYTHON` when set.
- Claude Code reuses the packaged plugin installer under
  `$CLAUDE_CONFIG_DIR/plugins/marketplaces/articultur`. Its lifecycle hooks
  share the same `AgentMemoryRuntime` namespace, injection guard, and capture
  path as the MCP/CLI integrations.
- OpenClaw gets a `plugins.slots.memory = "memplex"` entry in
  `$OPENCLAW_CONFIG_DIR/openclaw.json` or `~/.openclaw/openclaw.json`, with
  `triage`, `recall`, and `dream` enabled, plus an extension scaffold under
  `$OPENCLAW_CONFIG_DIR/extensions/memplex`. The installer accepts
  JSONC/trailing-comma config and writes both `openclaw.plugin.json` and
  `plugin.json` for host compatibility. Existing unmanaged `memplex` entries or
  extension directories are refused, not overwritten.
- Hermes gets a provider descriptor at
  `$HERMES_CONFIG_DIR/memory-providers/memplex.json` or
  `~/.hermes/memory-providers/memplex.json`, plus a Hermes-native provider
  plugin under `$HERMES_CONFIG_DIR/plugins/memory/memplex`. Existing unmanaged
  provider files or plugin directories are refused, not overwritten.
- `memplex agent install --agent all` is transactional: if a later host fails,
  previously installed Memplex-managed hosts are uninstalled in reverse order.

Use the closed loop from shell:

```bash
memplex --output json agent recall --agent codex "What did we decide about storage?"
memplex --output json agent capture \
  --agent codex \
  --user-message "Use lite storage for local dev." \
  --assistant-message "Recorded the local storage decision."
```

The shell CLI is process-per-call, so it persists captured turns and recalls them
through live search on the next invocation. Hermes-style zero-latency prefetch is
available when the adapter uses a long-lived MCP/server process; the prefetch
cache is scoped by storage path, project path, agent, user, and session.

Use the closed loop from MCP:

- `memory_agent_manifest`
- `memory_turn_begin`
- `memory_turn_end`
- existing memory CRUD/search/feedback tools

Claude Code's packaged plugin already has lifecycle hooks for prompt-time recall
and post-tool observation collection. `memory_turn_begin` / `memory_turn_end`
are the portable MCP turn contract for clients that can pass a complete
user/assistant turn; lifecycle-only clients can keep using the hook runner while
sharing the same `MemplexService` and storage.

## External Patterns Reflected Here

- Mem0's Claude Code integration combines MCP tools with lifecycle hooks that
  recall memories before prompts and capture learnings at lifecycle points.
- Mem0's OpenClaw integration names the core loop as triage, recall, and dream;
  Memplex uses the same shape in the OpenClaw manifest and installer.
- Mem0's Hermes integration uses background sync after a response and prefetch
  for the next turn; Memplex exposes `next_prompt_hint` for that path.
- LangGraph distinguishes short-term thread state from long-term namespaced
  memory, and separates hot-path writes from background writes. Memplex profiles
  carry `user_id` and `session_id`, while capture can run after the turn.

## Design Alignment

This implements the adapter-layer requirements for the public integration
surface:

- adapters share `MemplexService`;
- hooks/tools perform protocol conversion only;
- memory production is automatic through lifecycle capture;
- memory consumption is automatic through pre-turn recall;
- compaction remains the background "dream" path.
