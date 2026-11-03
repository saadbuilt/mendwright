"""Declines a worker reuses (mendwright.fleet.DECLINES): when the model looked at a page and
gave no usable heal, that answer is reused per (store file, key, page signature) for
MENDWRIGHT_DECLINE_TTL_SECONDS, by every worker of the process, with its reason and no model
call. The herds are deterministic as in test_herd.py: the fake provider blocks until the test
has seen the followers waiting. No model is called."""

import asyncio
import threading

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired
from mendwright.fleet import DECLINES, FLIGHTS
from mendwright.providers._errors import NOT_HERE_HINT
from test_aio import AsyncFakePage
from test_herd import CODE, KEY, URL, V1, V2, _count, _html, _instant, _journal, _store, _warm

N = 12
GONE = _html("<p class='note'>You accept the terms on the next page.</p>")
NOT_HERE = ('the model says no element on this page fits: the consent checkbox is not on '
            'this page. ' + NOT_HERE_HINT)


class Declining:
    """A provider that counts its calls in calls and blocks each one until release is set,
    then declines as a model does that finds nothing on the page (last_error, code not_here),
    or with a provider failure (error=...)."""

    def __init__(self, calls, release, changed, error=NOT_HERE):
        self.calls, self.release, self.changed, self.error = calls, release, changed, error
        self.last_error = None

    def propose(self, bundle):
        with self.changed:
            self.calls.append(bundle.get('key'))
            self.changed.notify_all()
        assert self.release.wait(60), 'the test never released the provider'
        self.last_error = self.error
        return None

    def report_outcome(self, key, xpath, success):
        pass


@pytest.fixture(autouse=True)
def _no_declines_kept():
    DECLINES.clear()
    yield
    DECLINES.clear()


def _open(make_page, tmp_path, provider, html=GONE, **kwargs):
    page = make_page(html, url=URL)
    kwargs.setdefault('timeout_ms', 0)
    heal = mendwright.open(page, provider=provider, store_path=_store(tmp_path),
                           fingerprint=False, **kwargs)
    return page, heal


def test_a_herd_on_a_decline_makes_one_model_call(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    healers = []
    for _ in range(N):
        _page, heal = _open(make_page, tmp_path, Declining(calls, release, changed),
                            timeout_ms=60000)
        heal._clock = _instant()
        healers.append(heal)
    messages = []

    def run(heal):
        try:
            heal.locator(KEY, CODE).check()
            messages.append('ok')
        except AttentionRequired as exc:
            messages.append(str(exc))
    threads = [threading.Thread(target=run, args=(heal,)) for heal in healers]
    for thread in threads:
        thread.start()
    with changed:
        assert changed.wait_for(lambda: len(calls) == 1, 60)
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    events = _journal(tmp_path)
    assert len(calls) == 1
    assert _count(events, 'heal.declined', code='not_here') == N
    assert _count(events, 'heal.declined', code='not_here', cached=True) == N - 1
    assert _count(events, 'heal.declined', code='rate_limit') == 0
    # Every worker fails with the model's own reason, not with a rate cap.
    assert len(messages) == N and all('no element on this page fits' in m for m in messages)
    assert sum('reuses its answer for 600 seconds' in m for m in messages) == N - 1


def test_the_async_healer_reuses_a_decline_too(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()

    async def main():
        healers = []
        for _ in range(N):
            heal = await mendwright.aio.open(
                AsyncFakePage(make_page(GONE, url=URL)), provider=Declining(calls, release,
                                                                            changed),
                store_path=_store(tmp_path), timeout_ms=60000, fingerprint=False)
            heal._clock = _instant()
            healers.append(heal)
        tasks = [asyncio.ensure_future(h.locator(KEY, CODE).check()) for h in healers]

        def ready():
            with changed:
                changed.wait_for(lambda: len(calls) == 1, 60)
            return FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
        waited = await asyncio.to_thread(ready)
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return waited, results
    waited, results = asyncio.run(main())
    assert waited and all(isinstance(r, AttentionRequired) for r in results)
    events = _journal(tmp_path)
    assert len(calls) == 1
    assert _count(events, 'heal.declined', code='not_here', cached=True) == N - 1


def _declined_once(make_page, tmp_path, stub_provider, error=NOT_HERE, **kwargs):
    """A store whose key one worker healed for nothing: (calls, a second worker)."""
    _warm(make_page, tmp_path, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    release.set()
    _page, first = _open(make_page, tmp_path, Declining(calls, release, changed, error),
                         **kwargs)
    with pytest.raises(AttentionRequired):
        first.locator(KEY, CODE)
    _page, second = _open(make_page, tmp_path, Declining(calls, release, changed, error),
                          **kwargs)
    return calls, second


def test_a_decline_is_reused_for_its_ttl_and_asked_again_after(tmp_path, make_page,
                                                               stub_provider):
    calls, second = _declined_once(make_page, tmp_path, stub_provider)
    now = second.coordinator.wallclock()
    second.coordinator.wallclock = lambda: now + 599
    with pytest.raises(AttentionRequired, match='599 seconds ago'):
        second.locator(KEY, CODE)
    assert len(calls) == 1
    second.coordinator.wallclock = lambda: now + 601
    with pytest.raises(AttentionRequired) as caught:
        second.locator(KEY, CODE)
    assert len(calls) == 2 and 'reuses its answer' not in str(caught.value)


def test_a_ttl_of_zero_asks_the_model_every_time(tmp_path, make_page, stub_provider,
                                                 monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DECLINE_TTL_SECONDS', '0')
    calls, second = _declined_once(make_page, tmp_path, stub_provider)
    with pytest.raises(AttentionRequired):
        second.locator(KEY, CODE)
    assert len(calls) == 2


def test_the_ttl_comes_from_the_environment(tmp_path, make_page, stub_provider, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_DECLINE_TTL_SECONDS', '30')
    calls, second = _declined_once(make_page, tmp_path, stub_provider)
    assert second.coordinator.decline_ttl == 30
    with pytest.raises(AttentionRequired, match='reuses its answer for 30 seconds'):
        second.locator(KEY, CODE)
    assert len(calls) == 1


@pytest.mark.parametrize('raw', ['ten', '-1', '1.5'])
def test_a_bad_ttl_stops_open_and_doctor_lists_it(make_page, tmp_path, monkeypatch, raw):
    from mendwright.cli import _settings_problems
    monkeypatch.setenv('MENDWRIGHT_DECLINE_TTL_SECONDS', raw)
    with pytest.raises(ValueError, match='MENDWRIGHT_DECLINE_TTL_SECONDS must be a whole '
                                         'number of seconds'):
        _open(make_page, tmp_path, None)
    assert any('MENDWRIGHT_DECLINE_TTL_SECONDS' in p for p in _settings_problems())


def test_a_failure_of_the_provider_is_not_reused(tmp_path, make_page, stub_provider):
    calls, second = _declined_once(make_page, tmp_path, stub_provider,
                                   error='the API answered HTTP 429 (rate limited).')
    with pytest.raises(AttentionRequired):
        second.locator(KEY, CODE)
    assert len(calls) == 2          # asking again can work: nothing was reused


def test_a_gate_decline_is_reused(tmp_path, make_page, stub_provider):
    _warm(make_page, tmp_path, stub_provider)
    wrong = stub_provider("//input[@name='email']")     # the wrong role: fails the gate
    _page, first = _open(make_page, tmp_path, wrong)
    with pytest.raises(AttentionRequired, match="passed mendwright's local gate"):
        first.locator(KEY, CODE)
    assert len(wrong.bundles) == 2                      # the attempt and its re-prompt
    again = stub_provider("//input[@name='email']")
    _page, second = _open(make_page, tmp_path, again)
    with pytest.raises(AttentionRequired, match='reuses its answer'):
        second.locator(KEY, CODE)
    assert again.bundles == []


def test_a_decline_on_another_page_is_not_reused(tmp_path, make_page, stub_provider):
    calls, second = _declined_once(make_page, tmp_path, stub_provider,
                                   heal_across_pages=True)
    second._page.url = 'https://shop.test/checkout/express'      # another page signature
    with pytest.raises(AttentionRequired):
        second.locator(KEY, CODE)
    assert len(calls) == 2


def test_a_heal_comes_before_a_reused_decline(tmp_path, make_page, stub_provider):
    calls, second = _declined_once(make_page, tmp_path, stub_provider)
    # The element is back, and another worker heals it; the decline is still in force.
    _page, third = _open(make_page, tmp_path, stub_provider("//input[@data-testid='tos-optin']"),
                         html=V2)
    third.coordinator.decline_ttl = 0            # this one asks the model regardless
    third.locator(KEY, CODE).check()
    second._page.set_html(V2)
    second.locator(KEY, CODE).check()            # adopts the heal: no decline, no call
    assert len(calls) == 1 and second.pending() == {KEY: "//input[@data-testid='tos-optin']"}


def _checkout(body):
    return ('<html><body><main><h1>Checkout</h1><form id="checkout"><label for="email">Email'
            '</label><input id="email" name="email" type="email">' + body + '<button id="pay" '
            'type="submit">Pay now</button></form></main></body></html>')


def test_a_decline_leaves_the_fingerprint_tier_to_heal_another_variant_of_the_page(
        tmp_path, make_page, stub_provider):
    # Two variants of one page path share its signature. On the first the consent moved away,
    # and the model said so; on the second it was redesigned, and the fingerprint tier, which
    # asks no model, heals it.
    path = _store(tmp_path)

    def healer(html, provider):
        return mendwright.open(make_page(html, url=URL, fingerprints=True), provider=provider,
                               store_path=path, timeout_ms=0, fingerprint=True)
    healer(_checkout('<label><input id="accept-terms" name="terms" type="checkbox"><span>I '
                     'accept the terms and conditions</span></label>'), stub_provider(None)
           ).locator(KEY, CODE, role='checkbox').check()
    declining = stub_provider(None, last_error=NOT_HERE)
    with pytest.raises(AttentionRequired):
        healer(GONE, declining).locator(KEY, CODE).check()
    assert len(declining.bundles) == 1
    model = stub_provider(None)
    redesigned = healer(_checkout(
        '<fieldset class="legal"><label class="control"><input type="checkbox" name="agreement" '
        'id="mui-generated-8842" data-testid="legal-consent"><span>I accept the terms and '
        'conditions</span></label></fieldset>'), model)
    redesigned.locator(KEY, CODE).check()
    assert [h.source for h in redesigned.heals] == ['fingerprint'] and model.bundles == []


def _fingerprinted(tmp_path, make_page, stub_provider):
    """The store after a run on V1 that recorded the key's fingerprint and role."""
    path = _store(tmp_path)
    mendwright.open(make_page(V1, url=URL, fingerprints=True), provider=stub_provider(None),
                    store_path=path, timeout_ms=0, fingerprint=True
                    ).locator(KEY, CODE, role='checkbox').check()
    return path


@pytest.fixture
def finds(monkeypatch):
    """Each run of the fingerprint tier's scoring (mendwright.fingerprint.find)."""
    import mendwright.fingerprint as fingerprint
    runs, real = [], fingerprint.find
    monkeypatch.setattr(fingerprint, 'find', lambda *a, **k: runs.append(1) or real(*a, **k))
    return runs


def test_the_page_a_decline_was_made_on_is_not_scored_again(tmp_path, make_page, stub_provider,
                                                            finds):
    # The tier found nothing on the page, then the model declined. A worker on the same page
    # takes that decline without scoring the page again. One on another variant of the page
    # path scores its own first (see the test above). Neither counts as a heal requested.
    path = _fingerprinted(tmp_path, make_page, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    release.set()

    def worker(html):
        heal = mendwright.open(make_page(html, url=URL, fingerprints=True),
                               provider=Declining(calls, release, changed), store_path=path,
                               timeout_ms=0, fingerprint=True)
        with pytest.raises(AttentionRequired) as caught:
            heal.locator(KEY, CODE).check()
        return str(caught.value)

    assert 'reuses its answer' not in worker(GONE)
    assert len(calls) == 1 and len(finds) == 1
    assert 'reuses its answer' in worker(GONE)
    assert len(calls) == 1 and len(finds) == 1
    assert 'reuses its answer' in worker(GONE.replace('the next page', 'the page after this'))
    assert len(calls) == 1 and len(finds) == 2
    events = _journal(tmp_path)
    assert _count(events, 'heal.requested') == 1
    assert _count(events, 'heal.declined', code='not_here', cached=True) == 2


def test_a_herd_on_a_decline_scores_the_page_once(tmp_path, make_page, stub_provider, finds):
    path = _fingerprinted(tmp_path, make_page, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    healers = []
    for _ in range(N):
        heal = mendwright.open(make_page(GONE, url=URL, fingerprints=True),
                               provider=Declining(calls, release, changed), store_path=path,
                               timeout_ms=60000, fingerprint=True)
        heal._clock = _instant()
        healers.append(heal)

    def run(heal):
        with pytest.raises(AttentionRequired):
            heal.locator(KEY, CODE).check()
    threads = [threading.Thread(target=run, args=(heal,)) for heal in healers]
    for thread in threads:
        thread.start()
    with changed:
        assert changed.wait_for(lambda: len(calls) == 1, 60)
    assert FLIGHTS.wait_until(lambda: FLIGHTS.waiting() == N - 1, 60)
    release.set()
    for thread in threads:
        thread.join(60)
    assert not any(t.is_alive() for t in threads)
    events = _journal(tmp_path)
    assert len(calls) == 1 and len(finds) == 1
    assert _count(events, 'heal.requested') == 1
    assert _count(events, 'heal.declined', code='not_here', cached=True) == N - 1


SIGN_IN = ("<html><body><main><form id='login'><button id='signin' name='signin' type='submit'>"
           "Sign in</button></form></main></body></html>")
# Its only Sign in button sits in the desktop navigation, which a stylesheet hides on a phone.
RESPONSIVE = ("<html><body><main><div class='desktop-only'><form><button name='signin' "
              "type='submit' class='nav-desktop'>Sign in</button></form></div></main></body>"
              "</html>")


def _hiding(make_page, hidden):
    """make_page's page, on which a stylesheet hides what is inside class hidden: the live
    page's is_visible() says so, the HTML doesn't."""
    found = type(make_page('<html><body></body></html>').locator('body'))

    class Locator(found):
        def is_visible(self):
            els = self._elements()
            if len(els) == 1 and any(hidden in (node.get('class') or '').split()
                                     for node in (els[0], *els[0].iterancestors())):
                return False
            return super().is_visible()

    class Page(make_page):
        def locator(self, selector):
            return Locator(self, selector)
    return Page


def test_a_decline_made_where_the_live_page_hid_the_element_leaves_the_tier_to_heal_it(
        tmp_path, make_page, stub_provider):
    # One page path and the same HTML on a phone and a desktop. On the phone the tier finds
    # no visible match and the model declines; on the desktop the tier, which asked the live
    # page then, heals from the fingerprint.
    path = _store(tmp_path)
    mendwright.open(make_page(SIGN_IN, url=URL, fingerprints=True), provider=stub_provider(None),
                    store_path=path, timeout_ms=0, fingerprint=True
                    ).locator('signin', '#signin', role='button').click()
    calls, release, changed = [], threading.Event(), threading.Condition()
    release.set()
    phone = mendwright.open(_hiding(make_page, 'desktop-only')(RESPONSIVE, url=URL,
                                                               fingerprints=True),
                            provider=Declining(calls, release, changed), store_path=path,
                            timeout_ms=0, fingerprint=True)
    with pytest.raises(AttentionRequired):
        phone.locator('signin', '#signin').click()
    desktop = mendwright.open(make_page(RESPONSIVE, url=URL, fingerprints=True),
                              provider=Declining(calls, release, changed), store_path=path,
                              timeout_ms=0, fingerprint=True)
    desktop.locator('signin', '#signin').click()
    assert len(calls) == 1 and [h.source for h in desktop.heals] == ['fingerprint']


def test_a_worker_with_no_model_gets_its_own_decline_not_another_s(tmp_path, make_page,
                                                                    stub_provider):
    path = _fingerprinted(tmp_path, make_page, stub_provider)
    calls, release, changed = [], threading.Event(), threading.Condition()
    release.set()
    with pytest.raises(AttentionRequired):
        mendwright.open(make_page(GONE, url=URL, fingerprints=True),
                        provider=Declining(calls, release, changed), store_path=path,
                        timeout_ms=0, fingerprint=True).locator(KEY, CODE).check()
    alone = mendwright.open(make_page(GONE, url=URL, fingerprints=True), provider=None,
                            store_path=path, timeout_ms=0, fingerprint=True)
    with pytest.raises(AttentionRequired, match='no model provider is configured'):
        alone.locator(KEY, CODE).check()
    declined = [e for e in _journal(tmp_path) if e['event'] == 'heal.declined']
    assert declined[-1]['code'] == 'no_provider' and 'cached' not in declined[-1]


def test_a_page_whose_text_holds_a_lone_surrogate_gets_a_digest_too():
    # A driver can hand over text that strict UTF-8 refuses; the lookup must not stop on it.
    from mendwright.fleet import scored_page
    digest = scored_page('<p>\ud83d</p>', {'v': 1, 'tag': 'button'}, 'button')
    assert len(digest) == 64 and digest != scored_page('<p></p>', {'v': 1, 'tag': 'button'},
                                                       'button')
