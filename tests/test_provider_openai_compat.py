"""OpenAICompatibleProvider: one adapter, many base_urls. Fully OFFLINE and deterministic —
the HTTP transport is stubbed (no real network, no API key, no SDK, no randomness/clock).
Asserts URL + header shaping for OpenAI, keyless Ollama and Azure, and that the canonical
parser turns the stubbed reply into a validated-shape proposal.
"""

import json

import pytest

from mendwright.providers.openai_compat import OpenAICompatibleProvider

BUNDLE = {'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
          'candidates': ['//old1', '//old2'], 'html': '<form></form>'}
REPLY = '{"xpath":"//a","fallbacks":["//b","//c"],"reasoning":"stable attr"}'


class StubResp:
    def __init__(self, content, status=200):
        self.status_code = status
        self._content = content

    def json(self):
        return {'choices': [{'message': {'content': self._content}}]}


class StubHttp:
    """Matches the client transport seam: post(url, *, json, headers, timeout) -> resp."""
    def __init__(self, content=REPLY, status=200):
        self._resp = StubResp(content, status)
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
        return self._resp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Keep construction deterministic regardless of the dev's shell environment.
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)


def test_openai_default_url_bearer_auth_and_json_body():
    http = StubHttp()
    prov = OpenAICompatibleProvider(model='gpt-test', api_key='sk-test', http=http)
    out = prov.propose(BUNDLE)

    call = http.calls[0]
    assert call['url'] == 'https://api.openai.com/v1/chat/completions'
    assert call['headers']['Authorization'] == 'Bearer sk-test'
    assert 'api-key' not in call['headers']
    # messages come straight from build_prompt: a system then a user turn.
    msgs = call['json']['messages']
    assert [m['role'] for m in msgs] == ['system', 'user']
    assert 'EXACTLY ONE' in msgs[0]['content']
    assert '//old1' in msgs[1]['content'] and 'terms' in msgs[1]['content']
    assert call['json']['model'] == 'gpt-test'
    assert call['json']['response_format'] == {'type': 'json_object'}
    # parse_proposal shaping is reused, not re-invented.
    assert out == {'xpath': '//a', 'fallbacks': ['//b', '//c'], 'reasoning': 'stable attr'}


def test_ollama_is_keyless_and_targets_localhost():
    http = StubHttp()
    prov = OpenAICompatibleProvider.for_ollama('llama3.1', http=http)
    prov.propose(BUNDLE)

    call = http.calls[0]
    assert call['url'] == 'http://127.0.0.1:11434/v1/chat/completions'
    assert 'Authorization' not in call['headers']  # keyless: no auth header at all
    assert 'api-key' not in call['headers']
    assert call['json']['model'] == 'llama3.1'


def test_azure_url_version_query_and_api_key_header():
    http = StubHttp()
    prov = OpenAICompatibleProvider.for_azure(
        'https://my-res.openai.azure.com', 'gpt4o-deploy', '2024-02-15-preview',
        key='az-secret', model='gpt-4o', http=http)
    prov.propose(BUNDLE)

    call = http.calls[0]
    assert call['url'] == ('https://my-res.openai.azure.com/openai/deployments/gpt4o-deploy'
                           '/chat/completions?api-version=2024-02-15-preview')
    assert call['headers']['api-key'] == 'az-secret'
    assert 'Authorization' not in call['headers']  # Azure uses api-key, not Bearer


def test_azure_defaults_body_model_to_deployment_when_model_omitted():
    http = StubHttp()
    prov = OpenAICompatibleProvider.for_azure(
        'https://my-res.openai.azure.com', 'gpt4o-deploy', '2024-02-15-preview',
        key='az-secret', http=http)
    prov.propose(BUNDLE)
    assert http.calls[0]['json']['model'] == 'gpt4o-deploy'


def test_model_from_env_when_arg_absent(monkeypatch):
    monkeypatch.setenv('OPENAI_MODEL', 'env-model')
    http = StubHttp()
    prov = OpenAICompatibleProvider(api_key='sk', http=http)
    prov.propose(BUNDLE)
    assert http.calls[0]['json']['model'] == 'env-model'


def test_missing_model_raises_naming_the_env_var():
    with pytest.raises(ValueError) as excinfo:
        OpenAICompatibleProvider(api_key='sk')
    assert 'OPENAI_MODEL' in str(excinfo.value)


def test_response_format_can_be_disabled():
    http = StubHttp()
    prov = OpenAICompatibleProvider(model='m', api_key='sk', http=http,
                                    response_format_json=False)
    prov.propose(BUNDLE)
    assert 'response_format' not in http.calls[0]['json']


def test_non_200_yields_no_proposal():
    http = StubHttp(status=500)
    prov = OpenAICompatibleProvider(model='m', api_key='sk', http=http)
    assert prov.propose(BUNDLE) is None


def test_unparseable_reply_yields_no_proposal():
    http = StubHttp(content='sorry, I cannot help with that')
    prov = OpenAICompatibleProvider(model='m', api_key='sk', http=http)
    assert prov.propose(BUNDLE) is None


def test_report_outcome_is_a_safe_noop():
    prov = OpenAICompatibleProvider(model='m', api_key='sk', http=StubHttp())
    assert prov.report_outcome('terms', '//a', True) is None  # never raises, never networks


def test_messages_are_valid_json_serialisable():
    # The body must be exactly what an OpenAI-compatible endpoint expects.
    http = StubHttp()
    prov = OpenAICompatibleProvider(model='m', api_key='sk', http=http)
    prov.propose(BUNDLE)
    json.dumps(http.calls[0]['json'])  # raises if anything non-serialisable slipped in
