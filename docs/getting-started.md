# Getting Started

This guide installs Memplex into local AI agents and verifies that the automatic
memory loop can both capture and recall context.

## Requirements

- macOS, Linux, or another shell environment with Bash.
- Python 3.11 or newer.
- One of `uv`, `python -m venv`, or an existing Python environment.
- Optional: Node.js/npm for the `npx memplex setup` entrypoint.

## Fast Path

Install into detected local agents:

```bash
npx memplex setup
```

Install into every supported agent config on the machine:

```bash
npx memplex setup --agent all --project-path "$PWD"
```

Preview without writing files:

```bash
npx memplex setup --agent all --project-path "$PWD" --dry-run
```

## Choose One Agent

```bash
npx memplex setup --agent codex --project-path "$PWD"
npx memplex setup --agent claude-code --project-path "$PWD"
npx memplex setup --agent openclaw --project-path "$PWD"
npx memplex setup --agent hermes --project-path "$PWD"
```

Supported agent ids are:

| Agent id | Host |
| --- | --- |
| `auto` | detect installed hosts |
| `all` | install every supported host |
| `codex` | Codex MCP config |
| `claude-code` | Claude Code plugin and lifecycle hooks |
| `openclaw` | OpenClaw memory plugin slot |
| `hermes` | Hermes memory provider plugin |

## Python-First Install

If npm is unavailable, install the Python package directly:

```bash
uv tool install memplex==3.2.7
memplex setup --agent all --project-path "$PWD"
```

If you are already inside a Python environment:

```bash
python -m pip install --upgrade memplex==3.2.7
python -m memplex setup --agent all --project-path "$PWD"
```

## Raw Script Fallback

For shell-only environments:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | bash
```

Pass options after `bash -s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh | \
  bash -s -- --agent codex --project-path "$PWD"
```

## What The Installer Writes

| Agent | Files/config written |
| --- | --- |
| Codex | `~/.codex/config.toml` or `$CODEX_HOME/config.toml` |
| Claude Code | plugin files under the Claude config marketplace |
| OpenClaw | `openclaw.json` plus `extensions/memplex` |
| Hermes | `memory-providers/memplex.json` plus `plugins/memory/memplex` |

The npm installer keeps the Python runtime at
`~/.local/share/memplex/agent-venv` unless `MEMPLEX_VENV_DIR` is set.

## Verify The Install

List supported profiles:

```bash
memplex --output json agent list
```

Inspect one agent manifest:

```bash
memplex --output json agent manifest --agent codex
```

Run a local capture and recall smoke:

```bash
memplex --output json agent capture \
  --agent codex \
  --user-message "Use lite storage for local development." \
  --assistant-message "Recorded."

memplex --output json agent recall \
  --agent codex \
  "What storage should local development use?"
```

The second command should return a JSON object with a non-empty `context` when
the memory was captured into the same user, session, project, and storage
namespace.

Run the normal health check:

```bash
memplex health
```

## Offline Or Mainland China

Memplex does not require HuggingFace for the default local-agent path. The
default Lite retrieval path uses a SQLite FTS5 sidecar index with `bm25()`
ranking plus generated trigram tokens. If FTS5 is unavailable, it falls back to
pure-Python local BM25/trigram matching. Setup, hooks, MCP tools, capture,
recall, and compaction can run without reaching `huggingface.co`.

To make this explicit in a shell or agent config:

```bash
export MEMPLEX_EMBEDDING_MODEL=tfidf
```

To add offline semantic vectors from a local ONNX model:

```bash
python -m pip install "memplex[local-onnx]"
export MEMPLEX_LOCAL_ONNX_MODEL=/models/bge-small/model.onnx
export MEMPLEX_LOCAL_ONNX_TOKENIZER=/models/bge-small/tokenizer.json
```

With those variables set, `MEMPLEX_EMBEDDING_MODEL=default` auto-enables the
local ONNX backend and safely falls back to local embedding if the runtime is
not available. You can also force explicit ONNX mode with
`MEMPLEX_EMBEDDING_MODEL=local-onnx` or
`MEMPLEX_EMBEDDING_MODEL=local-onnx:/models/bge-small/model.onnx`; explicit
ONNX mode reports configuration errors instead of silently downgrading.

Use HuggingFace-backed models only after you have network access, a local cache,
or an approved mirror:

```bash
export MEMPLEX_EMBEDDING_MODEL=minilm
export MEMPLEX_EMBEDDING_MODEL=bge-m3
export MEMPLEX_EMBEDDING_MODEL=hf:BAAI/bge-m3
```

If the host cannot reach HuggingFace, those explicit models fall back to local
embedding and SQLite FTS5/BM25+trigram retrieval instead of failing the agent
memory loop.

## Uninstall

Remove every Memplex-managed local agent integration:

```bash
npx memplex uninstall --agent all
```

Or remove one host:

```bash
npx memplex uninstall --agent codex
npx memplex uninstall --agent claude-code
npx memplex uninstall --agent openclaw
npx memplex uninstall --agent hermes
```

Python-first uninstall:

```bash
memplex uninstall --agent all
```

The uninstaller removes only entries marked as managed by Memplex. Existing
unmanaged `memplex` entries are left alone.

## Troubleshooting

### `npx` Uses An Old Package

Force a fresh lookup:

```bash
npx --yes memplex@latest setup --agent codex --project-path "$PWD"
```

If npm cache permissions are broken, use a temporary cache:

```bash
npx --yes --cache /tmp/memplex-npm-cache memplex@latest setup
```

### Python Package Install Fails

Install with `uv` directly:

```bash
uv tool install --force memplex==3.2.7
```

Or use a project-local venv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade memplex==3.2.7
.venv/bin/python -m memplex setup --agent codex --project-path "$PWD"
```

### The Agent Does Not See Memplex

Restart the host agent after installation. Then inspect the generated config:

```bash
memplex --output json agent manifest --agent codex
memplex --output json agent manifest --agent openclaw
memplex --output json agent manifest --agent hermes
```

### HuggingFace Is Blocked Or Slow

No action is required for the default setup. Keep
`MEMPLEX_EMBEDDING_MODEL=default` or set `MEMPLEX_EMBEDDING_MODEL=tfidf` to
avoid remote model loading entirely. Lite storage still uses SQLite
FTS5/BM25+trigram search for recall quality.

If you need a HuggingFace model for higher-quality semantic vectors, pre-cache
the model or configure an approved mirror in the HuggingFace/Python environment
before setting `MEMPLEX_EMBEDDING_MODEL=minilm`, `bge-m3`, or `hf:<model-id>`.

## Next Reading

- [Memplex Explainer](explainer.md)
- [Agent Integration Loop](agent-integration.md)
- [README](../README.md)
