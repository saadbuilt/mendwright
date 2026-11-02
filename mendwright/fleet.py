"""Many workers at once: how the workers of one store file share a heal instead of each
making its own.

A worker is a Healer: each one mendwright.open() or mendwright.aio.open() makes has a
SelectorStore of its own, so threads, asyncio tasks and processes on one store file are all
workers. When a key breaks for many of them at once (a deploy that renamed a field, under
a thousand parallel tests or scrapers), this module makes them heal it once:

- Single-flight in a process (single_flight, single_flight_async): the first worker to
  reach the heal of (store file, key, page signature) leads it. The others wait for it as
  long as it is at work, at most FOLLOW_CAP seconds whatever their step's own timeout, then
  adopt the leader's heal (store.refresh() and the live check on their own page, see
  HealCoordinator.adopt) or reuse its decline. A follower whose wait runs out makes its own
  attempt under the normal caps. A follower on the async healer awaits an asyncio future on
  its event loop, so it holds no worker thread.

- Declines a worker reuses (DECLINES): when the model looked at a page and gave no usable
  heal (it said no element fits, code not_here, or nothing it proposed passed the checks,
  code gate), that answer is kept per (store file, key, page signature) for
  MENDWRIGHT_DECLINE_TTL_SECONDS (600 by default, 0 turns it off). Inside that time a worker
  reuses the decline and its reason instead of asking the model again, and spends no budget.
  Failures of the provider itself (a network error, a rate limit, a bad key) are not kept:
  asking again can work.

The page signature (page_signature) says which pages a heal or a decline can be shared on.
It is deterministic, so the TypeScript port can compute it byte for byte.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import threading
import time

__all__ = ['page_signature', 'FLIGHTS', 'Flights', 'Flight', 'single_flight',
           'single_flight_async', 'FOLLOW_CAP', 'store_name', 'DECLINES', 'Declines',
           'REUSED_DECLINES', 'DECLINE_TTL_DEFAULT', 'decline_ttl_setting', 'KeyRecords',
           'records_for', 'LEASE_TTL', 'OWNER']


# --- the page signature -------------------------------------------------------------------------

SIGNATURE_VERSION = 'mendwright-page-signature/1'


def _netstring(text):
    data = text.encode('utf-8')
    return str(len(data)).encode('ascii') + b':' + data + b','


def page_signature(page, selectors):
    """Which page a heal or a decline of a key belongs to, as 64 lowercase hex digits.

    page is the page path as mendwright records it (flow.page_path: the path in lower case,
    record ids as '*', no host, query or fragment), or None for a page with no path (the site
    root, about:blank, a data: URL), written as the empty string. selectors are the key's
    selectors that stopped matching, as the store holds them (the old selectors of
    heal.applied).

    The digest is SHA-256 of the netstrings (djb's "<length>:<bytes>,", the length in
    decimal ASCII digits of the UTF-8 bytes) of, in order: the version tag
    'mendwright-page-signature/1', the page, and each distinct selector, sorted by their
    UTF-8 bytes. In JavaScript: TextEncoder for the bytes, and sort the selectors by those
    bytes (not with the default sort, which compares UTF-16 code units).

    No digest of the page's elements is in it, on purpose: the text and the generated ids of
    real pages differ from one worker to the next (a cart count, a user name, a React id), so
    every worker would get a signature of its own and nothing would be shared, and reading
    them costs a round trip before every heal. Two variants of one page path (an A/B test)
    share a signature: an adopted heal is still checked live on each worker's own page, and a
    shared decline lasts only its TTL."""
    rows = sorted({s for s in selectors if isinstance(s, str)}, key=lambda s: s.encode('utf-8'))
    blob = _netstring(SIGNATURE_VERSION) + _netstring(page if isinstance(page, str) else '')
    for selector in rows:
        blob += _netstring(selector)
    return hashlib.sha256(blob).hexdigest()


def store_name(store):
    """The name single-flight knows a store's file by (its absolute path, normalized for
    case on Windows; for a remote store, its place, see mendwright.remote.place_of), or None
    for a store without one (a custom store), which heals alone."""
    place = getattr(store, 'remote_place', None)
    if isinstance(place, str) and place:
        return place
    path = getattr(store, '_override_path', None)
    if path is None:
        return None
    try:
        return os.path.normcase(os.path.abspath(os.fspath(path)))
    except TypeError:
        return None


# --- single-flight in a process -----------------------------------------------------------------

class Flight:
    """One heal of (store file, key, page signature) under way in this process: the leader
    calls finish() when it is done (heal, decline or error), which wakes every follower."""

    def __init__(self, ident):
        self.ident = ident
        self.thread = threading.get_ident()   # where the leader began it
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._futures = []                    # (loop, asyncio future) of async followers
        self.waiting = 0

    @property
    def done(self):
        return self._done.is_set()

    def _finish(self):
        with self._lock:
            self._done.set()
            futures, self._futures = self._futures, []
        for loop, future in futures:
            try:
                loop.call_soon_threadsafe(_resolve, future)
            except RuntimeError:
                pass        # that loop has closed: its follower is gone


def _resolve(future):
    if not future.done():
        future.set_result(None)


class Flights:
    """The flights under way in this process, and how many followers wait for each."""

    def __init__(self):
        self._flights = {}
        self._changed = threading.Condition()
        self._lease_waiters = 0

    def begin(self, ident):
        """(flight, leader): the flight of ident under way, or a new one this caller leads."""
        with self._changed:
            flight = self._flights.get(ident)
            if flight is not None and not flight.done:
                return flight, False
            flight = self._flights[ident] = Flight(ident)
            return flight, True

    def finish(self, flight):
        """End flight: wake its followers and let the next heal of ident lead a new one."""
        with self._changed:
            if self._flights.get(flight.ident) is flight:
                del self._flights[flight.ident]
        flight._finish()
        with self._changed:
            self._changed.notify_all()

    def _waiting(self, flight, step):
        with self._changed:
            flight.waiting += step
            self._changed.notify_all()

    def wait(self, flight, timeout):
        """Wait, in this thread, until flight is done or timeout seconds pass. True when it
        is done. A thread never waits for a flight it leads itself (a nested lookup)."""
        if flight.thread == threading.get_ident():
            return flight.done
        self._waiting(flight, 1)
        try:
            return flight._done.wait(max(0.0, timeout))
        finally:
            self._waiting(flight, -1)

    async def wait_async(self, flight, timeout):
        """wait() for a follower on an event loop: it awaits a future the leader resolves
        from any thread, so it holds no worker thread while it waits."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with flight._lock:
            if flight._done.is_set():
                return True
            flight._futures.append((loop, future))
        self._waiting(flight, 1)
        try:
            await asyncio.wait_for(asyncio.shield(future), max(0.0, timeout))
            return True
        except asyncio.TimeoutError:
            return flight.done
        finally:
            self._waiting(flight, -1)

    def _lease_waiting(self, step):
        with self._changed:
            self._lease_waiters += step
            self._changed.notify_all()

    def lease_waiting(self):
        """How many leaders of this process wait for the lease of another process now."""
        with self._changed:
            return self._lease_waiters

    def _pause(self, seconds):
        time.sleep(seconds)

    def waiting(self, ident=None):
        """How many followers wait now: for the flight of ident, or for any flight."""
        with self._changed:
            flights = [self._flights.get(ident)] if ident is not None else list(self._flights.values())
            return sum(f.waiting for f in flights if f is not None)

    def wait_until(self, predicate, timeout):
        """Block until predicate() is true (it is checked each time a flight starts, ends or
        gains or loses a follower), or timeout seconds pass. Returns predicate(). For tests
        and diagnostics: no sleep and no polling."""
        with self._changed:
            return self._changed.wait_for(predicate, timeout)


# Every single-flight in this process goes through this one registry.
FLIGHTS = Flights()

# Seconds a worker waits at most for the heal another worker leads (of the same store file,
# key and page signature: the leader in its own process, or another process while that one
# holds the key's lease) before it makes its own attempt. The step's own timeout_ms does not
# bound the wait: a worker that stopped waiting would make the same model call itself, and the
# leader's answer comes no later than its own would. A leader that dies is found out sooner,
# when its lease goes LEASE_TTL seconds without a renewal.
FOLLOW_CAP = 60.0


def single_flight(store, key, signature, heal, *, clock=time.monotonic):
    """heal(), at most one at a time per (store file, key, page signature) in this process,
    and across the processes of this host.

    The first caller leads: it runs heal(), the whole heal and the retry of the step, and the
    flight ends when that returns or raises, so its heal, its first success and its decline
    are in the store (and the decline cache) before a follower wakes. A follower waits for
    it as long as it is at work, at most FOLLOW_CAP seconds whatever its step's timeout, then
    runs heal() itself: that adopts the leader's heal or reuses its decline when they fit (see
    HealCoordinator.try_heal), else makes its own attempt under the normal caps. Followers
    that wake together adopt in parallel. Before it heals, the leader takes the key's lease
    for the other processes (see the comment above LEASE_TTL), or waits for the process that
    holds it while that one renews it, at most FOLLOW_CAP seconds too. A store with no file
    (store_name) heals alone."""
    name = store_name(store)
    if name is None:
        return heal()
    flight, leader = FLIGHTS.begin((name, key, signature))
    if not leader:
        FLIGHTS.wait(flight, FOLLOW_CAP)
        return heal()
    try:
        if DECLINES.get(name, key, signature, time.time()) is not None:
            # The model's answer about this page is kept here: heal() reuses it, and the lease
            # would be two writes of the key's record for nothing.
            return heal()
        records = records_for(store)
        held = _lead_across_processes(records, key, signature, clock() + FOLLOW_CAP, clock,
                                      FLIGHTS._pause)
        try:
            return heal()
        finally:
            if held:
                _release(records, key)
    finally:
        FLIGHTS.finish(flight)


async def single_flight_async(store, key, signature, heal, *, clock=time.monotonic):
    """single_flight() for the async healer: heal is a coroutine function, a follower awaits
    the leader on its event loop (Flights.wait_async), at most FOLLOW_CAP seconds as well, and
    a leader that waits for the lease of another process awaits too. The key record's reads
    and writes of a remote store run in a worker thread (see _off_loop)."""
    name = store_name(store)
    if name is None:
        return await heal()
    flight, leader = FLIGHTS.begin((name, key, signature))
    if not leader:
        await FLIGHTS.wait_async(flight, FOLLOW_CAP)
        return await heal()
    try:
        if DECLINES.get(name, key, signature, time.time()) is not None:
            return await heal()     # a decline kept for this page: no lease (see single_flight)
        records = records_for(store)
        remote = getattr(store, 'remote_place', None) is not None
        held = await _lead_across_processes_async(records, key, signature,
                                                  clock() + FOLLOW_CAP, clock, remote=remote)
        try:
            return await heal()
        finally:
            if held:
                await _off_loop(remote, _release, records, key)
    finally:
        FLIGHTS.finish(flight)


# --- declines a worker reuses (the negative cache) ---------------------------------------------

# The heal.declined codes of a model's answer about a page, which workers reuse: it found no
# element that fits, or nothing it proposed passed the checks.
REUSED_DECLINES = ('not_here', 'gate')

# How long a decline is reused when MENDWRIGHT_DECLINE_TTL_SECONDS is not set: ten minutes.
DECLINE_TTL_DEFAULT = 600
DECLINE_TTL_VAR = 'MENDWRIGHT_DECLINE_TTL_SECONDS'


def decline_ttl_setting(environ=None):
    """MENDWRIGHT_DECLINE_TTL_SECONDS: how many seconds a model's decline is reused, a whole
    number, 0 to ask the model every time; DECLINE_TTL_DEFAULT when it is unset. Raises
    ValueError for anything else, as MENDWRIGHT_TIMEOUT_MS does."""
    raw = (os.environ if environ is None else environ).get(DECLINE_TTL_VAR)
    if raw is None or not raw.strip():
        return DECLINE_TTL_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError('mendwright: ' + DECLINE_TTL_VAR + ' must be a whole number of '
                         'seconds, 0 to ask the model every time, got ' + repr(raw) + '. Fix '
                         'it, or unset it for ' + str(DECLINE_TTL_DEFAULT) + ' seconds.')
    return value


class Declines:
    """The declines a process reuses: (store file, key, page signature) -> {'code', 'reason',
    'at', 'until'}, with wall-clock times (time.time()), so a record another process wrote
    (see mendwright.fleet's per-key records) means the same."""

    # Expired rows are dropped once the table has this many.
    PRUNE_AT = 1000

    def __init__(self):
        self._rows = {}
        self._lock = threading.Lock()

    def get(self, name, key, signature, now):
        """The decline in force for (name, key, signature) at now, as a dict, or None."""
        with self._lock:
            row = self._rows.get((name, key, signature))
            if row is None:
                return None
            if row['until'] <= now:
                del self._rows[(name, key, signature)]
                return None
            return dict(row)

    def put(self, name, key, signature, code, reason, now, ttl):
        """Keep a decline of code (one of REUSED_DECLINES) for ttl seconds from now. Any other
        code, and ttl 0, keep nothing."""
        if not ttl or ttl <= 0 or code not in REUSED_DECLINES:
            return
        with self._lock:
            if len(self._rows) >= self.PRUNE_AT:
                self._rows = {k: r for k, r in self._rows.items() if r['until'] > now}
            self._rows[(name, key, signature)] = {'code': code, 'reason': reason, 'at': now,
                                                  'until': now + ttl}

    def clear(self):
        with self._lock:
            self._rows.clear()


# Every decline a worker of this process reuses goes through this one table.
DECLINES = Declines()


def reused_note(at, now, ttl):
    """What a reused decline adds to its reason: whose answer it is, and for how long it is
    reused."""
    age = max(0, int(round(now - at)))
    return (' (mendwright asked the model about this page ' + str(age) + ' second'
            + ('' if age == 1 else 's') + ' ago, in this or another worker, and reuses its '
            'answer for ' + str(int(ttl)) + ' seconds instead of asking again: '
            + DECLINE_TTL_VAR + '.)')


# --- per-key records across the processes of one host -----------------------------------------
#
# Every process that opens a store file on this machine sees the same small record per key
# next to it (KeyRecords, through backends.file.FileBackend):
#
#     <store folder>/keys/<first 32 hex of sha256(key)>.json      (for overrides.json)
#     <store folder>/<file name>.keys/<...>.json                  (for any other file name)
#
# A record is JSON: {"schema": 1, "key": ..., "lease": {"owner", "sig", "at", "ttl"},
# "attempts": [time, ...], "declines": {sig: {"code", "reason", "at", "until"}}}, every time
# a wall-clock time.time() of this host (one host, one clock). It never goes in
# overrides.json: a writer that predates these fields drops what it does not know.
#
# - The lease makes single-flight work across processes: the in-process leader of a heal
#   takes it (a compare-and-swap, so exactly one process gets it) and holds it while it heals,
#   renewed every LEASE_RENEW seconds by a thread of its process. A process whose leader finds
#   the live lease of another process for the same page waits while that one renews it, at
#   most FOLLOW_CAP seconds, then adopts that heal or reuses that decline. A lease nobody
#   renewed for LEASE_TTL seconds belongs to a process that died: exactly one waiter takes it
#   over.
# - attempts holds the times of the model attempts made for the key in the last hour, by every
#   process: rate_per_key_per_hour holds for all of them together.
# - declines holds the model's declines of the key per page signature, with their TTL (see
#   DECLINES), so every process reuses them.
#
# A record that cannot be read or written means no coordination: the heal goes on alone,
# under the caps of its process, and never waits on it.

# Seconds a lease holds without a renewal, seconds between renewals, and between two looks of a
# waiter at the record.
LEASE_TTL = 15.0
LEASE_RENEW = 5.0
LEASE_POLL = 0.05
RECORD_SCHEMA = 1
# How many times a compare-and-swap is tried before the change is given up.
_CAS_TRIES = 50
# The same for the records of a remote store (mendwright.remote), where a look or a try is a
# request: a waiter looks four times a second, and a compare-and-swap is tried 8 times, with a
# random pause of up to 50 ms between tries.
REMOTE_LEASE_POLL = 0.25
REMOTE_CAS_TRIES = 8
REMOTE_CAS_PAUSE = 0.05


def _owner():
    import socket
    import uuid
    try:
        host = socket.gethostname()
    except Exception:
        host = 'host'
    return host + '/' + str(os.getpid()) + '/' + uuid.uuid4().hex[:12]


# Who holds a lease this process takes: the host, the process id and a random part, so a
# process id the OS gives again later is not taken for this process.
OWNER = _owner()


class KeyRecords:
    """The per-key records of one store file (see the comment above LEASE_TTL), through a
    backend that keeps the StoreBackend contract."""

    def __init__(self, backend, prefix='keys/', *, tries=_CAS_TRIES, pause=0.0, poll=LEASE_POLL):
        self.backend = backend
        self.prefix = prefix
        # How many times update() tries a compare-and-swap, the most it pauses between two
        # tries (a random part of it), and the seconds between two looks of a lease's waiter.
        self.tries, self.pause, self.poll = tries, pause, poll

    def name(self, key):
        return self.prefix + hashlib.sha256(key.encode('utf-8')).hexdigest()[:32] + '.json'

    def read(self, key):
        """(record, version): {} and None when there is none. A record that is not one (a
        partial write by hand, another tool's file) reads as {} with its version, so the next
        write replaces it. Backend errors propagate."""
        import json
        found = self.backend.get(self.name(key))
        if found.body is None:
            return {}, found.version
        try:
            record = json.loads(found.body.decode('utf-8'))
        except ValueError:
            return {}, found.version
        return (record if isinstance(record, dict) else {}), found.version

    def update(self, key, change):
        """Change key's record with a compare-and-swap: change(record) returns (new record or
        None for no write, result), and is called again on the record as it is now after a
        Conflict. Returns result. Backend errors propagate."""
        import json
        from .backends import Conflict
        for attempt in range(self.tries):
            if attempt and self.pause:
                time.sleep(random.uniform(0, self.pause))
            record, version = self.read(key)
            new, result = change(dict(record))
            if new is None:
                return result
            new = dict(new, schema=RECORD_SCHEMA, key=key)
            body = json.dumps(new, separators=(',', ':'), sort_keys=True).encode('utf-8')
            try:
                if version is None:
                    self.backend.put(self.name(key), body, create_only=True)
                else:
                    self.backend.put(self.name(key), body, if_match=version)
                return result
            except Conflict:
                continue
        raise Conflict('mendwright: ' + self.name(key) + ' kept changing under '
                       + str(self.tries) + ' tries')

    # --- the lease ------------------------------------------------------------------------------

    def look(self, key, now):
        """('none' | 'live' | 'expired', lease): the lease of key's record at now."""
        record, _version = self.read(key)
        return self.lease_of(record, now)

    @staticmethod
    def lease_of(record, now):
        """look() of a record already read."""
        lease = record.get('lease')
        if not isinstance(lease, dict) or not isinstance(lease.get('owner'), str):
            return 'none', None
        try:
            ends = float(lease.get('at')) + float(lease.get('ttl'))
        except (TypeError, ValueError):
            return 'expired', lease
        return ('live' if ends > now else 'expired'), lease

    def take(self, key, signature, owner, now, ttl):
        """Take key's lease for (owner, signature) when there is none, it is owner's for the
        same signature, or it has expired. True when owner holds it now. Racing processes:
        exactly one gets it (a compare-and-swap). A live lease owner holds for another
        signature is another heal of the key in that process, of another page: it stays."""
        def change(record):
            lease = record.get('lease')
            if isinstance(lease, dict) and (lease.get('owner') not in (None, owner)
                                            or lease.get('sig') != signature):
                try:
                    live = float(lease.get('at')) + float(lease.get('ttl')) > now
                except (TypeError, ValueError):
                    live = False
                if live:
                    return None, False
            record['lease'] = {'owner': owner, 'sig': signature, 'at': now, 'ttl': ttl}
            return record, True
        return self.update(key, change)

    def renew(self, key, owner, now):
        """Move the lease of owner on to now. False when owner no longer holds it."""
        def change(record):
            lease = record.get('lease')
            if not isinstance(lease, dict) or lease.get('owner') != owner:
                return None, False
            record['lease'] = dict(lease, at=now)
            return record, True
        return self.update(key, change)

    def release(self, key, owner):
        """Give the lease of owner back."""
        def change(record):
            lease = record.get('lease')
            if not isinstance(lease, dict) or lease.get('owner') != owner:
                return None, False
            record.pop('lease', None)
            return record, True
        return self.update(key, change)

    # --- the budget and the declines --------------------------------------------------------------

    def spend(self, key, cap, now, window=3600.0):
        """Count a model attempt for key at now when the attempts of every process in the last
        window seconds are fewer than cap: (True, attempts before it), else (False, attempts)."""
        def change(record):
            stamps = [t for t in record.get('attempts') or ()
                      if isinstance(t, (int, float)) and not isinstance(t, bool)
                      and now - window < t <= now + window]
            if cap is not None and len(stamps) >= cap:
                return None, (False, len(stamps))
            record['attempts'] = stamps + [now]
            return record, (True, len(stamps))
        return self.update(key, change)

    def decline(self, key, signature, now):
        """The decline in force for key on the page signature at now, or None."""
        record, _version = self.read(key)
        return self.decline_of(record, signature, now)

    @staticmethod
    def decline_of(record, signature, now):
        """decline() of a record already read."""
        declines = record.get('declines')
        row = declines.get(signature) if isinstance(declines, dict) else None
        if not isinstance(row, dict) or not isinstance(row.get('reason'), str):
            return None
        try:
            if float(row.get('until')) <= now:
                return None
            return {'code': str(row.get('code')), 'reason': row['reason'],
                    'at': float(row.get('at')), 'until': float(row.get('until'))}
        except (TypeError, ValueError):
            return None

    def keep_decline(self, key, signature, code, reason, now, ttl):
        """Keep a decline for every process, until now + ttl; expired ones are dropped."""
        def change(record):
            rows = {s: r for s, r in (record.get('declines') or {}).items()
                    if isinstance(r, dict) and isinstance(r.get('until'), (int, float))
                    and r['until'] > now}
            rows[signature] = {'code': code, 'reason': reason, 'at': now, 'until': now + ttl}
            record['declines'] = rows
            return record, True
        return self.update(key, change)


# The KeyRecords of each store file this process uses: path -> KeyRecords.
_RECORDS = {}
_RECORDS_LOCK = threading.Lock()


def records_for(store):
    """The KeyRecords of store's file (see the comment above LEASE_TTL), or None for a store
    without a file, one that never writes (read_only), and a local file whose name has a colon
    or a backslash, which no record name holds (it heals without them). A remote store's records are
    keys/<hash>.json next to its overrides.json, in its backend (mendwright.remote). For a
    read-only worker's store (writes False), a view that reads them and writes nothing
    (ReadOnlyRecords)."""
    from .backends import OVERRIDES
    from .backends.file import FileBackend
    if getattr(store, 'read_only', False) is True:
        return None
    name = store_name(store)
    if name is None:
        return None
    if name == getattr(store, 'remote_place', None):
        # Through the store's own backend, never one an earlier store of the place made: its
        # credentials are the ones read now (a token file can be renewed).
        found = KeyRecords(store.backend, 'keys/', tries=REMOTE_CAS_TRIES, pause=REMOTE_CAS_PAUSE,
                           poll=REMOTE_LEASE_POLL)
    else:
        with _RECORDS_LOCK:
            found = _RECORDS.get(name)
            if found is None:
                folder, file_name = os.path.split(os.path.abspath(os.fspath(store._override_path)))
                if ':' in file_name or '\\' in file_name:
                    # No object name holds it (FileBackend.path): such a store, as
                    # ./file:o.json on Linux, heals with no records, each process for itself.
                    return None
                prefix = 'keys/' if file_name == OVERRIDES else file_name + '.keys/'
                found = _RECORDS[name] = KeyRecords(FileBackend(folder), prefix)
    if getattr(store, 'writes', True) is False:
        return ReadOnlyRecords(found)
    return found


class ReadOnlyRecords:
    """The key records of a read-only worker's store: it reads the leases and the declines
    other workers keep, so it waits for another worker's heal of a page and reuses a decline,
    and writes nothing. take() and every other change raise Denied: such a worker heals without
    a lease (see _lead_across_processes), counts its attempts in its process only, and keeps its
    declines in this process."""

    def __init__(self, records):
        self._records = records
        self.prefix, self.poll = records.prefix, records.poll

    def name(self, key):
        return self._records.name(key)

    def read(self, key):
        return self._records.read(key)

    lease_of = staticmethod(KeyRecords.lease_of)
    decline_of = staticmethod(KeyRecords.decline_of)

    def look(self, key, now):
        return self._records.look(key, now)

    def decline(self, key, signature, now):
        return self._records.decline(key, signature, now)

    def _refuse(self, *args, **kwargs):
        from .backends import Denied
        raise Denied('a read-only worker writes no key record')

    update = take = renew = release = spend = keep_decline = _refuse


class _Renewer:
    """The thread that renews the leases this process holds, every LEASE_RENEW seconds,
    while their heals run (a model call can take longer than LEASE_TTL)."""

    def __init__(self):
        self._held = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    def add(self, records, key):
        with self._lock:
            self._held[(id(records), key)] = (records, key)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='mendwright-leases',
                                                daemon=True)
                self._thread.start()

    def remove(self, records, key):
        with self._lock:
            self._held.pop((id(records), key), None)

    def _run(self):
        while True:
            self._wake.wait(LEASE_RENEW)
            with self._lock:
                held = list(self._held.values())
            for records, key in held:
                try:
                    if not records.renew(key, OWNER, time.time()):
                        self.remove(records, key)       # taken over: it is not ours any more
                except Exception:
                    pass


RENEWER = _Renewer()


def _lead_across_processes(records, key, signature, deadline, clock, pause):
    """For the in-process leader: True when this process holds key's lease for signature
    now (it must release it, see _release), False when it heals without one. While the live
    lease of another process for the same page is held, and clock() is before deadline, it
    waits, pause(records.poll) between looks. A lease its owner gave back meanwhile ends the
    wait (that heal or decline is there to adopt or reuse); an expired one is taken over, by
    exactly one waiter. A decline another process kept for this page means no lease: heal()
    reuses it (see HealCoordinator._reused_decline), and the record is not written for it."""
    from .backends import BackendError, Conflict
    if records is None:
        return False
    waited = False
    try:
        while True:
            record, _version = records.read(key)
            now = time.time()
            if not waited and records.decline_of(record, signature, now) is not None:
                return False        # another process's decline of this page: reuse it
            state, lease = records.lease_of(record, now)
            if state == 'live' and (lease.get('owner') != OWNER or lease.get('sig') != signature):
                if lease.get('sig') != signature or clock() >= deadline:
                    # Another page's heal, of another process or of this one (whose lease
                    # this heal must not take over or end), or no time left: heal alone.
                    return False
                if not waited:
                    waited = True
                    FLIGHTS._lease_waiting(1)
                pause(records.poll)
                continue
            if state == 'none' and waited:
                return False        # the other process is done: adopt or reuse its outcome
            try:
                if records.take(key, signature, OWNER, time.time(), LEASE_TTL):
                    RENEWER.add(records, key)
                    return True
            except Conflict:
                pass
    except (BackendError, ValueError):     # ValueError: a name the backend takes no object by
        return False
    finally:
        if waited:
            FLIGHTS._lease_waiting(-1)


def _release(records, key):
    RENEWER.remove(records, key)
    try:
        records.release(key, OWNER)
    except Exception:
        pass        # it expires on its own


async def _off_loop(remote, fn, *args):
    """fn(*args), a call on the key records: in a worker thread for a remote store, where each
    is a network request that would hold up the event loop, and every other task on it, up to
    its timeout; on the loop for a local file, where each is one small file read or write."""
    if not remote:
        return fn(*args)
    from .aio import _in_thread
    return await _in_thread(fn, *args)


async def _lead_across_processes_async(records, key, signature, deadline, clock, *,
                                       remote=False):
    """_lead_across_processes() on an event loop: it awaits between looks, and for a remote
    store (remote=True) each read and write of the record runs in a worker thread (see
    _off_loop)."""
    from .backends import BackendError, Conflict
    if records is None:
        return False
    waited = False
    try:
        while True:
            record, _version = await _off_loop(remote, records.read, key)
            now = time.time()
            if not waited and records.decline_of(record, signature, now) is not None:
                return False        # another process's decline of this page: reuse it
            state, lease = records.lease_of(record, now)
            if state == 'live' and (lease.get('owner') != OWNER or lease.get('sig') != signature):
                if lease.get('sig') != signature or clock() >= deadline:
                    return False
                if not waited:
                    waited = True
                    FLIGHTS._lease_waiting(1)
                await asyncio.sleep(records.poll)
                continue
            if state == 'none' and waited:
                return False
            try:
                if await _off_loop(remote, records.take, key, signature, OWNER, time.time(),
                                   LEASE_TTL):
                    RENEWER.add(records, key)
                    return True
            except Conflict:
                pass
    except (BackendError, ValueError):     # ValueError: a name the backend takes no object by
        return False
    finally:
        if waited:
            FLIGHTS._lease_waiting(-1)
