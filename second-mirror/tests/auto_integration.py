"""Automatic whole-server mirrors. Disposable Docker fixtures, no real NAS."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

IMAGE = 'svn-second-mirror:local'
PREFIX = 'svnauto-test-' + uuid.uuid4().hex[:10]
containers, volumes = [], []


def docker(*args, ok=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True)
    if ok and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def call(*args):
    return docker(*args).stdout.strip()


def wait_ready(container):
    for _ in range(50):
        result = docker('exec', container, 'python3', '-c',
                        'import urllib.request; urllib.request.urlopen("http://127.0.0.1/healthz")', ok=False)
        if result.returncode == 0:
            return
        time.sleep(.2)
    log = docker('logs', container)
    raise RuntimeError(log.stdout + log.stderr + result.stderr)


try:
    call('network', 'create', PREFIX)
    for suffix in ('master', 'mirror'):
        name = PREFIX + '-' + suffix
        call('volume', 'create', name)
        volumes.append(name)
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        root = Path(directory)
        def write(name, value):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value.encode())
        write('master.json', json.dumps({'auto_discover': True, 'public_prefix': '/svn',
                                         'discovery_user': 'svnmirror', 'export_auth': True}))
        write('mirror.json', json.dumps({'auto_discover': True, 'master_base_url': 'http://master',
                                         'sync_auth': True, 'allow_http_discovery': True}))
        write('source.json', json.dumps({'default': {'username': 'svnmirror', 'password': 'service-pass'}}))
        write('auth/authz', '[Example:/]\nalice = rw\nbob = r\n* =\n[Second:/]\nalice = rw\n* =\n')
        fixture = '''set -eu
svnadmin create /repositories/Example
svn mkdir file:///repositories/Example/trunk -m initial
svnadmin create /repositories/Second
svn mkdir file:///repositories/Second/trunk -m initial
chown -R www-data:www-data /repositories
htpasswd -bc /fixture/auth/htpasswd alice test-pass
htpasswd -b /fixture/auth/htpasswd bob test-pass
htpasswd -b /fixture/auth/htpasswd svnmirror service-pass
'''
        call('run', '--rm', '--entrypoint', 'bash', '-v', volumes[0] + ':/repositories',
             '-v', str(root) + ':/fixture', IMAGE, '-c', fixture)
        master, mirror = PREFIX + '-master', PREFIX + '-mirror'
        containers.append(master)
        call('run', '-d', '--name', master, '--network', PREFIX, '--network-alias', 'master',
             '-e', 'ROLE=master', '-v', volumes[0] + ':/repositories',
             '-v', str(root / 'auth') + ':/auth:ro',
             '-v', str(root / 'master.json') + ':/config/repos.json:ro', IMAGE)
        wait_ready(master)
        def svn(container, *args, user='alice', password='test-pass', ok=True):
            return docker('exec', container, 'svn', *args, '--non-interactive', '--no-auth-cache',
                          '--username', user, '--password', password, ok=ok)
        # No authz permissions for the service account: the separate transport
        # must supply complete history while rejecting writes.
        assert svn(master, 'ls', 'http://master/svn/Example', user='svnmirror', password='service-pass', ok=False).returncode != 0
        assert svn(master, 'ls', 'http://master/svn-mirror-source/Example', user='svnmirror', password='service-pass').returncode == 0
        assert svn(master, 'mkdir', 'http://master/svn-mirror-source/Example/forbidden', '-m', 'no', user='svnmirror', password='service-pass', ok=False).returncode != 0
        # Ordinary users must NEVER receive auth hashes or the repository catalog.
        probe = '''import urllib.request, urllib.error, base64
r=urllib.request.Request('http://127.0.0.1/svn-mirror-meta',headers={'Authorization':'Basic '+base64.b64encode(b'alice:test-pass').decode()})
try: urllib.request.urlopen(r)
except urllib.error.HTTPError as e: assert e.code in (401, 403)
else: raise AssertionError('Catalog leaked')
'''
        call('exec', master, 'python3', '-c', probe)
        # Start the mirror BEFORE the worker. No auth files or repos exist yet.
        containers.append(mirror)
        call('run', '-d', '--name', mirror, '--network', PREFIX,
             '-v', volumes[1] + ':/data', '-v', str(root / 'mirror.json') + ':/config/repos.json:ro', IMAGE)
        wait_ready(mirror)
        assert docker('exec', mirror, 'python3', '/opt/svnmirror/app.py', 'health', ok=False).returncode != 0
        sync_args = ['--network', PREFIX, '-v', volumes[1] + ':/data',
                     '-v', str(root / 'mirror.json') + ':/config/repos.json:ro',
                     '-v', str(root / 'source.json') + ':/secrets/source.json:ro']
        def sync():
            return call('run', '--rm', *sync_args, IMAGE, 'sync-once')
        def wait_repo(name, success=True):
            for _ in range(30):
                result = svn(mirror, 'ls', 'http://127.0.0.1/svn/' + name, ok=False)
                if (result.returncode == 0) == success:
                    return
                time.sleep(.3)
            raise RuntimeError(result.stdout + result.stderr)
        sync()
        wait_repo('Example')
        wait_repo('Second')
        call('exec', mirror, 'python3', '/opt/svnmirror/app.py', 'health')
        # Normal user writes still go to the master, never the mirror filesystem.
        svn(mirror, 'mkdir', 'http://127.0.0.1/svn/Example/branches', '-m', 'proxy')
        assert call('exec', master, 'svnlook', 'youngest', '/repositories/Example') == '2'
        assert call('exec', mirror, 'svnlook', 'youngest', '/data/repositories/Example') == '1'
        sync()
        assert svn(mirror, 'mkdir', 'http://127.0.0.1/svn/Example/no', '-m', 'no', user='bob', ok=False).returncode != 0
        # Create a repository AFTER both servers started. No master/mirror restart.
        call('exec', master, 'bash', '-c',
             'svnadmin create /repositories/Third; svn mkdir file:///repositories/Third/trunk -m initial; chown -R www-data:www-data /repositories/Third')
        with (root / 'auth/authz').open('ab') as file:
            file.write(b'[Third:/]\nalice = rw\n* =\n')
        assert svn(master, 'ls', 'http://master/svn/Third').returncode == 0
        sync()
        wait_repo('Third')
        # Revocation of a normal user's rights is delivered automatically.
        write('auth/authz', '[Example:/]\nalice = rw\n* =\n[Second:/]\nalice = rw\n* =\n[Third:/]\nalice = rw\n* =\n')
        sync()
        assert svn(mirror, 'ls', 'http://127.0.0.1/svn/Example', user='bob', ok=False).returncode != 0
        # A missing source must disappear from the public mirror catalog, while
        # preserving the local data for diagnosis; no automatic deletion.
        call('exec', master, 'mv', '/repositories/Third', '/repositories/Third.removed')
        sync()
        wait_repo('Third', success=False)
        call('exec', mirror, 'test', '-f', '/data/repositories/Third/format')
        print('PASS: protected discovery, all-repository read-only replication account, empty bootstrap, automatic auth, dynamic repositories, proxied commit, authz revocation, removal preserves data')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
