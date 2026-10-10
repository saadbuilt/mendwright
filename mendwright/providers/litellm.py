"""LiteLLMProvider: one adapter for the 100+ models LiteLLM can route to. It wraps
``litellm.completion`` so any model LiteLLM supports (OpenAI, Anthropic, Gemini, Bedrock,
Azure, Ollama, ...) becomes a direct in-process heal provider via one model string.

Like every direct provider it only proposes: it builds the request with the shared
``build_prompt`` and parses the reply with ``parse_proposal``; the coordinator re-validates
every proposal through the local gate before anything is applied.

The ``litellm`` SDK is imported lazily inside ``propose``, so ``import mendwright`` (and
importing this module) never requires it. With no SDK installed and ``api_base`` set, the
provider falls back to a stdlib urllib POST to an OpenAI-compatible ``/chat/completions``
endpoint. The model is never hardcoded: it comes from the constructor arg or the
``LITELLM_MODEL`` env var, and construction raises if neither is set.

After each propose() call, ``last_error`` is None on success or a short reason when it
returned None. LiteLLM's exceptions carry a status code and the vendor's error text, so
the reason reads like "LiteLLM returned HTTP 401 (authentication_error): invalid x-api-key".
"""

import os

from ..client import _default_http
from ..prompt import build_prompt, parse_proposal
from ._errors import Describer, sentence

_HINTS = {
    'auth': "Check the key LiteLLM uses for this model (LITELLM_API_KEY, api_key=, or the "
            "vendor's own env var such as ANTHROPIC_API_KEY).",
    'model': 'Check the model string in LITELLM_MODEL, or pass model= to LiteLLMProvider.',
    'connect': 'Check LITELLM_API_BASE (or api_base=) and that the server is running.',
    'reply': 'Try a stronger model (LITELLM_MODEL or model=).',
}


def _message_content(resp):
    """Pull the assistant text out of a chat completion, tolerating both the SDK/object
    shape (``resp.choices[0].message.content``) and the plain-dict shape returned by the
    urllib fallback. Returns the text, or None (incl. when content is a list of parts with
    no text)."""
    try:
        choices = resp['choices'] if isinstance(resp, dict) else resp.choices
        first = choices[0]
        message = first['message'] if isinstance(first, dict) else first.message
        content = message['content'] if isinstance(message, dict) else message.content
    except (AttributeError, KeyError, IndexError, TypeError):
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal/content-parts replies
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get('text')
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(part, 'text', None)
                if isinstance(text, str):
                    parts.append(text)
        return ''.join(parts) or None
    return None


class LiteLLMProvider:
    """Heal provider backed by ``litellm.completion``.

    model: a LiteLLM model string (constructor arg or the LITELLM_MODEL env var; required).
    api_base / api_key: optional, from args or the LITELLM_API_BASE / LITELLM_API_KEY env
    vars (mendwright's own vars). They are forwarded to litellm, and api_base also drives the
    no-SDK urllib fallback.
    completion: inject the completion callable (tests, or a custom router) instead of
    importing litellm.
    http: inject an HTTP client ({.post(url, json=, headers=, timeout=)}) for the fallback.
    Extra keyword args are passed straight through to litellm.completion / the request body.
    """

    def __init__(self, model=None, *, api_base=None, api_key=None, completion=None,
                 http=None, timeout=30, **completion_kwargs):
        model = model or os.environ.get('LITELLM_MODEL')
        if not model:
            raise ValueError(
                'LiteLLMProvider requires a model: pass model=... or set the LITELLM_MODEL '
                'environment variable (e.g. a LiteLLM model string). No model ID is baked in.')
        self.model = model
        self.api_base = api_base or os.environ.get('LITELLM_API_BASE')
        self.api_key = api_key or os.environ.get('LITELLM_API_KEY')
        self.completion = completion
        self.http = http
        self.timeout = timeout
        self.completion_kwargs = completion_kwargs
        self.last_error = None

    def _errors(self):
        return Describer('LiteLLM', url=self.api_base, hints=_HINTS, secrets=(self.api_key,))

    def propose(self, bundle):
        # propose() must never raise: the coordinator calls it without a guard, so any
        # transport/auth/parse failure degrades to None (no heal) instead of crashing the
        # caller's automation, with the reason in self.last_error.
        self.last_error = None
        try:
            prompt = build_prompt(bundle)
        except Exception as exc:
            self.last_error = self._errors().prompt(exc)
            return None
        messages = [
            {'role': 'system', 'content': prompt['system']},
            {'role': 'user', 'content': prompt['user']},
        ]
        try:
            completion = self.completion
            if completion is None:
                try:
                    import litellm  # lazy: resolves to the top-level SDK, not this module
                    completion = litellm.completion
                except ImportError:
                    completion = None
            if completion is not None:
                kwargs = {'model': self.model, 'messages': messages}
                if self.api_base:
                    kwargs['api_base'] = self.api_base
                if self.api_key:
                    kwargs['api_key'] = self.api_key
                if self.timeout:
                    kwargs['timeout'] = self.timeout
                kwargs.update(self.completion_kwargs)
                text = _message_content(completion(**kwargs))
                if text is None:
                    self.last_error = self._errors().no_text()
            elif self.api_base:  # no SDK, no injected callable: stdlib fallback
                text = self._http_text(messages)
            else:
                self.last_error = sentence(
                    'litellm is not installed and no api_base is set, so there is nothing '
                    'to send the request to',
                    'Install it with: pip install "mendwright[litellm]", or set '
                    'LITELLM_API_BASE (or api_base=) to an OpenAI-compatible endpoint.')
                return None
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        if text is None:
            return None
        proposal = parse_proposal(text)
        if proposal is None:
            self.last_error = self._errors().reply(text)
        return proposal

    def _http_text(self, messages):
        http = self.http or _default_http
        headers = {}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        payload = {'model': self.model, 'messages': messages}
        payload.update(self.completion_kwargs)
        resp = http.post(self.api_base.rstrip('/') + '/chat/completions',
                         json=payload, headers=headers, timeout=self.timeout)
        if getattr(resp, 'status_code', 0) != 200:
            self.last_error = self._errors().response(resp)
            return None
        try:
            data = resp.json()
        except Exception:
            self.last_error = self._errors().bad_body()
            return None
        text = _message_content(data)
        if text is None:
            self.last_error = self._errors().no_text()
        return text

    def report_outcome(self, key, xpath, success):
        # No canary endpoint for a direct model call; outcome is a no-op (the store still
        # quarantines a failed candidate locally).
        pass
