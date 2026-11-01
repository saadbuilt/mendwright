"""Many workers at once: how the workers of one store file share a heal instead of each
making its own.

A worker is a Healer: each one mendwright.open() or mendwright.aio.open() makes has a
SelectorStore of its own, so threads, asyncio tasks and processes on one store file are all
workers. When a key breaks for many of them at once (a deploy that renamed a field, under
a thousand parallel tests or scrapers), this module makes them heal it once:

- Single-flight in a process (single_flight, single_flight_async): the first worker to
  reach the heal of (store file, key, page signature) leads it. The others wait for it,
  never longer than their step's own timeout, then adopt the leader's heal (store.refresh()
  and the live check on their own page, see HealCoordinator.adopt) or reuse its decline. A
  follower whose wait runs out makes its own attempt under the normal caps. A follower on the
  async healer awaits an asyncio future on its event loop, so it holds no worker thread.

The page signature (page_signature) says which pages a heal or a decline can be shared on.
It is deterministic, so the TypeScript port can compute it byte for byte.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time

__all__ = ['page_signature', 'FLIGHTS', 'Flights', 'Flight', 'single_flight',
           'single_flight_async', 'store_name']


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
    case on Windows), or None for a store without one (a custom store), which heals alone."""
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


def single_flight(store, key, signature, heal, timeout_s, *, clock=time.monotonic):
    """heal(), at most one at a time per (store file, key, page signature) in this process.

    The first caller leads: it runs heal(), the whole heal and the retry of the step, and the
    flight ends when that returns or raises, so its heal, its first success and its decline
    are in the store (and the decline cache) before a follower wakes. A follower waits for
    it, at most timeout_s seconds (the step's own timeout), then runs heal() itself: that
    adopts the leader's heal or reuses its decline when they fit (see
    HealCoordinator.try_heal), else makes its own attempt under the normal caps. Followers
    that wake together adopt in parallel. A store with no file (store_name) heals alone."""
    name = store_name(store)
    if name is None:
        return heal()
    flight, leader = FLIGHTS.begin((name, key, signature))
    if leader:
        try:
            return heal()
        finally:
            FLIGHTS.finish(flight)
    FLIGHTS.wait(flight, timeout_s)
    return heal()


async def single_flight_async(store, key, signature, heal, timeout_s):
    """single_flight() for the async healer: heal is a coroutine function, and a follower
    awaits the leader on its event loop (Flights.wait_async)."""
    name = store_name(store)
    if name is None:
        return await heal()
    flight, leader = FLIGHTS.begin((name, key, signature))
    if leader:
        try:
            return await heal()
        finally:
            FLIGHTS.finish(flight)
    await FLIGHTS.wait_async(flight, timeout_s)
    return await heal()
