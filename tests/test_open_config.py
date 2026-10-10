"""mendwright.open() settings: timeout_ms, MENDWRIGHT_* environment variables, the
journal for every provider type, and one rate limiter per store file."""

import json
import os

import pytest

import mendwright
from mendwright import AttentionRequired, GatewayProvider
from mendwright import facade
from mendwright.limits import RateLimiter

PAGE = "<form><button id='b'>Go</button></form>"
BROKEN = "//button[@id='gone']"
FIXED = "//button[@id='b']"


def _record_timeouts(monkeypatch):
    seen = []
    real = facade.resolve

    def recording(store, page, key, **kw):
        seen.append(kw.get('timeout_ms'))
        return real(store, page, key, **kw)

    monkeypatch.setattr(facade, 'resolve', recording)
    return seen


# --- timeout_ms -----------------------------------------------------------------------

def test_timeout_ms_is_the_default_for_every_locator(tmp_path, make_page, stub_provider,
                                                     monkeypatch):
    seen = _record_timeouts(monkeypatch)
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    assert healer.timeout_ms == 0
    healer.locator('go', FIXED, role='button')
    healer.locator('go', FIXED, role='button', timeout_ms=7)
    assert seen == [0, 7]


def test_the_default_timeout_is_ten_seconds(tmp_path, make_page, stub_provider):
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    assert healer.timeout_ms == 10000


def test_timeout_from_the_environment_and_the_argument_wins(tmp_path, make_page, stub_provider,
                                                            monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_TIMEOUT_MS', '2500')
    page = make_page(PAGE)
    sp = str(tmp_path / 'o.json')
    assert mendwright.open(page, provider=stub_provider(None), store_path=sp).timeout_ms == 2500
    assert mendwright.open(page, provider=stub_provider(None), store_path=sp,
                           timeout_ms=100).timeout_ms == 100
    monkeypatch.setenv('MENDWRIGHT_TIMEOUT_MS', 'ten')
    with pytest.raises(ValueError, match='MENDWRIGHT_TIMEOUT_MS must be a whole number'):
        mendwright.open(page, provider=stub_provider(None), store_path=sp)


# --- MENDWRIGHT_STORE -----------------------------------------------------------------

def test_store_path_from_the_environment(tmp_path, make_page, stub_provider, monkeypatch):
    target = tmp_path / 'from-env' / 'o.json'
    monkeypatch.setenv('MENDWRIGHT_STORE', str(target))
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None), timeout_ms=0)
    healer.locator('go', FIXED, role='button')
    assert 'go' in json.loads(target.read_text(encoding='utf-8'))['selectors']


def test_an_explicit_store_path_wins_over_the_environment(tmp_path, make_page, stub_provider,
                                                          monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_STORE', str(tmp_path / 'env.json'))
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None), timeout_ms=0,
                             store_path=str(tmp_path / 'arg.json'))
    healer.locator('go', FIXED, role='button')
    assert (tmp_path / 'arg.json').exists()
    assert not (tmp_path / 'env.json').exists()


def test_a_tilde_store_path_means_home(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MENDWRIGHT_STORE', '~/mw/o.json')
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None), timeout_ms=0)
    healer.locator('go', FIXED, role='button')
    assert (tmp_path / 'mw' / 'o.json').exists()
    assert not (tmp_path / '~').exists()


# --- MENDWRIGHT_DRY_RUN / MENDWRIGHT_DISABLE ----------------------------------------------

def test_dry_run_from_the_environment_and_the_argument_wins(tmp_path, make_page, stub_provider,
                                                            monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DRY_RUN', '1')
    sp = str(tmp_path / 'o.json')
    assert mendwright.open(make_page(PAGE), provider=stub_provider(None),
                           store_path=sp).coordinator.dry_run is True
    assert mendwright.open(make_page(PAGE), provider=stub_provider(None), store_path=sp,
                           dry_run=False).coordinator.dry_run is False
    monkeypatch.setenv('MENDWRIGHT_DRY_RUN', 'maybe')
    with pytest.raises(ValueError, match='MENDWRIGHT_DRY_RUN must be 1 or 0'):
        mendwright.open(make_page(PAGE), provider=stub_provider(None), store_path=sp)


def test_disable_resolves_but_never_heals(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    provider = stub_provider(FIXED)
    healer = mendwright.open(make_page(PAGE), provider=provider,
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    assert healer.locator('go', FIXED, role='button').count() == 1
    with pytest.raises(AttentionRequired, match='MENDWRIGHT_DISABLE'):
        healer.locator('other', BROKEN, role='button')
    assert provider.bundles == []


def test_disable_zero_leaves_healing_on(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '0')
    provider = stub_provider(FIXED)
    healer = mendwright.open(make_page(PAGE), provider=provider,
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    assert healer.locator('go', BROKEN, role='button').count() == 1


# --- journal --------------------------------------------------------------------------

class ModelProvider:
    model = 'test-model-1'

    def propose(self, bundle):
        return {'xpath': FIXED}

    def report_outcome(self, *a):
        pass


class FakeHttp:
    def post(self, url, *, json=None, headers=None, timeout=None):
        class Resp:
            status_code = 200

            def json(self_):  # noqa: N805
                return {'status': 'ok', 'xpath': FIXED}
        return Resp()


def _journal(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


@pytest.mark.parametrize('provider,name,model', [
    (ModelProvider(), 'ModelProvider', 'test-model-1'),
    (GatewayProvider('https://gw.example', 'k', http=FakeHttp()), 'GatewayProvider', None),
])
def test_every_provider_type_writes_the_journal(provider, name, model, tmp_path, make_page):
    healer = mendwright.open(make_page(PAGE), provider=provider,
                             store_path=str(tmp_path / 'o.json'), timeout_ms=0)
    healer.locator('go', BROKEN, role='button')
    events = _journal(tmp_path / 'journal.jsonl')
    applied = [e for e in events if e['event'] == 'heal.applied']
    assert len(applied) == 1
    entry = applied[0]
    assert entry['key'] == 'go'
    assert entry['old'] == [BROKEN]
    assert entry['xpath'] == FIXED
    assert entry['provider'] == name
    assert entry.get('model') == model
    assert isinstance(entry['latency_ms'], int) and entry['latency_ms'] >= 0


# --- one rate limiter per store file ----------------------------------------------------

def test_healers_on_one_store_share_the_hourly_cap(tmp_path, make_page, monkeypatch):
    monkeypatch.setattr(facade, 'RateLimiter',
                        lambda **kw: RateLimiter(clock=lambda: 0.0, **kw))
    sp = str(tmp_path / 'o.json')
    healed = 0
    for i in range(5):
        # Each page renames the button again, so earlier heals no longer match.
        page = make_page("<form><button id='v%d'>Go</button></form>" % i)

        class Fresh:
            def propose(self, bundle, i=i):
                return {'xpath': "//button[@id='v%d']" % i}

            def report_outcome(self, *a):
                pass

        healer = mendwright.open(page, provider=Fresh(), store_path=sp, timeout_ms=0,
                                 rate_per_key_per_hour=3)
        try:
            healer.locator('go', BROKEN, role='button')
            healed += 1
        except AttentionRequired as exc:
            assert 'rate/spend cap' in str(exc)
    assert healed == 3


def test_different_store_files_have_their_own_budget(tmp_path, make_page, stub_provider):
    a = mendwright.open(make_page(PAGE), provider=stub_provider(FIXED),
                        store_path=str(tmp_path / 'a.json'), rate_per_key_per_hour=1)
    b = mendwright.open(make_page(PAGE), provider=stub_provider(FIXED),
                        store_path=str(tmp_path / 'b.json'), rate_per_key_per_hour=1)
    assert a.coordinator.guardrails.rate_limiter is not b.coordinator.guardrails.rate_limiter
    same = mendwright.open(make_page(PAGE), provider=stub_provider(FIXED),
                           store_path=os.path.join(str(tmp_path), '.', 'a.json'),
                           rate_per_key_per_hour=1)
    assert same.coordinator.guardrails.rate_limiter is a.coordinator.guardrails.rate_limiter
