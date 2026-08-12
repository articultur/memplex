"""Fail-closed release metadata and artifact contracts.

This module deliberately contains no registry, network, or credential handling.  It
only turns already-built local artifacts into a deterministic manifest after all
repository release metadata has been proven consistent.
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import stat
import tarfile
import tomllib
import uuid
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

_RELEASE_SCHEMA_VERSION = 1
_SEMVER_RE = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_ABSOLUTE_RE = re.compile(r"[A-Za-z]:[/\\]")
_MANIFEST_KEYS = frozenset({"schema_version", "version", "tag", "artifacts"})
_ARTIFACT_KEYS = frozenset({"name", "sha256", "size"})
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "version",
        "tag",
        "release_manifest_sha256",
        "sbom_sha256",
        "checksums_sha256",
        "key_id",
        "status",
        "signature",
    }
)
_PRIVATE_COMPONENTS = frozenset(
    {
        ".codex",
        ".claude",
        ".git",
        ".omx",
        ".superpowers",
        "__pycache__",
        "secrets",
    }
)
_PRIVATE_FILENAMES = frozenset(
    {
        ".env",
        "changelog.json",
        "memory.json",
        "tombstones.json",
    }
)
_MAX_RELEASE_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_RELEASE_METADATA_BYTES = 1024 * 1024
_MAX_RELEASE_SBOM_BYTES = 16 * 1024 * 1024
_MAX_RELEASE_EVIDENCE_BYTES = 64 * 1024
_MAX_RELEASE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_RELEASE_UNPACKED_BYTES = 512 * 1024 * 1024
_MAX_RELEASE_MEMBERS = 10_000
_PRIVATE_CONTENT_RE = re.compile(
    rb"(?:/" + rb"Users/|[A-Za-z]:\\" + rb"Users\\|"
    rb"postgres(?:ql)?://[^\s'\"]+:[^\s'\"]+@|"
    rb"-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----)"
)


class ReleaseIntegrityError(RuntimeError):
    """Stable, redacted release validation failure."""

    def __init__(self, code: str = "release_integrity") -> None:
        self.code = code
        super().__init__("release manifest integrity check failed")


def _fail(code: str = "release_integrity") -> None:
    raise ReleaseIntegrityError(code)


def _require_exact_str(value: object) -> str:
    if type(value) is not str or not value:
        _fail()
    return value


def _require_exact_mapping(value: object) -> Mapping[str, Any]:
    if type(value) is not dict:
        _fail()
    return value


def _validate_version(value: object) -> str:
    version = _require_exact_str(value)
    if _SEMVER_RE.fullmatch(version) is None:
        _fail("release_version_invalid")
    return version


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReleaseIntegrityError() from exc


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    """One immutable artifact bound by name, size, and SHA-256."""

    name: str
    sha256: str
    size: int

    def __post_init__(self) -> None:
        validate_release_member_names((self.name,))
        if type(self.sha256) is not str or _SHA256_RE.fullmatch(self.sha256) is None:
            _fail()
        if type(self.size) is not int or self.size < 0:
            _fail()

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "sha256": self.sha256, "size": self.size}

    @classmethod
    def from_dict(cls, payload: object) -> ReleaseArtifact:
        data = _require_exact_mapping(payload)
        if frozenset(data) != _ARTIFACT_KEYS:
            _fail()
        return cls(name=data["name"], sha256=data["sha256"], size=data["size"])  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    """Canonical schema for readiness and provenance subject binding."""

    schema_version: int
    version: str
    tag: str
    artifacts: tuple[ReleaseArtifact, ...]

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _RELEASE_SCHEMA_VERSION:
            _fail()
        version = _validate_version(self.version)
        if type(self.tag) is not str or self.tag != f"v{version}":
            _fail("release_tag_mismatch")
        if type(self.artifacts) is not tuple or any(
            type(artifact) is not ReleaseArtifact for artifact in self.artifacts
        ):
            _fail()
        names = [artifact.name for artifact in self.artifacts]
        if names != sorted(names) or len(names) != len(set(names)):
            _fail("release_artifact_set_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "tag": self.tag,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_dict())

    def digest(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, payload: object) -> ReleaseManifest:
        data = _require_exact_mapping(payload)
        if frozenset(data) != _MANIFEST_KEYS or type(data["artifacts"]) is not list:
            _fail()
        artifacts = tuple(ReleaseArtifact.from_dict(item) for item in data["artifacts"])
        return cls(
            schema_version=data["schema_version"],  # type: ignore[arg-type]
            version=data["version"],  # type: ignore[arg-type]
            tag=data["tag"],  # type: ignore[arg-type]
            artifacts=artifacts,
        )


@dataclass(frozen=True, slots=True)
class ReleaseEvidence:
    """Local HMAC evidence; registry provenance remains OIDC-based."""

    schema_version: int
    version: str
    tag: str
    release_manifest_sha256: str
    sbom_sha256: str
    checksums_sha256: str
    key_id: str
    status: str
    signature: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            _fail()
        version = _validate_version(self.version)
        if type(self.tag) is not str or self.tag != f"v{version}":
            _fail()
        for digest in (
            self.release_manifest_sha256,
            self.sbom_sha256,
            self.checksums_sha256,
            self.signature,
        ):
            if type(digest) is not str or _SHA256_RE.fullmatch(digest) is None:
                _fail()
        if type(self.key_id) is not str or not self.key_id or self.key_id != self.key_id.strip():
            _fail()
        if self.status != "passed":
            _fail("release_evidence_not_passing")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "version": self.version,
            "tag": self.tag,
            "release_manifest_sha256": self.release_manifest_sha256,
            "sbom_sha256": self.sbom_sha256,
            "checksums_sha256": self.checksums_sha256,
            "key_id": self.key_id,
            "status": self.status,
            "signature": self.signature,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(self.to_dict())

    def canonical_unsigned_bytes(self) -> bytes:
        payload = self.to_dict()
        payload.pop("signature")
        return _canonical_json_bytes(payload)

    def verify(self, signing_key: bytes) -> None:
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("release_evidence_key_invalid")
        expected = hmac.new(signing_key, self.canonical_unsigned_bytes(), sha256).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            _fail("release_evidence_signature_invalid")

    @classmethod
    def from_dict(cls, payload: object) -> ReleaseEvidence:
        data = _require_exact_mapping(payload)
        if frozenset(data) != _EVIDENCE_KEYS:
            _fail()
        return cls(**data)  # type: ignore[arg-type]

    @classmethod
    def create(
        cls,
        *,
        manifest: ReleaseManifest,
        manifest_sha256: str,
        sbom_sha256: str,
        checksums_sha256: str,
        key_id: str,
        signing_key: bytes,
    ) -> ReleaseEvidence:
        unsigned = cls(
            schema_version=1,
            version=manifest.version,
            tag=manifest.tag,
            release_manifest_sha256=manifest_sha256,
            sbom_sha256=sbom_sha256,
            checksums_sha256=checksums_sha256,
            key_id=key_id,
            status="passed",
            signature="0" * 64,
        )
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("release_evidence_key_invalid")
        signature = hmac.new(signing_key, unsigned.canonical_unsigned_bytes(), sha256).hexdigest()
        return cls(**{**unsigned.to_dict(), "signature": signature})  # type: ignore[arg-type]


def validate_release_member_names(names: Iterable[str]) -> None:
    """Reject private, generated, absolute, or traversal archive members."""

    seen: set[str] = set()
    for raw_name in names:
        if type(raw_name) is not str or not raw_name or "\x00" in raw_name or "\\" in raw_name:
            _fail("release_private_asset")
        if raw_name.startswith("/") or _WINDOWS_ABSOLUTE_RE.match(raw_name):
            _fail("release_private_asset")
        path = PurePosixPath(raw_name)
        components = path.parts
        lowered = tuple(component.casefold() for component in components)
        if (
            raw_name in seen
            or any(component in {"", ".", ".."} for component in components)
            or any(component in _PRIVATE_COMPONENTS for component in lowered)
            or any(component.endswith(".pyc") for component in lowered)
            or any(component.startswith("postgresql:") for component in lowered)
            or any("secret" in component or "token" in component for component in lowered)
            or (lowered and lowered[-1] in _PRIVATE_FILENAMES)
        ):
            _fail("release_private_asset")
        seen.add(raw_name)


def _validate_release_member_payload(payload: bytes) -> None:
    if len(payload) > _MAX_RELEASE_MEMBER_BYTES or _PRIVATE_CONTENT_RE.search(payload):
        _fail("release_private_asset")


def _open_release_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = -1
    try:
        if path.is_absolute():
            directory_fd = os.open(os.sep, flags)
            components = path.parts[1:]
        else:
            directory_fd = os.open(".", flags)
            components = path.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                _fail("release_bundle_invalid")
            previous_fd = directory_fd
            directory_fd = os.open(component, flags, dir_fd=previous_fd)
            os.close(previous_fd)
        return directory_fd
    except BaseException:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise


def _read_release_file_at(directory_fd: int, name: str, *, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            _fail("release_bundle_invalid")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            _fail("release_bundle_invalid")
        return payload
    finally:
        os.close(file_fd)


def _load_release_bundle(directory: Path) -> tuple[ReleaseManifest, dict[str, bytes]]:
    directory_fd = -1
    try:
        directory_fd = _open_release_directory(Path(directory))
        manifest_bytes = _read_release_file_at(
            directory_fd,
            "release-manifest.json",
            limit=_MAX_RELEASE_METADATA_BYTES,
        )
        manifest = ReleaseManifest.from_dict(json.loads(manifest_bytes))
        allowed = {artifact.name for artifact in manifest.artifacts} | {
            "release-manifest.json"
        }
        if set(os.listdir(directory_fd)) != allowed:
            _fail("release_bundle_invalid")
        payloads = {"release-manifest.json": manifest_bytes}
        for artifact in manifest.artifacts:
            if artifact.name == "release-sbom.cdx.json":
                limit = _MAX_RELEASE_SBOM_BYTES
            elif artifact.name == "release-checksums.json":
                limit = _MAX_RELEASE_METADATA_BYTES
            else:
                limit = _MAX_RELEASE_ARCHIVE_BYTES
            payloads[artifact.name] = _read_release_file_at(
                directory_fd,
                artifact.name,
                limit=limit,
            )
        return manifest, payloads
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ReleaseIntegrityError,
    ) as exc:
        raise ReleaseIntegrityError("release_bundle_invalid") from exc
    finally:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _checksum_document_from_payloads(payloads: Mapping[str, bytes]) -> bytes:
    entries = [
        ReleaseArtifact(name, sha256(payload).hexdigest(), len(payload))
        for name, payload in payloads.items()
    ]
    entries.sort(key=lambda entry: entry.name)
    return _canonical_json_bytes(
        {"schema_version": 1, "artifacts": [entry.to_dict() for entry in entries]}
    )


def _verify_release_archives(payloads: Mapping[str, bytes], *, version: str) -> None:
    wheel_name = f"memplex-{version}-py3-none-any.whl"
    sdist_name = f"memplex-{version}.tar.gz"
    npm_name = f"memplex-{version}.tgz"
    try:
        with zipfile.ZipFile(io.BytesIO(payloads[wheel_name])) as archive:
            infos = archive.infolist()
            if (
                not infos
                or len(infos) > _MAX_RELEASE_MEMBERS
                or [info.filename for info in infos]
                != sorted(info.filename for info in infos)
                or sum(info.file_size for info in infos) > _MAX_RELEASE_UNPACKED_BYTES
                or len({info.date_time for info in infos}) != 1
            ):
                _fail("release_archive_member_invalid")
            validate_release_member_names(info.filename for info in infos)
            wheel_names = {info.filename for info in infos}
            required_wheel = {
                "memplex/release.py",
                "memplex/storage/migrations/0006_background_tasks.sql",
                "memplex/_plugin/.claude-plugin/plugin.json",
                "memplex/_plugin/.codex-plugin/plugin.json",
            }
            if not required_wheel.issubset(wheel_names):
                _fail("release_archive_member_invalid")
            for info in infos:
                mode = info.external_attr >> 16
                if not (info.is_dir() or stat.S_ISREG(mode)):
                    _fail("release_archive_member_invalid")
                if not info.is_dir():
                    _validate_release_member_payload(archive.read(info))
        for archive_name, npm_archive in ((sdist_name, False), (npm_name, True)):
            with tarfile.open(fileobj=io.BytesIO(payloads[archive_name]), mode="r:gz") as archive:
                members = archive.getmembers()
                if (
                    not members
                    or len(members) > _MAX_RELEASE_MEMBERS
                    or [member.name for member in members]
                    != sorted(member.name for member in members)
                    or sum(member.size for member in members) > _MAX_RELEASE_UNPACKED_BYTES
                    or len({member.mtime for member in members}) != 1
                ):
                    _fail("release_archive_member_invalid")
                validate_release_member_names(member.name for member in members)
                if any(
                    not (member.isdir() or member.isfile())
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname
                    or member.gname
                    or member.mode not in {0o644, 0o755}
                    for member in members
                ):
                    _fail("release_archive_member_invalid")
                names = {member.name for member in members}
                if npm_archive:
                    if names != {
                        "package/bin/memplex.js",
                        "package/install-agent.sh",
                        "package/package.json",
                    }:
                        _fail("release_archive_member_invalid")
                else:
                    root = f"memplex-{version}"
                    required_sdist = {
                        f"{root}/pyproject.toml",
                        f"{root}/README.md",
                        f"{root}/LICENSE",
                        f"{root}/memplex/release.py",
                        f"{root}/memplex/storage/migrations/0006_background_tasks.sql",
                    }
                    if not required_sdist.issubset(names):
                        _fail("release_archive_member_invalid")
                for member in members:
                    if member.isfile():
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            _fail("release_archive_member_invalid")
                        _validate_release_member_payload(extracted.read())
    except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ReleaseIntegrityError("release_archive_member_invalid") from exc


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        return _require_exact_mapping(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseIntegrityError) as exc:
        raise ReleaseIntegrityError("release_metadata_invalid") from exc


def _nested_version(data: Mapping[str, Any], *keys: object) -> str:
    current: object = data
    try:
        for key in keys:
            if type(key) is int:
                if type(current) is not list:
                    _fail("release_metadata_invalid")
                current = current[key]
            else:
                current = _require_exact_mapping(current)[key]  # type: ignore[index]
    except (IndexError, KeyError, TypeError) as exc:
        raise ReleaseIntegrityError("release_metadata_invalid") from exc
    return _validate_version(current)


def validate_release_version_set(project_root: Path, *, tag: str) -> str:
    """Prove all public distribution descriptors target one exact version."""

    root = Path(project_root)
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseIntegrityError("release_metadata_invalid") from exc
    version = _nested_version(_require_exact_mapping(project), "project", "version")
    if type(tag) is not str or tag != f"v{version}":
        _fail("release_tag_mismatch")

    descriptor_versions = (
        _nested_version(_read_json(root / "npm/memplex/package.json"), "version"),
        _nested_version(_read_json(root / "marketplace.json"), "plugins", 0, "version"),
        _nested_version(_read_json(root / "plugin/.claude-plugin/plugin.json"), "version"),
        _nested_version(_read_json(root / "plugin/.codex-plugin/plugin.json"), "version"),
        _nested_version(_read_json(root / "memplex/_plugin/.claude-plugin/plugin.json"), "version"),
        _nested_version(_read_json(root / "memplex/_plugin/.codex-plugin/plugin.json"), "version"),
    )
    compatibility_requirements = (
        _require_exact_str(
            _read_json(root / "npm/agent-installer/package.json")["dependencies"]["memplex"]  # type: ignore[index]
        ),
        _require_exact_str(
            _read_json(root / "npm/hermes-installer/package.json")["dependencies"]["memplex"]  # type: ignore[index]
        ),
    )
    if any(candidate != version for candidate in descriptor_versions + compatibility_requirements):
        _fail("release_version_mismatch")
    return version


def build_release_manifest(
    project_root: Path,
    *,
    tag: str,
    artifacts: Iterable[Path],
) -> ReleaseManifest:
    """Build a deterministic manifest from existing regular artifact files."""

    version = validate_release_version_set(Path(project_root), tag=tag)
    entries: list[ReleaseArtifact] = []
    for supplied_path in artifacts:
        path = Path(supplied_path)
        try:
            if path.is_symlink() or not path.is_file():
                _fail("release_artifact_invalid")
            payload = path.read_bytes()
        except OSError as exc:
            raise ReleaseIntegrityError("release_artifact_invalid") from exc
        entries.append(
            ReleaseArtifact(name=path.name, sha256=sha256(payload).hexdigest(), size=len(payload))
        )
    entries.sort(key=lambda artifact: artifact.name)
    return ReleaseManifest(
        schema_version=_RELEASE_SCHEMA_VERSION,
        version=version,
        tag=tag,
        artifacts=tuple(entries),
    )


def _dependency_name(requirement: object) -> str:
    raw = _require_exact_str(requirement)
    match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw)
    if match is None:
        _fail("release_lock_invalid")
    return match.group(0).replace("_", "-").lower()


def build_cyclonedx_sbom(project_root: Path) -> bytes:
    """Create a deterministic CycloneDX 1.6 SBOM from the exact uv lock."""

    root = Path(project_root)
    try:
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        lock_bytes = (root / "uv.lock").read_bytes()
        lock = tomllib.loads(lock_bytes.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseIntegrityError("release_lock_invalid") from exc
    project = _require_exact_mapping(_require_exact_mapping(pyproject)["project"])
    version = _validate_version(project["version"])
    requirements = list(project.get("dependencies", []))
    optional = _require_exact_mapping(project.get("optional-dependencies", {}))
    for values in optional.values():
        if type(values) is not list:
            _fail("release_lock_invalid")
        requirements.extend(values)
    direct_names = {
        name
        for requirement in requirements
        if (name := _dependency_name(requirement)) != "memplex"
    }

    packages = lock.get("package")
    if type(packages) is not list:
        _fail("release_lock_invalid")
    components: list[dict[str, object]] = []
    locked_names: set[str] = set()
    identities: set[tuple[str, str]] = set()
    for raw_package in packages:
        package = _require_exact_mapping(raw_package)
        name = _dependency_name(package.get("name"))
        package_version = _require_exact_str(package.get("version"))
        if name == "memplex":
            continue
        identity = (name, package_version)
        if identity in identities:
            _fail("release_lock_invalid")
        identities.add(identity)
        locked_names.add(name)
        source_hash: object | None = None
        sdist = package.get("sdist")
        if type(sdist) is dict:
            source_hash = sdist.get("hash")
        if source_hash is None and type(package.get("wheels")) is list and package["wheels"]:
            source_hash = _require_exact_mapping(package["wheels"][0]).get("hash")
        if type(source_hash) is not str or not source_hash.startswith("sha256:"):
            _fail("release_lock_invalid")
        digest = source_hash.removeprefix("sha256:")
        if _SHA256_RE.fullmatch(digest) is None:
            _fail("release_lock_invalid")
        components.append(
            {
                "type": "library",
                "name": name,
                "version": package_version,
                "purl": f"pkg:pypi/{name}@{package_version}",
                "hashes": [{"alg": "SHA-256", "content": digest}],
                "properties": [
                    {
                        "name": "memplex:direct-dependency",
                        "value": "true" if name in direct_names else "false",
                    }
                ],
            }
        )
    if not direct_names.issubset(locked_names):
        _fail("release_lock_invalid")
    components.sort(key=lambda component: (str(component["name"]), str(component["version"])))
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://github.com/articultur/memplex/sbom/{version}/{sha256(lock_bytes).hexdigest()}",
    )
    return _canonical_json_bytes(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": f"urn:uuid:{serial}",
            "version": 1,
            "metadata": {
                "component": {
                    "type": "application",
                    "name": "memplex",
                    "version": version,
                    "purl": f"pkg:pypi/memplex@{version}",
                }
            },
            "components": components,
        }
    )


def verify_cyclonedx_sbom(project_root: Path, payload: bytes) -> None:
    if type(payload) is not bytes:
        _fail("release_sbom_invalid")
    expected = build_cyclonedx_sbom(project_root)
    if not hmac.compare_digest(expected, payload) and not hmac.compare_digest(
        expected + b"\n", payload
    ):
        _fail("release_sbom_invalid")


def build_checksum_document(artifacts: Iterable[Path]) -> bytes:
    entries: list[ReleaseArtifact] = []
    for artifact in artifacts:
        path = Path(artifact)
        try:
            if path.is_symlink() or not path.is_file():
                _fail("release_artifact_invalid")
            payload = path.read_bytes()
        except OSError as exc:
            raise ReleaseIntegrityError("release_artifact_invalid") from exc
        entries.append(ReleaseArtifact(path.name, sha256(payload).hexdigest(), len(payload)))
    entries.sort(key=lambda entry: entry.name)
    if len({entry.name for entry in entries}) != len(entries):
        _fail("release_artifact_set_invalid")
    return _canonical_json_bytes(
        {"schema_version": 1, "artifacts": [entry.to_dict() for entry in entries]}
    )


def verify_release_bundle(project_root: Path, release_directory: Path) -> ReleaseManifest:
    """Verify exact filenames, bytes, SBOM, checksums, and repository version binding."""

    directory = Path(release_directory)
    try:
        manifest, payloads = _load_release_bundle(directory)
        validate_release_version_set(Path(project_root), tag=manifest.tag)
        for artifact in manifest.artifacts:
            payload = payloads[artifact.name]
            if len(payload) != artifact.size or sha256(payload).hexdigest() != artifact.sha256:
                _fail("release_bundle_invalid")
        verify_cyclonedx_sbom(project_root, payloads["release-sbom.cdx.json"])
        expected_checksums = _checksum_document_from_payloads(
            {
                artifact.name: payloads[artifact.name]
                for artifact in manifest.artifacts
                if artifact.name != "release-checksums.json"
            }
        )
        if not hmac.compare_digest(
            expected_checksums + b"\n", payloads["release-checksums.json"]
        ):
            _fail("release_bundle_invalid")
        _verify_release_archives(payloads, version=manifest.version)
    except (OSError, ReleaseIntegrityError) as exc:
        raise ReleaseIntegrityError("release_bundle_invalid") from exc
    return manifest


def verify_release_evidence(
    project_root: Path,
    release_directory: Path,
    evidence_payload: bytes,
    *,
    signing_key: bytes,
) -> ReleaseEvidence:
    """Verify local readiness evidence against the current exact bundle."""

    manifest = verify_release_bundle(project_root, release_directory)
    try:
        if type(evidence_payload) is not bytes or len(evidence_payload) > _MAX_RELEASE_EVIDENCE_BYTES:
            _fail("release_evidence_invalid")
        evidence = ReleaseEvidence.from_dict(json.loads(evidence_payload))
        evidence.verify(signing_key)
        loaded_manifest, payloads = _load_release_bundle(Path(release_directory))
        if loaded_manifest != manifest:
            _fail("release_evidence_invalid")
        expected = {
            "release_manifest_sha256": sha256(payloads["release-manifest.json"]).hexdigest(),
            "sbom_sha256": sha256(payloads["release-sbom.cdx.json"]).hexdigest(),
            "checksums_sha256": sha256(payloads["release-checksums.json"]).hexdigest(),
        }
    except (OSError, UnicodeError, json.JSONDecodeError, ReleaseIntegrityError) as exc:
        raise ReleaseIntegrityError("release_evidence_invalid") from exc
    if (
        evidence.version != manifest.version
        or evidence.tag != manifest.tag
        or evidence.release_manifest_sha256 != expected["release_manifest_sha256"]
        or evidence.sbom_sha256 != expected["sbom_sha256"]
        or evidence.checksums_sha256 != expected["checksums_sha256"]
    ):
        _fail("release_evidence_invalid")
    return evidence


def verify_release_readiness_evidence(
    release_directory: Path,
    evidence_payload: bytes,
    *,
    signing_key: bytes,
    expected_version: str,
) -> ReleaseEvidence:
    """Verify a signed immutable bundle from an installed runtime.

    Unlike :func:`verify_release_evidence`, this gate does not depend on a source
    checkout.  It binds the installed package version to the exact release
    manifest, distribution archives, checksum document, SBOM, and signed local
    evidence supplied by the deployment system.
    """

    version = _validate_version(expected_version)
    directory = Path(release_directory)
    try:
        if type(evidence_payload) is not bytes or len(evidence_payload) > _MAX_RELEASE_EVIDENCE_BYTES:
            _fail("release_evidence_invalid")
        manifest, payloads = _load_release_bundle(directory)
        manifest_bytes = payloads["release-manifest.json"]
        if not hmac.compare_digest(manifest.canonical_bytes() + b"\n", manifest_bytes):
            _fail("release_bundle_invalid")
        if manifest.version != version:
            _fail("release_evidence_invalid")
        required_names = {
            f"memplex-{version}-py3-none-any.whl",
            f"memplex-{version}.tar.gz",
            f"memplex-{version}.tgz",
            "release-checksums.json",
            "release-sbom.cdx.json",
        }
        if {artifact.name for artifact in manifest.artifacts} != required_names:
            _fail("release_bundle_invalid")
        for artifact in manifest.artifacts:
            payload = payloads[artifact.name]
            if len(payload) != artifact.size or not hmac.compare_digest(
                sha256(payload).hexdigest(), artifact.sha256
            ):
                _fail("release_bundle_invalid")
        _verify_release_archives(payloads, version=version)
        expected_checksums = _checksum_document_from_payloads(
            {
                artifact.name: payloads[artifact.name]
                for artifact in manifest.artifacts
                if artifact.name != "release-checksums.json"
            }
        )
        if not hmac.compare_digest(
            expected_checksums + b"\n", payloads["release-checksums.json"]
        ):
            _fail("release_bundle_invalid")
        sbom = _require_exact_mapping(
            json.loads(payloads["release-sbom.cdx.json"])
        )
        metadata = _require_exact_mapping(sbom.get("metadata"))
        component = _require_exact_mapping(metadata.get("component"))
        if (
            sbom.get("bomFormat") != "CycloneDX"
            or sbom.get("specVersion") != "1.6"
            or component.get("name") != "memplex"
            or component.get("version") != version
        ):
            _fail("release_sbom_invalid")
        evidence = ReleaseEvidence.from_dict(json.loads(evidence_payload))
        evidence.verify(signing_key)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ReleaseIntegrityError,
    ) as exc:
        raise ReleaseIntegrityError("release_evidence_invalid") from exc
    expected = {
        "release_manifest_sha256": sha256(manifest_bytes).hexdigest(),
        "sbom_sha256": sha256(payloads["release-sbom.cdx.json"]).hexdigest(),
        "checksums_sha256": sha256(payloads["release-checksums.json"]).hexdigest(),
    }
    if (
        evidence.version != version
        or evidence.tag != manifest.tag
        or evidence.release_manifest_sha256 != expected["release_manifest_sha256"]
        or evidence.sbom_sha256 != expected["sbom_sha256"]
        or evidence.checksums_sha256 != expected["checksums_sha256"]
    ):
        _fail("release_evidence_invalid")
    return evidence


def read_release_evidence_file(path: Path) -> bytes:
    """Read one evidence file through a pinned, no-symlink parent directory."""

    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        _fail("release_evidence_invalid")
    directory_fd = -1
    try:
        directory_fd = _open_release_directory(path.parent)
        return _read_release_file_at(
            directory_fd,
            path.name,
            limit=_MAX_RELEASE_EVIDENCE_BYTES,
        )
    except (OSError, ReleaseIntegrityError) as exc:
        raise ReleaseIntegrityError("release_evidence_invalid") from exc
    finally:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                pass


def write_release_evidence_atomic(path: Path, evidence: ReleaseEvidence) -> None:
    """Write evidence through a pinned no-symlink ancestor chain."""

    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        _fail("release_evidence_output_invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = -1
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary_created = False
    try:
        parent = path.parent
        if parent.is_absolute():
            directory_fd = os.open(os.sep, directory_flags)
            components = parent.parts[1:]
        else:
            directory_fd = os.open(".", directory_flags)
            components = parent.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                _fail("release_evidence_output_invalid")
            previous_fd = directory_fd
            directory_fd = os.open(component, directory_flags, dir_fd=previous_fd)
            os.close(previous_fd)
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            _fail("release_evidence_output_invalid")
        fd = os.open(temporary_name, file_flags, 0o600, dir_fd=directory_fd)
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
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
    except ReleaseIntegrityError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ReleaseIntegrityError("release_evidence_output_invalid") from exc
    finally:
        if directory_fd >= 0:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            try:
                os.close(directory_fd)
            except OSError:
                pass
