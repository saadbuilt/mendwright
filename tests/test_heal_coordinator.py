import unittest

from mendwright import HealCoordinator, resolve_with_heal
from mendwright import AttentionRequired

TERMS_HTML = ("<html><body><main><dml-checkbox><label><input type='checkbox'>"
              "<span>accept the <a href='https://shop.example.com/en/terms-and-conditions'>terms of use</a></span>"
              "</label></dml-checkbox></main></body></html>")
TERMS_XPATH = "//main//label[.//a[contains(@href,'terms-and-conditions')]]//input[@type='checkbox']"
META = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if '/heal/outcome' in url:
            return FakeResp({})
        return self.responses.pop(0)


class FakeStore:
    def __init__(self):
        self.added = []
        self.quarantined = []

    def candidates(self, key):
        return [{'xpath': '//broken'}]

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath, source))

    def quarantine(self, key, xpath):
        self.quarantined.append((key, xpath))


def coord(store, responses, guard=True):
    return HealCoordinator(store, 'https://gw', 'k', http=FakeHttp(responses),
                           html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                           guard=lambda: guard)


class CoordinatorTests(unittest.TestCase):
    def test_try_heal_applies_valid_proposal(self):
        store = FakeStore()
        c = coord(store, [FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        self.assertTrue(c.try_heal('terms', META, store.candidates('terms'), {}, None))
        self.assertEqual(store.added, [('terms', TERMS_XPATH, 'heal')])

    def test_guard_blocks_heal(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: False)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))
        self.assertEqual(http.calls, [])  # no heal attempted while a submission is pending

    def test_reprompts_once_then_fails(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'xpath': "//main//input[@id='nope']"}),
                         FakeResp({'xpath': "//main//input[@id='still-nope']"})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))
        self.assertEqual(len([u for u, _ in http.calls if u.endswith('/heal')]), 2)

    def test_reprompt_second_succeeds(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'xpath': "//main//input[@id='nope']"}),
                         FakeResp({'xpath': TERMS_XPATH})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
        self.assertTrue(c.try_heal('terms', META, [], {}, None))

    def test_confirm_failure_quarantines(self):
        store = FakeStore()
        c = coord(store, [])
        c.confirm('terms', TERMS_XPATH, success=False)
        self.assertEqual(store.quarantined, [('terms', TERMS_XPATH)])

    def test_confirm_success_does_not_quarantine(self):
        store = FakeStore()
        c = coord(store, [])
        c.confirm('terms', TERMS_XPATH, success=True)
        self.assertEqual(store.quarantined, [])

    def test_dry_run_validates_but_does_not_apply(self):
        store = FakeStore()
        logs = []
        c = HealCoordinator(store, 'https://gw', 'k',
                            http=FakeHttp([FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})]),
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True, dry_run=True, log=logs.append)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))  # never applies/retries in dry-run
        self.assertEqual(store.added, [])
        self.assertTrue(any('valid=True' in m for m in logs))


class ResolveWithHealTests(unittest.TestCase):
    def test_auto_retries_same_step_after_heal(self):
        store = FakeStore()
        c = coord(store, [FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        calls = {'n': 0}

        def resolve_fn():
            calls['n'] += 1
            if not store.added:          # healed candidate not yet present
                raise AttentionRequired('No matching element for: terms')
            return 'LOCATOR'

        result = resolve_with_heal(resolve_fn, c, 'terms', META, {}, None)
        self.assertEqual(result, 'LOCATOR')
        self.assertEqual(calls['n'], 2)  # first raised, second (post-heal) succeeded

    def test_reraises_when_no_coordinator(self):
        def resolve_fn():
            raise AttentionRequired('x')

        with self.assertRaises(AttentionRequired):
            resolve_with_heal(resolve_fn, None, 'terms', META, {}, None)


if __name__ == '__main__':
    unittest.main()
