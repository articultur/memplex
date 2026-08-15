"""Test memplex/adapters/_shared.py: shared adapter helpers.

Previously these had zero direct coverage (only exercised indirectly via
cli.py/mcp_server.py/agent_installer.py callers). Covers dataclass_to_dict,
get_plugin_source_dir, and marketplace_json.
"""

import os
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


from memplex.adapters._shared import (  # noqa: E402
    dataclass_to_dict,
    get_plugin_source_dir,
    marketplace_json,
)

# ── dataclass_to_dict ────────────────────────────────────────────────


@dataclass
class _Leaf:
    x: int
    y: str


@dataclass
class _Nested:
    leaf: _Leaf
    items: list


def test_dataclass_to_dict_leaf():
    out = dataclass_to_dict(_Leaf(x=1, y="a"))
    assert out == {"x": 1, "y": "a"}


def test_dataclass_to_dict_nested_with_list():
    out = dataclass_to_dict(_Nested(leaf=_Leaf(x=2, y="b"), items=[_Leaf(1, "z"), 9]))
    assert out == {"leaf": {"x": 2, "y": "b"}, "items": [{"x": 1, "y": "z"}, 9]}


def test_dataclass_to_dict_dict_value():
    @dataclass
    class WithDict:
        m: dict

    out = dataclass_to_dict(WithDict(m={"k": _Leaf(1, "v")}))
    assert out == {"m": {"k": {"x": 1, "y": "v"}}}


def test_dataclass_to_dict_passthrough_non_dataclass():
    assert dataclass_to_dict(42) == 42
    assert dataclass_to_dict("s") == "s"
    assert dataclass_to_dict(None) is None


def test_dataclass_to_dict_empty_containers():
    assert dataclass_to_dict([]) == []
    assert dataclass_to_dict({}) == {}


# ── marketplace_json ─────────────────────────────────────────────────


def test_marketplace_json_is_valid_json():
    import json

    data = json.loads(marketplace_json())
    assert data["name"] == "memplex"
    assert data["plugins"][0]["name"] == "memplex"
    assert data["plugins"][0]["source"]["source"] == "local"


def test_marketplace_json_stable_across_calls():
    # The descriptor must be deterministic (it is written to disk as a file).
    assert marketplace_json() == marketplace_json()


# ── get_plugin_source_dir ────────────────────────────────────────────


def test_get_plugin_source_dir_returns_existing_path():
    """In this repo the bundled _plugin/ dir exists, so resolution succeeds."""
    p = get_plugin_source_dir()
    assert isinstance(p, Path)
    assert p.exists()
    assert (p / "hooks").exists()


def test_get_plugin_source_dir_under_package_root():
    """The resolved dir is either memplex/_plugin (installed) or repo/plugin (dev)."""
    p = get_plugin_source_dir()
    assert p.name == "_plugin" or p.name == "plugin"


def test_get_plugin_source_dir_name_attribute():
    p = get_plugin_source_dir()
    assert p.name in {"_plugin", "plugin"}
