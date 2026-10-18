"""Run with Python 3 + Docker Desktop/Engine. Uses disposable containers/volumes only."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

IMAGE = 'svn-second-mirror:local'
PREFIX = 'svnmirror-test-' + uuid.uuid4().hex[:10]
containers, volumes = [], []


def docker(*args, ok=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True)
    if ok and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def call(*args):
    return docker(*args).stdout.strip()


def volume(suffix):
    name = PREFIX + '-' + suffix
    call('volume', 'create', name)
    volumes.append(name)
    return name


def start(name, *args):
    containers.append(name)
    return call('run', '-d', '--name', name, '--network', PREFIX, *args)


def wait_http(container, port=80):
    for _ in range(50):
        result = docker('exec', container, 'python3', '-c',
                        f'import urllib.request; urllib.request.urlopen("http://127.0.0.1:{port}/healthz")', ok=False)
        if result.returncode == 0:
            return
        time.sleep(.2)
    log = docker('logs', container)
    raise RuntimeError(log.stdout + log.stderr + '\n' + result.stderr + '\n' +
                       call('inspect', '--format', '{{json .State}}', container))


try:
    call('network', 'create', PREFIX)
    master, mirror, seed_data = volume('master'), volume('mirror'), volume('seed')
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        root = Path(directory)
        (root / 'master.json').write_text(json.dumps({'repositories': [{'name': 'Example', 'path': '/svn/Example'}]}))
        # Docker's network alias, never a real office endpoint.
        (root / 'repos.json').write_text(json.dumps({'repositories': [{
            'name': 'Example', 'path': '/svn/Example', 'master_url': 'http://master/svn/Example',
            'master_version': '1.14.1'}]}))
        (root / 'source.json').write_text(json.dumps({'Example': {'username': 'alice', 'password': 'test-pass'}}))
        (root / 'auth').mkdir()
        (root / 'auth' / 'authz').write_text('[Example:/]\nalice = rw\nbob = r\n* =\n')
        fixture = '''set -eu
svnadmin create /repositories/Example
svn mkdir file:///repositories/Example/trunk -m initial
printf 'binary-fixture' > /tmp/asset.uasset
svn import /tmp/asset.uasset file:///repositories/Example/trunk/asset.uasset -m asset
chown -R www-data:www-data /repositories/Example
htpasswd -bc /fixture/auth/htpasswd alice test-pass
htpasswd -b /fixture/auth/htpasswd bob test-pass
'''
        call('run', '--rm', '--entrypoint', '/bin/bash', '-v', master + ':/repositories',
             '-v', str(root) + ':/fixture', IMAGE, '-c', fixture)
        master_name, mirror_name = PREFIX + '-master', PREFIX + '-mirror'
        start(master_name, '--network-alias', 'master', '-e', 'ROLE=master',
              '-v', master + ':/repositories', '-v', str(root / 'auth') + ':/auth:ro',
              '-v', str(root / 'master.json') + ':/config/repos.json:ro', IMAGE)
        wait_http(master_name)
        sync_args = ['--network', PREFIX, '-v', mirror + ':/data',
                     '-v', str(root / 'repos.json') + ':/config/repos.json:ro',
                     '-v', str(root / 'source.json') + ':/secrets/source.json:ro']

        def sync(command):
            return call('run', '--rm', *sync_args, IMAGE, command)

        sync('init')
        sync('sync-once')
        sync('init')  # Restarting initialization is idempotent.
        start(mirror_name, '-v', mirror + ':/data', '-v', str(root / 'auth') + ':/auth:ro',
              '-v', str(root / 'repos.json') + ':/config/repos.json:ro', IMAGE)
        wait_http(mirror_name)

        def svn(*args, user='alice', ok=True):
            return docker('exec', mirror_name, 'svn', *args, '--non-interactive',
                          '--no-auth-cache', '--username', user, '--password', 'test-pass', ok=ok)

        local = 'http://127.0.0.1/svn/Example'
        live = 'http://127.0.0.1:81/svn/Example'
        assert svn('cat', local + '/trunk/asset.uasset').stdout == 'binary-fixture'
        master_uuid = call('exec', master_name, 'svnlook', 'uuid', '/repositories/Example')
        assert master_uuid == call('exec', mirror_name, 'svnlook', 'uuid', '/data/repositories/Example')
        # A commit through the mirror must create exactly one MASTER revision.
        svn('mkdir', local + '/branches', '-m', 'write-through')
        assert call('exec', master_name, 'svnlook', 'youngest', '/repositories/Example') == '3'
        assert call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example') == '2'
        sync('sync-once')
        assert call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example') == '3'
        # Server-side COPY/MOVE must rewrite the proxy URL correctly.
        svn('copy', local + '/trunk', local + '/branches/test', '-m', 'copy through proxy')
        sync('sync-once')
        svn('move', local + '/branches/test', local + '/branches/moved', '-m', 'move through proxy')
        sync('sync-once')
        assert svn('mkdir', local + '/forbidden', '-m', 'no', user='bob', ok=False).returncode != 0
        # An attempted file:// write as Apache's user is rejected by the mirror hook.
        blocked = docker('exec', '--user', 'www-data', mirror_name, 'svn', 'mkdir',
                         'file:///data/repositories/Example/illegal', '-m', 'blocked', ok=False)
        assert blocked.returncode != 0 and 'mirror' in blocked.stderr
        # The live port forwards locks AND lock reads, without pretending to copy locks.
        svn('lock', live + '/trunk/asset.uasset', '-m', 'asset lock')
        assert 'Lock Token' in svn('info', live + '/trunk/asset.uasset').stdout
        assert svn('lock', live + '/trunk/asset.uasset', user='bob', ok=False).returncode != 0
        svn('unlock', live + '/trunk/asset.uasset')
        assert 'Lock Token' not in svn('info', live + '/trunk/asset.uasset').stdout
        # Old log edits are reconciled, not merely new revision properties.
        call('exec', master_name, 'bash', '-c',
             'printf "#!/bin/sh\\nexit 0\\n" > /repositories/Example/hooks/pre-revprop-change; '
             'chmod 755 /repositories/Example/hooks/pre-revprop-change')
        svn('propset', '--revprop', '-r', '1', 'svn:log', 'edited log', 'http://master/svn/Example')
        sync('copy-revprops')
        assert 'edited log' in svn('propget', '--revprop', '-r', '1', 'svn:log', local).stdout
        # Cached revision data still works offline; no independent master appears.
        call('stop', master_name)
        assert svn('cat', local + '/trunk/asset.uasset').stdout == 'binary-fixture'
        assert svn('mkdir', local + '/offline', '-m', 'must fail', ok=False).returncode != 0
        failed = docker('run', '--rm', *sync_args, IMAGE, 'sync-once', ok=False)
        assert failed.returncode != 0
        assert docker('exec', mirror_name, 'python3', '/opt/svnmirror/app.py', 'health', ok=False).returncode != 0
        call('start', master_name)
        wait_http(master_name)
        sync('sync-once')
        call('exec', mirror_name, 'python3', '/opt/svnmirror/app.py', 'health')
        # A large-history bootstrap must quarantine copied master hooks.
        call('exec', master_name, 'bash', '-c',
             'printf "#!/bin/sh\\nexit 1\\n" > /repositories/Example/hooks/post-commit; '
             'chmod 755 /repositories/Example/hooks/post-commit')
        call('run', '--rm', '--entrypoint', '/bin/bash', '-v', master + ':/master',
             '-v', seed_data + ':/data', IMAGE, '-c',
             'mkdir -p /data/repositories; svnadmin hotcopy /master/Example /data/repositories/Example; '
             'chown -R 10001:10001 /data')
        seed_args = ['--network', PREFIX, '-v', seed_data + ':/data',
                     '-v', str(root / 'repos.json') + ':/config/repos.json:ro',
                     '-v', str(root / 'source.json') + ':/secrets/source.json:ro']
        # A normal init must refuse this nonempty seed; adoption is explicit.
        assert docker('run', '--rm', *seed_args, IMAGE, 'init', ok=False).returncode != 0
        call('run', '--rm', *seed_args, IMAGE, 'seed', 'Example')
        call('run', '--rm', *seed_args, IMAGE, 'sync-once')
        saved = call('run', '--rm', '--entrypoint', 'cat', '-v', seed_data + ':/data',
                     IMAGE, '/data/repositories/Example/seed-hooks.saved/post-commit')
        assert 'exit 1' in saved
        seed_name = PREFIX + '-seed'
        start(seed_name, '-v', seed_data + ':/data', '-v', str(root / 'auth') + ':/auth:ro',
              '-v', str(root / 'repos.json') + ':/config/repos.json:ro', IMAGE)
        wait_http(seed_name)
        call('exec', seed_name, 'svn', 'ls', 'http://127.0.0.1/svn/Example',
             '--non-interactive', '--username', 'alice', '--password', 'test-pass')
        print('PASS: initialization, UUID, LAN reads, proxied commit/copy/move, authz, write guard, live locks, revprops, outage/recovery, health, hotcopy seed')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
