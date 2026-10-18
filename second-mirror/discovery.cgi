#!/usr/bin/python3
"""Authenticated control endpoint. Apache restricts this to discovery_user."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

stage = 'read-config'
try:
    settings = json.loads(Path('/config/repos.json').read_text())
    stage = 'check-user'
    if not settings.get('auto_discover') or os.environ.get('REMOTE_USER') != settings['discovery_user']:
        raise PermissionError('Not authorized')
    prefix = settings.get('public_prefix', '/svn')
    repos = []
    stage = 'list-repositories'
    for path in sorted(Path('/repositories').iterdir()):
        if (path.is_dir() and not path.is_symlink()
                and re.fullmatch(r'[A-Za-z0-9_-]+', path.name)
                and (path / 'format').is_file() and (path / 'db').is_dir()):
            repos.append({'name': path.name, 'path': prefix + '/' + path.name,
                          'source_path': '/svn-mirror-source/' + path.name})
    stage = 'read-svn-version'
    response = {'repositories': repos,
                'lock_metadata_mode': 'master-skelta-v1',
                'master_version': subprocess.check_output(['svn', '--version', '--quiet'], text=True).strip()}
    if settings.get('export_auth', False):
        response['auth'] = {}
        for name in ('htpasswd', 'authz'):
            stage = 'read-' + name
            response['auth'][name] = (Path('/auth') / name).read_text()
    stage = 'write-response'
    print('Content-Type: application/json\r\nCache-Control: no-store\r\n\r\n' + json.dumps(response))
except Exception as error:
    # Do not expose paths, auth content or traceback to a caller.
    # Log only the stage and exception type: UnicodeDecodeError and JSON errors
    # can include raw file contents in their messages, including password hashes.
    print('SVN discovery failed: stage=' + stage + ' type=' + type(error).__name__
          + ' errno=' + str(getattr(error, 'errno', None)), file=sys.stderr, flush=True)
    print('Status: 503 Service Unavailable\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\n\r\nDiscovery unavailable')
