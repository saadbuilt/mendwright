import unittest

import pytest

from mendwright import resolve, present
from mendwright import AttentionRequired, InvalidSelector
from mendwright.resolver import THROUGH, present_get_by, resolve_get_by


class FakeLoc:
    def __init__(self, count=1, visible=True, raises=False):
        self._count, self._visible, self._raises = count, visible, raises
    def count(self):
        if self._raises:
            raise RuntimeError('bad xpath')
        return self._count
    def is_visible(self):
        return self._visible
    def scroll_into_view_if_needed(self):
        pass


class FakePage:
    def __init__(self, mapping):
        self.mapping = mapping  # xpath -> FakeLoc
    def locator(self, sel):
        return self.mapping[sel.split('xpath=', 1)[1]]
    def frame_locator(self, sel):   # faithful Playwright surface (no frames in these tests)
        return self


class FakeStore:
    def __init__(self, cands, role='button'):
        self._c = cands
        self.role = role
        self.succeeded = None
    def candidates(self, key):
        return self._c
    def meta(self, key):
        return {'expectedRole': self.role}
    def record_success(self, key, xpath):
        self.succeeded = xpath


class ResolveTests(unittest.TestCase):
    def test_first_matching_candidate_wins_and_records(self):
        store = FakeStore([{'xpath': '//a'}, {'xpath': '//b'}])
        page = FakePage({'//a': FakeLoc(count=0), '//b': FakeLoc(count=1, visible=True)})
        loc = resolve(store, page, 'k', timeout_ms=10, poll_ms=0, sleep=lambda s: None)
        self.assertIs(loc, page.mapping['//b'])
        self.assertEqual(store.succeeded, '//b')

    def test_raising_candidate_is_skipped_not_fatal(self):
        store = FakeStore([{'xpath': '//boom'}, {'xpath': '//ok'}])
        page = FakePage({'//boom': FakeLoc(raises=True), '//ok': FakeLoc(count=1)})
        resolve(store, page, 'k', timeout_ms=10, poll_ms=0, sleep=lambda s: None)
        self.assertEqual(store.succeeded, '//ok')

    def test_none_match_raises_attention_with_key(self):
        store = FakeStore([{'xpath': '//a'}])
        page = FakePage({'//a': FakeLoc(count=0)})
        clock = iter([0.0, 0.0, 999.0])
        with self.assertRaises(AttentionRequired) as e:
            resolve(store, page, 'terms', timeout_ms=5, poll_ms=0,
                    monotonic=lambda: next(clock), sleep=lambda s: None)
        self.assertIn('terms', str(e.exception))

    def test_hidden_checkbox_matches_without_visibility(self):
        # a hidden input (count==1, not visible) must resolve for a checkbox role
        store = FakeStore([{'xpath': '//cb'}], role='checkbox')
        page = FakePage({'//cb': FakeLoc(count=1, visible=False)})
        loc = resolve(store, page, 'terms', timeout_ms=10, poll_ms=0, sleep=lambda s: None)
        self.assertIs(loc, page.mapping['//cb'])
        # but a button role still requires visibility
        store2 = FakeStore([{'xpath': '//b'}], role='button')
        self.assertFalse(present(store2, FakePage({'//b': FakeLoc(count=1, visible=False)}), 'x'))
        self.assertTrue(present(FakeStore([{'xpath': '//b'}], role='checkbox'),
                                FakePage({'//b': FakeLoc(count=1, visible=False)}), 'x'))

    def test_empty_candidates_raises(self):
        with self.assertRaises(AttentionRequired):
            resolve(FakeStore([]), FakePage({}), 'k', timeout_ms=1, poll_ms=0, sleep=lambda s: None)

    def test_overlay_dismissed_once_before_deadline(self):
        store = FakeStore([{'xpath': '//a'}])
        hits = {'n': 0}
        class P:
            def frame_locator(self, sel):
                return self
            def locator(self, sel):
                hits['n'] += 1
                return FakeLoc(count=(1 if hits['n'] > 1 else 0))
        calls = {'overlay': 0}
        clock = iter([0.0, 0.0, 0.0, 1.0])
        resolve(store, P(), 'k', timeout_ms=100, poll_ms=0,
                on_overlay=lambda: calls.__setitem__('overlay', calls['overlay'] + 1),
                monotonic=lambda: next(clock), sleep=lambda s: None)
        self.assertEqual(calls['overlay'], 1)

    def test_present_is_quick_and_never_raises(self):
        store = FakeStore([{'xpath': '//a'}])
        self.assertFalse(present(store, FakePage({'//a': FakeLoc(count=0)}), 'k'))
        self.assertTrue(present(store, FakePage({'//a': FakeLoc(count=1)}), 'k'))
        self.assertFalse(present(FakeStore([]), FakePage({}), 'absent'))


# --- resolve_get_by(): the key of a heal.page.get_by_*() call ----------------------------------

CALL = 'get_by_role("button", name="Sign in")'
HEAL = "//button[@id='login']"


class CallDriver:
    """A driver whose page is set per selector: how many elements each matches, which are
    hidden, which the browser rejects. Records each question it is asked."""

    def __init__(self, counts, hidden=(), rejected=()):
        self.counts = counts if callable(counts) else (lambda selector, _c=counts: _c.get(selector, 0))
        self.hidden, self.rejected = set(hidden), set(rejected)
        self.asked = []

    def count(self, selector, frame_chain=()):
        self.asked.append(('count', selector))
        if selector in self.rejected:
            raise InvalidSelector('rejected', selector=selector, kind='get_by')
        return self.counts(selector)

    def is_visible(self, selector, frame_chain=()):
        self.asked.append(('is_visible', selector))
        return selector not in self.hidden

    def scroll_into_view(self, selector, frame_chain=()):
        self.asked.append(('scroll', selector))

    def handle(self, selector, frame_chain=()):
        return ('handle', selector)


class CallStore(FakeStore):
    def __init__(self, cands=(CALL, HEAL), role='button'):
        super().__init__([{'xpath': x} for x in cands], role)
        self.writes = 0

    def record_success(self, key, xpath):
        self.writes += 1
        super().record_success(key, xpath)


def _resolve(store, driver, clock=None, **kwargs):
    slept = []
    clock = iter(clock or [0.0, 0.0, 999.0])
    out = resolve_get_by(store, driver, CALL, timeout_ms=1000, poll_ms=50,
                         monotonic=lambda: next(clock), sleep=slept.append, **kwargs)
    return out, slept


def test_the_call_wins_when_it_matches_one_visible_element():
    store, driver = CallStore(), CallDriver({CALL: 1, HEAL: 1})
    out, slept = _resolve(store, driver)
    assert out == ('handle', CALL) and store.succeeded == CALL and slept == []
    assert ('count', HEAL) not in driver.asked     # a saved heal is not even looked at


def test_a_saved_heal_answers_in_the_same_poll_while_the_call_matches_nothing():
    store, driver = CallStore(), CallDriver({HEAL: 1})
    out, slept = _resolve(store, driver)
    assert out == ('handle', HEAL) and store.succeeded == HEAL and slept == []


def test_nothing_matches_waits_then_raises():
    store, driver = CallStore(), CallDriver({})
    with pytest.raises(AttentionRequired) as caught:
        _resolve(store, driver, clock=[0.0, 0.5, 2.0])
    assert str(caught.value) == ('No matching element for: ' + CALL + ' (tried 2 candidate(s)).')
    assert store.writes == 0


@pytest.mark.parametrize('driver', [CallDriver({CALL: 2, HEAL: 1}),
                                    CallDriver({CALL: 1, HEAL: 1}, hidden={CALL}),
                                    CallDriver({HEAL: 1}, rejected={CALL})],
                         ids=['several', 'one hidden', 'rejected'])
def test_a_call_that_matches_something_else_passes_through_and_no_heal_answers(driver):
    store = CallStore()
    out, slept = _resolve(store, driver)
    assert out is THROUGH and slept == [] and store.writes == 0
    assert ('count', HEAL) not in driver.asked


def test_a_hidden_checkbox_is_used():
    store, driver = CallStore(role='checkbox'), CallDriver({CALL: 1}, hidden={CALL})
    assert _resolve(store, driver)[0] == ('handle', CALL)
    assert ('is_visible', CALL) not in driver.asked


def test_the_first_poll_reuses_what_the_caller_read():
    store, driver = CallStore(), CallDriver({CALL: 1})
    assert _resolve(store, driver, first=(1, True))[0] == ('handle', CALL)
    assert driver.asked == [('scroll', CALL)]
    driver = CallDriver({HEAL: 1})
    assert _resolve(CallStore(), driver, first=(0, None))[0] == ('handle', HEAL)
    assert ('count', CALL) not in driver.asked


def test_a_call_that_starts_to_match_several_during_the_wait_passes_through_at_once():
    polls = []

    def counts(selector):
        if selector == CALL:
            polls.append(1)
            return 2 if len(polls) > 1 else 0
        return 0

    out, slept = _resolve(CallStore(), CallDriver(counts), clock=[0.0, 0.0, 0.0])
    assert out is THROUGH and len(slept) == 1


def test_a_seed_row_that_cannot_run_is_dropped_with_its_error():
    store = CallStore(cands=(CALL, '//div[', HEAL))
    out, _slept = _resolve(store, CallDriver({HEAL: 1}))
    assert out == ('handle', HEAL)
    store = CallStore(cands=(CALL, 'text=Sign in'))
    driver = CallDriver({}, rejected={'text=Sign in'})
    with pytest.raises(AttentionRequired) as caught:
        _resolve(store, driver)
    assert '1 of them could not run' in str(caught.value)


def test_present_get_by_follows_the_call_first():
    assert present_get_by(CallStore(), CallDriver({CALL: 1}), CALL) is True
    assert present_get_by(CallStore(), CallDriver({HEAL: 1}), CALL) is True
    for driver in (CallDriver({CALL: 2, HEAL: 1}), CallDriver({CALL: 1, HEAL: 1}, hidden={CALL}),
                   CallDriver({HEAL: 1}, rejected={CALL}), CallDriver({})):
        store = CallStore()
        assert present_get_by(store, driver, CALL) is False
        assert store.writes == 0 and not any(a[0] == 'scroll' for a in driver.asked)


if __name__ == '__main__':
    unittest.main()
