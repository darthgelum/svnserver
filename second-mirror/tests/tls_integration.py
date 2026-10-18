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
printf 'subjectAltName=IP:127.0.0.1\\nextendedKeyUsage=serverAuth\\n' > /fixture/extensions
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
        call('run', '-d', '--name', gateway, '--network', PREFIX,
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
        # Run a normal SVN client in the gateway's network namespace. The URL IP
        # matches the certificate SAN; the fixture CA is explicitly trusted.
        client_args = ['run', '--rm', '--network', 'container:' + gateway,
                       '-v', str(root) + ':/fixture:ro', '--entrypoint', 'svn', SVN]
        url = 'https://127.0.0.1/svn/Example'
        def svn(*args):
            return call(*client_args, *args, *flags)
        assert svn('cat', url + '/trunk/asset') == 'asset'
        svn('mkdir', url + '/branches', '-m', 'TLS commit')
        svn('copy', url + '/trunk', url + '/branches/copied', '-m', 'TLS copy')
        svn('move', url + '/branches/copied', url + '/branches/moved', '-m', 'TLS move')
        svn('lock', url + '/trunk/asset', '-m', 'TLS lock')
        assert 'Lock Token' in svn('info', url + '/trunk/asset')
        svn('unlock', url + '/trunk/asset')
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
        print('PASS: HTTP-only bootstrap, ACME failure/retry, issuance, automatic TLS reload, trusted IP TLS, SVN commit/copy/move/lock, HTTP isolation')
finally:
    for name in reversed(containers):
        docker('rm', '-f', name, ok=False)
    for name in reversed(volumes):
        docker('volume', 'rm', name, ok=False)
    docker('network', 'rm', PREFIX, ok=False)
