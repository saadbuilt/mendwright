"""LocalModelProvider: the local selector model behind Ollama's /api/chat. Offline: the HTTP
transport is a fake that answers like Ollama, and picks by reading the task it was sent."""

import json
import os
import re
import urllib.error

import pytest

import mendwright
from mendwright.candidates import SYSTEM_PROMPT
from mendwright.demo import StandInPage
from mendwright.exceptions import AttentionRequired
from mendwright.gate import NOT_LIVE
from mendwright.providers import LocalModelProvider, auto_provider
from mendwright.providers.local import DEFAULT_MODEL

PAGE = """<html><body>
<header><button type="button" aria-label="Open menu"></button></header>
<main><form id="login">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <button type="button" class="ghost">Show password</button>
  <button type="submit" class="btn-primary">Log in</button>
</form>
<div style="display:none"><button type="submit" class="btn-primary">Log in</button></div>
</main></body></html>"""

BUNDLE = {'key': 'login.submit', 'expectedRole': 'button', 'expectedText': [],
          'candidates': ["//button[@id='sign-in']"], 'html': PAGE, 'page': '/login',
          'lastPage': '/login'}


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, str):
            return json.loads(self._body)
        return self._body


class FakeOllama:
    """Answers /api/chat. reply is a str, or a callable taking the user message."""

    def __init__(self, reply='{"pick": 3}', status=200, body=None, error=None, logprobs=None):
        self.reply = reply
        self.status = status
        self.body = body
        self.error = error
        self.logprobs = logprobs
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append({'url': url, 'json': json, 'timeout': timeout})
        if self.error is not None:
            raise self.error
        if self.body is not None:
            return Resp(self.status, self.body)
        user = json['messages'][1]['content']
        content = self.reply(user) if callable(self.reply) else self.reply
        data = {'model': json['model'], 'message': {'role': 'assistant', 'content': content}, 'done': True}
        if self.logprobs is not None:
            data['logprobs'] = self.logprobs
        return Resp(self.status, data)


def pick_text(text):
    """A fake model that picks the candidate whose line has ``text`` in it."""
    def reply(user):
        for line in user.splitlines():
            m = re.match(r'^(\d+)\. ', line)
            if m and text in line and 'hidden' not in line:
                return '{"pick": %s}' % m.group(1)
        return '{"pick": null, "why": "removed"}'
    return reply


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in list(os.environ):
        if name.startswith(('ANTHROPIC_', 'OPENAI_', 'GEMINI_', 'OLLAMA_', 'MENDWRIGHT_')):
            monkeypatch.delenv(name, raising=False)


def test_the_request_is_a_json_chat_at_temperature_zero_with_the_task():
    http = FakeOllama()
    prov = LocalModelProvider(http=http)
    prov.propose(BUNDLE)
    call = http.calls[0]
    assert call['url'] == 'http://127.0.0.1:11434/api/chat'
    body = call['json']
    assert body['model'] == DEFAULT_MODEL == prov.model
    assert body['format'] == 'json' and body['stream'] is False
    assert body['options']['temperature'] == 0
    assert [m['role'] for m in body['messages']] == ['system', 'user']
    assert body['messages'][0]['content'] == SYSTEM_PROMPT
    user = body['messages'][1]['content']
    assert 'key: login.submit' in user and 'Candidates (4 buttons)' in user
    assert 'page: /login (where it was last found)' in user


def test_a_pick_becomes_a_unique_xpath_with_fallbacks_for_that_element():
    import lxml.html
    prov = LocalModelProvider(http=FakeOllama(pick_text('"Log in"')))
    out = prov.propose(BUNDLE)
    assert prov.last_error is None
    assert out['reasoning'].startswith('local model picked candidate 3 of 4')
    doc = lxml.html.document_fromstring(PAGE)
    target = doc.xpath('//form//button[@type="submit"]')[0]
    for xp in [out['xpath']] + out['fallbacks']:
        assert doc.xpath(xp) == [target], xp
    assert mendwright.validate(out['xpath'], PAGE, expected_role='button').ok
    assert prov.last_answer == {'pick': 3, 'why': None, 'candidates': 4, 'confidence': None}


def test_a_null_pick_returns_none_with_the_models_reason():
    prov = LocalModelProvider(http=FakeOllama('{"pick": null, "why": "moved"}'))
    assert prov.propose(BUNDLE) is None
    assert prov.last_error.startswith('the local model says no element on this page fits: the step '
                                      'seems to have moved to another page.')
    prov = LocalModelProvider(http=FakeOllama('{"pick": null, "why": "it is gone"}'))
    assert prov.propose(BUNDLE) is None
    assert 'no element on this page fits: it is gone.' in prov.last_error


@pytest.mark.parametrize('reply', ['not json', '{"xpath": "//a"}', '{"pick": 0}'])
def test_a_reply_without_a_pick_is_reported(reply):
    prov = LocalModelProvider(http=FakeOllama(reply))
    assert prov.propose(BUNDLE) is None
    assert prov.last_error.startswith('the local model reply did not say which element it picked')


def test_a_pick_past_the_end_of_the_list_is_refused():
    prov = LocalModelProvider(http=FakeOllama('{"pick": 9}'))
    assert prov.propose(BUNDLE) is None
    assert 'picked candidate 9, but there are only 4' in prov.last_error


def test_a_missing_model_says_how_to_create_it():
    http = FakeOllama(status=404, body={'error': "model 'mendwright-selector' not found"})
    prov = LocalModelProvider(http=http)
    assert prov.propose(BUNDLE) is None
    assert prov.last_error.startswith('Ollama returned HTTP 404')
    assert "ollama create mendwright-selector" in prov.last_error


def test_ollama_not_running_is_named():
    err = urllib.error.URLError(ConnectionRefusedError(10061, 'refused'))
    prov = LocalModelProvider(http=FakeOllama(error=err))
    assert prov.propose(BUNDLE) is None
    assert prov.last_error.startswith('could not connect to http://127.0.0.1:11434 (connection refused)')
    assert "ollama serve" in prov.last_error


def test_no_element_of_the_role_means_no_call():
    http = FakeOllama()
    prov = LocalModelProvider(http=http)
    assert prov.propose(dict(BUNDLE, expectedRole='combobox')) is None
    assert http.calls == []
    assert 'the page has no combobox element' in prov.last_error


def test_after_a_not_live_refusal_it_asks_again_without_that_element():
    http = FakeOllama(pick_text('Log in'))
    prov = LocalModelProvider(http=http)
    first = prov.propose(BUNDLE)
    second = prov.propose(dict(BUNDLE, rejectedXpath=first['xpath'], rejectReason=NOT_LIVE))
    assert len(http.calls) == 2
    assert 'Candidates (3 buttons)' in http.calls[1]['json']['messages'][1]['content']
    assert second is None or second['xpath'] != first['xpath']


def test_after_any_other_refusal_it_does_not_guess_again():
    http = FakeOllama(pick_text('Log in'))
    prov = LocalModelProvider(http=http)
    out = prov.propose(dict(BUNDLE, rejectedXpath="//button[@class='ghost']",
                            rejectReason='role mismatch: got link want button'))
    assert out is None and http.calls == []
    assert 'does not guess a second time' in prov.last_error


@pytest.mark.parametrize('host, url', [
    ('localhost:11434', 'http://127.0.0.1:11434'),
    ('LOCALHOST', 'http://127.0.0.1:11434'),
    ('0.0.0.0', 'http://127.0.0.1:11434'),
    ('http://gpu-box:8080/', 'http://gpu-box:8080'),
    ('http://localhost:11434/v1', 'http://127.0.0.1:11434'),
])
def test_ollama_host_is_read_like_ollama_reads_it(monkeypatch, host, url):
    monkeypatch.setenv('OLLAMA_HOST', host)
    assert LocalModelProvider().base_url == url


def test_auto_provider_picks_it_from_mendwright_provider_or_the_model_variable(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_PROVIDER', 'local')
    prov = auto_provider()
    assert isinstance(prov, LocalModelProvider) and prov.model == DEFAULT_MODEL
    monkeypatch.delenv('MENDWRIGHT_PROVIDER')
    monkeypatch.setenv('MENDWRIGHT_LOCAL_MODEL', 'my-selector:q8')
    monkeypatch.setenv('OLLAMA_MODEL', 'llama3')
    monkeypatch.setenv('OLLAMA_HOST', 'gpu-box:11434')
    prov = auto_provider()
    assert isinstance(prov, LocalModelProvider)
    assert prov.model == 'my-selector:q8' and prov.base_url == 'http://gpu-box:11434'


def test_cloud_keys_and_the_gateway_win_over_the_local_model(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_LOCAL_MODEL', 'mendwright-selector')
    monkeypatch.setenv('MENDWRIGHT_GATEWAY', 'https://heal.example')
    assert type(auto_provider()).__name__ == 'GatewayProvider'
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'sk-ant-test')
    monkeypatch.setenv('ANTHROPIC_MODEL', 'some-model')
    assert type(auto_provider()).__name__ == 'AnthropicProvider'


V1 = """<html><body><main><form>
<label for="email">Email</label><input id="email" type="email">
<button id="sign-in" type="submit">Sign in</button>
<button id="forgot" type="button">Forgot password?</button>
</form></main></body></html>"""
V2 = """<html><body><main><form>
<label for="em">Email</label><input id="em" type="email">
<div class="actions"><button type="button">Forgot password?</button>
<button type="submit" class="primary">Log in</button></div>
</form></main></body></html>"""


def test_a_heal_through_mendwright_open_uses_the_pick(tmp_path):
    page = StandInPage(V1)
    http = FakeOllama(pick_text('"Log in"'))
    prov = LocalModelProvider(http=http)
    heal = mendwright.open(page, provider=prov, store_path=tmp_path / 'o.json', timeout_ms=0,
                           rate_per_key_per_hour=None, rate_global_per_hour=None)
    heal.coordinator.log = lambda line: None
    heal.locator('login.submit', '#sign-in', poll_ms=0)
    page.set_content(V2)
    heal.locator('login.submit', '#sign-in', poll_ms=0)
    assert heal.heals[-1].new == "//button[normalize-space()='Log in']"
    assert heal.heals[-1].source == 'model'
    # The fingerprint recorded on v1 reaches this provider (it sets wants_fingerprint).
    user = http.calls[0]['json']['messages'][1]['content']
    assert '\nit was: button "Sign in" id="sign-in" | main>form' in user


def test_only_a_provider_that_asks_gets_the_fingerprint():
    from mendwright.capture import build_bundle
    fp = {'v': 1, 'tag': 'button', 'attrs': {'id': 'go'},
          'text': 'Pay 4111111111111111 at https://pay.example/x'}
    meta = {'expectedRole': 'button', 'fingerprint': fp}
    plain = build_bundle('pay', meta, ['#go'], [], '<html></html>', {})
    assert 'fingerprint' not in plain
    local = build_bundle('pay', meta, ['#go'], [], '<html></html>', {}, with_fingerprint=True)
    assert local['fingerprint']['text'] == 'Pay [redacted number] at [URL]'
    assert LocalModelProvider.wants_fingerprint is True


def test_a_null_pick_through_mendwright_open_says_why(tmp_path):
    page = StandInPage(V1)
    prov = LocalModelProvider(http=FakeOllama('{"pick": null, "why": "removed"}'))
    heal = mendwright.open(page, provider=prov, store_path=tmp_path / 'o.json', timeout_ms=0,
                           rate_per_key_per_hour=None, rate_global_per_hour=None)
    heal.coordinator.log = lambda line: None
    heal.locator('login.submit', '#sign-in', poll_ms=0)
    page.set_content(V2)
    with pytest.raises(AttentionRequired) as info:
        heal.locator('login.submit', '#sign-in', poll_ms=0)
    assert ('LocalModelProvider returned no selector for \'login.submit\': the local model says '
            'no element on this page fits: the element is not on this page.') in str(info.value)


def test_doctor_names_the_local_model_and_its_endpoint(monkeypatch):
    from mendwright.cli import _provider_info
    monkeypatch.setenv('MENDWRIGHT_LOCAL_MODEL', 'mendwright-selector')
    info = _provider_info()
    assert info['configured'] and info['model'] == 'mendwright-selector'
    assert info['name'].startswith('LocalModelProvider')
    assert info['endpoint'] == 'http://127.0.0.1:11434'
    assert info['selected_by'] == ['MENDWRIGHT_LOCAL_MODEL']


def _logprobs(value_logprob):
    return [{'token': '{"', 'logprob': 0.0}, {'token': 'pick', 'logprob': 0.0},
            {'token': '":', 'logprob': 0.0}, {'token': ' ', 'logprob': value_logprob},
            {'token': '3', 'logprob': 0.0}, {'token': '}', 'logprob': 0.0}]


def test_it_asks_for_logprobs_and_declines_a_pick_it_is_not_sure_of():
    import math
    http = FakeOllama('{"pick": 3}', logprobs=_logprobs(math.log(0.55)))
    prov = LocalModelProvider(http=http, min_confidence=0.8)
    assert prov.propose(BUNDLE) is None
    assert http.calls[0]['json']['logprobs'] is True
    assert prov.last_error.startswith('the local model was not sure which element the step meant '
                                      '(it picked button "Log in"')
    assert 'confidence 0.55, below 0.80' in prov.last_error
    sure = LocalModelProvider(http=FakeOllama('{"pick": 3}', logprobs=_logprobs(math.log(0.97))),
                              min_confidence=0.8)
    assert sure.propose(BUNDLE) is not None
    assert round(sure.last_answer['confidence'], 2) == 0.97
