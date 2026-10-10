"""Append-only JSONL event journal for heal activity: one line per event
(heal.requested / proposed / rejected / applied / canary / quarantine), recording the
provider, model, latency and token counts when they are known. Writing is best-effort and
never raises, so a bad path or a full disk cannot break a heal in progress.

The journal writes only the fields the caller passes: keys, selectors, provider and model
names, latency, reasons, and the model's reasoning text (which can quote the page). It
never reads the page or the row secrets itself.

NullJournal records nothing and touches no disk; HealCoordinator uses it unless given a
journal. Journal(path) appends to ./.mendwright/journal.jsonl (the path is configurable)
with mode 0o600, creating the parent directory on first write. mendwright.open() always
passes a Journal next to its store. The two classes are interchangeable, so the rest of
mendwright never has to special-case "no journal".
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Canonical event names. These are a log contract (dashboards/greps depend on them), so
# keep them stable and route every write through a named helper rather than a literal.
REQUESTED = 'heal.requested'
PROPOSED = 'heal.proposed'
REJECTED = 'heal.rejected'
APPLIED = 'heal.applied'
CANARY = 'heal.canary'
QUARANTINE = 'heal.quarantine'

# Default journal location, relative to the process cwd (like the override store's path).
DEFAULT_PATH = '.mendwright/journal.jsonl'

# ts/event frame the line; a stray field of the same name must never overwrite them.
_FRAME = ('ts', 'event')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Windows CreateFileW arguments for an append-only handle.
_FILE_APPEND_DATA = 0x0004
_SYNCHRONIZE = 0x00100000
_SHARE_ALL = 0x1 | 0x2 | 0x4          # read, write and delete: never block other processes
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80


def _open_append_windows(path):
    """A file descriptor for an append-only handle on Windows, or None if it cannot be
    made. The handle has FILE_APPEND_DATA without FILE_WRITE_DATA, so the kernel puts
    every write at the end of the file in one step, even with several processes."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel32.CreateFileW
        create.restype = wintypes.HANDLE
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        handle = create(str(path), _FILE_APPEND_DATA | _SYNCHRONIZE, _SHARE_ALL, None,
                        _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None)
        if not handle or handle == wintypes.HANDLE(-1).value:
            return None
        # No O_APPEND here, so the C runtime never seeks before a write.
        return msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
    except Exception:
        return None


def _open_append(path):
    """A file descriptor that appends to path, creating it with mode 0o600.

    O_APPEND makes each write() atomic at the end of the file on POSIX. On Windows the C
    runtime emulates O_APPEND as a seek followed by a write, which races between
    processes (lines overwrite each other), so there an append-only handle is opened
    instead, with os.open(O_APPEND) as the fallback. O_BINARY stops the Windows C runtime
    from rewriting a line feed as CR LF."""
    if os.name == 'nt':
        fd = _open_append_windows(path)
        if fd is not None:
            return fd
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_BINARY', 0)
    return os.open(str(path), flags, 0o600)


class _BaseJournal:
    """Shared named helpers so the event-name contract lives in one place and the Null
    and writing journals can never drift out of sync. Each helper is a thin wrapper over
    ``event(name, ...)``; ``key`` is first because every heal event is about a selector."""

    def event(self, name, /, **fields):  # pragma: no cover - overridden
        raise NotImplementedError

    def requested(self, key=None, **f):
        return self.event(REQUESTED, key=key, **f)

    def proposed(self, key=None, **f):
        return self.event(PROPOSED, key=key, **f)

    def rejected(self, key=None, **f):
        return self.event(REJECTED, key=key, **f)

    def applied(self, key=None, **f):
        return self.event(APPLIED, key=key, **f)

    def canary(self, key=None, **f):
        return self.event(CANARY, key=key, **f)

    def quarantine(self, key=None, **f):
        return self.event(QUARANTINE, key=key, **f)


class NullJournal(_BaseJournal):
    """The default journal: every event is a no-op. No disk, no cost, no failure surface."""

    def event(self, name, /, **fields):
        return None


class Journal(_BaseJournal):
    """Append-only JSONL journal. One compact JSON object per line, each with a ``ts`` and
    an ``event`` plus whatever non-None fields the caller passes (provider, model,
    latency_ms, tokens, xpath, reason, success, ...). Writes are best-effort: ``event``
    never raises into the caller, so observability failures stay silent.

    ``clock`` is injectable for deterministic tests; the default is a UTC ISO timestamp.
    """

    def __init__(self, path=DEFAULT_PATH, *, clock=_now_iso):
        self._path = Path(path)
        self._clock = clock

    # ``name`` is positional-only so a caller/helper field literally called "name" lands
    # in **extra as data instead of colliding with this parameter.
    def event(self, name, /, *, key=None, provider=None, model=None,
              latency_ms=None, tokens=None, now=None, **extra):
        """Append one event line. Returns None; never raises into the caller.

        None-valued fields are dropped so "when known" reads cleanly in the log. ``False``
        and ``0`` are kept (a canary ``success=False`` or ``latency_ms=0`` is real data)."""
        try:
            record = {'ts': now or self._clock(), 'event': name}
            for field, value in (('key', key), ('provider', provider), ('model', model),
                                 ('latency_ms', latency_ms), ('tokens', tokens)):
                if value is not None:
                    record[field] = value
            for field, value in extra.items():
                if value is None or field in _FRAME:
                    continue
                record[field] = value
            self._write_line(record)
        except Exception:
            # Best-effort side-channel: a heal must never fail because its journal couldn't
            # be written. Swallow clock errors, serialization errors and I/O errors alike.
            pass
        return None

    def _write_line(self, record):
        # default=str degrades an exotic VALUE to a string; skipkeys=True drops a non-native
        # dict KEY (default= never fires for keys). Together the line degrades instead of
        # raising and vanishing entirely.
        line = json.dumps(record, separators=(',', ':'), ensure_ascii=False,
                          default=str, skipkeys=True) + '\n'
        self._path.parent.mkdir(parents=True, exist_ok=True)
        created = not self._path.exists()
        # One write() per line on a handle that appends atomically, so lines from several
        # processes (pytest -n auto) never overwrite or split each other (see _open_append).
        fd = _open_append(self._path)
        try:
            os.write(fd, line.encode('utf-8'))
        finally:
            os.close(fd)
        if created:
            try:
                os.chmod(str(self._path), 0o600)
            except OSError:
                pass
