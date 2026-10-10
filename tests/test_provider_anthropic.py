"""AnthropicProvider: a direct in-process provider that calls the Anthropic Messages API.

Fully OFFLINE + deterministic: the HTTP transport is injected and stubbed, so there is no
real network, no real API key and no SDK required. We assert the request carries the
shared build_prompt() system+user and the right headers, that a canned reply parses to the
expected xpath, and that constructing without a model raises a clear, env-var-named error.
"""

import pytest

from mendwright.prompt import build_prompt
from mendwright.providers.anthropic import AnthropicProvider

BUNDLE = {
    'key': 'terms',
    'expectedRole': 'checkbox',
    'expectedText': [],
    'candidates': ['//old1', '//old2'],
    'html': '<form><input type="checkbox" id="tc"></form>',
}
HEALED = "//input[@type='checkbox']"
# The model reply, as the Anthropic Messages API returns it: content is a list of blocks.
REPLY_TEXT = ('here you go:\n{"xpath":"' + HEALED +
              '","fallbacks":["//form//input"],"reasoning":"stable type attribute"}')


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _StubTransport:
    """Records the single POST and replays a canned Messages-API response."""
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self._status = status_code
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
        return _Resp(self._status, self._payload)


def _clear_env(monkeypatch):
    monkeypatch.delenv('ANTHROPIC_MODEL', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)


def test_propose_sends_shared_prompt_and_parses_canned_reply(monkeypatch):
    _clear_env(monkeypatch)
    transport = _StubTransport({'content': [{'type': 'text', 'text': REPLY_TEXT}]})
    provider = AnthropicProvider(model='test-model-id', api_key='secret', http=transport)

    proposal = provider.propose(BUNDLE)

    # The canned JSON reply parsed through the shared parser to the right selector.
    assert proposal is not None
    assert proposal['xpath'] == HEALED
    assert proposal['fallbacks'] == ['//form//input']

    # Exactly one request, to the Messages endpoint.
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call['url'] == 'https://api.anthropic.com/v1/messages'

    # The request carries the system + user from build_prompt() verbatim (not re-invented).
    expected = build_prompt(BUNDLE)
    assert call['json']['system'] == expected['system']
    assert call['json']['messages'] == [{'role': 'user', 'content': expected['user']}]
    assert call['json']['model'] == 'test-model-id'

    # Auth + version headers are present and correct.
    assert call['headers']['x-api-key'] == 'secret'
    assert call['headers']['anthropic-version'] == '2023-06-01'


def test_missing_model_raises_naming_the_env_var(monkeypatch):
    _clear_env(monkeypatch)
    with pytest.raises(ValueError) as exc:
        AnthropicProvider(api_key='secret', http=_StubTransport({}))
    assert 'ANTHROPIC_MODEL' in str(exc.value)


def test_model_from_env_when_no_arg(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('ANTHROPIC_MODEL', 'env-model-id')
    transport = _StubTransport({'content': [{'type': 'text', 'text': REPLY_TEXT}]})
    provider = AnthropicProvider(api_key='secret', http=transport)
    provider.propose(BUNDLE)
    assert transport.calls[0]['json']['model'] == 'env-model-id'


def test_non_200_yields_no_proposal(monkeypatch):
    _clear_env(monkeypatch)
    transport = _StubTransport({'error': 'nope'}, status_code=429)
    provider = AnthropicProvider(model='test-model-id', api_key='secret', http=transport)
    assert provider.propose(BUNDLE) is None


def test_reply_without_json_yields_no_proposal(monkeypatch):
    _clear_env(monkeypatch)
    transport = _StubTransport({'content': [{'type': 'text', 'text': 'sorry, cannot help'}]})
    provider = AnthropicProvider(model='test-model-id', api_key='secret', http=transport)
    assert provider.propose(BUNDLE) is None


def test_report_outcome_is_a_noop(monkeypatch):
    _clear_env(monkeypatch)
    provider = AnthropicProvider(model='test-model-id', http=_StubTransport({}))
    assert provider.report_outcome('terms', HEALED, True) is None
