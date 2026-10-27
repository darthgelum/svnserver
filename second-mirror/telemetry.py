"""Bounded local operational history. Never pass credentials or raw config."""
import json
from pathlib import Path
import threading
import time

_mutex = threading.Lock()


def heartbeat(data, **fields):
    path = Path(data) / 'status/worker.json'
    with _mutex:
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        state.update(fields)
        state['checked_at'] = time.time()
        state['features'] = ['auto-recovery', 'idle-watchdog', 'live-progress']
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state) + '\n')
        temporary.replace(path)


def record(data, message, repository=None, level='info'):
    row = {'time': time.time(), 'level': level, 'repository': repository,
           'message': str(message)[:3000]}
    path = Path(data) / 'status/events.jsonl'
    with _mutex:
        if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
            older = path.with_suffix('.jsonl.1')
            oldest = path.with_suffix('.jsonl.2')
            if older.exists():
                older.replace(oldest)
            path.replace(older)
        with path.open('a') as output:
            output.write(json.dumps(row, ensure_ascii=False) + '\n')


def tail(path, limit=200, maximum=256 * 1024):
    try:
        with Path(path).open('rb') as stream:
            stream.seek(0, 2)
            size = stream.tell()
            start = max(0, size - maximum)
            stream.seek(start)
            data = stream.read(maximum)
        lines = data.decode('utf-8', 'replace').splitlines()
        if start:
            lines = lines[1:]
        return lines[-limit:]
    except FileNotFoundError:
        return []
