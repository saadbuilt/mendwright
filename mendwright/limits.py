"""In-process limits for direct providers (no gateway in front).

When mendwright calls a model provider in-process (Anthropic, OpenAI-compatible, Gemini,
Bedrock, LiteLLM), no gateway meters the calls: there is no per-key and hourly rate limit
for a page that keeps breaking, and no spend cap for a loop that would drain an API key.
These classes put that budget on the client side.

Two guards, each in memory, plus a wrapper:

  * ``RateLimiter``: sliding-window per-key and global hourly caps. The clock is
    injected (defaults to :func:`time.monotonic`) so tests advance time instead of
    sleeping. With a sliding window, capacity returns gradually as old hits age out, and
    is fully restored once an hour passes with no new hits.
  * ``SpendCap``: a ceiling on heal calls per run, plus an optional per-provider
    cumulative cost ceiling (so a cheap model and an expensive one can have different
    budgets in the same run).
  * ``Guardrails``: bundles one of each, and consumes a slot from both only when both
    allow the call.

Every guard fails closed: when a cap is reached, ``allow()`` returns ``False`` and records
nothing, so a blocked attempt never uses budget, and the block holds until the window
moves on (RateLimiter) or the run ends (SpendCap). HealCoordinator asks its guardrails
before each heal; on ``False`` it declines the heal without calling the provider, and the
AttentionRequired says the cap was reached.

Each guard holds one :class:`threading.Lock` around its check-and-consume. That is enough
for a provider healing one page at a time; this is not a server hot path.
"""

import math
import threading
import time

HOUR = 3600.0


def _finite_cost(cost):
    """Fail-closed cost sanitizer. A non-numeric, non-finite (NaN/inf) or negative cost
    becomes +inf so it trips any ceiling instead of silently bypassing it. NaN compares
    False to everything, which would otherwise let a poisoned cost disable the cap."""
    try:
        c = float(cost)
    except (TypeError, ValueError):
        return float('inf')
    return c if (math.isfinite(c) and c >= 0) else float('inf')


class RateLimiter:
    """Sliding-window rate limit with a per-key cap and a global cap, both per hour.

    ``per_key_per_hour`` bounds heals for any single selector key (a stuck step that keeps
    re-breaking); ``global_per_hour`` bounds heals across every key (a runaway page).
    ``None`` for either cap means that dimension is unlimited. ``window`` is the sliding
    window in the clock's units (default one hour); ``clock`` is any zero-arg callable
    returning a monotonically increasing float, injected so tests control time.
    """

    def __init__(self, *, per_key_per_hour=None, global_per_hour=None,
                 window=HOUR, clock=time.monotonic):
        self.per_key_per_hour = per_key_per_hour
        self.global_per_hour = global_per_hour
        self.window = float(window)
        self._clock = clock
        self._lock = threading.Lock()
        self._per_key = {}          # key -> list[timestamp], oldest first
        self._global = []           # list[timestamp], oldest first

    @staticmethod
    def _prune(stamps, cutoff):
        """Drop timestamps at or before ``cutoff`` (older than one window). In place."""
        i = 0
        n = len(stamps)
        while i < n and stamps[i] <= cutoff:
            i += 1
        if i:
            del stamps[:i]

    def check(self, key):
        """Peek: would a heal for ``key`` be allowed now? Prunes stale hits but
        consumes nothing. Thread-safe."""
        now = self._clock()
        with self._lock:
            return self._check_locked(key, now)

    def _check_locked(self, key, now):
        cutoff = now - self.window
        kstamps = self._per_key.get(key)
        if kstamps is not None:
            self._prune(kstamps, cutoff)
        self._prune(self._global, cutoff)
        if self.per_key_per_hour is not None and \
                len(kstamps or ()) >= self.per_key_per_hour:
            return False
        if self.global_per_hour is not None and \
                len(self._global) >= self.global_per_hour:
            return False
        return True

    def allow(self, key):
        """Check both caps for ``key``; if under both, record the hit and return ``True``.
        Otherwise record nothing and return ``False`` (fail closed)."""
        now = self._clock()
        with self._lock:
            if not self._check_locked(key, now):
                return False
            self._per_key.setdefault(key, []).append(now)
            self._global.append(now)
            return True

    def snapshot(self):
        """Current in-window counts, for logging/diagnostics: ``(global, {key: count})``.
        Prunes first so the counts are current."""
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            self._prune(self._global, cutoff)
            per_key = {}
            for key in list(self._per_key):          # copy: we mutate the dict below
                stamps = self._per_key[key]
                self._prune(stamps, cutoff)
                if stamps:
                    per_key[key] = len(stamps)
                else:
                    del self._per_key[key]            # evict empty keys so the dict can't grow unbounded
            return len(self._global), per_key


class SpendCap:
    """A per-run budget: a hard call ceiling plus an optional per-provider cost ceiling.

    ``max_calls`` is the total number of heal calls permitted for the lifetime of this cap
    (one run); ``None`` means unlimited calls. ``cost_ceiling`` is an optional mapping
    ``{provider_name: max_cumulative_cost}``; a provider absent from the mapping (or a
    ``None`` ceiling) carries no cost limit, only the shared call limit. Reserve budget by
    calling :meth:`allow` before each model call, passing the provider name and the
    (estimated or known) cost of that call.
    """

    def __init__(self, *, max_calls=None, cost_ceiling=None):
        self.max_calls = max_calls
        self.cost_ceiling = dict(cost_ceiling or {})
        self._lock = threading.Lock()
        self._calls = 0
        self._spent = {}            # provider -> cumulative cost

    def _check_locked(self, provider, cost):
        if self.max_calls is not None and self._calls >= self.max_calls:
            return False
        ceiling = self.cost_ceiling.get(provider)
        if ceiling is not None and self._spent.get(provider, 0.0) + _finite_cost(cost) > ceiling:
            return False
        return True

    def check(self, provider=None, cost=0.0):
        """Peek: would one more call of ``cost`` against ``provider`` be allowed? Consumes
        nothing. Thread-safe."""
        with self._lock:
            return self._check_locked(provider, cost)

    def allow(self, provider=None, cost=0.0):
        """Reserve one call (and ``cost`` against ``provider``'s ceiling) for this run.
        Returns ``True`` and records the spend when under every cap, else ``False`` and
        records nothing (fail closed).

        Pass ``cost`` as a pre-call estimate when you know it up front. When the real cost
        is only known from the response, reserve with ``cost=0`` here and record the
        actual figure with :meth:`charge` afterwards. The ceiling trips on the next
        ``allow`` either way."""
        cost = _finite_cost(cost)
        with self._lock:
            if not self._check_locked(provider, cost):
                return False
            self._calls += 1
            if cost:
                self._spent[provider] = self._spent.get(provider, 0.0) + cost
            return True

    def charge(self, provider=None, cost=0.0):
        """Record the actual cost of a completed call against ``provider``'s ceiling, for
        the common case where cost is only known from the response usage (so :meth:`allow`
        was reserved with ``cost=0``). Does not touch the call counter. Once the
        accumulated cost meets or crosses the ceiling, later ``allow``/``check`` for that
        provider fail closed."""
        cost = _finite_cost(cost)
        if not cost:
            return
        with self._lock:
            self._spent[provider] = self._spent.get(provider, 0.0) + cost

    @property
    def calls(self):
        """Calls reserved so far this run."""
        with self._lock:
            return self._calls

    def spent(self, provider=None):
        """Cumulative cost reserved against ``provider`` this run."""
        with self._lock:
            return self._spent.get(provider, 0.0)


class Guardrails:
    """Bundle a :class:`RateLimiter` and a :class:`SpendCap` (either optional) behind one
    ``allow`` so a direct provider has a single thing to consult.

    A ``check`` pre-pass peeks both guards so the common path does not spend a rate slot
    on a call the spend cap would reject. The return of each guard's ``allow`` is still
    honored, because a guard may be shared across providers and drained by another thread
    between the peek and the consume. Two properties always hold: it never returns
    ``True`` without consuming a slot from every present guard, and it never charges the
    spend cap for a call the rate limiter rejected. Under that cross-thread race it may
    spend one rate slot on a call it then blocks. That is conservative, and still fails
    closed."""

    def __init__(self, rate_limiter=None, spend_cap=None):
        self.rate_limiter = rate_limiter
        self.spend_cap = spend_cap
        self._lock = threading.Lock()

    def allow(self, key, provider=None, cost=0.0):
        """Return ``True`` (and consume one slot from each present guard) only if every
        guard allows the call; otherwise return ``False``, never charging the spend cap
        for a call the rate limiter rejected."""
        with self._lock:
            # Peek first so the happy path doesn't burn a rate slot when spend is capped.
            if self.rate_limiter is not None and not self.rate_limiter.check(key):
                return False
            if self.spend_cap is not None and not self.spend_cap.check(provider, cost):
                return False
            # Consume, honoring the result: a shared guard can be drained between peek and
            # consume, so a positive check() does not guarantee a positive allow().
            if self.rate_limiter is not None and not self.rate_limiter.allow(key):
                return False
            if self.spend_cap is not None and not self.spend_cap.allow(provider, cost):
                return False  # may have spent one rate slot: conservative, still closed
            return True
