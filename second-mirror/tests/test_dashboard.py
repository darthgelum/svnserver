import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dashboard


class DashboardTests(unittest.TestCase):
    def test_snapshot_progress_groups_and_no_password_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, auth = root / 'status', root / 'auth'
            status.mkdir()
            auth.mkdir()
            (auth / 'htpasswd').write_text('alice:HASH_MUST_NOT_LEAK\nbob:ANOTHER_HASH\n')
            (auth / 'authz').write_text('[groups]\nartists = alice\nteam = @artists, bob\nloop = @loop\n[Example:/]\n@team = rw\n')
            def write(name, value):
                (status / name).write_text(json.dumps(value))
            now = time.time()
            write('catalog.json', [{'name': 'Example', 'path': '/svn/Example', 'master_url': 'https://master.example/svn/Example'}])
            write('published.json', [])
            write('master-heads.json', {'checked_at': now, 'revisions': {'Example': 100}})
            write('Example.json', {'ok': False, 'error': 'Old failure', 'checked_at': now - 600})
            write('Example-progress.json', {'state': 'running', 'checked_at': now, 'local_revision': 80,
                                            'last_merged_revision': '79', 'copying_revision': '80'})
            write('discovery.json', {'ok': True, 'checked_at': now, 'last_success_at': now})
            doc = dashboard.snapshot(root, auth, {}, 'admin')
            self.assertNotIn('HASH_MUST_NOT_LEAK', json.dumps(doc))
            self.assertNotIn('ANOTHER_HASH', json.dumps(doc))
            self.assertEqual(doc['repositories'][0]['state'], 'syncing')
            self.assertEqual(doc['repositories'][0]['lag'], 21)
            self.assertFalse(doc['repositories'][0]['published'])
            self.assertEqual(doc['users'][0]['groups'], ['artists', 'team'])
            self.assertFalse(doc['connection']['stale'])

    def test_endpoint_denies_ordinary_users_and_all_writes(self):
        with patch.dict(os.environ, {'SVNMIRROR_STATUS_USERS': 'admin svnmirror',
                                      'SVNMIRROR_AUTH_ROOT': '/not-read', 'REMOTE_USER': 'ordinary',
                                      'REQUEST_METHOD': 'GET'}), patch.object(dashboard, 'snapshot') as snapshot:
            self.assertEqual(dashboard.respond()[0], '403 Forbidden')
            snapshot.assert_not_called()
            os.environ['REMOTE_USER'] = 'admin'
            os.environ['REQUEST_METHOD'] = 'POST'
            self.assertEqual(dashboard.respond()[0], '405 Method Not Allowed')
            snapshot.assert_not_called()
            os.environ['REQUEST_METHOD'] = 'GET'
            snapshot.return_value = {'safe': True}
            self.assertEqual(dashboard.respond(), ('200 OK', {'safe': True}))

    def test_error_details_never_expose_auth_file_contents(self):
        with patch.dict(os.environ, {'SVNMIRROR_STATUS_USERS': 'admin', 'SVNMIRROR_AUTH_ROOT': '/not-read',
                                      'REMOTE_USER': 'admin', 'REQUEST_METHOD': 'GET'}), \
                patch.object(dashboard, 'snapshot', side_effect=ValueError('SECRET_HASH')):
            status, body = dashboard.respond()
            self.assertEqual(status, '503 Service Unavailable')
            self.assertNotIn('SECRET_HASH', json.dumps(body))


if __name__ == '__main__':
    unittest.main()
