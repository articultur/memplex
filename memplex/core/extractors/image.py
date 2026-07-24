"""Extract text from images using OCR or external vision providers."""

import os
from typing import Callable, Optional


class ImageExtractor:
    """
    Extracts text and visual information from images.

    Supports two modes:
    1. Internal OCR: uses pytesseract (if available)
    2. External providers: registered via set_*_provider() methods

    Priority: External provider > Internal OCR
    """

    def __init__(self):
        self._ocr_available = None
        self._external_ocr: Optional[Callable] = None
        self._external_vision: Optional[Callable] = None
        self._vision_timeout = 60  # seconds
        self._vision_max_retries = 2

    def set_ocr_provider(self, fn: Callable[[str], Optional[str]]) -> None:
        """
        Register an external OCR provider.

        Args:
            fn: Callable that takes (image_path) returns OCR text or None.
                Example: lambda path: subprocess.run(['tesseract', path, 'stdout'])
        """
        self._external_ocr = fn

    def set_vision_provider(self, fn: Callable[[str], Optional[dict]]) -> None:
        """
        Register an external vision/LLM provider.

        Args:
            fn: Callable that takes (image_path) returns vision dict or None.
                The dict should have keys: page_type, components[], layout, design_tools, design_system
        """
        self._external_vision = fn

    @property
    def has_ocr(self) -> bool:
        """Check if any OCR is available (external or internal)."""
        if self._external_ocr is not None:
            return True
        if self._ocr_available is None:
            try:
                import pytesseract
                from PIL import Image

                self._ocr_available = True
            except ImportError:
                self._ocr_available = False
        return self._ocr_available

    @property
    def has_vision(self) -> bool:
        """Check if vision capability is available (external or internal)."""
        return self._external_vision is not None

    def extract(self, image_path: str) -> Optional[str]:
        """
        Extract text from image using OCR.

        Priority: External OCR provider > Internal pytesseract.

        Args:
            image_path: Path to image file

        Returns:
            Extracted text or None if OCR unavailable
        """
        if not os.path.exists(image_path):
            return None

        # 1. External OCR provider (priority)
        if self._external_ocr is not None:
            try:
                result = self._external_ocr(image_path)
                if result:
                    return result.strip() if isinstance(result, str) else result
            except Exception as e:
                print(f"External OCR failed: {e}")

        # 2. Internal pytesseract
        try:
            import pytesseract
            from PIL import Image

            image = Image.open(image_path)
            text = pytesseract.image_to_string(image, lang="eng+chi")
            return text.strip()
        except Exception as e:
            print(f"OCR failed for {image_path}: {e}")
            return None

    def extract_with_vision(self, image_path: str, prompt: str = None) -> Optional[dict]:
        """
        Extract visual understanding using vision model.

        Priority: External vision provider (MCP/LLM) > None.
        Applies timeout and retry with exponential backoff.

        Args:
            image_path: Path to image file
            prompt: Custom prompt for vision analysis

        Returns:
            dict with vision analysis or None
        """
        import time

        if not os.path.exists(image_path):
            return None

        # 1. External Vision provider (MCP/LLM)
        if self._external_vision is not None:
            last_error = None
            for attempt in range(self._vision_max_retries):
                try:
                    result = self._external_vision(image_path)
                    if result:
                        return result
                except TimeoutError:
                    last_error = f"Timeout after {self._vision_timeout}s (attempt {attempt + 1}/{self._vision_max_retries})"
                    print(f"External vision timeout: {last_error}")
                except Exception as e:
                    last_error = f"{type(e).__name__}: {e} (attempt {attempt + 1}/{self._vision_max_retries})"
                    print(f"External vision failed: {last_error}")

                if attempt < self._vision_max_retries - 1:
                    wait_time = 2**attempt  # exponential backoff: 1s, 2s
                    time.sleep(wait_time)

            if last_error:
                print(f"Vision exhausted after {self._vision_max_retries} attempts")

        return None

    def extract_full(self, image_path: str, vision_result: dict = None) -> dict:
        """
        Extract both OCR text and optionally pre-extracted vision understanding.

        Args:
            image_path: Path to image file
            vision_result: Pre-extracted vision analysis (from LLM MCP).
                          If provided, skips internal vision extraction.

        Returns:
            dict with:
            - ocr_text: raw text from OCR
            - has_ocr: whether OCR succeeded
            - vision: structured vision analysis
            - has_vision: whether vision was provided or extracted
            - combined_text: OCR + Vision text for paragraph extraction
        """
        result = {
            "ref": image_path,
            "type": "image",
            "ocr_text": None,
            "has_ocr": False,
            "vision": vision_result,
            "has_vision": vision_result is not None,
            "combined_text": "",
        }

        # 1. OCR extraction
        ocr_text = self.extract(image_path)
        if ocr_text:
            result["ocr_text"] = ocr_text
            result["has_ocr"] = True
            result["combined_text"] = ocr_text

        # 2. Vision result (may be pre-extracted via MCP)
        if vision_result:
            result["vision"] = vision_result
            result["has_vision"] = True
            vision_text = self._vision_to_text(vision_result)
            if result["combined_text"]:
                result["combined_text"] += "\n" + vision_text
            else:
                result["combined_text"] = vision_text
        else:
            internal_vision = self.extract_with_vision(image_path)
            if internal_vision:
                result["vision"] = internal_vision
                result["has_vision"] = True
                vision_text = self._vision_to_text(internal_vision)
                if result["combined_text"]:
                    result["combined_text"] += "\n" + vision_text
                else:
                    result["combined_text"] = vision_text

        return result

    def _vision_to_text(self, vision: dict) -> str:
        """Convert vision result to readable text."""
        lines = []

        if vision.get("page_type"):
            lines.append(f"页面类型: {vision['page_type']}")

        if vision.get("design_tools"):
            lines.append(f"设计工具: {', '.join(vision['design_tools'])}")

        if vision.get("design_system"):
            lines.append(f"设计系统: {vision['design_system']}")

        if vision.get("layout"):
            lines.append(f"布局结构: {vision['layout']}")

        if vision.get("components"):
            lines.append("组件列表:")
            for comp in vision["components"]:
                data_str = f" (数据: {comp['data']})" if comp.get("data") else ""
                lines.append(
                    f"  - [{comp['type']}] {comp['label']}: {comp.get('function', '')}{data_str}"
                )

        return "\n".join(lines)

    def extract_with_metadata(self, image_path: str) -> dict:
        """Extract text and image metadata (legacy method)."""
        full_result = self.extract_full(image_path)
        return {
            "ref": image_path,
            "type": "image",
            "ocr_text": full_result["ocr_text"] or "",
            "has_text": full_result["has_ocr"],
            "vision": full_result["vision"],
            "needs_vision_model": not full_result["has_vision"],
            "visual_note": f"OCR: {len(full_result.get('ocr_text') or '')} chars, Vision: {'yes' if full_result['has_vision'] else 'no'}",
        }
