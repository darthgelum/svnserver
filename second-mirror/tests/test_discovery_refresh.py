"""Scheduling regressions; runs without Docker or production credentials."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import auto


class DiscoveryRefreshTests(unittest.TestCase):
    def test_partial_batch_is_not_published_until_caught_up(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            repo = {'name': 'Example'}
            path = data / 'repositories/Example'
            path.mkdir(parents=True)
            status = data / 'status/Example.json'
            lag = [2]

            def sync_one(repo, full_props=False):
                auto.atomic_json(status, {'ok': True, 'lag_revisions': lag[0]})
                return True

            app = SimpleNamespace(repo_path=lambda repo: path, prop=lambda *args: 'source',
                                  sync_one=sync_one, SECRETS={})
            with patch.object(auto, 'DATA', data), patch.object(auto, 'settings', return_value={}), \
                    patch.object(auto, 'repositories', return_value=[repo]), \
                    patch.object(auto, 'discovery_health', return_value=0):
                self.assertEqual(auto.sync_passes(app, 'sync-once'), 0)
                self.assertEqual(auto.published(), [])
                lag[0] = 0
                self.assertEqual(auto.sync_passes(app, 'sync-once'), 0)
                self.assertEqual(auto.published(), [repo])

    def test_refresh_continues_and_recovers_during_blocked_transfer(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            entered = threading.Event()
            recovered = threading.Event()
            calls = []

            def discover(app):
                calls.append(threading.current_thread().ident)
                if len(calls) == 2:
                    raise urllib.error.HTTPError('https://hidden-url', 503, 'unavailable', {}, None)
                auto.atomic_json(data / 'status/discovery.json',
                                 {'ok': True, 'checked_at': auto.time.time()})
                if entered.is_set():
                    recovered.set()
                return []

            def blocked_passes(app, command):
                entered.set()
                self.assertTrue(recovered.wait(5), 'Discovery stalled behind transfer')
                return 0

            with patch.object(auto, 'DATA', data), \
                    patch.object(auto, 'discover', side_effect=discover), \
                    patch.object(auto, 'sync_passes', side_effect=blocked_passes), \
                    patch.dict(auto.os.environ, {'DISCOVERY_INTERVAL_SECONDS': '1',
                                                 'REQUIRE_SEED_ADOPTION': '0'}):
                self.assertEqual(auto.sync(SimpleNamespace(event=lambda *args: None), 'sync-loop'), 0)
            self.assertGreaterEqual(len(calls), 3)
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(json.loads((data / 'status/discovery.json').read_text())['ok'])

    def test_http_failure_reports_code_without_url_or_response(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            output = io.StringIO()
            error = urllib.error.HTTPError('https://hidden-url', 401, 'secret-response', {}, None)
            with patch.object(auto, 'DATA', data), \
                    patch.object(auto, 'discover', side_effect=error), \
                    contextlib.redirect_stdout(output):
                self.assertFalse(auto.refresh_discovery(SimpleNamespace(event=lambda *args: None)))
            state = (data / 'status/discovery.json').read_text()
            self.assertIn('HTTP 401', output.getvalue())
            self.assertNotIn('hidden-url', output.getvalue() + state)
            self.assertNotIn('secret-response', output.getvalue() + state)

    def test_one_shot_does_not_start_background_thread(self):
        with patch.object(auto, 'refresh_discovery'), \
                patch.object(auto, 'sync_passes', return_value=0), \
                patch.object(auto.threading, 'Thread') as thread, \
                patch.object(auto.telemetry, 'heartbeat'), \
                patch.dict(auto.os.environ, {'REQUIRE_SEED_ADOPTION': '0'}):
            self.assertEqual(auto.sync(object(), 'sync-once'), 0)
            thread.assert_not_called()


if __name__ == '__main__':
    unittest.main()
