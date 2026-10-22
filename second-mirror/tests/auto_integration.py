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
        # An old master must not silently allow bulk file bodies over WAN.
        call('exec', master, 'bash', '-ec',
             "cp /opt/svnmirror/discovery.cgi /tmp/discovery.saved; sed -i '/lock_metadata_mode/d' /opt/svnmirror/discovery.cgi")
        try:
            incompatible = docker('run', '--rm', *sync_args, IMAGE, 'discover', ok=False)
            assert incompatible.returncode != 0 and 'Update and recreate master-http' in incompatible.stderr
        finally:
            call('exec', master, 'cp', '/tmp/discovery.saved', '/opt/svnmirror/discovery.cgi')
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
        # Auto-discovered repos use the same authoritative lock path. Lock state
        # must change immediately even while the replication worker is stopped.
        call('exec', master, 'bash', '-c', 'printf asset > /tmp/asset; svn import /tmp/asset file:///repositories/Second/trunk/asset -m asset')
        sync()
        svn(master, 'lock', 'http://master/svn/Second/trunk/asset')
        assert '<owner>alice</owner>' in svn(mirror, 'info', '--xml', 'http://127.0.0.1/svn/Second/trunk/asset').stdout
        svn(mirror, 'unlock', 'http://127.0.0.1/svn/Second/trunk/asset')
        assert '<lock>' not in svn(master, 'info', '--xml', 'http://master/svn/Second/trunk/asset').stdout
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
        # Reuse an existing copy IN PLACE, with separate metadata storage.
        for suffix in ('existing', 'seed-state'):
            name = PREFIX + '-' + suffix
            call('volume', 'create', name)
            volumes.append(name)
        existing, metadata = volumes[-2:]
        # Reproduce Synology bind-root 0700 and an old root-owned 0600 lock.
        call('run', '--rm', '--entrypoint', 'bash', '-v', metadata + ':/data', IMAGE, '-c',
             'mkdir -p /data/status; touch /data/status/worker.lock; '
             'chmod 700 /data; chmod 600 /data/status/worker.lock')
        call('run', '--rm', '--entrypoint', 'bash', '-v', volumes[0] + ':/master:ro',
             '-v', existing + ':/existing', IMAGE, '-c',
             'svnadmin hotcopy /master/Example /existing/Example; '
             'svnadmin hotcopy /master/Second /existing/Second; '
             'chown -R www-data:www-data /existing')
        adopt_args = ['--network', PREFIX, '-e', 'REQUIRE_SEED_ADOPTION=1',
                      '-v', metadata + ':/data', '-v', existing + ':/data/repositories',
                      '-v', str(root / 'mirror.json') + ':/config/repos.json:ro',
                      '-v', str(root / 'source.json') + ':/secrets/source.json:ro']
        assert docker('run', '--rm', *adopt_args, IMAGE, 'sync-once', ok=False).returncode != 0
        call('run', '--rm', *adopt_args, IMAGE, 'discover')
        # Advance the source AFTER the local copy was taken.
        svn(master, 'mkdir', 'http://master/svn/Example/new-after-copy', '-m', 'delta')
        before = call('run', '--rm', '--entrypoint', 'svnlook', '-v', existing + ':/existing',
                      IMAGE, 'youngest', '/existing/Example')
        # A failure in a later repository must preserve earlier verification,
        # without adopting/chowning/quarantining any repository prematurely.
        call('run', '--rm', '--entrypoint', 'python3', '-v', existing + ':/existing', IMAGE, '-c',
             'from pathlib import Path; import shutil; p=Path("/existing"); '
             'shutil.move(str(p/"Second/db/revs/0/1"), str(p/"held-second-revision")); '
             'shutil.copy2(p/"Example/db/revs/0/0", p/"held-example-revision")')
        failed = docker('run', '--rm', *adopt_args, IMAGE, 'adopt-all', ok=False)
        assert failed.returncode != 0 and 'Example: verifying local history' in failed.stdout
        assert 'Second: verifying local history' in failed.stdout
        call('run', '--rm', '--entrypoint', 'test', '-v', existing + ':/existing',
             IMAGE, '!', '-e', '/existing/Example/seed-hooks.saved')
        resumed = docker('run', '--rm', *adopt_args, IMAGE, 'adopt-all', ok=False)
        assert resumed.returncode != 0 and 'Example: reusing successful local verification' in resumed.stdout
        assert 'Example: verifying local history' not in resumed.stdout
        # Even a same-size content change with mtime restored invalidates the
        # checkpoint through ctime; never trust only UUID and youngest revision.
        call('run', '--rm', '--entrypoint', 'python3', '-v', existing + ':/existing', IMAGE, '-c',
             'from pathlib import Path; import os; p=Path("/existing/Example/db/revs/0/0"); '
             's=p.stat(); data=p.read_bytes(); p.write_bytes(b"X"+data[1:]); '
             'os.utime(p, ns=(s.st_atime_ns, s.st_mtime_ns))')
        changed = docker('run', '--rm', *adopt_args, IMAGE, 'adopt-all', ok=False)
        assert changed.returncode != 0 and 'Example: verifying local history' in changed.stdout
        assert 'Example: reusing' not in changed.stdout
        call('run', '--rm', '--entrypoint', 'python3', '-v', existing + ':/existing', IMAGE, '-c',
             'from pathlib import Path; import shutil; p=Path("/existing"); '
             'shutil.copy2(p/"held-example-revision", p/"Example/db/revs/0/0")')
        forced = docker('run', '--rm', *adopt_args, IMAGE, 'adopt-all', '--reverify', ok=False)
        assert forced.returncode != 0 and 'Example: verifying local history' in forced.stdout
        call('run', '--rm', '--entrypoint', 'python3', '-v', existing + ':/existing', IMAGE, '-c',
             'from pathlib import Path; import shutil; p=Path("/existing"); '
             'shutil.move(str(p/"held-second-revision"), str(p/"Second/db/revs/0/1"))')
        # A writer changing an EARLIER seed while a later one is verified must
        # abort the whole adoption before any hooks/ownership are changed.
        race_probe = '''import sys
sys.path.insert(0, '/opt/svnmirror')
import app, auto
app.prepare_worker(drop=False)
original = app.run
def mutate_earlier(args):
    result = original(args)
    if args == ['svnadmin', 'verify', '/data/repositories/Second']:
        (app.DATA / 'repositories/Example/changed-during-adopt').write_text('changed')
    return result
app.run = mutate_earlier
with app.worker_lock():
    auto.adopt_all(app, app.config(), reverify=True)
'''
        raced = docker('run', '--rm', *adopt_args, '--entrypoint', 'python3', IMAGE, '-c', race_probe, ok=False)
        assert raced.returncode != 0 and 'Example: seed changed before adoption' in raced.stderr
        call('run', '--rm', '--entrypoint', 'test', '-v', existing + ':/existing',
             IMAGE, '!', '-e', '/existing/Example/seed-hooks.saved')
        call('run', '--rm', '--entrypoint', 'python3', '-v', existing + ':/existing', IMAGE, '-c',
             'from pathlib import Path; Path("/existing/Example/changed-during-adopt").unlink()')
        adopted = call('run', '--rm', *adopt_args, IMAGE, 'adopt-all')
        assert 'Example: verifying local history' in adopted
        assert 'Second: reusing successful local verification' in adopted
        after = call('run', '--rm', '--entrypoint', 'svnlook', '-v', existing + ':/existing',
                     IMAGE, 'youngest', '/existing/Example')
        assert before == after == '2'  # Adoption never downloads repository history.
        call('run', '--rm', *adopt_args, IMAGE, 'sync-once')
        after = call('run', '--rm', '--entrypoint', 'svnlook', '-v', existing + ':/existing',
                     IMAGE, 'youngest', '/existing/Example')
        assert after == '3'  # Only the missing revision was copied.
        seed_mirror = PREFIX + '-seed-mirror'
        containers.append(seed_mirror)
        call('run', '-d', '--name', seed_mirror, '--network', PREFIX,
             '-v', metadata + ':/data', '-v', existing + ':/data/repositories',
             '-v', str(root / 'mirror.json') + ':/config/repos.json:ro', IMAGE)
        wait_ready(seed_mirror)
        svn(seed_mirror, 'mkdir', 'http://127.0.0.1/svn/Example/from-existing', '-m', 'write through reused copy')
        assert call('exec', master, 'svnlook', 'youngest', '/repositories/Example') == '4'
        assert call('exec', seed_mirror, 'svnlook', 'youngest', '/data/repositories/Example') == '3'
        print('PASS: automatic repositories/auth, existing-copy adoption, resume after failure, unchanged checkpoint reuse, same-size/mtime-restored corruption invalidation, forced reverify, no premature adoption, delta-only sync')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
