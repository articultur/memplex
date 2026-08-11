"""Real-process G006 probe, SIGTERM, and signed report gate."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from memplex.operations import load_operations_report


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(url: str, *, api_key: str | None = None) -> tuple[int, bytes]:
    headers = {} if api_key is None else {"X-API-Key": api_key}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=2) as response:
            return int(response.status), response.read()
    except HTTPError as exc:
        return int(exc.code), exc.read()


def _wait_ready(base: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"uvicorn exited before readiness: {process.returncode}")
        try:
            status, _body = _request(base + "/health/ready")
            if status == 200:
                return
        except (OSError, URLError):
            pass
        time.sleep(0.05)
    raise AssertionError("uvicorn readiness timeout")


def test_real_uvicorn_sigterm_writes_verified_drain_report(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn")
    port = _free_port()
    report_path = tmp_path / "operations-report.json"
    key = b"o" * 32
    env = os.environ.copy()
    env.update(
        {
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
            "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
            "MEMPLEX_API_KEY": "process-api-key",
            "MEMPLEX_OPERATIONS_HMAC_KEY": base64.b64encode(key).decode("ascii"),
            "MEMPLEX_G006_REPORT_OUTPUT": str(report_path),
            "MEMPLEX_OPERATIONS_REPORT_KEY_ID": "process-ops-key",
        }
    )
    code = (
        "from memplex.adapters.http_api import create_app; import uvicorn; "
        f"uvicorn.run(create_app(),host='127.0.0.1',port={port},"
        "log_level='warning',access_log=False)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_ready(base, process)
        assert _request(base + "/health/live")[0] == 200
        assert _request(base + "/stats")[0] == 401
        assert _request(base + "/stats", api_key="process-api-key")[0] == 200
        process.send_signal(signal.SIGTERM)
        # Uvicorn 0.52 restores and re-raises the captured signal after its
        # graceful lifespan shutdown; both return conventions prove cleanup.
        assert process.wait(timeout=20) in {0, -signal.SIGTERM}
        report = load_operations_report(report_path)
        report.verify(key)
        assert report.request_count == 2  # unauthorized + authorized business requests
        assert report.successful_requests == 2  # 4xx is not server unavailability
        assert report.shutdown_drained is True
        assert report.shutdown_deadline_exceeded is False
        assert report.industrial_gate_closing is True
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_real_uvicorn_sigterm_drains_an_admitted_request(tmp_path: Path) -> None:
    pytest.importorskip("uvicorn")
    port = _free_port()
    report_path = tmp_path / "active-report.json"
    entered_path = tmp_path / "active-request-entered"
    key = b"o" * 32
    env = os.environ.copy()
    env.update(
        {
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(tmp_path / "active-store"),
            "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
            "MEMPLEX_API_KEY": "active-process-key",
            "MEMPLEX_OPERATIONS_HMAC_KEY": base64.b64encode(key).decode("ascii"),
            "MEMPLEX_G006_REPORT_OUTPUT": str(report_path),
            "MEMPLEX_OPERATIONS_REPORT_KEY_ID": "active-ops-key",
            "MEMPLEX_OPERATIONS_P95_LATENCY_TARGET_MS": "1000.0",
        }
    )
    code = (
        "import asyncio; from pathlib import Path; "
        "from memplex.adapters.http_api import create_app; import uvicorn; "
        "app=create_app()\n"
        "@app.get('/_operations_test_slow')\n"
        "async def slow():\n"
        f" Path({str(entered_path)!r}).write_text('entered',encoding='utf-8')\n"
        " await asyncio.sleep(0.5)\n"
        " return {'status':'finished'}\n"
        f"uvicorn.run(app,host='127.0.0.1',port={port},"
        "log_level='warning',access_log=False)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        _wait_ready(base, process)
        result: dict[str, object] = {}

        def request() -> None:
            result["response"] = _request(
                base + "/_operations_test_slow", api_key="active-process-key"
            )

        thread = threading.Thread(target=request)
        thread.start()
        deadline = time.monotonic() + 5
        while not entered_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered_path.exists()
        process.send_signal(signal.SIGTERM)
        thread.join(timeout=5)
        assert not thread.is_alive()
        status, body = result["response"]
        assert status == 200
        assert json.loads(body) == {"status": "finished"}
        assert process.wait(timeout=20) in {0, -signal.SIGTERM}
        report = load_operations_report(report_path)
        report.verify(key)
        assert report.request_count == 1
        assert report.successful_requests == 1
        assert report.shutdown_drained is True
        assert report.shutdown_deadline_exceeded is False
        assert report.industrial_gate_closing is True
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
