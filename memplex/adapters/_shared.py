"""Shared internal helpers for the adapter layer.

These helpers are intentionally adapter-internal (leading-underscore
module name) and exist to keep ``cli.py``, ``mcp_server.py``, and
``agent_installer.py`` from drifting apart on three concerns that all
three need:

- :func:`dataclass_to_dict` -- recursive dataclass/dict/list serializer
  for JSON-friendly output (Enum/datetime leaves are NOT handled here;
  callers that need those use the dedicated serializer in
  ``http_api._dataclass_to_dict``).
- :func:`get_plugin_source_dir` -- resolve the bundled ``_plugin/`` dir
  (or the dev-mode ``plugin/`` dir) relative to the memplex package.
- :func:`marketplace_json` -- the Claude Code marketplace descriptor.

Keeping them here means a future edit (e.g. a new bundled asset path) is
a one-line change instead of a three-file hunt.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path


def dataclass_to_dict(obj):
    """Recursively convert dataclasses to plain dicts.

    Walks ``__dataclass_fields__``, ``list``, and ``dict`` containers;
    any other leaf is returned unchanged.
    """
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
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
