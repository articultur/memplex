"""V3 pair authority, real old-reader rejection, and detached physical reads."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from memplex.auth import AuthorizationContext, Principal
from memplex.models import Fact, Function, Observation, Preference
from memplex.source_codec import decode_ledger, encode_envelope, encode_ledger, semantic_digest
from memplex.source_contract import (
    ALGORITHM_VERSION,
    CARRIER_KEY,
    AuthorityScope,
    PolicySnapshot,
    SourceClass,
    SourceContractError,
    SourceHead,
    SourceKey,
    SourceKind,
    SourceLedger,
    SourceRef,
    SourceState,
    SourceToken,
)
from memplex.storage.lite.durability import LiteDurability, LitePair, LiteStorageIntegrityError
from memplex.storage.lite.source_versions import LiteSourceVersionStore, _payload_locator
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceAdmission,
    SourceValidationHooks,
    _issue_admission,
)
from tests.source_v1_support import AUTHORITY_ID, INCARNATION, fact_envelope, source_snapshot

SCOPE = AuthorityScope(AUTHORITY_ID, "t1", "w1")
POLICY = PolicySnapshot(1, (), frozenset({ALGORITHM_VERSION}))
CONTEXT = AuthorizationContext(Principal("t1", "alice", authentication_id="auth"), "w1")
HOOKS = SourceValidationHooks(lambda snapshot, context: True, lambda snapshot: True)
OLD_COMMIT = "826335024c0b67b1b25fdb477596bf08ddeb7dad"
OLD_TREE = "7fd32f681a1a2cc9044823b672aaacfa96e63ef6"


def new_bank(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    backend = store.source_versions()
    admission = _issue_admission(store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    backend.initialize_authority(SCOPE, POLICY, admission=admission)
    return store, backend


def fixture_pair(store, *, raw=False):
    """Construct an authenticated format fixture, never a production writer."""
    base = store._durability.load_authoritative()
    ledger = decode_ledger(base.memory["source_validity"])
    if raw:
        payload = {"id": "physical_1", "raw_text": "evidence", "trust_tier": 1,
                   "created_at": "2026-10-11T00:00:00+00:00", "source": {},
                   "tenant_id": "t1", "workspace_id": "w1", "owner_subject_id": "alice",
                   "visibility": "workspace", "namespace": {}}
        key = SourceKey(AUTHORITY_ID, "t1", "w1", SourceKind.RAW_PARAGRAPH, "logical_1")
        collection = "paragraphs"
    else:
        payload = Fact(id="fact_one", subject="item", predicate="is", object_="present",
                       tenant_id="t1", workspace_id="w1", owner_subject_id="alice",
                       visibility="workspace").to_dict()
        key = SourceKey(AUTHORITY_ID, "t1", "w1", SourceKind.FACT, "fact_one")
        collection = "facts"
    canonical = {**payload, "id": key.logical_id}
    ref = SourceRef(key, SourceToken(INCARNATION, 1, 1), 1,
                    semantic_digest(canonical, input_context={}, root_refs=()))
    head = SourceHead(ref, SourceState.LIVE, SourceClass.DIRECT_INPUT,
                      _payload_locator(key, payload), 1, {}, ())
    memory = copy.deepcopy(base.memory)
    memory[collection] = [payload]
    memory["source_validity"] = encode_ledger(SourceLedger(ledger.authority, {key: head}, {}, {}))
    return LitePair(memory, [], base.generation + 1, "fixture"), key


def write_fixture(store, pair):
    # Deliberately persist even corrupt fixtures with correct pair digests:
    # these tests must exercise the loader, not only the writer validator.
    from memplex.storage.lite.durability import _digest
    common = {"format_version": 3 if pair.memory.get("schema_version") == 3 else 2,
              "generation": pair.generation, "transaction_id": pair.transaction_id}
    memory = {**common, "payload": pair.memory, "peer_digest": _digest(pair.changelog)}
    changelog = {**common, "payload": pair.changelog, "peer_digest": _digest(pair.memory)}
    store._path.write_text(json.dumps(memory))
    store._changelog._path.write_text(json.dumps(changelog))


def fence(backend):
    return backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 250_000_000)


def test_v3_pair_roundtrip_and_v2_stays_unchanged(tmp_path):
    legacy = LiteMemoryStore(tmp_path / "legacy" / "memory.json")
    legacy.add_fact(Fact(id="old", subject="old", predicate="is", object_="stable"))
    original = (legacy._path.read_bytes(), legacy._changelog._path.read_bytes())
    reopened = LiteMemoryStore(legacy._path)
    assert reopened.get_fact("old") is not None
    assert original == (legacy._path.read_bytes(), legacy._changelog._path.read_bytes())
    assert json.loads(original[0])["format_version"] == 2
    store, backend = new_bank(tmp_path / "v3")
    pair, key = fixture_pair(store)
    write_fixture(store, pair)
    with fence(backend) as session:
        assert session.resolve_id("fact_one") == key
        assert session.source(key).payload["object"] == "present"
        assert session.authority().commit_seq == 1
    doc = json.loads(store._path.read_text())
    assert doc["format_version"] == doc["payload"]["schema_version"] == 3
    assert set(doc["payload"]) == set(legacy._raw_memory()) | {"source_validity"}
    assert not list(store._path.parent.glob("*source_validity*"))


def test_old_reader_rejects_v3(tmp_path):
    store, _ = new_bank(tmp_path / "bank")
    git_dir = os.environ.get("MEMPLEX_OLD_READER_GIT_DIR", str(Path(__file__).resolve().parents[1] / ".git"))
    command = ["git", "--git-dir=" + git_dir]
    result = subprocess.run(command + ["rev-parse", OLD_COMMIT + "^{tree}"], capture_output=True, text=True, check=False)
    assert result.returncode == 0, "required fixed old-reader commit unavailable; CI must fetch it explicitly"
    assert result.stdout.strip() == OLD_TREE
    archive = subprocess.check_output(command + ["archive", OLD_COMMIT])
    export = tmp_path / "old-reader"
    export.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(export, filter="data")
    code = """import json, pathlib, sys
from coverage import Coverage
import memplex.storage.lite.store as module
from memplex.storage.lite.durability import LiteStorageIntegrityError
assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[2]).resolve())
assert Coverage.current() is None, 'old export polluted candidate coverage'
try:
    module.LiteMemoryStore(pathlib.Path(sys.argv[1]))
except LiteStorageIntegrityError as exc:
    print(json.dumps({'import': module.__file__, 'rejected': type(exc).__name__, 'reason': str(exc), 'coverage_active': False}))
else:
    raise AssertionError('old reader accepted V3')
"""
    # The historical code tree is external to candidate coverage. pytest-cov
    # documents removing COV_CORE_* to disable only this child's startup.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and key != "COVERAGE_PROCESS_START"}
    env.update(PYTHONPATH=str(export), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run([sys.executable, "-c", code, str(store._path), str(export)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    evidence = {"commit": OLD_COMMIT, "tree": OLD_TREE,
                "archive_sha256": hashlib.sha256(archive).hexdigest(), **json.loads(proc.stdout)}
    print(json.dumps(evidence, sort_keys=True))
    assert evidence["rejected"] == "LiteStorageIntegrityError"


@pytest.mark.parametrize("kind", [Function, Fact, Preference, Observation])
def test_four_models_roundtrip_carrier(kind):
    node = kind(id="node", namespace={CARRIER_KEY: encode_envelope(fact_envelope((source_snapshot("premise").head.ref,)))})
    assert kind.from_dict(node.to_dict()).namespace == node.namespace


@pytest.mark.parametrize("damage", ["missing_ledger", "missing_registration", "carrier", "marker_removed",
                                    "scope", "physical_id", "acl", "kind", "missing_id", "digest"])
def test_carrier_ledger_mismatch_is_quarantined(tmp_path, damage):
    store, backend = new_bank(tmp_path)
    pair, _ = fixture_pair(store)
    row = pair.memory["facts"][0]
    if damage == "missing_ledger":
        del pair.memory["source_validity"]
    elif damage == "missing_registration":
        ledger = decode_ledger(pair.memory["source_validity"])
        pair.memory["source_validity"] = encode_ledger(replace(ledger, sources={}))
    elif damage == "carrier":
        row["namespace"][CARRIER_KEY] = encode_envelope(fact_envelope((source_snapshot("premise").head.ref,)))
    elif damage == "marker_removed":
        pair.memory["schema_version"] = 2
    elif damage == "scope":
        row["tenant_id"] = "other"
    elif damage == "physical_id":
        row["id"] = "other"
    elif damage == "acl":
        row["owner_subject_id"] = "other"
    elif damage == "kind":
        row["memory_type"] = "preference"
    elif damage == "missing_id":
        del row["id"]
    else:
        row["object"] = "changed"
    with pytest.raises((LiteStorageIntegrityError, SourceContractError)):
        write_fixture(store, pair)
        with fence(backend):
            pass


def test_raw_physical_mapping_is_verified_before_logical_projection(tmp_path):
    store, backend = new_bank(tmp_path)
    pair, key = fixture_pair(store, raw=True)
    write_fixture(store, pair)
    with fence(backend) as session:
        snapshot = session.source(key)
        assert snapshot.payload["id"] == "logical_1"
        assert session.resolve_id("physical_1") == key
        assert session.resolve_id("logical_1") == key
        with pytest.raises(TypeError):
            snapshot.payload["raw_text"] = "changed"
    pair.memory["paragraphs"][0]["id"] = "physical_2"
    with pytest.raises((LiteStorageIntegrityError, SourceContractError)):
        write_fixture(store, pair)
        with fence(backend):
            pass


def test_empty_new_bank_admission_is_sealed_and_bound(tmp_path):
    store = LiteMemoryStore(tmp_path / "a" / "memory.json")
    other = LiteMemoryStore(tmp_path / "b" / "memory.json")
    admission = _issue_admission(other._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    for token in [None, {}, object(), admission]:
        with pytest.raises(SourceContractError):
            store.source_versions().initialize_authority(SCOPE, POLICY, admission=token)
    with pytest.raises(SourceContractError):
        SourceAdmission()
    with pytest.raises(SourceContractError):
        _issue_admission(store._durability, SCOPE, HOOKS, issuer=object())
    store.add_fact(Fact(id="existing"))
    token = _issue_admission(store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    with pytest.raises(SourceContractError):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=token)


def test_no_source_mutation_or_publication_is_enabled(tmp_path):
    _, backend = new_bank(tmp_path)
    for operation in [backend.mutate_source, backend.publish_if_current, backend.delete_target,
                      backend.recreate_target]:
        with pytest.raises(SourceContractError):
            operation(CONTEXT, None, **({"expected": None} if operation == backend.delete_target else {"expected_deleted": None} if operation == backend.recreate_target else {}), deadline_ns=time.monotonic_ns() + 10_000_000)


def test_untrusted_hooks_and_legacy_reopen_fail_closed(tmp_path):
    store, _ = new_bank(tmp_path)
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(store._path)
    fresh = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    with pytest.raises(SourceContractError), fence(fresh):
        pass
    fresh._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    with fence(fresh) as session:
        assert session.authority().scope == SCOPE
    with pytest.raises(SourceContractError):
        session.authority()


def test_bootstrap_journal_uses_v3_and_recovery_retains_it(tmp_path, monkeypatch):
    store = LiteMemoryStore(tmp_path / "memory.json")
    backend = store.source_versions()
    token = _issue_admission(store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    def crash():
        raise RuntimeError("crash after durable decision")
    monkeypatch.setattr(store._durability, "after_memory_replace", crash)
    with pytest.raises(RuntimeError, match="crash"):
        backend.initialize_authority(SCOPE, POLICY, admission=token)
    journal = json.loads(store._durability._journal_path.read_text())
    assert journal["format_version"] == 3
    assert journal["target"]["memory"]["schema_version"] == 3
    fresh = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    fresh._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    with fence(fresh) as session:
        assert session.authority().scope == SCOPE
    assert not store._durability._journal_path.exists()
    assert json.loads(store._changelog._path.read_text())["format_version"] == 3
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(store._path)


def test_existing_empty_v2_is_not_a_new_bank(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    with store._durability.writer_lock():
        base = store._durability._load_authoritative_locked()
        store._durability.commit_locked(base, LitePair(base.memory, [], 1, "persisted-v2"))
    before = store._path.read_bytes()
    token = _issue_admission(store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    with pytest.raises(SourceContractError):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=token)
    assert store._path.read_bytes() == before


def test_v3_downgrade_and_raw_envelope_stripping_are_rejected(tmp_path):
    store, _ = new_bank(tmp_path)
    base = store._durability.load_authoritative()
    memory = copy.deepcopy(base.memory)
    memory.pop("source_validity")
    memory["schema_version"] = 2
    with store._durability.writer_lock(), pytest.raises(LiteStorageIntegrityError):
        store._durability.commit_locked(base, LitePair(memory, [], 2, "downgrade"))
    with pytest.raises(LiteStorageIntegrityError):
        store._durability._decode_pair(base.memory, [])


def test_read_fence_checks_expected_authority_and_scope(tmp_path):
    store, backend = new_bank(tmp_path)
    wrong = replace(SCOPE, authority_id="66666666-6666-4666-8666-666666666666")
    fresh = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    fresh._install_hooks(HOOKS, scope=wrong, issuer=_RESOURCE_ISSUER)
    with pytest.raises(SourceContractError), fence(fresh):
        pass
    with pytest.raises(SourceContractError), backend.read_fence(
        replace(CONTEXT, workspace_id="other"), deadline_ns=time.monotonic_ns() + 250_000_000
    ):
        pass


def test_read_deadline_is_checked_and_session_does_not_escape(tmp_path, monkeypatch):
    from memplex.source_contract import SourceBusyError
    from memplex.storage.lite import source_versions as implementation
    _, backend = new_bank(tmp_path)
    now = [10_000_000_000]
    monkeypatch.setattr(implementation.time, "monotonic_ns", lambda: now[0])
    with pytest.raises(SourceBusyError), backend.read_fence(CONTEXT, deadline_ns=now[0]):
        pass
    with backend.read_fence(CONTEXT, deadline_ns=now[0] + 10_000_000_000) as session:
        now[0] += 250_000_000
        with pytest.raises(SourceBusyError):
            session.authority()
    with pytest.raises(SourceContractError):
        session.resolve_id("unknown")


@pytest.mark.parametrize("mode", ["read", "rw"])
def test_sqlite_authority_cannot_fallback_from_v3(tmp_path, monkeypatch, mode):
    store, _ = new_bank(tmp_path)
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", mode)
    assert store.source_versions() is None
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(store._path)


def test_exact_adapter_and_seals_are_not_wrapper_capabilities(tmp_path):
    import pickle

    from memplex.storage.source_versions import SourceCapability
    class Wrapper(LiteMemoryStore):
        pass
    assert Wrapper(tmp_path / "wrapper" / "memory.json").source_versions() is None
    with pytest.raises(SourceContractError):
        LiteSourceVersionStore(object())
    store, _ = new_bank(tmp_path / "bank")
    token = _issue_admission(store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER)
    with pytest.raises((TypeError, AttributeError)):
        pickle.dumps(token)
    with pytest.raises(AttributeError):
        token._scope = SCOPE
    capability = SourceCapability(issuer=_RESOURCE_ISSUER, resource=store._durability,
                                  backend="lite_json", scope=SCOPE, contract_version=1,
                                  state=SourceState.LIVE)
    assert (capability.backend, capability.scope, capability.contract_version, capability.state) == (
        "lite_json", SCOPE, 1, SourceState.LIVE)
    with pytest.raises(AttributeError):
        capability.scope = SCOPE


def derived_pair(store):
    from memplex.source_codec import output_digest
    from memplex.source_contract import PublicationRecord, PublicationState, TargetHead, TargetToken
    from tests.source_v1_support import PUBLICATION_ID, REQUEST_ID, TARGET_INCARNATION
    pair, source_key = fixture_pair(store, raw=True)
    ledger = decode_ledger(pair.memory["source_validity"])
    source = ledger.sources[source_key]
    row = Fact(id=PUBLICATION_ID, subject="summary", predicate="is", object_="derived",
               tenant_id="t1", workspace_id="w1", owner_subject_id="alice",
               visibility="workspace", trust_tier=1).to_dict()
    envelope = replace(fact_envelope((source.ref,)), output_digest=output_digest(row))
    row["namespace"][CARRIER_KEY] = encode_envelope(envelope)
    key = SourceKey(AUTHORITY_ID, "t1", "w1", SourceKind.FACT, PUBLICATION_ID)
    ref = SourceRef(key, SourceToken(INCARNATION, 1, 1), 1,
                    semantic_digest(row, input_context={}, root_refs=()))
    head = SourceHead(ref, SourceState.LIVE, SourceClass.DERIVED_V1,
                      _payload_locator(key, row), 1, {}, ())
    token = TargetToken(TARGET_INCARNATION, 1, PUBLICATION_ID)
    target = TargetHead(envelope.derivation_key, token, SourceState.LIVE, 1)
    publication = PublicationRecord(PUBLICATION_ID, envelope.derivation_key, token,
        PublicationState.ACTIVE, hashlib.sha256(encode_envelope(envelope).encode()).hexdigest(),
        envelope.output_digest, (source.ref,), ALGORITHM_VERSION, REQUEST_ID, "a" * 64, "b" * 64, 1, 1)
    pair.memory["facts"] = [row]
    pair.memory["source_validity"] = encode_ledger(SourceLedger(
        ledger.authority, {source_key: source, key: head}, {target.derivation_key: target},
        {PUBLICATION_ID: publication}))
    return pair, key


def test_registered_derived_fact_roundtrips_and_passes_pure_validator(tmp_path):
    from memplex.source_contract import ValidationLimits
    from memplex.source_validation import validate_sources
    store, backend = new_bank(tmp_path)
    pair, key = derived_pair(store)
    write_fixture(store, pair)
    with fence(backend) as session:
        snapshot = session.source(key)
        assert CARRIER_KEY in snapshot.payload["namespace"]
        result = validate_sources(session, [key.logical_id], authorization=CONTEXT,
                                  own_acl=HOOKS.own_acl, safe_for_model=HOOKS.safe_for_model,
                                  limits=ValidationLimits(), deadline_ns=time.monotonic_ns() + 250_000_000)
        assert set(result.accepted) == {key.logical_id}


@pytest.mark.parametrize("damage", ["carrier_missing", "registration_missing", "publication_missing",
                                    "body_missing", "envelope_digest", "output_digest", "target_missing"])
def test_persisted_derived_body_and_registration_reconcile(tmp_path, damage):
    store, backend = new_bank(tmp_path)
    pair, key = derived_pair(store)
    ledger = decode_ledger(pair.memory["source_validity"])
    if damage == "carrier_missing":
        pair.memory["facts"][0]["namespace"].pop(CARRIER_KEY)
    elif damage == "registration_missing":
        ledger = replace(ledger, sources={k: h for k, h in ledger.sources.items() if k != key})
    elif damage == "body_missing":
        pair.memory["facts"] = []
    elif damage in {"envelope_digest", "output_digest"}:
        record = ledger.publications[key.logical_id]
        ledger = replace(ledger, publications={key.logical_id: replace(record, **{damage: "f" * 64})})
    else:
        # Raw wire corruption may itself violate SourceLedger construction;
        # persist the invalid mapping to verify loader quarantine.
        pair.memory["source_validity"]["publications" if damage == "publication_missing" else "targets"] = []
    if damage not in {"publication_missing", "target_missing"}:
        pair.memory["source_validity"] = encode_ledger(ledger)
    write_fixture(store, pair)
    before = (store._path.read_bytes(), store._changelog._path.read_bytes())
    with pytest.raises((LiteStorageIntegrityError, SourceContractError)), fence(backend):
        pass
    assert before == (store._path.read_bytes(), store._changelog._path.read_bytes())


def _write_alternate_authority(path):
    from memplex.storage.lite.durability import _empty_sync_state
    from memplex.storage.lite.sqlite_v2 import ShadowSqliteWriter
    writer = ShadowSqliteWriter(path.parent / "shadow_v2.sqlite3")
    try:
        assert writer.flush_state({"fact": [Fact(id="alternate_fact").to_dict()]}, [],
                                  generation=1, sync_state=_empty_sync_state())
    finally:
        writer.close()


@pytest.mark.parametrize("mode", ["read", "rw"])
def test_v3_durable_journal_cannot_be_bypassed_by_sqlite(tmp_path, monkeypatch, mode):
    store = LiteMemoryStore(tmp_path / "memory.json")
    def crash():
        raise RuntimeError("durable V3 decision")
    monkeypatch.setattr(store._durability, "after_journal_rename_and_parent_dir_fsync", crash)
    with pytest.raises(RuntimeError, match="durable V3"):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
            store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER))
    assert not store._path.exists()
    assert json.loads(store._durability._journal_path.read_text())["format_version"] == 3
    _write_alternate_authority(store._path)
    before = store._durability._journal_path.read_bytes()
    monkeypatch.setenv("MEMPLEX_LITE_SQLITE_AUTHORITY", mode)
    with pytest.raises(LiteStorageIntegrityError):
        LiteMemoryStore(store._path)
    assert store._durability._journal_path.read_bytes() == before


def test_alternate_sqlite_authority_disqualifies_empty_bank_admission(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    _write_alternate_authority(store._path)
    with pytest.raises(SourceContractError):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
            store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER))
    assert not store._path.exists()
    assert not store._durability._journal_path.exists()


def test_query_cache_does_not_disqualify_empty_bank_admission(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    # This is the existing rebuildable FTS cache, not the SQLite write authority.
    cache = tmp_path / "memory.json.fts5.db"
    cache.write_bytes(b"rebuildable-query-cache")
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
        store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER))
    assert json.loads(store._path.read_text())["format_version"] == 3
    assert cache.read_bytes() == b"rebuildable-query-cache"


def test_session_visibility_keeps_existing_acl_semantics(tmp_path):
    from memplex.authorization import is_own_node_visible, source_authorization_node
    from memplex.source_contract import ValidationLimits
    from memplex.source_validation import validate_sources
    store, backend = new_bank(tmp_path)
    pair, key = fixture_pair(store)
    row = pair.memory["facts"][0]
    row.update(visibility="session", origin_session="session-1", provenance={"agent_id": "agent-1"})
    ledger = decode_ledger(pair.memory["source_validity"])
    head = ledger.sources[key]
    head = replace(head, payload_locator=_payload_locator(key, row))
    pair.memory["source_validity"] = encode_ledger(replace(ledger, sources={key: head}))
    write_fixture(store, pair)
    context = replace(CONTEXT, agent_id="agent-1", session_id="session-1")
    def own(snapshot, actual):
        return is_own_node_visible(source_authorization_node(snapshot), actual)
    with backend.read_fence(context, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        snapshot = session.source(key)
        assert own(snapshot, context)
        result = validate_sources(session, [key.logical_id], authorization=context, own_acl=own,
                                  safe_for_model=HOOKS.safe_for_model, limits=ValidationLimits(),
                                  deadline_ns=time.monotonic_ns() + 250_000_000)
        assert result.accepted
        for wrong in [replace(context, principal=replace(context.principal, subject_id="other")),
                      replace(context, workspace_id="other"), replace(context, agent_id="other"),
                      replace(context, session_id="other")]:
            assert not own(snapshot, wrong)
            result = validate_sources(session, [key.logical_id], authorization=wrong, own_acl=own,
                                      safe_for_model=HOOKS.safe_for_model, limits=ValidationLimits(),
                                      deadline_ns=time.monotonic_ns() + 250_000_000)
            assert not result.accepted


def test_expiry_during_first_read_prevents_next_io(tmp_path, monkeypatch):
    from memplex.source_contract import SourceBusyError
    from memplex.storage.lite import source_versions as implementation
    store, backend = new_bank(tmp_path)
    now = [10_000_000_000]
    reads = []
    real_read = store._durability._read
    def read(path):
        reads.append(path.name)
        value = real_read(path)
        now[0] += 300_000_000
        return value
    monkeypatch.setattr(implementation.time, "monotonic_ns", lambda: now[0])
    monkeypatch.setattr(store._durability, "_read", read)
    with pytest.raises(SourceBusyError), backend.read_fence(CONTEXT, deadline_ns=now[0] + 250_000_000):
        pass
    assert reads == ["memory.json"]


def test_expiry_during_reconciliation_stops_next_source(tmp_path, monkeypatch):
    from memplex.source_contract import SourceBusyError
    from memplex.storage.lite import source_versions as implementation
    store, backend = new_bank(tmp_path)
    pair, _ = derived_pair(store)
    write_fixture(store, pair)
    now = [10_000_000_000]
    calls = []
    real_project = implementation._canonical_payload
    def project(head, row):
        calls.append(head.ref.key)
        result = real_project(head, row)
        now[0] += 300_000_000
        return result
    monkeypatch.setattr(implementation.time, "monotonic_ns", lambda: now[0])
    monkeypatch.setattr(implementation, "_canonical_payload", project)
    with pytest.raises(SourceBusyError), backend.read_fence(CONTEXT, deadline_ns=now[0] + 250_000_000):
        pass
    assert len(calls) == 1


def test_pure_validator_import_does_not_initialize_storage(tmp_path):
    code = """import importlib, json, pathlib, sys, types
root = pathlib.Path(sys.argv[1])
package = types.ModuleType('memplex')
package.__path__ = [str(root / 'memplex')]
sys.modules['memplex'] = package
importlib.import_module('memplex.source_validation')
forbidden = [name for name in sys.modules if name == 'memplex.storage' or name.startswith(('memplex.storage.', 'memplex.service', 'memplex.adapters'))]
assert not forbidden, forbidden
print(json.dumps({'forbidden_modules': forbidden}))
"""
    proc = subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1])],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_recovery_timeout_keeps_durable_decision_for_next_fence(tmp_path, monkeypatch):
    from memplex.source_contract import SourceBusyError
    from memplex.storage.lite import durability as implementation
    store = LiteMemoryStore(tmp_path / "memory.json")
    def crash():
        raise RuntimeError("durable decision before pair replacement")
    monkeypatch.setattr(store._durability, "after_journal_rename_and_parent_dir_fsync", crash)
    with pytest.raises(RuntimeError, match="durable decision"):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
            store._durability, SCOPE, HOOKS, issuer=_RESOURCE_ISSUER))
    journal = store._durability._journal_path.read_bytes()
    backend = LiteSourceVersionStore(LiteDurability(store._path, store._changelog._path))
    backend._install_hooks(HOOKS, scope=SCOPE, issuer=_RESOURCE_ISSUER)
    now = [10_000_000_000]
    replacements = []
    real_replace = implementation._replace_and_fsync
    def replace_slowly(path, document):
        replacements.append(path.name)
        real_replace(path, document)
        now[0] += 300_000_000
    with monkeypatch.context() as stalled:
        stalled.setattr(implementation.time, "monotonic_ns", lambda: now[0])
        stalled.setattr(implementation, "_replace_and_fsync", replace_slowly)
        with pytest.raises(SourceBusyError), backend.read_fence(CONTEXT, deadline_ns=now[0] + 250_000_000):
            pass
    assert replacements == ["memory.json"]
    assert not store._changelog._path.exists()
    assert store._durability._journal_path.read_bytes() == journal
    with fence(backend) as session:
        assert session.authority().scope == SCOPE
    assert not store._durability._journal_path.exists()
    assert json.loads(store._changelog._path.read_text())["format_version"] == 3
