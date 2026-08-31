"""Test <private>...</private> redaction on the service write path.

Multi-angle evaluation security #4 finding: the redaction was wired only
in the Claude Code hook runner, so URL/file/clipboard/corpus content
flowing through other adapters was stored verbatim. After this change,
service.write strips <private> blocks at the boundary, covering every
write caller.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models import SourceDocument, SourceType  # noqa: E402
from memplex.privacy import strip_private_tags  # noqa: E402

# ── strip_private_tags unit behaviour ────────────────────────────────


def test_strip_removes_single_block():
    out = strip_private_tags("before <private>SECRET=1</private> after")
    assert "SECRET" not in out
    assert out == "before  after"


def test_strip_removes_multiple_blocks():
    out = strip_private_tags("a <private>x</private> b <private>y</private> c")
    assert "x" not in out and "y" not in out
    assert out == "a  b  c"


def test_strip_handles_multiline_block():
    out = strip_private_tags("intro\n<private>\nline1\nline2\n</private>\nend")
    assert "line1" not in out
    assert "intro" in out and "end" in out


def test_strip_case_insensitive():
    out = strip_private_tags("x <PRIVATE>hidden</PRIVATE> y")
    assert "hidden" not in out


def test_strip_no_tags_returns_unchanged():
    assert strip_private_tags("plain text") == "plain text"


def test_strip_empty_or_none():
    assert strip_private_tags("") == ""
    assert strip_private_tags(None) is None  # type: ignore[arg-type]


def test_strip_unclosed_tag_left_intact():
    """A malformed unclosed tag must not silently drop trailing content."""
    out = strip_private_tags("start <private> never closed rest")
    # Unclosed <private> is treated as literal text -- nothing dropped.
    assert "never closed rest" in out


def test_strip_fast_path_skips_when_no_tag_present():
    """No '<private>' substring -> unchanged (early return)."""
    big = "x" * 10000
    assert strip_private_tags(big) is big


def test_strip_unclosed_tag_logs_warning(caplog):
    """Fail-open keep of an unclosed tag must be observable: behaviour is
    unchanged (nothing dropped) but a warning names the risk."""
    import logging

    with caplog.at_level(logging.WARNING, logger="memplex.privacy"):
        out = strip_private_tags("start <private> never closed rest")
    assert "never closed rest" in out  # fail-open behaviour preserved
    assert any(
        r.levelno >= logging.WARNING and "privacy_unclosed_private_tag" in r.getMessage()
        for r in caplog.records
    )


def test_strip_closed_tags_emit_no_warning(caplog):
    """Fully closed blocks redact silently -- no unclosed-tag warning."""
    import logging

    with caplog.at_level(logging.WARNING, logger="memplex.privacy"):
        out = strip_private_tags("a <private>x</private> b")
    assert out == "a  b"
    assert not any("privacy_unclosed_private_tag" in r.getMessage() for r in caplog.records)


# ── Service write path integration ───────────────────────────────────


@pytest.fixture
def service(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    yield svc
    svc.stop()


def test_service_write_strips_private_block_before_storage(service):
    """The private-tagged secret must never become a searchable memory."""
    body = (
        "public-canary-token: this is safe. "
        "<private>super-secret-canary-token: must not be stored</private>"
    )
    service.write(SourceDocument(type="text", content=body, source_type=SourceType.WIKI))

    # Recall by the secret -- it must not be found.
    secret_result = service.query("super-secret-canary-token", top_k=10)
    assert all("super-secret-canary-token" not in r.summary for r in secret_result.results), (
        "private-tagged secret leaked into stored memory"
    )

    # Recall by the public canary -- it must still be found.
    public_result = service.query("public-canary-token", top_k=10)
    assert any("public-canary-token" in r.summary for r in public_result.results)


def test_service_write_text_strips_private_block(service):
    service.write_text("keep-this-token: visible. <private>hide-this-token: invisible</private>")
    hide = service.query("hide-this-token", top_k=10)
    assert all("hide-this-token" not in r.summary for r in hide.results)
    keep = service.query("keep-this-token", top_k=10)
    assert any("keep-this-token" in r.summary for r in keep.results)


def test_service_write_without_private_tags_unchanged(service):
    """Content with no private tags flows through unaffected."""
    service.write_text("plain-public-token: nothing redacted here.")
    r = service.query("plain-public-token", top_k=5)
    assert any("plain-public-token" in x.summary for x in r.results)
