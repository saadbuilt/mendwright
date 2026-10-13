"""provider.last_error: every provider explains why propose() returned None.

Offline and deterministic. Each provider gets an injected fake transport (or a fake
boto3 client / litellm completion callable) that answers with HTTP 401, 404 or 429, raises
"connection refused", returns a body that is not JSON, or returns model text that is not
JSON. No socket is opened, no SDK is imported and no real key is used.

Contract checked here (the coordinator relies on it): ``last_error`` is None after
construction, None after a propose() that returned a proposal, and a short str after a
propose() that returned None. It never contains the API key.
"""

import json
import sys
import urllib.error

import pytest

from mendwright import request_heal
from mendwright.providers import (AnthropicProvider, BedrockProvider, GatewayProvider,
                                  GeminiProvider, LiteLLMProvider, OpenAICompatibleProvider)
from mendwright.providers import _errors, gemini as gemini_mod

KEY = 'sk-test-SECRETKEY0123456789'
BUNDLE = {'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
          'candidates': ['//old'], 'html': '<form><input type="checkbox" id="tc"></form>'}
GOOD_TEXT = '{"xpath": "//input[@id=\'tc\']", "fallbacks": [], "reasoning": "id"}'


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ('ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'OPENAI_API_KEY', 'OPENAI_MODEL',
                 'GEMINI_API_KEY', 'GEMINI_MODEL', 'LITELLM_MODEL', 'LITELLM_API_BASE',
                 'LITELLM_API_KEY', 'BEDROCK_MODEL', 'AWS_REGION', 'OLLAMA_HOST',
                 'OLLAMA_MODEL', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY'):
        monkeypatch.delenv(name, raising=False)
    # LiteLLM's no-SDK fallback is the path under test; never import the real package.
    monkeypatch.setitem(sys.modules, 'litellm', None)


class FakeResp:
    def __init__(self, status, payload=None, *, not_json=False):
        self.status_code = status
        self._payload = payload
        self._not_json = not_json

    def json(self):
        if self._not_json:
            raise ValueError('Expecting value: line 1 column 1 (char 0)')
        return self._payload


class FakeHttp:
    """post() returns the queued responses in order, or raises a queued exception."""
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append(url)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _refused():
    # What urllib raises when nothing listens on the port. Built by hand: no socket.
    return urllib.error.URLError(ConnectionRefusedError(10061, 'No connection could be made '
                                                       'because the target machine actively '
                                                       'refused it'))


# One row per HTTP-based provider: how to build it, how a vendor wraps model text in a 200,
# how it shapes an error body, and the env vars its hints should name.
def _anthropic_ok(text):
    return {'content': [{'type': 'text', 'text': text}]}


def _anthropic_err(kind, message):
    return {'type': 'error', 'error': {'type': kind, 'message': message}}


def _openai_ok(text):
    return {'choices': [{'message': {'role': 'assistant', 'content': text}}]}


def _openai_err(kind, message):
    return {'error': {'message': message, 'type': 'invalid_request_error', 'code': kind}}


def _gemini_ok(text):
    return {'candidates': [{'content': {'parts': [{'text': text}]}, 'finishReason': 'STOP'}]}


def _gemini_err(kind, message):
    return {'error': {'code': 400, 'message': message, 'status': kind}}


def _gateway_err(kind, message):
    return {'status': 'error', 'reason': message}


PROVIDERS = {
    'anthropic': dict(
        make=lambda http: AnthropicProvider(model='test-model', api_key=KEY, http=http),
        ok=_anthropic_ok, err=_anthropic_err, who='Anthropic',
        key_hint='ANTHROPIC_API_KEY', model_hint='ANTHROPIC_MODEL'),
    'openai': dict(
        make=lambda http: OpenAICompatibleProvider(model='test-model', api_key=KEY, http=http),
        ok=_openai_ok, err=_openai_err, who='OpenAI',
        key_hint='OPENAI_API_KEY', model_hint='OPENAI_MODEL'),
    'ollama': dict(
        make=lambda http: OpenAICompatibleProvider.for_ollama('llama-test', api_key=KEY, http=http),
        ok=_openai_ok, err=_openai_err, who='Ollama',
        key_hint='api_key', model_hint='OLLAMA_MODEL'),
    'gemini': dict(
        make=lambda http: GeminiProvider(api_key=KEY, model='test-model', http=http),
        ok=_gemini_ok, err=_gemini_err, who='Gemini',
        key_hint='GEMINI_API_KEY', model_hint='GEMINI_MODEL'),
    'litellm': dict(
        make=lambda http: LiteLLMProvider('test/model', api_base='https://proxy.example/v1',
                                          api_key=KEY, http=http),
        ok=_openai_ok, err=_openai_err, who='LiteLLM',
        key_hint='LITELLM_API_KEY', model_hint='LITELLM_MODEL'),
    'gateway': dict(
        make=lambda http: GatewayProvider('https://gw.example', KEY, http=http),
        ok=None, err=_gateway_err, who='the gateway',
        key_hint='MENDWRIGHT_KEY', model_hint='MENDWRIGHT_GATEWAY'),
}
NAMES = sorted(PROVIDERS)


def _run(name, *outcomes):
    spec = PROVIDERS[name]
    provider = spec['make'](FakeHttp(*outcomes))
    assert provider.last_error is None  # set in __init__ too, not only after a call
    return provider, provider.propose(BUNDLE)


def _assert_clean(message):
    assert isinstance(message, str) and message
    assert KEY not in message
    assert chr(0x2014) not in message  # no em-dashes in user-facing text
    assert len(message) < 450


@pytest.mark.parametrize('name', NAMES)
def test_http_401_names_the_status_and_the_key_setting(name):
    spec = PROVIDERS[name]
    # The vendor echoes the key back; it must not reach last_error.
    body = spec['err']('authentication_error', 'invalid x-api-key ' + KEY)
    provider, proposal = _run(name, FakeResp(401, body))
    assert proposal is None
    _assert_clean(provider.last_error)
    assert provider.last_error.startswith(spec['who'] + ' returned HTTP 401')
    assert 'invalid x-api-key' in provider.last_error
    assert spec['key_hint'] in provider.last_error


@pytest.mark.parametrize('name', NAMES)
def test_http_404_points_at_the_model_setting(name):
    spec = PROVIDERS[name]
    body = spec['err']('model_not_found', "model 'x' not found")
    provider, proposal = _run(name, FakeResp(404, body))
    assert proposal is None
    _assert_clean(provider.last_error)
    assert 'HTTP 404' in provider.last_error
    assert "model 'x' not found" in provider.last_error
    assert spec['model_hint'] in provider.last_error


@pytest.mark.parametrize('name', NAMES)
def test_http_429_says_rate_limited_and_to_retry_later(name):
    provider, proposal = _run(name, FakeResp(429, {}))
    assert proposal is None
    _assert_clean(provider.last_error)
    assert PROVIDERS[name]['who'] + ' returned HTTP 429 (rate limited)' in provider.last_error
    assert 'retry' in provider.last_error.lower()


@pytest.mark.parametrize('name', NAMES)
def test_connection_refused(name):
    provider, proposal = _run(name, _refused())
    assert proposal is None
    _assert_clean(provider.last_error)
    assert provider.last_error.startswith('could not connect to ')
    assert '(connection refused)' in provider.last_error


@pytest.mark.parametrize('name', NAMES)
def test_response_body_that_is_not_json(name):
    provider, proposal = _run(name, FakeResp(200, not_json=True))
    assert proposal is None
    _assert_clean(provider.last_error)
    assert 'not valid JSON' in provider.last_error
    assert provider.last_error.startswith(PROVIDERS[name]['who'] + ' returned a response')


@pytest.mark.parametrize('name', [n for n in NAMES if n != 'gateway'])
def test_model_text_that_is_not_json(name):
    spec = PROVIDERS[name]
    provider, proposal = _run(name, FakeResp(200, spec['ok']('Sorry, I cannot help with that.')))
    assert proposal is None
    assert provider.last_error.startswith('the model reply was not valid JSON')
    assert spec['model_hint'] in provider.last_error


@pytest.mark.parametrize('name', [n for n in NAMES if n != 'gateway'])
def test_model_json_without_an_xpath(name):
    spec = PROVIDERS[name]
    provider, _ = _run(name, FakeResp(200, spec['ok']('{"reasoning": "no idea"}')))
    assert provider.last_error.startswith('the model reply did not include an xpath')


NOT_HERE = ('{"xpath": null, "reason": "The Continue button is not on this page; it seems to '
            'have moved to the next step. sk-test-SECRETKEY0123456789"}')


@pytest.mark.parametrize('name', [n for n in NAMES if n != 'gateway'])
def test_a_model_that_says_no_element_fits(name):
    spec = PROVIDERS[name]
    provider, proposal = _run(name, FakeResp(200, spec['ok'](NOT_HERE)))
    assert proposal is None
    assert provider.last_error == (
        'the model says no element on this page fits: The Continue button is not on this page; '
        'it seems to have moved to the next step. [redacted]. If the step moved to another '
        'page or the element was removed, update your script.')


def test_a_gateway_that_passes_on_a_no_element_reply():
    provider, _ = _run('gateway', FakeResp(200, {
        'status': 'needs_human',
        'reason': 'the model says no element on this page fits: the button moved'}))
    assert provider.last_error == (
        'the gateway did not return a selector (needs_human: the model says no element on this '
        'page fits: the button moved). If the step moved to another page or the element was '
        'removed, update your script.')


def test_the_coordinator_quotes_a_no_element_reply(tmp_path):
    from mendwright import HealCoordinator, load_store
    from mendwright.exceptions import AttentionRequired
    from mendwright.coordinator import resolve_with_heal

    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('continue', '//button[@id="continue"]', source='code')
    store.set_meta('continue', role='button')
    provider = AnthropicProvider(model='m', api_key=KEY, http=FakeHttp(
        FakeResp(200, PROVIDERS['anthropic']['ok']('{"xpath": null, "reason": "It moved."}'))))
    coordinator = HealCoordinator(store, provider=provider, html_provider=lambda: '<p>x</p>',
                                  inventory_provider=lambda: [], guard=lambda: True)

    def missing():
        raise AttentionRequired('No matching element for: continue (tried 1 candidate(s)).')
    with pytest.raises(AttentionRequired) as caught:
        resolve_with_heal(missing, coordinator, 'continue', store.meta('continue'), {})
    assert str(caught.value).endswith(
        "\n  -> AnthropicProvider returned no selector for 'continue': the model says no "
        'element on this page fits: It moved. If the step moved to another page or the element '
        'was removed, update your script.')


@pytest.mark.parametrize('name', NAMES)
def test_last_error_resets_to_none_after_a_success(name):
    spec = PROVIDERS[name]
    ok = (FakeResp(200, {'status': 'ok', 'xpath': '//a', 'fallbacks': []}) if name == 'gateway'
          else FakeResp(200, spec['ok'](GOOD_TEXT)))
    provider = spec['make'](FakeHttp(FakeResp(401, {}), ok))
    assert provider.propose(BUNDLE) is None
    assert provider.last_error
    assert provider.propose(BUNDLE) is not None
    assert provider.last_error is None


@pytest.mark.parametrize('name', NAMES)
def test_long_vendor_messages_are_cut(name):
    spec = PROVIDERS[name]
    provider, _ = _run(name, FakeResp(400, spec['err']('bad', 'x' * 5000)))
    assert 'x' * 190 in provider.last_error
    assert 'x' * 300 not in provider.last_error
    assert '...' in provider.last_error


def test_ollama_refused_asks_whether_ollama_is_running():
    provider, _ = _run('ollama', _refused())
    assert provider.last_error.startswith(
        'could not connect to http://127.0.0.1:11434/v1 (connection refused). '
        'Is Ollama running?')
    assert 'OLLAMA_HOST' in provider.last_error


def test_openai_compatible_names_the_host_it_talks_to():
    provider = OpenAICompatibleProvider(base_url='https://api.groq.com/openai/v1',
                                        model='m', api_key=KEY, http=FakeHttp(FakeResp(401, {})))
    provider.propose(BUNDLE)
    assert provider.last_error.startswith('api.groq.com returned HTTP 401 (unauthorized)')


def test_azure_names_azure():
    provider = OpenAICompatibleProvider.for_azure(
        'https://res.openai.azure.com', 'dep', '2024-06-01', key=KEY,
        http=FakeHttp(FakeResp(404, {})))
    provider.propose(BUNDLE)
    assert provider.last_error.startswith('Azure OpenAI returned HTTP 404')
    assert 'for_azure' in provider.last_error


def test_gemini_key_in_the_url_never_reaches_last_error():
    # Gemini puts the key in the query string. A transport whose exception message echoes
    # the full URL must not leak it.
    class Leaky:
        def post(self, url, *, json=None, headers=None, timeout=None):
            raise RuntimeError('request to ' + url + ' failed')
    provider = GeminiProvider(api_key='AIzaSyNOTAREALKEY000000', model='m', http=Leaky())
    assert provider.propose(BUNDLE) is None
    assert 'AIzaSyNOTAREALKEY000000' not in provider.last_error
    assert 'key=[redacted]' in provider.last_error


def test_gemini_blocked_prompt_says_so():
    provider, _ = _run('gemini', FakeResp(200, {'promptFeedback': {'blockReason': 'SAFETY'}}))
    assert provider.last_error.startswith('Gemini returned a reply with no text '
                                          '(prompt blocked: SAFETY)')


def test_anthropic_empty_content_mentions_the_stop_reason():
    provider, _ = _run('anthropic', FakeResp(200, {'content': [], 'stop_reason': 'max_tokens'}))
    assert provider.last_error.startswith('Anthropic returned a reply with no text '
                                          '(stop reason: max_tokens)')


def test_timeout_says_timed_out_and_names_timeout_argument():
    provider, _ = _run('anthropic', urllib.error.URLError(TimeoutError('timed out')))
    assert provider.last_error.startswith('the request to https://api.anthropic.com timed out')
    assert 'timeout=' in provider.last_error


def test_unserialisable_bundle_is_reported_not_raised():
    provider = AnthropicProvider(model='m', api_key=KEY, http=FakeHttp(FakeResp(200, {})))
    assert provider.propose(dict(BUNDLE, inventory=[object()])) is None
    assert provider.last_error.startswith('could not build the prompt')
    assert 'inventory_provider' in provider.last_error


# --- SDK-shaped exceptions (the path users with an SDK installed take) ------------------

class _StatusError(Exception):
    """Shaped like anthropic/openai APIStatusError: status_code + parsed JSON body."""
    def __init__(self, status_code, body):
        super().__init__('Error code: %d - %r' % (status_code, body))
        self.status_code = status_code
        self.body = body
        self.message = str(self)


class APIConnectionError(Exception):
    pass


def test_anthropic_sdk_status_error(monkeypatch):
    class Messages:
        def create(self, **kwargs):
            raise _StatusError(401, _anthropic_err('authentication_error', 'invalid x-api-key'))

    class Client:
        def __init__(self, **kwargs):
            self.messages = Messages()

    sdk = type('sdk', (), {'Anthropic': Client})
    monkeypatch.setattr(AnthropicProvider, '_load_sdk', staticmethod(lambda: sdk))
    provider = AnthropicProvider(model='m', api_key=KEY)
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith(
        'Anthropic returned HTTP 401 (authentication_error): invalid x-api-key.')


def test_openai_sdk_connection_error(monkeypatch):
    class Completions:
        def create(self, **kwargs):
            try:
                raise ConnectionRefusedError(111, 'Connection refused')
            except ConnectionRefusedError as inner:
                raise APIConnectionError('Connection error.') from inner

    class Client:
        def __init__(self, **kwargs):
            self.chat = type('chat', (), {'completions': Completions()})()

    sdk = type('sdk', (), {'OpenAI': Client})
    monkeypatch.setattr(OpenAICompatibleProvider, '_try_import_sdk', staticmethod(lambda: sdk))
    provider = OpenAICompatibleProvider.for_ollama('llama-test')
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith(
        'could not connect to http://127.0.0.1:11434/v1 (connection refused). Is Ollama running?')


def test_gemini_sdk_api_error(monkeypatch):
    class APIError(Exception):  # google-genai: code (int), status (str), message (str)
        def __init__(self):
            super().__init__('403 PERMISSION_DENIED')
            self.code, self.status, self.message = 403, 'PERMISSION_DENIED', 'Permission denied.'

    class Models:
        def generate_content(self, **kwargs):
            raise APIError()

    class Client:
        def __init__(self, api_key=None):
            self.models = Models()

    monkeypatch.setattr(gemini_mod, '_load_sdk', lambda: type('genai', (), {'Client': Client}))
    provider = GeminiProvider(api_key=KEY, model='m')
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith(
        'Gemini returned HTTP 403 (PERMISSION_DENIED): Permission denied.')
    assert 'GEMINI_API_KEY' in provider.last_error


# --- LiteLLM through its completion() callable -------------------------------------------

def _litellm_error(cls_name, status_code, message):
    cls = type(cls_name, (Exception,), {})
    exc = cls(message)
    exc.status_code, exc.message, exc.llm_provider = status_code, message, 'anthropic'
    return exc


@pytest.mark.parametrize('exc, expected', [
    (_litellm_error('AuthenticationError', 401,
                    'litellm.AuthenticationError: AnthropicException - '
                    '{"type":"error","error":{"type":"authentication_error",'
                    '"message":"invalid x-api-key"}}'),
     'LiteLLM returned HTTP 401 (authentication_error): invalid x-api-key.'),
    (_litellm_error('NotFoundError', 404, 'litellm.NotFoundError: model x not found'),
     'LiteLLM returned HTTP 404 (not found): model x not found.'),
    (_litellm_error('RateLimitError', 429, 'litellm.RateLimitError: slow down'),
     'LiteLLM returned HTTP 429 (rate limited): slow down.'),
    (_litellm_error('APIConnectionError', 500,
                    'litellm.APIConnectionError: OllamaException - [Errno 111] Connection refused'),
     'could not connect to LiteLLM (connection refused).'),
    (_litellm_error('Timeout', 408, 'litellm.Timeout: request timed out'),
     'the request to LiteLLM timed out.'),
])
def test_litellm_completion_errors(exc, expected):
    def completion(**kwargs):
        raise exc
    provider = LiteLLMProvider('anthropic/test', completion=completion)
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith(expected)


def test_litellm_completion_bad_reply():
    reply = {'choices': [{'message': {'content': 'no json here'}}]}
    provider = LiteLLMProvider('x/y', completion=lambda **kw: reply)
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith('the model reply was not valid JSON')


def test_litellm_without_sdk_or_api_base_gives_an_install_hint():
    provider = LiteLLMProvider('x/y')
    assert provider.propose(BUNDLE) is None
    assert 'pip install "mendwright[litellm]"' in provider.last_error
    assert 'LITELLM_API_BASE' in provider.last_error


# --- Bedrock through an injected boto3-shaped client -------------------------------------

class ClientError(Exception):
    """Shaped like botocore.exceptions.ClientError."""
    def __init__(self, status, code, message):
        super().__init__('An error occurred (%s) when calling the Converse operation: %s'
                         % (code, message))
        self.response = {'Error': {'Code': code, 'Message': message},
                         'ResponseMetadata': {'HTTPStatusCode': status}}


class EndpointConnectionError(Exception):
    pass


class NoCredentialsError(Exception):
    pass


class _BedrockClient:
    def __init__(self, outcome):
        self.outcome = outcome

    def converse(self, **kwargs):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return {'output': {'message': {'content': [{'text': self.outcome}]}},
                'stopReason': 'end_turn'}


def _endpoint_refused():
    try:
        raise ConnectionRefusedError(10061, 'actively refused it')
    except ConnectionRefusedError as inner:
        try:
            raise EndpointConnectionError('Could not connect to the endpoint URL') from inner
        except EndpointConnectionError as exc:
            return exc


@pytest.mark.parametrize('outcome, expected', [
    (ClientError(401, 'UnrecognizedClientException', 'The security token is invalid.'),
     'Bedrock in eu-west-1 returned HTTP 401 (UnrecognizedClientException): '
     'The security token is invalid. Check your AWS credentials'),
    (ClientError(404, 'ResourceNotFoundException', 'Model not found.'),
     'Bedrock in eu-west-1 returned HTTP 404 (ResourceNotFoundException): Model not found. '
     'Check the model id in BEDROCK_MODEL'),
    (ClientError(429, 'ThrottlingException', 'Too many requests.'),
     'Bedrock in eu-west-1 returned HTTP 429 (ThrottlingException): Too many requests. Wait'),
    (_endpoint_refused(),
     'could not connect to Bedrock in eu-west-1 (connection refused). Check AWS_REGION'),
    (NoCredentialsError('Unable to locate credentials'),
     'no AWS credentials were found for Bedrock. Configure them'),
    ('Sorry, no JSON today.', 'the model reply was not valid JSON. Try a stronger model'),
    ('', 'Bedrock in eu-west-1 returned a reply with no text (stop reason: end_turn).'),
])
def test_bedrock_errors(outcome, expected):
    provider = BedrockProvider(model='m', region='eu-west-1', client=_BedrockClient(outcome))
    assert provider.last_error is None
    assert provider.propose(BUNDLE) is None
    assert provider.last_error.startswith(expected)


def test_bedrock_last_error_resets_after_success():
    client = _BedrockClient(ClientError(429, 'ThrottlingException', 'slow'))
    provider = BedrockProvider(model='m', client=client)
    provider.propose(BUNDLE)
    assert provider.last_error
    client.outcome = GOOD_TEXT
    assert provider.propose(BUNDLE) is not None
    assert provider.last_error is None


# --- request_heal keeps its contract; on_error is opt-in ---------------------------------

def test_request_heal_reports_through_on_error():
    reasons = []
    out = request_heal({}, gateway='https://gw', download_key=KEY,
                       http=FakeHttp(FakeResp(429, {'status': 'error', 'reason': 'slow down'})),
                       on_error=reasons.append)
    assert out is None
    assert reasons == ['the gateway returned HTTP 429 (rate limited): slow down. '
                       "Wait for the gateway's rate limit window to reset, then retry."]


def test_request_heal_without_on_error_is_unchanged():
    assert request_heal({}, gateway='https://gw', download_key='k',
                        http=FakeHttp(FakeResp(401, {}))) is None
    proposal = request_heal({}, gateway='https://gw', download_key='k',
                            http=FakeHttp(FakeResp(200, {'status': 'ok', 'xpath': '//a'})))
    assert proposal == {'status': 'ok', 'xpath': '//a'}


def test_request_heal_on_error_that_raises_is_ignored():
    def boom(message):
        raise RuntimeError('caller bug')
    assert request_heal({}, gateway='https://gw', download_key='k',
                        http=FakeHttp(_refused()), on_error=boom) is None


def test_request_heal_json_list_body_returns_none():
    reasons = []
    assert request_heal({}, gateway='https://gw', download_key='k',
                        http=FakeHttp(FakeResp(200, ['not', 'a', 'dict'])),
                        on_error=reasons.append) is None
    assert 'not valid JSON' in reasons[0]


def test_gateway_needs_human_carries_the_reason():
    provider, _ = _run('gateway', FakeResp(200, {'status': 'needs_human',
                                                 'reason': 'no valid proposal'}))
    assert provider.last_error == ('the gateway did not return a selector '
                                   '(needs_human: no valid proposal). Try a stronger model '
                                   'on the gateway, or repair the selector by hand.')


def test_gateway_ok_without_xpath():
    provider, _ = _run('gateway', FakeResp(200, {'status': 'ok'}))
    assert provider.last_error.startswith('the gateway reply did not include an xpath.')


def test_gateway_connect_error_hides_credentials_in_the_url():
    provider = GatewayProvider('https://user:pw@gw.example/base/?token=abc', KEY,
                               http=FakeHttp(_refused()))
    provider.propose(BUNDLE)
    assert provider.last_error.startswith(
        'could not connect to https://gw.example/base (connection refused).')
    assert 'user:pw' not in provider.last_error
    assert 'token' not in provider.last_error


# --- the helpers themselves --------------------------------------------------------------

def test_scrub_masks_keys_tokens_and_secret_query_params():
    text = ('key sk-proj-abcdef123456 and AIzaSyABCDEFGHIJKLMNOP and Bearer abc.def.ghi.jkl.mno '
            'at https://x.example/v1?key=s3cret&alt=json and my-own-secret')
    out = _errors.scrub(text, secrets=('my-own-secret',))
    for leaked in ('sk-proj-abcdef123456', 'AIzaSyABCDEFGHIJKLMNOP', 'abc.def.ghi', 's3cret',
                   'my-own-secret'):
        assert leaked not in out
    assert 'alt=json' in out
    # Ordinary wording is left alone.
    prose = 'Basic authentication failed: the bearer token is invalid'
    assert _errors.scrub(prose) == prose


def test_scrub_masks_other_secret_shapes():
    text = ('aws AKIAIOSFODNN7EXAMPLE, azure 0123456789abcdef0123456789abcdef, '
            'jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl, '
            'header x-api-key: abcd1234efgh5678 and api-key="zyxw9876vuts5432" '
            'and Authorization: Basic dXNlcjpwYXNzd29yZDEyMzQ=')
    out = _errors.scrub(text)
    for leaked in ('AKIAIOSFODNN7EXAMPLE', '0123456789abcdef0123456789abcdef', 'eyJhbGciOiJIUzI1NiJ9',
                   'abcd1234efgh5678', 'zyxw9876vuts5432', 'dXNlcjpwYXNzd29yZDEyMzQ='):
        assert leaked not in out
    # Model ids, short numbers and request wording survive.
    prose = 'model claude-test-model not found; retry in 30 seconds (request 4242)'
    assert _errors.scrub(prose) == prose


@pytest.mark.parametrize('echo', [
    'Incorrect API key provided: {key}.',
    'invalid x-api-key: {key}',
    'bad request to https://api.example/v1?key={key}',
])
def test_an_echoed_key_never_reaches_last_error_the_error_or_the_log(echo, caplog):
    import logging
    from mendwright import AttentionRequired, HealCoordinator, resolve_with_heal
    body = _anthropic_err('authentication_error', echo.format(key=KEY))
    provider = AnthropicProvider(model='test-model', api_key=KEY, http=FakeHttp(FakeResp(401, body)))

    class Store:
        def candidates(self, key):
            return [{'xpath': "//input[@id='GONE']"}]

        def meta(self, key):
            return {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}

    coord = HealCoordinator(Store(), provider=provider,
                            html_provider=lambda: "<input type='checkbox' id='x'>",
                            inventory_provider=lambda: [], guard=lambda: True)

    def broken():
        raise AttentionRequired('No matching element for: terms (tried 1 candidate(s)).')

    with caplog.at_level(logging.DEBUG, logger='mendwright'):
        with pytest.raises(AttentionRequired) as excinfo:
            resolve_with_heal(broken, coord, 'terms', Store().meta('terms'), {})
    assert provider.last_error and KEY not in provider.last_error
    assert KEY not in str(excinfo.value)
    assert KEY not in caplog.text


def test_safe_url_drops_userinfo_query_and_fragment():
    assert _errors.safe_url('https://u:p@host.example:8443/v1/?key=1#x') == \
        'https://host.example:8443/v1'


def test_clip_collapses_whitespace_and_cuts():
    assert _errors.clip('a\n\n  b') == 'a b'
    assert len(_errors.clip('y' * 1000)) == _errors.MAX_DETAIL


def test_vendor_error_shapes():
    assert _errors.vendor_error(_anthropic_err('t', 'm')) == ('t', 'm')
    assert _errors.vendor_error({'error': 'plain'}) == (None, 'plain')
    assert _errors.vendor_error({'status': 'error', 'reason': 'auth'}) == (None, 'auth')
    assert _errors.vendor_error([{'error': {'status': 'X', 'message': 'm'}}]) == ('X', 'm')
    assert _errors.vendor_error(json.dumps({})) == (None, '{}')
