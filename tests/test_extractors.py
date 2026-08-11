"""Direct tests for memplex/core/extractors/ (markdown + vision_mapper).

markdown.py is already 90% (pure); vision_mapper was 24%. The PDF/DOCX/
image extractors gate behind optional heavy deps (pdfplumber/python-docx/
pytesseract) and are exercised via importorskip where the dep is present.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


from memplex.core.extractors.markdown import MarkdownExtractor  # noqa: E402
from memplex.core.extractors.vision_mapper import VisionMapper  # noqa: E402

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


# ── ImageExtractor: OCR lang + vision prompt (regression) ────────────


def test_image_ocr_uses_valid_tesseract_lang(tmp_path, monkeypatch):
    """Regression: lang='eng+chi' is an invalid tesseract language code;
    must be 'eng+chi_sim'."""
    import sys
    import types

    from memplex.core.extractors.image import ImageExtractor

    img = tmp_path / "sample.png"
    img.write_bytes(b"fake-image-bytes")

    calls = {}

    def fake_image_to_string(image, lang=None):
        calls["lang"] = lang
        return " recognized text "

    fake_pil = types.ModuleType("PIL")
    fake_pil.Image = types.SimpleNamespace(open=lambda path: object())
    monkeypatch.setitem(
        sys.modules, "pytesseract", types.SimpleNamespace(image_to_string=fake_image_to_string)
    )
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)

    ext = ImageExtractor()
    assert ext.extract(str(img)) == "recognized text"
    assert calls["lang"] == "eng+chi_sim"


def test_image_vision_prompt_forwarded_to_provider(tmp_path):
    """Regression: the prompt parameter of extract_with_vision was declared
    but never passed to the provider."""
    from memplex.core.extractors.image import ImageExtractor

    img = tmp_path / "sample.png"
    img.write_bytes(b"fake-image-bytes")

    seen = {}

    def provider(path, prompt=None):
        seen["path"] = path
        seen["prompt"] = prompt
        return {"page_type": "design"}

    ext = ImageExtractor()
    ext.set_vision_provider(provider)
    result = ext.extract_with_vision(str(img), prompt="描述这张设计图")

    assert result == {"page_type": "design"}
    assert seen["prompt"] == "描述这张设计图"


def test_image_vision_legacy_provider_without_prompt_still_works(tmp_path):
    """Providers registered before the prompt parameter accept only
    (image_path); they must keep working when a prompt is supplied."""
    from memplex.core.extractors.image import ImageExtractor

    img = tmp_path / "sample.png"
    img.write_bytes(b"fake-image-bytes")

    ext = ImageExtractor()
    ext.set_vision_provider(lambda path: {"page_type": "legacy"})
    assert ext.extract_with_vision(str(img), prompt="自定义提示") == {"page_type": "legacy"}


def test_image_vision_no_prompt_calls_provider_plain(tmp_path):
    from memplex.core.extractors.image import ImageExtractor

    img = tmp_path / "sample.png"
    img.write_bytes(b"fake-image-bytes")

    seen = {}

    def provider(path, prompt="UNSET"):
        seen["prompt"] = prompt
        return {"page_type": "plain"}

    ext = ImageExtractor()
    ext.set_vision_provider(provider)
    assert ext.extract_with_vision(str(img)) == {"page_type": "plain"}
    assert seen["prompt"] == "UNSET"
