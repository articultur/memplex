"""Handle remote URL input."""

import os
import re
import tempfile
from typing import Optional, Tuple
from urllib.parse import urlparse


class URLHandler:
    """Handles remote URL input and type resolution."""

    URL_TYPE_PATTERNS = {
        "pdf": [r"\.pdf$", r"/[^/]+\.pdf", r"\?.*\.pdf"],
        "markdown": [r"\.md$", r"\.markdown$", r"\.mdown$"],
        "html": [r"\.html?$", r"\.htm$"],
        "image": [r"\.(png|jpg|jpeg|gif|bmp|webp)$"],
        "docx": [r"\.docx$"],
    }

    DOMAIN_PARSERS = {
        "github.com": "github",
        "gist.github.com": "gist",
        "confluence": "confluence",
        "notion.so": "notion",
        "notion.site": "notion",
    }

    def can_handle(self, path: str) -> bool:
        """Check if input is a URL."""
        if not path:
            return False

        url_pattern = re.compile(
            r"^https?://"
            r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"
            r"localhost|"
            r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
            r"(?::\d+)?"
            r"(?:/?|[/?]\S+)$",
            re.IGNORECASE,
        )

        return bool(url_pattern.match(path))

    def resolve_type(self, url: str) -> str:
        """Resolve URL to content type based on extension."""
        url_lower = url.lower()

        for content_type, patterns in self.URL_TYPE_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, url_lower):
                    return content_type

        return "html"

    def get_parser_type(self, url: str) -> str:
        """Get the appropriate parser type for URL."""
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        for key, parser in self.DOMAIN_PARSERS.items():
            if key in domain:
                return parser

        return "generic"

    def extract_filename(self, url: str) -> Optional[str]:
        """Extract filename from URL path."""
        parsed = urlparse(url)
        path = parsed.path

        if "/" in path:
            filename = path.rsplit("/", 1)[-1]
            if filename:
                return filename

        return None

    def fetch(self, url: str) -> Optional[Tuple[str, str]]:
        """
        Fetch content from URL.

        Returns:
            (content_type, content_or_path) or None if fetch failed
        """
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Content-Extractor/1.0)"
                },
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                data = response.read()

                if "text" in content_type or "markdown" in content_type:
                    text = data.decode("utf-8", errors="replace")
                    resolved_type = self.resolve_type(url)
                    return (resolved_type if resolved_type != "html" else "text", text)

                if "image" in content_type or self.resolve_type(url) == "image":
                    ext = (
                        os.path.splitext(self.extract_filename(url) or "image.png")[1]
                        or ".png"
                    )
                    temp_file = tempfile.NamedTemporaryFile(suffix=ext, delete=False)
                    temp_file.write(data)
                    temp_file.close()
                    return ("image", temp_file.name)

                if "pdf" in content_type or self.resolve_type(url) == "pdf":
                    temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                    temp_file.write(data)
                    temp_file.close()
                    return ("pdf", temp_file.name)

                text = data.decode("utf-8", errors="replace")
                return ("html", text)

        except Exception as e:
            print(f"Failed to fetch URL {url}: {e}")
            return None

    def cleanup_temp_file(self, path: str) -> bool:
        """Delete a temp file if it exists."""
        try:
            if path and os.path.exists(path):
                os.unlink(path)
                return True
        except Exception:
            pass
        return False
