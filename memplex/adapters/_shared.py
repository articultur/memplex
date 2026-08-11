"""Shared internal helpers for the adapter layer.

These helpers are intentionally adapter-internal (leading-underscore
module name) and exist to keep ``cli.py``, ``mcp_server.py``, and
``agent_installer.py`` from drifting apart on three concerns that all
three need:

- :func:`dataclass_to_dict` -- recursive dataclass/dict/list serializer
  for JSON-friendly output (``Enum`` leaves -> ``.value``, ``datetime``
  -> isoformat). This is the canonical serializer for every adapter;
  ``http_api`` imports it from here as ``_dataclass_to_dict``.
- :func:`get_plugin_source_dir` -- resolve the bundled ``_plugin/`` dir
  (or the dev-mode ``plugin/`` dir) relative to the memplex package.
- :func:`marketplace_json` -- the Claude Code marketplace descriptor.

Keeping them here means a future edit (e.g. a new bundled asset path) is
a one-line change instead of a three-file hunt.
"""

from __future__ import annotations

from pathlib import Path

# Hard trust-boundary limits shared by every model-facing adapter.  These
# live outside any one host integration so Codex, Claude Code, OpenClaw and
# Hermes cannot drift onto different operational budgets.
MAX_MODEL_SEARCH_RESULTS = 100
MAX_MODEL_TOKEN_BUDGET = 32_000
MAX_MODEL_SEARCH_CANDIDATES = 500
MAX_MODEL_COLLECTION_RESULTS = 1_000
MAX_MODEL_SCAN_ITEMS = 1_000


def dataclass_to_dict(obj):
    """Recursively convert dataclasses to plain JSON-serializable values.

    Handles ``Enum`` (-> .value), ``datetime`` (-> isoformat), ``list``,
    ``dict``, and ``__dataclass_fields__`` containers. Any other leaf is
    returned unchanged. This is the canonical serializer shared by all
    adapters (CLI/MCP/HTTP) so Enum/datetime leaves are never a surprise.
    """
    from datetime import datetime
    from enum import Enum

    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {f: dataclass_to_dict(getattr(obj, f)) for f in obj.__dataclass_fields__}
    if isinstance(obj, (list, tuple)):
        return [dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: dataclass_to_dict(v) for k, v in obj.items()}
    return obj


def get_plugin_source_dir() -> Path:
    """Find the plugin directory bundled with the memplex package.

    Resolution order:
    1. Installed wheel: ``memplex/_plugin/`` (sibling of this file's
       parent's parent, i.e. the package root).
    2. Development checkout: ``<repo>/plugin/`` (one level above the
       package root).

    Raises ``FileNotFoundError`` when neither location has a ``hooks/``
    subdir, which is the marker that distinguishes a real plugin tree
    from a stale empty directory.
    """
    package_dir = Path(__file__).resolve().parent.parent
    bundled = package_dir / "_plugin"
    if bundled.exists() and (bundled / "hooks").exists():
        return bundled
    dev_plugin = package_dir.parent / "plugin"
    if dev_plugin.exists() and (dev_plugin / "hooks").exists():
        return dev_plugin
    raise FileNotFoundError("Cannot find plugin directory in memplex package")


def marketplace_json() -> str:
    """Return the Claude Code marketplace descriptor for Memplex."""
    return """{
  "name": "memplex",
  "interface": {
    "displayName": "Memplex (local)"
  },
  "plugins": [
    {
      "name": "memplex",
      "source": {
        "source": "local",
        "path": "./plugin"
      },
      "policy": {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL"
      },
      "category": "Productivity"
    }
  ]
}
"""
