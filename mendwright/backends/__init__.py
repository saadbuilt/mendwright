"""Where a store's files live: the contract every storage backend keeps.

A store is a few named objects under one place: ``overrides.json`` (the heals, in the
format Python and TypeScript share) and ``keys/<sha256(key)[:32]>.json`` (one small
coordination record per key: a lease, recent attempts, a cached decline). A backend reads
and writes those objects by name, with versions, so writers on several threads, processes
and machines can change them without losing each other's changes.

Every backend keeps these rules, and the contract tests hold each one to them:

- ``get(name)`` returns ``Read(body, version, not_modified)``. A missing object is
  ``Read(None, None, False)``. With ``if_none_match`` equal to the object's current
  version, it is ``Read(None, version, True)`` and no body is sent.
- ``put(name, body, if_match=v)`` writes only when the current version is ``v``, and
  ``put(name, body, create_only=True)`` only when the object does not exist. Otherwise
  it raises :class:`Conflict` and changes nothing. With neither, it writes whatever is
  there. It returns the new version. A write is all or nothing: a reader sees the old
  body or the new one, never a mix.
- ``delete(name, if_match=v)`` removes the object only when its version is ``v``, else
  raises :class:`Conflict`. Deleting an object that is already gone is not an error.
- A version is an opaque string that changes whenever the body changes. Compare versions
  only for equality.
- Network trouble, timeouts and server errors raise :class:`Unavailable`, refused
  credentials :class:`Denied`. No backend retries a write on its own: the caller merges
  and tries again, because only it knows how to merge.
- ``describe()`` names the place for messages, never with credentials.
"""

from __future__ import annotations

import threading
from typing import NamedTuple, Protocol, runtime_checkable

__all__ = ['Read', 'StoreBackend', 'BackendError', 'Conflict', 'Unavailable', 'Denied',
           'MemoryBackend', 'OVERRIDES', 'key_record_name']

OVERRIDES = 'overrides.json'


def key_record_name(key: str) -> str:
    """The name of a key's coordination record: keys/<first 32 hex of sha256(key)>.json."""
    import hashlib
    return 'keys/' + hashlib.sha256(key.encode('utf-8')).hexdigest()[:32] + '.json'


class Read(NamedTuple):
    body: bytes | None
    version: str | None
    not_modified: bool = False


class BackendError(Exception):
    """A backend could not do what was asked. Its message never holds credentials."""


class Conflict(BackendError):
    """A precondition failed: the object changed, exists, or is gone. Nothing was written."""


class Unavailable(BackendError):
    """The place could not be reached or answered with an error: try again later."""


class Denied(BackendError):
    """The credentials were refused, or are missing."""


@runtime_checkable
class StoreBackend(Protocol):
    def get(self, name: str, *, if_none_match: str | None = None) -> Read: ...

    def put(self, name: str, body: bytes, *, if_match: str | None = None,
            create_only: bool = False) -> str: ...

    def delete(self, name: str, *, if_match: str | None = None) -> None: ...

    def describe(self) -> str: ...


class MemoryBackend:
    """Objects in a dict, for tests and for one process: the reference for the contract."""

    def __init__(self):
        self._objects: dict[str, tuple[bytes, str]] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def get(self, name, *, if_none_match=None):
        with self._lock:
            found = self._objects.get(name)
        if found is None:
            return Read(None, None, False)
        body, version = found
        if if_none_match is not None and if_none_match == version:
            return Read(None, version, True)
        return Read(body, version, False)

    def put(self, name, body, *, if_match=None, create_only=False):
        if if_match is not None and create_only:
            raise ValueError('put() takes if_match= or create_only=True, not both')
        if not isinstance(body, (bytes, bytearray)):
            raise TypeError('put() takes the body as bytes')
        with self._lock:
            found = self._objects.get(name)
            if create_only and found is not None:
                raise Conflict(name + ' already exists')
            if if_match is not None and (found is None or found[1] != if_match):
                raise Conflict(name + ' changed since version ' + if_match)
            self._counter += 1
            version = str(self._counter)
            self._objects[name] = (bytes(body), version)
            return version

    def delete(self, name, *, if_match=None):
        with self._lock:
            found = self._objects.get(name)
            if found is None:
                return
            if if_match is not None and found[1] != if_match:
                raise Conflict(name + ' changed since version ' + if_match)
            del self._objects[name]

    def describe(self):
        return 'memory'
