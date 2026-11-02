"""A selector a heal or a seed file saved is checked again on the page where a lookup would
use it (resolver.Reuse, gate.reuse_reason): the element's role against the key's role when a
control is involved, its text when the key is destructive and has expected text, and its ARIA
role for the key of a get_by_role() call. One that fails is left out of that lookup, logged
once per key and page, journaled as heal.refused, and never quarantined. A selector the code
passes is used as before.

The cases come from the fleet analysis's probe of a saved heal reused on a page variant (Q5):
a test id that moved onto a "Delete my account" button, and //input[@name='terms'], saved on a
checkbox, matching an <input type="submit" name="terms">. Offline: conftest's lxml FakePage,
whose describe() reads the element as a browser does."""

import asyncio
import json
import logging

import pytest

import mendwright
import mendwright.aio
from mendwright import AttentionRequired, getby, load_store
from test_aio import AsyncFakePage

URL = 'https://shop.test/checkout'
KEY = 'accept_terms'
CODE = '#accept-terms'
TESTID = "//*[@data-testid='tos-optin']"
BY_NAME = "//input[@name='terms']"


def _html(body):
    return ("<html><body><main><h1>Checkout</h1><form id='checkout'>"
            "<label for='email'>Email</label><input id='email' name='email' type='email'>"
            + body + "<button id='pay' type='submit'>Pay now</button></form></main></body></html>")


CHECKBOX = _html("<label><input type='checkbox' data-testid='tos-optin' name='terms'>"
                 "<span>I accept the terms</span></label>")
DELETE = _html("<button type='button' data-testid='tos-optin'>Delete my account</button>")
SUBMIT = _html("<input type='submit' name='terms' value='Read the terms'>")


def _row(xpath, source='heal', **extra):
    return dict({'xpath': xpath, 'source': source, 'addedAt': '2026-10-01T00:00:00+00:00',
                 'lastSucceededAt': '2026-10-02T00:00:00+00:00', 'status': 'active'}, **extra)


CODE_ROW = _row(CODE, 'code', lastSucceededAt='2026-10-01T00:00:00+00:00')


def _write(path, rows, key=KEY, **entry):
    path.write_text(json.dumps({'schema': 2, 'selectors': {key: dict(
        {'expectedRole': 'checkbox', 'lastPage': '/checkout', 'pages': ['/checkout'],
         'candidates': list(rows)}, **entry)}}), encoding='utf-8')
    return str(path)


def _open(make_page, html, store_path, provider, url=URL, **kwargs):
    page = make_page(html, url=url)
    kwargs.setdefault('timeout_ms', 0)
    return page, mendwright.open(page, provider=provider, store_path=store_path, **kwargs)


def _journal(tmp_path, event=None):
    path = tmp_path / 'journal.jsonl'
    rows = ([json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            if path.exists() else [])
    return [r for r in rows if event is None or r['event'] == event]


def _disk_status(store_path, key, xpath):
    data = json.loads(open(store_path, encoding='utf-8').read())
    return next(c.get('status', 'active') for c in data['selectors'][key]['candidates']
                if c['xpath'] == xpath)


def _refusal_lines(caplog):
    return [r.getMessage() for r in caplog.records if 'did not use' in r.getMessage()]


@pytest.mark.parametrize('heal_xpath, html, got', [
    (TESTID, DELETE, 'button'),
    (BY_NAME, SUBMIT, 'button'),
], ids=['a test id on a Delete my account button', 'an input of type submit'])
@pytest.mark.parametrize('role', [{'role': 'checkbox'}, {}], ids=['role passed', 'role stored'])
def test_a_saved_heal_on_another_control_is_not_used(tmp_path, make_page, stub_provider, caplog,
                                                    heal_xpath, html, got, role):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(heal_xpath)])
    provider = stub_provider(None)
    page, heal = _open(make_page, html, store, provider)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        with pytest.raises(AttentionRequired) as caught:
            heal.locator(KEY, CODE, **role).check()
    reason = 'live element role mismatch: got ' + got + ' want checkbox'
    assert 'failed the live check here, so it was not used: ' + reason in str(caught.value)
    assert page.actions == []                         # nothing was clicked or checked
    assert len(provider.bundles) == 1                 # it went on to the heal path
    [event] = _journal(tmp_path, 'heal.refused')
    assert (event['key'], event['xpath'], event['source'], event['reason'], event['page']) == (
        KEY, heal_xpath, 'heal', reason, '/checkout')
    [line] = _refusal_lines(caplog)
    assert line == ('mendwright: did not use ' + heal_xpath + " for 'accept_terms' on /checkout: "
                    'a heal saved that selector, and the element it matches here failed the '
                    'live check (' + reason + '). mendwright left it in the store for the pages '
                    'where it fits and went on without it.')
    # Not quarantined: it may be right on other pages.
    assert heal.store.status(KEY, heal_xpath) == 'active'
    assert _disk_status(store, KEY, heal_xpath) == 'active'
    assert not _journal(tmp_path, 'heal.quarantine')


def test_the_same_heal_is_used_on_the_page_it_fits(tmp_path, make_page, stub_provider):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(BY_NAME)])
    provider = stub_provider(None)
    page, heal = _open(make_page, CHECKBOX, store, provider)
    heal.locator(KEY, CODE).check()
    assert page.actions[-1][:2] == ('xpath=' + BY_NAME, 'check')
    assert provider.bundles == [] and not _journal(tmp_path, 'heal.refused')


def test_a_seed_row_is_checked_too(tmp_path, make_page, stub_provider):
    seed = _write(tmp_path / 'seed.json', [_row(BY_NAME, 'seed', lastSucceededAt=None)])
    store = load_store(seed, str(tmp_path / 'overrides.json'))
    page = make_page(SUBMIT, url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store=store, timeout_ms=0)
    with pytest.raises(AttentionRequired, match='got button want checkbox'):
        heal.locator(KEY).check()
    assert page.actions == []
    [event] = _journal(tmp_path, 'heal.refused')
    assert (event['source'], event['xpath']) == ('seed', BY_NAME)
    page.set_html(CHECKBOX)
    heal.locator(KEY).check()                         # the page it fits: used
    assert page.actions[-1][:2] == ('xpath=' + BY_NAME, 'check')


def test_the_selector_in_the_code_is_used_as_before(tmp_path, make_page, stub_provider, caplog):
    store = str(tmp_path / 'overrides.json')
    page, heal = _open(make_page, SUBMIT, store, stub_provider(None))
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        heal.locator(KEY, BY_NAME, role='checkbox').click()
    assert page.actions[-1][:2] == ('xpath=' + BY_NAME, 'click')
    assert not _journal(tmp_path, 'heal.refused') and not _refusal_lines(caplog)
    # The warning a mismatched role= always gave.
    assert any("role='checkbox' was given for 'accept_terms'" in r.getMessage()
               for r in caplog.records)


def test_one_warning_per_key_and_page(tmp_path, make_page, stub_provider, caplog):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(TESTID)])
    page, heal = _open(make_page, DELETE, store, stub_provider(None), heal_across_pages=True)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        for _ in range(3):
            with pytest.raises(AttentionRequired):
                heal.locator(KEY, CODE)
        assert len(_refusal_lines(caplog)) == 1
        assert len(_journal(tmp_path, 'heal.refused')) == 1
        page.url = 'https://shop.test/checkout/express'
        with pytest.raises(AttentionRequired):
            heal.locator(KEY, CODE)
    assert [line.split(':')[1].split(' on ')[-1] for line in _refusal_lines(caplog)] == [
        '/checkout', '/checkout/express']
    assert [e['page'] for e in _journal(tmp_path, 'heal.refused')] == ['/checkout',
                                                                        '/checkout/express']


@pytest.mark.parametrize('expected_text, used', [(['Pay now'], False), (None, True)],
                         ids=['with expected text', 'without'])
def test_the_text_of_a_destructive_key_is_checked_when_it_has_one(tmp_path, make_page,
                                                                  stub_provider, expected_text,
                                                                  used):
    entry = {'expectedRole': 'button', 'destructive': True}
    if expected_text:
        entry['expectedText'] = expected_text
    seed = _write(tmp_path / 'seed.json', [_row("//button[@type='submit']", 'seed',
                                                 lastSucceededAt=None)], key='pay', **entry)
    store = load_store(seed, str(tmp_path / 'overrides.json'))
    page = make_page(_html('').replace('Pay now', 'Delete my account'), url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store=store, timeout_ms=0)
    if used:
        heal.locator('pay').click()                  # the guide's seed example: no text
        assert page.actions[-1][1] == 'click'
        return
    with pytest.raises(AttentionRequired, match='live action text mismatch: Delete my account'):
        heal.locator('pay').click()
    assert page.actions == []


def test_a_role_that_is_only_a_guess_is_not_compared(tmp_path, make_page, stub_provider):
    seed = _write(tmp_path / 'seed.json', [_row("//*[@id='note']", 'seed', lastSucceededAt=None)],
                  key='note', expectedRole='unknown')
    store = load_store(seed, str(tmp_path / 'overrides.json'))
    page = make_page(_html("<textarea id='note'></textarea>"), url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store=store, timeout_ms=0)
    heal.locator('note', role='textbox').fill('hello')     # textbox: input or textarea
    assert page.actions[-1][:2] == ("xpath=//*[@id='note']", 'fill')
    assert heal.store.meta('note')['expectedRole'] == 'textarea'


def test_another_tag_on_a_key_only_read_is_used(tmp_path, make_page, stub_provider):
    seed = _write(tmp_path / 'seed.json', [_row("//*[@id='total']", 'seed', lastSucceededAt=None)],
                  key='total', expectedRole='span')
    store = load_store(seed, str(tmp_path / 'overrides.json'))
    page = make_page(_html("<div id='total'>12.00</div>"), url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store=store, timeout_ms=0)
    assert heal.locator('total').inner_text() == '12.00'
    assert not _journal(tmp_path, 'heal.refused')


def test_present_and_is_visible_leave_a_refused_heal_out(tmp_path, make_page, stub_provider):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(TESTID)])
    page, heal = _open(make_page, DELETE, store, stub_provider(None))
    assert heal.present(KEY) is False
    assert heal._peek_visible(KEY) is False
    page.set_html(CHECKBOX)
    assert heal.present(KEY) is True


def test_a_saved_heal_of_a_get_by_role_call_needs_the_calls_aria_role(tmp_path, make_page,
                                                                       stub_provider):
    # getby.GetByNotFound is also Playwright's TimeoutError, so it needs Playwright.
    pytest.importorskip('playwright.sync_api')
    key = 'get_by_role("button", name="Sign in")'
    heal_xpath = "//button[@id='login']"
    store = _write(tmp_path / 'overrides.json',
                   [_row(key, 'code', lastSucceededAt='2026-10-01T00:00:00+00:00'),
                    _row(heal_xpath)], key=key, expectedRole='button')
    page = make_page(_html("<button id='login' role='tab'>Log in</button>"), url=URL)
    heal = mendwright.open(page, provider=stub_provider(None), store_path=store, timeout_ms=0,
                           heal_get_by=True)
    with pytest.raises(getby.GetByNotFound):
        heal.page.get_by_role('button', name='Sign in').click()
    [event] = _journal(tmp_path, 'heal.refused')
    assert event['reason'] == getby.role_mismatch(getby.parse(key))
    assert page.actions == []
    page.set_html(_html("<button id='login'>Log in</button>"))
    heal.page.get_by_role('button', name='Sign in').click()
    assert page.actions[-1][:2] == ('xpath=' + heal_xpath, 'click')


def test_drift_leaves_a_refused_heal_out_as_a_run_does(tmp_path, make_page, stub_provider):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(TESTID)])
    page, heal = _open(make_page, DELETE, store, stub_provider(None))
    [k] = heal.drift([KEY]).keys
    assert k.verdict != 'ok-heal' and k.used is None
    assert [p.result for p in k.probes] == [
        'no match', 'fails the live check: live element role mismatch: got button want checkbox']
    page.set_html(CHECKBOX)
    [k] = heal.drift([KEY]).keys
    assert (k.verdict, k.used) == ('ok-heal', TESTID)


def test_the_async_healer_checks_a_saved_heal_too(tmp_path, make_page, stub_provider):
    store = _write(tmp_path / 'overrides.json', [CODE_ROW, _row(TESTID)])
    sync_page = make_page(DELETE, url=URL)

    async def main():
        heal = await mendwright.aio.open(AsyncFakePage(sync_page), provider=stub_provider(None),
                                         store_path=store, timeout_ms=0)
        with pytest.raises(AttentionRequired, match='got button want checkbox'):
            await heal.locator(KEY, CODE).check()
        assert await heal.present(KEY) is False
        sync_page.set_html(CHECKBOX)
        await heal.locator(KEY, CODE).check()        # a good heal still resolves on aio
        assert await heal.present(KEY) is True
    asyncio.run(main())
    assert sync_page.actions[-1][:2] == ('xpath=' + TESTID, 'check')
    [event] = _journal(tmp_path, 'heal.refused')
    assert (event['xpath'], event['page']) == (TESTID, '/checkout')
