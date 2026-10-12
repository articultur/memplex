"""Strict, deterministic source validity V1 wire codecs and digest projections.

These functions perform no I/O and confer no authority. A carrier is a claim
that later storage and validation tasks must compare with the real ledger.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from types import UnionType
from typing import Any, cast, get_args, get_origin, get_type_hints

from memplex.source_contract import (
    ALGORITHM_VERSIONS,
    CARRIER_KEY,
    CONTRACT_VERSION,
    DIGEST_VERSION,
    PRODUCER,
    AuthorityState,
    DerivationEnvelope,
    PublicationRecord,
    RejectionCode,
    SourceContractError,
    SourceHead,
    SourceLedger,
    SourceRef,
    TargetHead,
    _check,
    _freeze_strings,
    _payload,
    _refs,
)

# Only these known bookkeeping fields are excluded at the payload root. A
# business object's identically named nested field remains semantic.
_OPERATIONAL_FIELDS = frozenset(
    {
        "access_count",
        "last_accessed_at",
        "created_at",
        "updated_at",
        "version",
        "observation_count",
        "last_observed_at",
        "sync_version",
        "transport_version",
        "storage_generation",
        "content_hash",
    }
)
_ROOT_IDENTITY_FIELDS = frozenset(
    {
        "id",
        "authority_id",
        "tenant_id",
        "workspace_id",
        "owner",
        "owner_subject_id",
        "principal_id",
        "authentication_id",
        "agent_id",
        "session_id",
        "origin_session",
        "visibility",
        "roles",
        "grants",
        "grant",
    }
)
# These aliases are identity claims only in the binder's declared locations.
_NAMESPACE_IDENTITY_FIELDS = frozenset(
    {"memplex_tenant_id", "memplex_subject_id", "memplex_workspace_id"}
)
_PROVENANCE_IDENTITY_FIELDS = frozenset(
    {"agent_id", "authentication_id", "request_id", "session_id"}
)
_PUBLICATION_FIELDS = frozenset(
    {
        "output_digest",
        "output_digest_version",
        "publication_id",
        "target_incarnation",
        "target_generation",
        "derivation_key",
        "envelope_digest",
        "request_id",
        "request_digest",
        "manifest_digest",
        "observed_commit_seq",
        "commit_seq",
        "last_commit_seq",
        "registration",
    }
)
_TIME_FIELDS = frozenset(
    {
        "valid_from",
        "valid_until",
        "invalid_at",
        "needs_review_until",
        "expires_at",
        "observed_at",
        "reference_datetime",
    }
)


def _wire(value: object) -> object:
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: _wire(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_wire(item) for item in cast(tuple[object, ...], value)]
    if type(value) is frozenset:
        return [_wire(item) for item in sorted(cast(frozenset[str], value))]
    return value


def _canonical(value: object) -> str:
    try:
        result = json.dumps(
            _wire(value), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        result.encode("utf-8")
        return result
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SourceContractError() from exc


def _strict_object(value: object, expected: set[str]) -> Mapping[str, object]:
    _check(isinstance(value, Mapping))
    obj = cast(Mapping[str, object], value)
    _check(all(type(key) is str for key in obj) and set(obj) == expected)
    return obj


def _decode_value(annotation: Any, value: object) -> Any:
    """Decode the closed DTO schema without Python's bool/int coercion."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is UnionType:
        for option in args:
            try:
                return _decode_value(option, value)
            except SourceContractError:
                continue
        raise SourceContractError()
    if origin in (tuple, frozenset):
        _check(type(value) is list)
        items = cast(list[object], value)
        decoded = [_decode_value(args[0], item) for item in items]
        if origin is frozenset:
            _check(len(set(decoded)) == len(decoded))
            return frozenset(decoded)
        return tuple(decoded)
    if origin is Mapping:
        _check(isinstance(value, Mapping))
        return {
            _decode_value(args[0], key): _decode_value(args[1], item)
            for key, item in cast(Mapping[object, object], value).items()
        }
    if annotation is object:
        # Detachment and finite-JSON validation remain the DTO's responsibility.
        return value
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        _check(type(value) is str)
        try:
            return annotation(cast(str, value))
        except ValueError as exc:
            raise SourceContractError() from exc
    if is_dataclass(annotation):
        names = {field.name for field in fields(annotation)}
        obj = _strict_object(value, names)
        hints = get_type_hints(annotation)
        return cast(Any, annotation)(
            **{name: _decode_value(hints[name], item) for name, item in obj.items()}
        )
    _check(type(value) is annotation)
    return value


def _pairs(items: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in items:
        _check(key not in result)
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise SourceContractError()


def _parse(value: str) -> object:
    _check(type(value) is str)
    try:
        _check(len(value.encode("utf-8")) <= 65536, RejectionCode.BUDGET_EXHAUSTED)
        return json.loads(value, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, SourceContractError):
            raise
        raise SourceContractError() from exc


def encode_envelope(value: DerivationEnvelope) -> str:
    _check(type(value) is DerivationEnvelope)
    encoded = _canonical(value)
    _check(len(encoded.encode("utf-8")) <= 65536, RejectionCode.BUDGET_EXHAUSTED)
    return encoded


def decode_envelope(value: str) -> DerivationEnvelope:
    return cast(DerivationEnvelope, _decode_value(DerivationEnvelope, _parse(value)))


def envelope_digest(value: DerivationEnvelope) -> str:
    """Bind the complete envelope, including its separately computed output digest."""
    return hashlib.sha256(encode_envelope(value).encode("utf-8")).hexdigest()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _check_times(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _TIME_FIELDS and item is not None:
                _check(type(item) is str)
                try:
                    at = datetime.fromisoformat(cast(str, item))
                except ValueError as exc:
                    raise SourceContractError() from exc
                _check(at.tzinfo is not None and at.utcoffset() == timedelta(0))
            _check_times(item)
    elif type(value) in (tuple, list):
        for item in cast(tuple[object, ...], value):
            _check_times(item)


def _validate_protocol(payload: Mapping[str, object]) -> None:
    for key, value in payload.items():
        if key in ("contract_version", "digest_version", "output_digest_version"):
            _check(type(value) is int and value == 1, RejectionCode.UNSUPPORTED_CAPABILITY)
        if key == "algorithm_version":
            _check(
                type(value) is str and value in ALGORITHM_VERSIONS,
                RejectionCode.UNSUPPORTED_CAPABILITY,
            )
        if key == "producer":
            _check(type(value) is str and value == PRODUCER, RejectionCode.UNSUPPORTED_CAPABILITY)
        _check(
            not key.startswith(("memplex_source_contract", "memplex_derivation_"))
            and key != "source_validity",
            RejectionCode.UNSUPPORTED_CAPABILITY,
        )


def _project_nested(payload: Mapping[str, object], *, output: bool) -> dict[str, object]:
    result = dict(payload)
    for container in ("namespace", "provenance"):
        if container not in result:
            continue
        metadata = _freeze_strings(result[container])
        _validate_protocol({key: value for key, value in metadata.items() if key != CARRIER_KEY})
        identity_fields = (
            _NAMESPACE_IDENTITY_FIELDS if container == "namespace" else _PROVENANCE_IDENTITY_FIELDS
        )
        clean: dict[str, object] = {
            key: value for key, value in metadata.items() if key not in identity_fields
        }
        if container == "namespace" and CARRIER_KEY in metadata:
            if output:
                clean.pop(CARRIER_KEY, None)
            else:
                # Equivalent set order/JSON whitespace has the same semantic meaning.
                clean[CARRIER_KEY] = _wire(decode_envelope(metadata[CARRIER_KEY]))
        if clean:
            result[container] = clean
        else:
            result.pop(container)
    return result


def _project(payload: Mapping[str, object], *, output: bool) -> Mapping[str, object]:
    frozen = _payload(payload)
    _validate_protocol(frozen)
    excluded = _OPERATIONAL_FIELDS | _ROOT_IDENTITY_FIELDS
    if output:
        excluded |= _PUBLICATION_FIELDS
    projected = {key: value for key, value in frozen.items() if key not in excluded}
    projected = _project_nested(projected, output=output)
    _check_times(projected)
    return projected


def semantic_digest(
    payload: Mapping[str, object],
    *,
    input_context: Mapping[str, str],
    root_refs: tuple[SourceRef, ...],
) -> str:
    context = _freeze_strings(input_context)
    _check_times(context)
    return _hash(
        {
            "digest_version": DIGEST_VERSION,
            "payload": _project(payload, output=False),
            "input_context": context,
            "root_refs": _refs(root_refs),
        }
    )


def output_digest(payload: Mapping[str, object]) -> str:
    """Hash outward content separately from the carrier and publication claims."""
    return _hash({"digest_version": DIGEST_VERSION, "payload": _project(payload, output=True)})


def encode_ledger(value: SourceLedger) -> Mapping[str, object]:
    _check(type(value) is SourceLedger)
    # Arrays provide an unambiguous representation for non-string SourceKey keys.
    return cast(
        Mapping[str, object],
        _wire(
            {
                "contract_version": CONTRACT_VERSION,
                "authority": value.authority,
                "sources": [
                    head
                    for _, head in sorted(
                        value.sources.items(),
                        key=lambda item: (
                            item[0].authority_id,
                            item[0].tenant_id,
                            item[0].workspace_id,
                            item[0].kind.value,
                            item[0].logical_id,
                        ),
                    )
                ],
                "targets": [head for _, head in sorted(value.targets.items())],
                "publications": [record for _, record in sorted(value.publications.items())],
            }
        ),
    )


def decode_ledger(value: Mapping[str, object]) -> SourceLedger:
    obj = _strict_object(
        value, {"contract_version", "authority", "sources", "targets", "publications"}
    )
    _check(
        type(obj["contract_version"]) is int and obj["contract_version"] == CONTRACT_VERSION,
        RejectionCode.UNSUPPORTED_CAPABILITY,
    )
    authority = cast(AuthorityState, _decode_value(AuthorityState, obj["authority"]))
    sources = cast(tuple[SourceHead, ...], _decode_value(tuple[SourceHead, ...], obj["sources"]))
    targets = cast(tuple[TargetHead, ...], _decode_value(tuple[TargetHead, ...], obj["targets"]))
    records = cast(
        tuple[PublicationRecord, ...],
        _decode_value(tuple[PublicationRecord, ...], obj["publications"]),
    )
    _check(len({head.ref.key for head in sources}) == len(sources))
    _check(len({head.derivation_key for head in targets}) == len(targets))
    _check(len({record.publication_id for record in records}) == len(records))
    return SourceLedger(
        authority,
        {head.ref.key: head for head in sources},
        {head.derivation_key: head for head in targets},
        {record.publication_id: record for record in records},
    )
