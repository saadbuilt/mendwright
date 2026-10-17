import unittest

from mendwright import apply, report_outcome, request_heal

TERMS_HTML = ("<html><body><main><dml-checkbox><label><input type='checkbox'>"
              "<span>accept the <a href='https://shop.example.com/en/terms-and-conditions'>terms of use</a></span>"
              "</label></dml-checkbox></main></body></html>")
TERMS_XPATH = "//main//label[.//a[contains(@href,'terms-and-conditions')]]//input[@type='checkbox']"


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        return self.resp


class FakeStore:
    def __init__(self):
        self.added = []

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath, source))


class HealClientTests(unittest.TestCase):
    def test_request_posts_and_returns_proposal(self):
        http = FakeHttp(FakeResp(200, {'status': 'ok', 'xpath': '//a', 'fallbacks': ['//b']}))
        prop = request_heal({'key': 'terms'}, gateway='https://gw', download_key='k', http=http)
        self.assertEqual(prop['xpath'], '//a')
        url, body, headers = http.calls[0]
        self.assertTrue(url.endswith('/heal'))
        self.assertEqual(headers['X-Heal-Key'], 'k')  # generic default auth header

    def test_request_uses_custom_auth_header(self):
        http = FakeHttp(FakeResp(200, {'status': 'ok', 'xpath': '//a'}))
        request_heal({'key': 't'}, gateway='https://gw', download_key='k', http=http,
                     auth_header='X-Custom-Heal-Key')
        self.assertEqual(http.calls[0][2]['X-Custom-Heal-Key'], 'k')

    def test_needs_human_returns_none(self):
        http = FakeHttp(FakeResp(200, {'status': 'needs_human'}))
        self.assertIsNone(request_heal({}, gateway='https://gw', download_key='k', http=http))

    def test_non_200_returns_none(self):
        http = FakeHttp(FakeResp(429, {}))
        self.assertIsNone(request_heal({}, gateway='https://gw', download_key='k', http=http))

    def test_apply_writes_valid_candidate(self):
        store = FakeStore()
        applied, reason = apply(store, 'terms', {'xpath': TERMS_XPATH, 'fallbacks': []}, TERMS_HTML,
                                {'expectedRole': 'checkbox'})
        self.assertEqual(applied, TERMS_XPATH)
        self.assertEqual(reason, 'ok')
        self.assertEqual(store.added, [('terms', TERMS_XPATH, 'heal')])

    def test_apply_rejects_invalid_proposal_and_returns_reason(self):
        store = FakeStore()
        applied, reason = apply(store, 'terms', {'xpath': "//main//input[@id='nope']"}, TERMS_HTML,
                                {'expectedRole': 'checkbox'})
        self.assertIsNone(applied)
        self.assertNotEqual(reason, 'ok')  # a real validation reason, for the re-prompt
        self.assertEqual(store.added, [])

    def test_apply_falls_back_to_second_valid(self):
        store = FakeStore()
        applied, _reason = apply(store, 'terms',
                                 {'xpath': "//main//input[@id='nope']", 'fallbacks': [TERMS_XPATH]},
                                 TERMS_HTML, {'expectedRole': 'checkbox'})
        self.assertEqual(applied, TERMS_XPATH)

    def test_report_outcome_posts(self):
        http = FakeHttp(FakeResp(200, {}))
        report_outcome('https://gw', 'terms', '//a', True, download_key='k', http=http)
        url, body, headers = http.calls[0]
        self.assertTrue(url.endswith('/heal/outcome'))
        self.assertTrue(body['success'])


if __name__ == '__main__':
    unittest.main()


def test_apply_passes_the_call_of_a_call_key_to_the_live_check(tmp_path, monkeypatch):
    from mendwright import SelectorStore, client
    from mendwright.getby import parse

    seen = []
    monkeypatch.setattr(client, 'live_reason',
                        lambda driver, xpath, meta, frame_chain=(), call=None: seen.append(call) or '')
    html = "<main><button id='login'>Log in</button></main>"
    for key in ('get_by_role("button", name="Sign in")', 'login'):
        store = SelectorStore({'schema': 2, 'selectors': {}}, str(tmp_path / (str(len(seen)) + '.json')))
        applied, _ = client.apply(store, key, {'xpath': "//button[@id='login']"}, html,
                                  {'expectedRole': 'button'}, driver=object())
        assert applied == "//button[@id='login']"
    assert seen == [parse('get_by_role("button", name="Sign in")'), None]
