"""Offline TLS lifecycle/SVN proxy test. Does NOT contact Let's Encrypt."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

PREFIX = 'svntls-test-' + uuid.uuid4().hex[:10]
TLS = 'svn-second-mirror-tls:local'
SVN = 'svn-second-mirror:local'
containers, volumes = [], []


def docker(*args, ok=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True)
    if ok and result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result


def call(*args):
    return docker(*args).stdout.strip()


try:
    call('network', 'create', PREFIX)
    for suffix in ('repo', 'certs'):
        name = PREFIX + '-' + suffix
        call('volume', 'create', name)
        volumes.append(name)
    with tempfile.TemporaryDirectory(prefix=PREFIX) as directory:
        root = Path(directory)
        def write(name, value):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(value.encode())
        write('master.json', json.dumps({'repositories': [{'name': 'Example', 'path': '/svn/Example'}]}))
        write('auth/authz', '[Example:/]\nalice = rw\n* =\n')
        fixture = '''set -eu
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=Offline-Test-CA -keyout /fixture/ca.key -out /fixture/ca.crt
openssl req -new -newkey rsa:2048 -nodes -subj /CN=127.0.0.1 -keyout /fixture/server.key -out /fixture/server.csr
printf 'subjectAltName=IP:127.0.0.1,DNS:https-gateway\\nextendedKeyUsage=serverAuth\\n' > /fixture/extensions
openssl x509 -req -in /fixture/server.csr -CA /fixture/ca.crt -CAkey /fixture/ca.key -set_serial 1 -days 2 -extfile /fixture/extensions -out /fixture/first.pem
openssl x509 -req -in /fixture/server.csr -CA /fixture/ca.crt -CAkey /fixture/ca.key -set_serial 2 -days 2 -extfile /fixture/extensions -out /fixture/second.pem
'''
        call('run', '--rm', '--entrypoint', '/bin/sh', '-v', str(root) + ':/fixture', TLS, '-c', fixture)
        fixture = '''set -eu
svnadmin create /repositories/Example
svn mkdir file:///repositories/Example/trunk -m initial
printf asset > /tmp/asset
svn import /tmp/asset file:///repositories/Example/trunk/asset -m asset
chown -R www-data:www-data /repositories/Example
htpasswd -bc /fixture/auth/htpasswd alice test-pass
'''
        call('run', '--rm', '--entrypoint', '/bin/sh', '-v', volumes[0] + ':/repositories',
             '-v', str(root) + ':/fixture', SVN, '-c', fixture)
        master, gateway = PREFIX + '-master', PREFIX + '-https'
        containers.append(master)
        call('run', '-d', '--name', master, '--network', PREFIX, '--network-alias', 'master-http',
             '-e', 'ROLE=master', '-v', volumes[0] + ':/repositories',
             '-v', str(root / 'auth') + ':/auth:ro', '-v', str(root) + ':/fixture:ro',
             '-v', str(root / 'master.json') + ':/config/repos.json:ro', SVN)
        # Fake Certbot: first request fails, retry issues a cert, next check renews.
        write('bin/certbot', '''#!/usr/local/bin/python3
from pathlib import Path
import shutil
import sys
counter = Path('/etc/letsencrypt/calls')
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
assert '--ip-address' in sys.argv and '--preferred-profile' in sys.argv
assert sys.argv[sys.argv.index('--preferred-profile') + 1] == 'shortlived'
if count == 1:
    sys.exit(1)
target = Path('/etc/letsencrypt/live/svn-ip')
target.mkdir(parents=True, exist_ok=True)
shutil.copyfile('/fixture/first.pem' if count == 2 else '/fixture/second.pem', target / 'fullchain.pem')
shutil.copyfile('/fixture/server.key', target / 'privkey.pem')
''')
        write('launch.py', '''import sys
sys.path.insert(0, '/app')
import run
clock = run.time.monotonic
run.time.monotonic = lambda: clock() * 120
run.main()
''')
        containers.append(gateway)
        call('run', '-d', '--name', gateway, '--network', PREFIX, '--network-alias', 'https-gateway',
             '-e', 'PUBLIC_IP=127.0.0.1', '-e', 'ACME_EMAIL=offline@example.invalid',
             '-e', 'SSL_CERT_FILE=/fixture/ca.crt', '-e', 'PATH=/fixture/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
             '-e', 'RENEW_INTERVAL_SECONDS=60', '-e', 'ACME_RETRY_SECONDS=60',
             '-v', str(root) + ':/fixture:ro', '-v', volumes[1] + ':/etc/letsencrypt',
             '--entrypoint', 'python3', TLS, '/fixture/launch.py')
        for _ in range(40):
            check = docker('exec', gateway, 'python3', '/app/run.py', 'health', ok=False)
            if check.returncode == 0:
                break
            time.sleep(.5)
        else:
            log = docker('logs', gateway)
            raise RuntimeError(log.stdout + log.stderr + check.stderr)
        # Trust the fixture CA, while retaining normal certificate/IP checks.
        flags = ['--non-interactive', '--no-auth-cache', '--username', 'alice', '--password', 'test-pass',
                 '--config-option', 'servers:global:ssl-authority-files=/fixture/ca.crt']
        # Run a normal SVN client against a DNS SAN, as used by the DSM proxy.
        # The fixture CA is explicitly trusted; Python also checks the IP SAN.
        client_args = ['run', '--rm', '--network', 'container:' + gateway,
                       '-v', str(root) + ':/fixture:ro', '--entrypoint', 'svn', SVN]
        url = 'https://https-gateway/svn/Example'
        def svn(*args):
            return call(*client_args, *args, *flags)
        assert svn('cat', url + '/trunk/asset') == 'asset'
        svn('mkdir', url + '/branches', '-m', 'TLS commit')
        svn('copy', url + '/trunk', url + '/branches/copied', '-m', 'TLS copy')
        svn('move', url + '/branches/copied', url + '/branches/moved', '-m', 'TLS move')
        svn('lock', url + '/trunk/asset', '-m', 'TLS lock')
        assert 'Lock Token' in svn('info', url + '/trunk/asset')
        svn('unlock', url + '/trunk/asset')
        # Actual mirror -> verified TLS -> reverse proxy -> master chain.
        # No certificate verification bypass and no replication during checks.
        data = PREFIX + '-mirror'
        call('volume', 'create', data)
        volumes.append(data)
        write('repos.json', json.dumps({'repositories': [{'name': 'Example', 'path': '/svn/Example',
              'master_url': 'https://https-gateway/svn/Example', 'master_version': '1.14.1'}]}))
        write('source.json', json.dumps({'default': {'username': 'alice', 'password': 'test-pass'}}))
        trust = 'cp /fixture/ca.crt /usr/local/share/ca-certificates/fixture.crt; update-ca-certificates >/dev/null; exec python3 /opt/svnmirror/app.py '
        args = ['--network', PREFIX, '-v', data + ':/data', '-v', str(root) + ':/fixture:ro',
                '-v', str(root / 'auth') + ':/auth:ro',
                '-v', str(root / 'repos.json') + ':/config/repos.json:ro',
                '-v', str(root / 'source.json') + ':/secrets/source.json:ro', '--entrypoint', 'bash']
        call('run', '--rm', *args, SVN, '-ec', trust + 'init')
        call('run', '--rm', *args, SVN, '-ec', trust + 'sync-once')
        mirror = PREFIX + '-mirror'
        containers.append(mirror)
        call('run', '-d', '--name', mirror, *args, SVN, '-ec', trust + 'serve')
        for _ in range(40):
            check = docker('exec', mirror, 'svn', 'info', 'http://127.0.0.1/svn/Example', *flags, ok=False)
            if check.returncode == 0:
                break
            time.sleep(.3)
        else:
            raise RuntimeError(check.stdout + check.stderr + docker('logs', mirror).stderr)
        before = docker('logs', master).stdout
        call('exec', mirror, 'svn', 'checkout', 'http://127.0.0.1/svn/Example/trunk', '/tmp/wc', *flags)
        assert '"GET ' not in docker('logs', master).stdout[len(before):]
        svn('lock', url + '/trunk/asset', '-m', 'direct master lock')
        assert '<owner>alice</owner>' in call('exec', mirror, 'svn', 'status', '-u', '--xml', '/tmp/wc', *flags)
        call('exec', mirror, 'svn', 'unlock', 'http://127.0.0.1/svn/Example/trunk/asset', *flags)
        assert 'Lock Token' not in svn('info', url + '/trunk/asset')
        call('exec', mirror, 'svn', 'lock', '/tmp/wc/asset', *flags)
        call('exec', mirror, 'bash', '-c', 'printf changed >> /tmp/wc/asset')
        call('exec', mirror, 'svn', 'commit', '/tmp/wc', '-m', 'locked commit over TLS', *flags)
        assert 'Lock Token' not in svn('info', url + '/trunk/asset')
        # Verify retry/renewal happened and nginx serves the replacement cert.
        probe = '''import socket, ssl
c = ssl.create_default_context()
with socket.create_connection(('127.0.0.1',443)) as s:
    with c.wrap_socket(s,server_hostname='127.0.0.1') as t:
        assert t.getpeercert()['serialNumber'] == '02'
'''
        call('exec', gateway, 'python3', '-c', probe)
        # HTTP does not expose repository content or request SVN credentials.
        probe = '''import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1/svn/Example')
except urllib.error.HTTPError as e:
    assert e.code == 404
else:
    raise AssertionError('HTTP must not expose SVN')
'''
        call('exec', gateway, 'python3', '-c', probe)
        print('PASS: TLS lifecycle, verified mirror HTTPS upstream, LAN checkout (no master GET), immediate master lock status, local lock/commit/unlock, copy/move and HTTP isolation')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
