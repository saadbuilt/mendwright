"""When a provider call fails, the AttentionRequired says so instead of suggesting a
stronger model. Providers report the failure through a last_error string."""

import unittest
from unittest.mock import Mock

from mendwright import AttentionRequired, HealCoordinator, resolve_with_heal

HTML = "<form><input type='checkbox' id='tc'></form>"
META = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}


class FakeStore:
    def candidates(self, key):
        return [{'xpath': "//input[@id='GONE']"}]

    def add_candidate(self, key, xpath, **kw):
        pass

    def quarantine(self, key, xpath):
        pass


class FailingProvider:
    """Returns None and explains why in last_error, like the built-in providers."""

    def __init__(self, last_error):
        self.last_error = last_error
        self.calls = 0

    def propose(self, bundle):
        self.calls += 1
        return None

    def report_outcome(self, key, xpath, success):
        pass


class RecordingJournal:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        def record(key=None, **fields):
            self.events.append((name, key, fields))
        return record


def _message(provider, journal=None):
    coord = HealCoordinator(FakeStore(), provider=provider, html_provider=lambda: HTML,
                            inventory_provider=lambda: [], guard=lambda: True, journal=journal)

    def broken():
        raise AttentionRequired('No matching element for: terms')

    try:
        resolve_with_heal(broken, coord, 'terms', META, {})
    except AttentionRequired as exc:
        return str(exc)
    raise AssertionError('expected AttentionRequired')


class ProviderErrorTests(unittest.TestCase):
    def test_failed_call_names_the_error_not_a_stronger_model(self):
        provider = FailingProvider('HTTP 401 authentication_error: invalid x-api-key')
        msg = _message(provider)
        self.assertIn("-> FailingProvider returned no selector for 'terms': HTTP 401 "
                      'authentication_error: invalid x-api-key', msg)
        self.assertNotIn('stronger model', msg)
        self.assertNotIn('mendwright.open()', msg)
        self.assertEqual(provider.calls, 1)   # no re-prompt after a failed call

    def test_genuine_empty_answer_keeps_the_stronger_model_hint(self):
        for last_error in (None, '', '   '):
            msg = _message(FailingProvider(last_error))
            self.assertIn('stronger model', msg, last_error)
            self.assertNotIn('returned no selector', msg)

    def test_provider_without_last_error_keeps_the_old_message(self):
        class Plain:
            def propose(self, bundle):
                return None

            def report_outcome(self, *a):
                pass

        self.assertIn('stronger model', _message(Plain()))

    def test_mock_last_error_is_not_mistaken_for_a_failure(self):
        provider = Mock()
        provider.propose.return_value = None
        msg = _message(provider)
        self.assertIn('stronger model', msg)

    def test_journal_records_the_provider_error(self):
        journal = RecordingJournal()
        _message(FailingProvider('connection refused'), journal=journal)
        rejected = [e for e in journal.events if e[0] == 'rejected']
        self.assertEqual(rejected, [('rejected', 'terms',
                                     {'reason': 'no proposal', 'error': 'connection refused'})])

    def test_a_weak_reply_is_quoted_without_blaming_the_api_key(self):
        from mendwright.client import _Resp
        from mendwright.providers import AnthropicProvider

        class Http:
            def post(self, url, **kw):
                return _Resp(200, '{"content": [{"type": "text", "text": "{\\"xpath\\": null}"}]}')

        msg = _message(AnthropicProvider(model='m', api_key='k', http=Http()))
        self.assertIn("AnthropicProvider returned no selector for 'terms': ", msg)
        self.assertIn('Try a stronger model', msg)
        self.assertNotIn('API key', msg)
        self.assertNotIn('.).', msg)

    def test_a_gateway_rate_limit_is_quoted_without_blaming_the_api_key(self):
        from mendwright.client import _Resp
        from mendwright.providers import GatewayProvider

        class Http:
            def post(self, url, **kw):
                return _Resp(200, '{"status": "needs_human", "reason": "rate limit"}')

        msg = _message(GatewayProvider('https://gw.example.test', 'key', http=Http()))
        self.assertIn("GatewayProvider returned no selector for 'terms': ", msg)
        self.assertIn('rate limit', msg)
        self.assertNotIn('API key', msg)
        self.assertNotIn('stronger model', msg)


if __name__ == '__main__':
    unittest.main()
