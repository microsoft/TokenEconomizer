"""JSON state on disk, written atomically and serialized across hook processes.

Two Claude Code sessions in one repository run their hooks concurrently, and a session's
background fold can overlap the next session's start. Every read-modify-write of shared state
therefore happens under an exclusive lock on a sibling lock file, and every write lands through a
temporary file and `os.replace`, so a reader never sees half a document. Per-session documents are
written by one session only and need no lock.

Locking uses `fcntl.flock`, and `msvcrt.locking` on Windows. Where neither is available the lock
degrades to a no-op rather than failing the hook: a lost update is recoverable, a crashed hook is
not.

`os.replace` is atomic on POSIX even while another process reads the target. On Windows it is
not: CPython opens a file for reading without `FILE_SHARE_DELETE`, so replacing one that a
concurrent hook or a `te status` has open fails with `PermissionError` (WinError 5). Those
readers are short-lived, so the replace is retried briefly rather than lost -- a repository with
two opencode sessions and a replay (`te import`) running concurrently hit this repeatedly.
"""
import contextlib
import json
import os
import time

# How long a write waits for a reader to let go of the target: a handful of tries over ~1.3s.
REPLACE_TRIES = 8
REPLACE_FIRST_WAIT_S = 0.01
REPLACE_MAX_WAIT_S = 0.4

try:
    import fcntl
except ImportError:          # pragma: no cover - Windows
    fcntl = None

try:
    import msvcrt
except ImportError:          # pragma: no cover - POSIX
    msvcrt = None


def _try_lock(fh):
    """Take an exclusive lock on `fh` without blocking; raise OSError when it is held."""
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(fh):
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    else:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def _replace(tmp, target):
    """`os.replace`, retried while a concurrent reader holds the target (Windows only: see the
    module). The last failure is raised, so a real permission problem still surfaces."""
    wait = REPLACE_FIRST_WAIT_S
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(wait)
            wait = min(wait * 2, REPLACE_MAX_WAIT_S)


class Store:
    """One directory of JSON documents and append-only JSONL logs."""

    def __init__(self, base):
        self.base = os.path.abspath(base)

    def path(self, *parts):
        return os.path.join(self.base, *parts)

    def ensure(self, *parts):
        d = self.path(*parts)
        os.makedirs(d, exist_ok=True)
        return d

    def read(self, name, default=None):
        try:
            with open(self.path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return default

    def write(self, name, obj):
        target = self.path(name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = f"{target}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2, sort_keys=True)
            _replace(tmp, target)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        return target

    def append(self, name, record):
        target = self.path(name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")

    def read_lines(self, name, limit=None):
        """Records of a JSONL log, oldest first; the last `limit` when given."""
        out = []
        try:
            with open(self.path(name), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return out[-limit:] if limit else out

    @contextlib.contextmanager
    def locked(self, name="state", timeout=10.0):
        """Exclusive lock over `name` for the duration of the block; yields whether it was taken.

        Polls a non-blocking flock rather than blocking outright, so a stuck holder costs at most
        `timeout` seconds -- a hook must never hang the agent, and a hook's caller proceeds
        either way. A caller that must not run unlocked (the fold) checks the yielded flag."""
        os.makedirs(self.base, exist_ok=True)
        if fcntl is None and msvcrt is None:
            yield True
            return
        fh = open(self.path(f".{name}.lock"), "a+", encoding="utf-8")
        got = False
        deadline = time.time() + timeout
        try:
            while True:
                try:
                    _try_lock(fh)
                    got = True
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.05)
            yield got
        finally:
            if got:
                _unlock(fh)
            fh.close()
