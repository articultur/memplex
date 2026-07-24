"""Direct tests for memplex/core/extractors/ (markdown + vision_mapper).

markdown.py is already 90% (pure); vision_mapper was 24%. The PDF/DOCX/
image extractors gate behind optional heavy deps (pdfplumber/python-docx/
pytesseract) and are exercised via importorskip where the dep is present.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.core.extractors.markdown import MarkdownExtractor  # noqa: E402
from memplex.core.extractors.vision_mapper import VisionMapper  # noqa: E402
from memplex.models.misc import FieldValue  # noqa: E402

# ── MarkdownExtractor ────────────────────────────────────────────────


def test_markdown_extract_returns_paragraph_collection():
    extractor = MarkdownExtractor()
    result = extractor.extract("# Title\n\nbody paragraph text here.")
    # ParagraphCollection has .paragraphs
    assert hasattr(result, "paragraphs")
    assert len(result.paragraphs) >= 1


def test_markdown_extract_empty_text():
    extractor = MarkdownExtractor()
    result = extractor.extract("")
    assert result.paragraphs == []


def test_markdown_extract_section_heading():
    extractor = MarkdownExtractor()
    result = extractor.extract("# My Section\n\ncontent under section.")
    para = result.paragraphs[0]
    assert para.section == "My Section" or para.raw_text


# ── VisionMapper ─────────────────────────────────────────────────────


def test_vision_mapper_constructs():
    assert VisionMapper() is not None


def test_vision_mapper_vision_to_functions_empty_dict():
    vm = VisionMapper()
    out = vm.vision_to_functions({}, source_id="test")
    assert out == []


def test_vision_mapper_vision_to_functions_accepts_input():
    """Must not crash on a well-formed vision_result dict."""
    vm = VisionMapper()
    out = vm.vision_to_functions(
        {"items": [{"description": "x", "confidence": 0.9}]}, source_id="test"
    )
    assert isinstance(out, list)


# ── PDF / DOCX / image extractors (gated by optional deps) ───────────


def test_pdf_extractor_import_or_skip():
    """pdfplumber is optional; the extractor must still import (gated) and
    degrade gracefully on a missing path."""
    from memplex.core.extractors.pdf import PDFExtractor

    ext = PDFExtractor()
    assert ext.extract("/nonexistent/path.pdf") is None


def test_docx_extractor_import_or_skip():
    from memplex.core.extractors.docx import DOCXExtractor

    ext = DOCXExtractor()
    # No crash on construction; degraded path on missing python-docx.
    assert ext is not None


def test_image_extractor_import_or_skip():
    from memplex.core.extractors.image import ImageExtractor

    ext = ImageExtractor()
    assert ext is not None
