#!/usr/bin/env python3
"""Independent HTTP SVN mirror; never imports or edits the USVN installation."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import urllib.request
import xml.etree.ElementTree as ET
import auto
import signal
import worker
import telemetry

DATA = Path('/data')
UID = 10001
SECRETS = {}
GUARD_FILE = Path(__file__).with_name('write-guard')


def redact(message):
    for secret in SECRETS.values():
        if secret.get('password'):
            message = message.replace(secret['password'], '[redacted]')
    return message


def event(repo, message, level='info'):
    safe = redact(str(message))
    telemetry.record(DATA, safe, repo['name'] if repo else None, level)
    print((repo['name'] + ': ' if repo else '') + safe, flush=True)


def run(args, **kwargs):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=kwargs.pop('timeout', int(os.getenv('SYNC_TIMEOUT_SECONDS', '21600'))),
                                pass_fds=worker.descriptors(), **kwargs)
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ includes argv, which can include a password.
        raise RuntimeError(f'{args[0]} timed out; check SYNC_TIMEOUT_SECONDS') from None
    if result.returncode:
        # Do not print command arguments: svnsync source passwords are argv values.
        message = result.stderr.strip()
        message = redact(message)
        raise RuntimeError(f'{args[0]} failed: {message}')
    return result.stdout.strip()


def config():
    settings = auto.settings()
    if settings.get('auto_discover'):
        return auto.repositories(settings)
    repos = settings['repositories']
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
            if repo.get('source_url'):
                source = urlsplit(repo['source_url'])
                if source.scheme != url.scheme or source.netloc != url.netloc or source.query or source.fragment:
                    raise ValueError('source_url must have the same origin as master_url')
    return repos


def repo_path(repo):
    return DATA / 'repositories' / repo['name']


def local_url(repo):
    return repo_path(repo).as_uri()


def prepare_worker(drop=True):
    global SECRETS
    # Only the worker mounts credentials. Apache never sees the source password.
    SECRETS = json.loads(Path('/secrets/source.json').read_text())
    DATA.mkdir(parents=True, exist_ok=True)
    if any((DATA / name).exists() for name in ('format', 'svnrepo', 'usvn')):
        raise RuntimeError('MIRROR_DATA must be a dedicated mirror-state directory, not a repository or the old volume folder')
    # Docker/Synology may create the bind-mount root as root:root 0700. Owning
    # its children is insufficient: the worker also needs to traverse /data.
    for folder in (DATA, DATA / 'repositories', DATA / 'status', DATA / 'svn-config', DATA / 'auth'):
        folder.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(folder, UID, UID)
            folder.chmod(0o2770)
    if drop:
        drop_worker_identity()
    os.umask(0o007)


def drop_worker_identity():
    if os.geteuid() == 0:
        os.setgroups([UID])
        os.setgid(UID)
        os.setuid(UID)


@contextlib.contextmanager
def worker_lock():
    # Every worker/admin operation uses the same OS lock, including --run containers.
    with worker.exclusive(DATA / 'status' / 'worker.lock',
                          'Another sync/admin process is running; stop sync first', owner=UID):
        yield


def repository_lock_path(repo):
    return repo_path(repo) / 'locks' / 'svn-mirror-worker.lock'


@contextlib.contextmanager
def repository_lock(repo):
    worker.require_lock(DATA / 'status' / 'worker.lock')
    with worker.exclusive(repository_lock_path(repo),
                          repo['name'] + ': another writer owns the repository OS lock'):
        yield


def source_auth(repo, sync=False):
    auth = SECRETS.get(repo['name'], SECRETS.get('default'))
    if not auth:
        raise ValueError('Missing source credentials for ' + repo['name'])
    return ['--non-interactive', '--no-auth-cache', '--config-dir', str(DATA / 'svn-config'),
            '--source-username' if sync else '--username', auth['username'],
            '--source-password' if sync else '--password', auth['password']]


def source_info(repo):
    source_url = repo.get('source_url', repo['master_url'])
    doc = ET.fromstring(run(['svn', 'info', '--xml', source_url, *source_auth(repo)],
                           timeout=int(os.getenv('SYNC_METADATA_TIMEOUT_SECONDS', '120'))))
    entry = doc.find('entry')
    if entry.findtext('repository/root') != source_url:
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
        shutil.copyfile(GUARD_FILE, target)
        target.chmod(0o750)


def initialize(repo, seed=False, verified_seed=False):
    path = repo_path(repo)
    source_uuid, _ = source_info(repo)
    if not path.exists():
        if seed:
            raise RuntimeError('Seed repository is absent')
        run(['svnadmin', 'create', str(path)])
    initialized = prop(repo, 'svn:sync-from-url')
    if initialized:
        if initialized != repo.get('source_url', repo['master_url']) or prop(repo, 'svn:sync-from-uuid') != source_uuid:
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
        if not verified_seed:
            run(['svnadmin', 'verify', str(path)])
        # A hotcopy brings hooks from the master. NEVER run them on a mirror.
        archive = path / 'seed-hooks.saved'
        if archive.exists():
            guard = Path('/opt/svnmirror/write-guard').read_bytes()
            hooks = list((path / 'hooks').iterdir())
            if any(h.name not in ('pre-commit', 'pre-revprop-change', 'pre-lock', 'pre-unlock')
                   or not h.is_file() or h.read_bytes() != guard for h in hooks):
                raise RuntimeError('Seed adoption previously interrupted; inspect hooks before retrying')
        else:
            (path / 'hooks').rename(archive)
            (path / 'hooks').mkdir()
    guards(repo)
    run(['svnadmin', 'setuuid', str(path), source_uuid])
    args = ['svnsync', 'initialize', local_url(repo), repo.get('source_url', repo['master_url']), *source_auth(repo, sync=True)]
    if seed:
        args.append('--allow-non-empty')
    run(args)


def sync_one(repo, full_props=False):
    try:
        with repository_lock(repo):
            return sync_one_locked(repo, full_props)
    except Exception as exc:
        message = redact(str(exc))
        auto.atomic_json(DATA / 'status' / (repo['name'] + '.json'),
                         {'ok': False, 'checked_at': time.time(), 'error': message})
        event(repo, message, 'error')
        return False


def check_mirror_maintenance(repo):
    worker.require_lock(DATA / 'status' / 'worker.lock')
    worker.require_lock(repository_lock_path(repo))
    if prop(repo, 'svn:sync-from-url') != repo.get('source_url', repo['master_url']):
        raise RuntimeError('Refusing recovery: mirror source differs')
    if prop(repo, 'svn:sync-from-uuid') != run(['svnlook', 'uuid', str(repo_path(repo))]):
        raise RuntimeError('Refusing recovery: mirror UUID differs')
    expected = GUARD_FILE.read_bytes()
    for name in ('pre-commit', 'pre-revprop-change', 'pre-lock', 'pre-unlock'):
        if (repo_path(repo) / 'hooks' / name).read_bytes() != expected:
            raise RuntimeError('Refusing automatic recovery: mirror write guard differs')


def fsfs_lock_records(repo):
    """Read FSFS hash records, including locks detached from the tree index.

    A non-atomic seed copy can lose a parent's children index while retaining
    the lock record used by commits. Never edit these files; native SVN will
    validate and remove each candidate through its own locking API.
    """
    if (repo_path(repo) / 'db/fs-type').read_text().strip() != 'fsfs':
        return {}  # Other backends use the native lock listing only.
    records = {}
    for path in sorted((repo_path(repo) / 'db/locks').rglob('*')):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise RuntimeError('Refusing a symlink in the local FSFS lock store')
        with path.open('rb') as handle:
            raw = handle.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise RuntimeError('Local FSFS lock record is too large; no file locks removed')
        stream, values = io.BytesIO(raw), {}
        while True:
            header = stream.readline()
            if header == b'END\n' and stream.read() == b'':
                break
            match = re.fullmatch(rb'K ([0-9]+)\n', header)
            if not match:
                raise RuntimeError('Invalid local FSFS lock record; no file locks removed')
            key = stream.read(int(match[1]))
            if stream.read(1) != b'\n':
                raise RuntimeError('Invalid local FSFS lock key')
            match = re.fullmatch(rb'V ([0-9]+)\n', stream.readline())
            if not match:
                raise RuntimeError('Invalid local FSFS lock value header')
            value = stream.read(int(match[1]))
            if len(value) != int(match[1]) or stream.read(1) != b'\n' or key in values:
                raise RuntimeError('Invalid local FSFS lock value')
            values[key] = value
        if b'path' not in values and b'token' not in values:
            continue  # Directory index, not a file lock.
        locked_path = values.get(b'path', b'').decode('utf-8')
        token = values.get(b'token', b'').decode('utf-8')
        if not locked_path.startswith('/') or not token or re.search(r'[\r\n\x00]', locked_path + token):
            raise RuntimeError('Ambiguous local FSFS lock identity; no file locks removed')
        if locked_path in records and records[locked_path] != token:
            raise RuntimeError('Conflicting local FSFS lock records')
        records[locked_path] = token
    return records


def recover_local_file_locks(repo):
    """Discard locks inherited with a seed; lock truth lives on the master.

    Never call against a master or an independently writable repository. Match
    each candidate token through svnlook before deleting anything, then verify the
    resulting list. This refuses ambiguous/multiline paths rather than guessing.
    """
    check_mirror_maintenance(repo)
    path = str(repo_path(repo))
    env = dict(os.environ, LC_ALL='C.UTF-8')
    listing = run(['svnadmin', 'lslocks', path], env=env)
    locks = re.findall(r'^Path: (/[^\r\n]*)\r?\nUUID Token: ([^\r\n]+)\r?\n', listing, re.M)
    if listing and (not locks or len(locks) != len(re.findall(r'^UUID Token: ', listing, re.M))):
        raise RuntimeError('Cannot safely parse inherited local locks; no file locks removed')
    candidates = fsfs_lock_records(repo)
    for locked_path, token in locks:
        if candidates.get(locked_path, token) != token:
            raise RuntimeError('Local lock listing and storage differ; no file locks removed')
        candidates[locked_path] = token
    locks = []
    for locked_path, token in candidates.items():
        info = run(['svnlook', 'lock', path, locked_path], env=env)
        if not info:
            continue  # An expired physical record is no longer an active lock.
        if re.findall(r'^UUID Token: ([^\r\n]+)\r?$', info, re.M) != [token]:
            raise RuntimeError('Local lock identity differs; no file locks removed')
        locks.append((locked_path, token))
    if not locks:
        return
    auto.atomic_json(DATA / 'status' / (repo['name'] + '-last-file-lock-cleanup.json'),
                     {'checked_at': time.time(), 'paths': [item[0] for item in locks],
                      'reason': 'mirror-only locks; metadata/locking forwarded to master'})
    for locked_path, token in locks:
        run(['svnadmin', 'rmlocks', path, locked_path], env=env)
        if run(['svnlook', 'lock', path, locked_path], env=env):
            raise RuntimeError('Local file lock removal failed direct verification')
    if run(['svnadmin', 'lslocks', path], env=env):
        raise RuntimeError('Inherited local lock cleanup failed verification')
    event(repo, f'Removed {len(locks)} inherited local file locks; master user locks unchanged', 'warning')


def recover_replication_lock(repo):
    check_mirror_maintenance(repo)
    # Read using the destination RA client and verify the mutation through the
    # same API. Preserve currently-copying and all other recovery checkpoints.
    def properties():
        xml = run(['svn', 'proplist', '--revprop', '-r', '0', '--xml', local_url(repo)])
        return {p.attrib['name']: p.text for p in ET.fromstring(xml).iter('property')}
    values = properties()
    if 'svn:sync-lock' not in values:
        return
    auto.atomic_json(DATA / 'status' / (repo['name'] + '-last-recovery.json'),
                     {'checked_at': time.time(), 'old_lock': values['svn:sync-lock'],
                      'reason': 'exclusive worker and repository OS locks acquired'})
    run(['svn', 'propdel', '--revprop', '-r', '0', 'svn:sync-lock', local_url(repo)])
    if 'svn:sync-lock' in properties():
        raise RuntimeError('Replication lock removal failed verification')
    event(repo, 'Recovered stale replication lock; interrupted revision checkpoint preserved', 'warning')


def transfer(repo, operation, head, through_revision=None):
    name = repo['name']
    progress_path = DATA / 'status' / (name + '-progress.json')
    state = {'phase': operation, 'started_at': time.time(), 'master_revision': head}
    def report(pid, elapsed, idle, activity):
        state.update({'state': 'running', 'pid': pid, 'checked_at': time.time(),
                      'elapsed_seconds': elapsed, 'idle_seconds': idle,
                      'local_revision': int(run(['svnlook', 'youngest', str(repo_path(repo))])),
                      'last_merged_revision': prop(repo, 'svn:sync-last-merged-rev'),
                      'copying_revision': prop(repo, 'svn:sync-currently-copying')})
        state['transaction_bytes'] = sum(item[1] for item in activity[2])
        telemetry.heartbeat(DATA, state='running', current_repository=name, phase=operation)
        auto.atomic_json(progress_path, state)
        event(repo, f'{operation}; local=r{state["local_revision"]}, '
              f'copying={state["copying_revision"] or "none"}, target=r{head}; '
              f'elapsed={elapsed}s idle={idle}s')
    try:
        stop_when = ((lambda line: line == f'Copied properties for revision {through_revision}.')
                     if through_revision is not None else None)
        worker.transfer(['svnsync', operation, local_url(repo), *source_auth(repo, sync=True)],
                        repo_path(repo), report,
                        lambda line: event(repo, line), redact, stop_when=stop_when)
        state['state'] = 'completed'
    except BaseException:
        state['state'] = 'interrupted'
        raise
    finally:
        state['checked_at'] = time.time()
        auto.atomic_json(progress_path, state)
        telemetry.heartbeat(DATA, current_repository=None, phase=None)


def sync_one_locked(repo, full_props=False):
    name = repo['name']
    try:
        if prop(repo, 'svn:sync-from-url') != repo.get('source_url', repo['master_url']):
            raise RuntimeError('Mirror not initialized for this source; run init first')
        source_uuid, head = source_info(repo)
        if run(['svnlook', 'uuid', str(repo_path(repo))]) != source_uuid:
            raise RuntimeError('Source UUID changed; refusing synchronization')
        if int(run(['svnlook', 'youngest', str(repo_path(repo))])) > head:
            raise RuntimeError('Mirror history is ahead of source; refusing automatic recovery/synchronization')
        recover_replication_lock(repo)
        recover_local_file_locks(repo)
        batch = max(0, int(os.getenv('SYNC_BATCH_REVISIONS', '0')))
        local = int(run(['svnlook', 'youngest', str(repo_path(repo))]))
        through = local + batch if batch and local + batch < head else None
        transfer(repo, 'synchronize', head, through_revision=through)
        caught_up = int(prop(repo, 'svn:sync-last-merged-rev')) >= head
        if full_props and caught_up:
            recover_replication_lock(repo)
            transfer(repo, 'copy-revprops', head)
        local = int(run(['svnlook', 'youngest', str(repo_path(repo))]))
        merged = int(prop(repo, 'svn:sync-last-merged-rev'))
        _, head = source_info(repo)
        state = {'ok': True, 'checked_at': time.time(), 'last_success_at': time.time(), 'local_revision': local,
                 'last_merged_revision': merged, 'master_revision': head,
                 'lag_revisions': head - merged}
        if full_props and caught_up:
            state['revprops_checked_at'] = time.time()
        else:
            old = DATA / 'status' / f'{name}.json'
            if old.exists():
                state['revprops_checked_at'] = json.loads(old.read_text()).get('revprops_checked_at', 0)
        event(repo, f'r{merged}/{head}; lag={head - merged}')
    except Exception as exc:
        state = auto.read_json(DATA / 'status' / f'{name}.json', {})
        state.update({'ok': False, 'checked_at': time.time(), 'error': redact(str(exc))})
        event(repo, str(exc), 'error')
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
    settings = auto.settings()
    automatic = settings.get('auto_discover', False)
    auth_root = '/data/auth/current' if automatic and not master and settings.get('sync_auth') else '/auth'
    auth = '''
        AuthType Basic
        AuthName "Subversion"
        AuthUserFile AUTH_ROOT/htpasswd
        AuthzSVNAccessFile AUTH_ROOT/authz
        Require valid-user
'''.replace('AUTH_ROOT', auth_root)
    for filename in ('htpasswd', 'authz'):
        if not (Path(auth_root) / filename).is_file():
            if automatic and not master and not repos:
                continue
            raise RuntimeError(f'Missing /auth/{filename}')
    blocks, live, metadata = [], [], []
    for repo in repos:
        if not master and automatic and repo.get('lock_metadata_mode') != 'master-skelta-v1':
            raise RuntimeError('Update master-http, then run sync discovery before starting the mirror')
        path = Path('/repositories') / repo['name'] if master else repo_path(repo)
        if not (path / 'format').is_file():
            raise RuntimeError(f'{repo["name"]}: initialize the mirror or fix the master repository mount')
        if not master:
            if prop(repo, 'svn:sync-from-url') != repo.get('source_url', repo['master_url']):
                raise RuntimeError('Mirror source changed or not initialized')
            if prop(repo, 'svn:sync-from-uuid') != run(['svnlook', 'uuid', str(path)]):
                raise RuntimeError('Mirror UUID mismatch')
        proxy = '' if master else f'''SVNMasterURI "{repo['master_url']}"
        SVNMasterVersion {repo['master_version']}'''
        blocks.append(f'''<Location "{repo['path']}">
        DAV svn
        SVNPath "{path}"
        SVNAllowBulkUpdates Off
        {proxy}
        {auth}
    </Location>''')
        if not master:
            # Lock discovery uses PROPFIND and REPORT (including status/update).
            # Keep ALL metadata authoritative instead of polling/copying locks.
            # The master MUST disable bulk updates, so REPORT carries tree and
            # property metadata only; the client GETs file deltas locally.
            origin = urlsplit(repo['master_url'])
            # RewriteRule backrefs are URL-decoded: an asset named "#?.uasset"
            # would turn into a fragment/query. Take the encoded path from the
            # original HTTP request line, after matching the validated repo.
            metadata.append(f'''RewriteCond %{{REQUEST_METHOD}} ^(?:OPTIONS|PROPFIND|REPORT)$
    RewriteCond %{{THE_REQUEST}} "^[^ ]+ (/[^ ?]*)"
    RewriteRule "^{re.escape(repo['path'])}(/.*)?$" "{origin.scheme}://{origin.netloc}%1" [P,L,NE]
    ProxyPassReverse "{repo['path']}" "{repo['master_url']}"''')
            # Same URL path on a different port. All operations, including locks,
            # are authoritative on this port; no local lock replication is claimed.
            live.append(f'''ProxyPass "{repo['path']}" "{repo['master_url']}" nocanon
    ProxyPassReverse "{repo['path']}" "{repo['master_url']}"''')
    if master and automatic:
        blocks = [auto.master_locations(settings, auth)]
    header = '''ProxyRequests Off
SSLProxyEngine On
SSLProxyVerify require
SSLProxyVerifyDepth 5
SSLProxyCheckPeerName On
SSLProxyCACertificateFile /etc/ssl/certs/ca-certificates.crt
ProxyTimeout 21600
Timeout 21600
'''
    health = '''Alias /healthz /var/www/html/healthz
    <Location /healthz>
        Require all granted
    </Location>'''
    status_location = ''
    if not master:
        users = [user for user in re.split(r'[,\s]+', os.getenv('STATUS_USERS', 'svnmirror')) if user]
        if not users or any(not re.fullmatch(r'[A-Za-z0-9_.@-]+', user) for user in users):
            raise ValueError('STATUS_USERS must contain comma-separated SVN administrator logins')
        status_dir = DATA / 'status'
        status_dir.mkdir(parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(status_dir, UID, UID)
            status_dir.chmod(0o2770)
        header += 'ErrorLog "|/opt/svnmirror/log-errors"\n'
        status_location = f'''ScriptAlias /svn-mirror-status/api /opt/svnmirror/status.cgi
    Alias /svn-mirror-status/ /opt/svnmirror/web/
    RedirectMatch 302 ^/svn-mirror-status$ /svn-mirror-status/
    <Directory /opt/svnmirror>
        Options +ExecCGI -Indexes
        AllowOverride None
        Require all denied
    </Directory>
    <Directory /opt/svnmirror/web>
        Options -Indexes
        AllowOverride None
        DirectoryIndex index.html
        Require all granted
    </Directory>
    <Location /svn-mirror-status>
        AuthType Basic
        AuthName "SVN mirror administration"
        AuthzSendForbiddenOnFailure On
        AuthUserFile {auth_root}/htpasswd
        SetEnv SVNMIRROR_STATUS_USERS "{' '.join(users)}"
        SetEnv SVNMIRROR_AUTH_ROOT "{auth_root}"
        Header always set Cache-Control "no-store"
        Header always set X-Content-Type-Options "nosniff"
        <RequireAll>
            Require user {' '.join(users)}
            Require method GET HEAD
        </RequireAll>
    </Location>'''
    else:
        header += 'ErrorLog /proc/self/fd/2\n'
    document = header + '\n<VirtualHost *:80>\n    RewriteEngine On\n    ' + '\n    '.join(metadata) + '\n    ' + health + '\n    ' + status_location + '\n    ' + '\n    '.join(blocks) + '''
    CustomLog /proc/self/fd/1 combined
</VirtualHost>
'''
    if not master:
        document += '<VirtualHost *:81>\n    ' + '\n    '.join(live) + '''
    <Location />
        Require all granted
    </Location>
    CustomLog /proc/self/fd/1 combined
</VirtualHost>
'''
    Path('/var/www/html/healthz').write_text('apache ready\n')
    Path('/etc/apache2/sites-enabled/svnmirror.conf').write_text(document)
    run(['apache2ctl', '-t'])


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    if command == 'configure-mirror':
        return auto.configure_mirror()
    automatic = auto.settings().get('auto_discover', False)
    repos = config()
    if command in ('serve', 'render'):
        if command == 'serve':
            worker.clean_apache_runtime()
        if automatic and os.getenv('ROLE', 'mirror') != 'master':
            repos = auto.published()
        render(repos)
        if command == 'serve':
            if automatic and os.getenv('ROLE', 'mirror') != 'master':
                return auto.serve(sys.modules[__name__], repos)
            os.execvp('apache2ctl', ['apache2ctl', '-D', 'FOREGROUND'])
        return 0
    if command in ('status', 'health'):
        return max(status(repos, check=command == 'health'), auto.discovery_health() if automatic else 0)
    # Acquire and normalize an existing root-owned lock BEFORE dropping UID.
    # Never unlink it: another container might hold a flock on the same inode.
    prepare_worker(drop=False)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    with worker_lock():
        if command != 'adopt-all':
            drop_worker_identity()
        if automatic and command == 'discover':
            repos = auto.discover(sys.modules[__name__])
            print('Discovered repositories (no history downloaded):', flush=True)
            for repo in repos:
                print(repo['name'], flush=True)
            return 0
        if automatic and command == 'adopt-all':
            if sys.argv[2:] not in ([], ['--reverify'], ['--resume-conversion']):
                raise ValueError('Usage: adopt-all [--reverify | --resume-conversion]')
            return auto.adopt_all(sys.modules[__name__], repos,
                                  reverify=sys.argv[2:] == ['--reverify'],
                                  resume_conversion=sys.argv[2:] == ['--resume-conversion'])
        if automatic and command in ('init', 'sync-once', 'sync-loop', 'copy-revprops'):
            return auto.sync(sys.modules[__name__], command)
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
            with repository_lock(repo):
                recover_replication_lock(repo)
            print(repo['name'] + ': verified — replication lock is absent', flush=True)
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
    except worker.Stopped:
        telemetry.heartbeat(DATA, state='stopped', current_repository=None, phase=None)
        print('Worker stopped; committed revisions retained, next start will recover replication locks', flush=True)
        sys.exit(0)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
