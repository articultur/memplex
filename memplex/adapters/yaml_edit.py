"""Small YAML source editor for reversible host configuration changes.

The editor intentionally handles only a two-component mapping path such as
``memory.provider``.  It preserves comments, key order, line endings, file
mode (the caller owns I/O), and unrelated formatting instead of round-tripping
the entire document through a YAML serializer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import yaml

_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_-]*)[ ]*:(?P<rest>.*)$")
_PLAIN_STRING_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-/]*$")
_YAML_KEYWORDS = {
    "false",
    "null",
    "off",
    "on",
    "true",
    "yes",
    "no",
    "~",
}


def set_yaml_scalar_path(text: str, path: Sequence[str], value: Any) -> str:
    """Set a two-level mapping scalar without reserializing the document."""

    parent, leaf = _validate_path(path)
    source = text or ""
    data = _mapping_document(source)
    current_parent = data.get(parent)
    if current_parent is not None and not isinstance(current_parent, dict):
        raise ValueError(f"YAML path component {parent!r} is not a mapping")

    lines = source.splitlines(keepends=True)
    parent_index = _find_top_level_key(lines, parent)
    if parent_index is None:
        if parent in data:
            raise ValueError(
                f"YAML key {parent!r} uses syntax the source-preserving editor cannot modify"
            )
        newline = _preferred_newline(source)
        prefix = source
        if prefix and not prefix.endswith(("\n", "\r")):
            prefix += newline
        return f"{prefix}{parent}:{newline}  {leaf}: {_format_scalar(value)}{newline}"

    match = _line_match(lines[parent_index])
    assert match is not None
    parent_indent = len(match.group("indent"))
    raw_rest = _without_newline(match.group("rest"))
    rest_value, inline_comment = _split_inline_comment(raw_rest)
    if rest_value.strip():
        if not rest_value.lstrip().startswith("{"):
            raise ValueError(f"YAML path component {parent!r} is not a block mapping")
        if not rest_value.rstrip().endswith("}"):
            raise ValueError("Multi-line YAML flow mappings are not supported")
        return _replace_flow_mapping(
            lines,
            parent_index,
            parent,
            leaf,
            value,
            inline_comment,
        )

    block_end = _block_end(lines, parent_index, parent_indent)
    direct_indent = _direct_child_indent(lines, parent_index + 1, block_end, parent_indent)
    leaf_index = _find_direct_child(lines, parent_index + 1, block_end, direct_indent, leaf)
    if leaf_index is not None:
        leaf_match = _line_match(lines[leaf_index])
        assert leaf_match is not None
        leaf_rest = _without_newline(leaf_match.group("rest"))
        _, leaf_comment = _split_inline_comment(leaf_rest)
        newline = _line_newline(lines[leaf_index]) or _preferred_newline(source)
        comment_suffix = f" {leaf_comment}" if leaf_comment else ""
        lines[leaf_index] = (
            f"{leaf_match.group('indent')}{leaf}: {_format_scalar(value)}{comment_suffix}{newline}"
        )
        return "".join(lines)

    newline = _line_newline(lines[parent_index]) or _preferred_newline(source)
    indent = " " * (direct_indent if direct_indent is not None else parent_indent + 2)
    lines.insert(parent_index + 1, f"{indent}{leaf}: {_format_scalar(value)}{newline}")
    return "".join(lines)


def remove_yaml_path(text: str, path: Sequence[str]) -> str:
    """Remove a two-level mapping member while preserving unrelated source."""

    parent, leaf = _validate_path(path)
    if not text:
        return text
    data = _mapping_document(text)
    current_parent = data.get(parent)
    if not isinstance(current_parent, dict) or leaf not in current_parent:
        return text

    lines = text.splitlines(keepends=True)
    parent_index = _find_top_level_key(lines, parent)
    if parent_index is None:
        return text
    match = _line_match(lines[parent_index])
    assert match is not None
    parent_indent = len(match.group("indent"))
    raw_rest = _without_newline(match.group("rest"))
    rest_value, inline_comment = _split_inline_comment(raw_rest)
    if rest_value.strip():
        if not rest_value.lstrip().startswith("{"):
            return text
        if not rest_value.rstrip().endswith("}"):
            raise ValueError("Multi-line YAML flow mappings are not supported")
        mapping = dict(current_parent)
        mapping.pop(leaf, None)
        if not mapping:
            del lines[parent_index]
            return "".join(lines)
        return _replace_flow_mapping(
            lines,
            parent_index,
            parent,
            None,
            mapping,
            inline_comment,
        )

    block_end = _block_end(lines, parent_index, parent_indent)
    direct_indent = _direct_child_indent(lines, parent_index + 1, block_end, parent_indent)
    leaf_index = _find_direct_child(lines, parent_index + 1, block_end, direct_indent, leaf)
    if leaf_index is None:
        return text
    del lines[leaf_index]
    remaining = dict(current_parent)
    remaining.pop(leaf, None)
    if not remaining:
        del lines[parent_index]
    return "".join(lines)


def yaml_path_value(text: str, path: Sequence[str]) -> tuple[bool, Any]:
    """Return ``(present, value)`` for a two-level mapping path."""

    parent, leaf = _validate_path(path)
    data = _mapping_document(text)
    parent_value = data.get(parent)
    if parent_value is None:
        return False, None
    if not isinstance(parent_value, dict):
        raise ValueError(f"YAML path component {parent!r} is not a mapping")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    return leaf in parent_value, parent_value.get(leaf)


def _validate_path(path: Sequence[str]) -> tuple[str, str]:
    if len(path) != 2 or not all(isinstance(item, str) and item for item in path):
        raise ValueError("YAML editor supports exactly one parent and one leaf key")
    return path[0], path[1]


def _mapping_document(text: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML document: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("YAML document root must be a mapping")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    return loaded


def _find_top_level_key(lines: list[str], key: str) -> int | None:
    for index, line in enumerate(lines):
        match = _line_match(line)
        if match and not match.group("indent") and match.group("key") == key:
            return index
    return None


def _line_match(line: str) -> re.Match[str] | None:
    return _KEY_RE.match(_without_newline(line))


def _block_end(lines: list[str], parent_index: int, parent_indent: int) -> int:
    for index in range(parent_index + 1, len(lines)):
        stripped = _without_newline(lines[index])
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent <= parent_indent:
            return index
    return len(lines)


def _direct_child_indent(lines: list[str], start: int, end: int, parent_indent: int) -> int | None:
    indents: list[int] = []
    for line in lines[start:end]:
        stripped = _without_newline(line)
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent > parent_indent:
            indents.append(indent)
    return min(indents) if indents else None


def _find_direct_child(
    lines: list[str], start: int, end: int, direct_indent: int | None, key: str
) -> int | None:
    if direct_indent is None:
        return None
    for index in range(start, end):
        match = _line_match(lines[index])
        if match and len(match.group("indent")) == direct_indent and match.group("key") == key:
            return index
    return None


def _replace_flow_mapping(
    lines: list[str],
    parent_index: int,
    parent: str,
    leaf: str | None,
    value: Any,
    inline_comment: str,
) -> str:
    original = "".join(lines)
    document = _mapping_document(original)
    mapping = dict(document.get(parent) or {}) if leaf is not None else dict(value)
    if leaf is not None:
        mapping[leaf] = value
    dumped = yaml.safe_dump(
        {parent: mapping},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip("\n")
    replacement = dumped.splitlines()
    newline = _line_newline(lines[parent_index]) or _preferred_newline(original)
    if inline_comment:
        replacement[0] = f"{replacement[0]} {inline_comment}"
    lines[parent_index : parent_index + 1] = [f"{line}{newline}" for line in replacement]
    return "".join(lines)


def _format_scalar(value: Any) -> str:
    if isinstance(value, str):
        lowered = value.lower()
        if _PLAIN_STRING_RE.fullmatch(value) and lowered not in _YAML_KEYWORDS:
            return value
        return json.dumps(value, ensure_ascii=False)
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    raise ValueError(f"Unsupported YAML scalar type: {type(value).__name__}")


def _split_inline_comment(value: str) -> tuple[str, str]:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quote == '"' and char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip(), value[index:].rstrip()
    return value.rstrip(), ""


def _without_newline(value: str) -> str:
    return value.rstrip("\r\n")


def _line_newline(value: str) -> str:
    if value.endswith("\r\n"):
        return "\r\n"
    if value.endswith("\n"):
        return "\n"
    return ""


def _preferred_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"
