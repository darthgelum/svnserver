#!/usr/bin/python3
"""Apache RewriteMap: serve immutable revisions locally once they exist."""
from pathlib import Path
import re
import sys


def route(key, data=Path('/data')):
    match = re.fullmatch(r'([A-Za-z0-9_-]+)/([0-9]{1,20})', key)
    if not match:
        return 'NULL'
    try:
        with (data / 'repositories' / match[1] / 'db/current').open('rb') as handle:
            head = int(handle.read(128).split()[0])
        return 'master' if int(match[2]) > head else 'local'
    except (OSError, ValueError, IndexError):
        # The authoritative server still enforces the requesting user's ACLs.
        return 'master'


if __name__ == '__main__':
    for line in sys.stdin:
        print(route(line.rstrip('\r\n')), flush=True)
