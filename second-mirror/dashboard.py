"""Read-only, allowlisted operational snapshot. No credentials leave the NAS."""
import configparser
import json
import os
from pathlib import Path
import re
import shutil
import time
import telemetry


def read(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def number(value):
    try:
        result = int(value)
        return result if result >= 0 else None
    except (TypeError, ValueError):
        return None


def accounts(auth):
    try:
        names = sorted({line.split(':', 1)[0] for line in (auth / 'htpasswd').read_text().splitlines()
                        if ':' in line and not line.startswith('#')})
        parser = configparser.ConfigParser(interpolation=None, strict=False, delimiters=('=',))
        parser.optionxform = str
        parser.read_string((auth / 'authz').read_text())
    except (FileNotFoundError, configparser.Error):
        return [], {}
    groups = {name: [member.strip() for member in value.split(',') if member.strip()]
              for name, value in parser.items('groups')} if parser.has_section('groups') else {}
    aliases = dict(parser.items('aliases')) if parser.has_section('aliases') else {}

    def includes(group, login, visited):
        if group in visited:
            return False
        visited = visited | {group}
        return any(member == login or (member.startswith('&') and aliases.get(member[1:]) == login)
                   or (member.startswith('@') and includes(member[1:], login, visited))
                   for member in groups.get(group, []))

    return [{'login': name, 'groups': [group for group in groups if includes(group, name, set())]}
            for name in names], groups


def snapshot(data, auth, config, viewer):
    status = data / 'status'
    now = time.time()
    discovery = read(status / 'discovery.json', {})
    connection = {key: discovery.get(key) for key in ('ok', 'checked_at', 'last_success_at', 'error')}
    connection['stale'] = now - (discovery.get('checked_at') or 0) > 120
    heads = read(status / 'master-heads.json', {})
    published = {r['name'] for r in read(status / 'published.json', [])}
    catalog = read(status / 'catalog.json', config.get('repositories', []))
    repos = []
    for repo in catalog:
        name = repo.get('name', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            continue
        state = read(status / (name + '.json'), {})
        progress = read(status / (name + '-progress.json'), {})
        fresh_progress = progress.get('state') == 'running' and now - progress.get('checked_at', 0) < 90
        local = number(progress.get('local_revision')) if fresh_progress else number(state.get('local_revision'))
        # Reading the tiny, atomically replaced FSFS current pointer is enough
        # for a physical HEAD sample, without running 11 CLI processes per poll.
        try:
            with (data / 'repositories' / name / 'db/current').open('rb') as stream:
                current = stream.read(128).split()[0]
            local = int(current) if current.isdigit() else local
        except (OSError, IndexError):
            pass
        merged = number(progress.get('last_merged_revision')) if fresh_progress else number(state.get('last_merged_revision'))
        head = number(heads.get('revisions', {}).get(name))
        head_checked = heads.get('checked_at')
        if head is None:
            head = number(progress.get('master_revision') if fresh_progress else state.get('master_revision'))
            head_checked = progress.get('started_at') if fresh_progress else state.get('checked_at')
        lag = max(0, head - merged) if head is not None and merged is not None else None
        phase = ('syncing' if fresh_progress else 'error' if state.get('ok') is False
                 else 'ready' if state.get('ok') and lag == 0 and name in published
                 else 'waiting')
        repos.append({'name': name, 'path': repo['path'], 'master_url': repo['master_url'],
                      'published': name in published, 'state': phase, 'local_revision': local,
                      'last_merged_revision': merged, 'master_revision': head,
                      'master_checked_at': head_checked, 'lag': lag,
                      'last_success_at': state.get('last_success_at') or (state.get('checked_at') if state.get('ok') else None),
                      'checked_at': state.get('checked_at'), 'error': state.get('error'),
                      'progress': {key: progress.get(key) for key in
                          ('state', 'phase', 'checked_at', 'elapsed_seconds', 'idle_seconds',
                           'copying_revision', 'transaction_bytes')},
                      'recovery': read(status / (name + '-last-recovery.json'), {}).get('checked_at')})
    users, groups = accounts(auth)
    events = []
    for suffix in ('.1', ''):
        for line in telemetry.tail(status / ('events.jsonl' + suffix), limit=160):
            try:
                row = json.loads(line)
                events.append({key: row.get(key) for key in ('time', 'level', 'repository', 'message')})
            except ValueError:
                pass
    disk = shutil.disk_usage(data / 'repositories' if (data / 'repositories').is_dir() else data)
    return {'mode': 'live', 'generated_at': now, 'viewer': viewer, 'label': 'Second mirror',
            'master': config.get('master_base_url'), 'connection': connection,
            'worker': read(status / 'worker.json', {}), 'repositories': repos,
            'users': users, 'groups': groups, 'auth_checked_at': discovery.get('last_success_at'),
            'events': events[-160:], 'apache_errors': telemetry.tail(status / 'apache-errors.log', limit=80),
            'disk': {'total': disk.total, 'free': disk.free, 'used': disk.used}}


def respond():
    allowed = os.environ.get('SVNMIRROR_STATUS_USERS', '').split()
    viewer = os.environ.get('REMOTE_USER', '')
    if not viewer or viewer not in allowed:
        return '403 Forbidden', {'error': 'Administrator access required'}
    if os.environ.get('REQUEST_METHOD') not in ('GET', 'HEAD'):
        return '405 Method Not Allowed', {'error': 'Read-only endpoint'}
    try:
        config = read('/config/repos.json', {})
        result = snapshot(Path('/data'), Path(os.environ['SVNMIRROR_AUTH_ROOT']), config, viewer)
        return '200 OK', result
    except Exception as exc:
        # Exception details can contain auth-file contents: do not return them.
        return '503 Service Unavailable', {'error': 'Status temporarily unavailable', 'type': type(exc).__name__}
