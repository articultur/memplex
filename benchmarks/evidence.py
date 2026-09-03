"""Create and verify canonical G003 aggregate benchmark evidence bundles."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.base import BenchmarkResult

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONTRACT_FILES = (
    "checksums.sha256",
    "datasets.json",
    "manifest.json",
    "results.jsonl",
)
_CHECKSUM_TARGETS = ("datasets.json", "manifest.json", "results.jsonl")
_COVERAGE_DIMENSIONS = frozenset(
    {
        "retrieval",
        "temporal_multihop",
        "acl",
        "sync",
        "latency_capacity",
        "recovery",
        "host_integration",
    }
)
_COVERAGE_STATUSES = frozenset({"failed", "not_measured", "passed", "unavailable"})
_SECRET_OPTION = re.compile(
    r"(?:api[-_]?key|auth|bearer|cookie|credential|dsn|pass(?:word|phrase)?|"
    r"private[-_]?key|secret|token)",
    re.IGNORECASE,
)
_SECRET_SHORT_OPTIONS = frozenset({"-a", "-k", "-p", "-t", "-u"})
_ENV_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_URL_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://.*@", re.DOTALL)
_URL_SUFFIX = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://.*[?#]", re.DOTALL)
_LIBPQ_CREDENTIAL = re.compile(
    r"(?<![\w-])(?:user|password|passfile|sslpassword)\s*=", re.IGNORECASE
)
_AUTHORIZATION_HEADER = re.compile(r"(\bAuthorization\s*:\s*).+", re.IGNORECASE)
_CHECKSUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\n")
_NORMALIZED_METRICS = frozenset(
    {
        "bleu",
        "event_tracking",
        "exact_match",
        "f1",
        "fact_retention_rate",
        "hop_coverage",
        "mrr",
        "multihop_accuracy",
        "observation_retention_rate",
        "persona_consistency",
        "preference_retention_rate",
        "recency_accuracy",
        "recency_ranking",
        "rouge_l",
        "substring_hit_rate",
        "token_f1",
    }
)
_NORMALIZED_METRIC_AT_K = re.compile(r"(?:hop_)?(?:precision|recall)@[1-9][0-9]*\Z")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _git(*args: str) -> bytes:
    try:
        return subprocess.run(  # noqa: UP022 - explicit PIPE form documents both streams
            ("git", *args),
            cwd=_PROJECT_ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("unable to record Git provenance") from exc


def _source_provenance() -> dict[str, object]:
    commit = _git("rev-parse", "HEAD").decode().strip()
    branch = _git("branch", "--show-current").decode().strip()
    status = _git("status", "--porcelain=v1", "--untracked-files=all")

    digest = hashlib.sha256()
    digest.update(_git("diff", "--binary", "HEAD", "--", "."))
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    for raw_path in sorted(item for item in untracked.split(b"\0") if item):
        relative_path = os.fsdecode(raw_path)
        path = _PROJECT_ROOT / relative_path
        digest.update(raw_path)
        digest.update(b"\0")
        if path.is_file() and not path.is_symlink():
            digest.update(path.read_bytes())
        digest.update(b"\0")

    return {
        "commit": commit,
        "branch": branch,
        "dirty": bool(status),
        "diff_digest": digest.hexdigest(),
    }


def _sample_provenance(records: object) -> tuple[int, str]:
    if not isinstance(records, list):
        raise ValueError("dataset JSON must be a list of samples")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)

    sample_ids: list[str] = []
    seen_sample_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("every dataset sample must be a JSON object")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
        identity_key = next(
            (key for key in ("id", "question_id", "conversation_id") if key in record),
            None,
        )
        if identity_key is None:
            sample_id = _sha256(_canonical_json(record))
        else:
            identity = record[identity_key]
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("dataset sample identities must be non-empty strings")
            sample_id = identity
        if sample_id in seen_sample_ids:
            raise ValueError("dataset sample identities must be unique; duplicate found")
        sample_ids.append(sample_id)
        seen_sample_ids.add(sample_id)
    return len(sample_ids), _sha256(_canonical_json(sorted(sample_ids)))


def _embedded_provenance(dataset: Mapping[str, object]) -> dict[str, object]:
    if set(dataset) != {"name", "records", "source_kind", "synthetic"}:
        raise ValueError("embedded dataset has invalid fields")
    name = dataset["name"]
    source_kind = dataset["source_kind"]
    synthetic = dataset["synthetic"]
    records = dataset["records"]
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError("dataset name must be a bundle-local file name")
    if not isinstance(source_kind, str) or not source_kind:
        raise ValueError("dataset source_kind must be a non-empty string")
    if not isinstance(synthetic, bool):
        raise ValueError("dataset synthetic must be a boolean")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    sample_count, sample_ids_digest = _sample_provenance(records)
    return {
        "digest": _sha256(_canonical_json(records)),
        "name": name,
        "path": "datasets.json",
        "sample_count": sample_count,
        "sample_ids_digest": sample_ids_digest,
        "source_kind": source_kind,
        "synthetic": synthetic,
    }


def _embed_dataset(
    dataset: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    if set(dataset) != {"path", "source_kind", "synthetic"}:
        raise ValueError("dataset provenance requires path, source_kind, and synthetic")
    raw_path = dataset["path"]
    source_kind = dataset["source_kind"]
    synthetic = dataset["synthetic"]
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("dataset path must be a non-empty string")
    if not isinstance(source_kind, str) or not source_kind:
        raise ValueError("dataset source_kind must be a non-empty string")
    if not isinstance(synthetic, bool):
        raise ValueError("dataset synthetic must be a boolean")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)

    path = Path(raw_path).resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ValueError("dataset path must identify a regular file")
    try:
        records = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("dataset must contain valid JSON") from exc
    embedded = {
        "name": path.name,
        "records": records,
        "source_kind": source_kind,
        "synthetic": synthetic,
    }
    return embedded, _embedded_provenance(embedded)


def _validated_datasets(
    datasets: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    provenance: list[dict[str, object]] = []
    identities: set[str] = set()
    names: set[str] = set()
    for dataset in datasets:
        item = _embedded_provenance(dataset)
        digest = item["digest"]
        name = item["name"]
        if digest in identities:
            raise ValueError("duplicate dataset identity")
        if name in names:
            raise ValueError("duplicate dataset name")
        identities.add(str(digest))
        names.add(str(name))
        provenance.append(item)
    return provenance


def _validated_coverage(
    coverage: Mapping[str, Mapping[str, str]],
) -> dict[str, dict[str, str]]:
    if set(coverage) != _COVERAGE_DIMENSIONS:
        raise ValueError("coverage must contain exactly the G003 dimensions")

    validated: dict[str, dict[str, str]] = {}
    for dimension in sorted(_COVERAGE_DIMENSIONS):
        item = coverage[dimension]
        if not isinstance(item, Mapping) or set(item) != {"status", "reason"}:
            raise ValueError("each coverage item requires status and reason")
        status = item["status"]
        reason = item["reason"]
        if status not in _COVERAGE_STATUSES:
            raise ValueError("coverage status is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("coverage reason must be a non-empty string")
        validated[dimension] = {"reason": reason, "status": status}
    return validated


def _redact_scalar(value: str) -> str:
    assignment = _ENV_ASSIGNMENT.fullmatch(value)
    if assignment and _SECRET_OPTION.search(assignment.group(1)):
        return f"{assignment.group(1)}=<redacted>"
    if _LIBPQ_CREDENTIAL.search(value):
        # Redact the complete DSN, including quoted/escaped libpq values.
        return "<redacted>"
    if _URL_USERINFO.search(value) or _URL_SUFFIX.search(value):
        # URI credentials may contain whitespace or ambiguous delimiters. Never
        # retain a partial userinfo/query/fragment: redact the complete scalar.
        return "<redacted>"
    return _AUTHORIZATION_HEADER.sub(r"\1<redacted>", value)


def _redact_config(value: object, *, key: str | None = None) -> object:
    if key is not None and _SECRET_OPTION.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for nested_key, nested_value in value.items():
            if not isinstance(nested_key, str):
                raise ValueError("config keys must be strings")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
            redacted[nested_key] = _redact_config(nested_value, key=nested_key)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_config(item) for item in value]
    if isinstance(value, str):
        return _redact_scalar(value)
    return value


def _redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for argument in command:
        if not isinstance(argument, str):
            raise ValueError("command arguments must be strings")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
        if hide_next:
            redacted.append("<redacted>")
            hide_next = False
            continue
        option, separator, _value = argument.partition("=")
        if option in _SECRET_SHORT_OPTIONS and not separator:
            redacted.append(option)
            hide_next = True
            continue
        if argument.startswith("-") and _SECRET_OPTION.search(option):
            if separator:
                redacted.append(f"{option}=<redacted>")
            else:
                redacted.append(argument)
                hide_next = True
            continue
        if len(argument) > 2 and argument[:2] in _SECRET_SHORT_OPTIONS:
            redacted.append(f"{argument[:2]}<redacted>")
            continue
        redacted.append(_redact_scalar(argument))
    if hide_next:
        raise ValueError("secret command option is missing its value")
    return redacted


def _evidence_level(manifest: Mapping[str, object]) -> str:
    del manifest
    return "E1"


def _validate_normalized_metric(metric: object, value: object) -> None:
    if not isinstance(metric, str) or not (
        metric in _NORMALIZED_METRICS or _NORMALIZED_METRIC_AT_K.fullmatch(metric)
    ):
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
    ):
        raise ValueError(f"normalized metric '{metric}' must be finite and within [0, 1]")


def _result_payload(result: BenchmarkResult) -> dict[str, object]:
    if not isinstance(result, BenchmarkResult):
        raise TypeError("results must contain BenchmarkResult instances")
    payload = result.to_dict()
    _validate_result(payload)
    return payload


def _require_fields(value: object, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError(f"{name} must contain exactly the required fields")
    return value


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_timestamp(value: object, name: str, *, require_timezone: bool = True) -> datetime:
    _require_text(value, name)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO timestamp") from exc
    if require_timezone and parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _require_digest(value: object, length: int, name: str) -> None:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"{name} must be a {length}-character hexadecimal digest")


_OPTIONAL_RESULT_FIELDS = frozenset({"latency_p50_ms", "latency_p99_ms"})


def _require_non_negative_number(value: object, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"result {name} must be a non-negative finite number")


def _validate_result(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("result must contain exactly the required fields")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    optional = {k: value[k] for k in _OPTIONAL_RESULT_FIELDS if k in value}
    row = _require_fields(
        {k: v for k, v in value.items() if k not in _OPTIONAL_RESULT_FIELDS},
        {"benchmark", "dataset", "metric", "value", "latency_ms", "samples", "timestamp"},
        "result",
    )
    for field in ("benchmark", "dataset", "metric"):
        _require_text(row[field], f"result {field}")
    _validate_normalized_metric(row["metric"], row["value"])
    if type(row["value"]) not in (int, float) or not math.isfinite(row["value"]):
        raise ValueError("result value must be a finite number")
    # latency_ms is a float-millisecond mean since the perf_counter migration.
    _require_non_negative_number(row["latency_ms"], "latency_ms")
    for name, percentile_value in optional.items():
        _require_non_negative_number(percentile_value, name)
    if type(row["samples"]) is not int or row["samples"] < 0:
        raise ValueError("result samples must be a non-negative integer")
    # Existing LoCoMo/memory aggregates use naive ISO timestamps; do not invent a zone.
    _require_timestamp(row["timestamp"], "result timestamp", require_timezone=False)


def _validate_provenance(manifest: Mapping[str, Any]) -> None:
    source = _require_fields(
        manifest["source"], {"commit", "branch", "dirty", "diff_digest"}, "source"
    )
    _require_digest(source["commit"], 40, "source commit")
    _require_digest(source["diff_digest"], 64, "source diff_digest")
    # An empty branch is legitimate for a detached HEAD.
    if not isinstance(source["branch"], str) or type(source["dirty"]) is not bool:
        raise ValueError("source branch/dirty types are invalid")
    environment = _require_fields(manifest["environment"], {"os", "arch", "python"}, "environment")
    for field, value in environment.items():
        _require_text(value, f"environment {field}")
    _require_digest(manifest["uv_lock_digest"], 64, "uv_lock_digest")
    timestamps = _require_fields(
        manifest["timestamps"], {"created_at", "finalized_at"}, "timestamps"
    )
    created = _require_timestamp(timestamps["created_at"], "created_at")
    finalized = _require_timestamp(timestamps["finalized_at"], "finalized_at")
    if finalized < created:
        raise ValueError("timestamps finalized_at precedes created_at")
    _require_text(manifest["cwd"], "cwd")


def _validate_manifest(value: object) -> None:
    manifest = _require_fields(
        value,
        {"schema_version", "source", "environment", "uv_lock_digest", "datasets", "config",
         "command", "cwd", "timestamps", "coverage", "limitations", "raw", "evidence_level"},
        "manifest",
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")
    _validate_provenance(manifest)
    if not isinstance(manifest["datasets"], list) or not manifest["datasets"]:
        raise ValueError("manifest requires dataset provenance")
    for dataset in manifest["datasets"]:
        _require_fields(
            dataset,
            {"digest", "name", "path", "sample_count", "sample_ids_digest", "source_kind", "synthetic"},
            "dataset provenance",
        )
        if type(dataset["synthetic"]) is not bool or type(dataset["sample_count"]) is not int:
            raise ValueError("dataset synthetic/sample_count types are invalid")
    coverage = manifest["coverage"]
    if not isinstance(coverage, dict):
        raise ValueError("manifest coverage is invalid")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    _validated_coverage(coverage)
    command = manifest["command"]
    if not isinstance(command, list) or not command or _redact_command(command) != command:
        raise ValueError("manifest command is invalid or contains unredacted secrets")
    config = manifest["config"]
    if not isinstance(config, dict) or _redact_config(config) != config:
        raise ValueError("manifest config is invalid or contains unredacted secrets")
    limitations = manifest["limitations"]
    if not isinstance(limitations, list):
        raise ValueError("manifest limitations must be a list")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    for reason in limitations:
        _require_text(reason, "limitation reason")
    _validate_raw(manifest["raw"])
    if manifest["evidence_level"] != _evidence_level(manifest):
        raise ValueError("manifest evidence_level exceeds its evidence inputs")


def _validate_raw(value: object) -> None:
    raw = _require_fields(value, {"status", "reason"}, "raw")
    if raw["status"] is not None:
        raise ValueError("aggregate-only bundles cannot claim a raw status")
    _require_text(raw["reason"], "unavailable raw evidence reason")


def create_bundle(
    run_dir: str | os.PathLike[str],
    results: Sequence[BenchmarkResult],
    dataset_files: Sequence[Mapping[str, object]],
    config: Mapping[str, object],
    coverage: Mapping[str, Mapping[str, str]],
    command: Sequence[str],
    raw_status: str | None,
    raw_reason: str | None,
) -> dict[str, object]:
    """Create a new canonical evidence bundle without exposing partial output."""

    destination = Path(run_dir)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    _validate_raw({"status": raw_status, "reason": raw_reason})

    created_at = _utc_now()
    if not results:
        raise ValueError("at least one benchmark result is required")
    result_lines = [_canonical_json(_result_payload(result)) + b"\n" for result in results]
    embedded_datasets: list[dict[str, object]] = []
    for dataset in dataset_files:
        embedded, _provenance = _embed_dataset(dataset)
        embedded_datasets.append(embedded)
    datasets = _validated_datasets(embedded_datasets)
    datasets_bytes = _canonical_json(embedded_datasets) + b"\n"
    recorded_coverage = _validated_coverage(coverage)
    source = _source_provenance()
    limitations = [
        item["reason"] for item in recorded_coverage.values() if item["status"] != "passed"
    ]
    if raw_reason is not None:
        limitations.append(raw_reason)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source": source,
        "environment": {
            "os": platform.system(),
            "arch": platform.machine(),
            "python": platform.python_version(),
        },
        "uv_lock_digest": _sha256((_PROJECT_ROOT / "uv.lock").read_bytes()),
        "datasets": datasets,
        "config": _redact_config(config),
        "command": _redact_command(command),
        "cwd": str(_PROJECT_ROOT),
        "timestamps": {"created_at": created_at, "finalized_at": _utc_now()},
        "coverage": recorded_coverage,
        "limitations": limitations,
        "raw": {"reason": raw_reason, "status": raw_status},
    }
    manifest["evidence_level"] = _evidence_level(manifest)
    _validate_manifest(manifest)
    manifest_bytes = _canonical_json(manifest) + b"\n"
    results_bytes = b"".join(result_lines)
    checksums = "".join(
        f"{_sha256(payload)}  {name}\n"
        for name, payload in (
            ("datasets.json", datasets_bytes),
            ("manifest.json", manifest_bytes),
            ("results.jsonl", results_bytes),
        )
    ).encode()

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        (temporary / "datasets.json").write_bytes(datasets_bytes)
        (temporary / "manifest.json").write_bytes(manifest_bytes)
        (temporary / "results.jsonl").write_bytes(results_bytes)
        (temporary / "checksums.sha256").write_bytes(checksums)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest


def _read_regular_file(run_dir: Path, name: str) -> bytes:
    path = run_dir / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"bundle contract file is not a regular file: {name}")
    return path.read_bytes()


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("checksum manifest must be ASCII") from exc
    matches = list(_CHECKSUM_LINE.finditer(text))
    if "".join(match.group(0) for match in matches) != text:
        raise ValueError("checksum manifest is malformed")

    parsed: dict[str, str] = {}
    for match in matches:
        digest, name = match.groups()
        if name in parsed:
            raise ValueError("checksum manifest contains a duplicate entry")
        if name not in _CHECKSUM_TARGETS:
            raise ValueError("checksum manifest contains an invalid path")
        parsed[name] = digest
    if tuple(parsed) != _CHECKSUM_TARGETS:
        raise ValueError("checksum manifest must cover contract files in canonical order")
    return parsed


def _load_canonical_json(payload: bytes, name: str) -> object:
    if not payload.endswith(b"\n") or payload == b"\n":
        raise ValueError(f"{name} is not canonical JSON")
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} contains invalid JSON") from exc
    if not hmac.compare_digest(_canonical_json(decoded) + b"\n", payload):
        raise ValueError(f"{name} is not canonical JSON")
    return decoded


def verify_bundle(run_dir: str | os.PathLike[str]) -> dict[str, object]:
    """Verify bundle shape, canonical encoding, checksums, and evidence cap."""

    directory = Path(run_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("bundle path must be a regular directory")
    entries = tuple(sorted(entry.name for entry in os.scandir(directory)))
    if entries != _CONTRACT_FILES:
        raise ValueError("bundle must contain exactly the four contract files")

    payloads = {name: _read_regular_file(directory, name) for name in _CONTRACT_FILES}
    checksums = _parse_checksums(payloads["checksums.sha256"])
    for name in _CHECKSUM_TARGETS:
        if not hmac.compare_digest(_sha256(payloads[name]), checksums[name]):
            raise ValueError(f"checksum mismatch for {name}")

    manifest = _load_canonical_json(payloads["manifest.json"], "manifest.json")
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be a JSON object")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
    _validate_manifest(manifest)
    embedded_datasets = _load_canonical_json(payloads["datasets.json"], "datasets.json")
    if not isinstance(embedded_datasets, list) or any(
        not isinstance(dataset, dict) for dataset in embedded_datasets
    ):
        raise ValueError("datasets.json must contain a list of dataset objects")
    recomputed_datasets = _validated_datasets(embedded_datasets)
    if manifest.get("datasets") != recomputed_datasets:
        raise ValueError("manifest dataset provenance is inconsistent with datasets.json")
    result_lines = payloads["results.jsonl"].splitlines(keepends=True)
    if not result_lines:
        raise ValueError("at least one benchmark result is required")
    for line in result_lines:
        result = _load_canonical_json(line, "results.jsonl")
        _validate_result(result)
    return manifest
