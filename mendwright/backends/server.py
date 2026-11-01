"""The reference store server: objects as files under one folder, served over HTTP with
strong ETags and atomic conditional writes. Standard library only.

    python -m mendwright.backends.server --root DIR [--host 127.0.0.1] [--port 8787]
        [--token-file FILE] [--no-auth] [--max-body BYTES]

It is what HttpBackend is tested against, and a small store for a team without S3. It
speaks plain HTTP, and mendwright connects to a host other than localhost over https only,
so to serve other machines, put a TLS-terminating proxy in front of it (see
docs/remote-store.md). Run one server per folder: a second one on the same folder refuses
to start, because the atomic writes rely on one process's locks.

- Every request needs "Authorization: Bearer <token>", else 401. The token comes from
  --token-file, or MENDWRIGHT_STORE_TOKEN_FILE, or MENDWRIGHT_STORE_TOKEN (one of the two
  variables), never from the command line, where other users of the machine can read it.
  --no-auth serves without a token, on localhost only.
- An object's name is its URL path without the leading '/': segments of lower-case
  letters, digits, '.', '_' and '-' joined by '/' (no '..', '%', '\\' or empty segment, no
  query string). Anything else is 400, so no request reaches outside the folder.
- An ETag is the first 32 hex digits of the SHA-256 of the body, quoted: strong, and the
  same for the same body.
- GET and HEAD: 200 with the ETag, 304 when If-None-Match matches it, 404 when missing.
- PUT: needs Content-Length (411 without one), at most --max-body bytes (413). The body is
  read first; then If-Match and If-None-Match are checked and the file replaced under one
  lock, so of two writers with the same If-Match only one wins. 412 when a precondition
  fails, else 201 (created) or 200 (replaced), with the new ETag.
- DELETE: 404 when the object is missing, whatever If-Match says (gone is not an error),
  412 when If-Match does not match, else 204.
- A write goes to a temporary file next to the object, is flushed to disk, and replaces the
  object in one step, so a reader gets the old body or the new one, never a mix.
- The log has the method, the object's name and the status: never a header, a token, a
  query string or a request line it could not parse.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import logging
import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import Denied
from ._common import (MAX_BODY, TOKEN_ENV, TOKEN_FILE_ENV, is_loopback, name_ok,
                      read_token_file, token_from_env, token_problem)

__all__ = ['StoreServer', 'etag_of', 'main']

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8787
MIN_TOKEN = 16
LOCK_FILE = '.mendwright-server.lock'   # a name no object can have: it starts with a dot
_STRIPES = 64
_TRIES = 8                              # tries of a read, replace or remove Windows refuses


def etag_of(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


def _matches(header, etag, *, weak):
    """Whether an If-Match (weak=False) or If-None-Match (weak=True) header matches etag,
    the current object's ETag, or None when there is no object."""
    if etag is None:
        return False
    header = header.strip()
    if header == '*':
        return True
    for tag in header.split(','):
        tag = tag.strip()
        if weak:
            if tag.removeprefix('W/') == etag:
                return True
        elif tag == etag:
            return True
    return False


def _retrying(action, *args):
    """action(*args), tried again for a while when Windows refuses it with PermissionError
    (another program, such as a virus scanner, has the file open for a moment)."""
    delay = 0.01
    for attempt in range(_TRIES):
        try:
            return action(*args)
        except PermissionError:
            if attempt == _TRIES - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.2)
    return None  # not reached


def _read(path):
    """The file's bytes, or None when there is no file (or a folder) at path."""
    def read():
        try:
            with open(path, 'rb') as fh:
                return fh.read()
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except PermissionError:
            if os.path.isdir(path):     # Windows refuses to open a folder this way
                return None
            raise
    return _retrying(read)


def _write(path, body):
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.', suffix='.tmp', dir=folder)    # 0600 on POSIX
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        _retrying(os.replace, tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _remove(path):
    def remove():
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _retrying(remove)


def _lock_folder(root):
    """An open file holding an OS lock on root's lock file, which the OS lets go when the
    process ends. Raises ValueError when another server holds it."""
    fd = os.open(os.path.join(root, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ValueError('another store server is serving ' + root + ': run one per folder') from None
    return fd


def _unlock_folder(fd):
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        os.close(fd)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'mendwright-store'
    timeout = 30            # seconds a connection may sit idle, or trickle a request

    def version_string(self):
        return self.server_version      # no Python version in the Server header

    def handle_one_request(self):
        self._name = None               # a kept-alive connection serves several requests
        self._unread = False
        super().handle_one_request()

    def do_GET(self):
        self._serve('GET')

    def do_HEAD(self):
        self._serve('HEAD')

    def do_PUT(self):
        self._serve('PUT')

    def do_DELETE(self):
        self._serve('DELETE')

    # --- logging: method, name and status only ---------------------------------------------

    def log_request(self, code='-', size='-'):
        logger.info('%s %s %s', self.command or '-', getattr(self, '_name', None) or '-', code)

    def log_error(self, format, *args):
        # http.server's own errors (a bad request line, headers too long, a timeout). Their
        # text can quote the request line, which can hold anything a client sent.
        if format.startswith('code') and args:
            logger.warning('%s: refused a request it could not read (HTTP %s)',
                           self.client_address[0], args[0])
        else:
            logger.warning('%s: a request failed or timed out', self.client_address[0])

    def log_message(self, format, *args):
        logger.debug('http.server: %s', format)

    # --- requests ---------------------------------------------------------------------------

    def _serve(self, method):
        store = self.server.store
        path = self.path
        if path.startswith('/') and name_ok(path[1:]):
            self._name = path[1:]
        # A body this request leaves unread would be taken for the next request on the
        # connection, so a reply that does not read it closes the connection.
        length = (self.headers.get('Content-Length') or '').strip()
        self._unread = bool(self.headers.get('Transfer-Encoding')) or length not in ('', '0')
        try:
            denied = store.refusal(self.headers.get('Authorization'))
            if denied:
                logger.info('%s %s refused: %s', method, self._name or '-', denied)
                self._reply(401, headers={'WWW-Authenticate': 'Bearer realm="mendwright-store"'})
                return
            file = store.file_of(self._name) if self._name else None
            if file is None:
                self._reply(400, b'not a store object name\n')
            elif method in ('GET', 'HEAD'):
                self._get(file, head=method == 'HEAD')
            elif method == 'PUT':
                self._put(file)
            else:
                self._delete(file)
        except (ConnectionError, TimeoutError):
            self.close_connection = True        # the client went away or stalled
        except Exception:
            logger.exception('%s %s failed', method, self._name or '-')
            try:
                self._reply(500, close=True)
            except OSError:
                self.close_connection = True

    def _get(self, file, *, head):
        store = self.server.store
        with store.lock_for(self._name):
            body = _read(file)
        if body is None:
            self._reply(404, head=head)
            return
        etag = etag_of(body)
        if_none_match = self.headers.get('If-None-Match')
        if if_none_match is not None and _matches(if_none_match, etag, weak=True):
            self._reply(304, etag=etag)
            return
        kind = 'application/json' if self._name.endswith('.json') else 'application/octet-stream'
        self._reply(200, body, etag=etag, head=head, headers={'Content-Type': kind})

    def _put(self, file):
        store = self.server.store
        length = (self.headers.get('Content-Length') or '').strip()
        if self.headers.get('Transfer-Encoding') or not length.isdigit():
            self._reply(411)
            return
        if int(length) > store.max_body:
            self._reply(413)                # the body is never read
            return
        body = self.rfile.read(int(length))
        if len(body) != int(length):
            self.close_connection = True    # the client went away mid-body: nothing written
            return
        self._unread = False
        if_match = self.headers.get('If-Match')
        if_none_match = self.headers.get('If-None-Match')
        if os.path.isdir(file):
            self._reply(400, b'a folder has this name\n')
            return
        with store.lock_for(self._name):
            current = _read(file)
            etag = None if current is None else etag_of(current)
            if if_match is not None and not _matches(if_match, etag, weak=False):
                status = 412
            elif if_none_match is not None and _matches(if_none_match, etag, weak=True):
                status = 412
            else:
                try:
                    _write(file, body)
                except (FileExistsError, FileNotFoundError, NotADirectoryError):
                    status = 400            # a folder on the way is an object
                else:
                    status = 201 if current is None else 200
        if status == 400:
            self._reply(400, b'an object has the name of a folder on the way\n')
        else:
            self._reply(status, etag=etag_of(body) if status != 412 else None)

    def _delete(self, file):
        store = self.server.store
        if_match = self.headers.get('If-Match')
        with store.lock_for(self._name):
            current = _read(file)
            if current is None:
                status = 404
            elif if_match is not None and not _matches(if_match, etag_of(current), weak=False):
                status = 412
            else:
                _remove(file)
                status = 204
        self._reply(status)

    def _reply(self, status, body=b'', *, etag=None, head=False, close=False, headers=None):
        self.send_response(status)
        if status not in (204, 304):
            self.send_header('Content-Length', str(len(body)))
        if etag:
            self.send_header('ETag', etag)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if close or self._unread:
            self.send_header('Connection', 'close')
            self.close_connection = True
        self.end_headers()
        if body and not head and status not in (204, 304):
            self.wfile.write(body)


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128    # Windows refuses, rather than queues, past a full backlog
    allow_reuse_address = os.name != 'nt'   # on Windows it would let another program share the port

    def __init__(self, address, store):
        self.address_family = socket.AF_INET6 if ':' in address[0] else socket.AF_INET
        self.store = store
        super().__init__(address, _Handler)

    def server_bind(self):
        # HTTPServer.server_bind looks the host up with getfqdn(), which can wait on DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


class StoreServer:
    """The server for one folder. start() serves in a thread (and `with` does), serve_forever()
    in this one; close() stops it. url is where it listens."""

    def __init__(self, root, *, host='127.0.0.1', port=0, token=None, no_auth=False,
                 max_body=MAX_BODY):
        if no_auth and token:
            raise ValueError('give a token or no_auth, not both')
        if not no_auth and not token:
            raise ValueError(
                'the store server needs a token: --token-file FILE, ' + TOKEN_FILE_ENV + ' or '
                + TOKEN_ENV + ' (or --no-auth, to serve on localhost without one)')
        if no_auth and not is_loopback(host):
            raise ValueError('--no-auth serves on localhost only (127.0.0.1, localhost or ::1)')
        if token:
            problem = token_problem(token)
            if problem:
                raise ValueError('the token cannot be used: ' + problem)
            if len(token) < MIN_TOKEN:
                raise ValueError(
                    'the token is shorter than %d characters: use a long random one, such as '
                    'python -c "import secrets; print(secrets.token_urlsafe(32))"' % MIN_TOKEN)
        if not isinstance(max_body, int) or max_body < 1:
            raise ValueError('max_body takes a number of bytes above 0')
        os.makedirs(root, mode=0o700, exist_ok=True)
        self.root = os.path.realpath(root)
        self.max_body = max_body
        self._token = token.encode('ascii') if token else None
        self._locks = [threading.Lock() for _ in range(_STRIPES)]
        self._thread = None
        self._lock_fd = _lock_folder(self.root)
        try:
            self._httpd = _HTTPServer((host, port), self)
        except BaseException:
            _unlock_folder(self._lock_fd)
            raise

    @property
    def url(self):
        host, port = self._httpd.server_address[:2]
        return 'http://%s:%d/' % ('[' + host + ']' if ':' in host else host, port)

    def refusal(self, header):
        """Why a request with this Authorization header is refused, or None."""
        if self._token is None:
            return None
        if not header:
            return 'no token'
        scheme, _, value = header.strip().partition(' ')
        if scheme.lower() != 'bearer':
            return 'not a bearer token'
        try:
            given = value.strip().encode('latin-1')
        except UnicodeEncodeError:
            return 'wrong token'
        return None if hmac.compare_digest(given, self._token) else 'wrong token'

    def file_of(self, name):
        """The file for an object's name, or None when it would be outside the folder, for
        example through a link someone made inside it. Only the file's folder is resolved:
        the server never replaces or removes a folder, while on Windows a file being deleted
        resolves to somewhere under C:\\$Extend\\$Deleted."""
        *folders, leaf = name.split('/')
        folder = os.path.join(self.root, *folders)
        try:
            inside = os.path.commonpath([self.root, os.path.realpath(folder)]) == self.root
        except ValueError:      # another drive
            return None
        path = os.path.join(folder, leaf)
        return path if inside and not os.path.islink(path) else None

    def lock_for(self, name):
        return self._locks[hash(name) % _STRIPES]

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={'poll_interval': 0.05},
                                        name='mendwright-store-server', daemon=True)
        self._thread.start()
        return self

    def serve_forever(self):
        self._httpd.serve_forever()

    def close(self):
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(5)
            self._thread = None
        self._httpd.server_close()
        if self._lock_fd is not None:
            _unlock_folder(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m mendwright.backends.server',
        description='Serve a mendwright store from a folder, over HTTP with ETags.')
    parser.add_argument('--root', required=True, help='the folder the objects are kept in')
    parser.add_argument('--host', default='127.0.0.1', help='the address to listen on (default 127.0.0.1)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help='the port to listen on (default %d; 0 picks a free one)' % DEFAULT_PORT)
    parser.add_argument('--token-file', help='a file holding the bearer token (else ' + TOKEN_FILE_ENV
                        + ' or ' + TOKEN_ENV + ')')
    parser.add_argument('--no-auth', action='store_true',
                        help='serve without a token, on localhost only')
    parser.add_argument('--max-body', type=int, default=MAX_BODY,
                        help='the largest object in bytes (default %d)' % MAX_BODY)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        if args.no_auth:
            token = None
        elif args.token_file:
            token = read_token_file(args.token_file, '--token-file')
        else:
            token = token_from_env()
        server = StoreServer(args.root, host=args.host, port=args.port, token=token,
                             no_auth=args.no_auth, max_body=args.max_body)
    except (ValueError, Denied, OSError) as exc:
        print('mendwright store server: ' + str(exc), file=sys.stderr)
        return 2
    if not is_loopback(args.host):
        logger.warning('serving plain HTTP on %s: put a TLS-terminating proxy in front, since '
                       'mendwright connects to other hosts over https only', args.host)
    print('mendwright store server: serving ' + server.root + ' at ' + server.url, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
