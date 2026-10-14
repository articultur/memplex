"""Task12 restore can never turn a stale backup into current admission."""
from __future__ import annotations

import pytest

from memplex.source_contract import SourceContractError
from memplex.storage.lite.durability import LiteStorageIntegrityError
from memplex.storage.lite.store import LiteMemoryStore
from tests.test_source_v1_compatibility import native_v1


def test_pg_restore_toc_preserves_managed_foreign_keys_and_withholds_schema_access():
    from memplex.storage.postgres_backup import _approved_restore_toc
    entries = [
        '1; 2615 12 SCHEMA - isolated owner',
        '2; 0 0 COMMENT - SCHEMA isolated owner',
        '3; 0 0 ACL - SCHEMA isolated owner',
        '4; 0 17 TABLE DATA isolated memplex_facts owner',
        '5; 2606 18 FK CONSTRAINT isolated memplex_sync_deliveries memplex_sync_deliveries_tenant_id_target_id_fkey owner',
        '6; 2606 19 FK CONSTRAINT isolated memplex_sync_deliveries memplex_sync_deliveries_tenant_id_stream_seq_fkey owner',
        '7; 2606 20 FK CONSTRAINT isolated memplex_sync_snapshot_items memplex_sync_snapshot_items_tenant_id_snapshot_id_fkey owner',
        '8; 0 0 ACL isolated TABLE memplex_facts owner',
        '9; 826 21 DEFAULT ACL isolated DEFAULT PRIVILEGES FOR TABLES owner',
    ]
    toc = '\n'.join(entries)
    data, access = _approved_restore_toc(toc, 'isolated')
    assert all(line in data for line in toc.splitlines()[3:8])
    assert access == toc.splitlines()[2] + '\n'
    assert 'COMMENT' not in data and 'DEFAULT PRIVILEGES' not in data
    assert 'SCHEMA' not in data


@pytest.mark.parametrize('entry', [
    '2; 1255 15 FUNCTION isolated arbitrary_sql() owner',
    '2; 1259 15 TABLE foreign memplex_facts owner',
    '2; 1259 15 TABLE isolated unknown_bytes owner',
    '1; 0 15 TABLE DATA isolated memplex_facts owner',
])
def test_pg_restore_toc_rejects_unknown_or_duplicate_objects(entry):
    from memplex.storage.postgres_backup import _approved_restore_toc
    with pytest.raises(ValueError):
        _approved_restore_toc('1; 0 14 TABLE DATA isolated memplex_facts owner\n' + entry, 'isolated')


def test_unsealed_v1_backup_cannot_activate_legacy_target(tmp_path):
    source = native_v1(tmp_path / "source")
    key = bytes(range(32))
    manifest = source.create_backup(tmp_path / "backups", key, "test-key")
    target = LiteMemoryStore(tmp_path / "target" / "memory.json")
    before = target._durability.load_authoritative()
    with pytest.raises(SourceContractError):
        target.restore_backup(tmp_path / "backups" / manifest.backup_id, key)
    assert target._durability.load_authoritative() == before


def test_lite_restore_quarantine_survives_invalid_archive_and_restart(tmp_path):
    from memplex.backup import BackupIntegrityError
    from memplex.source_compatibility import _lite_restore_authority, begin_v1_restore
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE

    target = LiteMemoryStore(tmp_path / 'target' / 'memory.json')
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    receipt = begin_v1_restore(authority.target, authority=authority)
    assert receipt.state == 'quarantined'
    with pytest.raises(BackupIntegrityError):
        target.restore_backup(tmp_path / 'missing.zip', b'x' * 32, authority=authority)
    with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
        LiteMemoryStore(target._path)


def test_lite_populated_restore_preserves_tombstones_and_stays_quarantined(tmp_path):
    from dataclasses import replace

    from memplex.source_codec import decode_ledger
    from memplex.source_compatibility import _lite_restore_authority, begin_v1_restore
    from memplex.source_contract import SourceOperation
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE, _command, _mutate

    source = native_v1(tmp_path / 'source')
    first = _mutate(source.source_versions(), _command())
    _mutate(source.source_versions(), replace(_command(), expected=first.ref.token,
            operation=SourceOperation.DELETE, payload=None))
    _mutate(source.source_versions(), _command(name='still-live'))
    original = source._durability.load_authoritative()
    key = bytes(range(32))
    manifest = source.create_backup(tmp_path / 'backups', key, 'restore-test')
    target = LiteMemoryStore(tmp_path / 'target' / 'memory.json')
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    begin_v1_restore(authority.target, authority=authority)
    target.restore_backup(tmp_path / 'backups' / manifest.backup_id, key, authority=authority)
    restored = target._durability.load_authoritative()
    assert restored.memory == original.memory
    assert restored.changelog == original.changelog
    assert decode_ledger(restored.memory['source_validity']).sources[first.ref.key].state.value == 'deleted'
    with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
        LiteMemoryStore(target._path)


@pytest.mark.parametrize('stage,durable', [
    ('before_journal_durable_publish', False),
    ('after_journal_rename_and_parent_dir_fsync', True),
    ('after_memory_replace', True), ('after_changelog_replace', True), ('after_journal_unlink', True),
])
def test_restore_failure_never_reopens_live(tmp_path, monkeypatch, stage, durable):
    from memplex.config import SourceContractConfig
    from memplex.service import MemplexService
    from memplex.source_compatibility import _lite_restore_authority, begin_v1_restore
    from memplex.storage.lite.durability import LiteDurability
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_compatibility import legacy_config
    from tests.test_source_v1_pg_transactions import SCOPE, _command, _mutate
    source = native_v1(tmp_path / 'source')
    _mutate(source.source_versions(), _command())
    key = bytes(range(32))
    manifest = source.create_backup(tmp_path / 'backups', key, 'fault')
    target = LiteMemoryStore(tmp_path / 'target' / 'memory.json')
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    begin_v1_restore(authority.target, authority=authority)
    def crash():
        raise RuntimeError('interrupted restore')
    with monkeypatch.context() as failure:
        failure.setattr(target._durability, stage, crash)
        with pytest.raises(RuntimeError, match='interrupted restore'):
            target.restore_backup(tmp_path / 'backups' / manifest.backup_id, key, authority=authority)
    for mode in ('off', 'v1'):
        cfg = legacy_config(tmp_path / 'target')
        if mode == 'v1':
            cfg.source_contract = SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id)
        with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
            MemplexService(cfg)
    recovered = LiteDurability(target._path, target._changelog._path).load_authoritative()
    assert (recovered.memory.get('schema_version') == 3) is durable
    if durable:
        assert recovered.memory == source._durability.load_authoritative().memory
        with pytest.raises(SourceContractError):
            target.restore_backup(tmp_path / 'backups' / manifest.backup_id, key, authority=authority)


@pytest.mark.parametrize('tamper', ['missing', 'scope', 'path', 'live_flag', 'version_true', 'version_float'])
def test_lite_restore_recovery_requires_external_exact_quarantine(tmp_path, monkeypatch, tamper):
    import json

    from memplex.source_compatibility import _lite_restore_authority, _marker_path, begin_v1_restore
    from memplex.storage.lite.durability import LiteDurability
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE, _command, _mutate
    source = native_v1(tmp_path / 'source')
    _mutate(source.source_versions(), _command())
    key = bytes(range(32))
    manifest = source.create_backup(tmp_path / 'backups', key, 'fault')
    target = LiteMemoryStore(tmp_path / 'target' / 'memory.json')
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    begin_v1_restore(authority.target, authority=authority)
    def crash():
        raise RuntimeError('durable decision')
    with monkeypatch.context() as failure:
        failure.setattr(target._durability, 'after_journal_rename_and_parent_dir_fsync', crash)
        with pytest.raises(RuntimeError):
            target.restore_backup(tmp_path / 'backups' / manifest.backup_id, key, authority=authority)
    marker_path = _marker_path(target._durability)
    original_marker = marker_path.read_bytes()
    marker = json.loads(original_marker)
    if tamper == 'missing':
        marker_path.rename(marker_path.with_suffix('.saved'))
    else:
        if tamper == 'scope':
            marker['scope']['authority_id'] = '22222222-2222-4222-8222-222222222222'
        elif tamper == 'path':
            marker['storage_identity'] = str(tmp_path / 'other')
        elif tamper.startswith('version_'):
            marker['version'] = True if tamper == 'version_true' else 1.0
        else:
            marker['state'] = 'live'
        marker_path.write_text(json.dumps(marker))
    with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
        LiteDurability(target._path, target._changelog._path).load_authoritative()
    assert target._durability._journal_path.exists()
    assert not target._path.exists()


def test_lite_quarantine_is_durable_before_archive_open(tmp_path, monkeypatch):
    from memplex.source_compatibility import (
        _lite_restore_authority,
        _read_lite_marker,
        begin_v1_restore,
    )
    from memplex.storage.lite import store as implementation
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE
    target = LiteMemoryStore(tmp_path / 'target' / 'memory.json')
    authority = _lite_restore_authority(target, SCOPE, issuer=_RESOURCE_ISSUER)
    receipt = begin_v1_restore(authority.target, authority=authority)
    def observe(*args):
        marker = _read_lite_marker(target._durability)
        assert marker['restore_id'] == receipt.restore_id and marker['state'] == 'quarantined'
        with pytest.raises((SourceContractError, LiteStorageIntegrityError)):
            LiteMemoryStore(target._path)
        raise RuntimeError('archive boundary observed')
    monkeypatch.setattr(implementation, 'open_verified_backup_artifact', observe)
    with pytest.raises(RuntimeError, match='archive boundary'):
        target.restore_backup(tmp_path / 'unused', bytes(range(32)), authority=authority)


from tests.test_source_v1_compatibility import (
    empty_pg_resource as empty_pg_resource,  # noqa: PLC0414
)
from tests.test_source_v1_compatibility import (
    ordinary_pg_owner as ordinary_pg_owner,  # noqa: PLC0414
)
from tests.test_source_v1_pg_transactions import NATIVE, POLICY


def _pg_artifact(resource, dsn, tmp_path):
    from memplex.storage.postgres_backup import PostgresBackupExecutor
    from tests.test_postgres_backup_integration import _pgserver_tools
    executor = PostgresBackupExecutor(expected_target=resource.ready_pool.target, tools=_pgserver_tools(), timeout_seconds=30)
    key = bytes(range(32))
    manifest = executor.create(migration_dsn=dsn, destination=tmp_path / 'backup', signing_key=key, key_id='native')
    return executor, tmp_path / 'backup' / manifest.backup_id, key


def _drop_pg_schema(dsn, schema):
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


@NATIVE
def test_native_pg_explicit_restore_live_bytes_remain_quarantined(empty_pg_resource, tmp_path, monkeypatch):
    from contextlib import closing

    import psycopg2

    from memplex.config import SourceContractConfig
    from memplex.source_compatibility import begin_v1_restore
    from memplex.storage.postgres_resources import _source_schema_marker
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE, _command, _mutate
    resource, store, admission, dsn = empty_pg_resource
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    _mutate(store.source_versions(), _command())
    target = resource.ready_pool.target
    executor, artifact, key = _pg_artifact(resource, dsn, tmp_path)
    _drop_pg_schema(dsn, target.schema)
    authority = executor._restore_authority(dsn, SCOPE, issuer=_RESOURCE_ISSUER)
    receipt = begin_v1_restore(authority.target, authority=authority)
    imported = []
    original = executor._restore_list
    def verify_before_bytes(*args):
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            marker = _source_schema_marker(cursor, target.schema)
            assert marker['restore_id'] == receipt.restore_id and marker['state'] == 'quarantined'
        imported.append(True)
        return original(*args)
    monkeypatch.setattr(executor, '_restore_list', verify_before_bytes)
    result = executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key,
                              target_schema=target.schema, authority=authority)
    assert result.restored and imported == [True]
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        assert _source_schema_marker(cursor, target.schema)['state'] == 'quarantined'
        cursor.execute('SELECT state FROM memplex_source_authorities')
        assert cursor.fetchall() == [('live',)]
        cursor.execute("SELECT count(*) FROM memplex_facts WHERE id='source'")
        assert cursor.fetchone() == (1,)
    with pytest.raises(SourceContractError):
        resource._source_startup(SourceContractConfig(), store, issuer=_RESOURCE_ISSUER)
    with pytest.raises(SourceContractError):
        resource._source_startup(SourceContractConfig('v1', SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id),
                                 store, issuer=_RESOURCE_ISSUER)


@NATIVE
def test_native_pg_generic_v1_archive_never_releases_quarantine(empty_pg_resource, tmp_path):
    from contextlib import closing

    import psycopg2

    from memplex.backup import BackupIntegrityError
    from memplex.storage.postgres_resources import _source_schema_marker
    from tests.test_source_v1_pg_transactions import SCOPE
    resource, store, admission, dsn = empty_pg_resource
    store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    target = resource.ready_pool.target
    executor, artifact, key = _pg_artifact(resource, dsn, tmp_path)
    _drop_pg_schema(dsn, target.schema)
    with pytest.raises(BackupIntegrityError):
        executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key, target_schema=target.schema)
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        marker = _source_schema_marker(cursor, target.schema)
        assert marker['kind'] == 'legacy_candidate' and marker['state'] == 'quarantined'
        cursor.execute("SELECT count(*) FROM pg_constraint WHERE connamespace=%s::regnamespace AND conname LIKE 'memplex_source_check_%%'", (target.schema,))
        assert cursor.fetchone() == (0,)


@NATIVE
@pytest.mark.parametrize('import_first', [False, True])
def test_native_pg_failed_restore_retry_requires_original_empty_attempt(empty_pg_resource, tmp_path, monkeypatch, import_first):
    from contextlib import closing

    import psycopg2

    from memplex.backup import BackupIntegrityError
    from memplex.storage.postgres_resources import _source_schema_marker
    resource, _store, _admission, dsn = empty_pg_resource
    target = resource.ready_pool.target
    executor, artifact, key = _pg_artifact(resource, dsn, tmp_path)
    _drop_pg_schema(dsn, target.schema)
    original = executor._restore_list
    def crash(*args):
        if import_first:
            original(*args)
        raise RuntimeError('restore interrupted')
    with monkeypatch.context() as failure:
        failure.setattr(executor, '_restore_list', crash)
        with pytest.raises(BackupIntegrityError):
            executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key, target_schema=target.schema)
    with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
        marker = _source_schema_marker(cursor, target.schema)
        assert marker['state'] == 'quarantined'
    seen = []
    def record(*args):
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            seen.append(_source_schema_marker(cursor, target.schema)['restore_id'])
        return original(*args)
    monkeypatch.setattr(executor, '_restore_list', record)
    if import_first:
        with pytest.raises(BackupIntegrityError, match='target_exists'):
            executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key, target_schema=target.schema)
        assert not seen
    else:
        assert executor.restore(migration_dsn=dsn, artifact=artifact, signing_key=key, target_schema=target.schema).restored
        assert seen and set(seen) == {marker['restore_id']}
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            assert _source_schema_marker(cursor, target.schema) is None


@NATIVE
def test_native_pg_ordinary_owner_legacy_restore_under_force_rls(ordinary_pg_owner, tmp_path):
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.storage.postgres_backup import PostgresBackupExecutor
    from memplex.storage.postgres_resources import _source_schema_marker
    from tests.test_postgres_backup_integration import _pgserver_tools
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        owner = cursor.fetchone()[0]
    with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(target.database), sql.Identifier(owner)))
        cursor.execute("INSERT INTO memplex_facts(id,data,tenant_id,workspace) VALUES ('legacy-hidden','{}','foreign','foreign')")
    try:
        executor = PostgresBackupExecutor(expected_target=target, tools=_pgserver_tools(), timeout_seconds=30)
        key = bytes(range(32))
        manifest = executor.create(migration_dsn=admin_dsn, destination=tmp_path / 'backup', signing_key=key, key_id='owner')
        _drop_pg_schema(admin_dsn, target.schema)
        assert executor.restore(migration_dsn=owner_dsn, artifact=tmp_path / 'backup' / manifest.backup_id,
                                signing_key=key, target_schema=target.schema).restored
        with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM memplex_facts')
            assert cursor.fetchone() == (0,)
            assert _source_schema_marker(cursor, target.schema) is None
        with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM memplex_facts')
            assert cursor.fetchone() == (1,)
    finally:
        with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(target.database), sql.Identifier(owner)))


@NATIVE
@pytest.mark.parametrize('explicit', [False, True])
def test_native_pg_restore_retains_object_acl_but_withholds_v1_schema_usage(empty_pg_resource, tmp_path, explicit):
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.source_compatibility import begin_v1_restore
    from memplex.storage.migrations import ApplicationAclContract
    from memplex.storage.postgres_backup import PostgresBackupExecutor
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_postgres_backup_integration import (
        _grant_application_acl_contract,
        _pgserver_tools,
    )
    from tests.test_source_v1_pg_transactions import SCOPE
    resource, store, admission, dsn = empty_pg_resource
    if explicit:
        store.source_versions().initialize_authority(SCOPE, POLICY, admission=admission)
    target = resource.ready_pool.target
    role = 'source_reader_' + uuid.uuid4().hex
    with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
    try:
        _grant_application_acl_contract(dsn, role)
        executor = PostgresBackupExecutor(expected_target=target, tools=_pgserver_tools(), timeout_seconds=30,
                                          application_acl=ApplicationAclContract(role), deployment_profile='production')
        key = bytes(range(32))
        manifest = executor.create(migration_dsn=dsn, destination=tmp_path / 'backup', signing_key=key, key_id='acl')
        _drop_pg_schema(dsn, target.schema)
        authority = None
        if explicit:
            authority = executor._restore_authority(dsn, SCOPE, issuer=_RESOURCE_ISSUER)
            begin_v1_restore(authority.target, authority=authority)
        assert executor.restore(migration_dsn=dsn, artifact=tmp_path / 'backup' / manifest.backup_id,
                                signing_key=key, target_schema=target.schema, authority=authority).restored
        with closing(psycopg2.connect(dsn)) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT has_schema_privilege(%s,%s,'USAGE'),has_table_privilege(%s,'memplex_facts','SELECT')",
                           (role, target.schema, role))
            assert cursor.fetchone() == (not explicit, True)
            cursor.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
            if explicit:
                with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                    cursor.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(target.schema, 'memplex_facts')))
            else:
                cursor.execute('SELECT count(*) FROM memplex_facts')
                assert cursor.fetchone() == (0,)
    finally:
        with closing(psycopg2.connect(dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@NATIVE
@pytest.mark.parametrize('defaults', ['SCHEMAS', 'TABLES', 'SEQUENCES'])
def test_native_pg_fresh_restore_checks_existing_owner_defaults(ordinary_pg_owner, defaults):
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.backup import BackupIntegrityError
    from memplex.source_compatibility import begin_v1_restore
    from memplex.storage.migrations import ApplicationAclContract
    from memplex.storage.postgres_backup import PostgresBackupExecutor
    from memplex.storage.postgres_resources import _source_schema_marker
    from memplex.storage.source_versions import _RESOURCE_ISSUER
    from tests.test_source_v1_pg_transactions import SCOPE
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    role = 'source_defaults_' + uuid.uuid4().hex
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        owner = cursor.fetchone()[0]
    privilege = 'SELECT' if defaults == 'TABLES' else 'USAGE'
    with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(role)))
        cursor.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(target.database), sql.Identifier(owner)))
        cursor.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} GRANT {} ON {} TO {}').format(
            sql.Identifier(owner), sql.SQL(privilege), sql.SQL(defaults), sql.Identifier(role)))
    try:
        _drop_pg_schema(admin_dsn, target.schema)
        executor = PostgresBackupExecutor(expected_target=target,
            application_acl=ApplicationAclContract(role) if defaults == 'SEQUENCES' else None,
            deployment_profile='production')
        authority = executor._restore_authority(owner_dsn, SCOPE, issuer=_RESOURCE_ISSUER)
        if defaults == 'SEQUENCES':
            receipt = begin_v1_restore(authority.target, authority=authority)
            with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
                assert _source_schema_marker(cursor, target.schema)['restore_id'] == receipt.restore_id
        else:
            with pytest.raises(BackupIntegrityError, match='isolated|defaults_unsafe'):
                begin_v1_restore(authority.target, authority=authority)
            with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
                cursor.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (target.schema,))
                assert cursor.fetchone() is None
    finally:
        with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE {} ON {} FROM {}').format(
                sql.Identifier(owner), sql.SQL(privilege), sql.SQL(defaults), sql.Identifier(role)))
            cursor.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(target.database), sql.Identifier(owner)))
            cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@NATIVE
@pytest.mark.parametrize('indirect', [False, True])
def test_native_pg_restore_rejects_noinherit_owner_membership_before_bytes(ordinary_pg_owner, tmp_path, monkeypatch, indirect):
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.backup import BackupIntegrityError
    from memplex.storage.postgres_backup import PostgresBackupExecutor
    from tests.test_postgres_backup_integration import _pgserver_tools
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    reader, bridge = 'source_old_' + uuid.uuid4().hex, 'source_bridge_' + uuid.uuid4().hex
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        owner = cursor.fetchone()[0]
    with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(reader)))
        cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOINHERIT').format(sql.Identifier(bridge)))
        cursor.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(owner), sql.Identifier(bridge if indirect else reader)))
        if indirect:
            cursor.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(bridge), sql.Identifier(reader)))
        cursor.execute(sql.SQL('GRANT CREATE ON DATABASE {} TO {}').format(sql.Identifier(target.database), sql.Identifier(owner)))
    try:
        # The existing test connection can impersonate this disposable role;
        # no new credential or login connection is created.
        with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('SET SESSION AUTHORIZATION {}').format(sql.Identifier(reader)))
            cursor.execute("SELECT has_schema_privilege(current_user,%s,'USAGE')", (target.schema,))
            assert cursor.fetchone() == (False,)
            cursor.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
            cursor.execute("SELECT has_schema_privilege(current_user,%s,'USAGE')", (target.schema,))
            assert cursor.fetchone() == (True,)
        executor = PostgresBackupExecutor(expected_target=target, tools=_pgserver_tools(), timeout_seconds=30)
        key = bytes(range(32))
        manifest = executor.create(migration_dsn=admin_dsn, destination=tmp_path / 'backup', signing_key=key, key_id='membership')
        _drop_pg_schema(admin_dsn, target.schema)
        imported = []
        monkeypatch.setattr(executor, '_restore_list', lambda *args: imported.append(True))
        with pytest.raises(BackupIntegrityError, match='target_not_isolated'):
            executor.restore(migration_dsn=owner_dsn, artifact=tmp_path / 'backup' / manifest.backup_id,
                             signing_key=key, target_schema=target.schema)
        assert imported == []
        with closing(psycopg2.connect(admin_dsn)) as connection, connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (target.schema,))
            assert cursor.fetchone() is None
    finally:
        with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('REVOKE CREATE ON DATABASE {} FROM {}').format(sql.Identifier(target.database), sql.Identifier(owner)))
            cursor.execute(sql.SQL('DROP ROLE {}, {}').format(sql.Identifier(reader), sql.Identifier(bridge)))


@NATIVE
def test_native_pg_quarantined_acl_rejects_configured_noinherit_owner_role(ordinary_pg_owner):
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.storage.migrations import ApplicationAclContract, MigrationIntegrityError
    from memplex.storage.postgres_backup import PostgresBackupExecutor
    admin_dsn, owner_dsn, target = ordinary_pg_owner
    role = 'source_app_' + uuid.uuid4().hex
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT current_user')
        owner = cursor.fetchone()[0]
    with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
        cursor.execute(sql.SQL('CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS').format(sql.Identifier(role)))
        cursor.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(owner), sql.Identifier(role)))
    try:
        executor = PostgresBackupExecutor(expected_target=target, application_acl=ApplicationAclContract(role),
                                          deployment_profile='production')
        with (closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cursor,
              pytest.raises(MigrationIntegrityError, match='SET ROLE')):
            executor._quarantined_object_acls(cursor)
    finally:
        with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cursor:
            cursor.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
