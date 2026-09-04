"""Handle remote URL input."""

import logging
import os
import re
import tempfile
from typing import Any, ClassVar
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class URLHandler:
    """Handles remote URL input and type resolution."""

    URL_TYPE_PATTERNS: ClassVar[dict[str, list[str]]] = {
        "pdf": [r"\.pdf$", r"/[^/]+\.pdf", r"\?.*\.pdf"],
        "markdown": [r"\.md$", r"\.markdown$", r"\.mdown$"],
        "html": [r"\.html?$", r"\.htm$"],
        "image": [r"\.(png|jpg|jpeg|gif|bmp|webp)$"],
        "docx": [r"\.docx$"],
    }

    DOMAIN_PARSERS: ClassVar[dict[str, str]] = {
        "github.com": "github",
        "gist.github.com": "gist",
        "confluence": "confluence",
        "notion.so": "notion",
        "notion.site": "notion",
    }

    # SSRF / resource guards.
    MAX_REDIRECTS = 3
    MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB

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

    def extract_filename(self, url: str) -> str | None:
        """Extract filename from URL path."""
        parsed = urlparse(url)
        path = parsed.path

        if "/" in path:
            filename = path.rsplit("/", 1)[-1]
            if filename:
                return filename

        return None

    def _resolve_safe_ip(self, hostname: str) -> str | None:
        """Resolve a hostname and return the first safe IP, or ``None``.

        This is the single-resolution point for the SSRF guard: the
        returned IP is pinned into the connection URL so a DNS rebinding
        attacker cannot present a safe address during the check and a
        loopback/metadata address during the actual connection.
        """
        import ipaddress
        import socket

        if not hostname:
            return None
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return None
        if not infos:
            return None
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError):
                return None
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
                return None
        return str(infos[0][4][0])

    def _is_safe_host(self, hostname: str) -> bool:
        """
        Return True only if hostname resolves exclusively to public IPs.

        Resolves every address returned by the resolver and rejects the host if
        any is loopback, private, link-local (this covers 169.254.169.254 and
        other cloud metadata endpoints), or unspecified. Bare IPs are resolved
        to themselves by ``getaddrinfo`` and judged directly. Resolution failure
        or an unparseable address also yields False.
        """
        import ipaddress
        import socket

        if not hostname:
            return False

        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror:
            return False

        if not infos:
            return False

        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except (ValueError, IndexError):
                return False
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified:
                return False

        return True

    @staticmethod
    def _read_limited(response: Any, limit: int) -> bytes | None:
        """Read at most ``limit`` bytes; return None if the body exceeds it."""
        total = 0
        chunks = []
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _build_opener() -> Any:
        """Build an opener that does NOT auto-follow redirects."""
        import urllib.request

        opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler(),
            urllib.request.UnknownHandler(),
            urllib.request.HTTPHandler(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPSHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            opener.add_handler(handler)
        return opener

    def fetch(self, url: str) -> tuple[str, str] | None:
        """
        Fetch content from URL with SSRF protection.

        Each request (including every redirect hop) is checked against
        :meth:`_is_safe_host` before connecting. Redirects are followed
        manually up to :attr:`MAX_REDIRECTS` times, and responses larger than
        :attr:`MAX_RESPONSE_BYTES` are aborted.

        Returns:
            (content_type, content_or_path) or None if fetch failed
        """
        import urllib.error
        import urllib.request
        from urllib.parse import urljoin

        opener = self._build_opener()
        current_url = url

        try:
            for _ in range(self.MAX_REDIRECTS + 1):
                parsed = urlparse(current_url)
                if parsed.scheme not in ("http", "https"):
                    logger.warning("Refusing non-http(s) URL: %s", current_url)
                    return None
                safe_ip = self._resolve_safe_ip(parsed.hostname or "")
                if safe_ip is None:
                    logger.warning("Refusing unsafe host: %s", parsed.hostname)
                    return None

                # Pin the resolved IP to eliminate the DNS rebinding TOCTOU
                # window between the safety check and the actual connection.
                # For HTTP the Host header preserves the original netloc.
                # For HTTPS, hostname verification uses the original URL's
                # hostname; only the TCP destination is pinned.
                if parsed.scheme == "http":
                    pinned = current_url.replace(parsed.hostname or "", safe_ip, 1)
                    host_header = parsed.netloc
                else:
                    pinned = current_url  # HTTPS: pin via custom handler below
                    host_header = None

                req = urllib.request.Request(
                    pinned,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; Content-Extractor/1.0)"},
                )
                if host_header:
                    req.add_header("Host", host_header)
                try:
                    response = opener.open(req, timeout=30)
                    status = response.getcode()
                    headers = response.headers
                except urllib.error.HTTPError as e:
                    # With no redirect handler, 3xx/4xx/5xx surface as HTTPError.
                    status = e.code
                    headers = e.headers
                    response = e

                if status in (301, 302, 303, 307, 308):
                    location = headers.get("Location")
                    response.close()
                    if not location:
                        logger.warning("Redirect without Location: %s", current_url)
                        return None
                    current_url = urljoin(current_url, location)
                    continue

                if status >= 400:
                    response.close()
                    logger.warning("HTTP %d fetching URL %s", status, current_url)
                    return None

                content_type = headers.get("Content-Type", "").lower()
                try:
                    data = self._read_limited(response, self.MAX_RESPONSE_BYTES)
                finally:
                    response.close()
                if data is None:
                    logger.warning(
                        "Response exceeds %d bytes: %s", self.MAX_RESPONSE_BYTES, current_url
                    )
                    return None

                if "text" in content_type or "markdown" in content_type:
                    text = data.decode("utf-8", errors="replace")
                    resolved_type = self.resolve_type(current_url)
                    return (resolved_type if resolved_type != "html" else "text", text)

                if "image" in content_type or self.resolve_type(current_url) == "image":
                    ext = (
                        os.path.splitext(self.extract_filename(current_url) or "image.png")[1]
                        or ".png"
                    )
                    temp_file = tempfile.NamedTemporaryFile(suffix=ext, delete=False)  # noqa: SIM115 - delete=False temp file must outlive the block
                    temp_file.write(data)
                    temp_file.close()
                    return ("image", temp_file.name)

                if "pdf" in content_type or self.resolve_type(current_url) == "pdf":
                    temp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)  # noqa: SIM115 - delete=False temp file must outlive the block
                    temp_file.write(data)
                    temp_file.close()
                    return ("pdf", temp_file.name)

                text = data.decode("utf-8", errors="replace")
                return ("html", text)

            logger.warning("Too many redirects for URL %s", url)
            return None

        except Exception as e:  # noqa: BLE001 - logged degradation path
            logger.warning("Failed to fetch URL %s: %s", url, e)
            return None

    def cleanup_temp_file(self, path: str) -> bool:
        """Delete a temp file if it exists."""
        try:
            if path and os.path.exists(path):
                os.unlink(path)
                return True
        except Exception:
            logger.debug("temp file cleanup failed for %s", path, exc_info=True)
        return False
