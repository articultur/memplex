"""Handle local file input."""

import os
from pathlib import Path
from typing import List, Optional, Tuple


class FileHandler:
    """Handles local file reading."""

    SUPPORTED_EXTENSIONS = {
        ".md",
        ".markdown",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".pdf",
        ".docx",
    }

    def can_handle(self, path: str) -> bool:
        """Check if file is supported."""
        ext = Path(path).suffix.lower()
        return ext in self.SUPPORTED_EXTENSIONS

    def read(self, path: str) -> Optional[Tuple[str, str]]:
        """
        Read file content.

        Returns:
            (content_type, content) or None if unsupported
        """
        if not os.path.exists(path):
            return None

        ext = Path(path).suffix.lower()

        if ext in (".md", ".markdown", ".txt"):
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            return ("markdown" if ext in (".md", ".markdown") else "text", content)

        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
            return ("image", path)

        if ext == ".pdf":
            return ("pdf", path)

        if ext == ".docx":
            return ("docx", path)

        return None

    def list_files(self, directory: str, recursive: bool = False) -> List[str]:
        """List supported files in directory."""
        files = []
        path = Path(directory)

        if recursive:
            for ext in self.SUPPORTED_EXTENSIONS:
                files.extend([str(p) for p in path.rglob(f"*{ext}")])
        else:
            for ext in self.SUPPORTED_EXTENSIONS:
                files.extend([str(p) for p in path.glob(f"*{ext}")])

        return files
