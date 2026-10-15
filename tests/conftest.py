"""Shared pytest configuration for the Memplex test suite.

Sets the storage backend to the implemented ``"lite"`` backend before any
test module is imported.  Several test files still carry their own
``os.environ.setdefault`` boilerplate at the top; this conftest acts as the
unified fallback so new test files do not need to repeat it.
"""

import hashlib
import json
import math
import os
import sys
import threading
import time
import types
import uuid
from collections.abc import Iterator

import pytest

try:
    import resource
except ImportError:
    resource = None

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


@pytest.fixture(scope="session")
def pg_server_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Run one private real PostgreSQL server for the PostgreSQL test module.

    Database-server startup is deliberately session-scoped, while every test
    receives its own schema below.  This keeps real-catalogue tests fast
    enough to run together without treating ``public`` as their scratchpad.
    """
    external_dsn = os.environ.get("MEMPLEX_TEST_POSTGRES_DSN")
    if external_dsn:
        # CI supplies pgvector through a service container.  Retain the
        # per-test schema fixture below, so an external server never turns
        # function-scoped catalogue tests into shared-public-state tests.
        yield external_dsn
        return

    pgserver = pytest.importorskip(
        "pgserver", reason="pgserver not installed (use .venv-pgcheck)"
    )
    data_dir = tmp_path_factory.mktemp("memplex-pgtest") / "data"
    server = pgserver.get_server(str(data_dir))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture
def pg_function_dsn(pg_server_dsn: str) -> Iterator[str]:
    """Yield one empty, uniquely named schema and remove it after one test.

    The options-based search path makes ordinary application, migration and
    catalogue connections target the same schema.  Cleanup uses the server
    DSN rather than the scoped DSN, so a failed migration cannot prevent
    ``DROP SCHEMA ... CASCADE`` from reclaiming its test catalogue.
    """
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    schema = f"g003_function_{uuid.uuid4().hex}"
    connection = psycopg2.connect(pg_server_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            connection.commit()
        finally:
            cursor.close()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()

    scoped_dsn = psycopg2.extensions.make_dsn(
        pg_server_dsn,
        options=f"-c search_path={schema}",
    )
    try:
        yield scoped_dsn
    finally:
        cleanup = psycopg2.connect(pg_server_dsn)
        try:
            cursor = cleanup.cursor()
            try:
                cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                cleanup.commit()
            finally:
                cursor.close()
        except BaseException:
            cleanup.rollback()
            raise
        finally:
            cleanup.close()


# Temporary T13 observation. No product objects/frames survive these hooks.
# COMPLETE describes one retained record, never all successful invocations.
_T13_REPRESENTATIVE = (
    'tests/test_source_v1_capture.py::'
    'test_repeated_text_uses_new_handles_and_exact_explicit_correction[lite]'
)
_T13_MODULE = 'tests/test_source_v1_capture.py::'
_T13_MODULES = tuple('memplex.storage.lite.' + name for name in
                     ('single_writer', 'source_versions', 'durability'))
_T13_NATIVE_WALL = getattr(time, 'monotonic_ns', None)
_T13_NATIVE_CPU = getattr(time, 'thread_time_ns', None)
_T13_ENUMS = frozenset({
    'T13_OBS_V1', 'FAILURE', 'RESOURCE', 'SUMMARY', 'SETUP', 'CALL', 'TEARDOWN',
    'REPRESENTATIVE', 'OTHER', 'QUEUED', 'DIRECT', 'UNKNOWN', 'SESSION_START',
    'BEFORE_CAPTURE', 'AFTER_CAPTURE', 'SESSION_FINISH', 'COMPLETE', 'INCOMPLETE',
    'NOT_REACHED', 'NOT_OBSERVED', 'TRUNCATED', 'SAMPLE_FAULT', 'CODE_UNRECOGNIZED',
    'BINDING_AMBIGUOUS', 'CLOCK_UNVERIFIED', 'TRACE_LIMIT', 'OUTPUT_LIMIT',
    'UNSUPPORTED_RESOURCE', 'REPORT_FAULT', 'INTERRUPTED', 'NANOSECONDS',
    'SECONDS', 'PLATFORM_NATIVE_UNKNOWN',
})
_T13_FIELDS = {
    'SUBMIT': ('admission_wall_ns', 'wait_wall_ns'),
    'RUN': ('worker_entry_wall_ns', 'worker_entry_cpu_ns', 'worker_guard_wall_ns'),
    'MUTATE': tuple(site + suffix for site in
                    ('lock_request', 'load_start', 'load_end', 'commit_request')
                    for suffix in ('_wall_ns', '_cpu_ns')),
    'COMMIT': tuple(site + suffix for site in
                    ('commit_entry', 'base_validation_start', 'target_validation_start',
                     'target_validation_end', 'encode_start', 'temporary_journal_start',
                     'temporary_journal_end') for suffix in ('_wall_ns', '_cpu_ns')),
    'GUARD': ('rejecting_guard_wall_ns', 'rejecting_guard_post_cpu_ns'),
}
_T13_WINDOWS = (
    ('admission_to_worker', 'admission', 'worker_entry'),
    ('lock_scope_entry', 'lock_request', 'load_start'),
    ('load_group', 'load_start', 'load_end'),
    ('mutation_group', 'load_end', 'commit_request'),
    ('commit_dispatch', 'commit_request', 'commit_entry'),
    ('base_validation', 'base_validation_start', 'target_validation_start'),
    ('target_validation', 'target_validation_start', 'target_validation_end'),
    ('validation_transition', 'target_validation_end', 'encode_start'),
    ('encode_group', 'encode_start', 'temporary_journal_start'),
    ('temporary_journal', 'temporary_journal_start', 'temporary_journal_end'),
)
_T13_COUNTERS = ('ru_utime', 'ru_stime', 'ru_maxrss', 'ru_minflt', 'ru_majflt',
                 'ru_inblock', 'ru_oublock', 'ru_nvcsw', 'ru_nivcsw')
_T13_KEYS = frozenset({
    'schema', 'kind', 'status', 'reason', 'phase', 'role', 'dispatch', 'ordinal',
    'samples', 'windows', 'checkpoint', 'wall_ns', 'threads', 'writers',
    'case_owned_survivors', 'session_owned_writers', 'rss_unit', 'cpu_unit',
    'deadline_ns', 'remaining', 'omitted', 'records', 'snapshots',
    'submit_status', 'run_status', 'mutate_status', 'commit_status', 'guard_status',
    'run_faults', 'mutate_faults', 'commit_faults', 'guard_faults',
    *[field for fields in _T13_FIELDS.values() for field in fields],
    *[name + suffix for name, _, _ in _T13_WINDOWS for suffix in
      ('_wall_ns', '_cpu_ns', '_to_guard_wall_ns', '_to_post_guard_cpu_ns')],
    *_T13_COUNTERS,
})
_T13_STATE = {'records': [], 'snapshots': [], 'others': 0, 'representative': False,
              'omitted': 0, 'bytes': 0, 'incomplete': False, 'clock_ok': False,
              'capture_started': False, 'capture_last': -1}

# Frozen candidate code digests and exact traceback transition sites.
_T13_CODE_SPECS = (('SUBMIT', 'memplex.storage.lite.single_writer', 'SingleWriterQueue', 'submit_until', '', 'd1204d34dc5f1f850718d44e5d57b51795623a2c4772c53519fa87676c90dc4a'), ('TRANSPORT', 'memplex.storage.lite.single_writer', 'SingleWriterQueue', '_run', '', 'b518ecb08f0ce8b2c051f0c27457aaa4e3ca14e135de4f7555fbdff2c1c4f117'), ('RUN', 'memplex.storage.lite.single_writer', 'SingleWriterQueue', 'submit_until', 'run', '950a5e11300f8d4238a8b362249d40889ab4b35d535dda5906cfeb1134bff2b3'), ('LAMBDA', 'memplex.storage.lite.source_versions', 'LiteSourceVersionStore', 'mutate_source', '<lambda>', '62b195a15c3c59697b6db282b2cffdb2e3bcf9fc1485641e51ff1457decd58f2'), ('MUTATE', 'memplex.storage.lite.source_versions', 'LiteSourceVersionStore', '_mutate_locked', '', '41c2eb902e8b90e6dfc71dd26e6322ec69b49b3b254e6ac158347218d42ae006'), ('COMMIT', 'memplex.storage.lite.durability', 'LiteDurability', 'commit_locked', '', '5d4fc776d608fb77ff1724aaf7a2317766f93cd46681c13a7f7a9394cccdfbbd'), ('GUARD', 'memplex.storage.lite.durability', '', '_check_read_deadline', '', '73c46472f14b9da43c8b0c7e9c83e273c8f3fd2306c5af45ddf43fa04eb5a308'), ('VALIDATE', 'memplex.storage.lite.durability', '', '_validate_memory_payload', '', 'f4707bfc07772ca334c19dab46ec53506fa0a2d2b568bdc56891d0e4fcec6cfe'), ('SOURCE_VALIDATE', 'memplex.storage.lite.source_versions', '', 'validate_source_memory', '', 'c8945a88cd4131d9f3ae461344ecac9756298b432354842b5e99b8c3cb433df1'), ('RECONCILE', 'memplex.storage.lite.source_versions', '', 'reconcile_memory', '', '55423518d865267224f70c32e99370bfcb87db36acb2804850654010ee273922'), ('WITHIN', 'memplex.storage.lite.source_versions', '', '_within_deadline', '', 'cb40f9ffe2255917df787a16c58dc3ab71aa71f57c0824fa1863fc4708afde2c'))
_T13_EDGES = {('TRANSPORT', 'RUN'): (78,), ('RUN', 'LAMBDA'): (133,), ('LAMBDA', 'MUTATE'): (541,), ('MUTATE', 'COMMIT'): (647,), ('MUTATE', 'GUARD'): (640,), ('COMMIT', 'VALIDATE'): (1245, 1246, 1247, 1248, 1255), ('COMMIT', 'GUARD'): (1233, 1265, 1276, 1278, 1299, 1317), ('VALIDATE', 'SOURCE_VALIDATE'): (635,), ('VALIDATE', 'GUARD'): (593, 630, 636), ('SOURCE_VALIDATE', 'RECONCILE'): (239,), ('RECONCILE', 'WITHIN'): (198, 199, 206, 221, 222, 226, 227), ('RECONCILE', 'GUARD'): (193, 195, 217, 231), ('WITHIN', 'GUARD'): (186, 188), ('SUBMIT', 'TRANSPORT'): (145,)}


def _t13_number(value):
    if type(value) is int and 0 <= value <= 2**63 - 1:
        return value
    if type(value) is float and math.isfinite(value) and 0 <= value <= 2**63 - 1:
        return value
    return None


def _t13_valid(value, depth=0):
    if depth > 4:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) in (int, float):
        return _t13_number(value) is not None
    if type(value) is str:
        return value in _T13_ENUMS
    if type(value) is dict and len(value) <= 128:
        return all(type(k) is str and k in _T13_KEYS and len(k) <= 64
                   and k.isascii() and _t13_valid(v, depth + 1)
                   for k, v in value.items())
    return False


def _t13_json(record, limit):
    if not _t13_valid(record):
        _T13_STATE['incomplete'] = True
        record = {'schema': 'T13_OBS_V1', 'status': 'UNKNOWN', 'reason': 'SAMPLE_FAULT'}
    result = json.dumps(record, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    if len(result) > limit:
        _T13_STATE['incomplete'] = True
        result = '{"schema":"T13_OBS_V1","status":"TRUNCATED","reason":"OUTPUT_LIMIT"}'
    return result


def _t13_delta(start, end, qualified):
    if (qualified and type(start) is int and type(end) is int
            and _t13_number(start) is not None and _t13_number(end) is not None and end >= start):
        return end - start
    return None


def _t13_code_data(code):
    def constant(value):
        if type(value) is types.CodeType:
            return _t13_code_data(value)
        if type(value) is tuple:
            return [constant(v) for v in value]
        if type(value) is frozenset:
            return ['frozenset', sorted(constant(v) for v in value)]
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is bytes:
            return ['bytes', value.hex()]
        if type(value) is float:
            return ['float', value.hex()]
        raise ValueError('unsupported code constant')
    return [code.co_code.hex(), code.co_firstlineno, code.co_name, code.co_qualname,
            code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
            code.co_flags, code.co_stacksize, code.co_names, code.co_varnames,
            code.co_freevars, code.co_cellvars, code.co_linetable.hex(),
            code.co_exceptiontable.hex(), [constant(v) for v in code.co_consts]]


def _t13_code_hash(code):
    return hashlib.sha256(json.dumps(_t13_code_data(code), separators=(',', ':')).encode()).hexdigest()


def _t13_codes():
    result = {}
    for role, module_name, owner, name, nested, digest in _T13_CODE_SPECS:
        module = sys.modules.get(module_name)
        if type(module) is not types.ModuleType:
            continue
        namespace = vars(module)
        if owner:
            cls = namespace.get(owner)
            if type(cls) is not type:
                continue
            namespace = vars(cls)
        function = namespace.get(name)
        if type(function) is not types.FunctionType:
            continue
        code = function.__code__
        if nested:
            candidates = [c for c in code.co_consts
                          if type(c) is types.CodeType and c.co_name == nested]
            if len(candidates) != 1:
                continue
            code = candidates[0]
        if _t13_code_hash(code) == digest:
            result[code] = role
    return result


def _t13_clocks():
    if (type(_T13_NATIVE_WALL) is not types.BuiltinFunctionType
            or type(_T13_NATIVE_CPU) is not types.BuiltinFunctionType
            or _T13_NATIVE_WALL.__module__ != 'time'
            or _T13_NATIVE_CPU.__module__ != 'time'
            or _T13_NATIVE_WALL.__name__ != 'monotonic_ns'
            or _T13_NATIVE_CPU.__name__ != 'thread_time_ns'):
        return False
    for name in _T13_MODULES:
        module = sys.modules.get(name)
        if type(module) is not types.ModuleType:
            return False
        values = vars(module)
        if values.get('_t13_wall') is not _T13_NATIVE_WALL or values.get('_t13_cpu') is not _T13_NATIVE_CPU:
            return False
        clock_module = values.get('time')
        if type(clock_module) is not types.ModuleType:
            return False
        if vars(clock_module).get('monotonic_ns') is not _T13_NATIVE_WALL:
            return False
    return True


def _t13_binding(roles):
    targets = ('SUBMIT', 'TRANSPORT', 'RUN', 'LAMBDA', 'MUTATE', 'COMMIT', 'GUARD')
    if any(roles.count(role) > 1 for role in roles) or 'UNRECOGNIZED' in roles:
        return 'UNKNOWN'
    selected = [r for r in roles if r in targets]
    if selected in (['SUBMIT', 'TRANSPORT', 'RUN', 'LAMBDA', 'MUTATE', 'COMMIT', 'GUARD'],
                    ['SUBMIT', 'TRANSPORT', 'RUN', 'LAMBDA', 'MUTATE', 'GUARD']):
        return 'QUEUED'
    if selected in (['MUTATE', 'COMMIT', 'GUARD'], ['MUTATE', 'GUARD']):
        return 'DIRECT'
    return 'UNKNOWN'


def _t13_copy(frame, role, samples):
    # Called for the five invocation-local targets only. Never for TRANSPORT.
    values = frame.f_locals
    for field in _T13_FIELDS[role]:
        value = values.get('_t13_' + field)
        samples[field] = _t13_number(value) if type(value) is int else None
    if role != 'SUBMIT':
        mask = values.get('_t13_faults')
        cap = {'RUN': 1, 'MUTATE': 15, 'COMMIT': 127, 'GUARD': 1}[role]
        samples[role.lower() + '_faults'] = mask if type(mask) is int and 0 <= mask <= cap else None
    if role in ('SUBMIT', 'MUTATE', 'COMMIT', 'GUARD'):
        deadline = values.get('deadline_ns')
        if type(deadline) is int:
            samples['deadline_ns'] = _t13_number(deadline)
    if role == 'SUBMIT':
        samples['remaining'] = _t13_number(values.get('remaining'))
    samples[role.lower() + '_status'] = 'COMPLETE'
    if any(samples[field] is None for field in _T13_FIELDS[role]):
        samples[role.lower() + '_status'] = 'NOT_REACHED'
    invalid = any('_t13_' + field in values and samples[field] is None
                  for field in _T13_FIELDS[role])
    invalid_mask = role != 'SUBMIT' and samples[role.lower() + '_faults'] is None
    if invalid or invalid_mask or samples.get(role.lower() + '_faults'):
        samples[role.lower() + '_status'] = 'UNKNOWN'


def _t13_trace(tb, codes):
    samples = {role.lower() + '_status': 'NOT_OBSERVED' for role in _T13_FIELDS}
    roles, sites = [], []
    reason = None
    for _ in range(128):
        if tb is None:
            break
        role = next((name for code, name in codes.items()
                     if tb.tb_frame.f_code is code), None)
        if role in _T13_FIELDS and role not in roles:
            _t13_copy(tb.tb_frame, role, samples)
        if role is not None:
            roles.append(role)
            sites.append(tb.tb_lineno)
        elif roles:
            roles.append('UNRECOGNIZED')
            sites.append(0)
        tb = tb.tb_next
    if tb is not None:
        reason = 'TRACE_LIMIT'
    # No cause/context/f_back walk and no transport-local access.
    for left, right, line in zip(roles, roles[1:], sites, strict=False):
        if line not in _T13_EDGES.get((left, right), ()):
            reason = 'BINDING_AMBIGUOUS'
    dispatch = _t13_binding(roles)
    if reason is not None:
        dispatch = 'UNKNOWN'
    return samples, dispatch, reason


def _t13_windows(samples, qualified, dispatch):
    result = {}
    for name, start, end in _T13_WINDOWS:
        for suffix in ('_wall_ns', '_cpu_ns'):
            allowed = qualified and (name != 'admission_to_worker' or
                                      (dispatch == 'QUEUED' and suffix == '_wall_ns'))
            result[name + suffix] = _t13_delta(samples.get(start + suffix), samples.get(end + suffix), allowed)
        if samples.get(end + '_wall_ns') is None:
            result[name + '_to_guard_wall_ns'] = _t13_delta(
                samples.get(start + '_wall_ns'), samples.get('rejecting_guard_wall_ns'), qualified)
            result[name + '_to_post_guard_cpu_ns'] = _t13_delta(
                samples.get(start + '_cpu_ns'), samples.get('rejecting_guard_post_cpu_ns'),
                qualified and name != 'admission_to_worker')
    return result


def _t13_failure(item, call, report, ordinal):
    role = 'REPRESENTATIVE' if item.nodeid == _T13_REPRESENTATIVE else 'OTHER'
    tb = call.excinfo.value.__traceback__ if call.excinfo is not None else None
    samples, dispatch, reason = _t13_trace(tb, _t13_codes())
    clock_ok = _T13_STATE['clock_ok'] and _t13_clocks()
    qualified = role == 'REPRESENTATIVE' and clock_ok and dispatch != 'UNKNOWN'
    fault = any(samples.get(role.lower() + '_faults')
                or samples.get(role.lower() + '_status') == 'UNKNOWN' for role in _T13_FIELDS)
    if not clock_ok or role != 'REPRESENTATIVE':
        reason = 'CLOCK_UNVERIFIED'
    elif dispatch == 'UNKNOWN':
        reason = reason or 'BINDING_AMBIGUOUS'
    if fault:
        reason = 'SAMPLE_FAULT'
    if reason is not None:
        _t13_incomplete()
    record = {'schema': 'T13_OBS_V1', 'kind': 'FAILURE', 'ordinal': ordinal,
              'phase': {'setup': 'SETUP', 'call': 'CALL', 'teardown': 'TEARDOWN'}.get(report.when, 'UNKNOWN'),
              'role': role, 'dispatch': dispatch, 'samples': samples,
              'windows': _t13_windows(samples, qualified and not fault, dispatch),
              'status': 'COMPLETE' if qualified and not fault else 'UNKNOWN', 'reason': reason}
    return _t13_json(record, 8192)


def _t13_reserve(text):
    # Budget includes fixed report/terminal labels and newline allowance.
    size = len(text) + 128
    if _T13_STATE['bytes'] + size > 65536:
        _T13_STATE['incomplete'] = True
        return False
    _T13_STATE['bytes'] += size
    return True


def _t13_snapshot(checkpoint):
    if len(_T13_STATE['snapshots']) >= 4:
        return
    record = {'schema': 'T13_OBS_V1', 'kind': 'RESOURCE', 'checkpoint': checkpoint,
              'wall_ns': _t13_number(_T13_NATIVE_WALL()),
              'case_owned_survivors': 'UNKNOWN', 'session_owned_writers': 'UNKNOWN',
              'rss_unit': 'PLATFORM_NATIVE_UNKNOWN', 'cpu_unit': 'SECONDS',
              'status': 'COMPLETE'}
    threads = threading.enumerate()
    record['threads'] = _t13_number(len(threads))
    record['writers'] = _t13_number(sum(t.name == 'lite-single-writer' for t in threads))
    usage = resource.getrusage(resource.RUSAGE_SELF) if resource is not None else None
    for name in _T13_COUNTERS:
        record[name] = _t13_number(getattr(usage, name, None))
    if any(record[name] is None for name in _T13_COUNTERS):
        record['status'] = 'INCOMPLETE'
        record['reason'] = 'UNSUPPORTED_RESOURCE'
        _T13_STATE['incomplete'] = True
    text = _t13_json(record, 2048)
    if _t13_reserve(text):
        _T13_STATE['snapshots'].append(text)


def _t13_incomplete():
    _T13_STATE['incomplete'] = True


def pytest_sessionstart(session):
    try:
        _t13_snapshot('SESSION_START')
    except Exception:  # noqa: BLE001 - in-flight observation, not pytest work
        _t13_incomplete()


def pytest_collection_finish(session):
    try:
        _T13_STATE['clock_ok'] = _t13_clocks()
        if not _T13_STATE['clock_ok']:
            _t13_incomplete()
        for index, item in enumerate(session.items):
            if item.nodeid.startswith(_T13_MODULE):
                _T13_STATE['capture_last'] = index
    except Exception:  # noqa: BLE001 - control-flow BaseException remains original
        _t13_incomplete()


def pytest_runtest_setup(item):
    if not _T13_STATE['capture_started'] and item.nodeid.startswith(_T13_MODULE):
        _T13_STATE['capture_started'] = True
        try:
            _t13_snapshot('BEFORE_CAPTURE')
        except Exception:  # noqa: BLE001 - only the added pre-setup observation
            _t13_incomplete()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    result = yield
    try:
        index = _T13_STATE['capture_last']
        if index >= 0 and item is item.session.items[index]:
            _t13_snapshot('AFTER_CAPTURE')
    except BaseException:  # noqa: BLE001 - settled teardown result remains primary
        _t13_incomplete()
    return result


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if not report.failed:
        return
    try:
        representative = item.nodeid == _T13_REPRESENTATIVE
        allowed = not _T13_STATE['representative'] if representative else _T13_STATE['others'] < 4
        if not allowed:
            _T13_STATE['omitted'] = min(2**31 - 1, _T13_STATE['omitted'] + 1)
            return
        ordinal = 0 if representative else _T13_STATE['others'] + 1
        if representative:
            _T13_STATE['representative'] = True
        else:
            _T13_STATE['others'] += 1
        text = _t13_failure(item, call, report, ordinal)
        if _t13_reserve(text):
            report.sections.append(('T13_OBS_V1', text))
            _T13_STATE['records'].append(text)
    except BaseException:  # noqa: BLE001 - original failed report remains primary
        _t13_incomplete()
        try:
            text = '{"schema":"T13_OBS_V1","status":"INCOMPLETE","reason":"REPORT_FAULT"}'
            if _t13_reserve(text):
                report.sections.append(('T13_OBS_V1', text))
        except BaseException:  # noqa: BLE001 - failed diagnostic output is UNKNOWN
            return


def pytest_sessionfinish(session, exitstatus):
    try:
        _t13_snapshot('SESSION_FINISH')
        reporter = session.config.pluginmanager.get_plugin('terminalreporter')
        if reporter is None:
            _t13_incomplete()
            return
        for text in _T13_STATE['snapshots']:
            reporter.write_line(text)
        summary = _t13_json({'schema': 'T13_OBS_V1', 'kind': 'SUMMARY',
                             'records': len(_T13_STATE['records']),
                             'snapshots': len(_T13_STATE['snapshots']),
                             'omitted': _T13_STATE['omitted'],
                             'status': 'INCOMPLETE' if _T13_STATE['incomplete'] else 'COMPLETE'}, 1024)
        if _t13_reserve(summary):
            reporter.write_line(summary)
        _T13_STATE['records'].clear()
        _T13_STATE['snapshots'].clear()
    except BaseException:  # noqa: BLE001 - original session exit remains primary
        _t13_incomplete()
        try:
            reporter = session.config.pluginmanager.get_plugin('terminalreporter')
            text = '{"schema":"T13_OBS_V1","status":"INCOMPLETE","reason":"REPORT_FAULT"}'
            if reporter is not None and _t13_reserve(text):
                reporter.write_line(text)
        except BaseException:  # noqa: BLE001 - unavailable output proves nothing
            return


def pytest_unconfigure(config):
    try:
        _T13_STATE['records'].clear()
        _T13_STATE['snapshots'].clear()
    except BaseException:  # noqa: BLE001 - observer-only release cannot replace result
        return
