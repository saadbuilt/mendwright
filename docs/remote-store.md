# Remote stores

A remote store lets many machines share one set of repairs. Instead of a file on each machine, the store lives in an S3 bucket or behind an HTTPS server, and a heal one machine makes reaches the others: they use it after the [live check](guide.md#what-is-checked-when-a-saved-heal-is-used) on their own page. The machines also share a heal in progress, so a key that breaks for a whole fleet at once costs one model call, and they share the model's declines and the hourly budget of each key.

The backends write with compare-and-swap (a write that names the version it read fails if someone else wrote first), which is what lets workers on many machines change one store at the same time without losing each other's changes.

## Using one

Point mendwright at the store's URL wherever it takes a store path:

```sh
export MENDWRIGHT_STORE=s3://my-bucket/mendwright/
```

```python
heal = mendwright.open(page, store_path='s3://my-bucket/mendwright/')
heal = await mendwright.aio.open(page, store_path='https://store.example.com/team/')
store = mendwright.load_store('seed.json', 's3://my-bucket/mendwright/')   # with a seed file
```

The pytest plugin takes one too: `--mendwright-store s3://my-bucket/mendwright/`, or `mendwright_store = s3://my-bucket/mendwright/` in its ini section. A plain path is a local store, as before.

### URL forms

| Store URL | Where the store is | Needs |
|---|---|---|
| `s3://bucket/prefix/` | S3, under the prefix | `pip install "mendwright[s3]"` (boto3 1.35.69 or later) |
| `https://host/path/` | an HTTPS server that speaks [the protocol below](#the-protocol), such as the [reference server](#the-reference-server) | nothing extra |
| `http://localhost:8787/path/` | plain HTTP, to `localhost`, `127.0.0.1` or `[::1]` only | nothing extra |
| `memory:` | a new, empty store in this process, for tests | nothing extra |
| a path | the local file, as before | nothing extra |

- **A URL names a folder:** the folder that holds `overrides.json` and `keys/`. A URL that ends in `/overrides.json` means its folder, so `s3://bucket/team/overrides.json` is the same store as `s3://bucket/team/`. A URL that ends in another `.json` name is refused, because the store's file is always `overrides.json`.
- **Credentials never go in the URL.** A URL with `user:password@`, or with a query string such as a presigned URL's `X-Amz-Signature` or a `token=`, is refused, and so is any query string or fragment. The error never quotes the URL.
- **Other schemes are refused:** `file:`, `ftp:`, `gs:` and the rest. For a local path that has a colon in it, start it with `./`.
- **`memory:` is a new store each time it is opened**, so two Healers opened with it don't share one. In tests, share one with `load_store(None, MemoryBackend())`: `load_store()` takes a backend object too.

Under the folder, the store has two kinds of object:

- `overrides.json`: the same file a local store has, so Python and TypeScript read it unchanged.
- `keys/<hash>.json`: one small coordination record per key, holding a lease, recent attempts and a cached decline. The hash is the first 32 hex digits of the key's SHA-256.

### What stays on this machine

- **The journal:** `journal.jsonl` goes to the folder `MENDWRIGHT_JOURNAL_DIR` names, else `./.mendwright` in the current folder, which then holds only the journal. Set the variable where the current folder can't be written, such as `/tmp` on AWS Lambda.
- **Seed files and `destructive_keys`:** they stay local and still win. A key your seed file or `destructive_keys=` marks destructive is never healed, whatever the store says.
- **The rate limit** is per store URL in each process, as it is per file for a local store, and the per-key cap counts the attempts of every machine on the store together.

## How workers share it

- **Reads.** A worker reads `overrides.json` when it opens. After that it asks again with `If-None-Match`, so an unchanged store costs a 304 and no body. The workers of one process share one copy per version, and the looks a step makes while it keeps missing, about once a second, share one request per second in each process. Before it heals, a worker always asks the store again.
- **Writes.** A worker reads, merges with the rules of a local store (a quarantine wins, then an acceptance; `destructive: true` wins; the newest success wins), and writes with `If-Match`, or `If-None-Match: *` for a store that has no `overrides.json` yet. When another writer wins, it reads, merges and tries again, up to 5 times with a pause of up to 200 ms; after that the change waits in memory and goes with the worker's next write. A change another worker already saved isn't written again, and the workers of a process take turns to write.
- **Coordination records.** The lease of a heal in progress, the model attempts of the last hour and the reused declines are in `keys/<hash>.json`, written with the same compare-and-swap. A worker waiting for another machine's heal looks at the record four times a second, at most 60 seconds, while that machine renews the lease every 5 seconds. A lease nobody renewed for 15 seconds belongs to a machine that died, and one waiting machine takes it over.
- **Clocks.** A lease and a decline are judged by each machine's own clock, so keep the clocks in sync (NTP, or the Amazon Time Sync Service on EC2). A machine whose clock runs more than 10 seconds ahead of the leader's can take its lease for a dead one and start a second heal: one more model call, never a wrong heal, since every heal is checked live where it is used.
- **Writes are all conditional.** Every write mendwright makes, to `overrides.json` or to a record, carries `If-Match` or `If-None-Match`, and it deletes nothing, so the [bucket policy below](#bucket-settings) that refuses unconditional writes can be on.

What it costs: once its keys are saved, a process makes at most one conditional GET of `overrides.json` a second, and only while its steps use saved heals or keep missing; a store that doesn't change answers each with a 304. Saving what the store doesn't have yet (a new key's selector and role, a first success, a first page) costs a read and a write, unless another worker saved it first, and a heal costs about a dozen small requests in all: the record's lease and budget, the heal, its first success and its page. Changes to what the store has, such as the order of the last successes, are written at most every 5 minutes per key and process.

## When the store fails

mendwright never takes a store it cannot read for an empty one: the next write would replace it and lose every saved heal, and a heal would not know the quarantines and destructive flags saved there.

- **When a worker opens.** A store that can't be reached, refuses the credentials, answers with any other error, or holds an `overrides.json` that isn't a selector file, is not read. The worker logs one WARNING that names the store (never its credentials) and journals `store.unavailable` with `"op": "open"`. Selectors in your code and seed files still resolve, but every heal is declined with the code `store` and the reason, until a later look reads the store: then the worker takes in all of it and heals again. A missing `overrides.json` is an empty store.
- **While workers run.** Reads keep the copy they have. A change that can't be written, a heal included, is used in memory and waits for a later write, or for `SelectorStore.flush()`. After a failure, the workers of the process don't ask the store again for 1 second, then 2, 4 and so on up to 60 seconds, each wait up to a fifth shorter at random so that workers don't all ask at once, so an outage costs one timeout per wait rather than one per step. Reads and writes wait apart, so a store that refuses writes is still read. The journal gets `store.unavailable` when an outage starts, with `op` (`read` or `write`), `error`, `reason` and `retry_in`, and `store.available` when it ends, with `after`, once each per process.
- **Any error.** Network trouble and timeouts, refused credentials, and every other answer a backend gives as an error (a redirect, a 400, a weak ETag) count the same.

What waits in memory when the process exits is lost, as for a local store whose write failed; the soft changes (the order of successes, page lists, fingerprints) are tried once more at exit.

## Read-only workers

Production workers can use the heals of a shared store and never write it: set `MENDWRIGHT_STORE_READ_ONLY=1` (or pass `writes=False` to `load_store()`).

- **What it reads:** the store and its records, as any worker: it uses the heals other workers save, after the live check, stops using one another worker quarantined, reuses their declines, and waits for a heal another machine is making of its page.
- **What it never writes:** `overrides.json` and `keys/`. Its own heals and quarantines stay in its memory, for its process only, and its journal says so: `heal.applied`, `heal.quarantine` and `heal.held` get `"saved": false`, and the heal's log line says it was kept in memory for this process. It heals without a lease and counts its model attempts in its own process.
- **IAM:** such a worker needs only `s3:GetObject` and the `ListBucket` statement of the [policy below](#a-minimal-iam-policy).

It works on a local store too, such as a file on a read-only volume.

## The command line

`mendwright report`, `fix`, `check`, `lists` and `notify` don't read a remote store yet. Given a store URL, by `--store` or `MENDWRIGHT_STORE`, each says so in one line, and how to download the store next to this machine's journal and run the command on the copy:

```sh
aws s3 cp s3://my-bucket/mendwright/overrides.json .mendwright/overrides.json
mendwright report --store .mendwright/overrides.json
```

For an HTTPS store, `curl -fsS -o .mendwright/overrides.json -H "Authorization: Bearer $MENDWRIGHT_STORE_TOKEN" https://store.example.com/team/overrides.json` does the same. `mendwright lists accept` and `reject` can't change a remote store yet. Don't upload an edited copy over a store that workers write: their changes since the download would be lost.

`mendwright doctor` names a remote store, the folder of this machine's journal, and whether the worker is read-only, and never contacts the store.

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
  - TLS is verified. To trust a private CA, point `SSL_CERT_FILE` at a bundle that holds it.
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

A **read-only** role, for production workers that adopt repairs but must never write (see [Read-only workers](#read-only-workers)), needs only `s3:GetObject` on `PREFIX/*`, the ListBucket statement, and `kms:Decrypt` if the bucket uses a customer managed key.

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

- **Every write mendwright makes is conditional** (see [How workers share it](#how-workers-share-it)). Turn the policy on once any other writer of the prefix is too: S3 refuses an unconditional write with 403, which mendwright reports as Denied.
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
- **Anyone who can write to the store can change the selectors every worker uses.** Give write access only to the workers that heal, and read-only access to the rest ([read-only workers](#read-only-workers)). A selector from the store is still [checked live](guide.md#what-is-checked-when-a-saved-heal-is-used) on each page where it is used.
- **The journal stays on each machine** ([what stays on this machine](#what-stays-on-this-machine)), with what a local store's journal holds.

## Checking a store

`tests/test_backends_contract.py` holds every backend to the contract in `mendwright/backends/__init__.py`:

- conditional reads, writes and deletes;
- 16 workers incrementing a counter without losing an update;
- racing creates with exactly one winner;
- racing deletes and writes.

It runs against memory, the reference server and S3 under moto. To check another store, add an entry to `BACKENDS` in that file.

## Limits

- **The command line** doesn't read a remote store yet: download it first (see [The command line](#the-command-line)). The GitHub Action and its cache work on a local store only.
- **The TypeScript package** in `js/` reads and writes a local store only.
- **The async healer** makes each request to a remote store in a worker thread: its reads and writes, and the key record's lease, which a worker waiting for another machine's heal reads four times a second for up to 60 seconds. The event loop goes on meanwhile, but the step waits for each request, up to its timeout while the store doesn't answer. What a list lookup saves goes in a write per change there, where a local store saves it in one.
- **Clocks:** a lease is judged by each machine's clock (see [How workers share it](#how-workers-share-it)).
- **One object:** the whole store is one `overrides.json`, read once per change and written whole. A store of 10,000 keys is about 7 MB.
