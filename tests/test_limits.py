"""Guardrails for DIRECT (no-gateway) mode: per-key + global hourly rate limits and a
per-run spend cap, all FAIL CLOSED. Fully offline and deterministic — an injected fake
clock drives the sliding window, so nothing sleeps and nothing depends on wall time."""

from mendwright.limits import HOUR, Guardrails, RateLimiter, SpendCap


class FakeClock:
    """Deterministic injectable clock: reads ``now``, advances only when told to."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt
        return self.now


# --- RateLimiter: per-key cap -------------------------------------------------

def test_per_key_cap_blocks_the_same_key_but_not_another():
    clock = FakeClock()
    rl = RateLimiter(per_key_per_hour=2, clock=clock)

    assert rl.allow('login') is True
    assert rl.allow('login') is True
    assert rl.allow('login') is False          # third hit on the same key is blocked
    # A different key has its own budget and is unaffected.
    assert rl.allow('search') is True
    assert rl.allow('search') is True
    assert rl.allow('search') is False


# --- RateLimiter: global cap --------------------------------------------------

def test_global_cap_blocks_across_all_keys():
    clock = FakeClock()
    rl = RateLimiter(global_per_hour=3, clock=clock)

    assert rl.allow('a') is True
    assert rl.allow('b') is True
    assert rl.allow('c') is True
    # Fourth heal anywhere is blocked even though every key is individually fresh.
    assert rl.allow('d') is False
    assert rl.allow('a') is False


# --- RateLimiter: fail-closed does not consume budget -------------------------

def test_blocked_attempt_records_nothing_fail_closed():
    clock = FakeClock()
    rl = RateLimiter(per_key_per_hour=1, clock=clock)

    assert rl.allow('x') is True               # t = 1000
    clock.advance(1800.0)                       # half an hour later
    assert rl.allow('x') is False              # blocked
    assert rl.allow('x') is False              # still blocked
    # If the blocked attempts at t=2800 had been recorded, one would still be inside the
    # window at t=4600 and keep the key blocked. Prove they were not recorded: once the
    # ONLY real hit (t=1000) ages out at t=4600, capacity is fully back.
    clock.advance(HOUR - 1800.0)                # t = 1000 + 3600 exactly; first hit ages out
    assert rl.snapshot() == (0, {})
    assert rl.allow('x') is True


# --- RateLimiter: window resets after an hour ---------------------------------

def test_window_resets_after_an_hour():
    clock = FakeClock()
    rl = RateLimiter(per_key_per_hour=2, global_per_hour=2, clock=clock)

    assert rl.allow('k') is True
    assert rl.allow('k') is True
    assert rl.allow('k') is False              # per-key (and global) cap reached

    clock.advance(HOUR + 1.0)                   # more than an hour passes
    assert rl.allow('k') is True               # window has reset, budget restored
    assert rl.allow('k') is True
    assert rl.allow('k') is False


def test_window_slides_rather_than_resetting_in_lockstep():
    clock = FakeClock()                         # starts at t = 1000
    rl = RateLimiter(per_key_per_hour=2, clock=clock)

    assert rl.allow('k') is True               # hit at t = 1000
    clock.advance(600.0)
    assert rl.allow('k') is True               # hit at t = 1600; now full
    assert rl.allow('k') is False

    # Land strictly between the two expiries: the first hit (t=1000) ages out at t=4600,
    # the second (t=1600) not until t=5200. At t=4800 exactly one slot frees, not both.
    clock.advance(3200.0)                       # t = 4800; first hit (1000) drops, 1600 stays
    assert rl.allow('k') is True               # one slot reclaimed
    assert rl.allow('k') is False              # second hit (1600) still inside the window


def test_none_caps_mean_unlimited():
    clock = FakeClock()
    rl = RateLimiter(clock=clock)               # no caps set
    for _ in range(50):
        assert rl.allow('anything') is True


# --- SpendCap: max calls per run ----------------------------------------------

def test_call_cap_blocks_after_max_calls():
    sc = SpendCap(max_calls=3)
    assert sc.allow('anthropic') is True
    assert sc.allow('anthropic') is True
    assert sc.allow('openai') is True          # call cap is shared across providers
    assert sc.allow('anthropic') is False      # fourth call blocked
    # Fail closed: the blocked call did not increment the counter past the cap.
    assert sc.calls == 3
    assert sc.allow('openai') is False         # still blocked, still 3
    assert sc.calls == 3


# --- SpendCap: per-provider cost ceiling --------------------------------------

def test_per_provider_cost_ceiling_blocks_when_exceeded():
    sc = SpendCap(cost_ceiling={'anthropic': 1.0})

    assert sc.allow('anthropic', cost=0.6) is True
    # 0.6 + 0.6 = 1.2 would cross the 1.0 ceiling -> blocked, nothing charged.
    assert sc.allow('anthropic', cost=0.6) is False
    assert sc.spent('anthropic') == 0.6
    # A call that lands exactly on the ceiling is allowed (block only when EXCEEDED).
    assert sc.allow('anthropic', cost=0.4) is True
    assert sc.spent('anthropic') == 1.0
    # Now any further positive cost is blocked.
    assert sc.allow('anthropic', cost=0.01) is False


def test_cost_ceiling_is_per_provider_not_shared():
    sc = SpendCap(cost_ceiling={'anthropic': 1.0})   # only anthropic is capped

    assert sc.allow('anthropic', cost=1.0) is True
    assert sc.allow('anthropic', cost=0.5) is False  # anthropic exhausted
    # A provider with no ceiling spends freely (only the absent call cap would stop it).
    assert sc.allow('gemini', cost=99.0) is True
    assert sc.allow('gemini', cost=99.0) is True


def test_charge_records_actual_cost_and_trips_ceiling_post_hoc():
    # The real cost of a model call is only known from the response, so a provider
    # reserves with cost=0 up front and records the actual figure afterwards.
    sc = SpendCap(cost_ceiling={'anthropic': 1.0})
    assert sc.allow('anthropic', cost=0.0) is True
    assert sc.calls == 1
    assert sc.spent('anthropic') == 0.0

    sc.charge('anthropic', 1.5)                 # response came back more expensive
    assert sc.spent('anthropic') == 1.5
    assert sc.calls == 1                        # charge does not count as a call
    # Ceiling is now exceeded -> the next reservation fails closed, even at cost=0.
    assert sc.check('anthropic') is False
    assert sc.allow('anthropic', cost=0.0) is False


def test_call_cap_and_cost_ceiling_are_independent():
    sc = SpendCap(max_calls=2, cost_ceiling={'anthropic': 10.0})
    assert sc.allow('anthropic', cost=1.0) is True
    assert sc.allow('anthropic', cost=1.0) is True
    # Well under the cost ceiling, but the call cap bites first.
    assert sc.allow('anthropic', cost=1.0) is False
    assert sc.calls == 2
    assert sc.spent('anthropic') == 2.0


# --- Guardrails: combine both, consume from neither when either blocks --------

def test_guardrails_blocks_and_consumes_nothing_when_rate_limited():
    clock = FakeClock()
    rl = RateLimiter(per_key_per_hour=1, clock=clock)
    sc = SpendCap(max_calls=5, cost_ceiling={'anthropic': 10.0})
    guard = Guardrails(rate_limiter=rl, spend_cap=sc)

    assert guard.allow('k', provider='anthropic', cost=1.0) is True
    assert sc.calls == 1 and sc.spent('anthropic') == 1.0
    # Rate limiter is now exhausted for 'k'; the combined guard must block AND must not
    # charge the spend cap for a call that never happens.
    assert guard.allow('k', provider='anthropic', cost=1.0) is False
    assert sc.calls == 1
    assert sc.spent('anthropic') == 1.0


def test_guardrails_blocks_and_consumes_nothing_when_spend_capped():
    clock = FakeClock()
    rl = RateLimiter(per_key_per_hour=5, clock=clock)
    sc = SpendCap(max_calls=1)
    guard = Guardrails(rate_limiter=rl, spend_cap=sc)

    assert guard.allow('k', provider='anthropic') is True
    # Spend cap is exhausted; the combined guard blocks and must not burn a rate slot.
    assert guard.allow('k', provider='anthropic') is False
    assert rl.snapshot() == (1, {'k': 1})      # only the one successful call is recorded


class _DrainedLimiter:
    """A rate limiter whose peek says yes but whose consume says no — models a shared
    limiter drained by another provider/thread between Guardrails' check() and allow()."""

    def check(self, key):
        return True

    def allow(self, key):
        return False


def test_guardrails_fails_closed_if_shared_limiter_drained_between_check_and_consume():
    sc = SpendCap(max_calls=5)
    guard = Guardrails(rate_limiter=_DrainedLimiter(), spend_cap=sc)

    # check() passed but allow() rejected -> the call must be blocked...
    assert guard.allow('k', provider='anthropic', cost=1.0) is False
    # ...and the spend cap must NOT have been charged for a call that never ran.
    assert sc.calls == 0
    assert sc.spent('anthropic') == 0.0


def test_guardrails_allows_when_both_allow():
    clock = FakeClock()
    guard = Guardrails(
        rate_limiter=RateLimiter(per_key_per_hour=2, global_per_hour=10, clock=clock),
        spend_cap=SpendCap(max_calls=10, cost_ceiling={'anthropic': 100.0}),
    )
    assert guard.allow('k', provider='anthropic', cost=1.0) is True
    assert guard.allow('k', provider='anthropic', cost=1.0) is True
    assert guard.allow('k', provider='anthropic', cost=1.0) is False   # rate cap hits


def test_guardrails_with_no_guards_always_allows():
    guard = Guardrails()
    for _ in range(10):
        assert guard.allow('k', provider='p', cost=1.0) is True
