"""GeminiProvider: a direct provider over Google's generateContent REST endpoint.

Fully OFFLINE + deterministic: the HTTP transport is injected and stubbed, the optional
google-genai SDK is monkeypatched (never imported for real), and no env/model id is baked
in. Imports come from the submodule because the orchestrator wires the package exports
(mendwright.providers.__init__ / top-level) afterwards.
"""

import json

import pytest

from mendwright.prompt import build_prompt
from mendwright.providers import gemini
from mendwright.providers.base import HealProvider
from mendwright.providers.gemini import GeminiProvider

BUNDLE = {
    'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False,
    'candidates': ['//old'], 'inventory': [], 'html': '<form><input type="checkbox" id="tc"></form>',
}


def _resp(text):
    """A well-formed generateContent response carrying one model text part."""
    return {'candidates': [{'content': {'role': 'model', 'parts': [{'text': text}]},
                            'finishReason': 'STOP'}]}


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeTransport:
    """Matches the GatewayProvider transport contract: .post(url, json=, timeout=) -> resp."""
    def __init__(self, payload=None, *, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=30):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'timeout': timeout})
        return FakeResp(self.status_code, self.payload)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    # The dev's own machine may have these set; clear so every test controls its own inputs.
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GEMINI_MODEL', raising=False)


# --- request shape + parse ------------------------------------------------------------

def test_request_shape_and_parse():
    text = json.dumps({'xpath': "//input[@type='checkbox']", 'fallbacks': ['//input'],
                       'reasoning': 'stable type attr'})
    transport = FakeTransport(_resp(text))
    prov = GeminiProvider(api_key='k-123', model='gemini-test', http=transport)

    proposal = prov.propose(BUNDLE)
    assert proposal == {'xpath': "//input[@type='checkbox']", 'fallbacks': ['//input'],
                        'reasoning': 'stable type attr'}

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call['url'] == ('https://generativelanguage.googleapis.com/v1beta/models/'
                           'gemini-test:generateContent?key=k-123')
    body = call['json']
    p = build_prompt(BUNDLE)
    assert body['systemInstruction']['parts'][0]['text'] == p['system']
    assert body['contents'][0]['role'] == 'user'
    assert body['contents'][0]['parts'][0]['text'] == p['user']
    assert body['generationConfig']['responseMimeType'] == 'application/json'
    assert call['timeout'] == 30


def test_thought_parts_are_ignored():
    payload = {'candidates': [{'content': {'role': 'model', 'parts': [
        {'text': 'let me think', 'thought': True},
        {'text': json.dumps({'xpath': '//ok', 'fallbacks': [], 'reasoning': 'r'})},
    ]}}]}
    prov = GeminiProvider(api_key='k', model='gemini-test', http=FakeTransport(payload))
    assert prov.propose(BUNDLE) == {'xpath': '//ok', 'fallbacks': [], 'reasoning': 'r'}


def test_model_prefix_is_stripped():
    transport = FakeTransport(_resp(json.dumps({'xpath': '//x'})))
    prov = GeminiProvider(api_key='k', model='models/gemini-test', http=transport)
    prov.propose(BUNDLE)
    url = transport.calls[0]['url']
    assert '/models/gemini-test:generateContent' in url
    assert 'models/models/' not in url


def test_api_key_is_url_quoted():
    transport = FakeTransport(_resp(json.dumps({'xpath': '//x'})))
    prov = GeminiProvider(api_key='a/b c', model='gemini-test', http=transport)
    prov.propose(BUNDLE)
    assert 'key=a%2Fb%20c' in transport.calls[0]['url']


# --- failure paths all return None (propose must never raise) -------------------------

def test_non_200_returns_none():
    transport = FakeTransport(_resp(json.dumps({'xpath': '//x'})), status_code=500)
    prov = GeminiProvider(api_key='k', model='gemini-test', http=transport)
    assert prov.propose(BUNDLE) is None


def test_blocked_prompt_no_candidates_returns_none():
    class T:
        def post(self, url, *, json=None, headers=None, timeout=30):
            return FakeResp(200, {'promptFeedback': {'blockReason': 'SAFETY'}})
    prov = GeminiProvider(api_key='k', model='gemini-test', http=T())
    assert prov.propose(BUNDLE) is None


def test_transport_raising_returns_none():
    class T:
        def post(self, url, *, json=None, headers=None, timeout=30):
            raise RuntimeError('network down')
    prov = GeminiProvider(api_key='k', model='gemini-test', http=T())
    assert prov.propose(BUNDLE) is None


def test_non_json_text_returns_none():
    prov = GeminiProvider(api_key='k', model='gemini-test',
                          http=FakeTransport(_resp('sorry, I cannot do that')))
    assert prov.propose(BUNDLE) is None


# --- constructor: key/model from arg or env, raise if missing -------------------------

def test_missing_api_key_raises_naming_env_var():
    with pytest.raises(ValueError) as exc:
        GeminiProvider(model='gemini-test')
    assert 'GEMINI_API_KEY' in str(exc.value)


def test_missing_model_raises_naming_env_var():
    with pytest.raises(ValueError) as exc:
        GeminiProvider(api_key='k')
    assert 'GEMINI_MODEL' in str(exc.value)


def test_key_and_model_from_env(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'env-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-env')
    transport = FakeTransport(_resp(json.dumps({'xpath': '//x'})))
    prov = GeminiProvider(http=transport)
    prov.propose(BUNDLE)
    assert 'models/gemini-env:generateContent?key=env-key' in transport.calls[0]['url']


def test_explicit_args_override_env(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'env-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-env')
    transport = FakeTransport(_resp(json.dumps({'xpath': '//x'})))
    prov = GeminiProvider(api_key='arg-key', model='gemini-arg', http=transport)
    prov.propose(BUNDLE)
    url = transport.calls[0]['url']
    assert 'gemini-arg' in url and 'arg-key' in url
    assert 'env' not in url


# --- path selection: injected http vs SDK vs urllib fallback --------------------------

def test_injected_transport_wins_over_sdk(monkeypatch):
    class BoomGenai:
        def Client(self, *a, **k):
            raise AssertionError('SDK must not be consulted when a transport is injected')
    monkeypatch.setattr(gemini, '_load_sdk', lambda: BoomGenai())
    transport = FakeTransport(_resp(json.dumps({'xpath': '//ok', 'fallbacks': [], 'reasoning': 'r'})))
    prov = GeminiProvider(api_key='k', model='gemini-test', http=transport)
    assert prov.propose(BUNDLE) == {'xpath': '//ok', 'fallbacks': [], 'reasoning': 'r'}


def test_sdk_branch_used_when_no_http(monkeypatch):
    sdk_calls = []

    class FakeSdkResp:
        text = json.dumps({'xpath': '//a', 'fallbacks': [], 'reasoning': 'sdk'})

    class FakeModels:
        def generate_content(self, *, model, contents, config):
            sdk_calls.append({'model': model, 'contents': contents, 'config': config})
            return FakeSdkResp()

    class FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.models = FakeModels()

    class FakeGenai:
        def Client(self, api_key=None):
            return FakeClient(api_key=api_key)

    class BoomHttp:
        def post(self, *a, **k):
            raise AssertionError('urllib fallback must not run when the SDK is present')

    monkeypatch.setattr(gemini, '_load_sdk', lambda: FakeGenai())
    monkeypatch.setattr(gemini, '_default_http', BoomHttp())

    prov = GeminiProvider(api_key='k', model='gemini-test')  # no http injected
    assert prov.propose(BUNDLE) == {'xpath': '//a', 'fallbacks': [], 'reasoning': 'sdk'}

    p = build_prompt(BUNDLE)
    assert sdk_calls[0]['model'] == 'gemini-test'
    assert sdk_calls[0]['contents'] == p['user']
    assert sdk_calls[0]['config']['system_instruction'] == p['system']
    assert sdk_calls[0]['config']['response_mime_type'] == 'application/json'


def test_no_sdk_and_no_http_falls_back_to_default_http(monkeypatch):
    transport = FakeTransport(_resp(json.dumps({'xpath': '//fallback'})))
    monkeypatch.setattr(gemini, '_load_sdk', lambda: None)
    monkeypatch.setattr(gemini, '_default_http', transport)
    prov = GeminiProvider(api_key='k', model='gemini-test')  # no http injected
    assert prov.propose(BUNDLE) == {'xpath': '//fallback', 'fallbacks': [], 'reasoning': ''}
    assert len(transport.calls) == 1


# --- contract ------------------------------------------------------------------------

def test_satisfies_heal_provider_protocol():
    prov = GeminiProvider(api_key='k', model='gemini-test',
                          http=FakeTransport(_resp(json.dumps({'xpath': '//x'}))))
    assert isinstance(prov, HealProvider)


def test_report_outcome_is_a_noop():
    prov = GeminiProvider(api_key='k', model='gemini-test',
                          http=FakeTransport(_resp(json.dumps({'xpath': '//x'}))))
    assert prov.report_outcome('terms', '//x', True) is None


# --- end-to-end through the coordinator ----------------------------------------------

def test_coordinator_applies_a_gemini_proposal(tmp_path):
    from mendwright import HealCoordinator, SelectorStore

    data = {'schema': 2, 'selectors': {'terms': {
        'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False,
        'candidates': [{'xpath': '//old', 'source': 'seed', 'status': 'active'}]}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    html = '<form><input type="checkbox" id="tc"></form>'
    healed = "//input[@type='checkbox']"

    transport = FakeTransport(_resp(json.dumps({'xpath': healed, 'fallbacks': [], 'reasoning': 'r'})))
    prov = GeminiProvider(api_key='k', model='gemini-test', http=transport)
    coord = HealCoordinator(store, provider=prov, html_provider=lambda: html,
                            inventory_provider=lambda: [], guard=lambda: True)

    applied = coord.try_heal('terms', store.meta('terms'), [], {'url': 'x'})
    assert applied == healed
