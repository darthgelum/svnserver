"""Real local SVN + POSIX process/lock tests; no Docker or office endpoints.

Run on Linux with svn, svnadmin, svnlook and svnsync in PATH.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import worker


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='svn-worker-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        (self.data / 'status').mkdir(parents=True)
        (self.data / 'repositories').mkdir()
        guard = self.root / 'write-guard'
        guard.write_text(app.GUARD_FILE.read_text().replace('10001', str(os.geteuid())))
        self.patches = [patch.object(app, 'DATA', self.data), patch.object(app, 'GUARD_FILE', guard),
                        patch.object(app, 'SECRETS', {'default': {'username': 'alice', 'password': 'fixture-secret'}}),
                        patch.dict(os.environ, {'SYNC_PROGRESS_SECONDS': '1', 'SYNC_IDLE_TIMEOUT_SECONDS': '10',
                                                'SYNC_TRANSFER_TIMEOUT_SECONDS': '0'})]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.source = self.root / 'source'
        app.run(['svnadmin', 'create', str(self.source)])
        app.run(['svn', 'mkdir', self.source.as_uri() + '/trunk', '-m', 'initial', '--username', 'alice'])
        self.repo = {'name': 'Example', 'master_url': self.source.as_uri()}
        with app.worker_lock():
            app.initialize(self.repo)
        self.url = app.local_url(self.repo)

    def setprop(self, name, value):
        app.run(['svn', 'propset', '--revprop', '-r', '0', name, value, self.url])

    def test_stale_lock_and_interrupted_revision_resume_without_redownloading(self):
        with app.worker_lock():
            self.assertTrue(app.sync_one(self.repo))
            self.assertEqual(app.run(['svnlook', 'youngest', str(app.repo_path(self.repo))]), '1')
            app.run(['svn', 'mkdir', self.source.as_uri() + '/next', '-m', 'next', '--username', 'alice'])
            self.setprop('svn:sync-lock', 'dead-container:old-token')
            self.setprop('svn:sync-currently-copying', '2')
            with app.repository_lock(self.repo):
                app.recover_replication_lock(self.repo)
                self.assertIsNone(app.prop(self.repo, 'svn:sync-lock'))
                self.assertEqual(app.prop(self.repo, 'svn:sync-currently-copying'), '2')
            self.setprop('svn:sync-lock', 'another-dead-token')
            self.assertTrue(app.sync_one(self.repo))
        self.assertEqual(app.run(['svnlook', 'youngest', str(app.repo_path(self.repo))]), '2')
        self.assertIsNone(app.prop(self.repo, 'svn:sync-lock'))
        state = json.loads((self.data / 'status/Example.json').read_text())
        self.assertEqual(state['lag_revisions'], 0)
        self.assertTrue((self.data / 'status/Example-last-recovery.json').is_file())

    def test_recovery_refuses_without_os_lock_or_with_changed_guard(self):
        self.setprop('svn:sync-lock', 'retain-me')
        with self.assertRaisesRegex(RuntimeError, 'Exclusive'):
            app.recover_replication_lock(self.repo)
        (app.repo_path(self.repo) / 'hooks/pre-lock').write_text('# altered guard\n')
        with app.worker_lock():
            self.assertFalse(app.sync_one(self.repo))
        self.assertEqual(app.prop(self.repo, 'svn:sync-lock'), 'retain-me')

    def test_inherited_file_lock_is_removed_but_master_lock_survives(self):
        self.check_inherited_file_lock('asset')

    def test_inherited_file_lock_with_encoded_asset_name(self):
        self.check_inherited_file_lock('asset space #?@é.uasset')

    def check_inherited_file_lock(self, filename):
        asset = self.root / 'asset'
        asset.write_text('initial')
        source_url = self.source.as_uri() + '/trunk/' + quote(filename, safe='') + '@'
        mirror_url = self.url + '/trunk/' + quote(filename, safe='') + '@'
        app.run(['svn', 'import', str(asset), source_url[:-1], '-m', 'asset', '--username', 'alice'])
        with app.worker_lock():
            self.assertTrue(app.sync_one(self.repo))
        app.run(['svn', 'lock', mirror_url, '--username', 'bob'])
        self.assertIn('Owner: bob', app.run(['svnadmin', 'lslocks', str(app.repo_path(self.repo))]))
        wc = self.root / 'wc'
        app.run(['svn', 'checkout', self.source.as_uri() + '/trunk', str(wc)])
        app.run(['svn', 'lock', str(wc / filename) + '@', '--username', 'alice'])
        (wc / filename).write_text('updated')
        app.run(['svn', 'commit', str(wc), '-m', 'locked change', '--no-unlock', '--username', 'alice'])
        before = app.run(['svn', 'info', '--xml', source_url])
        with app.worker_lock():
            self.assertTrue(app.sync_one(self.repo))
        after = app.run(['svn', 'info', '--xml', source_url])
        before_lock = {node.tag: node.text for node in ET.fromstring(before).find('entry/lock')}
        after_lock = {node.tag: node.text for node in ET.fromstring(after).find('entry/lock')}
        self.assertEqual(before_lock['owner'], 'alice')
        self.assertEqual(before_lock, after_lock)
        self.assertEqual(app.run(['svnadmin', 'lslocks', str(app.repo_path(self.repo))]), '')
        self.assertEqual(app.run(['svn', 'cat', mirror_url]), 'updated')

    def test_active_repository_writer_prevents_lock_removal(self):
        self.setprop('svn:sync-lock', 'active-token')
        code = ('import fcntl,sys,time; f=open(sys.argv[1],"a"); '
                'fcntl.flock(f,fcntl.LOCK_EX); print("ready",flush=True); time.sleep(60)')
        child = subprocess.Popen([sys.executable, '-c', code, str(app.repository_lock_path(self.repo))],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'ready')
            with app.worker_lock():
                self.assertFalse(app.sync_one(self.repo))
            self.assertEqual(app.prop(self.repo, 'svn:sync-lock'), 'active-token')
        finally:
            child.terminate()
            child.wait()
            child.stdout.close()

    def test_child_retains_both_os_locks_after_parent_is_killed(self):
        global_path = self.data / 'status/worker.lock'
        repo_path = app.repository_lock_path(self.repo)
        code = '''import sys, subprocess, time
sys.path.insert(0, sys.argv[1])
import worker
with worker.exclusive(sys.argv[2], 'busy'):
    with worker.exclusive(sys.argv[3], 'busy'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                 pass_fds=worker.descriptors(), stdout=subprocess.DEVNULL)
        print(child.pid, flush=True)
        time.sleep(60)
'''
        parent = subprocess.Popen([sys.executable, '-c', code, str(Path(worker.__file__).parent),
                                   str(global_path), str(repo_path)], stdout=subprocess.PIPE, text=True)
        orphan = None
        try:
            orphan = int(parent.stdout.readline().strip())
            parent.kill()
            parent.wait()
            for path in (global_path, repo_path):
                with self.assertRaisesRegex(RuntimeError, 'busy'):
                    with worker.exclusive(path, 'busy'):
                        pass
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if orphan:
                os.kill(orphan, signal.SIGTERM)
            parent.stdout.close()

    def test_active_transfer_outlives_old_total_timeout_and_redacts_logs(self):
        output = []
        reports = []
        with patch.dict(os.environ, {'SYNC_TIMEOUT_SECONDS': '1', 'SYNC_IDLE_TIMEOUT_SECONDS': '2'}):
            worker.transfer([sys.executable, '-u', '-c',
                             'import time; [(print("fixture-secret progress",flush=True),time.sleep(.4)) for _ in range(8)]'],
                            app.repo_path(self.repo), lambda *args: reports.append(args), output.append, app.redact)
        self.assertGreaterEqual(len(reports), 2)
        self.assertTrue(output)
        self.assertNotIn('fixture-secret', '\n'.join(output))

    def test_idle_transfer_is_stopped_and_os_lock_can_be_reused(self):
        pids = []
        with patch.dict(os.environ, {'SYNC_IDLE_TIMEOUT_SECONDS': '2'}):
            with app.worker_lock():
                with self.assertRaisesRegex(RuntimeError, 'no activity'):
                    worker.transfer([sys.executable, '-c', 'import time; time.sleep(60)'],
                                    app.repo_path(self.repo), lambda pid, *args: pids.append(pid), print, app.redact)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)
        with app.worker_lock():
            pass

    def test_cpu_spin_without_io_does_not_reset_idle_watchdog(self):
        pids = []
        with patch.dict(os.environ, {'SYNC_IDLE_TIMEOUT_SECONDS': '2'}):
            with self.assertRaisesRegex(RuntimeError, 'no activity'):
                worker.transfer([sys.executable, '-c', 'while True: pass'],
                                app.repo_path(self.repo), lambda pid, *args: pids.append(pid), print, app.redact)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_transaction_progress_excludes_abandoned_replays(self):
        repo = app.repo_path(self.repo)
        protorevs = repo / 'db/txn-protorevs'
        transactions = repo / 'db/transactions'
        for name in ('old', 'active'):
            (transactions / (name + '.txn')).mkdir()
            (transactions / (name + '.txn') / 'changes').write_bytes(b'123')
            (protorevs / (name + '.rev')).write_bytes(b'45678')
        with (protorevs / 'active.rev').open('rb'):
            evidence = worker.activity(type('Process', (), {'pid': os.getpid()})(), repo)
        self.assertEqual(sum(item[1] for item in evidence[2]), 8)
        self.assertEqual(len(evidence[2]), 2)

    def test_stop_during_transfer_terminates_writer_before_unlock(self):
        pids = []
        def stopped(pid, *args):
            pids.append(pid)
            raise worker.Stopped()
        with app.worker_lock():
            with self.assertRaises(worker.Stopped):
                worker.transfer([sys.executable, '-u', '-c', 'import time; print("ready",flush=True); time.sleep(60)'],
                                app.repo_path(self.repo), stopped, print, app.redact)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)
        with app.worker_lock():
            pass

    def test_apache_cleans_only_stale_runtime_artifacts(self):
        runtime, proc = self.root / 'apache', self.root / 'proc'
        runtime.mkdir()
        proc.mkdir()
        (runtime / 'apache2.pid').write_text('123')
        (runtime / 'keep-file').write_text('keep')
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(runtime / 'cgisock.123'))
        worker.clean_apache_runtime(runtime, proc)
        self.assertFalse((runtime / 'apache2.pid').exists())
        self.assertFalse((runtime / 'cgisock.123').exists())
        self.assertTrue((runtime / 'keep-file').exists())
        (proc / '123').mkdir()
        (proc / '123/comm').write_text('apache2\n')
        (runtime / 'apache2.pid').write_text('123')
        with self.assertRaisesRegex(RuntimeError, 'already running'):
            worker.clean_apache_runtime(runtime, proc)
        self.assertTrue((runtime / 'apache2.pid').exists())


if __name__ == '__main__':
    unittest.main()
