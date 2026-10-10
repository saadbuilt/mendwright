"""OpenAICompatibleProvider: one direct, in-process adapter for every service that speaks
the OpenAI /chat/completions wire format. ``base_url`` selects the service, so the same
code covers OpenAI (the default), Azure OpenAI, Ollama, Groq, OpenRouter, vLLM and LM Studio.

It builds its request with the shared ``build_prompt`` and parses the reply with
``parse_proposal`` (prompt.py is the single source of truth), so its proposals match the
gateway's and every other provider's. Like all providers it only proposes {xpath,
fallbacks, reasoning} or None and never applies anything.

No vendor SDK is required. The official ``openai`` package is imported lazily inside a
method; without it the provider falls back to a stdlib urllib POST, so ``import mendwright``
always works. A transport can be injected (``http=``) for offline tests.

The model id is never hardcoded: it comes from the ``model`` arg or the ``OPENAI_MODEL``
environment variable, and construction raises if neither is set. The endpoint is
``base_url``, else the ``OPENAI_BASE_URL`` environment variable (as the ``openai`` SDK reads
it), else https://api.openai.com/v1, for the SDK and the urllib fallback alike.

After each propose() call, ``last_error`` is None on success or a short reason when it
returned None, for example "could not connect to http://localhost:11434/v1 (connection
refused). Is Ollama running? ..." or "OpenAI returned HTTP 404 (model_not_found): ...".
"""

import os
import urllib.parse

from ..prompt import build_prompt, parse_proposal
from ._errors import Describer

DEFAULT_BASE_URL = 'https://api.openai.com/v1'
OLLAMA_BASE_URL = 'http://localhost:11434/v1'


class OpenAICompatibleProvider:
    def __init__(self, *, base_url=None, api_key=None, model=None, api_version=None,
                 auth_style='bearer', response_format_json=True, http=None, timeout=30,
                 extra_headers=None, use_sdk=True):
        # base_url=, else OPENAI_BASE_URL (what the openai SDK reads), else OpenAI itself.
        # for_ollama() and for_azure() always pass base_url.
        self.base_url = (base_url or os.environ.get('OPENAI_BASE_URL')
                         or DEFAULT_BASE_URL).rstrip('/')
        # Key is optional (keyless local backends like Ollama need none); model is not.
        self.api_key = api_key if api_key is not None else os.environ.get('OPENAI_API_KEY')
        self.model = model or os.environ.get('OPENAI_MODEL')
        if not self.model:
            raise ValueError(
                'OpenAICompatibleProvider: no model configured. Pass model=... or set the '
                "OPENAI_MODEL environment variable (use a current id from your provider's model list).")
        # 'bearer' -> Authorization: Bearer <key> (OpenAI/Groq/OpenRouter/vLLM/LM Studio/Ollama).
        # 'api-key' -> api-key: <key> header (Azure OpenAI).
        self.auth_style = auth_style
        self.api_version = api_version  # Azure: appended as the ?api-version= query param
        self.response_format_json = response_format_json
        self.http = http  # injectable transport; wins over the SDK so tests stay offline
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})
        self.use_sdk = use_sdk
        self.last_error = None
        self._ollama = False  # set by for_ollama(); only changes how errors are worded

    # --- factories -------------------------------------------------------------------
    @classmethod
    def for_ollama(cls, model, *, base_url=OLLAMA_BASE_URL, api_key=None, **kwargs):
        """Local, keyless Ollama (http://localhost:11434/v1). model is required by Ollama.

        Pass '' (not None) by default so the __init__ fallback never inherits OPENAI_API_KEY:
        a local keyless call must not ship your OpenAI key to localhost.
        """
        inst = cls(base_url=base_url, model=model,
                   api_key=(api_key if api_key is not None else ''),
                   auth_style='bearer', **kwargs)
        inst._ollama = True
        return inst

    @classmethod
    def for_azure(cls, endpoint, deployment, api_version, key=None, model=None, **kwargs):
        """Azure OpenAI: deployment lives in the URL path, the version is a query param, and
        the key goes in an ``api-key`` header. The body ``model`` is set to the deployment."""
        base_url = endpoint.rstrip('/') + '/openai/deployments/' + deployment
        return cls(base_url=base_url, model=model or deployment, api_key=key,
                   api_version=api_version, auth_style='api-key', **kwargs)

    # --- HealProvider ----------------------------------------------------------------
    def propose(self, bundle):
        # Never raises: the coordinator calls this without a guard. Every failure returns
        # None and leaves a readable reason in self.last_error.
        self.last_error = None
        try:
            prompt = build_prompt(bundle)
        except Exception as exc:
            self.last_error = self._errors().prompt(exc)
            return None
        messages = [{'role': 'system', 'content': prompt['system']},
                    {'role': 'user', 'content': prompt['user']}]
        try:
            content = self._complete(messages)
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        if not isinstance(content, str) or not content:
            if self.last_error is None:
                self.last_error = self._errors().no_text()
            return None
        proposal = parse_proposal(content)
        if proposal is None:
            self.last_error = self._errors().reply(content)
        return proposal

    def report_outcome(self, key, xpath, success):
        """A direct provider has no server-side journal to post the canary to; the outcome is
        recorded locally by the coordinator/store. No-op by design (and never networked)."""
        return None

    # --- error wording ---------------------------------------------------------------
    def _service(self):
        """How the backend is named in an error: OpenAI, Ollama, Azure OpenAI or its host."""
        host = (urllib.parse.urlsplit(self.base_url).hostname or '').lower()
        if self._ollama or ':11434' in self.base_url:
            return 'Ollama'
        if self.auth_style == 'api-key' or host.endswith('.openai.azure.com'):
            return 'Azure OpenAI'
        if host == 'api.openai.com':
            return 'OpenAI'
        return host or 'the server'

    def _errors(self):
        service = self._service()
        if service == 'Ollama':
            hints = {
                'auth': 'Check the api_key you passed (Ollama itself needs none).',
                'model': "Check OLLAMA_MODEL (or model=), and run 'ollama pull <model>' "
                         'if it is not downloaded yet.',
                'refused': "Is Ollama running? Start it with 'ollama serve', or set "
                           'OLLAMA_HOST (or base_url=) to where it listens.',
                'connect': 'Check OLLAMA_HOST (or base_url=) and that Ollama is running.',
                'reply': 'Try a larger model (OLLAMA_MODEL or model=).',
            }
        elif service == 'Azure OpenAI':
            hints = {
                'auth': 'Check the key passed to OpenAICompatibleProvider.for_azure(key=...).',
                'model': 'Check the deployment name and api_version passed to for_azure().',
                'connect': 'Check the endpoint passed to for_azure().',
                'reply': 'Try a stronger model deployment.',
            }
        elif service == 'OpenAI':
            hints = {
                'auth': 'Check OPENAI_API_KEY, or pass api_key=.',
                'model': 'Check the model id in OPENAI_MODEL, or pass model=.',
                'connect': 'Check your network connection, and base_url= if you use a proxy.',
                'reply': 'Try a stronger model (OPENAI_MODEL or model=).',
            }
        else:
            hints = {
                'auth': 'Check the api_key you passed for ' + service + '.',
                'model': 'Check the model id (OPENAI_MODEL or model=) and base_url.',
                'connect': 'Check base_url and that the server is running.',
                'reply': 'Try a stronger model (OPENAI_MODEL or model=).',
            }
        return Describer(service, url=self.base_url, hints=hints, secrets=(self.api_key,))

    # --- transport -------------------------------------------------------------------
    def _complete(self, messages):
        """Return the raw model text (choices[0].message.content), or None with
        last_error set.

        Order: an injected transport wins (keeps tests offline and deterministic); else the
        lazily-imported ``openai`` SDK; else a stdlib-urllib POST."""
        if self.http is not None:
            return self._http_complete(self.http, messages)
        sdk = self._try_import_sdk() if self.use_sdk else None
        if sdk is not None:
            return self._sdk_complete(sdk, messages)
        from ..client import _UrllibHttp  # stdlib-only fallback; imported lazily
        return self._http_complete(_UrllibHttp(), messages)

    @staticmethod
    def _try_import_sdk():
        try:
            import openai  # lazy: not a hard dependency
            return openai
        except Exception:
            return None

    def _chat_url(self):
        url = self.base_url + '/chat/completions'
        if self.api_version:  # Azure
            url += '?' + urllib.parse.urlencode({'api-version': self.api_version})
        return url

    def _auth_headers(self):
        headers = {'content-type': 'application/json'}
        headers.update(self.extra_headers)
        if self.api_key:  # omitted entirely when keyless (e.g. Ollama)
            if self.auth_style == 'api-key':
                headers['api-key'] = self.api_key
            else:
                headers['Authorization'] = 'Bearer ' + self.api_key
        return headers

    def _body(self, messages):
        body = {'model': self.model, 'messages': messages}
        if self.response_format_json:
            body['response_format'] = {'type': 'json_object'}
        return body

    def _http_complete(self, http, messages):
        try:
            resp = http.post(self._chat_url(), json=self._body(messages),
                             headers=self._auth_headers(), timeout=self.timeout)
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
        try:
            return data['choices'][0]['message']['content']
        except Exception:
            self.last_error = self._errors().no_text()
            return None

    def _sdk_complete(self, openai_mod, messages):
        client_kwargs = {'base_url': self.base_url,
                         'api_key': self.api_key or 'no-key',  # SDK requires a non-empty key
                         'timeout': self.timeout}
        headers, query = {}, {}
        if self.api_key and self.auth_style == 'api-key':
            headers['api-key'] = self.api_key
        if self.extra_headers:
            headers.update(self.extra_headers)
        if self.api_version:
            query['api-version'] = self.api_version
        if headers:
            client_kwargs['default_headers'] = headers
        if query:
            client_kwargs['default_query'] = query
        try:
            client = openai_mod.OpenAI(**client_kwargs)
            create_kwargs = {'model': self.model, 'messages': messages}
            if self.response_format_json:
                create_kwargs['response_format'] = {'type': 'json_object'}
            resp = client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            self.last_error = self._errors().exception(exc)
            return None
        try:
            return resp.choices[0].message.content
        except Exception:
            self.last_error = self._errors().no_text()
            return None
