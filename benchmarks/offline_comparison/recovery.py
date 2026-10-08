"""Local Linux process ownership for the offline pilot; never a background service.

The supervisor inherits the runner's flock, owns/reaps the worker process group,
and observes a pipe that closes if the runner dies. Only its durable cleanup proof
permits retry. A stale heartbeat is diagnostic, never permission to take over.
"""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import json
import math
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROTOCOL = 2
HEARTBEAT_SECONDS = 20.0
MAX_INFRA_RETRIES = 1
CLEANUP_SECONDS = 1.0


class RecoveryBlocked(RuntimeError):
    """Evidence or ownership is insufficient to resume safely."""


def atomic_text(path, text):
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


class RunLock:
    def __init__(self, out):
        self.path = out / 'run.lock'
        self.fd = None
        self.owner_id = uuid.uuid4().hex

    def __enter__(self):
        if sys.platform != 'linux':
            raise RecoveryBlocked('Safe process-tree recovery requires Linux subreaping')
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(self.fd)
            self.fd = None
            raise RecoveryBlocked('Run is already owned; heartbeat age cannot override its lock') from error
        return self

    def __exit__(self, *_args):
        # Do NOT LOCK_UN: inherited descriptors retain ownership after runner death.
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def load_journal(out, receipt):
    path = out / 'attempts.json'
    if not path.exists():
        return {'protocol': PROTOCOL, 'receipt_sha256': receipt, 'attempts': []}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise RecoveryBlocked('Malformed or unreadable attempt journal') from error
    if (not isinstance(value, dict) or value.get('protocol') != PROTOCOL
            or value.get('receipt_sha256') != receipt or not isinstance(value.get('attempts'), list)):
        raise RecoveryBlocked('Attempt journal identity mismatch or malformed journal')
    attempts = value['attempts']
    if any(not isinstance(a, dict) for a in attempts):
        raise RecoveryBlocked('Malformed attempt journal')
    ids = [a.get('attempt_id') for a in attempts]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise RecoveryBlocked('Attempt journal contains invalid or duplicate identities')
    for a in attempts:
        if not isinstance(a.get('question_id'), str) or a.get('state') not in ('prepared', 'running', 'result', 'interrupted'):
            raise RecoveryBlocked('Malformed attempt journal')
        validate_terminal_attempt(a)
        if a['state'] in ('prepared', 'running') or a.get('tree_reaped') is not True:
            raise RecoveryBlocked('Uncertain prior ownership; manual evidence review required, never heartbeat takeover')
    return value



def validate_product_result(result, *, allow_timeout=False):
    """Validate exactly the fields consumed by scoring and aggregation."""
    statuses = ('complete', 'error', 'timeout') if allow_timeout else ('complete', 'error')
    if not isinstance(result, dict) or result.get('status') not in statuses:
        raise ValueError('Unrecognized product result')
    if result['status'] != 'complete':
        if not isinstance(result.get('error'), str):
            raise ValueError('Product failure lacks its diagnostic')
        return
    ranked = result.get('ranked_session_ids')
    if not isinstance(ranked, list) or any(not isinstance(sid, str) for sid in ranked):
        raise ValueError('Invalid ranked session IDs')
    for key in ('ingest_ms', 'query_ms', 'storage_bytes'):
        value = result.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError(f'Invalid {key}')
    words = result.get('returned_word_tokens')
    if not isinstance(words, dict) or any(type(words.get(str(k))) is not int or words[str(k)] < 0 for k in (5, 10)):
        raise ValueError('Invalid returned word counts')


def validate_terminal_attempt(attempt):
    if attempt['state'] == 'result':
        try:
            validate_product_result(attempt.get('result'), allow_timeout=True)
        except ValueError as error:
            raise RecoveryBlocked('Malformed terminal result in attempt journal') from error
    if attempt['state'] == 'interrupted':
        if type(attempt.get('retryable')) is not bool:
            raise RecoveryBlocked('Malformed interruption in attempt journal')
        if attempt['retryable'] and attempt.get('reason') != 'owner_disconnected':
            raise RecoveryBlocked('Malformed retry proof: only confirmed owner interruption is eligible')


def next_attempt(journal, qid):
    previous = [a for a in journal['attempts'] if a['question_id'] == qid]
    if not previous:
        return None
    latest = previous[-1]
    if latest['state'] == 'result':
        return latest['result']
    if not latest.get('retryable'):
        raise RecoveryBlocked(latest.get('reason', 'Unproven infrastructure interruption'))
    if len(previous) > MAX_INFRA_RETRIES:
        raise RecoveryBlocked('Persisted infrastructure retry budget exhausted; question remains pending')
    return None



def checkpoint_identity(out):
    path = out / 'records.jsonl'
    data = path.read_bytes() if path.exists() else b''
    records = [json.loads(line) for line in data.splitlines()]
    return {'recorded_questions': len(records),
            'last_question_id': records[-1]['question_id'] if records else None,
            'sha256': hashlib.sha256(data).hexdigest()}


def run_attempt(out, lock, receipt, qid, command, result_path, env, cwd, timeout,
                heartbeat_seconds=HEARTBEAT_SECONDS):
    """Return a durable result or block; never classify a bare exit as product failure."""
    journal = load_journal(out, receipt)
    previous = next_attempt(journal, qid)
    if previous is not None:
        return previous
    attempt = {'attempt_id': uuid.uuid4().hex, 'question_id': qid, 'owner_id': lock.owner_id,
               'owner_pid': os.getpid(), 'state': 'prepared', 'tree_reaped': False,
               'started_at': time.time()}
    journal['attempts'].append(attempt)
    atomic_json(out / 'attempts.json', journal)
    read_fd, write_fd = os.pipe()
    checkpoint = checkpoint_identity(out)
    request = {'out': str(out.resolve()), 'journal': journal, 'command': command,
               'checkpoint': checkpoint,
               'result_path': str(result_path.resolve()), 'env': env, 'cwd': str(cwd),
               'timeout': timeout, 'heartbeat_seconds': heartbeat_seconds,
               'owner_pipe': read_fd, 'lock_fd': lock.fd}
    try:
        with tempfile.TemporaryDirectory(prefix='offline-supervisor-') as directory:
            spec_path = Path(directory) / 'request.json'
            atomic_json(spec_path, request)
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(spec_path)],
                                       pass_fds=(read_fd, lock.fd), start_new_session=True,
                                       stdin=subprocess.DEVNULL)
            os.close(read_fd)
            read_fd = None
            try:
                process.wait()
            finally:
                os.close(write_fd)
                write_fd = None
                # Owner-pipe closure tells the supervisor to reap before returning.
                process.wait()
    finally:
        for fd in (read_fd, write_fd):
            if fd is not None:
                os.close(fd)
    updated = load_journal(out, receipt)
    result = next_attempt(updated, qid)
    if result is None:
        raise RecoveryBlocked('Confirmed owner interruption; question remains pending for a bounded resume')
    return result


def enable_subreaper():
    if sys.platform != 'linux':
        raise RecoveryBlocked('Safe process-tree recovery requires Linux subreaping')
    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'Cannot enable process-tree reaping')


def children():
    """Direct and adopted descendants, scoped to this supervisor's PID namespace."""
    # Some kernels omit /proc/<pid>/task/<tid>/children. PPid is available
    # without that optional kernel feature. Refuse mismatched proc namespaces.
    if int(Path('/proc/self/stat').read_text().split()[0]) != os.getpid():
        raise RecoveryBlocked('Process visibility does not match the supervisor namespace')
    result = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            status = (entry / 'status').read_text()
        except FileNotFoundError:
            continue
        parent = next(line.split()[1] for line in status.splitlines() if line.startswith('PPid:'))
        if int(parent) == os.getpid():
            result.append(int(entry.name))
    return result


def reap_available():
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        if pid == 0:
            return False


def reap_tree(process):
    """Terminate owned children and prove ECHILD before releasing ownership."""
    # A reaped leader's numeric PID may have been reused. Only signal its group
    # while it is still our unreaped child; afterwards use adopted child PIDs.
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for terminating_signal in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + CLEANUP_SECONDS
        while time.monotonic() < deadline:
            for pid in children():
                try:
                    os.kill(pid, terminating_signal)
                except ProcessLookupError:
                    pass
            process.poll()
            if reap_available():
                return True
            time.sleep(.02)
    return False


def monitor_worker(process, request, heartbeat):
    deadline = time.monotonic() + request['timeout']
    next_heartbeat = 0
    owner_events = select.poll()
    owner_events.register(request['owner_pipe'], select.POLLIN | select.POLLHUP)
    while True:
        now = time.monotonic()
        if now >= next_heartbeat:
            heartbeat['sequence'] += 1
            heartbeat['updated_at'] = time.time()
            heartbeat['worker_pid'] = process.pid
            atomic_json(Path(request['out']) / 'heartbeat.json', heartbeat)
            next_heartbeat = now + request['heartbeat_seconds']
        if process.poll() is not None:
            return 'worker_exit'
        if owner_events.poll(0) and not os.read(request['owner_pipe'], 1):
            return 'owner_disconnected'
        if now >= deadline:
            return 'deadline'
        time.sleep(min(.02, max(0, deadline - now)))


def worker_outcome(reason, process, request):
    if reason == 'deadline':
        return {'state': 'result', 'result': {'status': 'timeout',
                'error': f"Exceeded per-question {request['timeout']}s budget"}}
    result_path = Path(request['result_path'])
    if reason == 'owner_disconnected' and not result_path.exists():
        return {'state': 'interrupted', 'reason': reason, 'retryable': True}
    if reason != 'owner_disconnected' and process.returncode != 0:
        return {'state': 'interrupted', 'reason': 'unproven_worker_exit',
                'returncode': process.returncode, 'retryable': False}
    # Inspect only after reaping: a fully published result wins over owner EOF,
    # while an existing malformed result is ambiguous and must never be replayed.
    try:
        result = json.loads(result_path.read_text())
        validate_product_result(result)
    except (OSError, ValueError):
        return {'state': 'interrupted', 'reason': 'invalid_or_missing_worker_result', 'retryable': False}
    return {'state': 'result', 'result': result}


def await_cleanup(process, request, journal, heartbeat):
    """Keep ownership, including on cleanup faults, until ECHILD is proved."""
    attempt = journal['attempts'][-1]
    while True:
        try:
            if reap_tree(process):
                return
        except Exception as error:  # noqa: BLE001 - fail closed while retaining the ownership lease.
            attempt['cleanup_error'] = f'{type(error).__name__}: {error}'
        attempt.update(state='interrupted', retryable=False, tree_reaped=False,
                       reason='process_tree_cleanup_unproven')
        heartbeat.update(phase='cleanup_unproven', updated_at=time.time(),
                         sequence=heartbeat['sequence'] + 1)
        try:
            atomic_json(Path(request['out']) / 'heartbeat.json', heartbeat)
            atomic_json(Path(request['out']) / 'attempts.json', journal)
        except OSError as error:
            # Even an unavailable evidence disk cannot authorize lease release.
            try:
                print(f'Cleanup evidence unavailable; retaining ownership: {error}', file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass  # A disconnected log stream cannot release process ownership.
        time.sleep(request['heartbeat_seconds'])


def supervise(request):
    """This process retains the inherited run lock until all children are reaped."""
    enable_subreaper()
    out = Path(request['out'])
    journal = request['journal']
    attempt = journal['attempts'][-1]
    attempt.update(state='running', supervisor_pid=os.getpid())
    atomic_json(out / 'attempts.json', journal)
    heartbeat = {key: attempt[key] for key in ('owner_id', 'owner_pid', 'attempt_id', 'question_id', 'supervisor_pid')}
    heartbeat.update(receipt_sha256=journal['receipt_sha256'], sequence=0, checkpoint=request['checkpoint'])
    # Worker logs cannot fill a pipe or leak credentials to the supervisor.
    with (out / f"worker-{attempt['attempt_id']}.log").open('wb') as log:
        process = subprocess.Popen(request['command'], cwd=request['cwd'], env=request['env'],
                                   pass_fds=(request['lock_fd'],), start_new_session=True,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            reason = monitor_worker(process, request, heartbeat)
        finally:
            await_cleanup(process, request, journal, heartbeat)
        outcome = worker_outcome(reason, process, request)
        for key in ('reason', 'retryable', 'cleanup_error'):
            attempt.pop(key, None)
        attempt.update(outcome, tree_reaped=True, finished_at=time.time())
        atomic_json(out / 'attempts.json', journal)
    return 0


if __name__ == '__main__':
    raise SystemExit(supervise(json.loads(Path(sys.argv[1]).read_text())))
