"""The payload `mendwright notify` posts, and the one POST that sends it.

- payload() builds the body from a summary (see mendwright.runsummary): {"text": ...} for
  Slack and the services that take Slack's incoming-webhook format, or a JSON document of
  at most 16 KiB for any other receiver. It holds counts, codes, mendwright's own phrases,
  key names (none with detail='counts'), the title, the run and pull request URLs, the test
  outcome and the version. Never page HTML, input values, selectors, reasons, popup text,
  page paths, node ids, provider or model names, or file paths.
- url_problem() says why a webhook URL is refused: it must be https:// with a host, or
  http:// on localhost, 127.0.0.1 or [::1].
- send() makes at most two attempts, one retry after 2 seconds on HTTP 429, a 5xx, a
  timeout or a connection error, follows no redirect, and verifies TLS.
- mask() takes the URL, its path and its query out of a text that is printed.

The webhook URL is a secret (its path is its key): it comes only from the environment, and
nothing here prints it.
"""

import json
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import runsummary
from ._md import _BIDI

# Characters an error body may not bring into a printed line: they can hide or reorder it.
_HIDDEN = _BIDI | {0x2028, 0x2029}

SCHEMA = 1
MAX_JSON = 16 * 1024
ATTEMPTS = 2
RETRY_AFTER = 2.0
DEFAULT_TIMEOUT = 10.0
ERROR_TEXT = 100            # characters of an error body that are shown
_LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')


def _version():
    from . import __version__
    return __version__


def payload(summary, *, fmt='slack', detail='keys', title=None, run_url=None, pr_url=None,
            tests=None, now=None, run=None):
    """The body to POST, as bytes (UTF-8 JSON). fmt is 'slack' or 'json', detail 'keys' or
    'counts' (no key anywhere). now is the time it is sent, an ISO 8601 text."""
    text = runsummary.message(summary, detail=detail, title=title, tests=tests, pr_url=pr_url,
                              run_url=run_url, slack=fmt == 'slack')
    if fmt == 'slack':
        return _dump({'text': text})
    return _json_payload(summary, text, detail=detail, title=title, run_url=run_url,
                         pr_url=pr_url, tests=tests, now=now, run=run)


def _dump(data):
    return json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def _json_payload(summary, text, *, detail, title, run_url, pr_url, tests, now, run):
    r = summary['repaired']
    keys = list(r['keys']) if detail == 'keys' else []
    needs = []
    for entry in summary['needs_human']:
        item = {'code': entry['code'], 'about': entry['about'],
                'text': runsummary.phrase(entry)}
        if detail == 'keys' and entry.get('key') is not None:
            item = dict({'key': runsummary.key_text(entry['key'])}, **item)
        needs.append(item)
    shown_keys = [runsummary.key_text(k) for k in keys]
    data = {
        'schema': SCHEMA, 'mendwright': _version(), 'title': runsummary.title_text(title) or None,
        'run': run, 'run_url': run_url, 'pr_url': pr_url, 'tests': tests,
        'changed': summary['changed'],
        'repaired': {'total': r['total'], 'fingerprint': r['fingerprint'], 'model': r['model'],
                     'frame': r['frame'], 'shadow': r['shadow'], 'confirmed': r['confirmed'],
                     'not_confirmed': r['not_confirmed'], 'quarantined': r['quarantined'],
                     'keys': shown_keys, 'more_keys': 0},
        'handled': dict(summary['handled']),
        'needs_human': needs, 'more_needs_human': 0,
        'not_repaired_on_purpose': dict(summary['not_repaired_on_purpose']),
        'text': text, 'sent_at': now,
    }
    body = _dump(data)
    # Over the limit: the lists lose their last items, a key and an entry in turn.
    while len(body) > MAX_JSON and (data['repaired']['keys'] or data['needs_human']):
        if data['needs_human'] and (len(data['needs_human']) >= len(data['repaired']['keys'])):
            data['needs_human'].pop()
            data['more_needs_human'] += 1
        else:
            data['repaired']['keys'].pop()
            data['repaired']['more_keys'] += 1
        body = _dump(data)
    if len(body) > MAX_JSON:
        data['text'] = runsummary.message(summary, detail=detail, title=title, tests=tests,
                                          pr_url=pr_url, run_url=run_url, limit=1000)
        body = _dump(data)
    return body


# --- the URL ---------------------------------------------------------------------------------

NOT_HTTPS = 'it does not start with https://'


def url_problem(url):
    """'' when url can take the summary, else why not (for the refusal message, which never
    shows the URL): NOT_HTTPS, or what else is wrong with it."""
    if not isinstance(url, str) or not url:
        return 'it is empty'
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return 'it holds a space or a control character'
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname
        if parts.port is not None and not 0 < parts.port < 65536:   # a bad port raises
            return 'its port is not a number from 1 to 65535'
    except ValueError:
        return 'it is not a URL'
    if scheme not in ('https', 'http'):
        return NOT_HTTPS
    if not host:
        return 'it has no host'
    if scheme == 'http' and host not in _LOCAL_HOSTS:
        return NOT_HTTPS
    return ''


def origin(url):
    """scheme://host[:port] of url: how a message names the webhook. Never the path, the
    query or user info."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        port = ':' + str(parts.port) if parts.port else ''
    except ValueError:
        return '(the webhook)'
    return (parts.scheme.lower() + '://' + host + port) if host else '(the webhook)'


def mask(text, url):
    """text with the URL, its path (when longer than 1 character), its query and its user
    info written as ***."""
    text = str(text)
    if not isinstance(url, str) or not url:
        return text
    secrets = [url, url.strip()]
    try:
        parts = urlsplit(url.strip())
        if len(parts.path) > 1:
            secrets.append(parts.path)
        if parts.query:
            secrets.append(parts.query)
        if '@' in parts.netloc:
            secrets.append(parts.netloc.rsplit('@', 1)[0])
    except ValueError:
        pass
    for value in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(value, '***')
    return text


# --- sending ---------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # a 3xx comes back as an HTTPError, which send() refuses


class UrllibHttp:
    """POST with urllib: TLS verified with the default context, HTTPS_PROXY honoured as urllib
    does, no redirect followed. post() returns (status, body bytes), and raises TimeoutError
    for a timeout and ConnectionError when the host cannot be reached."""

    def post(self, url, body, headers, timeout):
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect)
        request = urllib.request.Request(url, data=body, method='POST', headers=headers)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(4096)
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(4096) or b''
            except Exception:
                data = b''
            return exc.code, data
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise TimeoutError('timed out') from None
            raise ConnectionError(_reason(exc.reason)) from None
        except (socket.timeout, TimeoutError):
            raise TimeoutError('timed out') from None


def _reason(reason):
    if isinstance(reason, socket.gaierror):
        return 'its host name could not be resolved'
    if isinstance(reason, ConnectionRefusedError):
        return 'the connection was refused'
    if isinstance(reason, ssl.SSLError):
        return 'TLS failed: ' + type(reason).__name__
    return type(reason).__name__ if not isinstance(reason, str) else reason


@dataclass
class SendResult:
    """What send() did. ok: the webhook answered 2xx. status: the last HTTP status, or None.
    error: None, 'http', 'redirect', 'timeout' or 'connection'. detail: the first characters
    of the error body, or why the connection failed, with control characters removed (not yet
    masked). attempts: how many POSTs were made."""
    ok: bool
    status: int | None = None
    error: str | None = None
    detail: str = ''
    attempts: int = 0


def _clean(data):
    """An error body or reason as one short line: whitespace as one space, control and
    bidirectional characters and lone surrogates removed, at most ERROR_TEXT characters."""
    if isinstance(data, bytes):
        data = data.decode('utf-8', 'replace')
    out = []
    for ch in str(data):
        n = ord(ch)
        if ch.isspace():
            out.append(' ')
        elif not (n < 0x20 or 0x7F <= n <= 0x9F or n in _HIDDEN or 0xD800 <= n <= 0xDFFF):
            out.append(ch)
    return ' '.join(''.join(out).split())[:ERROR_TEXT]


def send(url, body, *, http=None, timeout=DEFAULT_TIMEOUT, sleep=time.sleep):
    """POST body to url, at most ATTEMPTS times. Any 2xx is success; a 3xx is a failure
    (no redirect is followed); HTTP 429, a 5xx, a timeout and a connection error are tried
    once more after RETRY_AFTER seconds. Never raises."""
    http = http or UrllibHttp()
    headers = {'User-Agent': 'mendwright/' + _version(), 'Content-Type': 'application/json'}
    result = SendResult(False)
    for attempt in range(1, ATTEMPTS + 1):
        result.attempts = attempt
        retry = False
        try:
            status, data = http.post(url, body, headers, timeout)
        except TimeoutError:
            result.status, result.error, result.detail = None, 'timeout', 'timed out'
            retry = True
        except (ConnectionError, OSError) as exc:
            result.status, result.error = None, 'connection'
            result.detail = _clean(str(exc) or type(exc).__name__)
            retry = True
        except Exception as exc:        # a broken http= must not fail the caller
            result.status, result.error = None, 'connection'
            result.detail = _clean(type(exc).__name__)
        else:
            result.status = status
            if 200 <= status < 300:
                result.ok, result.error, result.detail = True, None, ''
                return result
            result.error = 'redirect' if 300 <= status < 400 else 'http'
            result.detail = '' if result.error == 'redirect' else _clean(data or b'')
            retry = status == 429 or status >= 500
        if not retry or attempt == ATTEMPTS:
            return result
        sleep(RETRY_AFTER)
    return result
