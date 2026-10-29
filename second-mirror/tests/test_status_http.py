"""Real Apache/CGI access checks using a private, unprivileged fixture server.

Set APACHE_BINARY, APACHE_MODULE_DIR and HTPASSWD_BINARY when not in PATH.
"""
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class StatusHTTPTests(unittest.TestCase):
    def test_image_permissions_normalize_owner_only_source_modes(self):
        source = Path(app.__file__).parent
        with tempfile.TemporaryDirectory(prefix='svn-image-modes-') as directory:
            code = Path(directory) / 'code'
            code.mkdir(mode=0o700)
            names = ('app.py', 'auto.py', 'worker.py', 'telemetry.py', 'dashboard.py',
                     'write-guard', 'discovery.cgi', 'status.cgi', 'log-errors',
                     'web/index.html', 'web/app.js', 'web/style.css', 'web/demo.js')
            for name in names:
                path = code / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes((source / name).read_bytes())
                path.chmod(0o600)
            # Exercise the actual final Dockerfile RUN instruction rather than
            # reproducing chmod logic in a test. Docker itself runs it as root.
            command = (source / 'Dockerfile').read_text().rsplit('\nRUN ', 1)[1].split('\nEXPOSE', 1)[0]
            command = command.replace('\\\n', '').replace('/opt/svnmirror', shlex.quote(str(code)))
            subprocess.run(['sh', '-eu', '-c', command], check=True)
            for path in [code, *(code.rglob('*'))]:
                mode = path.stat().st_mode & 0o777
                if path.is_dir() or path.name in ('write-guard', 'discovery.cgi', 'status.cgi', 'log-errors'):
                    self.assertEqual(mode, 0o755, str(path))
                else:
                    self.assertEqual(mode, 0o644, str(path))

    def test_real_apache_auth_cgi_and_bounded_error_log(self):
        apache = os.getenv('APACHE_BINARY') or shutil.which('apache2')
        if not apache:
            self.skipTest('Apache binary not available')
        modules = Path(os.getenv('APACHE_MODULE_DIR', '/usr/lib/apache2/modules'))
        htpasswd = os.getenv('HTPASSWD_BINARY') or shutil.which('htpasswd')
        with tempfile.TemporaryDirectory(prefix='svn-status-http-') as directory:
            root = Path(directory)
            data, auth, code = root / 'data', root / 'auth', root / 'code'
            (data / 'status').mkdir(parents=True)
            auth.mkdir()
            code.mkdir()
            source = Path(app.__file__).parent
            for name in ('dashboard.py', 'telemetry.py'):
                shutil.copyfile(source / name, code / name)
            shutil.copytree(source / 'web', code / 'web')
            subprocess.run([htpasswd, '-bc', str(auth / 'htpasswd'), 'admin', 'test-pass'], check=True, capture_output=True)
            subprocess.run([htpasswd, '-b', str(auth / 'htpasswd'), 'ordinary', 'test-pass'], check=True, capture_output=True)
            (auth / 'authz').write_text('[Example:/]\nadmin = rw\n')
            config = {'auto_discover': True, 'sync_auth': False}
            actual_path = Path
            def path(value):
                mapping = {'/var/www/html/healthz': root / 'healthz',
                           '/etc/apache2/sites-enabled/svnmirror.conf': root / 'site.conf'}
                if str(value).startswith('/auth/'):
                    return auth / actual_path(value).name
                return mapping.get(str(value), actual_path(value))
            with patch.object(app, 'DATA', data), patch.object(app, 'Path', side_effect=path), \
                    patch.object(app.auto, 'settings', return_value=config), \
                    patch.object(app, 'run', return_value=''), \
                    patch.dict(os.environ, {'ROLE': 'mirror', 'STATUS_USERS': 'admin'}):
                app.render([])
            # Only remap fixture filesystem paths. CGI authorization and snapshot
            # code remain real; Apache supplies REMOTE_USER and the allowlist.
            cgi = (source / 'status.cgi').read_text().replace(
                'status, document = dashboard.respond()',
                'original = dashboard.snapshot\n'
                'dashboard.snapshot = lambda data, auth, config, viewer: original('
                f'dashboard.Path({str(data)!r}), dashboard.Path({str(auth)!r}), config, viewer)\n'
                'status, document = dashboard.respond()')
            (code / 'status.cgi').write_text(cgi)
            (code / 'status.cgi').chmod(0o755)
            (code / 'log-errors').write_text((source / 'log-errors').read_text().replace('/data/status', str(data / 'status')))
            (code / 'log-errors').chmod(0o755)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            names = ('mpm_event', 'authn_core', 'authn_file', 'authz_core', 'authz_user', 'authz_host',
                     'auth_basic', 'alias', 'dir', 'mime', 'env', 'headers', 'setenvif', 'dav',
                     'dav_svn', 'authz_svn', 'proxy', 'proxy_http', 'ssl', 'cgid', 'rewrite')
            loads = '\n'.join(f'LoadModule {name}_module {modules / ("mod_" + name + ".so")}' for name in names)
            (root / 'mime.types').write_text('text/html html\ntext/css css\napplication/javascript js\n')
            global_config = (f'ServerRoot "{root}"\nServerName localhost\nListen 127.0.0.1:{port}\n'
                             f'PidFile "{root}/httpd.pid"\nDefaultRuntimeDir "{root}"\n'
                             f'ScriptSock "{root}/cgi"\nUser #{os.geteuid()}\nGroup #{os.getegid()}\n'
                             f'TypesConfig "{root}/mime.types"\n{loads}\n'
                             '<Directory />\nRequire all denied\n</Directory>\n')
            site = (root / 'site.conf').read_text().replace('/opt/svnmirror', str(code)).replace('/auth/', str(auth) + '/').replace('/var/www/html/healthz', str(root / 'healthz')).replace('*:80>', f'*:{port}>')
            (root / 'httpd.conf').write_text(global_config + site)
            check = subprocess.run([apache, '-t', '-f', str(root / 'httpd.conf')], text=True, capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)
            process = subprocess.Popen([apache, '-X', '-f', str(root / 'httpd.conf')], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                def request(path, login=None, method='GET'):
                    headers = {}
                    if login:
                        headers['Authorization'] = 'Basic ' + base64.b64encode((login + ':test-pass').encode()).decode()
                    req = urllib.request.Request(f'http://127.0.0.1:{port}' + path, headers=headers, method=method)
                    try:
                        with urllib.request.urlopen(req, timeout=5) as response:
                            return response.status, response.read()
                    except urllib.error.HTTPError as error:
                        return error.code, error.read()
                for _ in range(40):
                    try:
                        if request('/healthz')[0] == 200:
                            break
                    except (OSError, urllib.error.URLError):
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('Apache did not start')
                self.assertEqual(request('/svn-mirror-status/')[0], 401)
                self.assertEqual(request('/svn-mirror-status/api', 'ordinary')[0], 403)
                self.assertEqual(request('/svn-mirror-status/', 'admin')[0], 200)
                status, body = request('/svn-mirror-status/api', 'admin')
                self.assertEqual(status, 200, body)
                doc = json.loads(body)
                self.assertEqual(doc['viewer'], 'admin')
                self.assertNotIn('$apr1$', body.decode())
                self.assertNotIn('test-pass', body.decode())
                self.assertGreaterEqual(request('/svn-mirror-status/api', 'admin', 'POST')[0], 400)
                self.assertEqual(request('/svn-mirror-status/app.js', 'admin')[0], 200)
                self.assertTrue((data / 'status/apache-errors.log').exists())
            finally:
                process.terminate()
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
