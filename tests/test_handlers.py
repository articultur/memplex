"""Direct tests for memplex/core/handlers/ (url/file/clipboard).

These are pure-logic handlers with no heavy deps. url_handler was 14%
covered (149 lines); file_handler 25%; clipboard 33%. Covers type
resolution, filename extraction, SSRF host safety, file detection, and
clipboard markdown detection.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.core.handlers.clipboard import ClipboardHandler  # noqa: E402
from memplex.core.handlers.file_handler import FileHandler  # noqa: E402
from memplex.core.handlers.url_handler import URLHandler  # noqa: E402

# ── URLHandler.can_handle / resolve_type ─────────────────────────────


def test_url_can_handle_http():
    assert URLHandler().can_handle("https://example.com/page") is True


def test_url_can_handle_rejects_non_url():
    assert URLHandler().can_handle("not a url") is False
    assert URLHandler().can_handle("/local/path") is False


def test_url_resolve_type_pdf():
    assert URLHandler().resolve_type("https://x.com/doc.pdf") == "pdf"


def test_url_resolve_type_html_default():
    assert URLHandler().resolve_type("https://x.com/page") == "html"


def test_url_get_parser_type_falls_back_to_generic():
    assert URLHandler().get_parser_type("https://unknown.example.com/x") == "generic"


def test_url_extract_filename_from_path():
    assert URLHandler().extract_filename("https://x.com/a/b/report.md") == "report.md"


def test_url_extract_filename_none_for_root():
    assert URLHandler().extract_filename("https://x.com/") is None


# ── URLHandler._is_safe_host (SSRF defence) ──────────────────────────


def test_is_safe_host_rejects_empty():
    assert URLHandler()._is_safe_host("") is False


def test_is_safe_host_rejects_localhost_name():
    """localhost resolves to 127.0.0.1 -> loopback -> rejected."""
    assert URLHandler()._is_safe_host("localhost") is False


def test_is_safe_host_rejects_loopback_ip():
    assert URLHandler()._is_safe_host("127.0.0.1") is False


def test_is_safe_host_rejects_private_ip():
    assert URLHandler()._is_safe_host("192.168.1.1") is False


def test_is_safe_host_rejects_link_local_metadata_ip():
    """169.254.169.254 is the cloud metadata endpoint -> must be blocked."""
    assert URLHandler()._is_safe_host("169.254.169.254") is False


def test_is_safe_host_rejects_unresolvable_name():
    """A name that fails DNS resolution -> rejected (fail closed)."""
    assert URLHandler()._is_safe_host("nonexistent-host-xyz-invalid.invalid") is False


# ── FileHandler ──────────────────────────────────────────────────────


def test_file_can_handle_existing_path(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("x")
    assert FileHandler().can_handle(str(f)) is True


def test_file_can_handle_rejects_url():
    assert FileHandler().can_handle("https://example.com/x") is False


def test_file_read_returns_type_and_content(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("# hello", encoding="utf-8")
    result = FileHandler().read(str(f))
    assert result is not None
    kind, text = result  # FileHandler.read returns (content_type, content)
    assert kind == "markdown"
    assert "hello" in text


def test_file_read_missing_returns_none(tmp_path):
    assert FileHandler().read(str(tmp_path / "nope.md")) is None


def test_file_list_files_non_recursive(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text("c")
    files = FileHandler().list_files(str(tmp_path), recursive=False)
    names = {os.path.basename(f) for f in files}
    assert "a.md" in names and "b.md" in names
    assert "c.md" not in names  # non-recursive skips subdir


def test_file_list_files_recursive(tmp_path):
    (tmp_path / "a.md").write_text("a")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.md").write_text("c")
    files = FileHandler().list_files(str(tmp_path), recursive=True)
    assert any("c.md" in f for f in files)


# ── ClipboardHandler ─────────────────────────────────────────────────


def test_clipboard_parse_returns_segments():
    segments = ClipboardHandler().parse("first line\n\nsecond paragraph")
    assert isinstance(segments, list)
    assert len(segments) >= 1


def test_clipboard_parse_empty_string():
    segments = ClipboardHandler().parse("")
    assert segments == []


def test_clipboard_is_markdown_detects_heading():
    assert ClipboardHandler()._is_markdown("# heading\n\nbody") is True


def test_clipboard_is_markdown_plain_text():
    assert ClipboardHandler()._is_markdown("just plain text here") is False
