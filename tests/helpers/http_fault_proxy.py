"""确定性的本地 TCP/HTTP 故障代理，仅供可靠性测试使用。"""

from __future__ import annotations

import json
import math
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class FaultAction:
    kind: str
    status_code: int = 200
    delay_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.kind not in {
            "pass",
            "drop_after_commit",
            "delay_after_commit",
            "status",
        }:
            raise ValueError("unknown fault action")
        if type(self.status_code) is not int or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be an exact HTTP status")
        if (
            type(self.delay_seconds) not in {int, float}
            or not math.isfinite(float(self.delay_seconds))
            or self.delay_seconds < 0
        ):
            raise ValueError("delay_seconds must be a non-negative finite number")


class HttpFaultProxy:
    """Run a loopback HTTP endpoint with a deterministic action queue."""

    def __init__(
        self,
        apply_batch: Callable[[bytes], dict[str, object]],
        *,
        get_page: Callable[[dict[str, str]], dict[str, object]] | None = None,
    ) -> None:
        if not callable(apply_batch):
            raise TypeError("apply_batch must be callable")
        self._apply_batch = apply_batch
        if get_page is not None and not callable(get_page):
            raise TypeError("get_page must be callable")
        self._get_page = get_page
        self._actions: deque[FaultAction] = deque()
        self._lock = threading.Lock()
        self.errors: list[str] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
                if self.path != "/sync/v1/batches":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "-1"))
                except ValueError:
                    self.send_error(400)
                    return
                if not 0 <= length <= 4 * 1024 * 1024:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                with owner._lock:
                    action = (
                        owner._actions.popleft()
                        if owner._actions
                        else FaultAction("pass")
                    )
                if action.kind == "status":
                    self._send_json(action.status_code, {"error": "injected"})
                    return
                try:
                    result = owner._apply_batch(body)
                except Exception as exc:  # pragma: no cover - asserted via errors
                    owner.errors.append(type(exc).__name__)
                    self._send_json(500, {"error": "apply_failed"})
                    return
                if action.kind == "drop_after_commit":
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                if action.kind == "delay_after_commit":
                    time.sleep(float(action.delay_seconds))
                self._send_json(200, result)

            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                parsed = urlsplit(self.path)
                if parsed.path != "/sync/v1/changes" or owner._get_page is None:
                    self.send_error(404)
                    return
                query = {
                    key: values[-1]
                    for key, values in parse_qs(
                        parsed.query, keep_blank_values=True
                    ).items()
                }
                try:
                    result = owner._get_page(query)
                except Exception as exc:  # pragma: no cover - asserted via errors
                    owner.errors.append(type(exc).__name__)
                    self._send_json(500, {"error": "page_failed"})
                    return
                self._send_json(200, result)

            def _send_json(self, status: int, value: dict[str, object]) -> None:
                payload = json.dumps(
                    value,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except OSError:
                    return

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="memplex-http-fault-proxy",
            daemon=True,
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def enqueue(self, action: FaultAction) -> None:
        if type(action) is not FaultAction:
            raise TypeError("action must be an exact FaultAction")
        with self._lock:
            self._actions.append(action)

    def start(self) -> HttpFaultProxy:
        self._thread.start()
        return self

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5.0)

    def __enter__(self) -> HttpFaultProxy:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.close()


class UrllibResponse:
    """Small requests-compatible streaming response for dependency-light PG tests."""

    def __init__(self, response: object) -> None:
        self._response = response
        self.status_code = int(getattr(response, "status"))

    def iter_content(self, chunk_size: int):
        while True:
            chunk = self._response.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._response.close()


class UrllibSession:
    """Expose the subset of the requests Session API used by SyncDispatcher."""

    def post(
        self,
        url: str,
        *,
        data: bytes,
        headers: dict[str, str],
        timeout: float,
        stream: bool,
    ) -> UrllibResponse:
        del stream
        request = Request(url, data=data, headers=headers, method="POST")
        try:
            return UrllibResponse(urlopen(request, timeout=timeout))
        except HTTPError as exc:
            return UrllibResponse(exc)

    def get(
        self,
        url: str,
        *,
        params: dict[str, object],
        headers: dict[str, str],
        timeout: float,
        stream: bool,
    ) -> UrllibResponse:
        del stream
        query = urlencode(params)
        request = Request(f"{url}?{query}", headers=headers, method="GET")
        try:
            return UrllibResponse(urlopen(request, timeout=timeout))
        except HTTPError as exc:
            return UrllibResponse(exc)
