"""heal.canary says what confirmed or refused a heal: by is test (with the node id in test),
expect, expect_value, confirm, action or destructive. mendwright fix shows it in the pull
request the GitHub Action opens. Code written for 0.8.0, a HealCoordinator subclass whose
confirm() has no by= or a journal whose canary() takes no extra fields, keeps working.

Offline: the lxml FakePage from conftest, its async twin from test_aio, the Selenium-shaped
fake from test_proxies, and a stub provider."""

import asyncio
import json

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, HealCoordinator, runsummary
from mendwright.journal import Journal

from test_aio import AsyncFakePage
from test_proxies import WebDriver

V2 = "<form><label>terms<input type='checkbox' id='new-id'></label><a id='l' href='#'>Docs</a></form>"
STALE = "//input[@id='old-id']"
HEALED = "//input[@type='checkbox']"
PRICES = "<form><span id='cost'>$1,299.00</span><span id='rating'>4.5</span></form>"


def canaries(tmp_path):
    path = tmp_path / 'journal.jsonl'
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    return [{k: v for k, v in e.items() if k not in ('ts', 'event')} for e in events
            if e['event'] == 'heal.canary']


def _open(page, tmp_path, provider):
    return mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'),
                           fingerprint=False, timeout_ms=0)


def _healed(tmp_path, make_page, stub_provider):
    page = make_page(V2)
    heal = _open(page, tmp_path, stub_provider(HEALED))
    box = heal.locator('terms', STALE, role='checkbox', timeout_ms=0, poll_ms=0)
    assert heal.coordinator.pending == {'terms': HEALED}
    return heal, page, box


def line(success, by, **extra):
    return dict({'key': 'terms', 'xpath': HEALED, 'success': success, 'by': by}, **extra)


class StaleElementReferenceException(Exception):
    """Named like Selenium's, which mendwright recognises by name."""


# --- sync ----------------------------------------------------------------------------------

@pytest.mark.parametrize('ok', [True, False])
def test_expect_on_an_action(tmp_path, make_page, stub_provider, ok):
    _, _, box = _healed(tmp_path, make_page, stub_provider)
    box.check(expect=lambda: ok)
    assert canaries(tmp_path) == [line(ok, 'expect')]


def test_a_failed_action(tmp_path, make_page, stub_provider):
    _, page, box = _healed(tmp_path, make_page, stub_provider)
    page.fail['check'] = RuntimeError('element is detached')
    with pytest.raises(RuntimeError):
        box.check()
    assert canaries(tmp_path) == [line(False, 'action')]


def test_a_stale_element_writes_no_canary(tmp_path, make_page, stub_provider):
    heal, page, box = _healed(tmp_path, make_page, stub_provider)
    page.fail['check'] = StaleElementReferenceException('stale element reference')
    with pytest.raises(StaleElementReferenceException):
        box.check()
    assert canaries(tmp_path) == []
    assert heal.coordinator.pending == {'terms': HEALED}


def test_confirm_and_confirm_all(tmp_path, make_page, stub_provider):
    _, _, box = _healed(tmp_path, make_page, stub_provider)
    box.confirm(True)
    assert canaries(tmp_path) == [line(True, 'confirm')]
    heal, _, _ = _healed(tmp_path / 'b', make_page, stub_provider)
    assert heal.confirm_all(False) == 1
    assert canaries(tmp_path / 'b') == [line(False, 'confirm')]


@pytest.mark.parametrize('proposal, ok', [("//span[@id='cost']", True), ("//span[@id='rating']", False)])
def test_expect_value_on_a_read(tmp_path, make_page, stub_provider, proposal, ok):
    heal = _open(make_page(PRICES), tmp_path, stub_provider(proposal))
    read = heal.locator('price', '#price', role='span', expect_value='money')
    if ok:
        assert read.inner_text() == '$1,299.00'
    else:
        with pytest.raises(AttentionRequired, match="'price' was healed to"):
            read.inner_text()
    expected = {'key': 'price', 'xpath': proposal, 'success': ok, 'by': 'expect_value'}
    if not ok:
        expected['reason'] = 'expect_value: inner_text() is not money'
    assert canaries(tmp_path) == [{k: expected[k] for k in ('key', 'xpath', 'success', 'reason', 'by')
                                   if k in expected}]


def test_a_heal_of_a_key_marked_destructive_after_it(tmp_path, make_page, stub_provider):
    heal, page, _ = _healed(tmp_path, make_page, stub_provider)
    with pytest.raises(AttentionRequired, match="'terms' is marked as a destructive control"):
        heal.locator('terms', STALE, role='checkbox', destructive=True).check()
    assert canaries(tmp_path) == [line(False, 'destructive')]
    assert page.actions == []


def _quarantines(tmp_path, run):
    """The heal.quarantine lines of run, and what mendwright notify says needs a human."""
    lines = [json.loads(line) for line in
             (tmp_path / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
    events, _ = runsummary.select(lines, run=run)
    found = [(e['key'], e['xpath'], e.get('by')) for e in events if e['event'] == 'heal.quarantine']
    return found, runsummary.summarize(events)['needs_human']


def test_a_heal_saved_before_its_key_was_marked_destructive(tmp_path, make_page, stub_provider):
    # Run 1 healed 'terms' and confirmed it; run 2, another process, marks it destructive.
    heal, page, _ = _healed(tmp_path, make_page, stub_provider)
    heal.confirm_all(True)
    again = mendwright.open(page, provider=stub_provider(HEALED), timeout_ms=0,
                            store_path=str(tmp_path / 'o.json'), fingerprint=False,
                            journal=Journal(tmp_path / 'journal.jsonl', run='run-2'))
    with pytest.raises(AttentionRequired, match="'terms' is marked as a destructive control"):
        again.locator('terms', STALE, role='checkbox', destructive=True).check()
    assert page.actions == []
    assert _quarantines(tmp_path, 'run-2') == (
        [('terms', HEALED, 'destructive')],
        [{'key': 'terms', 'code': 'destructive', 'about': 'selector'}])


def test_a_failed_click_on_an_element_heal_driver_found(tmp_path, stub_provider):
    page = ("<form><input type='email' id='email' name='email'>"
            "<input type='checkbox' id='terms'><button id='go'>Go</button></form>")
    web = WebDriver(page)
    heal = _open(web, tmp_path, stub_provider(HEALED))
    heal.driver.find_element('css selector', '#terms')
    web.set_html(page.replace("id='terms'", "id='terms-v2'"))
    healed = heal.driver.find_element('css selector', '#terms')

    def refuse():
        raise RuntimeError('element not interactable')
    healed.native.click = refuse
    with pytest.raises(RuntimeError):
        healed.click()
    assert canaries(tmp_path) == [{'key': 'css selector=#terms', 'xpath': HEALED,
                                   'success': False, 'by': 'action'}]


# --- async ---------------------------------------------------------------------------------

async def _ahealed(page, tmp_path, stub_provider, proposal=HEALED):
    heal = await mendwright.aio.open(page, provider=stub_provider(proposal),
                                     store_path=str(tmp_path / 'o.json'), fingerprint=False,
                                     timeout_ms=0)
    box = await heal.locator('terms', STALE, role='checkbox', timeout_ms=0, poll_ms=0)
    assert heal.pending() == {'terms': HEALED}
    return heal, box


@pytest.mark.parametrize('ok', [True, False])
def test_async_expect_on_an_action(tmp_path, make_page, stub_provider, ok):
    async def main():
        _, box = await _ahealed(AsyncFakePage(make_page(V2)), tmp_path, stub_provider)
        await box.check(expect=lambda: ok)
    asyncio.run(main())
    assert canaries(tmp_path) == [line(ok, 'expect')]


def test_async_failed_action_confirm_and_confirm_all(tmp_path, make_page, stub_provider):
    async def main():
        page = AsyncFakePage(make_page(V2))
        _, box = await _ahealed(page, tmp_path / 'a', stub_provider)
        page.sync.fail['click'] = RuntimeError('element is detached')
        with pytest.raises(RuntimeError):
            await box.click()
        _, box = await _ahealed(AsyncFakePage(make_page(V2)), tmp_path / 'b', stub_provider)
        box.confirm(True)
        heal, _ = await _ahealed(AsyncFakePage(make_page(V2)), tmp_path / 'c', stub_provider)
        assert heal.confirm_all(False) == 1
    asyncio.run(main())
    assert canaries(tmp_path / 'a') == [line(False, 'action')]
    assert canaries(tmp_path / 'b') == [line(True, 'confirm')]
    assert canaries(tmp_path / 'c') == [line(False, 'confirm')]


def test_async_stale_element_writes_no_canary(tmp_path, make_page, stub_provider):
    async def main():
        page = AsyncFakePage(make_page(V2))
        heal, box = await _ahealed(page, tmp_path, stub_provider)
        page.sync.fail['click'] = StaleElementReferenceException('stale element reference')
        with pytest.raises(StaleElementReferenceException):
            await box.click()
        return heal
    heal = asyncio.run(main())
    assert canaries(tmp_path) == [] and heal.pending() == {'terms': HEALED}


@pytest.mark.parametrize('proposal, ok', [("//span[@id='cost']", True), ("//span[@id='rating']", False)])
def test_async_expect_value_on_a_read(tmp_path, make_page, stub_provider, proposal, ok):
    async def main():
        heal = await mendwright.aio.open(AsyncFakePage(make_page(PRICES)),
                                         provider=stub_provider(proposal),
                                         store_path=str(tmp_path / 'o.json'), fingerprint=False,
                                         timeout_ms=0)
        read = heal.locator('price', '#price', role='span', expect_value='money')
        if ok:
            assert await read.inner_text() == '$1,299.00'
        else:
            with pytest.raises(AttentionRequired):
                await read.inner_text()
    asyncio.run(main())
    [canary] = canaries(tmp_path)
    assert (canary['xpath'], canary['success'], canary['by']) == (proposal, ok, 'expect_value')
    assert ('reason' in canary) is (not ok)


def test_async_heal_of_a_key_marked_destructive_after_it(tmp_path, make_page, stub_provider):
    async def main():
        page = AsyncFakePage(make_page(V2))
        heal, _ = await _ahealed(page, tmp_path, stub_provider)
        with pytest.raises(AttentionRequired, match="'terms' is marked as a destructive control"):
            await heal.locator('terms', STALE, role='checkbox', destructive=True).check()
    asyncio.run(main())
    assert canaries(tmp_path) == [line(False, 'destructive')]


def test_async_heal_saved_before_its_key_was_marked_destructive(tmp_path, make_page,
                                                               stub_provider):
    async def main():
        page = AsyncFakePage(make_page(V2))
        heal, _ = await _ahealed(page, tmp_path, stub_provider)
        heal.confirm_all(True)
        again = await mendwright.aio.open(page, provider=stub_provider(HEALED), timeout_ms=0,
                                          store_path=str(tmp_path / 'o.json'), fingerprint=False,
                                          journal=Journal(tmp_path / 'journal.jsonl',
                                                          run='run-2'))
        with pytest.raises(AttentionRequired, match="'terms' is marked as a destructive control"):
            await again.locator('terms', STALE, role='checkbox', destructive=True).check()
    asyncio.run(main())
    assert _quarantines(tmp_path, 'run-2') == (
        [('terms', HEALED, 'destructive')],
        [{'key': 'terms', 'code': 'destructive', 'about': 'selector'}])


# --- the 0.8.0 line, and code written for 0.8.0 ----------------------------------------------

class _Store:
    def __init__(self):
        self.quarantined = []

    def quarantine(self, key, xpath):
        self.quarantined.append((key, xpath))


class _Provider:
    def __init__(self):
        self.outcomes = []

    def propose(self, bundle):
        return None

    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, success))


def _coordinator(cls=HealCoordinator, journal=None):
    return cls(_Store(), provider=_Provider(), html_provider=lambda: '',
               inventory_provider=lambda: [], guard=lambda: True, journal=journal)


def test_a_canary_without_by_is_the_0_8_0_line(tmp_path):
    path = tmp_path / 'journal.jsonl'
    coordinator = _coordinator(journal=Journal(path, clock=lambda: '2026-10-16T05:01:04+00:00'))
    coordinator.confirm('k', '//a', True)
    coordinator.confirm('k', '//a', True, by='test', test='t.py::test_a')
    assert path.read_text(encoding='utf-8').splitlines() == [
        '{"ts":"2026-10-16T05:01:04+00:00","event":"heal.canary","key":"k","xpath":"//a",'
        '"success":true}',
        '{"ts":"2026-10-16T05:01:04+00:00","event":"heal.canary","key":"k","xpath":"//a",'
        '"success":true,"by":"test","test":"t.py::test_a"}',
    ]


def test_a_subclass_with_the_0_8_0_confirm_gets_one_call_without_by(tmp_path):
    calls = []

    class Old(HealCoordinator):
        def confirm(self, selector_key, xpath, success, reason=None):
            calls.append((selector_key, xpath, success, reason))
            super().confirm(selector_key, xpath, success, reason)

    coordinator = _coordinator(Old, Journal(tmp_path / 'journal.jsonl'))
    coordinator.pending['terms'] = HEALED
    coordinator.confirm_pending('terms', False, by='expect')
    assert calls == [('terms', HEALED, False, None)]
    assert canaries(tmp_path) == [{'key': 'terms', 'xpath': HEALED, 'success': False}]
    assert coordinator.store.quarantined == [('terms', HEALED)]
    assert coordinator.provider.outcomes == [('terms', HEALED, False)]


def test_a_journal_whose_canary_takes_no_more_fields_gets_one_line():
    lines = []

    class OldJournal:
        def canary(self, key, xpath=None, success=None, reason=None):
            lines.append(('canary', key, xpath, success, reason))

        def quarantine(self, key, xpath=None, reason=None):
            lines.append(('quarantine', key, xpath, reason))

    coordinator = _coordinator(journal=OldJournal())
    coordinator.pending['terms'] = HEALED
    coordinator.confirm_pending('terms', False, by='action')
    assert lines == [('canary', 'terms', HEALED, False, None),
                     ('quarantine', 'terms', HEALED, None)]
    assert coordinator.store.quarantined == [('terms', HEALED)]
    assert coordinator.provider.outcomes == [('terms', HEALED, False)]


def test_a_replaced_confirm_pending_without_by_still_gets_the_report(tmp_path, make_page,
                                                                     stub_provider):
    heal, _, box = _healed(tmp_path, make_page, stub_provider)
    reported = []
    heal.coordinator.confirm_pending = lambda key, success: reported.append((key, success))
    box.check(expect=lambda: True)
    box.confirm(False)
    heal.confirm_all(True)
    assert reported == [('terms', True), ('terms', False), ('terms', True)]


def test_takes_reads_the_signature_once_per_function():
    from mendwright.coordinator import _takes

    def plain(a, b=None):
        pass

    def open_ended(a, **kw):
        pass

    def positional(by, /):
        pass

    assert (_takes(plain, 'by'), _takes(open_ended, 'by'), _takes(positional, 'by')) == (
        False, True, False)
    assert _takes(len, 'by') is False
