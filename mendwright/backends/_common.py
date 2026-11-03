"""Rules the remote backends share: what an object name may be, what put() takes, which
URLs and hosts are allowed, and where an HTTP store's token comes from.

A name is one or more segments joined by '/'. A segment has lower-case letters, digits,
'.', '_' and '-', starts with a letter or a digit and doesn't end with a dot, so a name
can't climb out of its folder ('..'), start at a root or a drive, or hold a backslash.
Lower case keeps one name one object on a disk that ignores case, and the Windows device
names (con, nul, com1, ...) are refused on every system, so a store moves between them.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote, urlsplit

from . import Denied

DEFAULT_TIMEOUT = 10.0          # seconds to connect, and for each read of an answer
MAX_BODY = 32 * 1024 * 1024     # bytes in one object (a 10,000-key store is about 7 MB)

TOKEN_ENV = 'MENDWRIGHT_STORE_TOKEN'
TOKEN_FILE_ENV = 'MENDWRIGHT_STORE_TOKEN_FILE'
MAX_TOKEN = 4096
MAX_TOKEN_FILE = 16 * 1024

# Plain http:// is allowed to these hosts only. Anything else must be https://.
LOOPBACK_HOSTS = ('localhost', '127.0.0.1', '::1')

MAX_NAME = 512
MAX_DEPTH = 8
MAX_VERSION = 1024
_SEGMENT = re.compile(r'[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9_-])?')
_BUCKET = re.compile(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]')
_PREFIX = re.compile(r"[A-Za-z0-9!_.*'()-]+")    # S3's safe characters, per segment
_DEVICES = frozenset(['con', 'prn', 'aux', 'nul'] + ['com%d' % n for n in range(10)]
                     + ['lpt%d' % n for n in range(10)])

# Query parameters that carry a credential: a presigned S3 or GCS URL, an Azure SAS, a
# token. A store URL takes no query string at all; these names only make the message
# clearer. The message names the parameter, never its value.
SECRET_PARAMS = (
    'X-Amz-Signature', 'X-Amz-Credential', 'X-Amz-Security-Token', 'AWSAccessKeyId',
    'Signature', 'X-Goog-Signature', 'X-Goog-Credential', 'GoogleAccessId', 'sig', 'token',
    'access_token', 'id_token', 'api_key', 'apikey', 'key', 'password', 'secret', 'auth',
)


def is_loopback(host) -> bool:
    return isinstance(host, str) and host.lower() in LOOPBACK_HOSTS


def name_ok(name) -> bool:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        return False
    segments = name.split('/')
    if len(segments) > MAX_DEPTH:
        return False
    return all(_SEGMENT.fullmatch(s) and s.split('.', 1)[0] not in _DEVICES for s in segments)


def check_name(name) -> str:
    if not isinstance(name, str):
        raise TypeError('an object name is a str, not ' + type(name).__name__)
    if not name_ok(name):
        raise ValueError(
            repr(name[:80]) + ' is not a store object name: use segments of lower-case letters, '
            "digits, '.', '_' and '-' joined by '/', such as keys/<hex>.json")
    return name


def check_bucket(bucket) -> str:
    if not isinstance(bucket, str):
        raise TypeError('an S3 bucket name is a str, not ' + type(bucket).__name__)
    if not _BUCKET.fullmatch(bucket) or '..' in bucket:
        raise ValueError(repr(bucket[:80]) + ' is not an S3 bucket name: 3 to 63 lower-case '
                         'letters, digits, dots and hyphens, starting and ending with a letter '
                         'or a digit')
    return bucket


def check_prefix(prefix) -> str:
    """The prefix as S3 keys start with it: '' or segments, each ending in '/'."""
    if not isinstance(prefix, str):
        raise TypeError('an S3 prefix is a str, not ' + type(prefix).__name__)
    prefix = prefix.strip('/')
    if not prefix:
        return ''
    segments = prefix.split('/')
    if len(prefix) > MAX_NAME or not all(_PREFIX.fullmatch(s) and s not in ('.', '..') for s in segments):
        raise ValueError(repr(prefix[:80]) + " is not a store prefix: use letters, digits and "
                         "!_.*'()- in segments joined by '/'")
    return prefix + '/'


def check_put(body, if_match, create_only) -> bytes:
    """The checks MemoryBackend makes, with its messages, then the version's."""
    if if_match is not None and create_only:
        raise ValueError('put() takes if_match= or create_only=True, not both')
    if not isinstance(body, (bytes, bytearray)):
        raise TypeError('put() takes the body as bytes')
    check_version(if_match, 'if_match')
    return bytes(body)


def check_version(version, what) -> None:
    """A version goes back to the server in a header: refuse what a backend never gives."""
    if version is None:
        return
    if not isinstance(version, str):
        raise TypeError(what + '= takes a version (a str), not ' + type(version).__name__)
    if not version or len(version) > MAX_VERSION or not all(' ' <= ch <= '~' for ch in version):
        raise ValueError(what + '= takes a version a get() or put() gave, not ' + repr(version[:80]))


def token_problem(token) -> str | None:
    """Why token can't be a bearer token, or None. The reason never quotes the token."""
    if not token:
        return 'it is empty'
    if len(token) > MAX_TOKEN:
        return 'it is longer than %d characters' % MAX_TOKEN
    if not all('!' <= ch <= '~' for ch in token):
        return ('it has a character a header cannot carry (a space, a control character or a '
                'letter outside ASCII)')
    return None


def split_url(url):
    """(urlsplit(url), its port). Raises ValueError without quoting the URL. The error is
    raised outside the except clause, so it has no __context__ that could quote it."""
    try:
        parts = urlsplit(url)
        port = parts.port       # a port that is not a number raises here
    except ValueError:
        parts = None
    if parts is None:
        raise ValueError('the store URL is not a valid URL (check its host and port)')
    return parts, port


def read_token_file(path, source) -> str:
    """The token in the file at path, without the whitespace around it. Raises Denied, whose
    message names source and the path, never the file's content. Errors are raised outside
    the except clauses, so none has a __context__ that could hold the file's bytes."""
    where = source + ' (' + os.fspath(path) + ')'
    problem = data = None
    try:
        with open(path, 'rb') as fh:
            data = fh.read(MAX_TOKEN_FILE + 1)
    except FileNotFoundError:
        problem = 'the token file does not exist'
    except OSError as exc:
        problem = 'the token file cannot be read (' + type(exc).__name__ + ')'
    if problem is None and len(data) > MAX_TOKEN_FILE:
        problem = 'the token file is larger than a token'
    text = None
    if problem is None:
        try:
            # PowerShell 5 writes UTF-16 with > and Out-File; Notepad may add a UTF-8 BOM.
            utf16 = data[:2] in (b'\xff\xfe', b'\xfe\xff')
            text = data.decode('utf-16') if utf16 else data.decode('utf-8-sig')
        except UnicodeDecodeError:
            problem = 'the token file is not UTF-8 text'
    if problem is not None:
        raise Denied(where + ': ' + problem)
    token = text.strip()
    problem = token_problem(token)
    if problem:
        raise Denied(where + ': the token in it cannot be used: ' + problem)
    return token


def token_from_env(environ=None) -> str | None:
    """The bearer token from MENDWRIGHT_STORE_TOKEN, or from the file MENDWRIGHT_STORE_TOKEN_FILE
    names, or None when neither is set (an empty value counts as not set). Setting both is
    an error rather than a guess. Problems raise Denied without quoting the token."""
    environ = os.environ if environ is None else environ
    value = environ.get(TOKEN_ENV) or None
    path = environ.get(TOKEN_FILE_ENV) or None
    if value and path:
        raise Denied('set ' + TOKEN_ENV + ' or ' + TOKEN_FILE_ENV + ', not both')
    if path:
        return read_token_file(path, TOKEN_FILE_ENV)
    if value:
        token = value.strip()
        problem = token_problem(token)
        if problem:
            raise Denied(TOKEN_ENV + ' cannot be used as a token: ' + problem)
        return token
    return None


def refuse_secrets(url, parts) -> None:
    """Raise ValueError when a store URL carries a credential, or anything a store URL never
    needs that could hide one: user info, a query string, a fragment, a space, a control
    character or a backslash. The message never quotes the URL."""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F or ch == '\\' for ch in url):
        raise ValueError('the store URL has a space, a control character or a backslash in it')
    if '@' in parts.netloc:
        raise ValueError(
            'the store URL has a user name or password in it (user:password@). Take it out: '
            'S3 credentials come from the AWS credential chain (AWS_PROFILE, the environment, '
            'SSO or a role), and an HTTP store\'s token from ' + TOKEN_ENV + ' or '
            + TOKEN_FILE_ENV)
    if parts.query:
        names = {unquote(piece.split('=', 1)[0]).strip().lower()
                 for piece in re.split('[&;]', parts.query)}
        found = [p for p in SECRET_PARAMS if p.lower() in names]
        if found:
            raise ValueError(
                'the store URL has a credential in its query string (' + ', '.join(found) + '). '
                'A store URL takes no query string: use the folder\'s plain URL, with S3 '
                'credentials from the AWS credential chain and an HTTP store\'s token from '
                + TOKEN_ENV + ' or ' + TOKEN_FILE_ENV)
        raise ValueError('the store URL has a query string (?...): a store URL takes none')
    if parts.fragment:
        raise ValueError('the store URL has a fragment (#...): a store URL takes none')
