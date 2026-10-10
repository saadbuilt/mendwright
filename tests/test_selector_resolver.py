import unittest

from mendwright import resolve, present
from mendwright import AttentionRequired


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


if __name__ == '__main__':
    unittest.main()
