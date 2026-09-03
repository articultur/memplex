"""Real subprocess helpers shared by the G004 end-to-end tests."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

_REDACTED = "<redacted>"
_SENSITIVE_PARTS = frozenset(
    {
        "authorization",
        "credential",
        "key",
        "password",
        "passwd",
        "pwd",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_SENSITIVE_COMPOUNDS = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "bearertoken",
        "clientsecret",
        "hmackey",
        "idtoken",
        "oauthcode",
        "privatekey",
        "refreshtoken",
        "secretkey",
        "sessiontoken",
        "signingkey",
        "sslpassword",
    }
)
_DSN_NAMES = frozenset(
    {
        "application_name",
        "channel_binding",
        "connect_timeout",
        "dbname",
        "host",
        "hostaddr",
        "options",
        "passfile",
        "port",
        "service",
        "sslcert",
        "sslkey",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "user",
    }
)
_URI_PREFIX = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_ASSIGNMENT_NAME = re.compile(r"(?:^|[?&#;\s])([A-Za-z][A-Za-z0-9_.-]*)\s*=")


def _is_sensitive_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    parts = normalized.split("-") if normalized else []
    compact = "".join(parts)
    return bool(_SENSITIVE_PARTS & set(parts)) or compact in _SENSITIVE_COMPOUNDS


def _contains_sensitive_assignment(value: str) -> bool:
    return any(
        name.lower() == "user" or _is_sensitive_name(name)
        for name in _ASSIGNMENT_NAME.findall(value)
    )


def _looks_like_dsn(value: str) -> bool:
    names = _ASSIGNMENT_NAME.findall(value)
    return any(
        name.lower() in _DSN_NAMES or _is_sensitive_name(name) for name in names
    )


def _sanitize_parameter_component(value: str) -> str:
    if not value:
        return value
    try:
        items = parse_qsl(value, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return _REDACTED if _contains_sensitive_assignment(value) else value
    return urlencode(
        [
            (name, _REDACTED if _is_sensitive_name(name) else item)
            for name, item in items
        ]
    )


def _sanitize_uri(value: str) -> str:
    if not _URI_PREFIX.match(value):
        return value
    try:
        parsed = urlsplit(value)
        _ = parsed.hostname
        _ = parsed.port
    except ValueError:
        return _REDACTED
    if not parsed.scheme:
        return _REDACTED

    netloc = parsed.netloc
    if "@" in netloc:
        _, host = netloc.rsplit("@", 1)
        netloc = f"{_REDACTED}@{host}"
    query = _sanitize_parameter_component(parsed.query)
    fragment = _sanitize_parameter_component(parsed.fragment)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def _sanitize_assignments(value: str) -> str:
    if not _looks_like_dsn(value):
        return value
    try:
        parts = shlex.split(value)
    except ValueError:
        return _REDACTED
    if not parts or not all("=" in part for part in parts):
        return _REDACTED

    sanitized: list[str] = []
    for part in parts:
        name, item = part.split("=", 1)
        if name.lower() == "user" or _is_sensitive_name(name):
            item = _REDACTED
        sanitized.append(f"{name}={item}")
    return " ".join(sanitized)


def _sanitize_argument(value: str) -> str:
    if _URI_PREFIX.match(value):
        return _sanitize_uri(value)
    return _sanitize_assignments(value)


def sanitize_argv(args: object) -> object:
    """Return a credential-safe argv rendering without mutating the source."""
    if isinstance(args, str):
        try:
            values = shlex.split(args)
        except ValueError:
            return _sanitize_argument(args)
        return " ".join(shlex.quote(item) for item in sanitize_argv(values))
    if not isinstance(args, Sequence):
        return _sanitize_argument(str(args))

    sanitized: list[str] = []
    redact_next = False
    for raw_value in args:
        value = str(raw_value)
        if redact_next:
            sanitized.append(_REDACTED)
            redact_next = False
            continue
        if value.startswith("-"):
            flag, separator, assigned = value.partition("=")
            if _is_sensitive_name(flag):
                if separator:
                    sanitized.append(f"{flag}={_REDACTED}")
                else:
                    sanitized.append(flag)
                    redact_next = True
                continue
            if separator:
                sanitized.append(f"{flag}={_sanitize_argument(assigned)}")
                continue
        sanitized.append(_sanitize_argument(value))
    return sanitized


def require_executables(names: Sequence[str]) -> dict[str, str]:
    """Resolve required executables or fail before an external test starts."""
    resolved = {name: shutil.which(name) for name in names}
    missing = [name for name, path in resolved.items() if path is None]
    if missing:
        raise AssertionError(f"required executables unavailable: {', '.join(missing)}")
    return {name: path for name, path in resolved.items() if path is not None}


def run_cli(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float | None = 30,
    local: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one CLI argv sequence in a real subprocess."""
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    if local and not process_env.get("MEMPLEX_STORAGE_PATH"):
        raise ValueError("MEMPLEX_STORAGE_PATH is required for local CLI runs")

    return subprocess.run(
        args,
        input=stdin,
        capture_output=True,
        text=True,
        env=process_env,
        timeout=timeout,
        check=False,
    )


def _format_process_diagnostic(
    args: object,
    status: int | None,
    stdout: str | None,
    stderr: str | None,
) -> str:
    return (
        f"args: {sanitize_argv(args)!r}\n"
        f"status: {status}\n"
        f"stdout_chars: {len(stdout or '')}\n"
        f"stderr_chars: {len(stderr or '')}"
    )


def process_diagnostic(completed: subprocess.CompletedProcess[str]) -> str:
    """Return useful subprocess metadata without replaying captured content."""
    return _format_process_diagnostic(
        completed.args,
        completed.returncode,
        completed.stdout,
        completed.stderr,
    )


def parse_json_stdout(completed: subprocess.CompletedProcess[str]) -> Any:
    """Parse a completed process's stdout as generic JSON on explicit request."""
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            "CLI JSON parse failed\n" + process_diagnostic(completed)
        ) from None


def reserve_loopback_listener() -> socket.socket:
    """Bind and retain a real loopback listener for child-process handoff."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


@contextmanager
def running_process(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    cleanup_timeout: float = 10,
    local: bool = True,
    pass_fds: Sequence[int] = (),
) -> Iterator[subprocess.Popen[str]]:
    """Run a real child process and always reap it without invoking a shell."""
    process_env = os.environ.copy()
    if env is not None:
        process_env.update(env)
    if local and not process_env.get("MEMPLEX_STORAGE_PATH"):
        raise ValueError("MEMPLEX_STORAGE_PATH is required for local child processes")

    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        pass_fds=tuple(pass_fds),
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=cleanup_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=cleanup_timeout)


def wait_for_http_ready(
    url: str,
    process: subprocess.Popen[str],
    *,
    timeout: float = 20,
) -> None:
    """Wait for one child HTTP endpoint to return 200 or report diagnostics."""
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "child process exited before HTTP readiness\n"
                + _format_process_diagnostic(
                    process.args,
                    process.returncode,
                    stdout,
                    stderr,
                )
            )
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as error:
            last_error = error
        time.sleep(0.05)
    raise AssertionError(
        "child process HTTP readiness timeout\n"
        f"args: {sanitize_argv(process.args)!r}\n"
        f"last_error_type: {type(last_error).__name__ if last_error else 'none'}"
    )
