"""Embedded plugin assets written into host agent installations.

Extracted from ``agent_installer.py``: the OpenClaw extension manifest plus its
bundled JavaScript and the Hermes memory-provider plugin source. Pure
file-writing functions; re-exported from ``memplex.adapters.agent_installer``
and covered by the G008 host-contract digests via
``host_lifecycle._contract_files``.

``_package_version`` / ``_managed_identity_payload`` live in
``agent_installer`` (which imports this module at its end); they are
imported lazily inside the functions so module loading stays one-directional.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as formatted JSON (shared with the installer module)."""
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

def _write_openclaw_extension(
    extension_dir: Path,
    *,
    user_id: str,
    project_path: str,
    source_root: str,
    host_root: str,
    install_state: dict[str, Any],
) -> None:
    from memplex.adapters.agent_installer import (  # lazy: avoid circular import
        _managed_identity_payload,
        _package_version,
    )
    if extension_dir.exists():
        shutil.rmtree(extension_dir)
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "memplex",
        "name": "Memplex",
        "version": _package_version(),
        "description": "Memplex long-term memory for OpenClaw agents",
        "kind": "memory",
        "activation": {"onStartup": True},
        "contracts": {
            "tools": ["memory_recall", "memory_store"],
        },
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "userId": {"type": "string", "minLength": 1},
                "projectPath": {"type": "string", "minLength": 1},
                "python": {"type": "string", "minLength": 1},
                "sourceRoot": {"type": "string", "minLength": 1},
                "autoRecall": {"type": "boolean", "default": True},
                "autoCapture": {"type": "boolean", "default": True},
                "topK": {"type": "integer", "minimum": 1, "maximum": 50},
                "tokenBudget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 32000,
                },
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 60000,
                },
                "visibility": {
                    "type": "string",
                    "enum": ["session", "workspace", "user"],
                    "default": "workspace",
                },
                "managed": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        },
    }
    _write_json(extension_dir / "openclaw.plugin.json", manifest)
    _write_json(extension_dir / "plugin.json", manifest)
    _write_json(
        extension_dir / "package.json",
        {
            "name": "@memplex/openclaw-plugin",
            "version": _package_version(),
            "description": "Native OpenClaw lifecycle bridge for Memplex",
            "type": "module",
            "private": True,
            "main": "./index.js",
            "peerDependencies": {"openclaw": ">=2026.5.17"},
            "openclaw": {"extensions": ["./index.js"]},
        },
    )
    managed = {
        "by": "memplex",
        "installer": "memplex",
        "schema_version": 1,
    }
    _write_json(
        extension_dir / "memplex-agent.json",
        _managed_identity_payload(
            agent="openclaw",
            user_id=user_id,
            project_path=project_path,
            source_root=source_root,
            host_root=host_root,
            managed=managed,
        ),
    )
    install_state_path = extension_dir / ".memplex-install-state.json"
    _write_json(install_state_path, install_state)
    install_state_path.chmod(0o600)
    (extension_dir / "index.js").write_text(_openclaw_plugin_javascript())


def _openclaw_plugin_javascript() -> str:
    return r"""import { accessSync, constants, readFileSync, realpathSync, statSync } from "node:fs";
import { spawn } from "node:child_process";
import { delimiter, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function identityError(detail) {
  return new Error(`Memplex managed identity invalid; reinstall required: ${detail}`);
}

function parseJsonWithoutDuplicateKeys(text) {
  let index = 0;
  const skipWhitespace = () => {
    while (/\s/u.test(text[index] || "")) index += 1;
  };
  const parseString = () => {
    const start = index;
    if (text[index] !== '"') throw new SyntaxError("expected string");
    index += 1;
    while (index < text.length) {
      if (text[index] === "\\") {
        index += 2;
        continue;
      }
      if (text[index] === '"') {
        index += 1;
        return JSON.parse(text.slice(start, index));
      }
      index += 1;
    }
    throw new SyntaxError("unterminated string");
  };
  const parseValue = () => {
    skipWhitespace();
    if (text[index] === "{") return parseObject();
    if (text[index] === "[") {
      const values = [];
      index += 1;
      skipWhitespace();
      if (text[index] === "]") { index += 1; return values; }
      while (true) {
        values.push(parseValue());
        skipWhitespace();
        if (text[index] === "]") { index += 1; return values; }
        if (text[index] !== ",") throw new SyntaxError("expected array separator");
        index += 1;
      }
    }
    if (text[index] === '"') return parseString();
    const start = index;
    while (index < text.length && !/[\s,}\]]/u.test(text[index])) index += 1;
    if (start === index) throw new SyntaxError("expected value");
    return JSON.parse(text.slice(start, index));
  };
  const parseObject = () => {
    const value = {};
    const keys = new Set();
    index += 1;
    skipWhitespace();
    if (text[index] === "}") { index += 1; return value; }
    while (true) {
      skipWhitespace();
      const key = parseString();
      if (keys.has(key)) throw identityError(`duplicate key ${JSON.stringify(key)}`);
      keys.add(key);
      skipWhitespace();
      if (text[index] !== ":") throw new SyntaxError("expected object colon");
      index += 1;
      value[key] = parseValue();
      skipWhitespace();
      if (text[index] === "}") { index += 1; return value; }
      if (text[index] !== ",") throw new SyntaxError("expected object separator");
      index += 1;
    }
  };
  const value = parseValue();
  skipWhitespace();
  if (index !== text.length) throw new SyntaxError("trailing JSON content");
  return value;
}

function exactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  return actual.length === expected.length && actual.every((key, i) => key === expected[i]);
}

function validateIdentity(value, expectedHostRoot) {
  const keys = ["agent", "host_root", "managed", "project_path", "python", "source_root", "user_id"];
  if (!exactKeys(value, keys)) throw identityError("identity must contain exact keys");
  for (const field of ["agent", "user_id", "project_path", "python", "source_root", "host_root"]) {
    if (typeof value[field] !== "string" || !value[field] || value[field] !== value[field].trim() ||
        /[\u0000\r\n]/u.test(value[field])) {
      throw identityError(`${field} must be canonical non-empty text`);
    }
  }
  if (value.agent !== "openclaw") throw identityError("agent must be openclaw");
  if (!exactKeys(value.managed, ["by", "installer", "schema_version"])) {
    throw identityError("managed must contain exact ownership keys");
  }
  if (value.managed.by !== "memplex" || value.managed.installer !== "memplex" ||
      value.managed.schema_version !== 1) {
    throw identityError("managed ownership is invalid");
  }
  for (const field of ["project_path", "python", "source_root", "host_root"]) {
    if (!isAbsolute(value[field])) throw identityError(`${field} must be absolute`);
  }
  try {
    if (!statSync(value.python).isFile()) throw new Error("not a file");
    accessSync(value.python, constants.X_OK);
  } catch {
    throw identityError("recorded Python interpreter is unavailable or not executable");
  }
  for (const field of ["source_root", "host_root"]) {
    try {
      if (!statSync(value[field]).isDirectory()) throw new Error("not a directory");
    } catch {
      throw identityError(`${field} directory is unavailable`);
    }
  }
  let canonicalHostRoot;
  let canonicalExpectedRoot;
  try {
    canonicalHostRoot = realpathSync(value.host_root);
    canonicalExpectedRoot = realpathSync(expectedHostRoot);
  } catch {
    throw identityError("host_root binding cannot be resolved");
  }
  if (value.host_root !== canonicalHostRoot) {
    throw identityError("host_root must be a canonical path");
  }
  const hostStat = statSync(canonicalHostRoot);
  const expectedStat = statSync(canonicalExpectedRoot);
  if (canonicalHostRoot !== canonicalExpectedRoot || hostStat.dev !== expectedStat.dev ||
      hostStat.ino !== expectedStat.ino) {
    throw identityError("host_root does not match the actual installation root");
  }
  return value;
}

function loadIdentity() {
  try {
    const raw = readFileSync(new URL("./memplex-agent.json", import.meta.url), "utf8");
    const expectedHostRoot = realpathSync(resolve(pluginRoot, "../.."));
    return validateIdentity(parseJsonWithoutDuplicateKeys(raw), expectedHostRoot);
  } catch (error) {
    if (String(error).includes("reinstall required")) throw error;
    throw identityError("identity file is missing, unreadable, or invalid JSON");
  }
}

const pluginRoot = realpathSync(fileURLToPath(new URL(".", import.meta.url)));
const identity = loadIdentity();

function effectiveConfig(pluginConfig) {
  return {
    autoRecall: true,
    autoCapture: true,
    topK: 5,
    tokenBudget: 1500,
    timeoutMs: 10000,
    visibility: "workspace",
    ...(pluginConfig || {}),
    // Identity fields come from the installer-managed file.  Keep this
    // assignment after host configuration so an OpenClaw config can tune
    // behavior but cannot move memories into another principal/workspace.
    userId: identity.user_id,
    projectPath: identity.project_path,
    python: identity.python,
    sourceRoot: identity.source_root,
    hostRoot: identity.host_root,
    pluginRoot,
  };
}

function bridgeEnv(config) {
  const pythonPath = [config.sourceRoot, process.env.PYTHONPATH].filter(Boolean).join(delimiter);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    MEMPLEX_USER_ID: config.userId,
    MEMPLEX_PROJECT_ROOT: config.projectPath,
    OPENCLAW_CONFIG_DIR: config.hostRoot,
    MEMPLEX_PLUGIN_ROOT: config.pluginRoot,
  };
}

function callBridge(action, event, context, config) {
  return new Promise((resolve, reject) => {
    const child = spawn(config.python, ["-m", "memplex.adapters.openclaw_plugin", action], {
      env: bridgeEnv(config),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`Memplex ${action} timed out after ${config.timeoutMs}ms`));
    }, config.timeoutMs || 10000);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Memplex ${action} exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout || "{}"));
      } catch (error) {
        reject(new Error(`Memplex ${action} returned invalid JSON: ${String(error)}`));
      }
    });
    child.stdin.end(JSON.stringify({ config, event: event || {}, context: context || {} }));
  });
}

function textResult(result) {
  return {
    content: [{ type: "text", text: JSON.stringify(result) }],
    details: result,
  };
}

export default {
  id: "memplex",
  name: "Memplex",
  description: "Shared long-term memory for OpenClaw",
  kind: "memory",
  register(api) {
    const config = effectiveConfig(api.pluginConfig);
    const log = api.logger || console;

    api.on("before_prompt_build", async (event, context) => {
      if (config.autoRecall === false || !event?.prompt?.trim()) return;
      try {
        const result = await callBridge("recall", event, context, config);
        if (result.prependContext) return { prependContext: result.prependContext };
      } catch (error) {
        log.warn?.(`memplex: recall skipped: ${String(error)}`);
      }
    });

    api.on("agent_end", async (event, context) => {
      if (config.autoCapture === false || event?.success === false) return;
      try {
        await callBridge("capture", event, context, config);
      } catch (error) {
        log.warn?.(`memplex: capture skipped: ${String(error)}`);
      }
    });

    api.on("session_end", async (event, context) => {
      try {
        await callBridge("session-end", event, context, config);
      } catch (error) {
        log.debug?.(`memplex: session cleanup skipped: ${String(error)}`);
      }
    });

    api.registerTool((toolContext) => ({
      name: "memory_recall",
      label: "Memplex Recall",
      description: "Recall shared Memplex memories for the active workspace.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { query: { type: "string", minLength: 1 } },
        required: ["query"],
      },
      async execute(_toolCallId, params) {
        const result = await callBridge(
          "search",
          { query: params.query },
          toolContext || {},
          config,
        );
        return textResult(result);
      },
    }), { name: "memory_recall" });

    api.registerTool((toolContext) => ({
      name: "memory_store",
      label: "Memplex Store",
      description: "Store a durable memory in the active Memplex workspace.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { content: { type: "string", minLength: 1 } },
        required: ["content"],
      },
      async execute(_toolCallId, params) {
        const result = await callBridge(
          "store",
          { content: params.content },
          toolContext || {},
          config,
        );
        return textResult(result);
      },
    }), { name: "memory_store" });
  },
};
"""


def _write_hermes_provider_plugin(
    plugin_dir: Path,
    provider_config: dict[str, Any],
    *,
    install_state: dict[str, Any],
) -> None:
    from memplex.adapters.agent_installer import (  # lazy: avoid circular import
        _managed_identity_payload,
        _package_version,
    )
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: memplex",
                f"version: {_package_version()}",
                'description: "Memplex local long-term memory provider"',
                "hooks:",
                "  - sync_turn",
                "  - on_pre_compress",
                "  - on_session_end",
                "",
            ]
        ),
        encoding="utf-8",
    )
    managed = provider_config.get("managed", {"installer": "memplex"})
    _write_json(
        plugin_dir / "memplex-agent.json",
        _managed_identity_payload(
            agent="hermes",
            user_id=provider_config["user_id"],
            project_path=provider_config["project_path"],
            source_root=provider_config["source_root"],
            host_root=provider_config["host_root"],
            managed=managed,
        ),
    )
    (plugin_dir / "memplex-agent.json").chmod(0o600)
    _write_json(plugin_dir / ".memplex-install-state.json", install_state)
    (plugin_dir / ".memplex-install-state.json").chmod(0o600)
    (plugin_dir / "README.md").write_text(
        "# Memplex Memory Provider\n\n"
        "Hermes Agent 原生 MemoryProvider：共享 Codex、Claude Code 与 OpenClaw "
        "的本地 Memplex 记忆，并在压缩、会话结束和退出前冲刷已接收写入。\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        '''"""Hermes MemoryProvider bootstrap for Memplex."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_EXPECTED_HOST_ROOT = _PLUGIN_DIR.parents[1].resolve(strict=True)


def _identity_error(detail: str) -> ValueError:
    return ValueError(f"Memplex managed identity invalid; reinstall required: {detail}")


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _identity_error(f"duplicate key {key!r}")
        value[key] = item
    return value


def _load_identity():
    try:
        raw = (_PLUGIN_DIR / "memplex-agent.json").read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except ValueError as exc:
        if "reinstall required" in str(exc):
            raise
        raise _identity_error("identity is not valid JSON") from exc
    except (OSError, UnicodeError) as exc:
        raise _identity_error("identity file is missing or unreadable") from exc
    expected = {
        "agent", "user_id", "project_path", "python", "source_root", "host_root", "managed"
    }
    if type(value) is not dict or set(value) != expected:
        raise _identity_error("identity must contain exact keys")
    for field in ("agent", "user_id", "project_path", "python", "source_root", "host_root"):
        item = value[field]
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or any(control in item for control in ("\\x00", "\\n", "\\r"))
        ):
            raise _identity_error(f"{field} must be canonical non-empty text")
    if value["agent"] != "hermes":
        raise _identity_error("agent must be hermes")
    managed = value["managed"]
    if type(managed) is not dict or set(managed) != {"by", "installer", "schema_version"}:
        raise _identity_error("managed must contain exact ownership keys")
    if managed["by"] != "memplex" or managed["installer"] != "memplex":
        raise _identity_error("managed ownership is invalid")
    if type(managed["schema_version"]) is not int or managed["schema_version"] != 1:
        raise _identity_error("managed schema_version must be integer 1")
    for field in ("project_path", "python", "source_root", "host_root"):
        if not Path(value[field]).is_absolute():
            raise _identity_error(f"{field} must be absolute")
    python = Path(value["python"])
    if not python.is_file() or not os.access(python, os.X_OK):
        raise _identity_error("recorded Python interpreter is unavailable or not executable")
    for field in ("source_root", "host_root"):
        if not Path(value[field]).is_dir():
            raise _identity_error(f"{field} directory is unavailable")
    recorded_host_root = Path(value["host_root"])
    try:
        canonical_host_root = recorded_host_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _identity_error("host_root binding cannot be resolved") from exc
    if str(recorded_host_root) != str(canonical_host_root):
        raise _identity_error("host_root must be a canonical path")
    try:
        same_host = os.path.samefile(canonical_host_root, _EXPECTED_HOST_ROOT)
    except OSError as exc:
        raise _identity_error("host_root binding cannot be compared") from exc
    if not same_host:
        raise _identity_error("host_root does not match the actual installation root")
    return value


_IDENTITY = _load_identity()
_SOURCE_ROOT = _IDENTITY["source_root"]
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from memplex.adapters.hermes_memory_provider import MemplexMemoryProvider
from memplex.adapters.hermes_memory_provider import register as _register


def register(ctx) -> None:
    _register(ctx, identity=_IDENTITY)
''',
        encoding="utf-8",
    )
