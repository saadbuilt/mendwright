"""backend_for(url): the backend a store URL names, or None for a local path.

    s3://bucket/prefix/             S3Backend (needs boto3: pip install "mendwright[s3]")
    https://host/path/              HttpBackend
    http://localhost:8787/path/     HttpBackend; plain http to localhost, 127.0.0.1 or [::1] only
    memory:                         MemoryBackend, a new empty one each time, for tests

A URL names the folder that holds overrides.json and keys/. A URL that ends in
/overrides.json, as MENDWRIGHT_STORE does for a local store, means its folder; one that ends
in any other .json file name is refused, since the store's file is always overrides.json.

A plain path (relative, absolute, ~, or a Windows path such as C:\\store\\overrides.json)
gives None: the caller opens the local store. Any other scheme is refused, and so is a URL
with credentials in it (user:password@, or a query string such as a presigned URL's
X-Amz-Signature, or token=), with a message that never quotes the URL.
"""

from __future__ import annotations

import os
import re

from . import OVERRIDES, MemoryBackend
from ._common import check_bucket, check_prefix, refuse_secrets, split_url

__all__ = ['backend_for', 'store_url', 'is_store_url']

_SCHEME = re.compile(r'([A-Za-z][A-Za-z0-9+.-]*):')


def is_store_url(value):
    """True when value is written as a URL (a scheme of two or more letters and a colon), so
    store_url() and backend_for() take it for one, valid or not; False for a local path."""
    if not isinstance(value, str):
        return False
    found = _SCHEME.match(value)
    return found is not None and len(found.group(1)) > 1


def store_url(url):
    """The store URL as mendwright names the store in messages, normalized as its backend's
    describe() gives it (s3://bucket/prefix/, https://host:port/path/, memory:), or None for a
    local path. Builds no backend and imports no SDK. Raises ValueError for a URL backend_for()
    refuses, without quoting it."""
    if isinstance(url, os.PathLike) or not is_store_url(url):
        return None
    found = _SCHEME.match(url)
    scheme = found.group(1).lower()
    if scheme == 'memory':
        if url[found.end():]:
            raise ValueError('a memory store URL is memory: with nothing after it')
        return 'memory:'
    parts, port = split_url(url)
    refuse_secrets(url, parts)
    if scheme == 's3':
        bucket, prefix = _s3_place(parts, port)
        return 's3://' + bucket + '/' + prefix
    if scheme in ('https', 'http'):
        from .http import folder_url
        return folder_url(parts._replace(path='/' + _folder(parts.path)).geturl())[0]
    raise ValueError(
        'a store URL is s3://bucket/prefix/, https://host/path/ (http:// to localhost only) or '
        'memory:, not ' + scheme + ':. For a local path with a colon in it, start it with ./')


def backend_for(url):
    """The StoreBackend for a store URL, or None when url is a local path. Raises ValueError
    for a URL no backend takes, and ImportError for an s3:// URL without boto3."""
    if isinstance(url, os.PathLike):
        return None
    if not isinstance(url, str):
        raise TypeError('a store URL is a str, not ' + type(url).__name__)
    found = _SCHEME.match(url)
    if found is None or len(found.group(1)) == 1:      # C:\x and C:/x are drive letters
        return None
    scheme = found.group(1).lower()
    if scheme == 'memory':
        if url[found.end():]:
            raise ValueError('a memory store URL is memory: with nothing after it')
        return MemoryBackend()
    parts, port = split_url(url)
    refuse_secrets(url, parts)
    if scheme == 's3':
        bucket, prefix = _s3_place(parts, port)
        from .s3 import S3Backend      # boto3 only for an s3:// URL
        return S3Backend(bucket, prefix)
    if scheme in ('https', 'http'):
        from .http import HttpBackend
        folder = parts._replace(path='/' + _folder(parts.path))
        return HttpBackend(folder.geturl())
    raise ValueError(
        'a store URL is s3://bucket/prefix/, https://host/path/ (http:// to localhost only) or '
        'memory:, not ' + scheme + ':. For a local path with a colon in it, start it with ./')


def _folder(path):
    """The folder path (no leading '/', '' or ending in '/') a URL path names."""
    path = path.lstrip('/')
    if not path or path.endswith('/'):
        return path
    head, _, last = path.rpartition('/')
    if last == OVERRIDES:
        return head + '/' if head else ''
    if last.lower().endswith('.json'):
        raise ValueError(
            'the store URL ends in ' + last + ', a file: a remote store URL names the folder '
            'that holds overrides.json and keys/, such as s3://bucket/prefix/')
    return path + '/'


def _s3_place(parts, port):
    if port is not None or ':' in parts.netloc:
        raise ValueError('an s3:// URL takes no port: point boto3 at another endpoint with '
                         'AWS_ENDPOINT_URL_S3')
    if not parts.netloc:
        raise ValueError('an s3:// URL needs a bucket: s3://bucket/prefix/')
    path = _folder(parts.path)
    if '//' in path:
        raise ValueError('the S3 prefix in the store URL has an empty segment (//)')
    # Checked here too, so a bad URL is refused also where boto3 is not installed.
    return check_bucket(parts.netloc), check_prefix(path)
