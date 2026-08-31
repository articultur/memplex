"""Signed, deployment-bound evidence for the four real agent host lifecycles."""

from __future__ import annotations

import hmac
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterable, NoReturn

_SCHEMA_VERSION = 3
_HOSTS = ("claude-code", "codex", "hermes", "openclaw")
_SHA256_HEX_LENGTH = 64
_MAX_EVIDENCE_BYTES = 128 * 1024
_MAX_EVIDENCE_AGE = timedelta(hours=24)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_HERMES_REVISION = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
_HERMES_PROVIDER_SHA256 = "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd"
_SHARED_REQUIRED_NODES = (
    "tests/test_agent_diagnostics.py::test_installation_diagnostics_detect_config_drift",
    "tests/test_agent_diagnostics.py::test_installation_diagnostics_projects_unreadable_runtime_sidecar_as_degraded",
)
_HOST_REQUIRED_NODES = {
    "claude-code": (
        "tests/test_agent_hot_paths.py::test_claude_real_cli_strictly_validates_installed_plugin",
        "tests/test_agent_installer_registry.py::test_claude_single_host_failure_restores_preinstall_state",
        "tests/test_agent_installer_registry.py::test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[claude-code]",
        "tests/test_hooks.py::test_claude_real_prompt_failure_persists_degraded_host_runtime_state",
    ),
    "codex": (
        "tests/test_agent_installer_registry.py::test_codex_single_host_failure_restores_preinstall_state",
        "tests/test_agent_installer_registry.py::test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[codex]",
        "tests/test_codex_plugin.py::test_codex_real_cli_discovers_plugin_in_isolated_home",
        "tests/test_codex_plugin.py::test_codex_real_recall_failure_persists_degraded_host_runtime_state",
    ),
    "hermes": (
        "tests/test_agent_installer_registry.py::test_configured_host_failure_restores_preinstall_state[hermes-config.yaml-managed_paths1]",
        "tests/test_agent_installer_registry.py::test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[hermes]",
        "tests/test_hermes_memory_provider.py::test_hermes_official_cli_discovers_installed_provider_in_isolated_home",
        "tests/test_hermes_memory_provider.py::test_hermes_real_prefetch_failure_persists_degraded_host_runtime_state",
    ),
    "openclaw": (
        "tests/test_agent_installer_registry.py::test_configured_host_failure_restores_preinstall_state[openclaw-openclaw.json-managed_paths0]",
        "tests/test_agent_installer_registry.py::test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[openclaw]",
        "tests/test_openclaw_plugin.py::test_openclaw_cli_loads_memplex_runtime_from_an_isolated_profile",
        "tests/test_openclaw_plugin.py::test_openclaw_real_recall_failure_persists_degraded_host_runtime_state",
    ),
}
_HOST_PROOF_KEYS = frozenset(
    {
        "host",
        "cli_path",
        "cli_sha256",
        "cli_version",
        "contract_sha256",
        "isolated_root_sha256",
        "required_node_results",
        "required_node_manifest_sha256",
        "junit_sha256",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "memplex_version",
        "source_sha256",
        "artifact_sha256",
        "deployment_id",
        "target_identity_sha256",
        "generated_at",
        "hermes_source_revision",
        "hermes_provider_sha256",
        "hosts",
        "key_id",
        "status",
        "signature",
    }
)


class HostLifecycleIntegrityError(RuntimeError):
    """Stable, redacted failure for host lifecycle evidence."""

    def __init__(self, code: str = "host_lifecycle_integrity") -> None:
        self.code = code
        super().__init__("host lifecycle evidence integrity check failed")


def _fail(code: str = "host_lifecycle_integrity") -> NoReturn:
    raise HostLifecycleIntegrityError(code)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HostLifecycleIntegrityError() from exc


def _exact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _fail()
    return value


def _sha256_text(value: object) -> str:
    text = _exact_text(value)
    if len(text) != _SHA256_HEX_LENGTH or text != text.lower():
        _fail()
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise HostLifecycleIntegrityError() from exc
    return text


def _timestamp(value: object) -> datetime:
    text = _exact_text(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HostLifecycleIntegrityError() from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text:
        _fail()
    return parsed


def _digest_paths(paths: Iterable[Path], *, root: Path) -> str:
    digest = sha256()
    seen: set[str] = set()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            _fail("host_lifecycle_binding_missing")
        relative = path.relative_to(root).as_posix()
        if relative in seen:
            _fail("host_lifecycle_binding_invalid")
        seen.add(relative)
        payload = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    if not seen:
        _fail("host_lifecycle_binding_missing")
    return digest.hexdigest()


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _contract_files(project_root: Path) -> dict[str, tuple[Path, ...]]:
    package_root = project_root / "memplex"
    plugin_root = package_root / "_plugin"
    installer = package_root / "adapters" / "agent_installer.py"
    install_transaction = package_root / "adapters" / "install_transaction.py"
    agent_assets = package_root / "adapters" / "agent_assets.py"
    agent_runtime = package_root / "adapters" / "agent_runtime.py"
    adapter_shared = package_root / "adapters" / "_shared.py"
    managed_identity = package_root / "adapters" / "managed_identity.py"
    mcp_server = package_root / "adapters" / "mcp_server.py"
    runtime_status = package_root / "adapters" / "runtime_status.py"
    shared_runtime = (
        installer,
        install_transaction,
        agent_assets,
        agent_runtime,
        adapter_shared,
        managed_identity,
        runtime_status,
    )
    return {
        "claude-code": (
            plugin_root / ".claude-plugin" / "plugin.json",
            plugin_root / ".mcp.json",
            plugin_root / "hooks" / "hooks.json",
            plugin_root / "scripts" / "claude-hook.sh",
            plugin_root / "scripts" / "hook-runner.py",
            plugin_root / "scripts" / "mcp-server.sh",
            mcp_server,
            *shared_runtime,
        ),
        "codex": (
            package_root / "adapters" / "codex_plugin.py",
            plugin_root / ".codex-plugin" / "plugin.json",
            plugin_root / ".codex.mcp.json",
            plugin_root / "hooks" / "hooks-codex.json",
            plugin_root / "scripts" / "codex-plugin.sh",
            plugin_root / "scripts" / "mcp-server.sh",
            mcp_server,
            *shared_runtime,
        ),
        "openclaw": (
            package_root / "adapters" / "openclaw_plugin.py",
            *shared_runtime,
        ),
        "hermes": (
            package_root / "adapters" / "hermes_memory_provider.py",
            *shared_runtime,
        ),
    }


def current_host_contract_digests(*, project_root: Path | None = None) -> dict[str, str]:
    """Bind each host proof to its exact integration implementation."""
    root = _project_root() if project_root is None else project_root.resolve()
    return {host: _digest_paths(paths, root=root) for host, paths in _contract_files(root).items()}


@dataclass(frozen=True, slots=True)
class HostLifecycleBinding:
    """Explicit identity of the deployment authorized to consume G008 evidence."""

    deployment_id: str
    source_sha256: str
    artifact_sha256: str
    target_identity_sha256: str
    expected_key_id: str

    def __post_init__(self) -> None:
        _exact_text(self.deployment_id)
        _sha256_text(self.source_sha256)
        _sha256_text(self.artifact_sha256)
        _sha256_text(self.target_identity_sha256)
        _exact_text(self.expected_key_id)


def required_node_manifest_sha256(results: tuple[tuple[str, str], ...]) -> str:
    return sha256(_canonical_json([[node, outcome] for node, outcome in results])).hexdigest()


def required_host_node_results(host: str) -> tuple[tuple[str, str], ...]:
    """Return the fixed G008 verifier node contract for one real host."""
    if host not in _HOSTS:
        _fail("host_lifecycle_suite_invalid")
    nodes = tuple(sorted((*_SHARED_REQUIRED_NODES, *_HOST_REQUIRED_NODES[host])))
    return tuple((node, "passed") for node in nodes)


def _open_parent_directory(path: Path) -> int:
    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        _fail("host_lifecycle_evidence_invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = path.parent
    directory_fd = os.open(os.sep if parent.is_absolute() else ".", flags)
    try:
        components = parent.parts[1:] if parent.is_absolute() else parent.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                _fail("host_lifecycle_evidence_invalid")
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _regular_executable_sha256(path_value: str) -> str:
    path = Path(_exact_text(path_value))
    if not path.is_absolute() or path.is_symlink():
        _fail("host_cli_missing")
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
            _fail("host_cli_missing")
        return sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise HostLifecycleIntegrityError("host_cli_missing") from exc


@dataclass(frozen=True, slots=True)
class HostLifecycleProof:
    host: str
    cli_path: str
    cli_sha256: str
    cli_version: str
    contract_sha256: str
    isolated_root_sha256: str
    required_node_results: tuple[tuple[str, str], ...]
    required_node_manifest_sha256: str
    junit_sha256: str

    def __post_init__(self) -> None:
        if self.host not in _HOSTS:
            _fail()
        _exact_text(self.cli_path)
        _sha256_text(self.cli_sha256)
        _exact_text(self.cli_version)
        _sha256_text(self.contract_sha256)
        _sha256_text(self.isolated_root_sha256)
        if type(self.required_node_results) is not tuple or not self.required_node_results:
            _fail("host_lifecycle_suite_incomplete")
        if tuple(sorted(self.required_node_results)) != self.required_node_results:
            _fail("host_lifecycle_suite_incomplete")
        for node_id, outcome in self.required_node_results:
            _exact_text(node_id)
            if outcome != "passed":
                _fail("host_lifecycle_suite_failed")
        expected_results = required_host_node_results(self.host)
        if self.required_node_results != expected_results:
            _fail("host_lifecycle_suite_invalid")
        if self.required_node_manifest_sha256 != required_node_manifest_sha256(expected_results):
            _fail("host_lifecycle_suite_invalid")
        _sha256_text(self.junit_sha256)

    def verify_runtime_binding(self) -> None:
        if _regular_executable_sha256(self.cli_path) != self.cli_sha256:
            _fail("host_cli_digest_mismatch")

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "cli_path": self.cli_path,
            "cli_sha256": self.cli_sha256,
            "cli_version": self.cli_version,
            "contract_sha256": self.contract_sha256,
            "isolated_root_sha256": self.isolated_root_sha256,
            "required_node_results": [list(item) for item in self.required_node_results],
            "required_node_manifest_sha256": self.required_node_manifest_sha256,
            "junit_sha256": self.junit_sha256,
        }

    @classmethod
    def from_dict(cls, payload: object) -> HostLifecycleProof:
        if type(payload) is not dict or frozenset(payload) != _HOST_PROOF_KEYS:
            _fail()
        results = payload["required_node_results"]
        if type(results) is not list:
            _fail()
        parsed_results: list[tuple[str, str]] = []
        for result in results:
            if type(result) is not list or len(result) != 2:
                _fail()
            parsed_results.append((result[0], result[1]))
        return cls(
            host=payload["host"],
            cli_path=payload["cli_path"],
            cli_sha256=payload["cli_sha256"],
            cli_version=payload["cli_version"],
            contract_sha256=payload["contract_sha256"],
            isolated_root_sha256=payload["isolated_root_sha256"],
            required_node_results=tuple(parsed_results),
            required_node_manifest_sha256=payload["required_node_manifest_sha256"],
            junit_sha256=payload["junit_sha256"],
        )


@dataclass(frozen=True, slots=True)
class HostLifecycleEvidence:
    schema_version: int
    memplex_version: str
    source_sha256: str
    artifact_sha256: str
    deployment_id: str
    target_identity_sha256: str
    generated_at: str
    hermes_source_revision: str
    hermes_provider_sha256: str
    hosts: tuple[HostLifecycleProof, ...]
    key_id: str
    status: str
    signature: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _fail()
        _exact_text(self.memplex_version)
        _exact_text(self.deployment_id)
        for value in (self.source_sha256, self.artifact_sha256, self.target_identity_sha256):
            _sha256_text(value)
        _timestamp(self.generated_at)
        if self.hermes_source_revision != _HERMES_REVISION:
            _fail("hermes_source_revision_mismatch")
        if self.hermes_provider_sha256 != _HERMES_PROVIDER_SHA256:
            _fail("hermes_provider_digest_mismatch")
        if type(self.hosts) is not tuple or tuple(item.host for item in self.hosts) != _HOSTS:
            _fail("host_lifecycle_matrix_incomplete")
        if any(type(item) is not HostLifecycleProof for item in self.hosts):
            _fail("host_lifecycle_matrix_incomplete")
        _exact_text(self.key_id)
        if self.status != "passed":
            _fail("host_lifecycle_not_passing")
        _sha256_text(self.signature)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "memplex_version": self.memplex_version,
            "source_sha256": self.source_sha256,
            "artifact_sha256": self.artifact_sha256,
            "deployment_id": self.deployment_id,
            "target_identity_sha256": self.target_identity_sha256,
            "generated_at": self.generated_at,
            "hermes_source_revision": self.hermes_source_revision,
            "hermes_provider_sha256": self.hermes_provider_sha256,
            "hosts": [host.to_dict() for host in self.hosts],
            "key_id": self.key_id,
            "status": self.status,
            "signature": self.signature,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    def canonical_unsigned_bytes(self) -> bytes:
        payload = self.to_dict()
        payload.pop("signature")
        return _canonical_json(payload)

    def verify(
        self,
        signing_key: bytes,
        *,
        expected_version: str,
        expected_binding: HostLifecycleBinding,
        now: datetime | None = None,
    ) -> None:
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("host_lifecycle_key_invalid")
        if self.memplex_version != _exact_text(expected_version):
            _fail("host_lifecycle_version_mismatch")
        if not isinstance(expected_binding, HostLifecycleBinding):
            _fail("host_lifecycle_binding_invalid")
        if self.key_id != expected_binding.expected_key_id:
            _fail("host_lifecycle_key_id_mismatch")
        checked_at = datetime.now(timezone.utc) if now is None else now
        if type(checked_at) is not datetime or checked_at.tzinfo is None:
            _fail("host_lifecycle_freshness_invalid")
        generated_at = _timestamp(self.generated_at)
        checked_at = checked_at.astimezone(timezone.utc)
        if (
            generated_at > checked_at + _MAX_FUTURE_SKEW
            or checked_at - generated_at > _MAX_EVIDENCE_AGE
        ):
            _fail("host_lifecycle_freshness_invalid")
        if self.source_sha256 != expected_binding.source_sha256:
            _fail("host_lifecycle_source_mismatch")
        if self.artifact_sha256 != expected_binding.artifact_sha256:
            _fail("host_lifecycle_artifact_mismatch")
        if self.deployment_id != expected_binding.deployment_id:
            _fail("host_lifecycle_deployment_mismatch")
        if self.target_identity_sha256 != expected_binding.target_identity_sha256:
            _fail("host_lifecycle_target_mismatch")
        expected_digests = current_host_contract_digests()
        for proof in self.hosts:
            if proof.contract_sha256 != expected_digests[proof.host]:
                _fail("host_lifecycle_contract_mismatch")
        expected_signature = hmac.new(
            signing_key, self.canonical_unsigned_bytes(), sha256
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, self.signature):
            _fail("host_lifecycle_signature_invalid")

    @classmethod
    def from_dict(cls, payload: object) -> HostLifecycleEvidence:
        if type(payload) is not dict or frozenset(payload) != _EVIDENCE_KEYS:
            _fail()
        hosts = payload["hosts"]
        if type(hosts) is not list:
            _fail()
        return cls(
            schema_version=payload["schema_version"],
            memplex_version=payload["memplex_version"],
            source_sha256=payload["source_sha256"],
            artifact_sha256=payload["artifact_sha256"],
            deployment_id=payload["deployment_id"],
            target_identity_sha256=payload["target_identity_sha256"],
            generated_at=payload["generated_at"],
            hermes_source_revision=payload["hermes_source_revision"],
            hermes_provider_sha256=payload["hermes_provider_sha256"],
            hosts=tuple(HostLifecycleProof.from_dict(item) for item in hosts),
            key_id=payload["key_id"],
            status=payload["status"],
            signature=payload["signature"],
        )

    @classmethod
    def create(
        cls,
        *,
        memplex_version: str,
        host_proofs: tuple[HostLifecycleProof, ...],
        binding: HostLifecycleBinding,
        key_id: str,
        signing_key: bytes,
        generated_at: datetime | None = None,
    ) -> HostLifecycleEvidence:
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("host_lifecycle_key_invalid")
        if not isinstance(binding, HostLifecycleBinding) or key_id != binding.expected_key_id:
            _fail("host_lifecycle_key_id_mismatch")
        if type(host_proofs) is not tuple or tuple(item.host for item in host_proofs) != _HOSTS:
            _fail("host_lifecycle_matrix_incomplete")
        for proof in host_proofs:
            proof.verify_runtime_binding()
        generated = datetime.now(timezone.utc) if generated_at is None else generated_at
        if type(generated) is not datetime or generated.tzinfo is None:
            _fail("host_lifecycle_freshness_invalid")
        unsigned = cls(
            schema_version=_SCHEMA_VERSION,
            memplex_version=memplex_version,
            source_sha256=binding.source_sha256,
            artifact_sha256=binding.artifact_sha256,
            deployment_id=binding.deployment_id,
            target_identity_sha256=binding.target_identity_sha256,
            generated_at=generated.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            hermes_source_revision=_HERMES_REVISION,
            hermes_provider_sha256=_HERMES_PROVIDER_SHA256,
            hosts=host_proofs,
            key_id=key_id,
            status="passed",
            signature="0" * 64,
        )
        signature = hmac.new(signing_key, unsigned.canonical_unsigned_bytes(), sha256).hexdigest()
        return cls(**{**unsigned.to_dict(), "hosts": host_proofs, "signature": signature})  # type: ignore[arg-type]


def read_host_lifecycle_evidence(path: Path) -> HostLifecycleEvidence:
    """Read one regular, non-symlink evidence file with a strict size cap."""
    try:
        directory_fd = _open_parent_directory(path)
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EVIDENCE_BYTES:
                    _fail("host_lifecycle_evidence_invalid")
                payload = os.read(fd, _MAX_EVIDENCE_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        if len(payload) > _MAX_EVIDENCE_BYTES:
            _fail("host_lifecycle_evidence_invalid")

        def reject_duplicate_keys(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    _fail("host_lifecycle_evidence_invalid")
                result[key] = value
            return result

        return HostLifecycleEvidence.from_dict(
            json.loads(payload, object_pairs_hook=reject_duplicate_keys)
        )
    except HostLifecycleIntegrityError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_lifecycle_evidence_invalid") from exc


def write_host_lifecycle_evidence(path: Path, evidence: HostLifecycleEvidence) -> None:
    directory_fd = -1
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary_created = False
    try:
        directory_fd = _open_parent_directory(path)
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            _fail("host_lifecycle_evidence_invalid")
        fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        temporary_created = True
        try:
            view = memoryview(evidence.canonical_bytes() + b"\n")
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    except HostLifecycleIntegrityError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_lifecycle_evidence_invalid") from exc
    finally:
        if directory_fd >= 0:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
