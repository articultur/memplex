#!/usr/bin/env python3
"""Build normalized Python and npm release artifacts without network access."""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_release_module() -> ModuleType:
    # `from memplex.release import ...` would execute memplex/__init__.py and
    # pull the full runtime dependency set (yaml, ...), which the offline
    # CI build venv deliberately does not install; release.py itself is
    # stdlib-only, so load it directly by path. Registering it under
    # "memplex.release" (without going through the import system, so the
    # package __init__ never runs) keeps the class identity identical to a
    # regular import for tests that compare exceptions.
    existing = sys.modules.get("memplex.release")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "memplex.release", PROJECT_ROOT / "memplex" / "release.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("release_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[spec.name]
        raise
    return module


_release = _load_release_module()
ReleaseIntegrityError = _release.ReleaseIntegrityError
build_checksum_document = _release.build_checksum_document
build_cyclonedx_sbom = _release.build_cyclonedx_sbom
build_release_manifest = _release.build_release_manifest
validate_release_member_names = _release.validate_release_member_names

_SOURCE_FILES = ("pyproject.toml", "README.md", "LICENSE")
_IGNORED_COPY_NAMES = {
    ".DS_Store",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "memplex.egg-info",
}


def _validate_release_source_path(path: Path) -> None:
    """Reject links and special files before they can enter a release workspace."""
    try:
        root_stat = path.lstat()
        if stat.S_ISLNK(root_stat.st_mode):
            raise ReleaseIntegrityError("release_source_invalid")
        if stat.S_ISREG(root_stat.st_mode):
            return
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ReleaseIntegrityError("release_source_invalid")
        for current_root, directory_names, file_names in os.walk(path, followlinks=False):
            current = Path(current_root)
            for name in (*directory_names, *file_names):
                member_stat = (current / name).lstat()
                if stat.S_ISLNK(member_stat.st_mode) or not (
                    stat.S_ISDIR(member_stat.st_mode) or stat.S_ISREG(member_stat.st_mode)
                ):
                    raise ReleaseIntegrityError("release_source_invalid")
    except ReleaseIntegrityError:
        raise
    except OSError as exc:
        raise ReleaseIntegrityError("release_source_invalid") from exc


def _copy_release_sources(source: Path, destination: Path) -> tuple[Path, Path]:
    selected_sources = tuple(source / name for name in _SOURCE_FILES) + (
        source / "memplex",
        source / "npm/memplex/package.json",
        source / "npm/memplex/bin",
        source / "npm/memplex/install-agent.sh",
    )
    for selected_source in selected_sources:
        _validate_release_source_path(selected_source)

    python_root = destination / "python-source"
    npm_root = destination / "npm-source"
    python_root.mkdir()
    npm_root.mkdir()
    for name in _SOURCE_FILES:
        shutil.copyfile(source / name, python_root / name)
    shutil.copytree(
        source / "memplex",
        python_root / "memplex",
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
    )
    shutil.copyfile(source / "npm/memplex/package.json", npm_root / "package.json")
    shutil.copytree(source / "npm/memplex/bin", npm_root / "bin", symlinks=True)
    shutil.copyfile(source / "npm/memplex/install-agent.sh", npm_root / "install-agent.sh")
    _validate_release_source_path(python_root)
    _validate_release_source_path(npm_root)
    return python_root, npm_root


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseIntegrityError("release_build_failed") from exc


def _normalized_mode(name: str, original_mode: int, *, is_directory: bool) -> int:
    if is_directory:
        return 0o755
    if original_mode & 0o111 or name.endswith(".sh") or "/bin/" in f"/{name}":
        return 0o755
    return 0o644


def _normalize_tar_gz(source: Path, destination: Path, *, epoch: int) -> None:
    with tarfile.open(source, "r:gz") as archive:
        source_members = archive.getmembers()
        validate_release_member_names(member.name for member in source_members)
        if any(not (member.isdir() or member.isfile()) for member in source_members):
            raise ReleaseIntegrityError("release_archive_member_invalid")
        members: list[tuple[tarfile.TarInfo, bytes]] = []
        for source_member in source_members:
            payload = b""
            if source_member.isfile():
                extracted = archive.extractfile(source_member)
                if extracted is None:
                    raise ReleaseIntegrityError("release_archive_member_invalid")
                payload = extracted.read()
            member = tarfile.TarInfo(source_member.name)
            member.type = tarfile.DIRTYPE if source_member.isdir() else tarfile.REGTYPE
            member.size = len(payload)
            member.mode = _normalized_mode(
                source_member.name,
                source_member.mode,
                is_directory=source_member.isdir(),
            )
            member.mtime = epoch
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            members.append((member, payload))

    with tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024) as raw_tar:
        with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as output:
            for member, payload in sorted(members, key=lambda item: item[0].name):
                if member.isfile():
                    with tempfile.SpooledTemporaryFile(max_size=max(len(payload), 1)) as data:
                        data.write(payload)
                        data.seek(0)
                        output.addfile(member, data)
                else:
                    output.addfile(member)
        raw_tar.seek(0)
        with destination.open("wb") as target, gzip.GzipFile(filename="", mode="wb", fileobj=target, compresslevel=9, mtime=epoch) as zipped:
            shutil.copyfileobj(raw_tar, zipped)


def _normalize_wheel(source: Path, destination: Path, *, epoch: int) -> None:
    timestamp = time.gmtime(epoch)[:6]
    if timestamp[0] < 1980:
        raise ReleaseIntegrityError("release_epoch_invalid")
    with zipfile.ZipFile(source) as archive:
        source_infos = archive.infolist()
        validate_release_member_names(info.filename for info in source_infos)
        entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        for source_info in source_infos:
            source_mode = source_info.external_attr >> 16
            if stat.S_ISLNK(source_mode):
                raise ReleaseIntegrityError("release_archive_member_invalid")
            is_directory = source_info.is_dir()
            info = zipfile.ZipInfo(source_info.filename, date_time=timestamp)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = _normalized_mode(source_info.filename, source_mode, is_directory=is_directory)
            file_type = stat.S_IFDIR if is_directory else stat.S_IFREG
            info.external_attr = (file_type | mode) << 16
            entries.append((info, b"" if is_directory else archive.read(source_info.filename)))
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for info, payload in sorted(entries, key=lambda item: item[0].filename):
            output.writestr(info, payload)


def _assert_clean_checkout(source: Path) -> None:
    result = _run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source,
        env=os.environ.copy(),
    )
    if result.stdout.strip():
        raise ReleaseIntegrityError("release_checkout_dirty")


def build_release_artifacts(
    source: Path,
    output: Path,
    *,
    tag: str,
    source_date_epoch: int,
    require_clean: bool,
) -> None:
    source = source.resolve(strict=True)
    if type(source_date_epoch) is not int or source_date_epoch < 315532800:
        raise ReleaseIntegrityError("release_epoch_invalid")
    if require_clean:
        _assert_clean_checkout(source)
    if output.exists() and any(output.iterdir()):
        raise ReleaseIntegrityError("release_output_not_empty")
    output.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="memplex-release-") as temporary:
        workspace = Path(temporary)
        python_root, npm_root = _copy_release_sources(source, workspace)
        raw = workspace / "raw"
        raw.mkdir()
        cache = workspace / "npm-cache"
        env = os.environ.copy()
        env.update(
            {
                "LANG": "C",
                "LC_ALL": "C",
                "PYTHONHASHSEED": "0",
                "SOURCE_DATE_EPOCH": str(source_date_epoch),
                "TZ": "UTC",
                "UV_OFFLINE": "1",
                "npm_config_cache": str(cache),
                "npm_config_ignore_scripts": "true",
                "npm_config_update_notifier": "false",
            }
        )
        uv_command = [
            "uv",
            "build",
            "--offline",
            "--quiet",
            "--no-progress",
            "--no-create-gitignore",
        ]
        if env.get("MEMPLEX_RELEASE_NO_BUILD_ISOLATION") == "1":
            uv_command.append("--no-build-isolation")
        uv_command.extend(["--out-dir", str(raw), str(python_root)])
        _run(
            uv_command,
            cwd=workspace,
            env=env,
        )
        npm_result = _run(
            ["npm", "pack", "--json", "--ignore-scripts", "--pack-destination", str(raw)],
            cwd=npm_root,
            env=env,
        )
        try:
            npm_filename = json.loads(npm_result.stdout)[0]["filename"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ReleaseIntegrityError("release_build_failed") from exc

        wheel_sources = tuple(raw.glob("*.whl"))
        sdist_sources = tuple(raw.glob("*.tar.gz"))
        npm_source = raw / npm_filename
        if len(wheel_sources) != 1 or len(sdist_sources) != 1 or not npm_source.is_file():
            raise ReleaseIntegrityError("release_build_failed")

        wheel_destination = output / wheel_sources[0].name
        sdist_destination = output / sdist_sources[0].name
        npm_destination = output / npm_source.name
        _normalize_wheel(wheel_sources[0], wheel_destination, epoch=source_date_epoch)
        _normalize_tar_gz(sdist_sources[0], sdist_destination, epoch=source_date_epoch)
        _normalize_tar_gz(npm_source, npm_destination, epoch=source_date_epoch)

    sbom_destination = output / "release-sbom.cdx.json"
    sbom_destination.write_bytes(build_cyclonedx_sbom(source) + b"\n")
    checksums_destination = output / "release-checksums.json"
    checksums_destination.write_bytes(
        build_checksum_document(
            (wheel_destination, sdist_destination, npm_destination, sbom_destination)
        )
        + b"\n"
    )
    manifest = build_release_manifest(
        source,
        tag=tag,
        artifacts=(
            wheel_destination,
            sdist_destination,
            npm_destination,
            sbom_destination,
            checksums_destination,
        ),
    )
    (output / "release-manifest.json").write_bytes(manifest.canonical_bytes() + b"\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--umask", default="022")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        requested_umask = int(args.umask, 8)
        if not 0 <= requested_umask <= 0o777:
            raise ValueError
        os.umask(requested_umask)
        build_release_artifacts(
            args.source,
            args.output,
            tag=args.tag,
            source_date_epoch=args.source_date_epoch,
            require_clean=not args.allow_dirty,
        )
    except (ReleaseIntegrityError, ValueError):
        print("release_build_failed", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
