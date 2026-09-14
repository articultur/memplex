# Examples

Runnable, offline, side-effect-free introductions to the Memplex service
API. Every example stores its data in a throwaway temporary directory —
nothing touches `~/.memplex`.

```bash
# from a source checkout
.venv/bin/python examples/quickstart.py
.venv/bin/python examples/temporal_facts.py
```

| Example | Shows |
| --- | --- |
| [quickstart.py](quickstart.py) | Minimal closed loop: `write_text()` → `query()` → top-k recall. |
| [temporal_facts.py](temporal_facts.py) | Bi-temporal facts: correction supersedes the old value, `as_of` reconstructs what was believed at any point in time. |

For agent-host installation (Codex, Claude Code, OpenClaw, Hermes) see the
[root README](../README.md) and [docs/agent-integration.md](../docs/agent-integration.md).
