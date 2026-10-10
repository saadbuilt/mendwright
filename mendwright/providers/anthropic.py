"""AnthropicProvider: a direct, in-process HealProvider that calls the Anthropic
Messages API itself, with no mendwright gateway in front. Use it when the client may hold
a provider key; GatewayProvider keeps the key on a server instead.

The `anthropic` SDK is optional and imported lazily inside the call path. Without it the
provider falls back to a stdlib urllib POST, so `import mendwright` never needs the SDK.
The prompt comes from build_prompt()/parse_proposal() in mendwright.prompt, shared with
the gateway and every other provider. The provider only proposes {xpath, fallbacks,
reasoning} or None; it never applies anything.

The endpoint is base_url=, else the ANTHROPIC_BASE_URL environment variable (as the
`anthropic` SDK reads it), else https://api.anthropic.com. The SDK and the urllib
fallback always use the same one.

After each propose() call, ``last_error`` is None on success or a short reason, such as
"Anthropic returned HTTP 401 (authentication_error): invalid x-api-key", when it
returned None.
"""

import os

from ..client import _default_http
from ..prompt import build_prompt, parse_proposal
from ._errors import Describer

DEFAULT_BASE_URL = 'https://api.anthropic.com'
ANTHROPIC_VERSION = '2023-06-01'
DEFAULT_MAX_TOKENS = 1024

_HINTS = {
    'auth': 'Check ANTHROPIC_API_KEY, or pass api_key= to AnthropicProvider.',
    'model': 'Check the model id in ANTHROPIC_MODEL, or pass model= to AnthropicProvider.',
    'connect': 'Check your network connection, and base_url= if you use a proxy.',
    'reply': 'Try a stronger model (ANTHROPIC_MODEL or model=).',
}


class AnthropicProvider:
    def __init__(self, model=None, api_key=None, *, http=None, base_url=None,
                 version=ANTHROPIC_VERSION, max_tokens=DEFAULT_MAX_TOKENS, timeout=30):
        # The model id is never baked into the code: it comes from the caller or the env,
        # and a clear error names the env var when neither is set.
        self.model = model or os.environ.get('ANTHROPIC_MODEL')
        if not self.model:
            raise ValueError(
                'AnthropicProvider requires a model: pass model=... or set ANTHROPIC_MODEL')
        self.api_key = api_key or os.environ.get('ANTHROPIC_API_KEY')
        self.http = http  # injected transport (tests) wins over SDK + urllib
        # base_url=, else ANTHROPIC_BASE_URL (what the anthropic SDK reads), else the API.
        self.base_url = (base_url or os.environ.get('ANTHROPIC_BASE_URL')
                         or DEFAULT_BASE_URL).rstrip('/')
        self.version = version
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.last_error = None

    def _errors(self):
        return Describer('Anthropic', url=self.base_url, hints=_HINTS, secrets=(self.api_key,))

    def propose(self, bundle):
        # Never raises: the coordinator calls this without a guard. Every failure returns
        # None and leaves a readable reason in self.last_error.
        self.last_error = None
        try:
            prompt = build_prompt(bundle)
        except Exception as exc:
            self.last_error = self._errors().prompt(exc)
            return None
        try:
            text = self._complete(prompt['system'], prompt['user'])
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        if text is None:
            if self.last_error is None:
                self.last_error = self._errors().no_text()
            return None
        proposal = parse_proposal(text)
        if proposal is None:
            self.last_error = self._errors().reply(text)
        return proposal

    def report_outcome(self, key, xpath, success):
        # No-op: direct mode journals the canary elsewhere (the coordinator/store);
        # there is no gateway outcome endpoint to notify.
        pass

    # -- transport -----------------------------------------------------------------

    def _complete(self, system, user):
        """Return the model's reply text, or None (with last_error set) on failure."""
        body = {
            'model': self.model,
            'max_tokens': self.max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': user}],
        }
        # An explicitly injected transport (tests, or a custom HTTP client) takes
        # precedence over everything, keeping tests fully offline and deterministic.
        if self.http is not None:
            return self._via_http(body, self.http)
        sdk = self._load_sdk()
        if sdk is not None:
            return self._via_sdk(sdk, body)
        return self._via_http(body, _default_http)  # raw-urllib stdlib fallback

    @staticmethod
    def _load_sdk():
        """Lazy, optional import, never at module load, so `import mendwright` is SDK-free."""
        try:
            import anthropic  # noqa: PLC0415 (lazy on purpose)
            return anthropic
        except Exception:
            return None

    def _via_http(self, body, http):
        headers = {'anthropic-version': self.version}
        if self.api_key:
            headers['x-api-key'] = self.api_key
        try:
            resp = http.post(self.base_url + '/v1/messages', json=body,
                             headers=headers, timeout=self.timeout)
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        if getattr(resp, 'status_code', 0) != 200:
            self.last_error = self._errors().response(resp)
            return None
        try:
            data = resp.json()
        except Exception:
            self.last_error = self._errors().bad_body()
            return None
        if not isinstance(data, dict):
            self.last_error = self._errors().bad_body()
            return None
        text = _text_from_blocks(data.get('content'))
        if text is None:
            self.last_error = self._errors().no_text(_stop_detail(data.get('stop_reason')))
        return text

    def _via_sdk(self, sdk, body):
        try:
            # base_url is always passed, so the SDK and the urllib path send the key and the
            # page to the same host.
            client = sdk.Anthropic(api_key=self.api_key, base_url=self.base_url)
            message = client.messages.create(
                model=body['model'], max_tokens=body['max_tokens'],
                system=body['system'], messages=body['messages'])
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        # SDK content blocks are objects (block.type == 'text', block.text); tolerate dicts.
        text = _text_from_blocks(getattr(message, 'content', None))
        if text is None:
            self.last_error = self._errors().no_text(
                _stop_detail(getattr(message, 'stop_reason', None)))
        return text


def _stop_detail(stop_reason):
    return ('stop reason: ' + stop_reason) if isinstance(stop_reason, str) and stop_reason else None


def _text_from_blocks(blocks):
    """Join the text of every 'text' content block (Anthropic Messages shape), object or
    dict. Returns None when there is nothing usable so propose() yields None, not ''."""
    if not blocks or not isinstance(blocks, (list, tuple)):
        return None
    out = []
    for block in blocks:
        if isinstance(block, dict):
            if block.get('type') == 'text':
                out.append(block.get('text') or '')
        elif getattr(block, 'type', None) == 'text':
            out.append(getattr(block, 'text', '') or '')
    return ''.join(out) or None
