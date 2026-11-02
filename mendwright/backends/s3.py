"""S3Backend: a store under a prefix of an S3 bucket, using S3's conditional requests.
It needs boto3: pip install "mendwright[s3]".

- get(): GetObject, with IfNoneMatch=<version> when if_none_match= is given. S3 answers an
  unchanged object with 304, which botocore raises as a ClientError (code '304').
- put(): PutObject, with IfMatch=<version> for if_match=, or IfNoneMatch='*' for
  create_only=True. S3 answers a failed precondition with 412 PreconditionFailed, a race
  with another conditional write with 409 ConditionalRequestConflict, and an If-Match on a
  missing object with 404 NoSuchKey: each is a Conflict.
- delete(): DeleteObject, with IfMatch=<version> for if_match=. 404 NoSuchKey means already
  gone, which is not an error. On 409, one read tells "someone else deleted it first" (done)
  from "someone else wrote it" (Conflict).

Versions are S3's ETags, kept exactly as S3 gives them (quoted). For an object written in
one piece without SSE-KMS the ETag is the MD5 of the body, so the same body has the same
version.

Credentials come only from boto3's default chain: environment variables, AWS_PROFILE and the
shared config files, SSO, web identity, or a container or instance role. No key is taken in
code or in a URL. The region comes from AWS_REGION, AWS_DEFAULT_REGION or the config file,
and AWS_ENDPOINT_URL_S3 points boto3 at an S3-compatible store, which must honour If-Match
and If-None-Match on PUT and If-Match on DELETE. botocore's retries are off, since the
contract leaves retrying a write to the caller, who merges first.

Errors: 412 and 409 raise Conflict; 401, 403 (AccessDenied and the like), missing or refused
credentials and NoSuchBucket raise Denied; SlowDown and other throttling, 5xx, timeouts and
connection errors raise Unavailable; anything else raises BackendError. A message has the
error code and status S3 gave, never its text, which can quote a signature or a key id.
"""

from __future__ import annotations

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError, HTTPClientError, IncompleteReadError
    from botocore.exceptions import ConnectionError as BotoConnectionError
except ImportError as exc:
    raise ImportError(
        'mendwright.backends.s3 needs boto3, which is not installed. Install it with: '
        'pip install "mendwright[s3]"') from exc

import logging

from .. import __version__
from . import BackendError, Conflict, Denied, Read, Unavailable
from ._common import (MAX_BODY, check_bucket, check_name, check_prefix, check_put,
                      check_version)

__all__ = ['S3Backend']

logger = logging.getLogger(__name__)

DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
_CHUNK = 64 * 1024

_DENIED_CODES = frozenset([
    'AccessDenied', 'AllAccessDisabled', 'AccountProblem', 'InvalidAccessKeyId',
    'SignatureDoesNotMatch', 'ExpiredToken', 'InvalidToken', 'TokenRefreshRequired',
    'InvalidSecurity', 'NotSignedUp', 'RequestTimeTooSkewed', 'AuthorizationHeaderMalformed',
])
_BUSY_CODES = frozenset([
    'SlowDown', 'ServiceUnavailable', 'InternalError', 'RequestTimeout', 'Throttling',
    'ThrottlingException', 'RequestLimitExceeded', 'TooManyRequests', 'TooManyRequestsException',
    'BandwidthLimitExceeded', 'PriorRequestNotComplete',
])
_CONFLICT_CODES = frozenset(['PreconditionFailed', 'ConditionalRequestConflict', 'OperationAborted'])
# botocore's credential errors, by name, since some of them are not in every version.
_CREDENTIAL_ERRORS = frozenset([
    'NoCredentialsError', 'PartialCredentialsError', 'CredentialRetrievalError', 'ProfileNotFound',
    'TokenRetrievalError', 'SSOError', 'SSOTokenLoadError', 'UnauthorizedSSOTokenError',
    'InvalidConfigError',
])


def _attempt(action):
    """(action(), None), or (None, the error) when botocore raised one, or OSError did."""
    try:
        return action(), None
    except (BotoCoreError, ClientError, OSError) as exc:
        return None, exc


def _code(exc):
    """(error code, HTTP status, response headers) of a botocore ClientError."""
    response = getattr(exc, 'response', None) or {}
    meta = response.get('ResponseMetadata') or {}
    code = str((response.get('Error') or {}).get('Code') or '')
    return code, meta.get('HTTPStatusCode'), meta.get('HTTPHeaders') or {}


class S3Backend:
    """A store at s3://bucket/prefix/. See the module docstring."""

    def __init__(self, bucket, prefix='', *, connect_timeout=DEFAULT_CONNECT_TIMEOUT,
                 read_timeout=DEFAULT_READ_TIMEOUT, max_body=MAX_BODY):
        self.bucket = check_bucket(bucket)
        self.prefix = check_prefix(prefix)
        for what, value in (('connect_timeout', connect_timeout), ('read_timeout', read_timeout)):
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not value > 0:
                raise ValueError(what + '= takes a number of seconds above 0')
        if not isinstance(max_body, int) or isinstance(max_body, bool) or max_body < 1:
            raise ValueError('max_body= takes a number of bytes above 0')
        self._max_body = max_body
        self._place = 's3://' + self.bucket + '/' + self.prefix
        config = Config(connect_timeout=connect_timeout, read_timeout=read_timeout,
                        retries={'total_max_attempts': 1, 'mode': 'standard'},
                        user_agent_extra='mendwright/' + __version__)
        # A session of its own: boto3's default session is not safe to share between threads
        # that make clients.
        self._client, error = _attempt(lambda: boto3.Session().client('s3', config=config))
        if error is not None:
            raise Denied(self._place + ': boto3 could not set up an S3 client ('
                         + type(error).__name__ + '): check AWS_PROFILE and the AWS config file')
        _check_conditional_support(self._client)

    def describe(self):
        return self._place

    def __repr__(self):
        return 'S3Backend(' + repr(self._place) + ')'

    # --- the contract ---------------------------------------------------------------------
    #
    # botocore's errors are returned by _attempt, not raised, and mapped after it: an error
    # raised inside an except clause would keep botocore's as its __context__, with S3's own
    # text in it.

    def get(self, name, *, if_none_match=None):
        check_name(name)
        check_version(if_none_match, 'if_none_match')
        request = {'Bucket': self.bucket, 'Key': self.prefix + name}
        if if_none_match is not None:
            request['IfNoneMatch'] = if_none_match
        response, error = _attempt(lambda: self._client.get_object(**request))
        if isinstance(error, ClientError):
            code, status, headers = _code(error)
            if (status == 304 or code in ('304', 'NotModified')) and if_none_match is not None:
                etag = (headers.get('etag') or '').strip()
                return Read(None, etag or if_none_match, True)
            if code == 'NoSuchKey':
                return Read(None, None, False)
            raise self._error(error, 'GetObject', name)
        if error is not None:
            raise self._failure(error, name)
        version = response.get('ETag')
        if not version:
            response['Body'].close()
            raise BackendError(self._at(name) + ': S3 sent no ETag')
        body, error = _attempt(lambda: self._read(name, response))
        if error is not None:
            raise self._failure(error, name)
        if version == if_none_match:
            return Read(None, version, True)
        return Read(body, version, False)

    def put(self, name, body, *, if_match=None, create_only=False):
        body = check_put(body, if_match, create_only)
        check_name(name)
        request = {'Bucket': self.bucket, 'Key': self.prefix + name, 'Body': body,
                   'ContentType': 'application/json' if name.endswith('.json') else 'application/octet-stream'}
        if if_match is not None:
            request['IfMatch'] = if_match
        if create_only:
            request['IfNoneMatch'] = '*'
        response, error = _attempt(lambda: self._client.put_object(**request))
        if isinstance(error, ClientError):
            code, status, _ = _code(error)
            if code in _CONFLICT_CODES or status in (409, 412):
                raise self._conflict(name, if_match, create_only, code)
            if code == 'NoSuchKey' and if_match is not None:
                raise Conflict(self._at(name) + ' no longer exists (NoSuchKey)')
            raise self._error(error, 'PutObject', name)
        if error is not None:
            raise self._failure(error, name)
        version = response.get('ETag')
        if not version:
            raise BackendError(self._at(name) + ': S3 sent no ETag for the write')
        return version

    def delete(self, name, *, if_match=None):
        check_name(name)
        check_version(if_match, 'if_match')
        request = {'Bucket': self.bucket, 'Key': self.prefix + name}
        if if_match is not None:
            request['IfMatch'] = if_match
        _, error = _attempt(lambda: self._client.delete_object(**request))
        if isinstance(error, ClientError):
            code, status, _ = _code(error)
            if code == 'NoSuchBucket':
                raise self._error(error, 'DeleteObject', name)
            if code == 'NoSuchKey':
                return None
            if code == 'PreconditionFailed' or status == 412:
                raise self._conflict(name, if_match, False, code)
            if code in _CONFLICT_CODES or status == 409:
                # S3 answers 409 when another write or delete finished first. One read tells
                # them apart; it is not a retried write.
                if self.get(name).version is None:
                    return None
                raise self._conflict(name, if_match, False, code)
            raise self._error(error, 'DeleteObject', name)
        if error is not None:
            raise self._failure(error, name)
        return None

    def _read(self, name, response):
        stream = response['Body']
        try:
            if (response.get('ContentLength') or 0) > self._max_body:
                raise self._too_big(name)
            chunks, size = [], 0
            while True:
                chunk = stream.read(_CHUNK)     # at the end, botocore checks the length
                if not chunk:
                    return b''.join(chunks)
                size += len(chunk)
                if size > self._max_body:
                    raise self._too_big(name)
                chunks.append(chunk)
        finally:
            stream.close()

    # --- errors -----------------------------------------------------------------------------

    def _at(self, name):
        return name + ' at ' + self._place

    def _too_big(self, name):
        return BackendError(self._at(name) + ': the object is larger than max_body=%d bytes'
                            % self._max_body)

    def _conflict(self, name, if_match, create_only, code):
        if create_only:
            what = 'already exists'
        elif if_match is not None:
            what = 'changed since version ' + if_match
        else:
            what = 'is being changed by another writer'
        return Conflict(self._at(name) + ' ' + what + ' (' + code + ')')

    def _error(self, exc, operation, name):
        code, status, _ = _code(exc)
        said = '%s, HTTP %s' % (code or 'no code', status)
        if code == 'NoSuchBucket':
            return Denied(self._place + ': the bucket ' + self.bucket + ' does not exist '
                          '(NoSuchBucket). Check the bucket name in the store URL.')
        if status in (401, 403) or code in _DENIED_CODES:
            hint = ''
            if operation == 'GetObject':
                hint = (' If the object does not exist yet, S3 answers 403 unless the role may '
                        's3:ListBucket on the bucket (see docs/remote-store.md).')
            return Denied(self._at(name) + ': S3 refused ' + operation + ' (' + said + ').' + hint)
        if code in _BUSY_CODES or status == 429 or (isinstance(status, int) and status >= 500):
            return Unavailable(self._at(name) + ': S3 answered ' + operation + ' with ' + said
                               + ': try again later')
        return BackendError(self._at(name) + ': S3 answered ' + operation + ' with ' + said)

    def _failure(self, exc, name):
        kind = type(exc).__name__
        if any(cls.__name__ in _CREDENTIAL_ERRORS for cls in type(exc).__mro__):
            return Denied(self._place + ': no usable AWS credentials (' + kind + '). mendwright '
                          'takes them only from the AWS credential chain: the environment, '
                          'AWS_PROFILE, SSO, or a container or instance role.')
        if isinstance(exc, (BotoConnectionError, HTTPClientError, IncompleteReadError, OSError)):
            return Unavailable(self._at(name) + ': could not reach S3 (' + kind + ')')
        return BackendError(self._at(name) + ': boto3 failed (' + kind + ')')


def _check_conditional_support(client):
    """Raise ImportError when this botocore cannot send the conditional requests the store
    needs: IfMatch on PutObject came in botocore 1.35.69, on DeleteObject in 1.35.67. An older
    boto3 (the bedrock extra takes 1.34) would refuse them only on the first write."""
    model = client.meta.service_model

    def members(operation):
        return set(model.operation_model(operation).input_shape.members)

    if not ({'IfMatch', 'IfNoneMatch'} <= members('PutObject') and 'IfMatch' in members('DeleteObject')
            and 'IfNoneMatch' in members('GetObject')):
        raise ImportError(
            'mendwright.backends.s3 needs boto3 1.35.69 or later, for S3 conditional writes and '
            'deletes; this is boto3 ' + boto3.__version__ + '. Upgrade it with: '
            'pip install -U "mendwright[s3]"')
