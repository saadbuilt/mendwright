"""Offline, deterministic tests for LiteLLMProvider. No litellm SDK, no network, no key:
the completion callable (and, for the fallback, the HTTP client) are injected and stubbed.
"""

import sys
from types import SimpleNamespace

import pytest

from mendwright.prompt import build_prompt
from mendwright.providers.base import HealProvider
from mendwright.providers.litellm import LiteLLMProvider

BUNDLE = {
    'key': 'terms',
    'expectedRole': 'checkbox',
    'expectedText': [],
    'destructive': False,
    'candidates': ['//old'],
    'inventory': [],
    'html': '<form><input type="checkbox" id="tc"></form>',
}
REPLY = ('Here you go: {"xpath":"//input[@type=\'checkbox\']",'
         '"fallbacks":["//form//input"],"reasoning":"stable type attr"}')


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Hermetic: a stray LITELLM_* on the host machine must not steer any test.
    for var in ('LITELLM_MODEL', 'LITELLM_API_BASE', 'LITELLM_API_KEY'):
        monkeypatch.delenv(var, raising=False)


def _reply_obj(text):
    """A litellm-style response object: resp.choices[0].message.content."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_requires_a_model(monkeypatch):
    monkeypatch.delenv('LITELLM_MODEL', raising=False)
    with pytest.raises(ValueError, match='LITELLM_MODEL'):
        LiteLLMProvider()


def test_model_from_env(monkeypatch):
    monkeypatch.setenv('LITELLM_MODEL', 'test/env-model')
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _reply_obj(REPLY)

    prov = LiteLLMProvider(completion=fake_completion)
    prov.propose(BUNDLE)
    assert captured['model'] == 'test/env-model'  # arg omitted -> env wins


def test_proposes_from_injected_completion():
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return _reply_obj(REPLY)

    prov = LiteLLMProvider('test/fake-model', completion=fake_completion)
    out = prov.propose(BUNDLE)

    # Request is built from the canonical build_prompt, as system+user messages.
    expected = build_prompt(BUNDLE)
    assert captured['model'] == 'test/fake-model'
    assert captured['messages'] == [
        {'role': 'system', 'content': expected['system']},
        {'role': 'user', 'content': expected['user']},
    ]
    # Reply is parsed by parse_proposal.
    assert out == {
        'xpath': "//input[@type='checkbox']",
        'fallbacks': ['//form//input'],
        'reasoning': 'stable type attr',
    }


def test_garbage_reply_returns_none():
    prov = LiteLLMProvider('test/fake-model',
                           completion=lambda **kw: _reply_obj('no json here at all'))
    assert prov.propose(BUNDLE) is None


def test_completion_raising_returns_none_not_raise():
    def boom(**kwargs):
        raise RuntimeError('auth failed')  # simulate an SDK/auth error

    prov = LiteLLMProvider('test/fake-model', completion=boom)
    assert prov.propose(BUNDLE) is None  # degrades to no-heal, never crashes the caller


def test_urllib_fallback_when_no_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, 'litellm', None)  # make `import litellm` raise
    calls = {}

    class StubResp:
        status_code = 200

        def json(self):
            return {'choices': [{'message': {'content': REPLY}}]}  # dict shape

    class StubHttp:
        def post(self, url, *, json=None, headers=None, timeout=None):
            calls['url'] = url
            calls['headers'] = headers
            calls['json'] = json
            return StubResp()

    prov = LiteLLMProvider('test/fake-model', api_base='https://proxy.example/v1',
                           api_key='sk-test', http=StubHttp())
    out = prov.propose(BUNDLE)

    assert calls['url'] == 'https://proxy.example/v1/chat/completions'
    assert calls['headers']['Authorization'] == 'Bearer sk-test'
    assert calls['json']['model'] == 'test/fake-model'
    assert out['xpath'] == "//input[@type='checkbox']"


def test_no_sdk_no_api_base_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, 'litellm', None)
    stub_called = {'n': 0}

    class StubHttp:
        def post(self, *a, **k):
            stub_called['n'] += 1
            raise AssertionError('http must not be called without api_base')

    prov = LiteLLMProvider('test/fake-model', http=StubHttp())
    assert prov.propose(BUNDLE) is None
    assert stub_called['n'] == 0  # no transport attempted


def test_report_outcome_is_noop():
    prov = LiteLLMProvider('test/fake-model', completion=lambda **kw: _reply_obj(REPLY))
    assert prov.report_outcome('terms', '//x', True) is None


def test_satisfies_heal_provider_protocol():
    prov = LiteLLMProvider('test/fake-model', completion=lambda **kw: _reply_obj(REPLY))
    assert isinstance(prov, HealProvider)
