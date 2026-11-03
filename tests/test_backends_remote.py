"""What the remote backends do beyond the contract: how each maps errors, what it refuses,
and what keeps a token or a credential out of messages and logs.

- HttpBackend against a stub on 127.0.0.1 that gives scripted answers and keeps the
  requests it got.
- The reference server (mendwright.backends.server), sent raw requests, so that no check of
  the client's hides a gap in the server's.
- S3Backend against the same stub standing in for an S3 endpoint (AWS_ENDPOINT_URL_S3), so
  botocore's own parsing and retry handling run; and moto for a bucket that does not exist.
  Skipped without boto3 (or moto).
- backend_for(url).

Nothing here reaches AWS or any other network service: every address is 127.0.0.1, and
every AWS setting is a fake.
"""

import importlib
import inspect
import logging
import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import mendwright
from mendwright.backends import (OVERRIDES, BackendError, Conflict, Denied, MemoryBackend, Read,
                                 Unavailable, key_record_name)
from mendwright.backends.http import HttpBackend
from mendwright.backends.server import StoreServer, etag_of
from mendwright.backends.url import backend_for

TOKEN = 'remote-token-SECRETVALUE-0123456789'
WRONG = 'wrong-token-OTHERSECRET-9876543210'
SECRETS = ('SECRETVALUE', 'OTHERSECRET', 'QUERYSECRET')
BOTO3 = '1.35.69'
MOTO = '5.1.22'
LOCK = '.mendwright-server.lock'


@pytest.fixture(autouse=True)
def _no_store_token(monkeypatch):
    for name in ('MENDWRIGHT_STORE_TOKEN', 'MENDWRIGHT_STORE_TOKEN_FILE'):
        monkeypatch.delenv(name, raising=False)


def aws_offline(monkeypatch, tmp_path):
    """Fake credentials, and none of what could send boto3 to AWS or to another account:
    no profile, role, endpoint or config file of the developer's, no instance metadata."""
    for name in list(os.environ):
        if name.upper().startswith('AWS_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED', 'true')
    monkeypatch.setenv('AWS_CONFIG_FILE', str(tmp_path / 'no-aws-config'))
    monkeypatch.setenv('AWS_SHARED_CREDENTIALS_FILE', str(tmp_path / 'no-aws-credentials'))


def need_boto3():
    return pytest.importorskip(
        'boto3', minversion=BOTO3,
        reason='boto3 ' + BOTO3 + ' or later runs the S3 tests: pip install "mendwright[s3]" '
               '(the test extra has it)')


def need_moto():
    return pytest.importorskip(
        'moto', minversion=MOTO,
        reason='moto ' + MOTO + ' or later runs the S3 tests offline: pip install "moto[s3]>=' + MOTO
               + '" (the test extra has it)')


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def messages(exc):
    """str() and repr() of an exception and of every exception chained to it."""
    seen = []
    while exc is not None and len(seen) < 20:
        seen += [str(exc), repr(exc)]
        exc = exc.__cause__ or exc.__context__
    return seen


def leaks(*texts):
    return [s for s in SECRETS for text in texts if s in text]


# --- a stub HTTP server ----------------------------------------------------------------------

class _StubServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)        # no getfqdn() lookup
        self.server_name, self.server_port = '127.0.0.1', self.server_address[1]


class Stub:
    """Answers each request with the next of `answers`: (status, headers, body), or a
    function of the request handler that writes what it likes and returns None. Keeps
    each request in `requests` as (method, path, headers, body)."""

    def __init__(self):
        self.answers = []
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def answer(self):
                length = (self.headers.get('Content-Length') or '').strip()
                body = self.rfile.read(int(length)) if length.isdigit() else b''
                stub.requests.append((self.command, self.path, self.headers, body))
                answer = stub.answers.pop(0) if stub.answers else (599, {}, b'no answer left')
                if callable(answer):
                    answer(self)
                    self.close_connection = True
                    return
                status, headers, data = answer
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                if status not in (204, 304):
                    self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                if data and status not in (204, 304) and self.command != 'HEAD':
                    self.wfile.write(data)

            do_GET = do_PUT = do_DELETE = do_HEAD = do_POST = answer

            def log_message(self, *args):
                pass

        self.httpd = _StubServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d/' % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05},
                                       daemon=True)
        self.thread.start()

    def answer(self, status, headers=None, body=b''):
        self.answers.append((status, headers or {}, body))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    made = Stub()
    yield made
    made.close()


@pytest.fixture
def client(stub, monkeypatch):
    """An HttpBackend on the stub, with the token set."""
    def make(**options):
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
        return HttpBackend(stub.url, **options)
    return make


def call(backend, op, **options):
    if op == 'get':
        return backend.get(OVERRIDES, **options)
    if op == 'put':
        return backend.put(OVERRIDES, b'body', **options)
    return backend.delete(OVERRIDES, **options)


def slow(seconds):
    def answer(handler):
        time.sleep(seconds)
    return answer


def hang_up(handler):
    pass


def garbage(handler):
    handler.wfile.write(b'NOT HTTP AT ALL\r\n\r\n')


def cut_short(handler):
    handler.wfile.write(b'HTTP/1.1 200 OK\r\nETag: "v1"\r\nContent-Length: 100\r\n'
                        b'Connection: close\r\n\r\nonly part')


# --- HttpBackend: requests --------------------------------------------------------------------

def test_http_sends_each_condition_as_its_header_and_the_token_as_bearer(client, stub):
    backend = client()
    stub.answer(304, {'ETag': '"v1"'})
    stub.answer(200, {'ETag': '"v2"'})
    stub.answer(201, {'ETag': '"v3"'})
    stub.answer(204)
    stub.answer(200, {'ETag': '"v4"'}, b'the body')
    assert backend.get(OVERRIDES, if_none_match='"v1"') == Read(None, '"v1"', True)
    assert backend.put(OVERRIDES, b'one', if_match='"v1"') == '"v2"'
    assert backend.put(key_record_name('k'), b'two', create_only=True) == '"v3"'
    assert backend.delete(OVERRIDES, if_match='"v3"') is None
    assert backend.get(OVERRIDES) == Read(b'the body', '"v4"', False)
    (m1, p1, h1, _), (m2, p2, h2, b2), (m3, p3, h3, b3), (m4, _, h4, _), (m5, _, h5, _) = stub.requests
    assert (m1, p1, h1.get('If-None-Match')) == ('GET', '/overrides.json', '"v1"')
    assert (m2, p2, h2.get('If-Match'), b2) == ('PUT', '/overrides.json', '"v1"', b'one')
    assert 'If-None-Match' not in h2
    assert (m3, p3, h3.get('If-None-Match'), b3) == ('PUT', '/' + key_record_name('k'), '*', b'two')
    assert 'If-Match' not in h3
    assert (m4, h4.get('If-Match')) == ('DELETE', '"v3"')
    assert 'If-Match' not in h5 and 'If-None-Match' not in h5
    for _, _, headers, _ in stub.requests:
        assert headers.get('Authorization') == 'Bearer ' + TOKEN
        assert headers.get('User-Agent').startswith('mendwright-store/')


def test_http_without_a_token_sends_none_and_the_denial_says_how_to_set_one(stub, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', '')      # an empty value is not a token
    stub.answer(401)
    with pytest.raises(Denied) as info:
        HttpBackend(stub.url).get(OVERRIDES)
    assert 'Authorization' not in stub.requests[0][2]
    assert 'MENDWRIGHT_STORE_TOKEN' in str(info.value)


def test_http_reads_the_token_from_a_file_in_any_common_encoding(stub, monkeypatch, tmp_path):
    forms = [TOKEN.encode() + b'\n', b'\xef\xbb\xbf' + TOKEN.encode() + b'\r\n',
             (TOKEN + '\r\n').encode('utf-16')]        # PowerShell 5's > writes UTF-16
    for i, data in enumerate(forms):
        path = tmp_path / ('token%d' % i)
        path.write_bytes(data)
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN_FILE', str(path))
        stub.answer(404)
        HttpBackend(stub.url).get(OVERRIDES)
        assert stub.requests[-1][2].get('Authorization') == 'Bearer ' + TOKEN


@pytest.mark.parametrize('env, says', [
    ({'MENDWRIGHT_STORE_TOKEN': TOKEN, 'MENDWRIGHT_STORE_TOKEN_FILE': 'token.txt'}, 'not both'),
    ({'MENDWRIGHT_STORE_TOKEN_FILE': 'missing.txt'}, 'does not exist'),
    ({'MENDWRIGHT_STORE_TOKEN_FILE': 'empty.txt'}, 'empty'),
    ({'MENDWRIGHT_STORE_TOKEN_FILE': 'spaced.txt'}, 'header cannot carry'),
    ({'MENDWRIGHT_STORE_TOKEN': 'SECRETVALUE with spaces'}, 'header cannot carry'),
    ({'MENDWRIGHT_STORE_TOKEN': 'SECRETVALUE\r\nX-Injected: 1'}, 'header cannot carry'),
    ({'MENDWRIGHT_STORE_TOKEN': 'SECRETVALUE-caf\u00e9'}, 'header cannot carry'),
])
def test_http_a_token_it_cannot_use_is_denied_without_quoting_it(env, says, monkeypatch, tmp_path):
    (tmp_path / 'token.txt').write_text(TOKEN)
    (tmp_path / 'empty.txt').write_text('  \n')
    (tmp_path / 'spaced.txt').write_text('SECRETVALUE and more\n')
    monkeypatch.chdir(tmp_path)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(Denied) as info:
        HttpBackend('https://store.example.com/team/')
    assert says in str(info.value)
    assert not leaks(*messages(info.value))


# --- HttpBackend: answers ---------------------------------------------------------------------

@pytest.mark.parametrize('status', [401, 403])
@pytest.mark.parametrize('op', ['get', 'put', 'delete'])
def test_http_refused_credentials_raise_denied(client, stub, op, status):
    stub.answer(status)
    with pytest.raises(Denied):
        call(client(), op)


@pytest.mark.parametrize('status', [408, 429, 500, 502, 503, 504])
@pytest.mark.parametrize('op', ['get', 'put', 'delete'])
def test_http_server_trouble_raises_unavailable_without_a_retry(client, stub, op, status):
    stub.answer(status)
    with pytest.raises(Unavailable):
        call(client(), op)
    assert len(stub.requests) == 1


@pytest.mark.parametrize('status', [400, 405, 410, 413, 418])
@pytest.mark.parametrize('op', ['get', 'put', 'delete'])
def test_http_any_other_answer_raises_a_plain_backend_error(client, stub, op, status):
    stub.answer(status)
    with pytest.raises(BackendError) as info:
        call(client(), op)
    assert type(info.value) is BackendError


@pytest.mark.parametrize('status', [409, 412])
def test_http_a_failed_precondition_on_put_is_a_conflict(client, stub, status):
    backend = client()
    stub.answer(status)
    with pytest.raises(Conflict):
        backend.put(OVERRIDES, b'x', if_match='"v1"')
    stub.answer(status)
    with pytest.raises(Conflict, match='already exists'):
        backend.put(OVERRIDES, b'x', create_only=True)


def test_http_a_404_is_missing_on_get_gone_on_delete_and_a_conflict_on_put_if_match(client, stub):
    backend = client()
    stub.answer(404)
    assert backend.get(OVERRIDES, if_none_match='"v1"') == Read(None, None, False)
    stub.answer(404)
    assert backend.delete(OVERRIDES, if_match='"v1"') is None
    stub.answer(404)
    with pytest.raises(Conflict, match='no longer exists'):
        backend.put(OVERRIDES, b'x', if_match='"v1"')
    stub.answer(404)                    # a write to a place that is not there
    with pytest.raises(BackendError) as info:
        backend.put(OVERRIDES, b'x')
    assert type(info.value) is BackendError


@pytest.mark.parametrize('status', [409, 412])
def test_http_a_failed_delete_is_checked_with_one_read(client, stub, status):
    backend = client()
    stub.answer(status)                 # RFC 9110's answer to If-Match on a missing object
    stub.answer(404)
    assert backend.delete(OVERRIDES, if_match='"v1"') is None
    stub.answer(status)
    stub.answer(200, {'ETag': '"v2"'}, b'changed')
    with pytest.raises(Conflict):
        backend.delete(OVERRIDES, if_match='"v1"')
    assert [r[0] for r in stub.requests] == ['DELETE', 'GET', 'DELETE', 'GET']


def test_http_not_modified_keeps_the_version_and_a_server_that_ignores_if_none_match_too(client, stub):
    backend = client()
    stub.answer(304)                    # no ETag on the 304
    assert backend.get(OVERRIDES, if_none_match='"v1"') == Read(None, '"v1"', True)
    stub.answer(200, {'ETag': '"v1"'}, b'body')
    assert backend.get(OVERRIDES, if_none_match='"v1"') == Read(None, '"v1"', True)


@pytest.mark.parametrize('answer, says', [
    ((200, {}, b'body'), 'no ETag'),
    ((200, {'ETag': 'W/"v1"'}, b'body'), 'weak ETag'),
])
def test_http_a_store_needs_strong_etags(client, stub, answer, says):
    backend = client()
    stub.answers.append(answer)
    with pytest.raises(BackendError, match=says):
        backend.get(OVERRIDES)
    stub.answers.append((answer[0], answer[1], b''))
    with pytest.raises(BackendError, match=says):
        backend.put(OVERRIDES, b'x')


def test_http_follows_no_redirect(client, stub):
    other = Stub()
    try:
        backend = client()
        stub.answer(302, {'Location': other.url + 'overrides.json'})
        stub.answer(307, {'Location': other.url + 'overrides.json'})
        with pytest.raises(BackendError, match='redirect'):
            backend.get(OVERRIDES)
        with pytest.raises(BackendError, match='redirect'):
            backend.put(OVERRIDES, b'x')
        assert other.requests == []
    finally:
        other.close()


def test_http_refuses_an_answer_larger_than_max_body(client, stub):
    stub.answer(200, {'ETag': '"v1"'}, b'x' * 100)
    with pytest.raises(BackendError, match='max_body'):
        client(max_body=10).get(OVERRIDES)

    def chunked(handler):               # no Content-Length to go by
        handler.wfile.write(b'HTTP/1.1 200 OK\r\nETag: "v1"\r\nTransfer-Encoding: chunked\r\n'
                            b'Connection: close\r\n\r\n64\r\n' + b'x' * 100 + b'\r\n0\r\n\r\n')
    stub.answers.append(chunked)
    with pytest.raises(BackendError, match='max_body'):
        client(max_body=10).get(OVERRIDES)


@pytest.mark.parametrize('answer, says', [
    (slow(2.0), 'no answer within 0.3 s'),
    (hang_up, 'connection was lost'),
    (garbage, 'not valid HTTP'),
    (cut_short, 'could not reach'),
])
def test_http_a_timeout_or_a_broken_answer_raises_unavailable(client, stub, answer, says):
    stub.answers.append(answer)
    started = time.monotonic()
    with pytest.raises(Unavailable, match=says):
        client(timeout=0.3).get(OVERRIDES)
    assert time.monotonic() - started < 2.0
    assert len(stub.requests) == 1


def test_http_a_refused_connection_raises_unavailable(monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    backend = HttpBackend('http://127.0.0.1:%d/store/' % free_port())
    with pytest.raises(Unavailable, match='refused|could not reach'):
        backend.get(OVERRIDES)


def test_http_a_loopback_store_never_goes_through_a_proxy(client, stub, monkeypatch):
    proxy = 'http://127.0.0.1:%d' % free_port()        # nothing listens there
    for name in ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy'):
        monkeypatch.setenv(name, proxy)
    for name in ('NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(name, raising=False)
    stub.answer(404)
    assert client().get(OVERRIDES) == Read(None, None, False)
    assert len(stub.requests) == 1


@pytest.mark.parametrize('url, place', [
    ('https://store.example.com/team/', 'https://store.example.com/team/'),
    ('https://store.example.com/team', 'https://store.example.com/team/'),
    ('HTTPS://Store.Example.com:8443', 'https://store.example.com:8443/'),
    ('http://localhost:8787/x/', 'http://localhost:8787/x/'),
    ('http://127.0.0.1/', 'http://127.0.0.1/'),
    ('http://[::1]:8787/store/', 'http://[::1]:8787/store/'),
    # A path that is not ASCII is percent-encoded, as UTF-8; one already encoded stays.
    ('https://store.example.com/équipe/', 'https://store.example.com/%C3%A9quipe/'),
    ('https://store.example.com/%C3%A9quipe/', 'https://store.example.com/%C3%A9quipe/'),
])
def test_http_takes_https_anywhere_and_http_on_loopback(url, place):
    assert HttpBackend(url).describe() == place


def test_http_a_store_url_with_a_path_that_is_not_ascii_is_asked_for_encoded(stub, monkeypatch):
    # http.client sends only ASCII: the store opened with a raw UnicodeEncodeError.
    from mendwright import load_store
    from mendwright.backends.url import store_url
    url = stub.url + 'café/'
    assert store_url(url) == stub.url + 'caf%C3%A9/'
    monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
    stub.answer(404)
    store = load_store(None, url)
    assert store.unread is None and stub.requests[0][:2] == ('GET', '/caf%C3%A9/overrides.json')


@pytest.mark.parametrize('url', [
    'http://store.example.com/team/', 'http://10.1.2.3:8787/', 'http://localhost.example.com/',
    'http://127.0.0.2/', 'ftp://store.example.com/', 'store.example.com/team/', 'https:///team/',
])
def test_http_refuses_plain_http_off_loopback_and_other_schemes(url):
    with pytest.raises(ValueError, match='https://'):
        HttpBackend(url)


def test_http_the_token_is_in_no_description_message_or_log(client, stub, caplog):
    caplog.set_level(logging.DEBUG)
    backend = client(timeout=0.3)
    texts = [backend.describe(), repr(backend)]
    for answer in [(401, {}, b''), (403, {}, b''), (500, {}, b'Bearer ' + TOKEN.encode()),
                   (400, {}, TOKEN.encode()), (302, {'Location': 'http://127.0.0.1:9/'}, b''),
                   (200, {}, b'x'), (200, {'ETag': 'W/"x"'}, b'x'), hang_up, garbage, slow(1.0)]:
        stub.answers.append(answer)
        with pytest.raises(BackendError) as info:
            backend.get(OVERRIDES)
        texts += messages(info.value)
    stub.answer(412)
    with pytest.raises(Conflict) as info:
        backend.put(OVERRIDES, b'x', if_match='"v1"')
    texts += messages(info.value)
    assert not leaks(*texts, caplog.text)


# --- names and versions, refused before any request -------------------------------------------

BAD_NAMES = [
    '', '/overrides.json', '../overrides.json', 'keys/../overrides.json', 'keys//x.json',
    'keys/x.json/', 'keys/', 'Overrides.json', 'keys\\x.json', 'C:/x.json', 'c:x.json', '.hidden',
    'keys/.x.json', 'x.json.', 'con.json', 'keys/nul', 'COM1', 'a b.json', 'x%2e%2e.json',
    'x?.json', 'x#.json', '\u00e9.json', 'x\x00.json', 'a/' * 8 + 'x', 'x' * 513,
]


@pytest.fixture(params=['http', 's3'])
def any_remote(request, monkeypatch, tmp_path):
    """A remote backend whose place nothing listens at: no request may be sent."""
    closed = 'http://127.0.0.1:%d' % free_port()
    if request.param == 'http':
        return HttpBackend(closed + '/store/')
    need_boto3()
    aws_offline(monkeypatch, tmp_path)
    monkeypatch.setenv('AWS_ENDPOINT_URL_S3', closed)
    from mendwright.backends.s3 import S3Backend
    return S3Backend('names-bucket', 'store')


@pytest.mark.parametrize('name', BAD_NAMES)
def test_a_name_outside_the_rules_is_refused_before_any_request(any_remote, name):
    for op in (lambda: any_remote.get(name), lambda: any_remote.put(name, b'x'),
               lambda: any_remote.delete(name)):
        with pytest.raises(ValueError, match='not a store object name'):
            op()


def test_a_version_no_backend_gives_is_refused_before_any_request(any_remote):
    with pytest.raises(TypeError):
        any_remote.get(5)
    with pytest.raises(TypeError):
        any_remote.get(OVERRIDES, if_none_match=5)
    for bad in ('', '"v1"\r\nX-Injected: 1', 'x' * 2000):
        with pytest.raises(ValueError):
            any_remote.put(OVERRIDES, b'x', if_match=bad)
        with pytest.raises(ValueError):
            any_remote.delete(OVERRIDES, if_match=bad)


# --- the reference server, spoken to raw --------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    with StoreServer(tmp_path / 'root', token=TOKEN, max_body=4096) as made:
        yield made


def raw(server, data, timeout=10):
    """Send bytes as they are; (status, headers, body) of the answer, or (None, {}, b'')."""
    port = int(server.url.rstrip('/').rsplit(':', 1)[1])
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as sock:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionError:
                break
            if not chunk:
                break
            chunks.append(chunk)
    answer = b''.join(chunks)
    if not answer.startswith(b'HTTP/'):     # nothing, or an HTTP/0.9 error page with no head
        return None, {}, answer
    head, _, body = answer.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    return int(lines[0].split()[1]), headers, body


def request(server, method, target, *, token=TOKEN, body=None, headers=None):
    lines = ['%s %s HTTP/1.1' % (method, target), 'Host: 127.0.0.1', 'Connection: close']
    if token:
        lines.append('Authorization: Bearer ' + token)
    lines += ['%s: %s' % item for item in (headers or {}).items()]
    if body is not None:
        lines.append('Content-Length: %d' % len(body))
    return raw(server, ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1') + (body or b''))


def only_the_lock_file(server):
    return sorted(os.listdir(server.root)) == [LOCK]


OUTSIDE = [
    '/../outside.json', '/..%2foutside.json', '/%2e%2e/outside.json', '/%2e%2e%2foutside.json',
    '/%252e%252e/outside.json', '/keys/../../outside.json', '/keys/..%5c..%5coutside.json',
    '/keys\\..\\..\\outside.json', '/..\\outside.json', '/./outside.json', '/keys/./x.json',
    '/C:/outside.json', '/c:/outside.json', '/C:%5coutside.json', '/keys/%00.json',
    '/keys/x.json%00', '/keys/\x00.json', '/outside.json.', '/keys./x.json', '/con.json',
    '/keys/NUL', '/keys/nul.json', '/aux', '/com1.json', '/lpt9.txt', '/Overrides.json',
    '/.hidden', '/' + LOCK, '/overrides.json?token=QUERYSECRET', '/overrides.json#x',
    'http://127.0.0.1/../outside.json', '*', '/', '/keys/', '/keys/x.json/',
    '/' + 'a/' * 9 + 'x.json', '/' + 'a' * 600, '/overrides.json%20', '/caf%C3%A9.json',
]


@pytest.mark.parametrize('target', OUTSIDE)
def test_the_server_refuses_every_path_that_is_not_an_object_name(server, tmp_path, target):
    for method, body in (('PUT', b'{"x": 1}'), ('GET', None), ('HEAD', None), ('DELETE', None)):
        status, _, _ = request(server, method, target, body=body)
        assert status == 400, (method, target, status)
    assert only_the_lock_file(server)
    assert sorted(p.name for p in tmp_path.iterdir()) == ['root']


def test_the_server_does_not_follow_a_link_out_of_its_folder(server, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    try:
        os.symlink(outside, os.path.join(server.root, 'keys'), target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != 'nt':
            raise
        import _winapi      # a junction needs no privilege on Windows
        _winapi.CreateJunction(str(outside), os.path.join(server.root, 'keys'))
    status, _, _ = request(server, 'PUT', '/keys/x.json', body=b'x')
    assert status == 400
    status, _, _ = request(server, 'GET', '/keys/x.json')
    assert status == 400
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize('authorization', [
    None, 'Bearer ' + WRONG, 'Bearer ' + TOKEN + 'x', 'Bearer ' + TOKEN[:-1], 'Bearer',
    'Basic ' + TOKEN, TOKEN, 'Token ' + TOKEN, 'Bearer\u00e9 ' + TOKEN,
])
def test_the_server_refuses_a_request_without_the_token(server, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    for method, body in (('PUT', b'x'), ('GET', None), ('DELETE', None)):
        status, answer, _ = request(server, method, '/overrides.json', token=None, headers=headers,
                                    body=body)
        assert status == 401, (method, authorization)
        assert answer['www-authenticate'].startswith('Bearer')
    assert only_the_lock_file(server)


def test_the_server_takes_the_bearer_scheme_in_any_case(server):
    status, _, _ = request(server, 'PUT', '/overrides.json', token=None, body=b'x',
                           headers={'Authorization': 'bearer ' + TOKEN})
    assert status == 201


def test_the_server_refuses_a_body_over_max_body_without_waiting_for_it(server):
    # Content-Length says 4097 and no body follows: the answer must come anyway.
    head = ('PUT /overrides.json HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n'
            'Content-Length: 4097\r\n\r\n' % TOKEN).encode()
    status, _, _ = raw(server, head, timeout=5)
    assert status == 413
    assert only_the_lock_file(server)
    status, _, _ = request(server, 'PUT', '/overrides.json', body=b'x' * 4096)
    assert status == 201


def test_the_server_needs_a_content_length_and_writes_nothing_of_a_body_cut_short(server):
    status, _, _ = request(server, 'PUT', '/overrides.json')
    assert status == 411
    status, _, _ = request(server, 'PUT', '/overrides.json', headers={'Transfer-Encoding': 'chunked'})
    assert status == 411
    port = int(server.url.rstrip('/').rsplit(':', 1)[1])
    with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
        sock.sendall(('PUT /overrides.json HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n'
                      'Content-Length: 100\r\n\r\nonly ten b' % TOKEN).encode())
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(65536) == b''
    assert only_the_lock_file(server)


def test_a_content_length_of_other_digits_than_0_to_9_is_a_bad_request(server, caplog):
    # '\u00b2' passes str.isdigit(), and int() refuses it: the server answered 500 and logged
    # a traceback that quoted the header.
    head = ('PUT /overrides.json HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n'
            'Content-Length: \u00b2\r\n\r\nab' % TOKEN).encode('latin-1')
    with caplog.at_level(logging.DEBUG):
        status, headers, _ = raw(server, head, timeout=5)
    assert status == 400 and headers.get('connection') == 'close'
    assert not [r for r in caplog.records if r.exc_info or '\u00b2' in r.getMessage()]
    assert only_the_lock_file(server)


def test_http_reads_a_body_whose_content_length_has_other_digits(client, stub):
    def answer(handler):
        handler.wfile.write(('HTTP/1.1 200 OK\r\nETag: "v1"\r\nContent-Length: \u00b2\r\n'
                             'Connection: close\r\n\r\nab').encode('latin-1'))
    stub.answers.append(answer)
    read = client().get(OVERRIDES)
    assert (read.body, read.version) == (b'ab', '"v1"')


def test_the_server_etag_is_a_strong_hash_of_the_body(server):
    body = b'{"selectors": {}}'
    status, headers, _ = request(server, 'PUT', '/overrides.json', body=body)
    etag = headers['etag']
    assert status == 201 and etag == etag_of(body) and etag.startswith('"') and len(etag) == 34
    status, headers, _ = request(server, 'PUT', '/overrides.json', body=body)
    assert (status, headers['etag']) == (200, etag)
    status, headers, data = request(server, 'GET', '/overrides.json')
    assert (status, headers['etag'], data) == (200, etag, body)
    assert headers['content-type'] == 'application/json' and headers['cache-control'] == 'no-store'
    assert headers['server'] == 'mendwright-store'
    status, headers, data = request(server, 'HEAD', '/overrides.json')
    assert (status, headers['etag'], headers['content-length'], data) == (200, etag, str(len(body)), b'')


def test_the_server_compares_tags_as_rfc_9110_says(server):
    status, headers, _ = request(server, 'PUT', '/overrides.json', body=b'one')
    etag = headers['etag']
    for tag in (etag, 'W/' + etag, '"other", ' + etag, '*'):    # weak comparison for GET
        status, headers, _ = request(server, 'GET', '/overrides.json', headers={'If-None-Match': tag})
        assert (status, headers['etag']) == (304, etag), tag
    status, _, _ = request(server, 'PUT', '/overrides.json', body=b'two',
                           headers={'If-Match': 'W/' + etag})          # strong for If-Match
    assert status == 412
    status, _, _ = request(server, 'PUT', '/overrides.json', body=b'two', headers={'If-None-Match': '*'})
    assert status == 412
    status, headers, _ = request(server, 'PUT', '/overrides.json', body=b'two',
                                 headers={'If-Match': '"other", ' + etag})
    assert (status, headers['etag']) == (200, etag_of(b'two'))
    status, _, _ = request(server, 'DELETE', '/overrides.json', headers={'If-Match': etag})
    assert status == 412
    status, _, _ = request(server, 'DELETE', '/overrides.json', headers={'If-Match': '*'})
    assert status == 204
    # A missing object: If-Match on PUT fails, and DELETE says 404 whatever If-Match says.
    status, _, _ = request(server, 'PUT', '/overrides.json', body=b'x', headers={'If-Match': '*'})
    assert status == 412
    status, _, _ = request(server, 'DELETE', '/overrides.json', headers={'If-Match': etag})
    assert status == 404
    assert only_the_lock_file(server)


def test_the_server_logs_names_and_statuses_and_never_a_token(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    with StoreServer(tmp_path / 'root', token=TOKEN) as server:
        request(server, 'PUT', '/overrides.json', body=b'x')
        request(server, 'GET', '/overrides.json', token=WRONG)
        request(server, 'GET', '/overrides.json', token=None, headers={'Authorization': 'Basic ' + WRONG})
        request(server, 'GET', '/overrides.json?token=QUERYSECRET')
        raw(server, b'GET /x?token=QUERYSECRET HTTP/1.1 junk ' + TOKEN.encode() + b'\r\n\r\n')
        raw(server, b'BREW /overrides.json?token=QUERYSECRET HTTP/1.1\r\nAuthorization: Bearer '
            + TOKEN.encode() + b'\r\n\r\n')
    assert 'PUT overrides.json 201' in caplog.text
    assert 'GET overrides.json refused: wrong token' in caplog.text
    assert 'GET overrides.json refused: not a bearer token' in caplog.text
    assert not leaks(caplog.text)


def test_the_server_needs_a_long_token_unless_it_serves_localhost_without_one(tmp_path):
    with pytest.raises(ValueError, match='needs a token'):
        StoreServer(tmp_path / 'a')
    with pytest.raises(ValueError, match='shorter than 16'):
        StoreServer(tmp_path / 'b', token='short')
    with pytest.raises(ValueError, match='header cannot carry'):
        StoreServer(tmp_path / 'c', token='a token with spaces in it')
    with pytest.raises(ValueError, match='localhost only'):
        StoreServer(tmp_path / 'd', no_auth=True, host='0.0.0.0')
    with pytest.raises(ValueError, match='not both'):
        StoreServer(tmp_path / 'e', no_auth=True, token=TOKEN)
    with StoreServer(tmp_path / 'f', no_auth=True) as server:
        backend = HttpBackend(server.url)
        version = backend.put(OVERRIDES, b'x')
        assert backend.get(OVERRIDES) == Read(b'x', version, False)


def test_the_server_serves_one_folder_once(tmp_path):
    with StoreServer(tmp_path / 'root', token=TOKEN):
        with pytest.raises(ValueError, match='another store server'):
            StoreServer(tmp_path / 'root', token=TOKEN)
    with StoreServer(tmp_path / 'root', token=TOKEN):
        pass


# --- the server command -----------------------------------------------------------------------

def server_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('MENDWRIGHT_STORE_TOKEN')}
    package_root = str(Path(mendwright.__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
    env.update(extra)
    return env


def run_server(*args, **env):
    return subprocess.run([sys.executable, '-m', 'mendwright.backends.server', *args],
                          capture_output=True, text=True, env=server_env(**env), timeout=60)


def test_the_server_command_explains_itself():
    done = run_server('--help')
    assert done.returncode == 0
    assert '--root' in done.stdout and '--token-file' in done.stdout and '--no-auth' in done.stdout


def test_the_server_command_will_not_start_without_a_token(tmp_path):
    done = run_server('--root', str(tmp_path / 'root'), '--port', '0')
    assert done.returncode == 2 and 'MENDWRIGHT_STORE_TOKEN' in done.stderr
    done = run_server('--root', str(tmp_path / 'root'), '--no-auth', '--host', '0.0.0.0')
    assert done.returncode == 2 and 'localhost only' in done.stderr


def test_the_server_command_serves_a_store_and_logs_no_token(tmp_path, monkeypatch):
    token_file = tmp_path / 'token'
    token_file.write_text(TOKEN + '\n')
    process = subprocess.Popen(
        [sys.executable, '-m', 'mendwright.backends.server', '--root', str(tmp_path / 'root'),
         '--port', '0', '--token-file', str(token_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=server_env())
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        first = lines.get(timeout=60)
        assert ' at http://127.0.0.1:' in first, first
        url = first.rsplit(' at ', 1)[1].strip()
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', TOKEN)
        backend = HttpBackend(url)
        version = backend.put(OVERRIDES, b'{"selectors": {}}', create_only=True)
        assert backend.get(OVERRIDES) == Read(b'{"selectors": {}}', version, False)
        monkeypatch.setenv('MENDWRIGHT_STORE_TOKEN', WRONG)
        with pytest.raises(Denied):
            HttpBackend(url).get(OVERRIDES)
    finally:
        process.terminate()
        out, err = process.communicate(timeout=60)
    assert 'PUT overrides.json 201' in err
    assert not leaks(out, err)


# --- S3Backend --------------------------------------------------------------------------------

def s3_error(status, code):
    body = ('<?xml version="1.0" encoding="UTF-8"?><Error><Code>%s</Code><Message>The message, '
            'which can quote a key id</Message><RequestId>R1</RequestId></Error>' % code).encode()
    return status, {'Content-Type': 'application/xml'}, body


@pytest.fixture
def s3(stub, monkeypatch, tmp_path):
    """An S3Backend whose endpoint is the stub."""
    need_boto3()
    aws_offline(monkeypatch, tmp_path)
    monkeypatch.setenv('AWS_ENDPOINT_URL_S3', stub.url.rstrip('/'))
    from mendwright.backends.s3 import S3Backend
    return S3Backend('stub-bucket', 'team', connect_timeout=2, read_timeout=2)


def test_s3_sends_each_condition_as_its_parameter(s3, stub):
    stub.answer(304, {'ETag': '"v1"'})
    stub.answer(200, {'ETag': '"v2"'})
    stub.answer(200, {'ETag': '"v3"'})
    stub.answer(204)
    stub.answer(200, {'ETag': '"v4"'}, b'the body')
    stub.answers.append(s3_error(404, 'NoSuchKey'))
    assert s3.get(OVERRIDES, if_none_match='"v1"') == Read(None, '"v1"', True)
    assert s3.put(OVERRIDES, b'one', if_match='"v1"') == '"v2"'
    assert s3.put(key_record_name('k'), b'two', create_only=True) == '"v3"'
    assert s3.delete(OVERRIDES, if_match='"v3"') is None
    assert s3.get(OVERRIDES) == Read(b'the body', '"v4"', False)
    assert s3.get(OVERRIDES) == Read(None, None, False)
    (m1, p1, h1, _), (m2, _, h2, _), (m3, p3, h3, _), (m4, _, h4, _) = stub.requests[:4]
    assert (m1, p1, h1.get('If-None-Match')) == ('GET', '/stub-bucket/team/overrides.json', '"v1"')
    assert (m2, h2.get('If-Match')) == ('PUT', '"v1"') and 'If-None-Match' not in h2
    assert (m3, p3, h3.get('If-None-Match')) == ('PUT', '/stub-bucket/team/' + key_record_name('k'), '*')
    assert (m4, h4.get('If-Match')) == ('DELETE', '"v3"')


S3_ERRORS = [
    ('get', [s3_error(403, 'AccessDenied')], Denied),
    ('get', [s3_error(404, 'NoSuchBucket')], Denied),
    ('get', [s3_error(400, 'ExpiredToken')], Denied),
    ('get', [s3_error(503, 'SlowDown')], Unavailable),
    ('get', [s3_error(500, 'InternalError')], Unavailable),
    ('get', [s3_error(400, 'InvalidArgument')], BackendError),
    ('put', [s3_error(412, 'PreconditionFailed')], Conflict),
    ('put', [s3_error(409, 'ConditionalRequestConflict')], Conflict),
    ('put', [s3_error(404, 'NoSuchKey')], Conflict),
    ('put', [s3_error(404, 'NoSuchBucket')], Denied),
    ('put', [s3_error(403, 'AccessDenied')], Denied),
    ('put', [s3_error(503, 'SlowDown')], Unavailable),
    ('put', [s3_error(502, 'BadGateway')], Unavailable),
    ('delete', [s3_error(412, 'PreconditionFailed')], Conflict),
    ('delete', [s3_error(409, 'ConditionalRequestConflict'), (200, {'ETag': '"v2"'}, b'x')], Conflict),
    ('delete', [s3_error(404, 'NoSuchBucket')], Denied),
    ('delete', [s3_error(403, 'AccessDenied')], Denied),
    ('delete', [s3_error(503, 'SlowDown')], Unavailable),
]


@pytest.mark.parametrize('op, answers, error', S3_ERRORS)
def test_s3_errors_map_to_the_contract_without_a_retry(s3, stub, op, answers, error):
    stub.answers.extend(answers)
    with pytest.raises(error) as info:
        call(s3, op, **({} if op == 'get' else {'if_match': '"v1"'}))
    if error is BackendError:
        assert type(info.value) is BackendError
    assert len(stub.requests) == len(answers)       # botocore retried nothing
    assert 'which can quote' not in str(info.value)  # S3's own text is left out


def test_s3_a_missing_object_after_a_conflicting_delete_is_done(s3, stub):
    stub.answers += [s3_error(409, 'ConditionalRequestConflict'), s3_error(404, 'NoSuchKey')]
    assert s3.delete(OVERRIDES, if_match='"v1"') is None
    stub.answers.append(s3_error(404, 'NoSuchKey'))
    assert s3.delete(OVERRIDES, if_match='"v1"') is None
    assert [r[0] for r in stub.requests] == ['DELETE', 'GET', 'DELETE']


def test_s3_a_403_on_get_says_how_list_bucket_turns_a_missing_object_into_404(s3, stub):
    stub.answers.append(s3_error(403, 'AccessDenied'))
    with pytest.raises(Denied, match='s3:ListBucket'):
        s3.get(OVERRIDES)


def test_s3_a_timeout_or_no_endpoint_raises_unavailable(s3, stub, monkeypatch):
    from mendwright.backends.s3 import S3Backend
    stub.answers.append(slow(3.0))
    started = time.monotonic()
    with pytest.raises(Unavailable):
        S3Backend('stub-bucket', 'team', read_timeout=0.5).get(OVERRIDES)
    assert time.monotonic() - started < 3.0 and len(stub.requests) == 1
    monkeypatch.setenv('AWS_ENDPOINT_URL_S3', 'http://127.0.0.1:%d' % free_port())
    with pytest.raises(Unavailable, match='could not reach S3'):
        S3Backend('stub-bucket', 'team').put(OVERRIDES, b'x')


def test_s3_without_credentials_is_denied(s3, stub, monkeypatch):
    from mendwright.backends.s3 import S3Backend
    monkeypatch.delenv('AWS_ACCESS_KEY_ID')
    monkeypatch.delenv('AWS_SECRET_ACCESS_KEY')
    with pytest.raises(Denied, match='no usable AWS credentials'):
        S3Backend('stub-bucket', 'team').get(OVERRIDES)
    assert stub.requests == []


def test_s3_a_bucket_that_does_not_exist_is_denied(monkeypatch, tmp_path):
    need_boto3()
    moto = need_moto()
    aws_offline(monkeypatch, tmp_path)
    from mendwright.backends.s3 import S3Backend
    with moto.mock_aws():
        backend = S3Backend('no-such-bucket-here', 'team')
        for op in ('get', 'put', 'delete'):
            with pytest.raises(Denied, match='does not exist'):
                call(backend, op)


def test_s3_takes_credentials_only_from_the_aws_chain():
    need_boto3()
    from mendwright.backends.s3 import S3Backend
    names = set(inspect.signature(S3Backend).parameters)
    assert names == {'bucket', 'prefix', 'connect_timeout', 'read_timeout', 'max_body'}


def test_s3_without_boto3_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, 'boto3', None)
    monkeypatch.delitem(sys.modules, 'mendwright.backends.s3', raising=False)
    with pytest.raises(ImportError, match=r'pip install "mendwright\[s3\]"'):
        importlib.import_module('mendwright.backends.s3')
    with pytest.raises(ImportError, match=r'mendwright\[s3\]'):
        backend_for('s3://store-bucket/team/')
    # The other kinds of store need no boto3.
    assert backend_for('https://store.example.com/team/').describe() == 'https://store.example.com/team/'
    assert isinstance(backend_for('memory:'), MemoryBackend)


def test_s3_with_a_boto3_too_old_for_conditional_writes_says_how_to_upgrade():
    need_boto3()
    from mendwright.backends import s3

    class Old:
        """A client whose S3 model has no IfMatch, as boto3 before 1.35.69."""
        class meta:
            class service_model:
                @staticmethod
                def operation_model(name):
                    class operation:
                        class input_shape:
                            members = {'Bucket': None, 'Key': None, 'IfNoneMatch': None}
                    return operation

    with pytest.raises(ImportError, match=r'pip install -U "mendwright\[s3\]"'):
        s3._check_conditional_support(Old())


# --- backend_for ------------------------------------------------------------------------------

@pytest.mark.parametrize('path', [
    'C:\\store\\overrides.json', 'C:/store/overrides.json', 'c:store', '\\\\server\\share\\overrides.json',
    '/srv/store/overrides.json', './.mendwright/overrides.json', '.mendwright/overrides.json',
    '~/store/overrides.json', 'overrides.json', '', Path('store') / 'overrides.json',
])
def test_backend_for_a_local_path_is_none(path):
    assert backend_for(path) is None


@pytest.mark.parametrize('url, place', [
    ('https://store.example.com/team/', 'https://store.example.com/team/'),
    ('https://store.example.com/team', 'https://store.example.com/team/'),
    ('https://store.example.com/team/overrides.json', 'https://store.example.com/team/'),
    ('https://store.example.com', 'https://store.example.com/'),
    ('https://store.example.com/overrides.json', 'https://store.example.com/'),
    ('http://localhost:8787/x/', 'http://localhost:8787/x/'),
    ('http://[::1]:8787/', 'http://[::1]:8787/'),
])
def test_backend_for_an_https_url_is_an_http_backend_on_its_folder(url, place):
    backend = backend_for(url)
    assert isinstance(backend, HttpBackend) and backend.describe() == place


@pytest.mark.parametrize('url, place', [
    ('s3://store-bucket/team/', 's3://store-bucket/team/'),
    ('s3://store-bucket/team', 's3://store-bucket/team/'),
    ('s3://store-bucket/team/overrides.json', 's3://store-bucket/team/'),
    ('s3://store-bucket/a/b/c/', 's3://store-bucket/a/b/c/'),
    ('S3://store-bucket', 's3://store-bucket/'),
    ('s3://store-bucket/overrides.json', 's3://store-bucket/'),
])
def test_backend_for_an_s3_url_is_an_s3_backend_on_its_prefix(url, place, monkeypatch, tmp_path):
    need_boto3()
    aws_offline(monkeypatch, tmp_path)
    from mendwright.backends.s3 import S3Backend
    backend = backend_for(url)
    assert isinstance(backend, S3Backend) and backend.describe() == place


def test_backend_for_memory_is_a_new_memory_backend_each_time():
    first, second = backend_for('memory:'), backend_for('MEMORY:')
    assert isinstance(first, MemoryBackend) and isinstance(second, MemoryBackend) and first is not second


@pytest.mark.parametrize('url, says', [
    ('https://user:SECRETVALUE@store.example.com/x/', 'user name or password'),
    ('https://SECRETVALUE@store.example.com/x/', 'user name or password'),
    ('s3://AKIAEXAMPLE:SECRETVALUE@store-bucket/x/', 'user name or password'),
    ('https://store.example.com/x/?X-Amz-Signature=SECRETVALUE&X-Amz-Credential=AKIAEXAMPLE',
     'X-Amz-Signature, X-Amz-Credential'),
    ('s3://store-bucket/x/?X-Amz-Security-Token=SECRETVALUE', 'X-Amz-Security-Token'),
    ('https://store.example.com/x/?token=SECRETVALUE', '(token)'),
    ('https://store.example.com/x/?sv=2024&sig=SECRETVALUE', '(sig)'),
    ('https://store.example.com/x/?X%2DAmz%2DSignature=SECRETVALUE', 'X-Amz-Signature'),
    ('https://store.example.com/x/?SECRETVALUE', 'query string'),
    ('https://store.example.com/x/#SECRETVALUE', 'fragment'),
    ('https://store.example.com/x/ SECRETVALUE', 'space'),
    ('https://store.example.com\\@SECRETVALUE/x/', 'backslash'),
    ('http://store.example.com/x/', 'https://'),
    ('http://10.1.2.3:8787/x/', 'https://'),
    ('https://store.example.com:SECRETVALUE/x/', 'not a valid URL'),
    ('ftp://store.example.com/x/', 'not ftp:'),
    ('file:///srv/store/', 'not file:'),
    ('gs://store-bucket/x/', 'not gs:'),
    ('localhost:8787/x/', 'not localhost:'),
    ('s3://store-bucket/x/store.json', 'a file'),
    ('https://store.example.com/x/store.json', 'a file'),
    ('https://store.example.com/x/SECRETVALUE.json', 'a file'),
    ('s3://Store_Bucket/x/', 'not an S3 bucket name'),
    ('s3://store-bucket:9000/x/', 'no port'),
    ('s3:///x/', 'needs a bucket'),
    ('s3://store-bucket/a//b/', 'empty segment'),
    ('s3://store-bucket/a/../b/', 'not a store prefix'),
    ('memory:x', 'nothing after it'),
])
def test_backend_for_refuses_what_no_backend_takes_and_quotes_no_secret(url, says):
    with pytest.raises(ValueError) as info:
        backend_for(url)
    assert says in str(info.value)
    assert not leaks(*messages(info.value))
