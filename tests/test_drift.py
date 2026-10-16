"""heal.drift(): one verdict per key from the decisions a run makes, offline.

A store is recorded the way a first run records it, by looking the keys up with
mendwright.open() on v1 of a signup page (conftest's FakePage, which answers the
fingerprint read). Then the page changes to v2 and a new healer on the same store checks
drift. Every check goes through drifted(), which also asserts what heal.drift() must never
do: write the store file, write the journal, call a store writer, act on the page, ask the
provider, log more than its one INFO line, or change what the healer holds.
"""

import json
import logging

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, drift
from mendwright.coordinator import _destructive_message
from mendwright.drift import DriftReport, KeyDrift
from mendwright.flow import flow_change_message
from mendwright.store import load_store

URL = 'https://shop.example/signup'

V1 = """<form id='signup'>
  <label for='email'>Email</label><input id='email' name='email' type='email'>
  <label><input type='checkbox' id='accept-terms' name='terms'> I accept the terms</label>
  <label><input type='checkbox' id='news' class='opt'> Send me the newsletter</label>
  <button type='button' id='create-account'>Create account</button>
  <button type='button' id='pay'>Pay now</button>
  <span id='total'>10 EUR</span>
</form>"""

V2 = """<form id='signup'>
  <label for='email'>Email</label><input id='email' name='email' type='email'>
  <label><input type='checkbox' name='terms' data-testid='legal-consent'> I accept the
    terms</label>
  <label><input type='checkbox' class='opt'> Send me the newsletter</label>
  <label><input type='checkbox' class='opt'> Send me the newsletter</label>
  <button type='button' id='create-account-v2'>Create account</button>
  <button type='button' id='pay-now'>Pay now</button>
  <div id='total'>10 EUR</div>
</form>"""

KEYS = [('email', '#email', 'input'), ('accept_terms', '#accept-terms', 'checkbox'),
        ('newsletter', '#news', 'checkbox'), ('create_account', '#create-account', 'button'),
        ('pay', '#pay', 'button'), ('total', '#total', 'span')]

# The store writers heal.drift() must never call on the healer's store.
WRITERS = ('set_code_candidates', 'set_meta', 'set_last_page', 'set_last_step',
           'set_last_url', 'record_success', 'add_candidate', 'quarantine', 'guess_role',
           '_persist_override', '_write')


def _store(tmp_path):
    return str(tmp_path / 'store' / 'overrides.json')


def record(make_page, stub_provider, tmp_path, html=V1, keys=KEYS, url=URL, destructive=('pay',),
           **open_kw):
    """A first run: look each key up on html, so the store holds its selector, role, page,
    address and fingerprint. Returns the page."""
    page = make_page(html, url=url, fingerprints=True)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           **open_kw)
    for key, selector, role in keys:
        heal.locator(key, selector, role=role, destructive=True if key in destructive else None,
                     timeout_ms=0, poll_ms=0)
    return page


class NoJournal:
    """A journal that fails the test on any event."""

    def __getattr__(self, name):
        def event(*args, **kwargs):
            raise AssertionError('heal.drift() wrote a journal event: ' + name)
        return event


def _spy_writers(store):
    """Make every store writer raise, on the healer's store only: its snapshot, which the
    heal preview writes to, gets the real ones back."""
    def raiser(name):
        def write(*args, **kwargs):
            raise AssertionError('heal.drift() called store.' + name)
        return write
    real_snapshot = store.snapshot
    for name in WRITERS:
        setattr(store, name, raiser(name))

    def snapshot():
        copy = real_snapshot()
        for name in WRITERS:
            copy.__dict__.pop(name, None)
        return copy
    store.snapshot = snapshot


def _limiter_state():
    from mendwright import facade
    return [limiter.snapshot() for limiter in facade._LIMITERS.values()]


def drifted(heal, page, caplog, keys=None, **kwargs):
    """heal.drift(keys, **kwargs), checking everything it must leave alone."""
    store = heal.store
    path = store._override_path
    journal = path.parent / 'journal.jsonl'
    before = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    journal_before = journal.read_bytes() if journal.exists() else None
    files = sorted(p.name for p in path.parent.iterdir()) if path.parent.exists() else []
    _spy_writers(store)
    heal.coordinator.journal = NoJournal()
    reasons = dict(heal.coordinator.heal_reasons)
    provider = heal.coordinator.provider
    limits = _limiter_state()
    actions = list(page.actions)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger='mendwright'):
        report = heal.drift(keys, **kwargs)
    assert isinstance(report, DriftReport)
    # Nothing written: the store file, the journal, no lock file.
    assert ((path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None) == before
    assert (journal.read_bytes() if journal.exists() else None) == journal_before
    assert (sorted(p.name for p in path.parent.iterdir()) if path.parent.exists() else []) == files
    # Nothing done to the page, no model asked, one INFO line.
    assert page.actions == actions
    assert getattr(provider, 'bundles', []) == [] and getattr(provider, 'outcomes', []) == []
    records = [r for r in caplog.records if r.name == 'mendwright']
    assert [(r.levelno, r.getMessage().split(':')[0]) for r in records] == [
        (logging.INFO, 'mendwright')], [r.getMessage() for r in records]
    assert records[0].getMessage() == 'mendwright: ' + report.summary()
    # The healer holds what it held.
    assert heal.heals == [] and heal.pending() == {} and heal.handled_interruptions == []
    assert heal.coordinator.heal_reasons == reasons
    assert _limiter_state() == limits
    return report


def checker(make_page, stub_provider, tmp_path, html=V2, provider=None, **open_kw):
    """A healer on the recorded store, on html (v2 by default): (heal, page)."""
    page = make_page(html, url=open_kw.pop('url', URL), fingerprints=True)
    heal = mendwright.open(page, provider=provider or stub_provider(None),
                           store_path=_store(tmp_path), timeout_ms=0, **open_kw)
    return heal, page


def by_key(report):
    return {k.key: k for k in report.keys}


# --- the verdicts ----------------------------------------------------------------------------

def test_a_redesign_gets_one_verdict_per_key(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    report = drifted(heal, page, caplog)
    got = by_key(report)
    assert {k: v.verdict for k, v in got.items()} == {
        'email': 'ok', 'accept_terms': 'would-heal', 'newsletter': 'needs-model',
        'create_account': 'would-heal', 'pay': 'refused', 'total': 'ok'}
    assert [k.key for k in report.keys] == sorted(got)       # the keys of this page, sorted
    assert report.page == '/signup' and report.url == URL
    assert not report.ok and {k.key for k in report.broken} == {
        'accept_terms', 'newsletter', 'create_account', 'pay'}
    assert got['email'].reason == '#email matches one visible input.'
    assert got['email'].counts_as == 'ok' and got['email'].used == '#email'


def test_ok_reads_the_role_of_the_element(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, html=V1)
    report = drifted(heal, page, caplog)
    assert report.ok and {k.verdict for k in report.keys} == {'ok'}
    email = by_key(report)['email']
    assert (email.role, email.live_role) == ('input', 'input')
    assert [p.result for p in email.probes] == ['one visible element']
    assert report.summary() == 'drift on /signup: 6 keys, 6 ok'


def _with_heal(tmp_path, html, journal_lines=()):
    """A store where 'create_account' has its code selector and a heal saved after it."""
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('create_account', ['#create-account'])
    store.add_candidate('create_account', "//button[normalize-space(.)='Create account']",
                        source='heal', now='2026-10-10T09:00:00+00:00')
    store.set_meta('create_account', role='button')
    store.set_last_page('create_account', '/signup')
    store.set_last_url('create_account', URL)
    if journal_lines:
        (tmp_path / 'store' / 'journal.jsonl').write_text(
            ''.join(json.dumps(line) + '\n' for line in journal_lines), encoding='utf-8')


HEALED = "//button[normalize-space(.)='Create account']"


@pytest.mark.parametrize('success, confirmed', [(True, True), (False, False), (None, None)])
def test_ok_heal_says_whether_a_test_confirmed_the_heal(make_page, stub_provider, tmp_path,
                                                         caplog, success, confirmed):
    lines = [] if success is None else [{'ts': '2026-10-10T09:00:01+00:00',
                                         'event': 'heal.canary', 'key': 'create_account',
                                         'xpath': HEALED, 'success': success}]
    _with_heal(tmp_path, V2, lines)
    heal, page = checker(make_page, stub_provider, tmp_path)
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.used, k.confirmed) == ('ok-heal', HEALED, confirmed)
    assert k.reason.startswith('The selectors before it miss (#create-account: no match); the '
                               'heal saved on 2026-10-10, ' + HEALED + ', matches one visible '
                               'button, and a run uses it. Run mendwright fix to put it in '
                               'your code.')
    assert ('No passing test confirmed it' in k.reason) is (confirmed is not True)
    assert not k.fails() and k.fails(strict=True) and k.fails(fail_on='unhealable', strict=True)


def test_a_control_of_another_role_is_wrong_role(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path, keys=[('submit', '#submit', 'button')],
           html="<form><button id='submit'>Send</button></form>")
    heal, page = checker(make_page, stub_provider, tmp_path,
                         html="<form><div id='submit'>Send</div></form>")
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.role, k.live_role, k.counts_as) == ('wrong-role', 'button', 'div',
                                                             'broken')
    assert k.reason == ("#submit matches one visible element, but it is a div, and 'submit' is "
                        'a button. A run acts on it all the same: mendwright compares roles '
                        'only for heals. Check the selector in your code.')
    assert k.fails() and k.fails(fail_on='unhealable')


def test_another_tag_on_a_key_read_from_is_only_noted(make_page, stub_provider, tmp_path,
                                                      caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    total = by_key(drifted(heal, page, caplog, ['total']))['total']
    assert total.verdict == 'ok' and total.live_role == 'div'
    assert total.reason == ("#total matches one visible div. Its tag changed from span to div, "
                            "which isn't counted: neither is a control.")


def test_a_role_that_is_not_known_yet_is_not_compared(make_page, stub_provider, tmp_path,
                                                      caplog):
    store = load_store(None, _store(tmp_path))
    store.add_candidate('total', '#total', source='code')
    store.set_last_page('total', '/signup')
    heal, page = checker(make_page, stub_provider, tmp_path)
    [k] = drifted(heal, page, caplog, ['total']).keys
    assert k.verdict == 'ok' and k.role is None
    assert k.reason.endswith(" Its role isn't known yet, so it wasn't compared.")


def test_a_heal_from_memory_is_reported_with_its_score(make_page, stub_provider, tmp_path,
                                                       caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    k = by_key(drifted(heal, page, caplog, ['accept_terms']))['accept_terms']
    assert (k.verdict, k.detail, k.counts_as) == ('would-heal', 'fingerprint', 'broken')
    assert k.heal['source'] == 'fingerprint' and k.heal['frame'] is None
    assert k.heal['selector'] == "//input[@data-testid='legal-consent']"
    assert 0 < k.heal['runner_up'] < k.heal['score'] <= 1
    assert k.reason == ('#accept-terms matches nothing. A run would heal it from memory to '
                        "//input[@data-testid='legal-consent'] (fingerprint score "
                        + format(k.heal['score'], '.2f') + ', next best '
                        + format(k.heal['runner_up'], '.2f') + '), which passed every check of '
                        'a heal on this page. Nothing was saved.')
    assert k.fails() and not k.fails(fail_on='unhealable')
    # The store still has no heal for it.
    assert [c['xpath'] for c in load_store(None, _store(tmp_path)).candidates('accept_terms')] \
        == ['#accept-terms']


def test_lookalikes_with_no_provider_are_refused_in_the_run_s_words(make_page, stub_provider,
                                                                     tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, provider=None)
    heal.coordinator.provider = mendwright.facade._NullProvider()
    k = by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter']
    assert k.verdict == 'refused'
    assert k.reason.startswith("#news matches nothing. A run would not heal it: the fingerprint "
                               "match for 'newsletter' was not confident")
    assert k.reason.endswith(', and no model provider is configured to try instead. '
                             + mendwright.facade._NullProvider._DEFAULT)
    assert k.fails(fail_on='unhealable')


def test_a_configured_provider_gives_needs_model_and_is_never_asked(make_page, stub_provider,
                                                                    tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    provider = stub_provider('//input')
    provider.model = 'claude-x'
    heal, page = checker(make_page, stub_provider, tmp_path, provider=provider)
    k = by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter']
    assert (k.verdict, k.provider) == ('needs-model', 'StubProvider')
    assert k.reason.startswith("#news matches nothing, and the fingerprint match for "
                               "'newsletter' was not confident")
    assert k.reason.endswith('. A run would ask StubProvider, model claude-x. heal.drift() '
                             'asks no model.')
    assert provider.bundles == [] and provider.outcomes == []
    assert k.fails() and not k.fails(fail_on='unhealable')


def test_a_destructive_key_that_misses_is_refused_before_any_fingerprint(
        make_page, stub_provider, tmp_path, caplog, monkeypatch):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    called = []
    monkeypatch.setattr(mendwright.fingerprint, 'find',
                        lambda *a, **kw: called.append(a) or None)
    [k] = drifted(heal, page, caplog, ['pay']).keys
    assert (k.verdict, k.destructive) == ('refused', True)
    assert k.reason == '#pay matches nothing. A run would not heal it: ' + _destructive_message('pay')
    assert called == []


def test_a_destructive_key_matched_through_a_heal_is_refused_and_nothing_quarantined(
        make_page, stub_provider, tmp_path, caplog):
    _with_heal(tmp_path, V2)
    store = load_store(None, _store(tmp_path))
    store.set_meta('create_account', destructive=True)
    heal, page = checker(make_page, stub_provider, tmp_path)
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.used) == ('refused', HEALED)
    assert k.reason == ('it matches only through ' + HEALED + ", a selector a heal saved, and "
                        "'create_account' is marked as a destructive control: a run quarantines "
                        'that heal and raises AttentionRequired. heal.drift() quarantined '
                        'nothing. Fix the selector in your code.')
    assert load_store(None, _store(tmp_path)).status('create_account', HEALED) == 'active'
    assert not (tmp_path / 'store' / 'journal.jsonl').exists()


def test_destructive_keys_of_open_count(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, destructive_keys={'create_account'})
    assert by_key(drifted(heal, page, caplog, ['create_account']))[
        'create_account'].verdict == 'refused'
    heal, page = checker(make_page, stub_provider, tmp_path)
    assert by_key(drifted(heal, page, caplog, ['create_account']))[
        'create_account'].verdict == 'would-heal'


def test_what_each_selector_matched_is_reported(make_page, stub_provider, tmp_path, caplog):
    store = load_store(None, _store(tmp_path))
    for key, selectors, role in (('several', ['//button'], 'button'),
                                 ('hidden', ['#ghost'], 'button'),
                                 ('box', ['#box'], 'checkbox'),
                                 ('first', ['#nope', '#go', '#later'], 'button')):
        store.set_code_candidates(key, selectors)
        store.set_meta(key, role=role)
        store.set_last_page(key, '/signup')
    html = ("<form><button id='go'>Go</button><button id='later'>Later</button>"
            "<button id='ghost' style='display:none'>Ghost</button>"
            "<input type='checkbox' id='box' style='display:none'></form>")
    heal, page = checker(make_page, stub_provider, tmp_path, html=html)
    got = by_key(drifted(heal, page, caplog))
    assert [p.result for p in got['several'].probes] == ['matches 3 elements']
    assert got['several'].probes[0].matches == 3 and got['several'].probes[0].visible is None
    assert [p.result for p in got['hidden'].probes] == ['one hidden element']
    assert got['hidden'].probes[0].visible is False
    assert got['box'].verdict == 'ok'
    assert [p.result for p in got['box'].probes] == ['one hidden checkbox']
    assert got['box'].reason == '#box matches one hidden checkbox.'
    assert [p.result for p in got['first'].probes] == ['no match', 'one visible element',
                                                       'not tried']
    assert got['several'].reason.startswith('//button matches 3 elements, and ')
    assert got['hidden'].reason.startswith('#ghost matches one hidden element, and ')


def _selenium_page(html, url=URL):
    """A Selenium driver over an lxml document (XPath only), for heal.drift() on Selenium."""
    from mendwright.drivers.selenium import SeleniumDriver
    doc = lxml.html.fromstring(html)

    class Element:
        def __init__(self, el):
            self.el = el

        def is_displayed(self):
            return 'display:none' not in (self.el.get('style') or '').replace(' ', '')

        def get_attribute(self, name):
            return lxml.html.tostring(self.el, encoding='unicode') if name == 'outerHTML' else None

    class WebDriver:
        current_url = url
        switch_to = None
        actions = []

        def find_elements(self, by, value):
            return [Element(e) for e in doc.xpath(value) if hasattr(e, 'tag')]

        def execute_script(self, script, *args):
            if 'outerHTML' in script and args:
                return {'tag': args[0].el.tag, 'html': args[0].get_attribute('outerHTML')}
            return None

        @property
        def page_source(self):
            return lxml.html.tostring(doc, encoding='unicode')

    driver = SeleniumDriver(WebDriver())
    driver.actions = []
    return driver


def test_a_playwright_selector_on_selenium_cannot_run(stub_provider, tmp_path, caplog):
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('save', ['role=button[name="Save"]'])
    store.set_meta('save', role='button')
    store.set_last_page('save', '/signup')
    driver = _selenium_page("<form><button>Save</button></form>")
    heal = mendwright.open(driver, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, driver, caplog).keys
    assert (k.verdict, k.detail, k.counts_as) == ('skipped', 'unrunnable', 'unchecked')
    assert k.reason == ('none of its selectors can run here: \'role=button[name="Save"]\' is a '
                        'Playwright selector, and Selenium cannot run it.')
    assert [p.result for p in k.probes] == [
        'cannot run: \'role=button[name="Save"]\' is a Playwright selector, and Selenium cannot '
        'run it.']
    assert not k.fails() and not k.fails(fail_on='unhealable')


def test_heal_drift_runs_on_selenium(stub_provider, tmp_path, caplog):
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('email', ["//input[@id='email']"])
    store.set_meta('email', role='input')
    store.set_last_page('email', '/signup')
    driver = _selenium_page("<form><input id='email' name='email'></form>")
    heal = mendwright.open(driver, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, driver, caplog).keys
    assert (k.verdict, k.live_role) == ('ok', 'input')


# --- iframes ---------------------------------------------------------------------------------

FRAME = 'iframe[name="consent"]'
OUTER = "<main><h1>Sign up</h1><iframe name='consent'></iframe></main>"
INNER = "<label><input type='checkbox' id='inner-terms' name='agree'> I agree</label>"


def _find(doc, selector):
    body = selector
    for prefix in ('xpath=', 'css='):
        if body.startswith(prefix):
            body = body[len(prefix):]
    if body.startswith('#'):
        return doc.xpath('//*[@id=$i]', i=body[1:])
    if body.startswith('iframe['):
        return doc.xpath('//iframe')
    try:
        return [e for e in doc.xpath(body) if hasattr(e, 'tag')]
    except Exception:
        return []


class Doc:
    """A driver over one lxml document; the inner one is reached through FRAME. It records
    every action, which heal.drift() must never make."""

    def __init__(self, html, inner=None, url=URL):
        self.doc = lxml.html.fromstring(html)
        self.inner = inner
        self._url = url
        self.actions = []
        self.frame_sel = FRAME

    def _doc(self, frame_chain):
        if tuple(frame_chain) == (self.frame_sel,):
            return self.inner.doc
        assert not frame_chain, frame_chain
        return self.doc

    def count(self, selector, frame_chain=()):
        return len(_find(self._doc(frame_chain), selector))

    def is_visible(self, selector, frame_chain=()):
        return self.count(selector, frame_chain) == 1

    def scroll_into_view(self, selector, frame_chain=()):
        self.actions.append(('scroll', selector))

    def handle(self, selector, frame_chain=()):
        self.actions.append(('handle', selector))
        return (selector, tuple(frame_chain))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        self.actions.append((verb, selector, tuple(frame_chain)))

    def act_on(self, native, verb, *args, **kwargs):
        self.actions.append((verb,) + tuple(native))

    def url(self):
        return self._url

    def outer_html(self, scope=None):
        return lxml.html.tostring(self.doc, encoding='unicode')

    def describe(self, selector, frame_chain=()):
        found = _find(self._doc(frame_chain), selector)
        if len(found) != 1:
            return None
        el = found[0]
        role = 'checkbox' if el.get('type') == 'checkbox' else el.tag
        return {'tag': el.tag, 'role': role, 'text': el.text_content().strip(),
                'outer_html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}

    def inventory(self, limit=200):
        return []


class TwoDocs(Doc):
    def __init__(self, inner_html, frames=1, **frame):
        super().__init__(OUTER, inner=Doc(inner_html))
        self.frame = dict({'name': 'consent', 'url': 'about:srcdoc', 'nested': False,
                           'selector': FRAME}, **frame)
        self.frame_sel = self.frame['selector'] or FRAME
        self.frames = frames

    def frame_matches(self, selector):
        n = self.inner.count(selector)
        return [dict(self.frame, count=n)] * self.frames if n else []

    def framed(self, frame_selector):
        return self.inner if frame_selector == self.frame_sel else None


def _terms_store(tmp_path, frame=None):
    store = load_store(None, _store(tmp_path))
    store.set_code_candidates('terms', ['#inner-terms'])
    if frame:
        store.add_candidate('terms', 'css=#inner-terms', source='heal', frame_xpath=frame)
        store.record_success('terms', 'css=#inner-terms')
    store.set_meta('terms', role='checkbox')
    store.set_last_page('terms', '/signup')
    return store


def _locator_text(page, tmp_path, stub_provider, **open_kw):
    """What heal.locator() raises for 'terms' on page, on a copy of the store."""
    import shutil
    copy = tmp_path / 'copy'
    shutil.copytree(tmp_path / 'store', copy)
    heal = mendwright.open(page, provider=stub_provider(None),
                           store_path=str(copy / 'overrides.json'), **open_kw)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('terms', role='checkbox', timeout_ms=0, poll_ms=0)
    return str(info.value).split('\n  -> ', 1)[1]


def test_a_selector_saved_inside_an_iframe_is_probed_there(stub_provider, tmp_path, caplog):
    _terms_store(tmp_path, frame=FRAME)
    page = TwoDocs(INNER)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.used) == ('ok-heal', 'css=#inner-terms')
    probe = [p for p in k.probes if p.selector == 'css=#inner-terms'][0]
    assert (probe.frame, probe.result) == (FRAME, 'one visible element')


def test_a_key_that_moved_into_an_iframe_would_be_followed(stub_provider, tmp_path, caplog):
    _terms_store(tmp_path)
    page = TwoDocs(INNER)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.detail) == ('would-heal', 'frame')
    assert k.heal == {'source': 'frame', 'selector': 'css=#inner-terms', 'frame': FRAME,
                      'score': None, 'runner_up': None}
    assert k.reason == ('#inner-terms matches nothing on the page, but it matches one element '
                        'inside the iframe ' + FRAME + '. A run looks it up there from now on, '
                        'which counts as a heal (--mendwright-strict fails on it). Nothing was '
                        'saved.')
    assert page.actions == [] and page.inner.actions == []


@pytest.mark.parametrize('how', [
    {'nested': True}, {'selector': None}, {'frames': 2}, 'several', 'quarantined', 'hidden',
    'another-page'])
def test_an_iframe_a_run_would_not_follow_is_refused_in_its_words(how, stub_provider, tmp_path,
                                                                  caplog):
    inner = INNER
    if how == 'several':
        inner = INNER + "<input type='checkbox' id='inner-terms'>"
    if how == 'hidden':
        inner = INNER.replace("type='checkbox'", "type='text'")     # another role there
    store = _terms_store(tmp_path)
    if how == 'quarantined':
        store.add_candidate('terms', 'css=#inner-terms', source='heal', frame_xpath=FRAME)
        store.quarantine('terms', 'css=#inner-terms')
    frame = how if isinstance(how, dict) else {}
    pages = [TwoDocs(inner, **frame), TwoDocs(inner, **frame)]
    if how == 'another-page':
        # 'terms' was found on /signup only: the page guard stops the follow on /checkout.
        for doc in pages:
            doc._url = 'https://shop.example/checkout'
    expected = _locator_text(pages[0], tmp_path, stub_provider)
    if how == 'another-page':
        assert expected.startswith("'terms' was last found on /signup, and this page is "
                                   '/checkout.'), expected
    page = pages[1]
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, page, caplog, keys=['terms']).keys
    assert k.verdict == 'refused'
    assert k.reason.endswith('A run would not heal it: ' + expected)


def test_a_key_last_found_in_an_iframe_heals_inside_it(make_page, stub_provider, tmp_path,
                                                       caplog):
    _terms_store(tmp_path, frame=FRAME)
    first = TwoDocs(INNER)
    # A fingerprint recorded in the iframe, as a run records one there.
    from mendwright.fingerprint import of_element
    el = first.inner.doc.xpath("//input")[0]
    load_store(None, _store(tmp_path)).set_meta('terms', fingerprint=of_element(el))
    page = TwoDocs(INNER.replace("id='inner-terms'", "id='terms-v2'"))
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.detail) == ('would-heal', 'fingerprint')
    assert k.heal['frame'] == FRAME and k.heal['selector'].startswith('xpath=')


# --- lists, optional keys, steps -------------------------------------------------------------

CARDS = "<ul>" + "<li class='card'>A</li>" * 3 + "</ul>"


def _list_store(tmp_path, make_page, stub_provider, **open_kw):
    page = make_page(CARDS, url=URL, fingerprints=True)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           **open_kw)
    assert len(heal.locator_all('cards', "//li[@class='card']", timeout_ms=0, poll_ms=0)) == 3
    return page


def test_a_list_is_probed_as_a_list(make_page, stub_provider, tmp_path, caplog):
    _list_store(tmp_path, make_page, stub_provider)
    heal, page = checker(make_page, stub_provider, tmp_path, html=CARDS.replace('</ul>',
                         "<li class='card'>B</li></ul>"))
    [k] = drifted(heal, page, caplog).keys
    assert k.verdict == 'ok'
    assert k.reason == "//li[@class='card'] matches 4 elements (3 when its list fingerprint was " \
                       'recorded).'


def test_a_list_that_matches_nothing_is_never_healed(make_page, stub_provider, tmp_path,
                                                     caplog):
    _list_store(tmp_path, make_page, stub_provider)
    heal, page = checker(make_page, stub_provider, tmp_path,
                         html="<ul><li class='tile'>A</li><li class='tile'>B</li></ul>")
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.counts_as) == ('list-missing', 'unchecked')
    assert "'cards' matched 3 elements when its list fingerprint was recorded" in k.reason
    assert k.reason.endswith("heal.drift() can't tell an empty list from a broken one either.")
    heal, page = checker(make_page, stub_provider, tmp_path, fingerprint=False,
                         html="<ul><li class='tile'>A</li></ul>")
    [k] = drifted(heal, page, caplog).keys
    assert (k.verdict, k.counts_as) == ('list-missing', 'none')
    assert k.reason == ("//li[@class='card'] matches nothing: a run gets [] at once, since the "
                        'fingerprint tier is off.')


def test_a_key_runs_skipped_is_optional(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    (tmp_path / 'store' / 'journal.jsonl').write_text(json.dumps({
        'ts': '2026-10-14T08:00:00+00:00', 'event': 'flow.skip', 'key': 'newsletter',
        'reason': 'interruption absent'}) + '\n', encoding='utf-8')
    heal, page = checker(make_page, stub_provider, tmp_path)
    k = by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter']
    assert (k.verdict, k.counts_as) == ('optional', 'none')
    assert k.reason == ('#news matches nothing, and earlier runs skipped it (interruption '
                        'absent, 2026-10-14): a run skips it when it is missing. Not counted.')
    (tmp_path / 'store' / 'journal.jsonl').unlink()
    heal, page = checker(make_page, stub_provider, tmp_path)
    assert by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter'].verdict == (
        'needs-model')
    heal, page = checker(make_page, stub_provider, tmp_path)
    heal.interrupt('newsletter', '#news-popup')
    k = by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter']
    assert k.verdict == 'optional' and 'registered with heal.interrupt()' in k.reason


def test_a_key_found_during_a_step_needs_its_steps_only_when_loaded_fresh(
        make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    store = load_store(None, _store(tmp_path))
    store.set_last_step('newsletter', 'payment')
    heal, page = checker(make_page, stub_provider, tmp_path)
    report = drift.probe_page(heal, ['newsletter'], timeout_ms=0, fresh=True,
                              who='mendwright check')
    [k] = report.keys
    assert (k.verdict, k.counts_as) == ('needs-steps', 'unchecked')
    assert k.reason == ("#news matches nothing. It was found during step 'payment' of "
                        'run_steps(), and mendwright check loaded this page without the steps '
                        "before it, so the miss may be the page's state, not drift. Check it "
                        'with heal.drift() inside your flow.')
    heal, page = checker(make_page, stub_provider, tmp_path)
    assert by_key(drifted(heal, page, caplog, ['newsletter']))['newsletter'].verdict == (
        'needs-model')


# --- the run's settings ----------------------------------------------------------------------

def test_mendwright_disable_refuses_every_miss(make_page, stub_provider, tmp_path, caplog,
                                               monkeypatch):
    record(make_page, stub_provider, tmp_path)
    monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    heal, page = checker(make_page, stub_provider, tmp_path)
    reads = page.fingerprint_reads
    got = by_key(drifted(heal, page, caplog))
    assert got['email'].verdict == 'ok'
    for key in ('accept_terms', 'newsletter', 'create_account'):
        assert got[key].verdict == 'refused'
        assert 'healing is turned off by MENDWRIGHT_DISABLE=1' in got[key].reason
    assert page.fingerprint_reads == reads
    _terms_store(tmp_path)
    frames = TwoDocs(INNER)
    heal = mendwright.open(frames, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0)
    k = by_key(drifted(heal, frames, caplog, ['terms']))['terms']
    assert k.verdict == 'refused' and 'MENDWRIGHT_DISABLE=1' in k.reason


def test_dry_run_heals_fail_under_unhealable(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, dry_run=True)
    k = by_key(drifted(heal, page, caplog, ['accept_terms']))['accept_terms']
    assert k.verdict == 'would-heal' and k.dry_run
    assert k.reason.endswith(' Dry run is on, so a run would check this heal and not apply it.')
    assert k.fails(fail_on='unhealable')
    _terms_store(tmp_path)
    page = TwoDocs(INNER)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=_store(tmp_path),
                           timeout_ms=0, dry_run=True)
    k = by_key(drifted(heal, page, caplog, ['terms']))['terms']
    assert (k.verdict, k.detail) == ('would-heal', 'frame')
    assert k.reason.endswith(' Dry run is on, so a run would check this heal and not apply it.')
    assert k.fails(fail_on='unhealable')


def test_a_key_from_another_page_gets_the_page_guard(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path,
                         url='https://shop.example/signup/details')
    k = by_key(drifted(heal, page, caplog, ['accept_terms']))['accept_terms']
    assert k.verdict == 'refused'
    assert k.reason.endswith(flow_change_message('accept_terms', '/signup', '/signup/details'))
    heal, page = checker(make_page, stub_provider, tmp_path, heal_across_pages=True,
                         url='https://shop.example/signup/details')
    assert by_key(drifted(heal, page, caplog, ['accept_terms']))[
        'accept_terms'].verdict == 'would-heal'


def test_a_blocking_guard_refuses(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, guard=lambda: False)
    k = by_key(drifted(heal, page, caplog, ['accept_terms']))['accept_terms']
    assert k.verdict == 'refused' and 'a transaction guard is currently blocking heals' in k.reason


def test_a_page_with_no_key_is_not_ok(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, url='https://shop.example/help')
    report = drifted(heal, page, caplog)
    assert report.keys == () and report.ok is False
    assert report.text() == 'No key in the store was found on /help.\n'
    assert report.summary() == 'drift on /help: no key in the store was found on this page'


def test_keys_on_the_site_root(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path, url='https://shop.example/')
    heal, page = checker(make_page, stub_provider, tmp_path, html=V1,
                         url='https://shop.example/')
    report = drifted(heal, page, caplog)
    assert report.page == '/' and report.ok and len(report.keys) == len(KEYS)
    assert {k.page for k in report.keys} == {'/'}


def test_keys_on_the_site_root_of_a_test_app_on_a_new_port(make_page, stub_provider, tmp_path,
                                                          caplog):
    # A live server on a random port: the store names last run's port. Other pages match
    # by path, so the root matches by host, whatever the port.
    record(make_page, stub_provider, tmp_path, url='http://127.0.0.1:5001/')
    heal, page = checker(make_page, stub_provider, tmp_path, html=V1,
                         url='http://127.0.0.1:5002/')
    report = drifted(heal, page, caplog)
    assert report.ok and len(report.keys) == len(KEYS)
    # Another host's root is another site.
    heal, page = checker(make_page, stub_provider, tmp_path, html=V1,
                         url='http://localhost:5002/')
    assert drifted(heal, page, caplog).keys == ()


@pytest.mark.parametrize('call, error, text', [
    (lambda h: h.drift('email'), TypeError, "takes a list of keys, such as heal.drift(['email'])"),
    (lambda h: h.drift(['email', 3]), TypeError, 'takes key names as strings, got 3'),
    (lambda h: h.drift(['x', 'email', 'y']), ValueError,
     "got keys the store doesn't have: 'x', 'y'. Check the names, or look the key up once with "
     'heal.locator() first.'),
    (lambda h: h.drift(timeout_ms=-1), ValueError, 'timeout_ms=0 or more'),
    (lambda h: h.drift(timeout_ms='5'), TypeError, 'timeout_ms= as a whole number'),
    (lambda h: h.drift(poll_ms=0), ValueError, 'poll_ms=1 or more'),
    (lambda h: h.drift(poll_ms=1.5), TypeError, 'poll_ms= as a whole number'),
])
def test_argument_errors_come_before_the_page(call, error, text, make_page, stub_provider,
                                              tmp_path):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    page.queries.clear()
    with pytest.raises(error) as info:
        call(heal)
    assert text in str(info.value)
    assert page.queries == []


def test_duplicate_keys_are_checked_once_in_order(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path, html=V1)
    report = drifted(heal, page, caplog, ['total', 'email', 'total'])
    assert [k.key for k in report.keys] == ['total', 'email']


def test_a_store_without_snapshot_gives_no_preview(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    from mendwright.store import SelectorStore

    class Wrapper(SelectorStore):
        snapshot = None

    base = load_store(None, _store(tmp_path))
    store = Wrapper(base._data, _store(tmp_path), override=json.loads(
        (tmp_path / 'store' / 'overrides.json').read_text(encoding='utf-8')))
    page = make_page(V2, url=URL, fingerprints=True)
    heal = mendwright.open(page, provider=stub_provider(None), store=store, timeout_ms=0)
    report = heal.drift(['accept_terms', 'email'])
    got = by_key(report)
    assert got['email'].verdict == 'ok'
    assert (got['accept_terms'].verdict, got['accept_terms'].detail) == ('skipped', 'no-preview')
    assert got['accept_terms'].reason == ("#accept-terms matches nothing, and this store has no "
                                          "snapshot(), so heal.drift() can't look for a heal "
                                          'without saving one.')


def test_inside_run_steps_drift_logs_one_line(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    load_store(None, _store(tmp_path)).set_last_step('accept_terms', 'consent')
    heal, page = checker(make_page, stub_provider, tmp_path,
                         url='https://shop.example/consent')
    reports = []

    @heal.step('consent', '#email', last=True)
    def consent(p):
        reports.append(drifted(heal, page, caplog, ['accept_terms']))

    heal.run_steps(timeout_ms=0, poll_ms=1)
    [k] = reports[0].keys
    assert k.verdict == 'would-heal'       # the step's page counts for the page guard


def test_report_shapes(make_page, stub_provider, tmp_path, caplog):
    record(make_page, stub_provider, tmp_path)
    heal, page = checker(make_page, stub_provider, tmp_path)
    report = drifted(heal, page, caplog)
    data = report.as_dict()
    assert set(data) == {'page', 'url', 'notes', 'ok', 'keys'}
    assert {k['key'] for k in data['keys']} == {k.key for k in report.keys}
    assert set(data['keys'][0]) == {f.name for f in __import__('dataclasses').fields(KeyDrift)} \
        | {'fails'}
    text = report.text()
    assert text.startswith(URL + '\n  ')
    assert '  would heal     accept_terms    #accept-terms matches nothing.' in text
    assert report.failed(fail_on='unhealable') == tuple(
        k for k in report.keys if k.verdict == 'refused')
    with pytest.raises(ValueError):
        report.failed(fail_on='broken')
