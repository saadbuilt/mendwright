"""The hourly model budget from the environment: MENDWRIGHT_RATE_PER_KEY_PER_HOUR and
MENDWRIGHT_RATE_GLOBAL_PER_HOUR, the defaults of open()'s two rates."""

import inspect

import pytest

import mendwright
from mendwright import AttentionRequired, cli, facade
from mendwright import aio

PAGE = "<form><button id='b'>Go</button></form>"
BROKEN = "//button[@id='gone']"
FIXED = "//button[@id='b']"


def _limiter(healer):
    return healer.coordinator.guardrails.rate_limiter


def _open(make_page, stub_provider, tmp_path, **kw):
    return mendwright.open(make_page(PAGE), provider=stub_provider(FIXED),
                           store_path=str(tmp_path / 'o.json'), timeout_ms=0, **kw)


def test_without_the_variables_the_budget_is_3_and_60(tmp_path, make_page, stub_provider):
    limiter = _limiter(_open(make_page, stub_provider, tmp_path))
    assert (limiter.per_key_per_hour, limiter.global_per_hour) == (3, 60)


def test_the_variables_set_the_budget(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', ' 7 ')
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', '900')
    limiter = _limiter(_open(make_page, stub_provider, tmp_path))
    assert (limiter.per_key_per_hour, limiter.global_per_hour) == (7, 900)


def test_none_in_a_variable_removes_that_limit(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', 'None')
    limiter = _limiter(_open(make_page, stub_provider, tmp_path))
    assert (limiter.per_key_per_hour, limiter.global_per_hour) == (None, 60)


def test_an_empty_variable_counts_as_unset(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', '  ')
    assert _limiter(_open(make_page, stub_provider, tmp_path)).global_per_hour == 60


def test_a_rate_passed_to_open_wins_over_the_variable(tmp_path, make_page, stub_provider,
                                                      monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', '7')
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', '900')
    limiter = _limiter(_open(make_page, stub_provider, tmp_path,
                             rate_per_key_per_hour=2, rate_global_per_hour=None))
    assert (limiter.per_key_per_hour, limiter.global_per_hour) == (2, None)


def test_a_rate_of_3_passed_to_open_wins_over_the_variable_too(tmp_path, make_page,
                                                               stub_provider, monkeypatch):
    # The same number as the built-in default is still the caller's own choice.
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', '7')
    limiter = _limiter(_open(make_page, stub_provider, tmp_path, rate_per_key_per_hour=3))
    assert limiter.per_key_per_hour == 3


def test_zero_lets_no_model_attempt_through(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', '0')
    provider = stub_provider(FIXED)
    healer = mendwright.open(make_page(PAGE), provider=provider, fingerprint=False,
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    with pytest.raises(AttentionRequired, match='rate/spend cap') as exc:
        healer.locator('go', BROKEN, role='button')
    assert provider.bundles == []
    assert str(exc.value).endswith(
        "mendwright did not ask the model to repair 'go': rate_per_key_per_hour is 0, which lets "
        'no model attempt through (the rate/spend cap of a direct provider; each attempt makes up '
        'to 2 model calls). It fails closed. MENDWRIGHT_RATE_PER_KEY_PER_HOUR sets this cap.')


@pytest.mark.parametrize('raw', ['ten', '-1', '2.5', 'unlimited'])
@pytest.mark.parametrize('var, param', [
    ('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', 'rate_per_key_per_hour'),
    ('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', 'rate_global_per_hour'),
])
def test_a_bad_value_stops_open_and_names_the_variable(tmp_path, make_page, stub_provider,
                                                       monkeypatch, raw, var, param):
    monkeypatch.setenv(var, raw)
    with pytest.raises(ValueError) as exc:
        _open(make_page, stub_provider, tmp_path)
    assert var + ' must be a whole number of heal attempts per hour, or none' in str(exc.value)
    assert param + '= to mendwright.open()' in str(exc.value)


def test_a_bad_value_is_ignored_when_both_rates_are_passed(tmp_path, make_page,
                                                           stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', 'ten')
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', 'lots')
    limiter = _limiter(_open(make_page, stub_provider, tmp_path,
                             rate_per_key_per_hour=4, rate_global_per_hour=40))
    assert (limiter.per_key_per_hour, limiter.global_per_hour) == (4, 40)


def test_the_async_open_has_the_same_defaults():
    for name in ('rate_per_key_per_hour', 'rate_global_per_hour'):
        assert (inspect.signature(aio.open).parameters[name].default
                is inspect.signature(mendwright.open).parameters[name].default)
    assert repr(facade.RATE_PER_KEY_DEFAULT) == 'MENDWRIGHT_RATE_PER_KEY_PER_HOUR or 3'
    assert repr(facade.RATE_GLOBAL_DEFAULT) == 'MENDWRIGHT_RATE_GLOBAL_PER_HOUR or 60'


def test_doctor_shows_the_variables(capsys, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', '10')
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', 'none')
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out.splitlines()
    assert '  MENDWRIGHT_RATE_PER_KEY_PER_HOUR=10' in out
    assert '  MENDWRIGHT_RATE_GLOBAL_PER_HOUR=none' in out
    assert 'Problems' not in out


def test_doctor_lists_a_bad_value_and_still_exits_0(capsys, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', 'lots')
    assert cli.main(['doctor']) == 0
    out = capsys.readouterr().out.splitlines()
    problems = out[out.index('Problems') + 1:]
    assert any('MENDWRIGHT_RATE_GLOBAL_PER_HOUR must be a whole number' in line
               for line in problems)


GO = "<html><body><form><button id='b'>Go</button><button id='c'>Stop</button></form></body></html>"


def _try(heal, key='k', selector="//button[@id='gone']"):
    with pytest.raises(AttentionRequired) as caught:
        heal.locator(key, selector, role='button')
    return str(caught.value)


def test_a_refusal_by_the_shared_budget_spends_none_of_this_process_slots(
        tmp_path, make_page, stub_provider, monkeypatch):
    # Two Healers with limiters of their own stand for two processes on one store: they share
    # the key's record. A's 3 attempts use up k's budget for both. B's refusals by that budget
    # spend no slot of B's own, so they never say B made attempts it did not make, and never
    # stop another key B could heal.
    monkeypatch.setenv('MENDWRIGHT_DECLINE_TTL_SECONDS', '0')     # nothing reused
    path = str(tmp_path / 'o.json')

    def healer(provider, global_cap):
        return mendwright.open(make_page(GO), provider=provider, store_path=path, timeout_ms=0,
                               fingerprint=False, rate_per_key_per_hour=3,
                               rate_global_per_hour=global_cap)
    a = healer(stub_provider(None), 60)
    for _ in range(3):
        _try(a)
    b_model = stub_provider("//button[@id='c']")
    b = healer(b_model, 2)
    assert b.coordinator.guardrails.rate_limiter is not a.coordinator.guardrails.rate_limiter
    for _ in range(3):
        assert 'the workers that share this store already made 3 heal attempts' in _try(b)
    assert b.coordinator.guardrails.rate_limiter.snapshot() == (0, {})
    assert b_model.bundles == []
    b.locator('stop', "//button[@id='stop-gone']", role='button').click()    # healed
    assert len(b_model.bundles) == 1 and b.heals[-1].new == "//button[@id='c']"


def _refused_twice(heal, first='a', second='a'):
    """The last line of the error of a heal the cap stops: a first broken key uses the one
    attempt the caps allow, and the second is refused."""
    with pytest.raises(AttentionRequired):
        heal.locator(first, "//button[@id='gone-" + first + "']", role='button')
    with pytest.raises(AttentionRequired) as caught:
        heal.locator(second, "//button[@id='gone-" + second + "']", role='button')
    return str(caught.value).splitlines()[-1]


def test_the_cap_message_names_the_variable_of_a_rate_not_passed(tmp_path, make_page,
                                                                  stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_RATE_PER_KEY_PER_HOUR', '1')
    heal = mendwright.open(make_page(GO), provider=stub_provider(None), fingerprint=False,
                           store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    text = _refused_twice(heal)
    assert 'rate_per_key_per_hour is 1' in text
    assert text.endswith('It fails closed, and asks again once older attempts drop out of the '
                         'hour. MENDWRIGHT_RATE_PER_KEY_PER_HOUR sets this cap.')


def test_the_cap_message_names_the_argument_of_a_rate_passed_to_open(tmp_path, make_page,
                                                                     stub_provider, monkeypatch):
    # The variable changes nothing then: naming it would send you to the wrong place.
    monkeypatch.setenv('MENDWRIGHT_RATE_GLOBAL_PER_HOUR', '100')
    heal = mendwright.open(make_page(GO), provider=stub_provider(None), fingerprint=False,
                           rate_global_per_hour=1, store_path=str(tmp_path / 'o.json'),
                           timeout_ms=0)
    text = _refused_twice(heal, 'a', 'b')
    assert 'rate_global_per_hour is 1' in text and 'MENDWRIGHT_RATE' not in text
    assert text.endswith('The rate_global_per_hour= passed to mendwright.open() sets this cap.')


def test_the_spend_cap_message_says_it_holds_for_the_run(tmp_path, make_page, stub_provider):
    from mendwright.limits import Guardrails, RateLimiter, SpendCap
    guard = Guardrails(RateLimiter(per_key_per_hour=None, global_per_hour=None),
                       SpendCap(max_calls=1))
    heal = mendwright.open(make_page(GO), provider=stub_provider(None), guardrails=guard,
                           store_path=str(tmp_path / 'o.json'), timeout_ms=0, fingerprint=False)
    text = _refused_twice(heal, 'a', 'b')
    assert 'this run already made the 1 heal calls its spend cap allows' in text
    assert text.endswith('It fails closed for the rest of this run. The SpendCap of the guardrails '
                         'passed to mendwright.open() sets it.')
    assert 'hour' not in text.split('model calls).')[-1] and 'MENDWRIGHT_RATE' not in text
