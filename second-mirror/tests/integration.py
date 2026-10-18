"""Run with Python 3 + Docker Desktop/Engine. Uses disposable containers/volumes only."""
import json
import os
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
        (root / 'auth' / 'authz').write_text('[Example:/]\nalice = rw\nbob = rw\nreader = r\n* =\n')
        fixture = '''set -eu
export LC_ALL=C.UTF-8
svnadmin create /repositories/Example
svn mkdir file:///repositories/Example/trunk -m initial
head -c 4194304 /dev/urandom > /tmp/asset.uasset
svn import /tmp/asset.uasset file:///repositories/Example/trunk/asset.uasset -m asset
svn checkout file:///repositories/Example/trunk /tmp/fixture-wc
printf special > '/tmp/fixture-wc/asset space #?é.uasset'
svn add '/tmp/fixture-wc/asset space #?é.uasset'
svn commit /tmp/fixture-wc -m 'encoded paths'
chown -R www-data:www-data /repositories/Example
htpasswd -bc /fixture/auth/htpasswd alice test-pass
htpasswd -b /fixture/auth/htpasswd bob test-pass
htpasswd -b /fixture/auth/htpasswd reader test-pass
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
        start(mirror_name, '-p', '127.0.0.1::80', '-v', mirror + ':/data', '-v', str(root / 'auth') + ':/auth:ro',
              '-v', str(root / 'repos.json') + ':/config/repos.json:ro', IMAGE)
        wait_http(mirror_name)

        def svn(*args, user='alice', ok=True):
            return docker('exec', '-e', 'LC_ALL=C.UTF-8', mirror_name, 'svn', *args, '--non-interactive',
                          '--no-auth-cache', '--username', user, '--password', 'test-pass', ok=ok)

        local = 'http://127.0.0.1/svn/Example'
        live = 'http://127.0.0.1:81/svn/Example'
        # Force even a bulk-preferring client to fetch asset bytes over LAN.
        # No worker runs during this interval: every master GET would be WAN
        # content downloaded by the client, rather than replication traffic.
        before = docker('logs', master_name).stdout
        svn('checkout', local + '/trunk', '/tmp/alice', '--config-option',
            'servers:global:http-bulk-updates=yes')
        traffic = docker('logs', master_name).stdout[len(before):]
        assert '"GET ' not in traffic, traffic
        assert '"REPORT ' in traffic, traffic
        assert '"GET ' in docker('logs', mirror_name).stdout
        assert call('exec', mirror_name, 'sha256sum', '/tmp/alice/asset.uasset').split()[0] == call(
            'exec', master_name, 'bash', '-c',
            'svnlook cat /repositories/Example trunk/asset.uasset | sha256sum').split()[0]
        svn('lock', '/tmp/alice/asset space #?é.uasset')
        special = local + '/trunk/asset%20space%20%23%3F%C3%A9.uasset'
        assert '<owner>alice</owner>' in svn('info', '--xml', special).stdout
        svn('unlock', '/tmp/alice/asset space #?é.uasset')
        master_uuid = call('exec', master_name, 'svnlook', 'uuid', '/repositories/Example')
        assert master_uuid == call('exec', mirror_name, 'svnlook', 'uuid', '/data/repositories/Example')
        # A commit through the mirror must create exactly one MASTER revision.
        svn('mkdir', local + '/branches', '-m', 'write-through')
        assert call('exec', master_name, 'svnlook', 'youngest', '/repositories/Example') == '4'
        assert call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example') == '3'
        sync('sync-once')
        assert call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example') == '4'
        # Server-side COPY/MOVE must rewrite the proxy URL correctly.
        svn('copy', local + '/trunk', local + '/branches/test', '-m', 'copy through proxy')
        sync('sync-once')
        svn('move', local + '/branches/test', local + '/branches/moved', '-m', 'move through proxy')
        sync('sync-once')
        assert svn('mkdir', local + '/forbidden', '-m', 'no', user='reader', ok=False).returncode != 0
        # An attempted file:// write as Apache's user is rejected by the mirror hook.
        blocked = docker('exec', '--user', 'www-data', mirror_name, 'svn', 'mkdir',
                         'file:///data/repositories/Example/illegal', '-m', 'blocked', ok=False)
        assert blocked.returncode != 0 and 'mirror' in blocked.stderr
        # Unreal uses svn status --show-updates --xml and info for lock state.
        # These must reflect the master immediately, WITHOUT a sync pass.
        svn('propset', 'svn:needs-lock', '*', '/tmp/alice/asset.uasset')
        svn('commit', '/tmp/alice', '-m', 'needs lock')
        sync('sync-once')
        svn('checkout', local + '/trunk', '/tmp/bob', user='bob')
        head = call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example')
        svn('lock', '/tmp/alice/asset.uasset', '-m', 'artist alice')
        assert '<owner>alice</owner>' in svn('info', '--xml', local + '/trunk/asset.uasset', user='bob').stdout
        state = svn('status', '--show-updates', '--xml', '/tmp/bob', user='bob').stdout
        assert '<owner>alice</owner>' in state, state
        assert svn('lock', '/tmp/bob/asset.uasset', user='bob', ok=False).returncode != 0
        assert svn('lock', local + '/trunk/asset.uasset', user='reader', ok=False).returncode != 0
        call('exec', mirror_name, 'bash', '-c', 'chmod u+w /tmp/bob/asset.uasset; printf bad >> /tmp/bob/asset.uasset')
        assert svn('commit', '/tmp/bob', '-m', 'no lock', user='bob', ok=False).returncode != 0
        svn('revert', '/tmp/bob/asset.uasset', user='bob')
        call('exec', mirror_name, 'bash', '-c', 'printf good >> /tmp/alice/asset.uasset')
        svn('commit', '/tmp/alice', '-m', 'locked asset commit')
        assert '<lock>' not in svn('info', '--xml', local + '/trunk/asset.uasset', user='bob').stdout
        assert call('exec', mirror_name, 'svnlook', 'youngest', '/data/repositories/Example') == head
        sync('sync-once')
        before = docker('logs', master_name).stdout
        svn('update', '/tmp/bob', user='bob')
        assert '"GET ' not in docker('logs', master_name).stdout[len(before):]
        # Direct Belgrade locks and force-steals must also be visible locally.
        svn('lock', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
        assert '<owner>bob</owner>' in svn('status', '-u', '--xml', '/tmp/alice').stdout
        svn('unlock', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
        svn('update', '/tmp/alice')
        svn('lock', '/tmp/alice/asset.uasset')
        svn('lock', '--force', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
        state = svn('status', '-u', '--xml', '/tmp/alice').stdout
        assert '<owner>bob</owner>' in state, state
        svn('update', '/tmp/alice')
        state = svn('info', '--xml', '/tmp/alice/asset.uasset').stdout
        assert '<lock>' not in state, state  # stale local token removed by update
        svn('unlock', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
        assert '<lock>' not in svn('status', '-u', '--xml', '/tmp/alice').stdout
        # The compatibility LIVE port remains available.
        svn('lock', live + '/trunk/asset.uasset')
        assert 'Lock Token' in svn('info', live + '/trunk/asset.uasset').stdout
        svn('unlock', live + '/trunk/asset.uasset')
        # Optional: run the exact SVN executable shipped with Unreal/Tortoise,
        # against this disposable fixture. No production WC or user auth cache.
        if os.getenv('SVN_CLIENT_EXE'):
            endpoint = call('port', mirror_name, '80/tcp')
            native_url = 'http://' + endpoint + '/svn/Example/trunk'
            alice_wc, bob_wc = str(root / 'native-alice'), str(root / 'native-bob')
            def native(*args, user='alice', ok=True):
                result = subprocess.run([os.environ['SVN_CLIENT_EXE'], *args,
                    '--non-interactive', '--no-auth-cache', '--username', user, '--password', 'test-pass',
                    '--config-dir', str(root / 'client-config')], capture_output=True,
                    encoding='utf-8', errors='replace', timeout=90)
                if ok and result.returncode:
                    raise RuntimeError(result.stdout + result.stderr)
                return result
            before = docker('logs', master_name).stdout
            # The Linux URL-escaping fixture deliberately contains '?', which
            # Windows filesystems cannot represent. Materialize only the asset.
            native('checkout', '--depth', 'empty', native_url, alice_wc)
            native('checkout', '--depth', 'empty', native_url, bob_wc, user='bob')
            native('update', str(Path(alice_wc) / 'asset.uasset'))
            native('update', str(Path(bob_wc) / 'asset.uasset'), user='bob')
            assert '"GET ' not in docker('logs', master_name).stdout[len(before):]
            svn('lock', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
            assert '<owner>bob</owner>' in native('status', '--show-updates', '--xml', alice_wc).stdout
            assert native('lock', str(Path(alice_wc) / 'asset.uasset'), ok=False).returncode != 0
            svn('unlock', 'http://master/svn/Example/trunk/asset.uasset', user='bob')
            native('lock', str(Path(alice_wc) / 'asset.uasset'))
            assert '<owner>alice</owner>' in native('status', '--show-updates', '--xml', bob_wc, user='bob').stdout
            with (Path(alice_wc) / 'asset.uasset').open('ab') as file:
                file.write(b'native-client-edit')
            native('commit', alice_wc, '-m', 'Unreal SVN binary locked commit')
            assert '<lock>' not in native('info', '--xml', native_url + '/asset.uasset', user='bob').stdout
            sync('sync-once')
            before = docker('logs', master_name).stdout
            native('update', str(Path(bob_wc) / 'asset.uasset'), user='bob')
            assert (Path(alice_wc) / 'asset.uasset').read_bytes() == (Path(bob_wc) / 'asset.uasset').read_bytes()
            assert '"GET ' not in docker('logs', master_name).stdout[len(before):]
            print('PASS: native SVN client checkout, Unreal XML status, master locks, lock rejection, locked commit/unlock and LAN update', flush=True)
        # Old log edits are reconciled, not merely new revision properties.
        call('exec', master_name, 'bash', '-c',
             'printf "#!/bin/sh\\nexit 0\\n" > /repositories/Example/hooks/pre-revprop-change; '
             'chmod 755 /repositories/Example/hooks/pre-revprop-change')
        svn('propset', '--revprop', '-r', '1', 'svn:log', 'edited log', 'http://master/svn/Example')
        sync('copy-revprops')
        assert 'edited log' in svn('propget', '--revprop', '-r', '1', 'svn:log', local).stdout
        # Offline metadata operations fail closed, never invent an unlocked state.
        # Raw cached GETs remain readable; a normal SVN operation needs metadata.
        call('stop', master_name)
        assert svn('status', '-u', '--xml', '/tmp/bob', user='bob', ok=False).returncode != 0
        assert svn('lock', '/tmp/bob/asset.uasset', user='bob', ok=False).returncode != 0
        call('exec', mirror_name, 'python3', '-c',
             'import urllib.request,base64; r=urllib.request.Request("http://127.0.0.1/svn/Example/trunk/asset.uasset", headers={"Authorization":"Basic "+base64.b64encode(b"alice:test-pass").decode()}); assert len(urllib.request.urlopen(r).read()) > 4194304')
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
        print('PASS: LAN asset checkout/update (no master GET), authoritative info/status locks, two writers, lock rejection, locked commit/unlock, direct master locks, stolen token update, authz, copy/move, offline failure, revprops, seed and health')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
