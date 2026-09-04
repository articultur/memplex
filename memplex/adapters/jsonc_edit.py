"""Small JSONC source editor used for reversible host configuration changes.

The editor intentionally supports only object-path set/remove operations.  It
keeps comments, trailing commas, key order, and unrelated formatting intact;
callers still parse the result with their normal JSONC validator afterwards.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _Member:
    key: str
    key_start: int
    value_start: int
    value_end: int
    comma: int | None


def set_jsonc_path(text: str, path: Sequence[str], value: Any) -> str:
    """Set an object path without reserializing the surrounding document."""

    if not path:
        raise ValueError("JSONC path cannot be empty")
    updated = text or "{}\n"
    while True:
        object_start = _root_object_start(updated)
        missing_parent = False
        for key in path[:-1]:
            member = _find_member(updated, object_start, key)
            if member is None:
                updated = _insert_member(updated, object_start, key, {})
                missing_parent = True
                break
            value_start = _skip_trivia(updated, member.value_start)
            if value_start >= len(updated) or updated[value_start] != "{":
                raise ValueError(f"JSONC path component {key!r} is not an object")
            object_start = value_start
        if not missing_parent:
            break

    key = path[-1]
    member = _find_member(updated, object_start, key)
    if member is None:
        return _insert_member(updated, object_start, key, value)
    indent = _line_indent(updated, member.key_start)
    replacement = _format_value(value, indent)
    return updated[: member.value_start] + replacement + updated[member.value_end :]


def remove_jsonc_path(text: str, path: Sequence[str]) -> str:
    """Remove an object member while preserving the rest of the JSONC source."""

    if not path or not text:
        return text
    object_start = _root_object_start(text)
    for key in path[:-1]:
        member = _find_member(text, object_start, key)
        if member is None:
            return text
        value_start = _skip_trivia(text, member.value_start)
        if value_start >= len(text) or text[value_start] != "{":
            return text
        object_start = value_start

    members = _object_members(text, object_start)
    target_index = next(
        (index for index, member in enumerate(members) if member.key == path[-1]),
        None,
    )
    if target_index is None:
        return text
    target = members[target_index]
    if target.comma is not None:
        end = target.comma + 1
        return text[: target.key_start] + text[end:]
    if target_index > 0 and members[target_index - 1].comma is not None:
        previous_comma = members[target_index - 1].comma
        return text[:previous_comma] + text[target.value_end :]
    return text[: target.key_start] + text[target.value_end :]


def _root_object_start(text: str) -> int:
    start = _skip_trivia(text, 0)
    if start >= len(text) or text[start] != "{":
        raise ValueError("JSONC document root must be an object")
    _matching_delimiter(text, start)
    return start


def _find_member(text: str, object_start: int, key: str) -> _Member | None:
    return next(
        (member for member in _object_members(text, object_start) if member.key == key), None
    )


def _object_members(text: str, object_start: int) -> list[_Member]:
    if text[object_start] != "{":
        raise ValueError("Expected JSONC object")
    object_end = _matching_delimiter(text, object_start)
    members: list[_Member] = []
    cursor = object_start + 1
    while True:
        cursor = _skip_trivia(text, cursor, object_end)
        if cursor >= object_end:
            return members
        key_start = cursor
        if text[cursor] != '"':
            raise ValueError("JSONC object keys must be double-quoted")
        key_end = _scan_string(text, cursor)
        try:
            key = json.loads(text[cursor:key_end])
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSONC object key") from exc
        cursor = _skip_trivia(text, key_end, object_end)
        if cursor >= object_end or text[cursor] != ":":
            raise ValueError(f"Missing ':' after JSONC key {key!r}")
        value_start = _skip_trivia(text, cursor + 1, object_end)
        value_end = _scan_value_end(text, value_start, object_end)
        cursor = _skip_trivia(text, value_end, object_end)
        comma = cursor if cursor < object_end and text[cursor] == "," else None
        members.append(
            _Member(
                key=str(key),
                key_start=key_start,
                value_start=value_start,
                value_end=value_end,
                comma=comma,
            )
        )
        if comma is None:
            cursor = _skip_trivia(text, cursor, object_end)
            if cursor != object_end:
                raise ValueError(f"Missing comma after JSONC key {key!r}")
            return members
        cursor = comma + 1


def _insert_member(text: str, object_start: int, key: str, value: Any) -> str:
    object_end = _matching_delimiter(text, object_start)
    members = _object_members(text, object_start)
    object_indent = _line_indent(text, object_start)
    child_indent = object_indent + "  "
    serialized = _format_value(value, child_indent)
    property_text = f"{child_indent}{json.dumps(key, ensure_ascii=False)}: {serialized}"

    updated = text
    if members and members[-1].comma is None:
        comma_at = members[-1].value_end
        updated = updated[:comma_at] + "," + updated[comma_at:]
        object_end += 1

    before_close = updated[:object_end]
    after_close = updated[object_end:]
    if members:
        separator = "" if before_close.endswith("\n") else "\n"
        insertion = f"{separator}{property_text}\n{object_indent}"
    else:
        inner = updated[object_start + 1 : object_end]
        if inner.strip():
            insertion = f"\n{property_text}\n{object_indent}"
        else:
            before_close = updated[: object_start + 1]
            insertion = f"\n{property_text}\n{object_indent}"
    return before_close + insertion + after_close


def _format_value(value: Any, indent: str) -> str:
    raw = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    lines = raw.splitlines()
    if len(lines) == 1:
        return lines[0]
    return lines[0] + "\n" + "\n".join(indent + line for line in lines[1:])


def _line_indent(text: str, position: int) -> str:
    line_start = text.rfind("\n", 0, position) + 1
    cursor = line_start
    while cursor < position and text[cursor] in " \t":
        cursor += 1
    return text[line_start:cursor]


def _skip_trivia(text: str, cursor: int, limit: int | None = None) -> int:
    end = len(text) if limit is None else min(limit, len(text))
    while cursor < end:
        if text[cursor].isspace():
            cursor += 1
            continue
        if text.startswith("//", cursor):
            newline = text.find("\n", cursor + 2, end)
            cursor = end if newline < 0 else newline + 1
            continue
        if text.startswith("/*", cursor):
            close = text.find("*/", cursor + 2, end)
            if close < 0:
                raise ValueError("Unterminated JSONC block comment")
            cursor = close + 2
            continue
        return cursor
    return cursor


def _scan_string(text: str, start: int) -> int:
    cursor = start + 1
    escaped = False
    while cursor < len(text):
        char = text[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return cursor + 1
        cursor += 1
    raise ValueError("Unterminated JSONC string")


def _matching_delimiter(text: str, start: int) -> int:
    opening = text[start]
    closing = "}" if opening == "{" else "]" if opening == "[" else None
    if closing is None:
        raise ValueError("Expected JSONC object or array")
    stack = [closing]
    cursor = start + 1
    while cursor < len(text):
        if text[cursor] == '"':
            cursor = _scan_string(text, cursor)
            continue
        if text.startswith("//", cursor):
            newline = text.find("\n", cursor + 2)
            cursor = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", cursor):
            close = text.find("*/", cursor + 2)
            if close < 0:
                raise ValueError("Unterminated JSONC block comment")
            cursor = close + 2
            continue
        char = text[cursor]
        if char == "{":
            stack.append("}")
        elif char == "[":
            stack.append("]")
        elif char in "}]":
            if not stack or char != stack[-1]:
                raise ValueError("Mismatched JSONC delimiter")
            stack.pop()
            if not stack:
                return cursor
        cursor += 1
    raise ValueError("Unterminated JSONC container")


def _scan_value_end(text: str, start: int, object_end: int) -> int:
    if start >= object_end:
        raise ValueError("Missing JSONC value")
    if text[start] == '"':
        return _scan_string(text, start)
    if text[start] in "{[":
        return _matching_delimiter(text, start) + 1

    cursor = start
    while cursor < object_end:
        if text.startswith("//", cursor) or text.startswith("/*", cursor):
            break
        if text[cursor] == ",":
            break
        cursor += 1
    end = cursor
    while end > start and text[end - 1].isspace():
        end -= 1
    if end == start:
        raise ValueError("Missing JSONC scalar value")
    return end
