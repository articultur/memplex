"""Fault-injection contracts for local, credential-free benchmark recovery."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def harness():
    spec = importlib.util.spec_from_file_location('offline_protocol', ROOT / 'benchmarks/offline_comparison/longmemeval.py')
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


class TestAtomicCheckpoint(unittest.TestCase):
    def test_atomic_json_fsyncs_parent_directory_after_replace(self):
        h = harness()
        calls = []
        real_fsync = os.fsync
        def fsync(fd):
            calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            real_fsync(fd)
        with tempfile.TemporaryDirectory() as directory, patch.object(h.os, 'fsync', fsync):
            h.atomic_json(Path(directory) / 'state.json', {'ready': True})
        self.assertEqual(calls, ['file', 'directory'])

    def test_fault_before_replace_keeps_canonical_records_unchanged(self):
        h = harness()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            original = b'{"question_id":"q1"}\n'
            path.write_bytes(original)
            with patch.object(os, 'replace', side_effect=OSError('injected before replace')), self.assertRaisesRegex(OSError, 'injected'):
                h.publish_records(path, [{'question_id': 'q1'}, {'question_id': 'q2'}])
            self.assertEqual(path.read_bytes(), original)

    def test_atomic_checkpoint_publishes_all_rows_and_trailing_newline(self):
        h = harness()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            h.publish_records(path, [{'question_id': 'q1'}, {'question_id': 'q2'}])
            self.assertEqual([json.loads(x)['question_id'] for x in path.read_text().splitlines()], ['q1', 'q2'])
            self.assertTrue(path.read_bytes().endswith(b'\n'))

    def test_torn_records_fail_closed_without_touching_any_evidence(self):
        h = harness()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'receipt.json').write_text('{"version":2}')
            (root / 'records.jsonl').write_text('{"question_id":')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaises(ValueError):
                h.prepare_resume(root, {'version': 2}, {'pilot_ids': ['q1']})
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})


def recovery():
    path = ROOT / 'benchmarks/offline_comparison/recovery.py'
    assert path.exists(), 'Recovery supervisor is required'
    spec = importlib.util.spec_from_file_location('offline_recovery', path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestRecoverySupervisor(unittest.TestCase):
    def test_same_run_lock_cannot_be_acquired_twice(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory, r.RunLock(Path(directory)), self.assertRaisesRegex(r.RecoveryBlocked, 'owned'), r.RunLock(Path(directory)):
            self.fail('A second runner took ownership')

    def test_worker_success_and_product_error_are_terminal_results(self):
        import sys
        r = recovery()
        for result in ({'status': 'complete', 'ranked_session_ids': ['s1'], 'ingest_ms': 1, 'query_ms': 1,
                        'storage_bytes': 0, 'returned_word_tokens': {'5': 0, '10': 0}},
                       {'status': 'error', 'error': 'product failure'}):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                result_path = out / 'result.json'
                command = [sys.executable, '-c', f'from pathlib import Path; Path({str(result_path)!r}).write_text({json.dumps(result)!r})']
                with r.RunLock(out) as lock:
                    actual = r.run_attempt(out, lock, 'receipt', 'q1', command, result_path,
                                           dict(os.environ), ROOT, 2, heartbeat_seconds=.03)
                self.assertEqual(actual, result)
                attempt = json.loads((out / 'attempts.json').read_text())['attempts'][0]
                self.assertEqual(attempt['state'], 'result')
                self.assertTrue(attempt['tree_reaped'])

    def test_external_nonzero_is_not_a_product_failure_or_automatically_retried(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            command = [sys.executable, '-c', 'raise SystemExit(7)']
            with r.RunLock(out) as lock:
                for _ in range(2):
                    with self.assertRaisesRegex(r.RecoveryBlocked, 'unproven_worker_exit'):
                        r.run_attempt(out, lock, 'receipt', 'q1', command, out / 'result.json',
                                      dict(os.environ), ROOT, 2, heartbeat_seconds=.03)
            self.assertEqual(len(json.loads((out / 'attempts.json').read_text())['attempts']), 1)
            self.assertFalse((out / 'records.jsonl').exists())

    def test_only_intentional_deadline_produces_timeout_and_heartbeat_has_identity(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with r.RunLock(out) as lock:
                result = r.run_attempt(out, lock, 'receipt', 'q1', [sys.executable, '-c', 'import time; time.sleep(10)'],
                                       out / 'result.json', dict(os.environ), ROOT, .15, heartbeat_seconds=.03)
            self.assertEqual(result['status'], 'timeout')
            heartbeat = json.loads((out / 'heartbeat.json').read_text())
            self.assertEqual(heartbeat['question_id'], 'q1')
            self.assertEqual(heartbeat['receipt_sha256'], 'receipt')
            self.assertIn('attempt_id', heartbeat)
            self.assertIn('owner_id', heartbeat)
            self.assertIn('supervisor_pid', heartbeat)
            self.assertGreaterEqual(heartbeat['sequence'], 2)
            self.assertTrue(json.loads((out / 'attempts.json').read_text())['attempts'][0]['tree_reaped'])

    def test_completed_result_is_reused_without_relaunch(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            result_path = out / 'result.json'
            command = [sys.executable, '-c', f'from pathlib import Path; Path({str(result_path)!r}).write_text(\'{{"status":"error","error":"product"}}\')']
            with r.RunLock(out) as lock:
                first = r.run_attempt(out, lock, 'receipt', 'q1', command, result_path, dict(os.environ), ROOT, 2)
            with r.RunLock(out) as lock:
                second = r.run_attempt(out, lock, 'receipt', 'q1', ['must-not-launch'], result_path, dict(os.environ), ROOT, 2)
            self.assertEqual(first, second)
            self.assertEqual(len(json.loads((out / 'attempts.json').read_text())['attempts']), 1)


def wait_for(predicate, seconds=5):
    import time
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('Condition did not become true')


def launch_owner(out, worker_code):
    import subprocess
    import sys
    code = f'''
import importlib.util, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('recovery', {str(ROOT / 'benchmarks/offline_comparison/recovery.py')!r})
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
out = Path({str(out)!r})
with r.RunLock(out) as lock:
    r.run_attempt(out, lock, 'receipt', 'q1', [sys.executable, '-c', {worker_code!r}],
                  out / 'result.json', dict(os.environ), {str(ROOT)!r}, 30, heartbeat_seconds=.03)
'''
    return subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestInterruptedOwnership(unittest.TestCase):
    def test_parent_death_reaps_descendants_before_release_and_retry_is_bounded(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            marker = out / 'pids.json'
            worker = f'''
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'], start_new_session=True)
Path({str(marker)!r}).write_text(json.dumps([os.getpid(), child.pid]))
time.sleep(30)
'''
            for _ in range(2):
                marker.unlink(missing_ok=True)
                owner = launch_owner(out, worker)
                try:
                    wait_for(marker.exists)
                    pids = json.loads(marker.read_text())
                    owner.kill()
                    owner.wait()
                    with self.assertRaises(r.RecoveryBlocked), r.RunLock(out):
                        self.fail('Inherited lock released before descendant cleanup')
                    wait_for(lambda: json.loads((out / 'attempts.json').read_text())['attempts'][-1]['state'] == 'interrupted')
                    for pid in pids:
                        self.assertFalse(Path(f'/proc/{pid}').exists(), f'Unreaped descendant {pid}')
                    def available():
                        try:
                            with r.RunLock(out):
                                return True
                        except r.RecoveryBlocked:
                            return False
                    wait_for(available)
                finally:
                    if owner.poll() is None:
                        owner.kill()
                    owner.wait()
            with r.RunLock(out) as lock, self.assertRaisesRegex(r.RecoveryBlocked, 'budget exhausted'):
                r.run_attempt(out, lock, 'receipt', 'q1', [sys.executable, '-c', 'pass'],
                              out / 'result.json', dict(os.environ), ROOT, 2)
            attempts = json.loads((out / 'attempts.json').read_text())['attempts']
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(a['retryable'] and a['tree_reaped'] for a in attempts))

    def test_stale_heartbeat_never_authorizes_unresolved_attempt(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            journal = {'protocol': 2, 'receipt_sha256': 'receipt', 'attempts': [
                {'attempt_id': 'old', 'question_id': 'q1', 'state': 'running', 'tree_reaped': False}]}
            (out / 'attempts.json').write_text(json.dumps(journal))
            (out / 'heartbeat.json').write_text('{"updated_at":0,"owner_pid":9999999}')
            with r.RunLock(out) as lock, self.assertRaisesRegex(r.RecoveryBlocked, 'Uncertain prior ownership'):
                r.run_attempt(out, lock, 'receipt', 'q1', ['must-not-launch'], out / 'result.json', {}, ROOT, 2)
            self.assertEqual(json.loads((out / 'attempts.json').read_text()), journal)

    def test_corrupt_attempt_entry_fails_with_recovery_blocked(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / 'attempts.json').write_text('{"protocol":2,"receipt_sha256":"receipt","attempts":[null]}')
            with self.assertRaises(r.RecoveryBlocked):
                r.load_journal(out, 'receipt')


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestHarnessIntegration(unittest.TestCase):
    def fixtures(self, directory):
        from types import SimpleNamespace

        from tests.test_offline_comparison import sample
        h = harness()
        rows = [sample(f'q{i}') for i in range(500)]
        manifest = h.build_manifest(rows)
        path = Path(directory) / 'manifest.json'
        path.write_text(json.dumps(manifest))
        args = SimpleNamespace(dataset='mock-data', manifest=str(path), out=str(Path(directory) / 'run'),
                               timeout=120, run_id='new-protocol-test')
        return h, rows, manifest, args

    def test_new_harness_requires_explicit_new_run_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            h, rows, _manifest, args = self.fixtures(directory)
            del args.run_id
            with patch.object(h, 'load_dataset', return_value=rows), patch.object(h, 'child_run', return_value={'status': 'timeout'}), self.assertRaisesRegex(ValueError, 'run-id'):
                h.run(args)

    def test_run_uses_atomic_records_under_lock_and_complete_resume_launches_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            h, rows, manifest, args = self.fixtures(directory)
            r = recovery()
            launched = []
            def child(public, timeout, **kwargs):
                self.assertEqual(timeout, 120)
                self.assertEqual(kwargs['out'], Path(args.out))
                with self.assertRaises(r.RecoveryBlocked), r.RunLock(Path(args.out)):
                    self.fail('Worker launched without owning run lock')
                launched.append(public['question_id'])
                return {'status': 'timeout', 'error': 'budget'}
            with patch.object(h, 'load_dataset', return_value=rows), patch.object(h, 'child_run', side_effect=child), patch.object(h, 'publish_records', wraps=h.publish_records) as publish:
                self.assertEqual(h.run(args), 2)
                self.assertEqual(publish.call_count, 100)
                self.assertEqual(h.run(args), 2)
            self.assertEqual(launched, manifest['pilot_ids'])
            receipt_path = Path(args.out) / 'receipt.json'
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt['run_id'], args.run_id)
            self.assertEqual(receipt['recovery_protocol'], 2)
            self.assertEqual(receipt['recovery_sha256'], h.file_hash(ROOT / 'benchmarks/offline_comparison/recovery.py'))

    def test_infrastructure_interruption_leaves_question_pending_and_receipt_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            h, rows, _manifest, args = self.fixtures(directory)
            r = recovery()
            with patch.object(h, 'load_dataset', return_value=rows), patch.object(h, 'child_run', side_effect=r.RecoveryBlocked('interruption')), self.assertRaises(r.RecoveryBlocked):
                h.run(args)
            out = Path(args.out)
            self.assertFalse((out / 'records.jsonl').exists())
            self.assertEqual(json.loads((out / 'summary.json').read_text())['recorded_questions'], 0)
            original = (out / 'receipt.json').read_bytes()
            args.run_id = 'different-run'
            with patch.object(h, 'load_dataset', return_value=rows), self.assertRaisesRegex(ValueError, 'mismatch'):
                h.run(args)
            self.assertEqual((out / 'receipt.json').read_bytes(), original)

    def test_attempt_journal_without_original_receipt_is_not_certified(self):
        h = harness()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'attempts.json').write_text('{"old":"evidence"}')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                h.prepare_resume(root, {'recovery_protocol': 2}, {'pilot_ids': []})
            self.assertFalse((root / 'receipt.json').exists())


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestAdditionalFaults(unittest.TestCase):
    def test_directory_fsync_failure_leaves_complete_new_checkpoint(self):
        h = harness()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'records.jsonl'
            path.write_text('{"question_id":"q1"}\n')
            real = os.fsync
            def fsync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError('directory durability unknown')
                return real(fd)
            with patch.object(os, 'fsync', fsync), self.assertRaises(OSError):
                h.publish_records(path, [{'question_id': 'q1'}, {'question_id': 'q2'}])
            self.assertEqual([json.loads(x)['question_id'] for x in path.read_text().splitlines()], ['q1', 'q2'])

    def test_killed_supervisor_leaves_inherited_lock_and_uncertain_journal(self):
        import signal
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            worker = f'import os,time; from pathlib import Path; Path({str(out / "worker.pid")!r}).write_text(str(os.getpid())); time.sleep(30)'
            owner = launch_owner(out, worker)
            worker_pid = None
            try:
                wait_for((out / 'worker.pid').exists)
                worker_pid = int((out / 'worker.pid').read_text())
                state = json.loads((out / 'attempts.json').read_text())['attempts'][-1]
                os.kill(state['supervisor_pid'], signal.SIGKILL)
                owner.wait(timeout=5)
                with self.assertRaises(r.RecoveryBlocked), r.RunLock(out):
                    self.fail('Worker lost its inherited lock')
                os.kill(worker_pid, signal.SIGKILL)
                def unlocked():
                    try:
                        with r.RunLock(out):
                            return True
                    except r.RecoveryBlocked:
                        return False
                wait_for(unlocked)
                with self.assertRaisesRegex(r.RecoveryBlocked, 'Uncertain prior ownership'):
                    r.load_journal(out, 'receipt')
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait()
                if worker_pid is not None:
                    try:
                        os.kill(worker_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_signal_killed_worker_is_not_timeout_and_never_retried(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with r.RunLock(out) as lock, self.assertRaisesRegex(r.RecoveryBlocked, 'unproven_worker_exit'):
                r.run_attempt(out, lock, 'receipt', 'q1',
                              [sys.executable, '-c', 'import os,signal; os.kill(os.getpid(), signal.SIGTERM)'],
                              out / 'result.json', dict(os.environ), ROOT, 2)
            entry = json.loads((out / 'attempts.json').read_text())['attempts'][0]
            self.assertFalse(entry['retryable'])
            self.assertNotIn('result', entry)

    def test_heartbeat_identifies_last_durable_checkpoint(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / 'records.jsonl').write_text('{"question_id":"q0"}\n')
            with r.RunLock(out) as lock:
                r.run_attempt(out, lock, 'receipt', 'q1', [sys.executable, '-c', 'import time; time.sleep(10)'],
                              out / 'result.json', dict(os.environ), ROOT, .1, heartbeat_seconds=.02)
            heartbeat = json.loads((out / 'heartbeat.json').read_text())
            self.assertEqual(heartbeat['checkpoint']['recorded_questions'], 1)
            self.assertEqual(heartbeat['checkpoint']['last_question_id'], 'q0')
            self.assertEqual(len(heartbeat['checkpoint']['sha256']), 64)

    def test_finished_worker_result_wins_over_simultaneous_owner_eof(self):
        from types import SimpleNamespace
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            try:
                reason = r.monitor_worker(SimpleNamespace(pid=0, poll=lambda: 0),
                    {'out': directory, 'owner_pipe': read_fd, 'timeout': 120, 'heartbeat_seconds': 20},
                    {'sequence': 0})
            finally:
                os.close(read_fd)
            self.assertEqual(reason, 'worker_exit')


class TestCleanupIdentity(unittest.TestCase):
    def test_reaped_group_leader_pid_is_never_signalled_again(self):
        from types import SimpleNamespace
        r = recovery()
        already_reaped = SimpleNamespace(pid=123456, returncode=0, poll=lambda: 0, wait=lambda: 0)
        with patch.object(r.os, 'killpg') as killpg, patch.object(r, 'children', return_value=[]), patch.object(r.os, 'waitpid', side_effect=ChildProcessError):
            self.assertTrue(r.reap_tree(already_reaped))
        killpg.assert_not_called()


class TestJournalValidation(unittest.TestCase):
    def test_invalid_terminal_result_cannot_turn_into_a_retry(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            journal = {'protocol': 2, 'receipt_sha256': 'receipt', 'attempts': [
                {'attempt_id': 'a', 'question_id': 'q1', 'state': 'result', 'tree_reaped': True, 'result': None}]}
            (out / 'attempts.json').write_text(json.dumps(journal))
            with self.assertRaisesRegex(r.RecoveryBlocked, 'Malformed'):
                r.load_journal(out, 'receipt')

    def test_retryable_flag_alone_is_not_proof_of_owner_interruption(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            journal = {'protocol': 2, 'receipt_sha256': 'receipt', 'attempts': [
                {'attempt_id': 'a', 'question_id': 'q1', 'state': 'interrupted', 'tree_reaped': True,
                 'reason': 'unproven_worker_exit', 'retryable': True}]}
            (out / 'attempts.json').write_text(json.dumps(journal))
            with self.assertRaisesRegex(r.RecoveryBlocked, 'Malformed'):
                r.load_journal(out, 'receipt')

    def test_malformed_json_is_reported_as_blocked_without_rewrite(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            path = out / 'attempts.json'
            path.write_text('{"attempts":')
            with self.assertRaisesRegex(r.RecoveryBlocked, 'Malformed'):
                r.load_journal(out, 'receipt')
            self.assertEqual(path.read_text(), '{"attempts":')


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestReviewedRecoveryEdges(unittest.TestCase):
    def test_owner_disconnect_preserves_published_result_before_worker_exit(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            expected = {'status': 'error', 'error': 'original completed product result'}
            worker = f'''
import json, os, time
from pathlib import Path
with Path({str(out / 'result.json')!r}).open('w') as file:
    file.write({json.dumps(expected)!r}); file.flush(); os.fsync(file.fileno())
Path({str(out / 'ready')!r}).touch()
time.sleep(30)
'''
            owner = launch_owner(out, worker)
            try:
                wait_for((out / 'ready').exists)
                owner.kill()
                owner.wait()
                wait_for(lambda: json.loads((out / 'attempts.json').read_text())['attempts'][-1]['tree_reaped'])
                attempt = json.loads((out / 'attempts.json').read_text())['attempts'][-1]
                self.assertEqual(attempt['state'], 'result')
                self.assertEqual(attempt['result'], expected)
                def available():
                    try:
                        with r.RunLock(out):
                            return True
                    except r.RecoveryBlocked:
                        return False
                wait_for(available)
                with r.RunLock(out) as lock:
                    recovered = r.run_attempt(out, lock, 'receipt', 'q1', ['must-not-launch'],
                                              out / 'result.json', {}, ROOT, 2)
                self.assertEqual(recovered, expected)
                self.assertEqual(len(json.loads((out / 'attempts.json').read_text())['attempts']), 1)
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait()

    def test_worker_result_is_atomically_fsynced_before_publication(self):
        import socket
        h = harness()
        calls = []
        real_fsync = os.fsync
        def fsync(fd):
            calls.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
            real_fsync(fd)
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            (out / 'input.json').write_text('{}')
            with patch.object(h, 'run_memplex', return_value={'status': 'error', 'error': 'product'}), patch.object(socket.socket, 'connect'), patch.object(socket, 'create_connection'), patch.object(socket, 'getaddrinfo'), patch.object(os, 'fsync', fsync):
                h.offline_worker(out / 'input.json', out / 'result.json', out / 'bank')
            self.assertEqual(calls, ['file', 'directory'])
            self.assertEqual(json.loads((out / 'result.json').read_text())['status'], 'error')

    def test_malformed_terminal_worker_result_is_not_certified(self):
        import sys
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            command = [sys.executable, '-c', f'from pathlib import Path; Path({str(out / "result.json")!r}).write_text(\'{{"status":"complete"}}\')']
            with r.RunLock(out) as lock, self.assertRaisesRegex(r.RecoveryBlocked, 'invalid_or_missing_worker_result'):
                r.run_attempt(out, lock, 'receipt', 'q1', command, out / 'result.json', dict(os.environ), ROOT, 2)

    def test_cleanup_failure_retains_lock_until_reaping_is_proven(self):
        import signal
        import subprocess
        import sys
        r = recovery()
        for exception in (False, True):
            with self.subTest(exception=exception), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                marker, allow = out / 'descendant.pid', out / 'allow-cleanup'
                worker = f'''
import subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
Path({str(marker)!r}).write_text(str(child.pid))
Path({str(out / 'result.json')!r}).write_text('{{"status":"error","error":"finished"}}')
'''
                read_fd, write_fd = os.pipe()
                with r.RunLock(out) as lock:
                    request = {'out': str(out), 'journal': {'protocol': 2, 'receipt_sha256': 'receipt', 'attempts': [
                        {'attempt_id': 'a', 'question_id': 'q1', 'owner_id': 'owner', 'owner_pid': os.getpid(),
                         'state': 'prepared', 'tree_reaped': False}]}, 'command': [sys.executable, '-c', worker],
                        'result_path': str(out / 'result.json'), 'env': dict(os.environ), 'cwd': str(ROOT),
                        'timeout': 2, 'heartbeat_seconds': .02, 'owner_pipe': read_fd, 'lock_fd': lock.fd,
                        'checkpoint': {'recorded_questions': 0, 'last_question_id': None, 'sha256': 'empty'}}
                    spec = out / 'request.json'
                    spec.write_text(json.dumps(request))
                    code = f'''
import importlib.util, json
from pathlib import Path
spec = importlib.util.spec_from_file_location('r', {str(ROOT / 'benchmarks/offline_comparison/recovery.py')!r})
r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
real = r.reap_tree
r.HEARTBEAT_SECONDS = .02
r.CLEANUP_SECONDS = .05
def injected(process):
    if Path({str(allow)!r}).exists():
        return real(process)
    if {exception!r}:
        raise OSError('injected cleanup failure')
    return False
r.reap_tree = injected
r.supervise(json.loads(Path({str(spec)!r}).read_text()))
'''
                    supervisor = subprocess.Popen([sys.executable, '-c', code], pass_fds=(read_fd, lock.fd),
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                os.close(read_fd)
                descendant = None
                try:
                    wait_for(marker.exists)
                    descendant = int(marker.read_text())
                    def blocked_or_exited(out=out, supervisor=supervisor):
                        journal = out / 'attempts.json'
                        return supervisor.poll() is not None or (journal.exists() and json.loads(journal.read_text())['attempts'][-1].get('reason') == 'process_tree_cleanup_unproven')
                    wait_for(blocked_or_exited)
                    self.assertIsNone(supervisor.poll(), 'Supervisor released ownership while descendant survived')
                    with self.assertRaises(r.RecoveryBlocked), r.RunLock(out):
                        self.fail('Unproven cleanup lost the inherited ownership lock')
                    heartbeat = json.loads((out / 'heartbeat.json').read_text())
                    self.assertEqual(heartbeat['phase'], 'cleanup_unproven')
                    allow.touch()
                    supervisor.wait(timeout=5)
                    attempt = json.loads((out / 'attempts.json').read_text())['attempts'][-1]
                    self.assertTrue(attempt['tree_reaped'])
                    self.assertEqual(attempt['state'], 'result')
                    self.assertFalse(Path(f'/proc/{descendant}').exists())
                finally:
                    allow.touch()
                    if supervisor.poll() is None:
                        supervisor.wait(timeout=5)
                    os.close(write_fd)
                    if descendant is not None:
                        try:
                            os.kill(descendant, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestConcurrentStarters(unittest.TestCase):
    def test_two_simultaneous_runners_launch_exactly_one_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            marker, release = out / 'launches', out / 'release'
            worker = f'''
import os, time
from pathlib import Path
with Path({str(marker)!r}).open('a') as file:
    file.write(str(os.getpid()) + '\\n'); file.flush()
while not Path({str(release)!r}).exists():
    time.sleep(.01)
Path({str(out / 'result.json')!r}).write_text('{{"status":"error","error":"finished"}}')
'''
            owners = [launch_owner(out, worker), launch_owner(out, worker)]
            try:
                wait_for(marker.exists)
                wait_for(lambda: any(owner.poll() is not None for owner in owners))
                self.assertEqual(len(marker.read_text().splitlines()), 1)
                self.assertEqual(sum(owner.poll() is None for owner in owners), 1)
                release.touch()
                codes = sorted(owner.wait(timeout=5) for owner in owners)
                self.assertEqual(codes, [0, 1])
                self.assertEqual(len(json.loads((out / 'attempts.json').read_text())['attempts']), 1)
            finally:
                release.touch()
                for owner in owners:
                    if owner.poll() is None:
                        owner.wait(timeout=5)


class TestCleanupEvidenceFailure(unittest.TestCase):
    def test_unavailable_disk_and_stderr_cannot_release_cleanup_ownership(self):
        from types import SimpleNamespace
        r = recovery()
        journal = {'attempts': [{'state': 'running'}]}
        heartbeat = {'sequence': 0}
        broken_stderr = SimpleNamespace(write=lambda *_args: (_ for _ in ()).throw(OSError('closed stderr')))
        with patch.object(r, 'reap_tree', side_effect=[False, True]), patch.object(r, 'atomic_json', side_effect=OSError('disk unavailable')), patch.object(r.sys, 'stderr', broken_stderr), patch.object(r.time, 'sleep'):
            r.await_cleanup(None, {'out': '/unused', 'heartbeat_seconds': 20}, journal, heartbeat)
        self.assertEqual(journal['attempts'][0]['reason'], 'process_tree_cleanup_unproven')


@unittest.skipUnless(sys.platform == 'linux', 'Process ownership requires Linux subreaping')
class TestHighDescriptorOwnership(unittest.TestCase):
    def test_owner_pipe_above_select_limit_still_detects_disconnect(self):
        import fcntl
        from types import SimpleNamespace
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            read_fd, write_fd = os.pipe()
            high_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 4096)
            os.close(read_fd)
            os.close(write_fd)
            try:
                reason = r.monitor_worker(SimpleNamespace(pid=0, poll=lambda: None),
                    {'out': directory, 'owner_pipe': high_fd, 'timeout': 2, 'heartbeat_seconds': 20},
                    {'sequence': 0})
                self.assertEqual(reason, 'owner_disconnected')
            finally:
                os.close(high_fd)


class TestUnsupportedRecoveryPlatform(unittest.TestCase):
    def test_unsupported_platform_is_rejected_before_ownership_or_evidence_creation(self):
        r = recovery()
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            with patch.object(r.sys, 'platform', 'darwin'), self.assertRaisesRegex(r.RecoveryBlocked, 'Linux'), r.RunLock(out):
                self.fail('Unsupported supervisor platform acquired ownership')
            self.assertEqual(list(out.iterdir()), [])
