"""Short, readable reasons for a failed ``propose()`` call.

Every provider keeps ``last_error``: None after a call that returned a proposal, and a
one-line reason after a call that returned None. The coordinator shows that line to a
person, so the text here follows a few rules:

- It never contains an API key, an auth header or a request body. Known secrets are
  replaced with ``[redacted]``, key-shaped tokens and secret query parameters are masked,
  and a URL is shown without its query string or user info.
- A vendor's own error message is kept (it is usually the most useful part) but cut to
  about ``MAX_DETAIL`` characters.
- It says what happened, then gives one next step when there is a useful one.

Nothing here imports a vendor SDK. Exceptions from the SDKs are recognised by shape
(``status_code``, ``body``, ``response``, class name), so this module works whether or
not anthropic, openai, google-genai, botocore or litellm are installed.
"""

import json
import re
import ssl
import socket
import urllib.error
import urllib.parse

MAX_DETAIL = 200

_STATUS_WORDS = {
    400: 'bad request', 401: 'unauthorized', 403: 'forbidden', 404: 'not found',
    405: 'method not allowed', 408: 'request timeout', 409: 'conflict',
    413: 'request too large', 422: 'unprocessable request', 429: 'rate limited',
    500: 'server error', 502: 'bad gateway', 503: 'service unavailable',
    504: 'gateway timeout', 529: 'overloaded',
}

# Vendor "kind" values that say nothing useful on their own.
_GENERIC_KINDS = {'error', 'ok', 'failed', 'failure', 'needs_human'}

DEFAULT_HINTS = {
    'rate': 'Wait a few minutes and retry, or heal less often with '
            'mendwright.open(rate_per_key_per_hour=...).',
    'server': 'This is usually temporary, so retry later.',
    'timeout': "Retry, or give the provider more time with its timeout= argument.",
}

_KEYLIKE = re.compile(
    r'\b(?:sk|rk|pk|gsk|xai)[-_][A-Za-z0-9_\-*.]{6,}'   # OpenAI, Anthropic, Groq, xAI, ...
    r'|\bAIza[0-9A-Za-z_\-]{10,}'                        # Google API keys
    r'|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'                    # AWS access key ids
    r'|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*'  # JWTs
    r'|\b[0-9a-fA-F]{32,}\b')                            # long hex keys (Azure and others)
# An echoed "Bearer <token>". Real tokens are long; the length floor keeps prose such as
# "bearer token is invalid" readable.
_AUTH_VALUE = re.compile(r'(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-*]{16,}')
# An echoed header or setting: "x-api-key: abc...", "api-key=abc...", "Authorization: ...".
_HEADER_SECRET = re.compile(
    r'(?i)\b((?:x-)?(?:api[_-]?key|goog-api-key|auth[_-]?token|access[_-]?token|secret'
    r'|authorization|heal-key)\s*[:=]\s*)(?:"|\')?(?!\[redacted\])[^\s"\',;&<>]{8,}')
_QUERY_SECRET = re.compile(
    r'(?i)([?&](?:key|api[_-]?key|access[_-]?token|token|sig|signature|x-amz-[a-z-]+)=)'
    r'[^&\s"\'<>]+')


def scrub(text, secrets=()):
    """Remove known secrets and anything that looks like a key or token from ``text``."""
    text = str(text)
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4:
            text = text.replace(secret, '[redacted]')
    text = _QUERY_SECRET.sub(r'\1[redacted]', text)
    text = _AUTH_VALUE.sub(r'\1 [redacted]', text)
    text = _HEADER_SECRET.sub(r'\1[redacted]', text)
    return _KEYLIKE.sub('[redacted]', text)


def clip(text, limit=MAX_DETAIL):
    """Collapse whitespace and cut ``text`` to ``limit`` characters."""
    text = ' '.join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit - 3].rstrip() + '...'


def safe_url(url):
    """``url`` as scheme://host[:port]/path, without user info, query or fragment."""
    try:
        parts = urllib.parse.urlsplit(str(url))
    except Exception:
        return 'the configured URL'
    if not parts.scheme or not parts.hostname:
        return str(url).split('?', 1)[0].split('#', 1)[0]
    host = parts.hostname
    if ':' in host:  # IPv6 literal
        host = '[' + host + ']'
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host + (':' + str(port) if port else '')
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip('/'), '', ''))


def sentence(text, hint=None):
    """Join ``text`` and an optional ``hint`` into one or two sentences."""
    text = str(text).rstrip()
    if not text.endswith(('.', '!', '?')):
        text += '.'
    return text + (' ' + hint if hint else '')


def vendor_error(body):
    """Pull (kind, message) out of a vendor error body. Handles the Anthropic, OpenAI,
    Gemini and Ollama shapes, a bare inner error object, a plain-text body and the
    reference gateway's {status, reason}. Either value may be None."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    if isinstance(body, str):
        return None, (body if body.strip() else None)
    if not isinstance(body, dict):
        return None, None
    err = body.get('error')
    if isinstance(err, str):
        return None, err
    node = err if isinstance(err, dict) else body
    kind = None
    for name in ('code', 'type', 'status'):
        value = node.get(name)
        if isinstance(value, str) and value and value.lower() not in _GENERIC_KINDS:
            kind = value
            break
    message = None
    for name in ('message', 'reason', 'detail', 'error_description'):
        value = node.get(name)
        if isinstance(value, str) and value.strip():
            message = value
            break
    return kind, message


def _embedded_json(text):
    """Parse the JSON object embedded in an SDK error string, or None."""
    if not isinstance(text, str):
        return None
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except Exception:
        return None


def _category(status, message):
    lowered = (message or '').lower()
    if status in (401, 403):
        return 'auth'
    if status in (400, 422) and ('api key' in lowered or 'api_key' in lowered):
        return 'auth'
    if status == 404 or (status in (400, 422) and 'model' in lowered):
        return 'model'
    if status in (408, 504):
        return 'timeout'
    if status == 429:
        return 'rate'
    if status >= 500:
        return 'server'
    return None


def _read_body(resp):
    """The parsed JSON body of an HTTP response, or its short plain text, or None."""
    try:
        return resp.json()
    except Exception:
        pass
    text = getattr(resp, 'text', None)
    if callable(text) or not isinstance(text, str):
        text = getattr(resp, '_text', None)
    if isinstance(text, str) and text.strip() and '<' not in text:
        return text  # plain text only; an HTML error page would just be noise
    return None


def _walk(exc, depth=6):
    """``exc`` and the exceptions it wraps (cause, context, URLError.reason)."""
    seen = []
    node = exc
    while isinstance(node, BaseException) and node not in seen and len(seen) < depth:
        seen.append(node)
        reason = getattr(node, 'reason', None)
        if isinstance(reason, BaseException) and reason not in seen:
            node = reason
        else:
            node = node.__cause__ or node.__context__
    return seen


def _network_reason(exc, deep=True):
    """'timed out' / 'connection refused' / ... when ``exc`` is a network failure, else
    None. With ``deep=False`` (an exception that carries an HTTP status) only the class
    name counts, so a 403 whose message says "refused" stays a 403. LiteLLM gives its
    connection and timeout errors a status code, which is why the name is checked at all."""
    if not deep:
        name = type(exc).__name__
        if 'Timeout' in name:
            return 'timed out'
        if 'Connect' not in name:
            return None
    network = False
    for node in _walk(exc):
        name = type(node).__name__
        text = str(node).lower()
        if isinstance(node, TimeoutError) or 'Timeout' in name or 'timed out' in text:
            return 'timed out'
        if isinstance(node, ConnectionRefusedError) or 'refused' in text:
            return 'connection refused'
        if isinstance(node, socket.gaierror) or any(s in text for s in (
                'getaddrinfo', 'name or service not known', 'nodename nor servname',
                'name resolution', 'no address associated')):
            return 'host name not found'
        if isinstance(node, ssl.SSLError) or 'certificate' in text:
            return 'TLS error'
        if isinstance(node, ConnectionResetError):
            return 'connection reset'
        if (isinstance(node, (ConnectionError, urllib.error.URLError))
                and not isinstance(node, urllib.error.HTTPError)) or 'Connect' in name:
            network = True
    return 'network error' if network else None


def _status_of(exc):
    """(status, body) for an HTTP-shaped exception from any SDK, else (None, None)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code, None
    response = getattr(exc, 'response', None)
    if isinstance(response, dict) and isinstance(response.get('Error'), dict):  # botocore
        meta = response.get('ResponseMetadata') or {}
        status = meta.get('HTTPStatusCode')
        error = response['Error']
        body = {'error': {'type': error.get('Code'), 'message': error.get('Message')}}
        return (status if isinstance(status, int) else 0), body
    status = getattr(response, 'status_code', None)  # requests' raise_for_status()
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        return status, _read_body(response)
    for attr in ('status_code', 'status', 'code'):
        status = getattr(exc, attr, None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            break
    else:
        return None, None
    body = getattr(exc, 'body', None)
    if not isinstance(body, (dict, list)):
        status_text = getattr(exc, 'status', None)  # google-genai APIError
        message = getattr(exc, 'message', None)
        if isinstance(status_text, str) or isinstance(message, str):
            embedded = _embedded_json(message)
            if isinstance(embedded, (dict, list)) and any(vendor_error(embedded)):
                body = embedded
            else:
                body = {'error': {'status': status_text if isinstance(status_text, str) else None,
                                  'message': _strip_prefix(message)}}
    return status, body


def _strip_prefix(message):
    """Drop an SDK prefix such as 'litellm.AuthenticationError: ' or 'Error code: 401 - '."""
    if not isinstance(message, str):
        return None
    message = re.sub(r'^\s*[\w.]+(Error|Exception)\s*:\s*', '', message)
    return re.sub(r'^\s*Error code:\s*\d+\s*-\s*', '', message)


_HTML_PAGE = re.compile(r'<\s*(!doctype|html|head|body|title)\b', re.I)


class Describer:
    """Builds ``last_error`` strings for one provider.

    who:     how the service is named in a sentence ('Anthropic', 'the gateway').
    url:     the base URL used in "could not connect to ..." (shown without its query).
    hints:   next steps by failure kind: auth, model, rate, server, timeout, connect,
             refused, reply, body. Missing kinds fall back to DEFAULT_HINTS or nothing.
    secrets: values that must never appear in the text (the provider's API key).
    """

    def __init__(self, who, *, url=None, hints=None, secrets=()):
        self.who = who
        self.url = url
        self.hints = dict(DEFAULT_HINTS)
        self.hints.update({k: v for k, v in (hints or {}).items() if v})
        self.secrets = tuple(s for s in secrets if isinstance(s, str) and s)

    def _clean(self, text):
        return clip(scrub(text, self.secrets))

    def status(self, status, body=None):
        """'<who> returned HTTP <status> (<kind>): <vendor message>. <hint>'"""
        kind, message = vendor_error(body)
        if message and _HTML_PAGE.search(message):
            message = None  # a proxy's HTML error page is noise in a one-line reason
        label = kind or _STATUS_WORDS.get(status) or ('server error' if status >= 500 else None)
        text = self.who + ' returned HTTP ' + str(status)
        if label:
            text += ' (' + clip(scrub(label, self.secrets), 60) + ')'
        if message:
            text += ': ' + self._clean(message)
        return sentence(text, self.hints.get(_category(status, message)))

    def response(self, resp):
        """Describe a non-200 HTTP response object (``status_code`` + ``json()``)."""
        status = getattr(resp, 'status_code', 0)
        if not isinstance(status, int):
            status = 0
        if not status:
            return sentence(self.who + ' returned a response without a status code')
        return self.status(status, _read_body(resp))

    def exception(self, exc):
        """Describe an exception raised while calling the service."""
        status, body = _status_of(exc)
        reason = _network_reason(exc, deep=status is None)
        if reason is not None:
            return self.network(reason)
        if status:
            return self.status(status, body)
        detail = self._clean(_strip_prefix(str(exc)) or '')
        name = type(exc).__name__
        text = 'the request to ' + self.who + ' failed (' + name + ')'
        if detail and detail != name:
            text += ': ' + detail
        return sentence(text)

    def network(self, reason):
        target = safe_url(self.url) if self.url else self.who
        if reason == 'timed out':
            return sentence('the request to ' + target + ' timed out', self.hints.get('timeout'))
        hint = self.hints.get('refused') if reason == 'connection refused' else None
        return sentence('could not connect to ' + target + ' (' + reason + ')',
                        hint or self.hints.get('connect'))

    def prompt(self, exc):
        """build_prompt() itself failed, almost always on a non-JSON inventory."""
        detail = self._clean(type(exc).__name__ + ': ' + str(exc))
        return sentence('could not build the prompt from the capture bundle (' + detail + ')',
                        'Check that inventory_provider returns plain JSON data '
                        '(lists, dicts, strings, numbers).')

    def bad_body(self):
        """A 200 response whose body was not JSON."""
        return sentence(self.who + ' returned a response that was not valid JSON',
                        self.hints.get('body'))

    def no_text(self, detail=None):
        """A well-formed response that carried no reply text."""
        text = self.who + ' returned a reply with no text'
        if detail:
            text += ' (' + self._clean(detail) + ')'
        return sentence(text, self.hints.get('reply'))

    def reply(self, text):
        """Why parse_proposal() found no proposal in the model's reply ``text``."""
        hint = self.hints.get('reply')
        if text is None or not str(text).strip():
            return sentence('the model returned an empty reply', hint)
        match = re.search(r'\{[\s\S]*\}', str(text))
        try:
            obj = json.loads(match.group(0)) if match else None
        except Exception:
            obj = None
        if obj is None:
            return sentence('the model reply was not valid JSON', hint)
        if not isinstance(obj, dict):
            return sentence('the model reply was JSON but not an object', hint)
        if not obj.get('xpath'):
            return sentence('the model reply did not include an xpath', hint)
        return sentence('the model reply could not be used', hint)
