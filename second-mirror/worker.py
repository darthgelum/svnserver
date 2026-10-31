"""Exclusive mirror maintenance and observable, cancellable SVN transfers."""
import contextlib
from collections import deque
import fcntl
import os
from pathlib import Path
import queue
import signal
import stat
import subprocess
import threading
import time

_held = {}


def clean_apache_runtime(runtime=Path('/var/run/apache2'), proc=Path('/proc')):
    """A restarted container can retain a dead CGI socket/PID file.

    Only remove these named runtime artifacts when no Apache process is alive
    in this container's PID namespace. Never touch repository/worker locks.
    """
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            if (process / 'comm').read_text().strip() == 'apache2':
                raise RuntimeError('Apache is already running; refusing runtime cleanup')
        except FileNotFoundError:
            pass
    if not runtime.is_dir():
        return
    pid = runtime / 'apache2.pid'
    if pid.is_file() and not pid.is_symlink():
        pid.unlink()
    for path in runtime.glob('cgisock.*'):
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


class Stopped(BaseException):
    """Not a repository failure: unwind the worker instead of retrying on stop."""


def stop(signum, frame):
    raise Stopped()


def descriptors():
    return tuple(_held.values())


def require_lock(path):
    if str(Path(path).resolve()) not in _held:
        raise RuntimeError('Exclusive worker/repository lock is required for recovery')


@contextlib.contextmanager
def exclusive(path, message, owner=None):
    key = str(Path(path).resolve())
    if key in _held:
        raise RuntimeError('Lock already held by this process: ' + str(path))
    with Path(path).open('a') as handle:
        if owner is not None and os.geteuid() == 0:
            os.fchown(handle.fileno(), owner, owner)
            os.fchmod(handle.fileno(), 0o660)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError(message) from None
        _held[key] = handle.fileno()
        try:
            yield
        finally:
            del _held[key]
            # Close, never LOCK_UN: an orphaned child may still hold the same
            # open-file description. Its inherited descriptor protects the repo.


def activity(process, repository):
    """Cheap activity evidence, without walking/reading historical assets."""
    cpu, io = (), ()
    proc = Path('/proc') / str(process.pid)
    try:
        fields = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
        cpu = tuple(fields[11:13])  # utime/stime, excluding the variable comm field
        counters = dict(line.split(':', 1) for line in (proc / 'io').read_text().splitlines())
        io = tuple(counters.get(k, '').strip() for k in ('rchar', 'wchar', 'read_bytes', 'write_bytes'))
    except (OSError, IndexError):
        pass  # Process may have just exited.
    # Only the current writer's transaction belongs in live progress. Failed
    # replays can leave older transactions containing gigabytes of stale data.
    active = set()
    try:
        for descriptor in (proc / 'fd').iterdir():
            try:
                path = Path(os.readlink(descriptor))
                if path.parent == Path(repository) / 'db/txn-protorevs' and path.name.endswith('.rev'):
                    active.add(path.name[:-4])
            except OSError:
                pass
    except OSError:
        pass
    transactions = []
    for parent in ('transactions', 'txn-protorevs'):
        root = Path(repository) / 'db' / parent
        if not root.exists():
            continue
        for folder, dirs, files in os.walk(root, followlinks=False):
            if Path(folder) == root:
                dirs[:] = [name for name in dirs if name.endswith('.txn') and name[:-4] in active]
            for name in sorted(files):
                if parent == 'txn-protorevs' and name not in {item + '.rev' for item in active}:
                    continue
                path = Path(folder) / name
                try:
                    stat = path.stat()
                    transactions.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
                except FileNotFoundError:
                    pass  # A revision committed while we were sampling.
    return cpu, io, tuple(transactions)


def terminate(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def transfer(args, repository, report, emit, redact, stop_when=None):
    """Keep completed revisions; abort only idle transfers or an explicit cap.

    SVN stdout/stderr is drained continuously. All held flocks are inherited by
    the actual writer, so killing Python cannot authorize a second writer.
    """
    idle = max(0, int(os.getenv('SYNC_IDLE_TIMEOUT_SECONDS', '3600')))
    cap = max(0, int(os.getenv('SYNC_TRANSFER_TIMEOUT_SECONDS', '0')))
    interval = max(1, int(os.getenv('SYNC_PROGRESS_SECONDS', '30')))
    started = last_activity = last_report = time.monotonic()
    previous = None
    lines = queue.Queue(maxsize=256)
    tail = deque(maxlen=30)
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace',
                               start_new_session=True, pass_fds=descriptors(),
                               env={**os.environ, 'LC_ALL': 'C.UTF-8'})

    def drain():
        try:
            for line in iter(lambda: process.stdout.readline(4096), ''):
                lines.put(line.rstrip())
        finally:
            process.stdout.close()
            lines.put(None)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    eof = False
    try:
        while not eof or process.poll() is None:
            try:
                line = lines.get(timeout=1)
                if line is None:
                    eof = True
                else:
                    last_activity = time.monotonic()
                    safe = redact(line)
                    tail.append(safe)
                    emit(safe)
                    if stop_when is not None and stop_when(safe):
                        # svnsync has no revision-range option. Yield only after
                        # its completed revision-property checkpoint is reported.
                        # Reap the writer before releasing either maintenance lock.
                        terminate(process)
                        report(process.pid, int(time.monotonic() - started), 0,
                               activity(process, repository))
                        emit('Revision batch completed; yielding to the next repository')
                        return
            except queue.Empty:
                pass
            now = time.monotonic()
            if previous is None or now - last_report >= interval:
                current = activity(process, repository)
                # A disconnected serf client can spin at 100% CPU indefinitely.
                # CPU usage alone is not evidence that a replay is progressing.
                if previous is None or current[1:] != previous[1:]:
                    last_activity = now
                previous, last_report = current, now
                report(process.pid, int(now - started), int(now - last_activity), current)
            if cap and now - started >= cap:
                raise RuntimeError('svnsync reached SYNC_TRANSFER_TIMEOUT_SECONDS; retry will resume committed revisions')
            if idle and now - last_activity >= idle:
                raise RuntimeError('svnsync has no activity for SYNC_IDLE_TIMEOUT_SECONDS; retry will recover its replication lock')
        process.wait()
        if process.returncode:
            raise RuntimeError('svnsync failed: ' + '\n'.join(tail))
    except BaseException:
        terminate(process)
        raise
    finally:
        reader.join(timeout=2)
