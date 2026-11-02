"""A store kept elsewhere than a local file: in S3, behind an HTTPS server, or in memory for
tests (see mendwright.backends and docs/remote-store.md), so a fleet of machines shares one
store of repairs.

RemoteSelectorStore is a SelectorStore whose override file is the object overrides.json of a
StoreBackend. Everything a SelectorStore does to its file it does there, with the same merge
rules, and nothing else changes: the seed file and destructive_keys stay local and still win,
every heal is checked live on the page that uses it, and the journal stays on this machine.

- Reading: a store reads overrides.json when it loads. Later looks (refresh(), before a write
  it may skip) ask with if_none_match, so an unchanged store costs a 304, and the stores of a
  process share one parse per version (_Doc): looks that ask at once make one request, and the
  looks of a lookup's poll (max_age) take one another store of the process made that recently.
- Writing: get, merge with what this store changed (the rules of a local write), then
  put(if_match=version), or create_only=True for a store with no overrides.json yet. On a
  Conflict another writer won: read again, merge again, try again, at most CAS_TRIES times,
  with a short random pause. After that the change stays in memory and is written with the
  store's next write, as a local store keeps a change it could not save. The stores of a
  process take turns to write (as for a local file), and the merge base moves to each version
  merged, so a retry never mistakes another writer's change for a change of both sides.
- A store that cannot be read when it loads (unreachable, credentials refused, anything a
  backend raises, or an overrides.json that is not a selector file) is never taken for an empty
  one: the store starts from the seed alone, selectors in code and seed files still resolve,
  and every heal is refused with the reason (heal_refusal()), until a later look reads it. A
  missing overrides.json is an empty store.
- While the store does not answer, every store of the process waits before it asks again
  (_Health: 1 second, doubling to 60), so an outage costs one timeout per wait, not one per
  lookup. Reads keep the last copy read; writes keep the change in memory and make it with a
  later write. The journal gets store.unavailable when an outage starts and store.available
  when it ends, once per process.
"""

from __future__ import annotations

import copy
import itertools
import json
import logging
import math
import random
import threading
import time
import uuid
import weakref

from .backends import OVERRIDES, BackendError, Conflict, MemoryBackend, Unavailable
from .journal import NullJournal
from .store import (SOFT, SelectorStore, StoreReadError, _apply_soft, _call_keys, _empty,
                    _expand, _parse, _read_schema2, _read_text, _same_store, _writes, merge)

__all__ = ['RemoteSelectorStore', 'load_remote_store', 'place_of']

logger = logging.getLogger('mendwright')


# --- which store: a place ------------------------------------------------------------------------

_MEMORY_NAMES = weakref.WeakKeyDictionary()
_MEMORY_LOCK = threading.Lock()


def place_of(backend):
    """The name mendwright knows a remote store by, in messages and as the key of everything a
    process shares per store (single-flight, reused declines, the rate limit, the parse of
    overrides.json): the store URL its backend describes (s3://bucket/prefix/,
    https://host:port/path/), and for a MemoryBackend, which describes itself as 'memory' and
    numbers its versions from 1, memory: and a name of its own."""
    if isinstance(backend, MemoryBackend):
        with _MEMORY_LOCK:
            name = _MEMORY_NAMES.get(backend)
            if name is None:
                name = _MEMORY_NAMES[backend] = 'memory:' + uuid.uuid4().hex[:12]
            return name
    return str(backend.describe())


# --- an outage of a store, for every store of the process on it ----------------------------------

class _Health:
    """Whether a remote store answered lately, for one kind of call: reads (get) or writes (put,
    delete), apart, so a store that refuses writes (a read-only role) is still read. After a
    failure, every store of this process on it waits FIRST seconds before it asks again, then
    twice as long after each failure that follows, at most MOST seconds, with a little jitter;
    a call that succeeds ends the wait. The start and the end of an outage wait in news until a
    store of the process takes them for its journal (take), so each is said once, whichever call
    met it: a store's own, or one of the key records'."""

    FIRST = 1.0
    MOST = 60.0

    def __init__(self, kind):
        self.kind = kind
        self._lock = threading.Lock()
        self.failures = 0
        self.retry_at = 0.0
        self.since = None
        self.news = []
        self.clock = time.monotonic

    def wait(self):
        """Seconds until the store may be asked again: 0 when it may now."""
        with self._lock:
            return max(0.0, self.retry_at - self.clock()) if self.failures else 0.0

    def failed(self, exc):
        """Note a failure, and the start of an outage when the store answered before."""
        with self._lock:
            self.failures += 1
            pause = min(self.MOST, self.FIRST * 2 ** min(self.failures - 1, 16))
            pause *= random.uniform(0.8, 1.2)
            self.retry_at = self.clock() + pause
            if self.failures == 1:
                self.since = self.clock()
                self.news.append(('down', self.kind, exc, pause))

    def answered(self):
        """Note a success, and the end of an outage when there was one."""
        with self._lock:
            if not self.failures:
                return
            lasted = self.clock() - (self.since if self.since is not None else self.clock())
            self.failures, self.retry_at, self.since = 0, 0.0, None
            self.news.append(('up', self.kind, None, lasted))

    def take(self):
        """The starts and ends of outages nobody said yet, oldest first; they are cleared."""
        with self._lock:
            news, self.news = self.news, []
            return news


_HEALTH = {}
_HEALTH_LOCK = threading.Lock()


def _health(place, kind):
    with _HEALTH_LOCK:
        found = _HEALTH.get((place, kind))
        if found is None:
            found = _HEALTH[(place, kind)] = _Health(kind)
        return found


class _Guarded:
    """A StoreBackend that asks the store only when its _Health allows: while reads (or
    writes) wait after a failure, a read (or a write) raises Unavailable at once, with no
    request. A call that fails with any BackendError but Conflict starts or extends the wait;
    any answer ends it. news() gives the outages nobody said yet, for the journal."""

    def __init__(self, backend, place):
        self.backend = backend
        self.place = place
        self.reads = _health(place, 'read')
        self.writes = _health(place, 'write')

    def describe(self):
        return self.backend.describe()

    def get(self, name, *, if_none_match=None):
        return self._call(self.reads, lambda: self.backend.get(name, if_none_match=if_none_match))

    def put(self, name, body, *, if_match=None, create_only=False):
        return self._call(self.writes, lambda: self.backend.put(name, body, if_match=if_match,
                                                                create_only=create_only))

    def delete(self, name, *, if_match=None):
        return self._call(self.writes, lambda: self.backend.delete(name, if_match=if_match))

    def _call(self, health, action):
        wait = health.wait()
        if wait:
            raise Unavailable(self.place + ': the store failed a moment ago, so mendwright waits '
                              '%d s before it asks it again' % math.ceil(wait))
        try:
            result = action()
        except Conflict:
            health.answered()
            raise
        except BackendError as exc:
            health.failed(exc)
            raise
        health.answered()
        return result

    def news(self):
        """('down', kind, error, seconds to the next try) and ('up', kind, None, seconds it
        lasted) of the outages of this store nobody said yet; they are cleared."""
        return self.reads.take() + self.writes.take()


# --- overrides.json as this process last read it ----------------------------------------------

class _Doc:
    """The parse of a remote store's overrides.json the stores of this process share: its
    version and its schema-2 data (never changed in place; a store merges a deep copy), and
    when the read that gave them started. One read at a time per store per process. A body
    that is not a selector file is remembered by its version (seen, error), so asking again
    costs a 304 until it changes, and the stores keep what they read before it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.version = None
        self.data = None
        self.seen = None
        self.error = None
        self.read_at = None         # clock() when the last read started, for max_age
        self.read_turn = None       # its turn (_TURNS): which calls asked before it started

    def latest(self, backend, place, known, max_age, clock, destructive_keys):
        """(data, version), as SelectorStore._latest() gives them. A read that started after
        this call did will do, and so will one that started less than max_age seconds before it;
        else this call asks the store, with if_none_match, so an unchanged store costs a 304.
        Raises the store's error, or StoreReadError for a body that is not a selector file."""
        asked, asked_at = next(_TURNS), clock()
        with self.lock:
            fresh = self.read_turn is not None and (
                self.read_turn > asked or (max_age and asked_at - self.read_at < max_age))
            if not fresh:
                turn, started = next(_TURNS), clock()
                read = backend.get(OVERRIDES, if_none_match=self.seen)
                if not read.not_modified:
                    self.seen, self.error = read.version, None
                    if read.body is None:
                        self.version, self.data = None, None
                    else:
                        try:
                            data, native = _parse(_text(read.body, place), place + OVERRIDES,
                                                  destructive_keys)
                        except StoreReadError as exc:
                            self.error = exc
                        else:
                            self.version, self.data = read.version, (data if native else None)
                self.read_turn, self.read_at = turn, started
            if self.error is not None:
                raise StoreReadError(str(self.error))
            if self.version is None:
                return None, None
            if self.version in known:
                return None, self.version
            return self.data, self.version

    def wrote(self, previous, version, data):
        """Note the version a store of this process just wrote over previous, with its data,
        unless the parse here moved on to another version meanwhile."""
        with self.lock:
            if self.seen in (previous, None) and self.error is None:
                self.version, self.data, self.seen = version, data, version


_DOCS = {}
_DOCS_LOCK = threading.Lock()
# Increasing numbers that order the calls of _Doc.latest() and the reads they make: a read that
# started after a call asked has a higher one (a clock can give two calls the same time).
_TURNS = itertools.count()


def _doc(place):
    with _DOCS_LOCK:
        found = _DOCS.get(place)
        if found is None:
            found = _DOCS[place] = _Doc()
        return found


def _text(body, place):
    try:
        return body.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise StoreReadError('mendwright: ' + place + OVERRIDES + ' is not UTF-8 text (' + str(exc)
                             + '). Save it as UTF-8, or delete it.') from None


def _soft_writer(backend, place, tries):
    """The writer SOFT flushes a remote store's soft changes with (see SoftWrites.register):
    get, apply them as a local flush does, and put with if_match only when that says to write.
    Never makes overrides.json for soft changes alone."""
    def write(pending):
        for _ in range(tries):
            read = backend.get(OVERRIDES)
            if read.body is None:
                return
            data, _native = _parse(_text(read.body, place), place + OVERRIDES)
            if not _apply_soft(data, pending):
                return
            try:
                backend.put(OVERRIDES, json.dumps(data, separators=(',', ':')).encode('utf-8'),
                            if_match=read.version)
                return
            except Conflict:
                continue
        raise Conflict(place + OVERRIDES + ' kept changing under ' + str(tries) + ' writes')
    return write


# --- the store ----------------------------------------------------------------------------------

class RemoteSelectorStore(SelectorStore):
    """A SelectorStore whose override file is overrides.json of a StoreBackend (see the module
    docstring). load_remote_store() makes one, and mendwright.open() for a store URL."""

    # How many times a write of overrides.json is tried when another writer wins the
    # compare-and-swap, and the most it pauses before the next try (a random part of it).
    CAS_TRIES = 5
    CAS_PAUSE = 0.2

    def __init__(self, data, backend, place, destructive_keys=frozenset(), seed=None,
                 override=None, *, read_only=False, version=None, unread=None, writes=True):
        super().__init__(data, place, destructive_keys, seed, override, read_only=read_only,
                         file_version=version, writes=writes)
        # backend is a _Guarded around the StoreBackend; place names the store.
        self.backend = backend
        self.remote_place = place
        # Why the store could not be read when it loaded, or None once it was read, and the
        # name of the error that said so.
        self.unread = unread
        self.unread_error = None
        # The journal of the Healer this store serves, for store.unavailable and
        # store.available (mendwright.open() sets it).
        self.journal = NullJournal()
        if not read_only:
            SOFT.register(place, _soft_writer(backend, place, self.CAS_TRIES))

    # A remote store has no override file on this machine: a use of its path is a mistake,
    # and fails loudly here; getattr(store, '_override_path', None) gives None.
    @property
    def _override_path(self):
        raise AttributeError('a remote store has no override file path')

    @_override_path.setter
    def _override_path(self, value):
        pass    # SelectorStore.__init__ sets it

    def __repr__(self):
        return 'RemoteSelectorStore(' + repr(self.remote_place) + ')'

    # --- the seams of SelectorStore -------------------------------------------------------------

    def _soft_path(self):
        return self.remote_place

    def _turns(self):
        return _writes('remote:' + self.remote_place)

    def _latest(self, known, max_age=0.0):
        try:
            return _doc(self.remote_place).latest(self.backend, self.remote_place, known,
                                                  max_age, self._clock, self._destructive_keys)
        finally:
            self._news()

    def _disk(self):
        try:
            read = self.backend.get(OVERRIDES)
        finally:
            self._news()
        return _parse(None if read.body is None else _text(read.body, self.remote_place),
                      self.remote_place + OVERRIDES, self._destructive_keys)

    # --- reading ------------------------------------------------------------------------------

    def refresh(self, key, *, max_age=0.0):
        if self.unread is not None and not self._read_only:
            self._read_again(max_age)
        return super().refresh(key, max_age=max_age)

    def heal_refusal(self, key=None):
        """Why no heal may run on this store now, or '': it could not be read when it loaded,
        and a look now does not read it either (see the module docstring)."""
        if self.unread is None:
            return ''
        self._read_again()
        return self.unread or ''

    def _read_again(self, max_age=0.0):
        """Read the store that could not be read when it loaded, and take it all in: True when
        it is read now. A store that still cannot be read keeps its reason."""
        try:
            data, version = self._latest((), max_age)
        except (BackendError, StoreReadError) as exc:
            self.unread = _unread_reason(self.remote_place, exc)
            self.unread_error = type(exc).__name__
            return False
        with self._mutex:
            if self.unread is None:
                return True
            if data is not None:
                taken = copy.deepcopy(data)
                self._absorb(taken)
                self._remember_base(taken)
            self._version = version
            self._key_versions = {}
            self.unread = self.unread_error = None
        logger.warning('mendwright: read the store %s, which could not be read when mendwright '
                       'opened it; heals are made again.', self.remote_place)
        return True

    # --- writing ------------------------------------------------------------------------------

    def flush(self):
        """SelectorStore.flush(), and a write that failed is made again too. True when nothing
        waits any more."""
        if not self.writes:
            return True
        if self._soft_waiting or self._unsaved:
            self._persist_override()
        return SOFT.flush(self._soft_path(), sleep=self._sleep) and not self._unsaved

    def _persist_override(self):
        if not self.writes:
            return
        state = self._batch
        if getattr(state, 'depth', 0):
            state.wanted = True
            return
        turns = self._turns()
        try:
            with turns.lock:
                try:
                    self._write_remote()
                finally:
                    turns.count += 1
            if self._soft_waiting:
                SOFT.written(self._soft_path(), self._soft_waiting)
                self._soft_waiting = set()
        except Exception as exc:
            self._unsaved = True
            self._persist_failed(exc)

    def _write_remote(self):
        """Merge with overrides.json and write it with a compare-and-swap (see the module
        docstring). Raises when the store cannot be read or written, and Conflict when other
        writers won CAS_TRIES times."""
        place, doc = self.remote_place, _doc(self.remote_place)
        try:
            self._cas(place, doc)
        finally:
            self._news()

    def _cas(self, place, doc):
        for attempt in range(self.CAS_TRIES):
            if attempt:
                self._sleep(random.uniform(0, self.CAS_PAUSE))
            read = self.backend.get(OVERRIDES)
            # A body that is not a selector file raises here: it is never replaced.
            disk, native = _parse(None if read.body is None else _text(read.body, place),
                                  place + OVERRIDES, self._destructive_keys)
            with self._mutex:
                self._absorb(disk)
                payload = self._payload()
                if native and _same_store(payload, disk):
                    self._remember_base(payload)
                    self._version, self._key_versions = read.version, {}
                    self._unsaved = False
                    return
                body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
                # The merge base moves to the version just merged: should another writer win,
                # the next try merges its change as the file's alone.
                self._remember_base(disk)
                self._version, self._key_versions = read.version, {}
            try:
                if read.version is None:
                    version = self.backend.put(OVERRIDES, body, create_only=True)
                else:
                    version = self.backend.put(OVERRIDES, body, if_match=read.version)
            except Conflict:
                continue
            with self._mutex:
                self._remember_base(payload)
                self._version, self._key_versions = version, {}
                self._unsaved = False
            doc.wrote(read.version, version,
                      _parse(body.decode('utf-8'), place + OVERRIDES, self._destructive_keys)[0])
            return
        raise Conflict(place + OVERRIDES + ' kept changing under ' + str(self.CAS_TRIES)
                       + ' writes; this store keeps its change in memory and writes it with '
                       'its next write')

    # --- failures -----------------------------------------------------------------------------

    def _news(self):
        """Log and journal the starts and ends of outages of the store that no store of the
        process said yet (see _Health), once each."""
        news = self.backend.news() if isinstance(self.backend, _Guarded) else ()
        for kind, op, detail, seconds in news:
            if kind == 'down':
                logger.warning(
                    'mendwright: could not %s the store %s (%s). mendwright keeps working: reads '
                    'use the copy it has, and changes wait in memory and are written once the '
                    'store takes them. It asks again in %d s.', op, self.remote_place,
                    _said(detail), math.ceil(seconds))
                _journal(self.journal, 'store.unavailable', store=self.remote_place, op=op,
                         error=type(detail).__name__, reason=str(detail),
                         retry_in=math.ceil(seconds))
            else:
                logger.info('mendwright: the store %s answers %ss again, after %d s.',
                            self.remote_place, op, round(seconds))
                _journal(self.journal, 'store.available', store=self.remote_place, op=op,
                         after=round(seconds))

    def _persist_failed(self, exc):
        detail = _said(exc)
        if isinstance(exc, BackendError) and not isinstance(exc, Conflict):
            logger.debug('mendwright: could not save to %s (%s)', self.remote_place, detail)
            return      # logged and journaled once per outage (_news)
        if self._warned:
            logger.debug('mendwright: still cannot save to %s (%s)', self.remote_place, detail)
            return
        self._warned = True
        if isinstance(exc, StoreReadError):
            logger.warning(
                'mendwright is not saving selector changes to %s, because the store there cannot '
                'be read now: %s It keeps working in memory, and leaves the store as it is. Fix '
                'or delete overrides.json there.', self.remote_place, str(exc))
        else:
            logger.warning(
                'mendwright could not save selector changes to %s (%s). It keeps working in memory '
                'and writes them with its next write.', self.remote_place, detail)
        _journal(self.journal, 'store.unavailable', store=self.remote_place, op='write',
                 error=type(exc).__name__, reason=str(exc))


def _said(exc):
    return type(exc).__name__ + (': ' + str(exc) if str(exc) else '')


def _journal(journal, name, **fields):
    try:
        journal.event(name, **fields)
    except Exception:
        pass        # a journal never breaks the store


def _unread_reason(place, exc):
    """Why heals are refused on a store that could not be read (see heal_refusal)."""
    if isinstance(exc, StoreReadError):
        what = str(exc)[len('mendwright: '):] if str(exc).startswith('mendwright: ') else str(exc)
    else:
        what = 'it could not be read (' + _said(exc) + ')'
    return ('mendwright could not read the store ' + place + ': ' + what.rstrip('.') + '. It '
            'never takes a store it cannot read for an empty one, which could undo a quarantine '
            'or a destructive flag saved there, so it heals nothing until it reads the store. '
            'Selectors in your code and seed files still work.')


def load_remote_store(seed_path, store, destructive_keys=frozenset(), *, read_only=False,
                      writes=True):
    """A RemoteSelectorStore over a local seed file (None for none, as load_store() takes it)
    and a remote store: a store URL (s3://bucket/prefix/, https://host/path/, memory:, see
    mendwright.backends.url) or a StoreBackend.

    A URL no backend takes raises ValueError, without quoting it, and an s3:// URL without
    boto3 ImportError. A store that cannot be reached, refuses the credentials, or holds an
    overrides.json that is not a selector file is not an error: the store starts from the seed
    alone, refuses every heal (heal_refusal), and reads the store again when it looks at it.
    writes=False: a read-only worker's store (see SelectorStore.writes)."""
    from .backends.url import backend_for, store_url
    if isinstance(store, str):
        place = store_url(store)            # raises ValueError for a URL nothing takes
        try:
            backend = backend_for(store)
        except BackendError as exc:         # a token file that cannot be read, a client setup
            backend, failure = None, exc
        else:
            failure = None
            place = place_of(backend)
    else:
        backend, failure, place = store, None, place_of(store)
    if seed_path is None:
        seed = _empty()
    else:
        seed_path = _expand(seed_path)
        if _read_text(seed_path) is None:
            import os
            raise FileNotFoundError(
                'mendwright: seed file ' + seed_path + ' does not exist (relative paths are '
                'read from ' + os.getcwd() + '). Pass an absolute path, or seed_path=None '
                'for no seed.')
        seed = _call_keys(_read_schema2(seed_path, destructive_keys), seed_path)
    override, native, version, unread, error = _empty(), False, None, None, failure
    if failure is not None:
        guarded = _Guarded(_Unbuilt(place, failure), place)
    else:
        guarded = _Guarded(backend, place)
        try:
            read = guarded.get(OVERRIDES)
            text = None if read.body is None else _text(read.body, place)
            override, native = _parse(text, place + OVERRIDES, destructive_keys)
            version = read.version
        except (BackendError, StoreReadError) as exc:
            override, native, version, error = _empty(), False, None, exc
    if error is not None:
        guarded.news()      # mendwright.open() says it, as store.unavailable with op open
        unread = _unread_reason(place, error)
    store = RemoteSelectorStore(merge(seed, override), guarded, place, destructive_keys,
                                seed=seed, override=override if native else None,
                                read_only=read_only, version=version if native else None,
                                unread=unread, writes=writes)
    if error is not None:
        store.unread_error = type(error).__name__
    if unread is None and native:
        _doc(place).wrote(None, version, override)
    return store


class _Unbuilt:
    """The backend of a store URL whose backend could not be made (backend_for raised a
    BackendError): every call raises that error again, so the store stays unread."""

    def __init__(self, place, error):
        self._place, self._error = place, error

    def describe(self):
        return self._place

    def _raise(self, *args, **kwargs):
        raise type(self._error)(str(self._error))

    get = put = delete = _raise
