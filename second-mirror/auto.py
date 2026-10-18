"""Automatic repository discovery, auth delivery and mirror publication."""
import base64
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

DATA = Path('/data')


def settings():
    return json.loads(Path('/config/repos.json').read_text())


def read_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value) + '\n')
    temporary.replace(path)


def valid_prefix(prefix):
    if not isinstance(prefix, str) or not re.fullmatch(r'/[A-Za-z0-9_-]+', prefix) or prefix.startswith('/svn-mirror-'):
        raise ValueError('public_prefix must be a single safe URL path, e.g. /svn')
    return prefix


def base_url(cfg):
    value = cfg['master_base_url'].rstrip('/')
    url = urlsplit(value)
    if (not url.hostname or url.username or url.password or url.path or url.query or url.fragment
            or re.search(r'[\s<>"\\]', value) or 'REPLACE' in value.upper()
            or url.scheme not in ('https', 'http')):
        raise ValueError('master_base_url must be an HTTP(S) origin without path or credentials')
    if url.scheme != 'https' and not cfg.get('allow_http_discovery', False):
        raise ValueError('Automatic auth delivery requires HTTPS; HTTP is only an explicit test override')
    return value


def repositories(cfg):
    if os.getenv('ROLE', 'mirror') == 'master':
        valid_prefix(cfg.get('public_prefix', '/svn'))
        if not re.fullmatch(r'[A-Za-z0-9_.@-]+', cfg.get('discovery_user', '')):
            raise ValueError('Set a safe discovery_user login in master.json')
        return []
    base_url(cfg)
    return read_json(DATA / 'status' / 'catalog.json', [])


def published():
    return read_json(DATA / 'status' / 'published.json', [])


def master_locations(cfg, auth):
    prefix = valid_prefix(cfg.get('public_prefix', '/svn'))
    user = cfg['discovery_user']
    if not re.fullmatch(r'[A-Za-z0-9_.@-]+', user):
        raise ValueError('Invalid discovery_user')
    # The replication alias provides full history to ONE dedicated account, with
    # writes rejected by method. Normal clients keep the original USVN authz.
    return f'''<Location "{prefix}">
        DAV svn
        SVNParentPath /repositories
        SVNListParentPath Off
        {auth}
    </Location>
    <Location /svn-mirror-source>
        DAV svn
        SVNParentPath /repositories
        SVNListParentPath Off
        AuthType Basic
        AuthName "SVN mirror replication"
        AuthUserFile /auth/htpasswd
        <RequireAll>
            Require user {user}
            Require method GET HEAD OPTIONS PROPFIND REPORT
        </RequireAll>
    </Location>
    ScriptAlias /svn-mirror-meta /opt/svnmirror/discovery.cgi
    <Directory /opt/svnmirror>
        Options +ExecCGI -Indexes
        AllowOverride None
        Require all denied
    </Directory>
    <Location /svn-mirror-meta>
        AuthType Basic
        AuthName "SVN mirror replication"
        AuthUserFile /auth/htpasswd
        <RequireAll>
            Require user {user}
            Require method GET HEAD
        </RequireAll>
    </Location>'''


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover(app):
    cfg = settings()
    origin = base_url(cfg)
    credentials = app.SECRETS['default']
    token = base64.b64encode((credentials['username'] + ':' + credentials['password']).encode()).decode()
    request = urllib.request.Request(origin + '/svn-mirror-meta', headers={'Authorization': 'Basic ' + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=30) as response:
        raw = response.read(16 * 1024 * 1024 + 1)
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError('Discovery response too large')
    document = json.loads(raw)
    version = document['master_version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid discovered master_version')
    repos, names, prefix = [], set(), None
    if not isinstance(document['repositories'], list):
        raise ValueError('Invalid repository catalog')
    for item in document['repositories']:
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name) or name in names:
            raise ValueError('Invalid or duplicate discovered repository name')
        names.add(name)
        path = item['path']
        this_prefix = path.rsplit('/', 1)[0]
        valid_prefix(this_prefix)
        if path != this_prefix + '/' + name or (prefix is not None and prefix != this_prefix):
            raise ValueError('Inconsistent repository path')
        prefix = this_prefix
        if item['source_path'] != '/svn-mirror-source/' + name:
            raise ValueError('Invalid replication source path')
        repos.append({'name': name, 'path': path, 'master_url': origin + path,
                      'source_url': origin + item['source_path'], 'master_version': version})
    if cfg.get('sync_auth', False):
        auth = document.get('auth')
        if not isinstance(auth, dict) or not all(isinstance(auth.get(n), str) and auth[n].strip() for n in ('htpasswd', 'authz')):
            raise ValueError('Master must enable export_auth to synchronize access rights')
        root = DATA / 'auth'
        current = root / 'current'
        same = current.exists() and all((current / name).read_text() == auth[name] for name in ('htpasswd', 'authz'))
        if not same:
            generation = root / ('generation-' + str(time.time_ns()))
            generation.mkdir(mode=0o750)
            for name in ('htpasswd', 'authz'):
                (generation / name).write_text(auth[name])
                (generation / name).chmod(0o640)
            app.run(['svnauthz', 'validate', str(generation / 'authz')])
            app.run(['htpasswd', '-vb', str(generation / 'htpasswd'), credentials['username'], credentials['password']])
            temporary = root / 'current.next'
            if temporary.is_symlink():
                temporary.unlink()
            temporary.symlink_to(generation.name, target_is_directory=True)
            temporary.replace(current)
            # Keep the two most recent immutable generations for diagnosis/rollback.
            import shutil
            for obsolete in sorted(root.glob('generation-*'))[:-2]:
                shutil.rmtree(obsolete)
    atomic_json(DATA / 'status' / 'catalog.json', repos)
    atomic_json(DATA / 'status' / 'discovery.json', {'ok': True, 'checked_at': time.time(), 'repositories': len(repos)})
    return repos


def discovery_health():
    try:
        state = read_json(DATA / 'status' / 'discovery.json', {})
        return 0 if (state.get('ok') and time.time() - state['checked_at'] <= int(os.getenv('MAX_SYNC_AGE_SECONDS', '300'))) else 1
    except (ValueError, OSError, KeyError):
        return 1


def adoption_identity():
    return read_json(DATA / 'repositories' / '.svn-mirror-adoption.json', {})


def adopt_all(app, repos):
    """Explicit, offline conversion of a local seed. No repository data transfer."""
    if os.getenv('ROLE', 'mirror') == 'master' or os.geteuid() != 0:
        raise RuntimeError('adopt-all must run as root in the Yerevan sync container')
    if not repos:
        raise RuntimeError('Run discover first; it downloads only the catalog and access rules')
    existing = [repo for repo in repos if app.repo_path(repo).exists()]
    if not existing:
        raise RuntimeError('No matching local repositories. Check EXISTING_REPOS; refusing an empty seed')
    pending = []
    # Validate ALL selected copies before changing ownership, hooks or svnsync metadata.
    for repo in existing:
        path = app.repo_path(repo)
        if path.is_symlink() or not (path / 'format').is_file() or not (path / 'db').is_dir():
            raise RuntimeError(repo['name'] + ': not a regular repository directory')
        source_uuid, head = app.source_info(repo)
        if app.run(['svnlook', 'uuid', str(path)]) != source_uuid:
            raise RuntimeError(repo['name'] + ': UUID differs from master; refusing adoption')
        if int(app.run(['svnlook', 'youngest', str(path)])) > head:
            raise RuntimeError(repo['name'] + ': local history is ahead of master; refusing adoption')
        initialized = app.prop(repo, 'svn:sync-from-url')
        if initialized and initialized != repo.get('source_url', repo['master_url']):
            raise RuntimeError(repo['name'] + ': already mirrors a different source')
        for folder, dirs, files in os.walk(path, followlinks=False):
            if any((Path(folder) / name).is_symlink() for name in dirs + files):
                raise RuntimeError(repo['name'] + ': seed contains symlinks; inspect before adoption')
        print(repo['name'] + ': verifying local history (no history download)', flush=True)
        app.run(['svnadmin', 'verify', str(path)])
        pending.append(repo)
    for repo in pending:
        path = app.repo_path(repo)
        # Only the explicitly selected Yerevan copies; never the master or a parent tree.
        for folder, dirs, files in os.walk(path, followlinks=False):
            directory = Path(folder)
            os.chown(directory, app.UID, app.UID)
            directory.chmod((directory.stat().st_mode & 0o777) | 0o2070)
            for name in files:
                child = directory / name
                os.chown(child, app.UID, app.UID)
                child.chmod((child.stat().st_mode & 0o777) | 0o060)
    app.drop_worker_identity()
    for repo in pending:
        app.initialize(repo, seed=True, verified_seed=True)
        print(repo['name'] + ': adopted at r' + app.run(['svnlook', 'youngest', str(app.repo_path(repo))]), flush=True)
    import uuid
    identity = {'id': str(uuid.uuid4())}
    atomic_json(DATA / 'repositories' / '.svn-mirror-adoption.json', identity)
    atomic_json(DATA / 'status' / 'adoption.json', identity)
    print(f'Adopted {len(pending)} local repositories; history was not downloaded. Start sync to fetch newer revisions.', flush=True)
    return 0


def sync(app, command):
    if os.getenv('REQUIRE_SEED_ADOPTION') == '1':
        marker = read_json(DATA / 'status' / 'adoption.json', {})
        if not marker or marker != adoption_identity():
            raise RuntimeError('Existing-copy mode requires discover then adopt-all before synchronization')
    while True:
        success = True
        try:
            repos = discover(app)
            print(f'Discovered {len(repos)} repositories', flush=True)
        except Exception as exc:
            # Do not include server response content, auth header or credentials.
            message = 'Discovery failed: ' + type(exc).__name__
            print(message, flush=True)
            atomic_json(DATA / 'status' / 'discovery.json', {'ok': False, 'checked_at': time.time(), 'error': message})
            repos = repositories(settings())
            success = False
        ready = {repo['name']: repo for repo in published() if any(r == repo for r in repos)}
        atomic_json(DATA / 'status' / 'published.json', list(ready.values()))
        for repo in repos:
            try:
                path = app.repo_path(repo)
                if not path.exists() or app.prop(repo, 'svn:sync-from-url') is None:
                    app.initialize(repo)
                old = read_json(DATA / 'status' / (repo['name'] + '.json'), {})
                rescan = time.time() - old.get('revprops_checked_at', 0) >= int(os.getenv('REVPROP_RESCAN_SECONDS', '3600'))
                ok = app.sync_one(repo, full_props=rescan or command == 'copy-revprops')
                if ok:
                    ready[repo['name']] = repo
                    # Publish each completed mirror immediately, even when another
                    # repository's first history transfer will take hours.
                    atomic_json(DATA / 'status' / 'published.json', list(ready.values()))
                success = ok and success
            except Exception as exc:
                message = str(exc)
                for auth in app.SECRETS.values():
                    if auth.get('password'):
                        message = message.replace(auth['password'], '[redacted]')
                print(repo['name'] + ': ' + message, flush=True)
                atomic_json(DATA / 'status' / (repo['name'] + '.json'), {'ok': False, 'checked_at': time.time(), 'error': message})
                success = False
        if command != 'sync-loop':
            return 0 if success else 1
        time.sleep(max(1, int(os.getenv('SYNC_INTERVAL_SECONDS', '30'))))


def serve(app, initial):
    process = subprocess.Popen(['apache2ctl', '-D', 'FOREGROUND'])
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
        process.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    previous = initial
    try:
        while not stopped:
            if process.poll() is not None:
                raise RuntimeError('Apache exited unexpectedly')
            current = published()
            if current != previous:
                app.render(current)
                app.run(['apache2ctl', '-k', 'graceful'])
                previous = current
                print(f'Published {len(current)} mirrored repositories', flush=True)
            time.sleep(2)
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def configure_mirror():
    """Interactive helper: password stays on the NAS, never in shell argv/history."""
    root = Path('/setup')
    outputs = [root / '.env', root / 'config/repos.json', root / 'secrets/source.json']
    if any(path.exists() for path in outputs):
        raise RuntimeError('Existing .env/repos.json/source.json found; refusing to overwrite configuration')
    origin = input('Master HTTPS origin (https://host:port): ').strip().rstrip('/')
    base_url({'master_base_url': origin})
    username = input('Replication SVN login [svnmirror]: ').strip() or 'svnmirror'
    password = getpass.getpass('Replication SVN password: ')
    if not password:
        raise ValueError('Password cannot be empty')
    bind = str(ipaddress.IPv4Address(input('Yerevan NAS LAN IPv4 for clients: ').strip()))
    data = input('Absolute NAS data path [/volume1/docker/svn-second-mirror-data]: ').strip() or '/volume1/docker/svn-second-mirror-data'
    if not data.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+', data) or '..' in Path(data).parts:
        raise ValueError('Use an absolute data path without spaces or ..')
    existing = input('Existing repository parent path (blank ONLY for a new empty mirror): ').strip()
    if existing and (not existing.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]+', existing)
                     or '..' in Path(existing).parts):
        raise ValueError('Use an absolute existing repository parent path without spaces or ..')
    for name in ('config', 'secrets'):
        (root / name).mkdir(exist_ok=True)
    (root / 'secrets').chmod(0o700)
    environment = f'MIRROR_DATA={data}\nBIND_IP={bind}\nMIRROR_PORT=18080\nLIVE_PORT=18081\nSYNC_INTERVAL_SECONDS=30\nMAX_SYNC_AGE_SECONDS=1800\nREVPROP_RESCAN_SECONDS=86400\n'
    if existing:
        environment += 'EXISTING_REPOS=' + existing + '\n'
    outputs[0].write_text(environment)
    outputs[0].chmod(0o600)
    outputs[1].write_text(json.dumps({'auto_discover': True, 'master_base_url': origin, 'sync_auth': True}, indent=2) + '\n')
    outputs[2].write_text(json.dumps({'default': {'username': username, 'password': password}}, indent=2) + '\n')
    outputs[2].chmod(0o600)
    if existing:
        print('Configuration saved for EXISTING COPY. Use both compose.auto.yaml and compose.existing.yaml.', flush=True)
        print('Before starting services: run sync discover, then sync adopt-all. No history is downloaded by these commands.', flush=True)
    else:
        print('Configuration saved. Start: docker compose -f compose.auto.yaml up -d', flush=True)
