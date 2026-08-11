"""Extract text and images from PDF files."""

import logging
import os
import tempfile
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PDFExtractor:
    """Extracts text and images from PDF files."""

    def __init__(self):
        self._available = None
        self._pymupdf_available = None

    def is_available(self) -> bool:
        """Check if PDF extraction is available."""
        if self._available is None:
            try:
                import pdfplumber

                self._available = True
            except ImportError:
                self._available = False
        return self._available

    def _is_pymupdf_available(self) -> bool:
        """Check if PyMuPDF is available for image extraction."""
        if self._pymupdf_available is None:
            try:
                import fitz

                self._pymupdf_available = True
            except ImportError:
                self._pymupdf_available = False
        return self._pymupdf_available

    def extract(self, pdf_path: str) -> Optional[List[str]]:
        """Extract text from PDF file. Returns list of page texts."""
        if not self.is_available():
            return None
        if not os.path.exists(pdf_path):
            return None
        try:
            import pdfplumber

            pages = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    text = page.extract_text()
                    if text:
                        pages.append(text)
            return pages if pages else None
        except Exception as e:
            logger.warning("PDF text extraction failed for %s: %s", pdf_path, e)
            return None

    def _extract_images_pymupdf(self, pdf_path: str) -> List[List[Dict[str, Any]]]:
        """Extract images from PDF using PyMuPDF. Returns list of images per page."""
        try:
            import fitz

            images_per_page = []
            doc = fitz.open(pdf_path)
            for page_num, page in enumerate(doc):
                page_images = []
                image_list = page.get_images(full=True)
                for img_index, img in enumerate(image_list):
                    xref = img[0]
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha < 4:
                        img_data = pix.tobytes("png")
                    else:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                        img_data = pix.tobytes("png")
                    temp_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    temp_file.write(img_data)
                    temp_file.close()
                    page_images.append(
                        {
                            "path": temp_file.name,
                            "page": page_num,
                            "index": img_index,
                            "width": pix.width,
                            "height": pix.height,
                        }
                    )
                images_per_page.append(page_images)
            doc.close()
            return images_per_page
        except Exception as e:
            logger.warning("PyMuPDF image extraction failed for %s: %s", pdf_path, e)
            return []

    def _extract_images_pdfplumber(self, pdf_path: str) -> List[List[Dict[str, Any]]]:
        """Extract image metadata from PDF using pdfplumber (no actual bytes)."""
        try:
            import pdfplumber

            images_per_page = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    page_images = []
                    for img in page.images:
                        page_images.append(
                            {
                                "path": None,
                                "page": page.page_number - 1,
                                "index": len(page_images),
                                "width": img.get("width"),
                                "height": img.get("height"),
                                "bbox": (
                                    img.get("x0"),
                                    img.get("y0"),
                                    img.get("x1"),
                                    img.get("y1"),
                                ),
                            }
                        )
                    images_per_page.append(page_images)
            return images_per_page
        except Exception as e:
            logger.warning("pdfplumber image extraction failed for %s: %s", pdf_path, e)
            return []

    def extract_full(self, pdf_path: str) -> Optional[dict]:
        """Extract full content with text, metadata, and images."""
        if not self.is_available():
            return None
        if not os.path.exists(pdf_path):
            return None
        try:
            import pdfplumber

            with pdfplumber.open(pdf_path) as pdf:
                pages_text = [page.extract_text() or "" for page in pdf.pages]

            if self._is_pymupdf_available():
                images = self._extract_images_pymupdf(pdf_path)
            else:
                images = self._extract_images_pdfplumber(pdf_path)

            return {
                "pages": pages_text,
                "metadata": pdf.metadata if hasattr(pdf, "metadata") else {},
                "page_count": len(pages_text),
                "images": images,
            }
        except Exception as e:
            logger.warning("PDF full extraction failed for %s: %s", pdf_path, e)
            return None
