"""GeminiProvider: a direct, in-process HealProvider backed by Google's Gemini
``generateContent`` REST endpoint. There is no gateway in front, so this provider holds the
API key itself. Use it for trusted runs (CI, a developer's own machine), not a distributed
client; GatewayProvider (key kept server-side) is for that.

The optional ``google-genai`` SDK is imported lazily inside a helper, with a stdlib urllib
POST as the fallback, so ``import mendwright`` never needs it. The request and response go
through the shared build_prompt()/parse_proposal(), so Gemini heals the same way as the
gateway and the other providers. Like every provider it only proposes {xpath, fallbacks,
reasoning} or None; the coordinator re-validates and applies.

After each propose() call, ``last_error`` is None on success or a short reason when it
returned None, for example "Gemini returned HTTP 400 (INVALID_ARGUMENT): API key not
valid...". The key travels in the URL query, so every reason is built without it.
"""

import os
import urllib.parse

from ..client import _default_http
from ..prompt import build_prompt, parse_proposal
from ._errors import Describer

_API_ROOT = 'https://generativelanguage.googleapis.com/v1beta'

_HINTS = {
    'auth': 'Check GEMINI_API_KEY, or pass api_key= to GeminiProvider.',
    'model': 'Check the model id in GEMINI_MODEL, or pass model= to GeminiProvider.',
    'connect': 'Check your network connection.',
    'reply': 'Try a stronger model (GEMINI_MODEL or model=).',
}


def _load_sdk():
    """Import the optional google-genai SDK lazily; return the module or None. Kept at
    module level so tests can monkeypatch it and so ``import mendwright`` works without it."""
    try:
        from google import genai
        return genai
    except Exception:
        return None


def _text_from_response(data):
    """Pull the concatenated text out of a generateContent response, or None. A blocked
    prompt (promptFeedback, no candidates), a missing content/parts, or parts that are all
    'thought' with no text all yield None."""
    try:
        candidates = data.get('candidates') or []
        parts = candidates[0].get('content', {}).get('parts') or []
    except Exception:
        return None
    texts = [p.get('text', '') for p in parts
             if isinstance(p, dict) and not p.get('thought') and p.get('text')]
    return ''.join(texts) if texts else None


def _why_no_text(data):
    """A short detail for a response with no usable text: a block reason or finish reason."""
    if not isinstance(data, dict):
        return None
    feedback = data.get('promptFeedback')
    if isinstance(feedback, dict) and isinstance(feedback.get('blockReason'), str):
        return 'prompt blocked: ' + feedback['blockReason']
    candidates = data.get('candidates')
    if not candidates:
        return 'no candidates'
    first = candidates[0] if isinstance(candidates, list) else None
    if isinstance(first, dict) and isinstance(first.get('finishReason'), str):
        return 'finish reason: ' + first['finishReason']
    return None


class GeminiProvider:
    def __init__(self, api_key=None, model=None, *, http=None, timeout=30):
        self.api_key = api_key or os.environ.get('GEMINI_API_KEY')
        if not self.api_key:
            raise ValueError(
                'GeminiProvider requires an API key: pass api_key=... or set GEMINI_API_KEY.')
        model = model or os.environ.get('GEMINI_MODEL')
        if not model:
            raise ValueError(
                'GeminiProvider requires a model: pass model=... or set GEMINI_MODEL '
                '(mendwright never hardcodes a model id).')
        # A caller may pass the fully-qualified 'models/<id>' form; strip it so the URL
        # can't become '.../models/models/<id>:generateContent'.
        self.model = model[len('models/'):] if model.startswith('models/') else model
        self.http = http
        self.timeout = timeout
        self._sdk_client = None
        self.last_error = None

    def _errors(self):
        # url is the API root on purpose: the full request URL carries ?key=.
        return Describer('Gemini', url=_API_ROOT, hints=_HINTS,
                         secrets=(self.api_key, urllib.parse.quote(self.api_key, safe='')))

    def propose(self, bundle):
        # Must never raise: coordinator.try_heal() calls this without a try/except, so any
        # exception here would crash the user's automation step. Every failure => None,
        # with the reason in self.last_error.
        self.last_error = None
        try:
            prompt = build_prompt(bundle)
        except Exception as exc:
            self.last_error = self._errors().prompt(exc)
            return None
        try:
            if self.http is not None:
                text = self._text_via_rest(self.http, prompt)
            else:
                genai = _load_sdk()
                if genai is not None:
                    text = self._text_via_sdk(genai, prompt)
                else:
                    text = self._text_via_rest(_default_http, prompt)
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
        # Gemini's generateContent has no outcome endpoint; the coordinator still runs the
        # canary and quarantines a bad healed candidate locally. A provider only proposes.
        return None

    def _text_via_rest(self, http, prompt):
        url = (_API_ROOT + '/models/' + self.model + ':generateContent?key='
               + urllib.parse.quote(self.api_key, safe=''))
        body = {
            'systemInstruction': {'parts': [{'text': prompt['system']}]},
            'contents': [{'role': 'user', 'parts': [{'text': prompt['user']}]}],
            'generationConfig': {'responseMimeType': 'application/json'},
        }
        resp = http.post(url, json=body, timeout=self.timeout)
        if getattr(resp, 'status_code', 0) != 200:
            self.last_error = self._errors().response(resp)
            return None
        try:
            data = resp.json()
        except Exception:
            self.last_error = self._errors().bad_body()
            return None
        text = _text_from_response(data) if isinstance(data, dict) else None
        if text is None:
            self.last_error = self._errors().no_text(_why_no_text(data))
        return text

    def _text_via_sdk(self, genai, prompt):
        if self._sdk_client is None:
            self._sdk_client = genai.Client(api_key=self.api_key)
        resp = self._sdk_client.models.generate_content(
            model=self.model,
            contents=prompt['user'],
            config={
                'system_instruction': prompt['system'],
                'response_mime_type': 'application/json',
            },
        )
        text = getattr(resp, 'text', None)
        if not text:
            self.last_error = self._errors().no_text()
            return None
        return text
