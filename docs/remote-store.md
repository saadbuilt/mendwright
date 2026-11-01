# Remote stores

> **Draft.** The backends on this page exist and pass the contract tests, but `mendwright.open()` doesn't use them yet. The work that wires them in will finish this page, and the guide will link to it then.

A remote store lets many machines share one set of repairs. Instead of a file on each machine, the store lives in an S3 bucket or behind an HTTPS server. Every write is a compare-and-swap, so workers on many machines can change it at the same time without losing each other's changes.

## URL forms

| Store URL | Where the store is | Needs |
|---|---|---|
| `s3://bucket/prefix/` | S3, under the prefix | `pip install "mendwright[s3]"` (boto3 1.35.69 or later) |
| `https://host/path/` | an HTTPS server that speaks [the protocol below](#the-protocol), such as the [reference server](#the-reference-server) | nothing extra |
| `http://localhost:8787/path/` | plain HTTP, to `localhost`, `127.0.0.1` or `[::1]` only | nothing extra |
| `memory:` | a new, empty store in this process, for tests | nothing extra |
| a path | the local file, as today | nothing extra |

- **A URL names a folder:** the folder that holds `overrides.json` and `keys/`. A URL that ends in `/overrides.json` means its folder, so `s3://bucket/team/overrides.json` is the same store as `s3://bucket/team/`. A URL that ends in another `.json` name is refused, because the store's file is always `overrides.json`.
- **Credentials never go in the URL.** A URL with `user:password@`, or with a query string such as a presigned URL's `X-Amz-Signature` or a `token=`, is refused, and so is any query string or fragment. The error never quotes the URL.
- **Other schemes are refused:** `file:`, `ftp:`, `gs:` and the rest. For a local path that has a colon in it, start it with `./`.

Under the folder, the store has two kinds of object:

- `overrides.json`: the same file a local store has, so Python and TypeScript read it unchanged.
- `keys/<hash>.json`: one small coordination record per key, holding a lease, recent attempts and a cached decline. The hash is the first 32 hex digits of the key's SHA-256.

Until the wiring lands, you can use a store from Python:

```python
from mendwright.backends import OVERRIDES
from mendwright.backends.url import backend_for

store = backend_for('s3://my-bucket/mendwright/')   # None for a local path
read = store.get(OVERRIDES)                          # Read(body, version, not_modified)
```

## Credentials

### S3

- **The AWS credential chain only.** boto3's default chain supplies the credentials: environment variables, `AWS_PROFILE` with the shared config and credentials files, IAM Identity Center (SSO), web identity (for example GitHub Actions OIDC, or EKS), or a container or instance role. mendwright takes no key in code or in the URL.
- **Region:** `AWS_REGION`, `AWS_DEFAULT_REGION` or the config file.
- **An S3-compatible store:** set `AWS_ENDPOINT_URL_S3`. The store must honour `If-Match` and `If-None-Match` on PUT, and `If-Match` on DELETE. One that ignores them loses updates without a word, so run the contract tests against it first (see [Checking a store](#checking-a-store)).
- **Timeouts:** 5 seconds to connect and 10 seconds for each read. botocore's retries are off: on a conflict the caller reads again, merges and writes again, because only the caller knows how to merge.

### HTTP

- **A bearer token:** from `MENDWRIGHT_STORE_TOKEN`, or from the file that `MENDWRIGHT_STORE_TOKEN_FILE` names, such as a Docker or Kubernetes secret.
  - Set one of them. Setting both is an error.
  - The file may end in a newline. It may be UTF-8, with or without a BOM, or UTF-16 as PowerShell 5 writes it.
  - Without either, no `Authorization` header is sent.
- **https:// only.** Plain `http://` is allowed to `localhost`, `127.0.0.1` and `[::1]` only.
  - TLS is verified. `SSL_CERT_FILE` adds a private CA.
  - `HTTPS_PROXY` is honoured, except for a loopback URL.
  - No redirect is followed, so the token goes to no other host.
- **Timeouts:** 10 seconds to connect and for each read, with no retries.

## S3

### A minimal IAM policy

This is for the workers that heal, so they read and write. Replace `BUCKET` and `PREFIX`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadAndWriteTheStore",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/*"
    },
    {
      "Sid": "DeleteKeyRecordsOnly",
      "Effect": "Allow",
      "Action": "s3:DeleteObject",
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/keys/*"
    },
    {
      "Sid": "MissingObjectsAre404",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET",
      "Condition": { "StringLike": { "s3:prefix": ["PREFIX/*"] } }
    }
  ]
}
```

What each statement does:

- **GetObject and PutObject** cover the whole prefix: `overrides.json` and the key records.
- **DeleteObject** covers only `keys/`, so a worker can never delete `overrides.json`.
  - A conditional delete (`If-Match` with an ETag) also needs `s3:GetObject` on the object. The first statement grants that.
- **ListBucket** makes a missing object a 404.
  - Without it, S3 answers a GET of an object that doesn't exist yet with 403 instead of 404, so a fresh store reports Denied.
  - AWS documents that rule, but not how a prefix condition on ListBucket changes it. One reported test saw 404 for a missing object under the granted prefix.
  - If a fresh store still reports Denied, drop the `Condition`. The role can then list key names in the whole bucket, which is harmless in a bucket that holds only stores.
- **SSE-KMS with a customer managed key** needs `kms:GenerateDataKey` and `kms:Decrypt` on that key.

A **read-only** role, for production workers that adopt repairs but must never write, needs only `s3:GetObject` on `PREFIX/*`, the ListBucket statement, and `kms:Decrypt` if the bucket uses a customer managed key.

### Bucket settings

- **Block Public Access:** turn on all four settings, for the bucket or the whole account.
- **Default encryption:** SSE-KMS, with an S3 Bucket Key to cut KMS calls.
  - With SSE-KMS, an ETag isn't the MD5 of the body. mendwright never relies on that: a version is opaque to it.
- **Versioning:** turn it on, for rollback and audit.
  - Every write of `overrides.json` becomes a version you can restore.
  - With CloudTrail data events, you can also see who wrote it.
  - Key records change often, so add a lifecycle rule that expires noncurrent versions after a few weeks and removes expired delete markers.
- **Optional: require conditional writes.** This bucket policy refuses a PutObject to the store that has neither `If-Match` nor `If-None-Match`, and a DeleteObject without `If-Match`. Read the caveats before you turn it on.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RequireConditionalWrites",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/*",
      "Condition": { "Null": { "s3:if-match": "true", "s3:if-none-match": "true" } }
    },
    {
      "Sid": "RequireConditionalDeletes",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:DeleteObject",
      "Resource": "arn:aws:s3:::BUCKET/PREFIX/*",
      "Condition": { "Null": { "s3:if-match": "true" } }
    }
  ]
}
```

Caveats:

- **Turn it on only once every writer writes conditionally.** The backend contract allows an unconditional `put()`, and S3 refuses one with 403, which mendwright reports as Denied. Check this when the wiring lands.
- **It applies to every principal, people included.** It also blocks a CopyObject into the prefix.
- **Two keys in one `Null` block must both be absent** for the Deny to apply. So a PutObject is refused only when it has neither header.

### How the S3 backend calls S3

| Call | S3 request | Answer, and what mendwright makes of it |
|---|---|---|
| `get(name, if_none_match=v)` | GetObject with `IfNoneMatch` | 304: not modified. NoSuchKey: missing. |
| `put(name, body, if_match=v)` | PutObject with `IfMatch` | 412 PreconditionFailed, 409 ConditionalRequestConflict, or 404 NoSuchKey (the object is gone): Conflict. |
| `put(name, body, create_only=True)` | PutObject with `IfNoneMatch='*'` | 412 or 409: Conflict. |
| `delete(name, if_match=v)` | DeleteObject with `IfMatch` | NoSuchKey: already gone, which is not an error. 412: Conflict. 409: one read decides. If the object is gone, the delete is done; otherwise it is a Conflict. |

Other answers map as follows:

- **Denied:** 401 and 403 (AccessDenied and the like), missing or refused credentials, and NoSuchBucket.
- **Unavailable:** SlowDown and other throttling, 5xx, timeouts and connection errors.
- **BackendError:** anything else.

A message holds S3's error code and status, never its text.

## HTTP

### The protocol

Any server can hold a store if it does what the reference server does:

- **Where objects are:** the store URL is a folder, and each object is at `<folder URL><name>`, such as `overrides.json` or `keys/<hash>.json`.
- **GET:** 200 with the body and a strong `ETag`. 304 when `If-None-Match` matches. 404 when the object is missing.
- **PUT:** carries `If-Match: <etag>` or `If-None-Match: *`.
  - The server checks the condition and replaces the object in one atomic step.
  - 2xx with the new `ETag`.
  - 412 when the condition fails, also for `If-Match` on a missing object.
- **DELETE:** carries `If-Match: <etag>`.
  - 2xx when deleted.
  - 404 when the object is missing, whatever `If-Match` says: gone is not an error.
  - 412 when `If-Match` doesn't match.
  - A server that follows RFC 9110 answers 412 for a missing object instead; the client then reads once to tell the two apart.
- **Tokens:** 401 or 403 for a missing or refused token.
- **ETags must be strong.** A weak (`W/"..."`) or missing ETag is an error, since `If-Match` compares strong tags only.

A proxy in front of the server must not change any of this:

- It passes `ETag`, `If-Match` and `If-None-Match` through.
- It doesn't compress the store's answers, because compression turns ETags weak.
- It doesn't cache them.

How HttpBackend maps the answers:

| Answer | Result |
|---|---|
| 401, 403 | Denied |
| 409, 412 | Conflict. A 404 to a PUT with `If-Match` is a Conflict too. On a DELETE, one read decides: a missing object means done. |
| 408, 429, 5xx, a timeout, a refused or lost connection, a TLS failure, an answer that isn't HTTP | Unavailable |
| a redirect, 400, 413 and any other answer | BackendError |

### The reference server

The reference server is what HttpBackend is tested against, and a small store for a team without S3. It needs only Python and mendwright.

```sh
python -c "import secrets; print(secrets.token_urlsafe(32))" > /etc/mendwright/store-token
python -m mendwright.backends.server --root /srv/mendwright-store --token-file /etc/mendwright/store-token
```

| Option | What it does |
|---|---|
| `--root DIR` | The folder the objects are kept in. Required. It is made if it doesn't exist. |
| `--host` | The address to listen on. Default `127.0.0.1`. |
| `--port` | The port to listen on. Default 8787; 0 picks a free one. |
| `--token-file FILE` | The bearer token, at least 16 characters. Without it, `MENDWRIGHT_STORE_TOKEN_FILE` or `MENDWRIGHT_STORE_TOKEN`. |
| `--no-auth` | Serve without a token, on localhost only. For trying it out and for tests. |
| `--max-body BYTES` | The largest object. Default 32 MiB; a 10,000-key store is about 7 MB. |

What to know when you run it:

- **There is no `--token` option on purpose:** other users of the machine can read a command line.
- **It speaks plain HTTP**, and mendwright connects to any host but localhost over https only.
  - To serve other machines, run it on `127.0.0.1` behind a TLS-terminating proxy on the same host, such as Caddy:

    ```
    store.example.com {
        reverse_proxy 127.0.0.1:8787
    }
    ```

  - Caddy compresses only when you add `encode`, so leave that out.
  - With nginx, keep `gzip off;` for the store's location: many distributions turn gzip on in `nginx.conf`.
- **One server per folder.** A second server on the same folder refuses to start, because the atomic writes rely on one process's locks.
- **Writes are atomic.** A write goes to a temporary file next to the object, is flushed to disk, and replaces the object under a lock. A reader gets the old body or the new one, never a mix, and of two writers with the same `If-Match`, only one wins.
- **ETags** are the first 32 hex digits of the body's SHA-256.
- **Names** are segments of lower-case letters, digits, `.`, `_` and `-`, joined by `/`. Any other path is answered with 400, so no request reaches outside the folder.
- **The log** has the method, the object's name and the status: never a header, a token or a query string.
- **Backups:** objects are plain files, so copy or snapshot the folder.

What it doesn't do yet: TLS of its own, a read-only token, and a limit on connections.

## What the store holds

The store holds what the local `overrides.json` holds; [SECURITY.md](../SECURITY.md#files-mendwright-writes) lists every field.

- **What's in it:**
  - selectors and their status;
  - each key's role, expected text, destructive flag and `intent=` text;
  - short element fingerprints;
  - page paths, in lower case, with record ids, tokens and email addresses masked as `*`, and never a query, a fragment or user info;
  - the key records: a lease, recent attempts and a cached decline.
- **What's never in it:** page HTML and input values.
- **It can still quote page text.**
  - A healed XPath often anchors on a label.
  - A `text=` you pass is saved as expected text.
  - A fingerprint keeps up to 80 characters of an element's visible text, with runs of 12 to 19 digits masked.
  - Visible text can be personal, so protect the store as you would the local file.
- **Anyone who can write to the store can change the selectors every worker uses.** Give write access only to the workers that heal, and read-only access to the rest.

## Checking a store

`tests/test_backends_contract.py` holds every backend to the contract in `mendwright/backends/__init__.py`:

- conditional reads, writes and deletes;
- 16 workers incrementing a counter without losing an update;
- racing creates with exactly one winner;
- racing deletes and writes.

It runs against memory, the reference server and S3 under moto. To check another store, add an entry to `BACKENDS` in that file.
