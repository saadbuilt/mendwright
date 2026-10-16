"""The one live validation gate: a proposal must match the LIVE page (unique + visible,
unless checkbox-role), not merely validate against a captured snapshot, before it commits."""

import pytest

import mendwright
from mendwright import SelectorStore, apply

CHECKBOX_HTML = '<form><input type="checkbox" id="tc"></form>'
CHECKBOX_XP = "//input[@type='checkbox']"


class FakeDriver:
    def __init__(self, count, visible=True):
        self._c, self._v = count, visible
    def count(self, xpath, frame_chain=()):
        return self._c
    def is_visible(self, xpath, frame_chain=()):
        return self._v


def _store(tmp_path, role='checkbox'):
    return SelectorStore({'schema': 2, 'selectors': {'k': {'expectedRole': role, 'candidates': []}}},
                         str(tmp_path / 'o.json'))


def test_rejects_statically_valid_but_absent_on_live_page(tmp_path):
    # Validates against the snapshot, but the live driver finds zero -> NOT committed.
    applied, reason = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                            {'expectedRole': 'checkbox'}, driver=FakeDriver(count=0))
    assert applied is None
    assert 'live page' in reason


def test_rejects_when_live_has_two_matches(tmp_path):
    # Snapshot has exactly one (static validate passes), but the LIVE page now has two —
    # the live gate must reject (pins the count != 1 check, not a weaker count < 1).
    applied, reason = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                            {'expectedRole': 'checkbox'}, driver=FakeDriver(count=2))
    assert applied is None
    assert 'live page' in reason


def test_accepts_when_uniquely_present_live(tmp_path):
    applied, _ = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                       {'expectedRole': 'checkbox'}, driver=FakeDriver(count=1, visible=True))
    assert applied == CHECKBOX_XP


def test_checkbox_role_ignores_visibility_live(tmp_path):
    # a hidden (toggled-by-label) checkbox with count==1 is accepted
    applied, _ = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                       {'expectedRole': 'checkbox'}, driver=FakeDriver(count=1, visible=False))
    assert applied == CHECKBOX_XP


def test_button_role_requires_visibility_live(tmp_path):
    store = _store(tmp_path, role='button')
    applied, reason = apply(store, 'k', {'xpath': '//button'}, '<button>x</button>',
                            {'expectedRole': 'button'}, driver=FakeDriver(count=1, visible=False))
    assert applied is None and 'live page' in reason


# --- the live element is read again: role, and text for destructive keys ---------------------

class DescribingDriver(FakeDriver):
    """A live page whose single match describes as ``described`` (None: unreadable)."""

    def __init__(self, described, count=1, visible=True):
        super().__init__(count, visible)
        self.described = described

    def describe(self, xpath, frame_chain=()):
        return self.described


def _link(text='Archive'):
    return {'tag': 'a', 'role': 'link', 'text': text,
            'outer_html': "<a href='/x'>" + text + '</a>'}


def test_a_live_role_mismatch_is_refused_with_a_reason(tmp_path):
    store = _store(tmp_path, role='button')
    applied, reason = apply(store, 'k', {'xpath': '//*[@data-step]'},
                            "<button data-step='1'>Save draft</button>",
                            {'expectedRole': 'button'}, driver=DescribingDriver(_link()))
    assert applied is None
    assert reason == 'live element role mismatch: got link want button'
    assert store.candidates('k') == []


def test_a_checkbox_key_still_checks_the_live_role(tmp_path):
    # Checkboxes skip the visibility check, but not the role check.
    applied, reason = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                            {'expectedRole': 'checkbox'}, driver=DescribingDriver(_link()))
    assert applied is None and 'role mismatch: got link want checkbox' in reason


def test_an_unreadable_live_element_fails_closed(tmp_path):
    applied, reason = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                            {'expectedRole': 'checkbox'}, driver=DescribingDriver(None))
    assert applied is None and reason.startswith('could not read the live element')


def test_a_matching_live_element_is_applied(tmp_path):
    live = {'tag': 'input', 'role': 'checkbox', 'text': '',
            'outer_html': "<input type='checkbox' id='tc'>"}
    applied, _ = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                       {'expectedRole': 'checkbox'}, driver=DescribingDriver(live))
    assert applied == CHECKBOX_XP


def test_a_destructive_key_checks_the_live_text(tmp_path):
    store = _store(tmp_path, role='button')
    live = {'tag': 'button', 'role': 'button', 'text': 'Delete account',
            'outer_html': '<button>Delete account</button>'}
    applied, reason = apply(store, 'k', {'xpath': '//button'}, '<button>Pay now</button>',
                            {'expectedRole': 'button', 'expectedText': ['Pay now'],
                             'destructive': True}, driver=DescribingDriver(live))
    assert applied is None and reason == 'live action text mismatch: Delete account'


def test_a_page_that_re_renders_during_the_model_call_is_checked_live(tmp_path, make_page,
                                                                       stub_provider):
    # The snapshot has a button. While the model works, the page re-renders and the same
    # attribute-only xpath now matches a link. Nothing is saved or clicked.
    page = make_page("<form><button data-step='primary'>Save draft</button></form>")
    provider = stub_provider("//*[@data-step='primary']")
    real_propose = provider.propose

    def propose(bundle):
        page.set_html("<form><a data-step='primary' href='/archive'>Archive</a></form>")
        return real_propose(bundle)
    provider.propose = propose
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / 'o.json'))
    with pytest.raises(mendwright.AttentionRequired, match='role mismatch: got link want button'):
        heal.locator('save_draft', '#save', role='button', timeout_ms=0, poll_ms=0).click()
    assert provider.bundles[1]['rejectReason'] == 'live element role mismatch: got link want button'
    assert page.actions == []
    assert heal.store.meta('save_draft')['expectedRole'] == 'button'


def test_the_sync_bridge_hides_a_missing_describe():
    import asyncio

    from mendwright.aio import SyncBridge

    class NoDescribe:
        async def count(self, selector, frame_chain=()):
            return 1

    class WithDescribe(NoDescribe):
        async def describe(self, selector, frame_chain=()):
            return None

    loop = asyncio.new_event_loop()
    try:
        assert SyncBridge(NoDescribe(), loop).describe is None
        assert callable(SyncBridge(WithDescribe(), loop).describe)
    finally:
        loop.close()


def test_no_driver_is_static_only(tmp_path):
    # legacy low-level path with no live driver: static gate only (retry provides the live check)
    applied, _ = apply(_store(tmp_path), 'k', {'xpath': CHECKBOX_XP}, CHECKBOX_HTML,
                       {'expectedRole': 'checkbox'})
    assert applied == CHECKBOX_XP


# --- the ARIA role of a heal of a get_by_role() call --------------------------------------

class RoleDriver(DescribingDriver):
    """A live page that also says whether Playwright gives the element an ARIA role."""

    def __init__(self, described, answer, count=1, visible=True):
        super().__init__(described, count, visible)
        self.answer = answer
        self.asked = []

    def role_match(self, selector, role, level=None, frame_chain=()):
        self.asked.append((selector, role, level, frame_chain))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _button():
    return {'tag': 'button', 'role': 'button', 'text': 'Settings',
            'outer_html': "<button id='settings'>Settings</button>"}


def test_a_get_by_role_heal_must_have_the_calls_aria_role():
    from mendwright.gate import live_reason
    from mendwright.getby import ROLE_UNREADABLE, parse

    meta = {'expectedRole': 'button'}
    tab = parse('get_by_role("tab", name="Settings")')
    heading = parse('get_by_role("heading", name="Cart", level=2)')
    driver = RoleDriver(_button(), True)
    assert live_reason(driver, "//button[@id='settings']", meta, call=tab) == ''
    assert driver.asked == [("//button[@id='settings']", 'tab', None, ())]
    assert live_reason(RoleDriver(_button(), False), '//b', meta, call=tab) == (
        "live element role mismatch: Playwright does not give it the ARIA role 'tab'")
    assert live_reason(RoleDriver(_button(), False), '//b', meta, call=heading) == (
        "live element role mismatch: Playwright does not give it the ARIA role 'heading' at "
        'level 2')
    assert live_reason(RoleDriver(_button(), None), '//b', meta, call=tab) == ROLE_UNREADABLE
    assert live_reason(RoleDriver(_button(), RuntimeError('gone')), '//b', meta,
                       call=tab) == ROLE_UNREADABLE
    assert live_reason(DescribingDriver(_button()), '//b', meta, call=tab) == ROLE_UNREADABLE


def test_no_call_or_another_method_asks_no_role():
    from mendwright.gate import live_reason
    from mendwright.getby import parse

    meta = {'expectedRole': 'button'}
    for call in (None, parse('get_by_text("Settings")'), parse('get_by_test_id("settings")')):
        driver = RoleDriver(_button(), False)
        assert live_reason(driver, '//b', meta, call=call) == ''
        assert driver.asked == []          # no extra round trip
