"""Task12 real native admission and legacy isolation regressions."""
from __future__ import annotations

import pytest

from memplex.config import MemplexConfig
from memplex.models import Fact
from memplex.service import MemplexService
from memplex.source_contract import SourceContractError
from memplex.storage.lite.durability import LiteStorageIntegrityError
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.source_versions import (
    _RESOURCE_ISSUER,
    SourceValidationHooks,
    _issue_admission,
)
from memplex.sync import SyncableStore
from tests.test_source_v1_pg_transactions import POLICY, SCOPE


def native_v1(path):
    store = LiteMemoryStore(path / "memory.json")
    backend = store.source_versions()
    hooks = SourceValidationHooks(lambda snapshot, context: True, lambda snapshot: True)
    admission = _issue_admission(store._durability, SCOPE, hooks, issuer=_RESOURCE_ISSUER)
    backend.initialize_authority(SCOPE, POLICY, admission=admission)
    return store


def legacy_config(path):
    config = MemplexConfig()
    config.storage.path = str(path)
    config.embedding.model = "tfidf"
    config.llm.provider = "rule-based"
    config.llm.factual_capture = False
    config.llm.query_enhancement = False
    config.embedding.hyde_enabled = False
    config.wiki.enabled = False
    config.sleep_time.enabled = False
    return config


def test_off_cannot_serve_existing_v1_bank(tmp_path):
    native_v1(tmp_path)
    service = None
    try:
        with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
            service = MemplexService(legacy_config(tmp_path))
    finally:
        if service is not None:
            service.stop()


def test_off_legacy_bank_remains_usable(tmp_path):
    service = MemplexService(legacy_config(tmp_path))
    try:
        service.store.add_fact(Fact(id="legacy", subject="tea", predicate="is", object_="warm"))
        assert service.get("legacy").object_ == "warm"
    finally:
        service.stop()


def test_old_wrapper_cannot_delegate_capability(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    wrapper = SyncableStore(store)
    assert wrapper.source_versions() is None


def test_unregistered_writer_rejected(tmp_path):
    store = native_v1(tmp_path)
    with pytest.raises(SourceContractError):
        store.add_fact(Fact(id="unregistered", subject="derived", predicate="is", object_="unverified"))
    assert store.get_fact("unregistered") is None


def test_sync_v1_egress_refuses_old_peer(tmp_path):
    store = native_v1(tmp_path)
    with pytest.raises(SourceContractError):
        store.sync_page("old-peer", "reader", None, 10)


def test_wiki_writer_rejected_before_export(tmp_path):
    from memplex.worker import BackgroundWorker

    store = native_v1(tmp_path / "bank")
    config = legacy_config(tmp_path / "bank")
    config.wiki.enabled = True
    config.wiki.dir = str(tmp_path / "wiki")
    worker = BackgroundWorker(store=store, config=config)
    with pytest.raises(SourceContractError):
        worker._handle_compile_wiki({})
    assert not (tmp_path / "wiki").exists()


def test_v1_configuration_is_explicit_and_scope_exact(tmp_path):
    from memplex.config import SourceContractConfig
    from memplex.source_compatibility import _admit_lite, assert_v1_admission

    store = native_v1(tmp_path)
    cfg = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    capability = _admit_lite(store, cfg, issuer=_RESOURCE_ISSUER)
    full = legacy_config(tmp_path)
    full.source_contract = cfg
    assert_v1_admission(full, capability)
    with pytest.raises(SourceContractError):
        assert_v1_admission(legacy_config(tmp_path), capability)
    full.source_contract = SourceContractConfig('v1', SCOPE.authority_id, 'other', SCOPE.workspace_id)
    with pytest.raises(SourceContractError):
        assert_v1_admission(full, capability)


def test_trusted_v1_service_cold_open_serves_exact_authority(tmp_path):
    from memplex.config import SourceContractConfig

    native_v1(tmp_path)
    cfg = legacy_config(tmp_path)
    cfg.source_contract = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    service = MemplexService(cfg)
    try:
        assert service.store.source_versions()._scope == SCOPE
    finally:
        service.stop()


@pytest.mark.parametrize('version', [True, 1.0])
def test_lite_malformed_admission_version_cannot_cold_open(tmp_path, version):
    import json

    from memplex.config import SourceContractConfig
    from memplex.source_compatibility import _marker_path
    store = native_v1(tmp_path)
    path = _marker_path(store._durability)
    marker = json.loads(path.read_bytes())
    marker['version'] = version
    path.write_text(json.dumps(marker))
    cfg = legacy_config(tmp_path)
    cfg.source_contract = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    service = None
    try:
        with pytest.raises(LiteStorageIntegrityError) as rejected:
            service = MemplexService(cfg)
        assert isinstance(rejected.value.__cause__, SourceContractError)
        assert rejected.value.__cause__.code == 'quarantined'
    finally:
        if service is not None:
            service.stop()


@pytest.mark.parametrize('setting,value', [('mode', 'V1'), ('authority_id', 'unknown')])
def test_invalid_source_configuration_fails_before_runtime(tmp_path, setting, value):
    from memplex.config import SourceContractConfig
    cfg = legacy_config(tmp_path)
    cfg.source_contract = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    setattr(cfg.source_contract, setting, value)
    with pytest.raises(SourceContractError):
        MemplexService(cfg)
    assert not (tmp_path / 'memory.json').exists()


@pytest.mark.parametrize('backend,sync', [('standard', False), ('enterprise', False), ('lite', True)])
def test_v1_rejects_aliases_and_sync_before_open(tmp_path, backend, sync):
    from memplex.config import SourceContractConfig
    cfg = legacy_config(tmp_path)
    cfg.source_contract = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    cfg.storage.backend, cfg.sync.enabled = backend, sync
    with pytest.raises(SourceContractError):
        MemplexService(cfg)
    assert not (tmp_path / 'memory.json').exists()


def test_preexisting_wrapper_cannot_operate_after_native_admission(tmp_path):
    store = LiteMemoryStore(tmp_path / 'memory.json')
    wrapper = SyncableStore(store)
    backend = store.source_versions()
    backend.initialize_authority(SCOPE, POLICY, admission=_issue_admission(
        store._durability, SCOPE, SourceValidationHooks(lambda *a: True, lambda *a: True), issuer=_RESOURCE_ISSUER))
    with pytest.raises(SourceContractError):
        wrapper.get_fact('x')
    assert wrapper.source_versions() is None


@pytest.mark.parametrize('authorized', [False, True])
@pytest.mark.parametrize('v1', [False, True])
def test_audited_sync_facades_reject_before_extraction_but_keep_legacy(tmp_path, authorized, v1):
    from types import SimpleNamespace

    from memplex.worker import BackgroundWorker
    from tests.test_source_v1_pg_transactions import CONTEXT
    store = LiteMemoryStore(tmp_path / 'memory.json')
    wrapper = SyncableStore(store)
    facade = wrapper.authorized(CONTEXT) if authorized else wrapper
    if v1:
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=_issue_admission(
            store._durability, SCOPE, SourceValidationHooks(lambda *a: True, lambda *a: True), issuer=_RESOURCE_ISSUER))
    called = []
    def extract(source):
        called.append(source)
        return SimpleNamespace(functions=[], graph=SimpleNamespace(edges=[]))
    worker = BackgroundWorker(store=facade, config=legacy_config(tmp_path), engine=SimpleNamespace(extract=extract))
    if v1:
        with pytest.raises(SourceContractError):
            worker._handle_extract({'content': 'synthetic input'})
        assert not called
    else:
        assert worker._handle_extract({'content': 'synthetic input'})['status'] == 'completed'
        assert len(called) == 1


@pytest.mark.parametrize('handler', ['_handle_extract', '_handle_compile_wiki', '_handle_compaction', '_handle_refresh_vector'])
def test_worker_producers_reject_before_side_effects(tmp_path, handler):
    from memplex.worker import BackgroundWorker
    store = native_v1(tmp_path)
    worker = BackgroundWorker(store=store, config=legacy_config(tmp_path))
    before = store._durability.load_authoritative()
    with pytest.raises(SourceContractError):
        getattr(worker, handler)({})
    assert store._durability.load_authoritative() == before


def test_sleep_and_archive_compaction_reject_v1(tmp_path):
    from types import SimpleNamespace

    from memplex.sleep_time import SleepTimeAgent
    store = native_v1(tmp_path)
    service = SimpleNamespace(store=store)
    with pytest.raises(SourceContractError):
        SleepTimeAgent(service).run_once()
    with pytest.raises(SourceContractError):
        MemplexService.compact(service)


def test_consolidation_and_entity_hubs_cannot_publish_unregistered_derivations(tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import UTC, datetime

    from memplex.consolidation import consolidate
    from memplex.entity_hubs import build_entity_hubs
    from tests.test_entity_hubs import _fake_llm
    from tests.test_source_v1_lite_format import new_bank
    from tests.test_source_v1_lite_transactions import mutate, raw_command
    store, backend = new_bank(tmp_path)
    for index, text in enumerate([
        'Maya practices piano recital every Saturday morning.',
        'Maya practices piano recital every Saturday afternoon.',
        'Maya practices piano recital every Saturday evening.',
    ]):
        command = raw_command(str(index), physical=str(index), text=text)
        payload = dict(command.payload)
        payload.update(source={'session': str(index)}, created_at=f'2026-10-{10+index}T00:00:00+00:00')
        mutate(backend, replace(command, payload=payload))
    before = store._durability.load_authoritative()
    monkeypatch.setenv('MEMPLEX_CONSOLIDATION', '1')
    report = consolidate(store, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert report.clusters and report.note.startswith('commit failed:')
    assert report.promoted == report.evicted == []
    assert store._durability.load_authoritative() == before
    monkeypatch.setenv('MEMPLEX_ENTITY_HUBS', '1')
    with pytest.raises(SourceContractError):
        build_entity_hubs(store, _fake_llm)
    assert store._durability.load_authoritative() == before


@pytest.fixture
def empty_pg_resource(pg_function_dsn):
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources
    resource = PostgresStorageResources._for_source_validity(pg_function_dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    resource.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
    store = PostgresMemoryStore(pg_function_dsn, ready_pool=resource.ready_pool, require_authorization=True)
    admission = resource._install_source_versions(store, SCOPE,
        SourceValidationHooks(lambda *a: True, lambda *a: True), issuer=_RESOURCE_ISSUER)
    try:
        yield resource, store, admission, pg_function_dsn
    finally:
        resource.close()


from tests.test_source_v1_pg_transactions import NATIVE


def _insert_legacy_fact(cursor, memory_id, context):
    """Seed a coherent legacy row without skipping required SQL identity."""
    from psycopg2.extras import Json

    from memplex.auth import bind_node_identity

    fact = bind_node_identity(Fact(id=memory_id, subject='item', predicate='is', object_='legacy'), context)
    cursor.execute(
        'INSERT INTO memplex_facts(id,data,tenant_id,owner_subject,workspace,visibility,source_agent,source_session) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
        (fact.id, Json(fact.to_dict()), context.principal.tenant_id, context.principal.subject_id,
         context.workspace_id, fact.visibility, context.agent_id, context.session_id),
    )


@NATIVE
@pytest.mark.parametrize('policy_revision', [1, 7])
def test_native_pg_real_initialization_and_cold_admission(empty_pg_resource, policy_revision):
    from dataclasses import replace

    from memplex.config import SourceContractConfig
    from memplex.source_compatibility import assert_v1_admission
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources
    resource, store, admission, dsn = empty_pg_resource
    policy = replace(POLICY, revision=policy_revision)
    state = store.source_versions().initialize_authority(SCOPE, policy, admission=admission)
    assert state.scope == SCOPE
    config = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
    with pytest.raises(SourceContractError):
        resource._source_startup(SourceContractConfig(), store, issuer=_RESOURCE_ISSUER)
    cold = PostgresStorageResources._for_source_validity(dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    try:
        cold.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
        reopened = PostgresMemoryStore(dsn, ready_pool=cold.ready_pool, require_authorization=True)
        capability = cold._source_startup(config, reopened, issuer=_RESOURCE_ISSUER)
        full = MemplexConfig(source_contract=config)
        full.storage.backend, full.storage.path = 'postgres', dsn
        assert_v1_admission(full, capability)
        from tests.test_source_v1_pg_transactions import _fence
        with _fence(reopened.source_versions()) as session:
            assert session.authority().policy == policy
            assert session.authority().commit_seq == 1
    finally:
        cold.close()


@NATIVE
def test_native_pg_missing_marker_cannot_open_as_legacy(empty_pg_resource):
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.config import SourceContractConfig
    resource, store, admission, dsn = empty_pg_resource
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('COMMENT ON SCHEMA {} IS NULL').format(sql.Identifier(resource.ready_pool.target.schema)))
    with pytest.raises(psycopg2.errors.CheckViolation):
        resource._source_startup(SourceContractConfig(), store, issuer=_RESOURCE_ISSUER)
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'memplex_source_check_%'")
        assert cursor.fetchone() == (0,)


@NATIVE
@pytest.mark.parametrize('version', [True, 1.0])
def test_native_pg_malformed_admission_version_cannot_reopen(empty_pg_resource, version):
    import json
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.config import SourceContractConfig
    from memplex.source_compatibility import _MARKER_PREFIX
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources, _source_schema_marker
    resource, store, admission, dsn = empty_pg_resource
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        marker = _source_schema_marker(cursor, resource.ready_pool.target.schema)
        marker['version'] = version
        cursor.execute(sql.SQL('COMMENT ON SCHEMA {} IS %s').format(sql.Identifier(resource.ready_pool.target.schema)),
                       (_MARKER_PREFIX + json.dumps(marker),))
    cold = PostgresStorageResources._for_source_validity(dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    try:
        cold.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
        reopened = PostgresMemoryStore(dsn, ready_pool=cold.ready_pool, require_authorization=True)
        config = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
        with pytest.raises(SourceContractError):
            cold._source_startup(config, reopened, issuer=_RESOURCE_ISSUER)
    finally:
        cold.close()


@NATIVE
@pytest.mark.parametrize('handler', ['_handle_compile_wiki', '_handle_extract'])
def test_native_pg_authorized_facade_refuses_before_export_or_model(empty_pg_resource, tmp_path, handler):
    from types import SimpleNamespace

    from memplex.worker import BackgroundWorker
    from tests.test_source_v1_pg_transactions import CONTEXT
    _resource, store, admission, _dsn = empty_pg_resource
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    called = []
    def extract(source):
        called.append(source)
        return SimpleNamespace(functions=[], graph=SimpleNamespace(edges=[]))
    config = legacy_config(tmp_path)
    config.wiki.enabled, config.wiki.dir = True, str(tmp_path / 'wiki')
    worker = BackgroundWorker(store=store.authorized(CONTEXT), config=config,
                              engine=SimpleNamespace(extract=extract))
    with pytest.raises(SourceContractError):
        getattr(worker, handler)({'content': 'synthetic input'})
    assert not called and not (tmp_path / 'wiki').exists()


@NATIVE
def test_native_pg_dual_initializers_different_authorities_serialize(empty_pg_resource):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources
    _resource, store, admission, dsn = empty_pg_resource
    other = PostgresStorageResources._for_source_validity(dsn, issuer=_RESOURCE_ISSUER, capacity=2)
    barrier = Barrier(2)
    second_scope = replace(SCOPE, authority_id='22222222-2222-4222-8222-222222222222')
    try:
        other.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
        second = PostgresMemoryStore(dsn, ready_pool=other.ready_pool, require_authorization=True)
        token = other._install_source_versions(second, second_scope, admission._hooks, issuer=_RESOURCE_ISSUER)
        def initialize(native, scope, seal):
            barrier.wait(timeout=5)
            try:
                return native.source_versions().initialize_authority(scope, POLICY, admission=seal)
            except SourceContractError:
                return None
        with ThreadPoolExecutor(2) as threads:
            results = [threads.submit(initialize, store, SCOPE, admission),
                       threads.submit(initialize, second, second_scope, token)]
            states = [future.result(timeout=10) for future in results]
        assert sum(state is not None for state in states) == 1
    finally:
        other.close()


@NATIVE
def test_native_pg_insert_race_cannot_pass_empty_proof(empty_pg_resource, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import psycopg2

    from memplex.storage import postgres_resources as implementation
    from tests.test_source_v1_pg_transactions import CONTEXT
    _resource, store, admission, dsn = empty_pg_resource
    connection = psycopg2.connect(dsn)
    locked = Event()
    original = implementation._source_target_lock
    def observe(cursor, target, **kwargs):
        original(cursor, target, **kwargs)
        locked.set()
    monkeypatch.setattr(implementation, '_source_target_lock', observe)
    try:
        with connection.cursor() as cursor:
            _insert_legacy_fact(cursor, 'racing-legacy', CONTEXT)
        with ThreadPoolExecutor(1) as threads:
            future = threads.submit(store.source_versions().initialize_authority, SCOPE, POLICY, admission=admission)
            assert locked.wait(3)
            connection.commit()
            with pytest.raises(psycopg2.errors.CheckViolation):
                future.result(timeout=10)
        with connection, connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM memplex_source_authorities')
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT count(*) FROM pg_constraint WHERE conname LIKE 'memplex_source_check_%'")
            assert cursor.fetchone() == (0,)
    finally:
        connection.rollback()
        connection.close()


@NATIVE
def test_native_pg_deleted_legacy_facts_leave_history_that_refuses_admission(empty_pg_resource):
    from contextlib import closing

    import psycopg2

    from memplex.auth import bind_node_identity
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources, _source_schema_marker
    from tests.test_source_v1_pg_transactions import CONTEXT

    resource, store, admission, dsn = empty_pg_resource
    legacy = PostgresStorageResources(dsn)
    try:
        legacy.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
        old = PostgresMemoryStore(dsn, ready_pool=legacy.ready_pool, require_authorization=True)
        fact = bind_node_identity(Fact(id='deleted-legacy', subject='item', predicate='is', object_='legacy'), CONTEXT)
        old.authorized(CONTEXT).add_fact(fact)
        old.authorized(CONTEXT).delete_fact(fact.id)
    finally:
        legacy.close()
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM memplex_facts')
        assert cursor.fetchone() == (0,)
        cursor.execute('SELECT count(*) FROM memplex_changelog')
        history_count = cursor.fetchone()[0]
        assert history_count >= 2
    with pytest.raises(psycopg2.errors.CheckViolation):
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM memplex_source_authorities')
        assert cursor.fetchone() == (0,)
        cursor.execute('SELECT count(*) FROM memplex_changelog')
        assert cursor.fetchone() == (history_count,)
        assert _source_schema_marker(cursor, resource.ready_pool.target.schema) is None
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE connamespace=%s::regnamespace "
                       "AND conname LIKE 'memplex_source_check_%%'", (resource.ready_pool.target.schema,))
        assert cursor.fetchone() == (0,)


@pytest.fixture
def ordinary_pg_owner(pg_function_dsn):
    """Existing test connection changes role; no new credential or LOGIN role."""
    import uuid

    import psycopg2
    from psycopg2 import sql

    from memplex.storage.migrations import PostgresMigrationRunner
    from memplex.storage.migrations._constants import _MANAGED_TABLES
    runner = PostgresMigrationRunner(pg_function_dsn)
    runner.apply()
    target = runner.inspect_target()
    role = 'source_owner_' + uuid.uuid4().hex
    connection = psycopg2.connect(pg_function_dsn)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
            cursor.execute(sql.SQL('ALTER SCHEMA {} OWNER TO {}').format(sql.Identifier(target.schema), sql.Identifier(role)))
            for table in _MANAGED_TABLES:
                cursor.execute(sql.SQL('ALTER TABLE {} OWNER TO {}').format(sql.Identifier(target.schema, table), sql.Identifier(role)))
            cursor.execute('SELECT oid::regprocedure::text FROM pg_proc WHERE pronamespace=%s::regnamespace', (target.schema,))
            for (signature,) in cursor.fetchall():
                cursor.execute(sql.SQL('ALTER FUNCTION {} OWNER TO {}').format(sql.SQL(signature), sql.Identifier(role)))
        owner_dsn = psycopg2.extensions.make_dsn(pg_function_dsn, options=f'-c search_path={target.schema} -c role={role}')
        yield pg_function_dsn, owner_dsn, target
    finally:
        connection.rollback()
        with connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(target.schema)))
            cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
        connection.close()


@NATIVE
@pytest.mark.parametrize('hidden', [False, True])
def test_native_ordinary_owner_global_empty_proof_defeats_force_rls(ordinary_pg_owner, hidden):
    from contextlib import closing
    from dataclasses import replace

    import psycopg2

    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources
    from tests.test_source_v1_pg_transactions import CONTEXT
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    if hidden:
        with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
            foreign = replace(CONTEXT, principal=replace(CONTEXT.principal, tenant_id='foreign'), workspace_id='foreign')
            _insert_legacy_fact(cursor, 'hidden', foreign)
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user')
        assert cursor.fetchone() == (False, False)
        cursor.execute('SELECT count(*) FROM memplex_facts')
        assert cursor.fetchone() == (0,)
    resource = PostgresStorageResources._for_source_validity(admin_dsn, migration_dsn=owner_dsn,
                                                            issuer=_RESOURCE_ISSUER, capacity=2)
    try:
        resource.ensure_ready(VectorCapabilityRequest(0, 'disabled'), 'development')
        store = PostgresMemoryStore(admin_dsn, ready_pool=resource.ready_pool, require_authorization=True)
        token = resource._install_source_versions(store, SCOPE,
            SourceValidationHooks(lambda *a: True, lambda *a: True), issuer=_RESOURCE_ISSUER)
        if hidden:
            with pytest.raises(psycopg2.errors.CheckViolation):
                store.source_versions().initialize_authority(SCOPE, POLICY, admission=token)
        else:
            assert store.source_versions().initialize_authority(SCOPE, POLICY, admission=token).scope == SCOPE
        with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM memplex_source_authorities')
            assert cursor.fetchone() == ((0 if hidden else 1),)
            cursor.execute("SELECT count(*) FROM pg_constraint WHERE connamespace=%s::regnamespace AND conname LIKE 'memplex_source_check_%%'", (target.schema,))
            assert cursor.fetchone() == (0,)
    finally:
        resource.close()


@NATIVE
def test_native_owner_empty_proof_drop_failure_rolls_back_every_constraint(ordinary_pg_owner):
    from contextlib import closing

    import psycopg2

    from memplex.storage.postgres_resources import _source_global_proof
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    with (closing(psycopg2.connect(owner_dsn)) as connection,
          pytest.raises(RuntimeError, match='drop interrupted'), connection, connection.cursor() as cursor):
        class InterruptDrop:
            def execute(self, statement, args=None):
                if hasattr(statement, 'as_string') and 'DROP CONSTRAINT' in statement.as_string(cursor):
                    raise RuntimeError('drop interrupted')
                return cursor.execute(statement, args)
            def fetchall(self):
                return cursor.fetchall()
        _source_global_proof(InterruptDrop(), target.schema, empty=True)
    with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE connamespace=%s::regnamespace AND conname LIKE 'memplex_source_check_%%'", (target.schema,))
        assert cursor.fetchone() == (0,)
        cursor.execute('SELECT count(*) FROM memplex_source_authorities')
        assert cursor.fetchone() == (0,)


def test_sync_typed_without_raw_stays_quarantined(tmp_path):
    from tests.test_sync_ingress import _batch
    store = native_v1(tmp_path)
    before = store._durability.load_authoritative()
    with pytest.raises(SourceContractError):
        store.sync_apply_batch(_batch())
    assert store._durability.load_authoritative() == before
    assert not before.memory['source_validity']['sources']


@pytest.mark.parametrize('carrier', ['unsupported-v99', '{}'])
def test_sync_carriers_never_become_legacy_direct_input(carrier):
    from memplex.sync_ingress import validate_ingress_batch
    from memplex.sync_repository import SyncBatchRejected
    from tests.test_sync_ingress import _batch
    batch = _batch(payload={'namespace': {'memplex_derivation_contract': carrier}})
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(batch.canonical_bytes, batch.request_digest)
