"""ANTHROPIC_BASE_URL and OPENAI_BASE_URL are honoured the way the vendor SDKs honour them,
on the SDK path and on the urllib path alike, so the key and the page go to one host the
user chose. Offline: transports and SDK modules are stubbed, nothing is sent."""

import types

import pytest

import mendwright
from mendwright.providers import AnthropicProvider, OpenAICompatibleProvider

BUNDLE = {'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
          'candidates': ['//old'], 'html': '<form></form>'}


class Http:
    def __init__(self):
        self.urls = []

    def post(self, url, **kw):
        self.urls.append(url)
        raise OSError('stub: nothing is sent')


def _fake_sdk(name, client_name):
    """A stand-in vendor SDK module that records the client kwargs and refuses to call."""
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
            raise RuntimeError('stub SDK: nothing is sent')

    return types.SimpleNamespace(**{client_name: Client}), seen


def test_anthropic_base_url_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://127.0.0.1:18799/')
    provider = AnthropicProvider(model='m', api_key='k')
    assert provider.base_url == 'http://127.0.0.1:18799'
    http = Http()
    AnthropicProvider(model='m', api_key='k', http=http).propose(BUNDLE)
    assert http.urls == ['http://127.0.0.1:18799/v1/messages']


def test_anthropic_sdk_gets_the_same_base_url(monkeypatch):
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://127.0.0.1:18799')
    sdk, seen = _fake_sdk('anthropic', 'Anthropic')
    provider = AnthropicProvider(model='m', api_key='k')
    monkeypatch.setattr(AnthropicProvider, '_load_sdk', staticmethod(lambda: sdk))
    assert provider.propose(BUNDLE) is None
    assert seen['base_url'] == 'http://127.0.0.1:18799'


def test_anthropic_default_and_explicit_base_url(monkeypatch):
    assert AnthropicProvider(model='m').base_url == 'https://api.anthropic.com'
    sdk, seen = _fake_sdk('anthropic', 'Anthropic')
    monkeypatch.setattr(AnthropicProvider, '_load_sdk', staticmethod(lambda: sdk))
    AnthropicProvider(model='m').propose(BUNDLE)
    assert seen['base_url'] == 'https://api.anthropic.com'   # never left to the SDK's env read
    monkeypatch.setenv('ANTHROPIC_BASE_URL', 'http://127.0.0.1:1')
    assert AnthropicProvider(model='m', base_url='https://proxy.example/').base_url == (
        'https://proxy.example')


def test_openai_base_url_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://api.groq.com/openai/v1')
    monkeypatch.setenv('OPENAI_API_KEY', 'not-a-real-key')
    monkeypatch.setenv('OPENAI_MODEL', 'llama-3.3-70b')
    provider = mendwright.auto_provider()
    assert provider.base_url == 'https://api.groq.com/openai/v1'
    http = Http()
    OpenAICompatibleProvider(http=http).propose(BUNDLE)
    assert http.urls == ['https://api.groq.com/openai/v1/chat/completions']
    sdk, seen = _fake_sdk('openai', 'OpenAI')
    monkeypatch.setattr(OpenAICompatibleProvider, '_try_import_sdk', staticmethod(lambda: sdk))
    OpenAICompatibleProvider().propose(BUNDLE)
    assert seen['base_url'] == 'https://api.groq.com/openai/v1'


@pytest.mark.parametrize('make', [
    lambda: OpenAICompatibleProvider.for_ollama('llama3'),
    lambda: OpenAICompatibleProvider.for_azure('https://x.openai.azure.com', 'dep', '2024-06-01'),
    lambda: OpenAICompatibleProvider(model='m', base_url='https://explicit.example/v1'),
])
def test_an_explicit_endpoint_wins_over_openai_base_url(make, monkeypatch):
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://elsewhere.example/v1')
    assert 'elsewhere' not in make().base_url


def test_openai_default_base_url():
    assert OpenAICompatibleProvider(model='m').base_url == 'https://api.openai.com/v1'
