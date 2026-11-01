"""HttpBackend: a store over HTTP(S) with ETags, using only the standard library.

The store is a folder URL, and each object is at <folder URL><name>:

- get(): GET, with If-None-Match: <version> when if_none_match= is given. 200 is the body
  and its ETag, 304 "not modified", 404 "missing".
- put(): PUT, with If-Match: <version> for if_match=, or If-None-Match: * for
  create_only=True. 2xx carries the new ETag, 412 (or 409) is a Conflict, and so is 404 for
  an If-Match on an object that is gone.
- delete(): DELETE, with If-Match: <version> for if_match=. 2xx or 404 (already gone) is
  done. 412 (or 409) is a Conflict, unless a read then finds the object gone, because a
  server that follows RFC 9110 answers 412 for an If-Match on a missing object.

Versions are the server's ETags, which must be strong: a weak one (W/"...") or none at all
is an error, since If-Match compares strong tags only. mendwright.backends.server is the
reference server.

The token is sent as "Authorization: Bearer <token>". It comes from MENDWRIGHT_STORE_TOKEN,
or from the file MENDWRIGHT_STORE_TOKEN_FILE names (set one of them), never from the code or
the URL. Without either, no Authorization header is sent.

The URL must be https://, or http:// to localhost, 127.0.0.1 or [::1]. TLS is verified with
the default context (SSL_CERT_FILE adds a CA). No redirect is followed, so the token goes to
no other host, and HTTPS_PROXY is honoured as urllib honours it, except for a loopback URL,
which never goes through a proxy. timeout= holds for the connection and for each read of an
answer. A request is never retried: see the contract in mendwright.backends.

Errors: 401 and 403 raise Denied; a timeout, a refused or lost connection, a TLS failure, an
answer that is not HTTP, 408, 429 and 5xx raise Unavailable; any other answer (a redirect,
400, 413, ...) raises BackendError. No message holds the token or an answer's body.
"""

from __future__ import annotations

import http.client
import logging
import socket
import ssl
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .. import __version__
from . import BackendError, Conflict, Denied, Read, Unavailable
from ._common import (DEFAULT_TIMEOUT, MAX_BODY, TOKEN_ENV, TOKEN_FILE_ENV, check_name,
                      check_put, check_version, is_loopback, refuse_secrets, token_from_env)

__all__ = ['HttpBackend']

logger = logging.getLogger(__name__)

USER_AGENT = 'mendwright-store/' + __version__
_CHUNK = 64 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None     # a 3xx comes back as an HTTPError, which is refused


def folder_url(url) -> tuple[str, bool]:
    """(the normalized folder URL, whether its host is a loopback address). Raises
    ValueError for a URL HttpBackend cannot use, without quoting the URL."""
    if not isinstance(url, str):
        raise TypeError('HttpBackend takes the store URL as a str, not ' + type(url).__name__)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError('the store URL is not a valid URL (check its host and port)') from None
    refuse_secrets(url, parts)
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in ('https', 'http') or not host:
        raise ValueError('an HTTP store URL is https://host/path/ (or http:// to localhost)')
    loopback = is_loopback(host)
    if scheme == 'http' and not loopback:
        raise ValueError(
            'an HTTP store must use https://, which keeps its token secret on the way; plain '
            'http:// is allowed to localhost, 127.0.0.1 and [::1] only')
    netloc = ('[' + host + ']' if ':' in host else host) + ('' if port is None else ':%d' % port)
    path = parts.path or '/'
    if not path.endswith('/'):
        path += '/'
    return scheme + '://' + netloc + path, loopback


class HttpBackend:
    """A store at a folder URL, over HTTP(S). See the module docstring for the protocol."""

    def __init__(self, url, *, timeout=DEFAULT_TIMEOUT, max_body=MAX_BODY):
        self._base, loopback = folder_url(url)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not timeout > 0:
            raise ValueError('timeout= takes a number of seconds above 0')
        if not isinstance(max_body, int) or isinstance(max_body, bool) or max_body < 1:
            raise ValueError('max_body= takes a number of bytes above 0')
        self._timeout = float(timeout)
        self._max_body = max_body
        self._token = token_from_env()
        handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context()), _NoRedirect()]
        if loopback:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def describe(self):
        return self._base

    def __repr__(self):
        return 'HttpBackend(' + repr(self._base) + ')'

    # --- the contract ---------------------------------------------------------------------

    def get(self, name, *, if_none_match=None):
        check_name(name)
        check_version(if_none_match, 'if_none_match')
        headers = {} if if_none_match is None else {'If-None-Match': if_none_match}
        status, answer, body = self._send('GET', name, headers=headers)
        if status == 304 and if_none_match is not None:
            return Read(None, _strong(answer.get('ETag')) or if_none_match, True)
        if status == 404:
            return Read(None, None, False)
        if 200 <= status < 300:
            version = self._version(name, answer)
            if version == if_none_match:     # a server that ignored If-None-Match
                return Read(None, version, True)
            return Read(body, version, False)
        raise self._refused(status, 'GET', name)

    def put(self, name, body, *, if_match=None, create_only=False):
        body = check_put(body, if_match, create_only)
        check_name(name)
        headers = {}
        if if_match is not None:
            headers['If-Match'] = if_match
        if create_only:
            headers['If-None-Match'] = '*'
        status, answer, _ = self._send('PUT', name, body=body, headers=headers)
        if 200 <= status < 300:
            return self._version(name, answer)
        if status in (409, 412) or (status == 404 and if_match is not None):
            raise self._conflict(name, if_match, create_only, status)
        raise self._refused(status, 'PUT', name)

    def delete(self, name, *, if_match=None):
        check_name(name)
        check_version(if_match, 'if_match')
        headers = {} if if_match is None else {'If-Match': if_match}
        status, _, _ = self._send('DELETE', name, headers=headers)
        if 200 <= status < 300 or status == 404:
            return None
        if status in (409, 412):
            # RFC 9110 answers 412 for If-Match on a missing object, and gone is not an error.
            # One read tells the two apart; it is not a retried write.
            if self.get(name).version is None:
                return None
            raise self._conflict(name, if_match, False, status)
        raise self._refused(status, 'DELETE', name)

    # --- one request ----------------------------------------------------------------------

    def _at(self, name):
        return name + ' at ' + self._base

    def _send(self, method, name, *, body=None, headers=None):
        """(status, headers, body) for any HTTP answer. A failure to get one raises Unavailable;
        an error answer's body is never read."""
        request = urllib.request.Request(self._base + quote(name, safe='/'), data=body, method=method)
        request.add_header('User-Agent', USER_AGENT)
        request.add_header('Accept-Encoding', 'identity')
        if body is not None:
            request.add_header('Content-Type', 'application/octet-stream')
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        if self._token:
            request.add_unredirected_header('Authorization', 'Bearer ' + self._token)
        started = time.monotonic()
        try:
            response = self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            status, answer, data = exc.code, exc.headers, b''
            exc.close()
        except urllib.error.URLError as exc:
            raise self._unreachable(name, exc.reason) from None
        except (OSError, http.client.HTTPException) as exc:
            raise self._unreachable(name, exc) from None
        else:
            with response:
                status, answer = response.status, response.headers
                data = self._read(name, response) if method == 'GET' else b''
        logger.debug('%s %s%s: HTTP %s in %.0f ms', method, self._base, name, status,
                     (time.monotonic() - started) * 1000)
        return status, answer, data

    def _read(self, name, response):
        length = (response.headers.get('Content-Length') or '').strip()
        try:
            if length.isdigit():
                if int(length) > self._max_body:
                    raise self._too_big(name)
                return response.read()     # raises IncompleteRead when the body is cut short
            chunks, size = [], 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    return b''.join(chunks)
                size += len(chunk)
                if size > self._max_body:
                    raise self._too_big(name)
                chunks.append(chunk)
        except (OSError, http.client.HTTPException) as exc:
            raise self._unreachable(name, exc) from None

    # --- answers ----------------------------------------------------------------------------

    def _version(self, name, answer):
        etag = (answer.get('ETag') or '').strip()
        if not etag:
            raise BackendError(self._at(name) + ': the server sent no ETag, so it cannot hold a '
                               'store (it must send a strong ETag with every GET and PUT)')
        if etag.startswith('W/'):
            raise BackendError(self._at(name) + ': the server sent a weak ETag (W/...), which '
                               'If-Match never matches. A store needs strong ETags: turn off '
                               'compression for its path')
        if len(etag) > 1024 or not all(' ' <= ch <= '~' for ch in etag):
            raise BackendError(self._at(name) + ': the server sent an ETag that is not one')
        return etag

    def _conflict(self, name, if_match, create_only, status):
        if create_only:
            what = 'already exists'
        elif status == 404:
            what = 'no longer exists'
        elif if_match is not None:
            what = 'changed since version ' + if_match
        else:
            what = 'is being changed by another writer'
        return Conflict(self._at(name) + ' ' + what + ' (HTTP %d)' % status)

    def _refused(self, status, method, name):
        at = self._at(name)
        if status in (401, 403):
            if not self._token:
                return Denied(at + ': the server wants a token (HTTP %d). Set %s or %s.'
                              % (status, TOKEN_ENV, TOKEN_FILE_ENV))
            if status == 401:
                return Denied(at + ': the server refused the token (HTTP 401). Check %s or %s.'
                              % (TOKEN_ENV, TOKEN_FILE_ENV))
            return Denied(at + ': the server refused the request (HTTP 403): the token may not '
                          + method + ' it')
        if status in (408, 429) or status >= 500:
            return Unavailable(at + ': the server answered HTTP %d to %s: try again later'
                               % (status, method))
        if status == 304:
            return BackendError(at + ': the server answered 304 (not modified) to a ' + method
                                + ' that asked for no comparison')
        if 300 <= status < 400:
            return BackendError(at + ': the server answered with a redirect (HTTP %d), which is '
                                'not followed: put the final address in the store URL' % status)
        if status == 413:
            return BackendError(at + ': the object is larger than the server takes (HTTP 413)')
        return BackendError(at + ': the server answered HTTP %d to %s' % (status, method))

    def _too_big(self, name):
        return BackendError(self._at(name) + ': the answer is larger than max_body=%d bytes'
                            % self._max_body)

    def _unreachable(self, name, reason):
        if isinstance(reason, (TimeoutError, socket.timeout)):
            why = 'no answer within %g s' % self._timeout
        elif isinstance(reason, socket.gaierror):
            why = 'its host name could not be resolved'
        elif isinstance(reason, ConnectionRefusedError):
            why = 'the connection was refused'
        elif isinstance(reason, ssl.SSLCertVerificationError):
            why = 'its TLS certificate could not be verified (SSL_CERT_FILE adds a CA)'
        elif isinstance(reason, ssl.SSLError):
            why = 'TLS failed (' + type(reason).__name__ + ')'
        elif isinstance(reason, ConnectionError):
            why = 'the connection was lost (' + type(reason).__name__ + ')'
        elif isinstance(reason, http.client.HTTPException):
            why = 'the answer was not valid HTTP (' + type(reason).__name__ + ')'
        else:
            why = 'the request failed (' + type(reason).__name__ + ')'
        return Unavailable(self._at(name) + ': could not reach the store: ' + why)


def _strong(etag):
    etag = (etag or '').strip()
    return etag if etag and not etag.startswith('W/') else None
