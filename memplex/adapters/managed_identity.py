"""Strict lifecycle contract for installer-managed agent identities."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

IDENTITY_KEYS = frozenset(
    {
        "agent",
        "user_id",
        "project_path",
        "python",
        "source_root",
        "host_root",
        "managed",
    }
)
MANAGED_KEYS = frozenset({"by", "installer", "schema_version"})
_PLUGIN_LAYOUTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "codex": (("plugins", "marketplaces", "memplex", "plugin"),),
    "claude-code": (("plugins", "marketplaces", "articultur", "plugin"),),
    "openclaw": (("extensions", "memplex"),),
    "hermes": (("plugins", "memplex"),),
}
_VERSIONED_PLUGIN_LAYOUTS: dict[str, tuple[str, ...]] = {
    "codex": ("plugins", "cache", "memplex", "memplex"),
    "claude-code": ("plugins", "cache", "articultur", "memplex"),
}


class ManagedIdentityError(ValueError):
    """The persisted identity cannot safely authorize a managed launcher."""


def _invalid(reason: str) -> ManagedIdentityError:
    return ManagedIdentityError(f"managed identity invalid; reinstall required: {reason}")


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _invalid(f"duplicate key {key!r}")
        value[key] = item
    return value


def derive_managed_host_root(plugin_root: str | Path, *, expected_agent: str) -> Path:
    """Derive the host root from an approved canonical plugin installation layout."""

    try:
        configured_root = Path(plugin_root).expanduser()
        if not configured_root.is_absolute():
            raise _invalid("plugin root must be absolute")
        canonical_plugin_root = configured_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _invalid("plugin root is unavailable") from exc
    if not canonical_plugin_root.is_dir():
        raise _invalid("plugin root is not a directory")

    fixed_layouts = _PLUGIN_LAYOUTS.get(expected_agent, ())
    versioned_prefix = _VERSIONED_PLUGIN_LAYOUTS.get(expected_agent)
    for candidate_root in canonical_plugin_root.parents:
        relative = canonical_plugin_root.relative_to(candidate_root).parts
        if relative in fixed_layouts:
            return candidate_root
        if (
            versioned_prefix is not None
            and len(relative) == len(versioned_prefix) + 1
            and relative[:-1] == versioned_prefix
            and relative[-1]
        ):
            return candidate_root
    raise _invalid(
        f"{expected_agent} plugin root is outside an approved installation layout"
    )


def parse_managed_identity_json(
    raw: str,
    *,
    expected_agent: str,
    expected_host_root: str | Path,
) -> dict[str, Any]:
    """Parse JSON without duplicate-key collapse, then validate the exact schema."""

    try:
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except ManagedIdentityError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise _invalid("identity is not valid JSON") from exc
    return validate_managed_identity(
        value,
        expected_agent=expected_agent,
        expected_host_root=expected_host_root,
    )


def load_managed_identity(
    path: str | Path,
    *,
    expected_agent: str,
    expected_host_root: str | Path,
) -> dict[str, Any]:
    """Load and validate an installed identity, failing closed on every read error."""

    identity_path = Path(path)
    try:
        raw = identity_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _invalid("identity file is missing or unreadable") from exc
    return parse_managed_identity_json(
        raw,
        expected_agent=expected_agent,
        expected_host_root=expected_host_root,
    )


def validate_managed_identity(
    value: Any,
    *,
    expected_agent: str,
    expected_host_root: str | Path,
) -> dict[str, Any]:
    """Validate exact keys, strong types, ownership, and persistent path bindings."""

    if type(value) is not dict:
        raise _invalid("top-level value must be an object")
    keys = set(value)
    if keys != IDENTITY_KEYS:
        missing = sorted(IDENTITY_KEYS - keys)
        extra = sorted(keys - IDENTITY_KEYS)
        detail = []
        if missing:
            detail.append(f"missing keys {missing}")
        if extra:
            detail.append(f"unexpected keys {extra}")
        raise _invalid(", ".join(detail))

    for field in ("agent", "user_id", "project_path", "python", "source_root", "host_root"):
        item = value[field]
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or any(control in item for control in ("\x00", "\n", "\r"))
        ):
            raise _invalid(f"{field} must be canonical non-empty text")
    if value["agent"] != expected_agent:
        raise _invalid(f"agent must be {expected_agent!r}")

    managed = value["managed"]
    if type(managed) is not dict or set(managed) != MANAGED_KEYS:
        raise _invalid("managed must contain exact ownership keys")
    if managed.get("by") != "memplex" or managed.get("installer") != "memplex":
        raise _invalid("managed ownership does not belong to memplex")
    if type(managed.get("schema_version")) is not int or managed["schema_version"] != 1:
        raise _invalid("managed schema_version must be integer 1")

    for field in ("project_path", "python", "source_root", "host_root"):
        if not Path(value[field]).is_absolute():
            raise _invalid(f"{field} must be an absolute path")

    interpreter = Path(value["python"])
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise _invalid("recorded Python interpreter is unavailable or not executable")
    for field in ("source_root", "host_root"):
        if not Path(value[field]).is_dir():
            raise _invalid(f"{field} directory is unavailable")

    recorded_host_root = Path(value["host_root"])
    try:
        canonical_recorded_root = recorded_host_root.resolve(strict=True)
        requested_expected_root = Path(expected_host_root).expanduser()
        if not requested_expected_root.is_absolute():
            raise _invalid("expected host_root must be absolute")
        canonical_expected_root = requested_expected_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _invalid("host_root binding cannot be resolved") from exc
    if str(recorded_host_root) != str(canonical_recorded_root):
        raise _invalid("host_root must be a canonical path")
    try:
        same_host = os.path.samefile(canonical_recorded_root, canonical_expected_root)
    except OSError as exc:
        raise _invalid("host_root binding cannot be compared") from exc
    if not same_host:
        raise _invalid("host_root does not match the actual installation root")

    return dict(value)
