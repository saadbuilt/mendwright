"""A store's objects as files in a folder on this machine: the StoreBackend contract (see
mendwright.backends) for a local disk, for every process of one host.

- A name is a path relative to the root folder, with '/' between its parts, such as
  'keys/0a1b...json'. Names that leave the folder ('..', an absolute path, a drive) are
  refused with ValueError.
- A version is the file's inode, mtime in nanoseconds and size, read from the same open handle
  as the body (os.fstat), so a body always goes with its own version: a write replaces the
  file (a new inode), and a handle keeps the file it opened. get(if_none_match=v) only stats
  the file when it has not changed, so polling it is cheap.
- put() and delete() hold the lock file next to the object (the one SelectorStore takes for
  overrides.json, see mendwright.store._locked: '<object>.lock', made with O_EXCL, stale after
  lock_stale_after seconds) while they compare versions, and put() writes a temp file and
  os.replace()s it in, so a reader sees the old body or the new one, never a mix.
  create_only=True writes only when the object does not exist, as O_EXCL does: every writer
  of an object takes its lock, so two that race cannot both create it.
- A lock that stays held past lock_timeout seconds, or a disk that keeps refusing a read or a
  write, raises Unavailable; a permission the OS refuses raises Denied. No write is retried
  after a Conflict: the caller merges and tries again. Windows refuses a replace while any
  reader holds the file, so put() tries it again, every 20 ms at most, for up to lock_timeout
  seconds.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
from pathlib import Path, PurePosixPath

from . import Conflict, Denied, Read, Unavailable

__all__ = ['FileBackend']

# How often a read that Windows refuses for a moment (a writer is replacing the file) is tried,
# as SelectorStore does.
_TRIES = 8


def _version(st):
    return '%d-%d-%d' % (st.st_ino, st.st_mtime_ns, st.st_size)


class FileBackend:
    """Objects as files under the folder root (see the module docstring)."""

    # Seconds put() and delete() wait for another writer's lock, and the age after which a
    # leftover lock file is taken for an abandoned one.
    lock_timeout = 5.0
    lock_stale_after = 10.0

    def __init__(self, root, *, sleep=time.sleep):
        self.root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
        self._sleep = sleep

    def describe(self):
        return 'the folder ' + str(self.root)

    def path(self, name):
        """The file of the object name. ValueError for a name that leaves the root."""
        if not isinstance(name, str) or not name or '\\' in name or ':' in name:
            raise ValueError('mendwright: a store object name is a relative path with /, got '
                             + repr(name))
        parts = name.split('/')
        if PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') for p in parts):
            # Outside the folder, or a second spelling of an object ('keys//a.json').
            raise ValueError('mendwright: a store object name is a relative path inside its '
                             'folder, one name per object, got ' + repr(name))
        return self.root.joinpath(*parts)

    # --- reading ------------------------------------------------------------------------------

    def get(self, name, *, if_none_match=None):
        path = self.path(name)
        if if_none_match is not None:
            try:
                if _version(os.stat(path)) == if_none_match:
                    return Read(None, if_none_match, True)
            except (FileNotFoundError, NotADirectoryError):
                return Read(None, None, False)
            except OSError as exc:
                raise Unavailable(self._said('read', name, exc)) from None
        delay = 0.01
        for attempt in range(_TRIES):
            try:
                with open(path, 'rb') as fh:
                    version = _version(os.fstat(fh.fileno()))
                    body = fh.read()
                break
            except (FileNotFoundError, NotADirectoryError):
                return Read(None, None, False)
            except PermissionError as exc:
                if attempt == _TRIES - 1:
                    raise Unavailable(self._said('read', name, exc)) from None
                self._sleep(delay)
                delay = min(delay * 2, 0.2)
            except OSError as exc:
                raise Unavailable(self._said('read', name, exc)) from None
        if if_none_match is not None and version == if_none_match:
            return Read(None, version, True)
        return Read(body, version, False)

    def _current(self, path):
        try:
            return _version(os.stat(path))
        except (FileNotFoundError, NotADirectoryError):
            return None

    # --- writing ------------------------------------------------------------------------------

    def put(self, name, body, *, if_match=None, create_only=False):
        if if_match is not None and create_only:
            raise ValueError('put() takes if_match= or create_only=True, not both')
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError('put() takes the body as bytes')
        path = self.path(name)
        with self._locked(name, path):
            current = self._current(path)
            if create_only and current is not None:
                raise Conflict(name + ' already exists')
            if if_match is not None and current != if_match:
                raise Conflict(name + ' changed since version ' + if_match)
            self._replace(name, path, bytes(body))
            version = self._current(path)
        if version is None:
            raise Unavailable(self.describe() + ': ' + name + ' was gone right after it was '
                              'written')
        return version

    def delete(self, name, *, if_match=None):
        path = self.path(name)
        with self._locked(name, path):
            current = self._current(path)
            if current is None:
                return
            if if_match is not None and current != if_match:
                raise Conflict(name + ' changed since version ' + if_match)
            try:
                os.unlink(path)
            except FileNotFoundError:
                return
            except PermissionError as exc:
                raise Denied(self._said('delete', name, exc)) from None
            except OSError as exc:
                raise Unavailable(self._said('delete', name, exc)) from None

    @contextlib.contextmanager
    def _locked(self, name, path):
        """Hold the lock file of the object at path (see store._locked) for the block."""
        from ..store import _locked
        held = _locked(path, timeout=self.lock_timeout, stale_after=self.lock_stale_after,
                       sleep=self._sleep)
        try:
            held.__enter__()
        except TimeoutError:
            raise Unavailable(self.describe() + ': the lock of ' + name + ' is held by '
                              'another writer') from None
        except PermissionError as exc:
            raise Denied(self._said('lock', name, exc)) from None
        except OSError as exc:
            raise Unavailable(self._said('lock', name, exc)) from None
        try:
            yield
        finally:
            held.__exit__(None, None, None)

    def _replace(self, name, path, body):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp',
                                       dir=str(path.parent))
        except PermissionError as exc:
            raise Denied(self._said('write', name, exc)) from None
        except OSError as exc:
            raise Unavailable(self._said('write', name, exc)) from None
        try:
            with os.fdopen(fd, 'wb') as fh:
                fh.write(body)
            # On Windows the replace is refused while any reader holds the file, and readers
            # that keep reading leave only short gaps: try often, for up to lock_timeout
            # seconds of sleep in all.
            waited, delay = 0.0, 0.001
            while True:
                try:
                    os.replace(tmp, str(path))
                    return
                except PermissionError as exc:
                    if waited >= self.lock_timeout:
                        raise Unavailable(self._said('write', name, exc)) from None
                    self._sleep(delay)
                    waited += delay
                    delay = min(delay * 2, 0.02)
        except OSError as exc:
            raise Unavailable(self._said('write', name, exc)) from None
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _said(self, what, name, exc):
        return (self.describe() + ': could not ' + what + ' ' + name + ' ('
                + type(exc).__name__ + ': ' + str(exc) + ')')
