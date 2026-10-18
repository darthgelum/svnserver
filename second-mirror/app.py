#!/usr/bin/env python3
"""Independent HTTP SVN mirror; never imports or edits the USVN installation."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit
import urllib.request
import xml.etree.ElementTree as ET

DATA = Path('/data')
UID = 10001
SECRETS = {}


def run(args):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=int(os.getenv('SYNC_TIMEOUT_SECONDS', '21600')))
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ includes argv, which can include a password.
        raise RuntimeError(f'{args[0]} timed out; check SYNC_TIMEOUT_SECONDS') from None
    if result.returncode:
        # Do not print command arguments: svnsync source passwords are argv values.
        message = result.stderr.strip()
        for secret in SECRETS.values():
            if secret.get('password'):
                message = message.replace(secret['password'], '[redacted]')
        raise RuntimeError(f'{args[0]} failed: {message}')
    return result.stdout.strip()


def config():
    repos = json.loads(Path('/config/repos.json').read_text())['repositories']
    if not isinstance(repos, list) or not repos:
        raise ValueError('repositories must be a non-empty list')
    names, paths = set(), set()
    for repo in repos:
        name, path = repo['name'], repo['path']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError('Repository names must contain only letters, numbers, _ or -')
        if not re.fullmatch(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', path):
            raise ValueError('Invalid public repository path')
        if name in names or any(path == p or path.startswith(p + '/') or p.startswith(path + '/') for p in paths):
            raise ValueError('Duplicate name or overlapping repository URL paths')
        names.add(name)
        paths.add(path)
        if os.getenv('ROLE', 'mirror') != 'master':
            url = urlsplit(repo['master_url'])
            if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.path != path
                    or 'REPLACE' in repo['master_url'].upper()
                    or re.search(r'[\s<>"\\]', repo['master_url'])):
                raise ValueError('master_url must be HTTP(S), have the SAME path, no credentials/query/trailing slash')
            if not re.fullmatch(r'\d+\.\d+\.\d+', repo['master_version']):
                raise ValueError('Set the actual master_version (svn --version --quiet)')
    return repos


def repo_path(repo):
    return DATA / 'repositories' / repo['name']


def local_url(repo):
    return repo_path(repo).as_uri()


def prepare_worker():
    global SECRETS
    # Only the worker mounts credentials. Apache never sees the source password.
    SECRETS = json.loads(Path('/secrets/source.json').read_text())
    DATA.mkdir(parents=True, exist_ok=True)
    for folder in (DATA / 'repositories', DATA / 'status', DATA / 'svn-config'):
        folder.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(folder, UID, UID)
            folder.chmod(0o2770)
    if os.geteuid() == 0:
        os.setgroups([UID])
        os.setgid(UID)
        os.setuid(UID)
    os.umask(0o007)


@contextlib.contextmanager
def worker_lock():
    # Every worker/admin operation uses the same OS lock, including --run containers.
    with (DATA / 'status' / 'worker.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another sync/admin process is running; stop sync first')
        yield


def source_auth(repo, sync=False):
    auth = SECRETS[repo['name']]
    return ['--non-interactive', '--no-auth-cache', '--config-dir', str(DATA / 'svn-config'),
            '--source-username' if sync else '--username', auth['username'],
            '--source-password' if sync else '--password', auth['password']]


def source_info(repo):
    doc = ET.fromstring(run(['svn', 'info', '--xml', repo['master_url'], *source_auth(repo)]))
    entry = doc.find('entry')
    if entry.findtext('repository/root') != repo['master_url']:
        raise RuntimeError('master_url must point to a repository ROOT, not trunk or a subdirectory')
    return entry.findtext('repository/uuid'), int(entry.attrib['revision'])


def prop(repo, name):
    # svnlook returns an error for an absent revprop; do not mask other failures.
    props = run(['svnlook', 'proplist', '--revprop', '-r', '0', str(repo_path(repo))]).splitlines()
    if name not in [line.strip() for line in props]:
        return None
    return run(['svnlook', 'propget', '--revprop', '-r', '0', str(repo_path(repo)), name])


def guards(repo):
    for hook in ('pre-commit', 'pre-revprop-change', 'pre-lock', 'pre-unlock'):
        target = repo_path(repo) / 'hooks' / hook
        shutil.copyfile('/opt/svnmirror/write-guard', target)
        target.chmod(0o750)


def initialize(repo, seed=False):
    path = repo_path(repo)
    source_uuid, _ = source_info(repo)
    if not path.exists():
        if seed:
            raise RuntimeError('Seed repository is absent')
        run(['svnadmin', 'create', str(path)])
    initialized = prop(repo, 'svn:sync-from-url')
    if initialized:
        if initialized != repo['master_url'] or prop(repo, 'svn:sync-from-uuid') != source_uuid:
            raise RuntimeError('Existing mirror belongs to a different source; create a NEW data directory')
        if run(['svnlook', 'uuid', str(path)]) != source_uuid:
            raise RuntimeError('Mirror UUID does not match its source')
        guards(repo)
        return
    youngest = int(run(['svnlook', 'youngest', str(path)]))
    if youngest and not seed:
        raise RuntimeError('Refusing an uninitialized non-empty repository; use the documented seed procedure')
    if seed:
        if run(['svnlook', 'uuid', str(path)]) != source_uuid:
            raise RuntimeError('Seed UUID differs from source UUID')
        run(['svnadmin', 'verify', str(path)])
        # A hotcopy brings hooks from the master. NEVER run them on a mirror.
        archive = path / 'seed-hooks.saved'
        if archive.exists():
            raise RuntimeError('seed-hooks.saved already exists; inspect previous seed attempt')
        (path / 'hooks').rename(archive)
        (path / 'hooks').mkdir()
    guards(repo)
    run(['svnadmin', 'setuuid', str(path), source_uuid])
    args = ['svnsync', 'initialize', local_url(repo), repo['master_url'], *source_auth(repo, sync=True)]
    if seed:
        args.append('--allow-non-empty')
    run(args)


def sync_one(repo, full_props=False):
    name = repo['name']
    try:
        if prop(repo, 'svn:sync-from-url') != repo['master_url']:
            raise RuntimeError('Mirror not initialized for this source; run init first')
        source_uuid, head = source_info(repo)
        if run(['svnlook', 'uuid', str(repo_path(repo))]) != source_uuid:
            raise RuntimeError('Source UUID changed; refusing synchronization')
        run(['svnsync', 'synchronize', local_url(repo), *source_auth(repo, sync=True)])
        if full_props:
            run(['svnsync', 'copy-revprops', local_url(repo), *source_auth(repo, sync=True)])
        local = int(run(['svnlook', 'youngest', str(repo_path(repo))]))
        merged = int(prop(repo, 'svn:sync-last-merged-rev'))
        _, head = source_info(repo)
        state = {'ok': True, 'checked_at': time.time(), 'local_revision': local,
                 'last_merged_revision': merged, 'master_revision': head,
                 'lag_revisions': head - merged}
        if full_props:
            state['revprops_checked_at'] = time.time()
        else:
            old = DATA / 'status' / f'{name}.json'
            if old.exists():
                state['revprops_checked_at'] = json.loads(old.read_text()).get('revprops_checked_at', 0)
        print(f'{name}: r{merged}/{head}; lag={head - merged}', flush=True)
    except Exception as exc:
        state = {'ok': False, 'checked_at': time.time(), 'error': str(exc)}
        print(f'{name}: {exc}', file=sys.stderr, flush=True)
    target = DATA / 'status' / f'{name}.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(state) + '\n')
    temporary.replace(target)
    return state['ok']


def status(repos, check=False):
    healthy = True
    for repo in repos:
        try:
            state = json.loads((DATA / 'status' / f'{repo["name"]}.json').read_text())
            age = time.time() - state['checked_at']
            good = (state['ok'] and age <= int(os.getenv('MAX_SYNC_AGE_SECONDS', '300'))
                    and 0 <= state['lag_revisions'] <= int(os.getenv('MAX_LAG_REVISIONS', '0'))
                    and state['local_revision'] == state['last_merged_revision'])
            print(repo['name'], json.dumps(state), f'age={int(age)}s')
            healthy = healthy and good
        except (OSError, KeyError, ValueError) as exc:
            print(repo['name'], 'no valid sync status:', exc)
            healthy = False
    if check:
        # Probe Apache separately from worker state. No credentials or repository data.
        try:
            urllib.request.urlopen('http://127.0.0.1/healthz', timeout=3).read()
        except Exception:
            healthy = False
    return 0 if healthy else 1


def render(repos):
    master = os.getenv('ROLE', 'mirror') == 'master'
    auth = '''
        AuthType Basic
        AuthName "Subversion"
        AuthUserFile /auth/htpasswd
        AuthzSVNAccessFile /auth/authz
        Require valid-user
'''
    for filename in ('htpasswd', 'authz'):
        if not (Path('/auth') / filename).is_file():
            raise RuntimeError(f'Missing /auth/{filename}')
    blocks, live = [], []
    for repo in repos:
        path = Path('/repositories') / repo['name'] if master else repo_path(repo)
        if not (path / 'format').is_file():
            raise RuntimeError(f'{repo["name"]}: initialize the mirror or fix the master repository mount')
        if not master:
            if prop(repo, 'svn:sync-from-url') != repo['master_url']:
                raise RuntimeError('Mirror source changed or not initialized')
            if prop(repo, 'svn:sync-from-uuid') != run(['svnlook', 'uuid', str(path)]):
                raise RuntimeError('Mirror UUID mismatch')
        proxy = '' if master else f'''SVNMasterURI "{repo['master_url']}"
        SVNMasterVersion {repo['master_version']}'''
        blocks.append(f'''<Location "{repo['path']}">
        DAV svn
        SVNPath "{path}"
        {proxy}
        {auth}
    </Location>''')
        if not master:
            # Same URL path on a different port. All operations, including locks,
            # are authoritative on this port; no local lock replication is claimed.
            live.append(f'''ProxyPass "{repo['path']}" "{repo['master_url']}" nocanon
    ProxyPassReverse "{repo['path']}" "{repo['master_url']}"''')
    header = '''ProxyRequests Off
SSLProxyEngine On
SSLProxyVerify require
SSLProxyCheckPeerName On
SSLProxyCACertificateFile /etc/ssl/certs/ca-certificates.crt
ProxyTimeout 21600
Timeout 21600
'''
    health = '''Alias /healthz /var/www/html/healthz
    <Location /healthz>
        Require all granted
    </Location>'''
    document = header + '\n<VirtualHost *:80>\n    ' + health + '\n    ' + '\n    '.join(blocks) + '''
    ErrorLog /proc/self/fd/2
    CustomLog /proc/self/fd/1 combined
</VirtualHost>
'''
    if not master:
        document += '<VirtualHost *:81>\n    ' + '\n    '.join(live) + '''
    <Location />
        Require all granted
    </Location>
    ErrorLog /proc/self/fd/2
    CustomLog /proc/self/fd/1 combined
</VirtualHost>
'''
    Path('/var/www/html/healthz').write_text('apache ready\n')
    Path('/etc/apache2/sites-enabled/svnmirror.conf').write_text(document)
    run(['apache2ctl', '-t'])


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    repos = config()
    if command in ('serve', 'render'):
        render(repos)
        if command == 'serve':
            os.execvp('apache2ctl', ['apache2ctl', '-D', 'FOREGROUND'])
        return 0
    if command in ('status', 'health'):
        return status(repos, check=command == 'health')
    prepare_worker()
    with worker_lock():
        if command in ('init', 'seed'):
            if command == 'seed':
                if len(sys.argv) != 3:
                    raise ValueError('Usage: seed REPO_NAME')
                repos = [repo for repo in repos if repo['name'] == sys.argv[2]]
                if not repos:
                    raise ValueError('Unknown repository name')
            for repo in repos:
                initialize(repo, seed=command == 'seed')
            return 0
        if command == 'recover':
            if len(sys.argv) != 3:
                raise ValueError('Usage: recover REPO_NAME')
            repo = next(r for r in repos if r['name'] == sys.argv[2])
            if prop(repo, 'svn:sync-lock') is not None:
                run(['svn', 'propdel', '--revprop', '-r', '0', 'svn:sync-lock', local_url(repo)])
            return 0
        if command not in ('sync-once', 'sync-loop', 'copy-revprops'):
            raise ValueError('Unknown command')
        while True:
            success = True
            for repo in repos:
                target = DATA / 'status' / f'{repo["name"]}.json'
                previous = json.loads(target.read_text()) if target.exists() else {}
                rescan = time.time() - previous.get('revprops_checked_at', 0) >= int(os.getenv('REVPROP_RESCAN_SECONDS', '3600'))
                success = sync_one(repo, full_props=rescan or command == 'copy-revprops') and success
            if command != 'sync-loop':
                return 0 if success else 1
            time.sleep(max(1, int(os.getenv('SYNC_INTERVAL_SECONDS', '30'))))


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
