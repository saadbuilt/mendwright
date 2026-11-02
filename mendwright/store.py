"""Two-layer selector store: a read-only seed file and a writable override file. Each key
holds an ordered list of candidate selectors, so old selectors are kept and a page that
reverts, or serves an A/B variant, still matches without a model call. The store never
writes to the seed file.

Tool-agnostic: the set of destructive keys is injected (``destructive_keys``) rather
than hardcoded, so the store can serve any tool.

Per-key metadata set with ``set_meta`` (expectedRole, expectedText, destructive, intent,
fingerprint and listFingerprint) is saved in the override file next to the candidates, so the next process
sees it, and so is where the key's element was last found (``set_last_page``: lastPage
and pages; ``set_last_url``: lastUrl, the page's address with its record ids masked, see
mendwright.flow.page_url, and ``set_meta(shadow_hosts=...)``: shadowHosts, the shadow hosts
around it, see mendwright.shadow) and the steps of run_steps() it was found during
(``set_last_step``: steps). A candidate inside an open shadow root has shadowHosts too, and
a heal saved with its get_by twin has getBy (see client.get_by_twin): a get_by_* call that
matched the healed element when the heal was made, which mendwright fix suggests and nothing
runs. A process before 0.11 reads such a file as before and drops getBy when it writes it.
The file stays schema 2; the fields are optional. A key is destructive when the seed says
so, when it is in ``destructive_keys``, or when ``set_meta`` marked it.
``set_meta(destructive=False)`` only clears the last of these. A key of ``destructive_keys``
or of a seed file written as a get_by_* call in another spelling than mendwright's, such as
get_by_role('button', name='Pay'), stands for the call's key (getby.canonical_key).

A list found for heal.locator_all(..., nonempty=True) is saved as a candidate with
"list": true and the status 'proposed' (``propose_list_heal``), with the evidence a person
checks it by (its ``proposal``), and becomes 'accepted' only through ``edit_list_heal``,
which the mendwright lists command runs. Both statuses are inactive: no lookup ever tries
such a row, and mendwright fix writes an accepted one into the code. ``retire_list_heals``
ends a list heal once the selector it replaced matches again.

Only a missing file reads as empty. A seed or override file that exists but is not
valid JSON (a hand edit, a merge conflict) or is not a selector file raises
StoreReadError: reading it as empty would drop its destructive flags and quarantines,
and the next write would replace the file. A read that Windows refuses for a moment
(another process replacing the file) is retried briefly.

A store made with read_only=True (`mendwright check` opens its store that way) never
touches the file after reading it, and snapshot() gives a read-only copy in memory.
"""
import contextlib
import copy
import json
import logging
import os
import tempfile
import threading
import time
import dataclasses
from datetime import datetime, timezone
from pathlib import Path

from . import getby
from .fingerprint import valid as _valid_fingerprint
from .flow import page_url
from .lists import placed as _placed_list, valid as _valid_list_fingerprint
from .roles import infer_role  # noqa: F401  (re-exported: mendwright.store.infer_role)
from .selectors import frame_spelling
from .shadow import saved_hosts, text_hosts

logger = logging.getLogger('mendwright')


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def migrate_schema1(data, destructive_keys=frozenset()):
    """Convert legacy {'selectors': [{key,xpath,stage?,action?,frameXPath?}, ...]}
    into {'schema': 2, 'selectors': {key: {...metadata, candidates: [...]}}}."""
    out = {}
    for row in data.get('selectors', []):
        key = row.get('key')
        xpath = row.get('xpath')
        if not key or not xpath:
            continue
        entry = out.setdefault(key, {
            'expectedRole': infer_role(xpath),
            'expectedText': [],
            'destructive': key in destructive_keys,
            'maxCandidates': 5,
            'stage': row.get('stage'),
            'action': row.get('action'),
            'candidates': [],
        })
        if any(c['xpath'] == xpath for c in entry['candidates']):
            continue
        candidate = {'xpath': xpath, 'source': 'seed', 'addedAt': None,
                     'lastSucceededAt': None, 'status': 'active'}
        if row.get('frameXPath'):
            candidate['frameXPath'] = row['frameXPath']
        entry['candidates'].append(candidate)
    return {'schema': 2, 'selectors': out}


def merge(seed, override):
    """Union candidates per key (override overlays seed by xpath). Non-candidate
    metadata comes from the seed, falling back to the override for override-only
    keys. A list heal of the override that waits (proposed or accepted) and that the seed
    holds too is the seed's row: a person put it in the seed file, as mendwright fix puts
    one in the code (see SelectorStore.set_code_candidates)."""
    out = {'schema': 2, 'selectors': {}}
    keys = set(seed.get('selectors', {})) | set(override.get('selectors', {}))
    for key in keys:
        s = seed.get('selectors', {}).get(key, {})
        o = override.get('selectors', {}).get(key, {})
        base = dict(s) if s else dict(o)
        candidates = {}
        for c in s.get('candidates', []):
            candidates[c['xpath']] = dict(c)
        for c in o.get('candidates', []):
            if c['xpath'] in candidates:
                if _is_list_heal(c):
                    continue
                candidates[c['xpath']].update(c)
            else:
                candidates[c['xpath']] = dict(c)
        base['candidates'] = list(candidates.values())
        out['selectors'][key] = base
    return out


# The fields of a candidate that say where it runs or what it stands for, merged three ways
# (see _merge_place): its iframe, its CSS form and shadow hosts, and its get_by twin (getBy,
# a get_by_* call that matched the healed element when the heal was made, see
# client.get_by_twin). css and getBy are saved for mendwright fix and never run.
_PLACE_FIELDS = ('frameXPath', 'css', 'shadowHosts', 'getBy')


def _place_value(c, field):
    """A candidate's place field as it is compared: '' when it has none, a tuple for its
    shadowHosts."""
    value = c.get(field) or ''
    return tuple(value) if isinstance(value, list) else value


def _place(c):
    """(frameXPath, css, shadowHosts, getBy) of a candidate, '' for each one it doesn't
    have."""
    return tuple(_place_value(c, field) for field in _PLACE_FIELDS)


def _row_state(c):
    """What a seed row is compared on to tell whether this store changed it."""
    return (c.get('status', 'active'), c.get('lastSucceededAt')) + _place(c)


def _merge_place(local, theirs, base):
    """Three-way merge of a candidate's frameXPath, css, shadowHosts and getBy (see
    SelectorStore._absorb). base is what the store last read or wrote, or None when it
    never saw the row."""
    for i, field in enumerate(_PLACE_FIELDS):
        if base is None:
            if field in theirs and field not in local:
                local[field] = copy.deepcopy(theirs[field])
        elif _place_value(local, field) == base[i] and _place_value(theirs, field) != base[i]:
            local[field] = copy.deepcopy(theirs.get(field)) or ''


class StoreReadError(ValueError):
    """A selector file exists but cannot be used: it is not valid JSON, or not a selector
    file. The message names the file and what to do."""


# How often a read that Windows refuses with PermissionError is tried, and the pauses in
# between (the same backoff SelectorStore._write uses for os.replace).
_READ_TRIES = 8


def _empty():
    return {'schema': 2, 'selectors': {}}


def _read_text(path, sleep=None):
    """The file's text, or None when it does not exist. A PermissionError (on Windows,
    another process replacing the file at that moment) is retried with a short backoff
    and raised if it persists. A file that is not UTF-8 raises StoreReadError."""
    return _read_text_versioned(path, sleep)[0]


def file_version(st):
    """The version of a file from its os.stat() or os.fstat() result: (inode, mtime in
    nanoseconds, size). Every write of a store file replaces it (a new inode), so a change
    gives a new version; versions are compared only for equality."""
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def stat_version(path):
    """The version of the file at path (see file_version), or None when it does not exist.
    Other OS errors are raised."""
    try:
        return file_version(os.stat(str(path)))
    except (FileNotFoundError, NotADirectoryError):
        return None


def _read_text_versioned(path, sleep=None):
    """(text, version) of the file at path, or (None, None) when it does not exist. The
    version is read from the open handle the text is read from (os.fstat), so the two
    always go together: a writer replaces the file, and the handle keeps the old one. Errors
    as _read_text()."""
    sleep = sleep or time.sleep
    delay = 0.01
    for attempt in range(_READ_TRIES):
        try:
            with Path(path).open('rb') as fh:
                version = file_version(os.fstat(fh.fileno()))
                raw = fh.read()
            return raw.decode('utf-8-sig'), version
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        except PermissionError:
            if attempt == _READ_TRIES - 1:
                raise
            sleep(delay)
            delay = min(delay * 2, 0.2)
        except UnicodeDecodeError as exc:
            raise StoreReadError(
                'mendwright: ' + str(path) + ' is not UTF-8 text (' + str(exc) + '). Save it '
                'as UTF-8 (PowerShell 5 writes UTF-16 with > and Out-File), or delete it.'
            ) from None
    return None, None  # not reached


def _respell_frame_heals(data):
    """Give a heal saved inside an iframe the text mendwright saves it with since 0.6.1:
    the XPath with its 'xpath=' prefix written out (selectors.frame_spelling), which keeps
    it, and a quarantine on it, apart from the same XPath on the page. 0.6.0 saved those
    heals as the plain XPath. Only rows with source 'heal' and a frameXPath change, and
    not one whose new text another row of the key already has. Returns data."""
    for entry in (data.get('selectors') or {}).values():
        rows = entry.get('candidates') if isinstance(entry, dict) else None
        if not isinstance(rows, list):
            continue
        texts = {c.get('xpath') for c in rows if isinstance(c, dict)}
        for c in rows:
            if not (isinstance(c, dict) and c.get('source') == 'heal' and c.get('frameXPath')
                    and isinstance(c.get('xpath'), str)):
                continue
            try:
                spelled = frame_spelling(c['xpath'])
            except (TypeError, ValueError):
                continue
            if spelled and spelled != c['xpath'] and spelled not in texts:
                texts.discard(c['xpath'])
                c['xpath'] = spelled
                texts.add(spelled)
    return data


def _restore_shadow_hosts(data):
    """Give a heal saved inside a shadow root back the shadowHosts a mendwright that knows
    nothing of them (0.9.0 and before) dropped when it wrote the file: without them its
    chain would run as Playwright runs it, and a heal of the key on the page around the
    host. They are read back from the heal's text (shadow.text_hosts), the chain of its
    hosts and a CSS selector, which 0.9.0 never saves as a heal itself. Only rows with
    source 'heal' and no frameXPath or shadowHosts change. Returns data."""
    for entry in (data.get('selectors') or {}).values():
        rows = entry.get('candidates') if isinstance(entry, dict) else None
        if not isinstance(rows, list):
            continue
        for c in rows:
            if not (isinstance(c, dict) and c.get('source') == 'heal'
                    and not c.get('frameXPath') and not c.get('shadowHosts')):
                continue
            hosts = text_hosts(c.get('xpath'))
            if hosts:
                c['shadowHosts'] = list(hosts)
    return data


# How many times in a row _locked() tries again, a few milliseconds apart, when creating the
# lock file is refused and no lock file is there (see the comment in _locked).
_LOCK_DENIED_TRIES = 4


@contextlib.contextmanager
def _locked(path, *, timeout, stale_after, clock=time.monotonic, sleep=time.sleep,
            wallclock=time.time):
    """Hold the lock file next to the store file ``path`` (overrides.json.lock) for the
    block, waiting up to ``timeout`` seconds for another process's lock and removing one
    older than ``stale_after`` seconds (its owner most likely died while writing). Raises
    TimeoutError when the lock stays held. SelectorStore and edit_list_heal() share it."""
    path = Path(path)
    lock = path.with_name(path.name + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = clock() + timeout
    delay = 0.005
    denied = 0
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError) as exc:
            # On Windows a lock file that is being deleted gives PermissionError for a
            # moment, and can be gone by the time it is looked for. A few quick tries tell
            # that from a real permission problem, which still raises within milliseconds,
            # not at the deadline.
            if isinstance(exc, PermissionError) and not lock.exists():
                denied += 1
                if denied > _LOCK_DENIED_TRIES:
                    raise
                sleep(0.002 * denied)
                continue
            # The lock is there: the refusals before were writers coming and going, so a
            # waiter behind many of them meets that moment once per release, not too often.
            denied = 0
            if _break_if_stale(lock, stale_after, wallclock):
                continue
            if clock() >= deadline:
                raise TimeoutError('the lock file ' + str(lock) + ' is held by another '
                                   'process') from None
            sleep(delay)
            delay = min(delay * 2, 0.1)
            continue
        try:
            os.write(fd, str(os.getpid()).encode('ascii'))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        try:
            os.unlink(str(lock))
        except OSError:
            pass


def _break_if_stale(lock, stale_after, wallclock=time.time):
    """Remove a lock file older than stale_after seconds. True if it was removed."""
    try:
        age = wallclock() - os.stat(str(lock)).st_mtime
    except OSError:
        return False
    if age <= stale_after:
        return False
    try:
        os.unlink(str(lock))
    except FileNotFoundError:
        pass
    except OSError:
        return False
    logger.warning('mendwright: removed a stale lock file %s (%.0f seconds old)', lock, age)
    return True


def _atomic_write(path, text, sleep=time.sleep):
    """Write text to a temp file next to path, then os.replace() it in. On Windows the
    replace fails while another process has the file open for reading, so it is retried
    briefly."""
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.sel-', suffix='.json', dir=str(parent))
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        delay = 0.01
        for attempt in range(8):
            try:
                os.replace(tmp, str(path))
                break
            except PermissionError:
                if attempt == 7:
                    raise
                sleep(delay)
                delay = min(delay * 2, 0.2)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_schema2(path, destructive_keys=frozenset()):
    """Read a selector file, migrating schema-1 to schema-2. A missing file is an empty
    store; one that exists but cannot be used raises StoreReadError."""
    return _read(path, destructive_keys)[0]


def _read(path, destructive_keys=frozenset(), *, sleep=None):
    """(schema-2 data, True if the file itself was schema 2). A missing (or blank) file is
    an empty store. Raises StoreReadError for a file that is not valid JSON or not a
    selector file, and PermissionError when the file stays unreadable."""
    return _parse(_read_text(path, sleep), path, destructive_keys)


def _read_versioned(path, destructive_keys=frozenset(), *, sleep=None):
    """_read() and the version of what it read (see file_version): (data, native, version),
    version None for a missing file."""
    text, version = _read_text_versioned(path, sleep)
    data, native = _parse(text, path, destructive_keys)
    return data, native, version


# The override files this process parsed for SelectorStore.refresh(): path -> (version,
# data), the latest version of each only. One parse per file version per process, however
# many stores refresh from it: the data is shared, so nothing may change it (refresh() takes
# a deep copy of the one key it merges).
_PARSED = {}
_PARSED_LOCK = threading.Lock()
# path -> the lock held while that file is read and parsed, so a version is parsed once.
_PARSING = {}


def parsed_override(path, version=None, *, sleep=None):
    """(data, version) of the override file at path, read and parsed at most once per file
    version per process: version is what the caller just saw with stat_version(), and the
    file is read only when this process has not parsed that version yet. The version
    returned is the one of the content (it can be newer than the one asked for). data is
    None for a file that is not schema 2 (a schema-1 file is converted by the next write).
    Raises StoreReadError, and OSError, for a file that cannot be read: never an empty
    store in its place."""
    name = os.path.normcase(os.path.abspath(str(path)))
    with _PARSED_LOCK:
        hit = _PARSED.get(name)
        if hit is not None and version is not None and hit[0] == version:
            return hit[1], hit[0]
        lock = _PARSING.setdefault(name, threading.Lock())
    with lock:
        with _PARSED_LOCK:
            hit = _PARSED.get(name)
        if hit is not None and version is not None and hit[0] == version:
            return hit[1], hit[0]
        data, native, read = _read_versioned(path, sleep=sleep)
        if not native:
            return None, read
        with _PARSED_LOCK:
            _PARSED[name] = (read, data)
        return data, read


def _parse(text, path, destructive_keys=frozenset()):
    """_read() of the text of the file at path (None for a missing file)."""
    if text is None or not text.strip():
        return _empty(), False
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise StoreReadError(
            'mendwright: ' + str(path) + ' is not valid JSON (' + str(exc) + '). Fix it (a '
            'merge conflict or a hand edit?) or delete it.') from None
    selectors = data.get('selectors') if isinstance(data, dict) else None
    if data == {}:
        return _empty(), False
    if isinstance(data, dict) and data.get('schema') == 2 and isinstance(selectors, dict):
        return _restore_shadow_hosts(_respell_frame_heals(data)), True
    if isinstance(data, dict) and data.get('schema') in (None, 1) and isinstance(selectors, list):
        return migrate_schema1(data, destructive_keys), False
    raise StoreReadError(
        'mendwright: ' + str(path) + ' is not a selector file (expected {"schema": 2, '
        '"selectors": {...}}). Point mendwright at the right file, or delete this one.')


# Metadata fields saved in the override file: the ones set_meta() sets, the pages
# set_last_page() records, the address set_last_url() records and the steps set_last_step()
# records. intent is what the element
# is for, in words (locator(intent=)); fingerprint is what the element looked like the last
# time it matched (see mendwright.fingerprint), and listFingerprint what the items of a
# list looked like the last time it matched two or more (see mendwright.lists).
# shadowHosts is where the element was the last time it matched, when that was inside an
# open shadow root: the selectors of its shadow hosts, outermost first (see
# mendwright.shadow).
META_FIELDS = ('expectedRole', 'expectedText', 'destructive', 'lastPage', 'pages', 'intent',
               'fingerprint', 'steps', 'listFingerprint', 'lastUrl', 'shadowHosts')

# How many pages set_last_page() keeps per key, most recent first, and how many steps
# set_last_step() keeps.
MAX_PAGES = 5
MAX_STEPS = 5


def _known_role(role):
    return bool(role) and role != 'unknown'


def _meta_fields(entry):
    """The saved metadata of one override entry, with malformed values dropped."""
    out = {}
    if not isinstance(entry, dict):
        return out
    role = entry.get('expectedRole')
    if isinstance(role, str) and role:
        out['expectedRole'] = role
    text = entry.get('expectedText')
    if isinstance(text, list) and all(isinstance(t, str) for t in text):
        out['expectedText'] = list(text)
    if isinstance(entry.get('destructive'), bool):
        out['destructive'] = entry['destructive']
    page = entry.get('lastPage')
    if isinstance(page, str) and page:
        out['lastPage'] = page
    if _valid_url(entry.get('lastUrl')):
        out['lastUrl'] = entry['lastUrl']
    pages = _pages(entry.get('pages'))
    if pages:
        out['pages'] = pages
    steps = _pages(entry.get('steps'))
    if steps:
        out['steps'] = steps
    intent = entry.get('intent')
    if isinstance(intent, str) and intent.strip():
        out['intent'] = intent
    if _valid_fingerprint(entry.get('fingerprint')):
        out['fingerprint'] = entry['fingerprint']
    if _valid_list_fingerprint(entry.get('listFingerprint')):
        out['listFingerprint'] = entry['listFingerprint']
    # A malformed value keeps a host nothing tells apart (None), so the key is never taken
    # to be on the page because of it.
    hosts = list(saved_hosts(entry.get('shadowHosts')))
    if hosts:
        out['shadowHosts'] = hosts
    return out


def _valid_url(value):
    """Whether value is an address set_last_url() saves: one page_url() gives back as it
    is, so it has no user info, query, fragment or unmasked record id."""
    return isinstance(value, str) and bool(value) and page_url(value) == value


def _pages(value):
    """A saved 'pages' list, or [] when it is missing or malformed."""
    if isinstance(value, list) and all(isinstance(p, str) and p for p in value):
        return list(value)[:MAX_PAGES]
    return []


# --- writes that can wait -------------------------------------------------------------------
#
# A lookup changes the store in small ways that need not reach the file at once: a
# candidate's lastSucceededAt (the order candidates are tried in, and the first success
# mendwright report shows), the pages, steps and address a key was found on, and its
# fingerprints. With many workers on one file, writing each of them at once rewrote the file
# for every worker (and with dynamic text in an element, on every lookup). What the file does
# not have yet is still written at once: a first success (unless another worker recorded one
# already: the store looks at the file first), a key's first pages, steps and address, its
# first fingerprint. Changes to what it has (the order of the last successes, a new page in
# the list, a new fingerprint) are "soft": written at most every SOFT_EVERY seconds per key per
# process, the first of a key in a process at once, with a write of the store as before; a
# later one stays in the store's memory and goes with the store's next write (a heal, a
# quarantine, a soft change that is due). Each is also kept in a small table the whole
# process shares (SOFT): when the process exits (atexit, and multiprocessing's exit hook,
# which runs where atexit does not), what a store never wrote, because it was dropped first,
# is written there. That flush edits
# the file in place under its lock with the merge rules of a write, and writes nothing when the
# file already holds the change: the first success another worker recorded, a page list it
# already has. Heals, quarantines, roles, destructive flags, code selectors, list heals and
# shadow hosts are written at once, as before.

# Seconds between two writes of a key's soft changes by one process.
SOFT_EVERY = 300.0
# The metadata a soft change carries (the rest of a key's metadata is written at once).
SOFT_META = ('lastPage', 'pages', 'steps', 'lastUrl', 'fingerprint', 'listFingerprint')


def _changes_order(rows, row, when):
    """True when giving row lastSucceededAt=when moves it up among the active rows: another
    active row succeeded at or after row's old time and before when."""
    old = row.get('lastSucceededAt') or ''
    for other in rows:
        if other is row or other.get('status', 'active') != 'active':
            continue
        at = other.get('lastSucceededAt') or ''
        if old <= at < when:
            return True
    return False


def _apply_soft(data, pending):
    """Apply soft changes ({key: {'rows': {xpath: lastSucceededAt}, 'meta': {field: value},
    'replaced': {field: value}}}) to the parsed override file data, in place, with the merge
    rules of a write. True when the file must be written: a first success the file does not
    have, a success that changes the order, pages or steps it does not list, a new
    fingerprint, a first address. A new order of known pages, a newer success of the row
    already on top and a new address are written only with such a change, as a store writes
    them with its next write. meta holds only what the process changed (see
    SelectorStore._persist_soft); a fingerprint or list fingerprint with the value it replaced
    in replaced is written only while the file still holds that value: another process's
    newer one stays."""
    selectors = data.setdefault('selectors', {})
    changed, carried = False, []
    for key, delta in pending.items():
        entry = selectors.get(key)
        meta = delta.get('meta') or {}
        if not isinstance(entry, dict):
            if not meta:
                continue        # its rows are not in the file (a seed row): nothing to do
            entry = selectors[key] = {'candidates': []}
        rows = [c for c in entry.get('candidates') or ()
                if isinstance(c, dict) and isinstance(c.get('xpath'), str)]
        for xpath, when in (delta.get('rows') or {}).items():
            row = next((c for c in rows if c['xpath'] == xpath), None)
            if row is None or not isinstance(when, str) or not when:
                continue
            old = row.get('lastSucceededAt')
            if not old:
                row['lastSucceededAt'] = when       # a first success nobody recorded
                changed = True
            elif when > old:
                if _changes_order(rows, row, when):
                    row['lastSucceededAt'] = when
                    changed = True
                else:
                    carried.append((row, 'lastSucceededAt', when))
        for field, cap in (('pages', MAX_PAGES), ('steps', MAX_STEPS)):
            if field not in meta:
                continue
            theirs = _pages(entry.get(field))
            merged = list(dict.fromkeys(_pages(meta[field]) + theirs))[:cap]
            if set(merged) != set(theirs):
                entry[field] = merged
                changed = True
            elif merged != theirs:
                carried.append((entry, field, merged))
        replaced = delta.get('replaced') or {}
        for field in ('fingerprint', 'listFingerprint'):
            if field not in meta or entry.get(field) == meta[field]:
                continue
            if field in replaced and entry.get(field) != replaced[field]:
                continue        # another process saved one since this one was read
            entry[field] = copy.deepcopy(meta[field])
            changed = True
        for field in ('lastUrl', 'lastPage'):
            if field not in meta or entry.get(field) == meta[field]:
                continue
            if not entry.get(field):
                entry[field] = meta[field]
                changed = True
            else:
                carried.append((entry, field, meta[field]))
    if changed:
        for target, field, value in carried:
            target[field] = value
    return changed


class SoftWrites:
    """The soft changes of every store of this process that wait to be written (see the
    comment above SOFT_EVERY), per store file and key: only the changed rows' times and the
    key's soft metadata, never a store, so a store can be dropped while its changes wait."""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}          # path -> {key: {'rows': {...}, 'meta': {...}}}
        self._flushed = {}          # (path, key) -> clock() of its last flush
        self._failed = set()        # paths whose flush failed (logged once)
        # path -> writer(pending) for a store kept elsewhere than a local file (see register).
        self._writers = {}
        self._hooked = False
        self.clock = time.monotonic

    def register(self, path, writer):
        """Have flush(path) write the soft changes that wait with writer(pending) instead of
        editing a local file: for a store kept elsewhere (mendwright.remote), path is its name.
        writer applies them as _apply_soft() does and writes only when that says so; it raises
        when the store cannot be read or written, and the changes keep waiting."""
        with self._lock:
            self._writers[path] = writer

    def record(self, path, key, rows=None, meta=None, replaced=None):
        """Keep a soft change of key in the file at path: rows {xpath: lastSucceededAt} (the
        newest wins) and meta (pages and steps join, the newest of the rest wins), and for a
        fingerprint, what it replaced (the latest wins: see _apply_soft)."""
        with self._lock:
            entry = self._pending.setdefault(path, {}).setdefault(
                key, {'rows': {}, 'meta': {}, 'replaced': {}})
            for field, value in (replaced or {}).items():
                entry.setdefault('replaced', {})[field] = copy.deepcopy(value)
            for xpath, when in (rows or {}).items():
                if isinstance(when, str) and when > (entry['rows'].get(xpath) or ''):
                    entry['rows'][xpath] = when
            for field, value in (meta or {}).items():
                if field in ('pages', 'steps'):
                    cap = MAX_PAGES if field == 'pages' else MAX_STEPS
                    value = list(dict.fromkeys(_pages(value) + _pages(entry['meta'].get(field))))
                    entry['meta'][field] = value[:cap]
                else:
                    entry['meta'][field] = copy.deepcopy(value)
            if not self._hooked:
                self._hooked = True
                self._hook_exit()

    def due(self, path, keys):
        """True when one of keys of the file at path was not written by this process in the
        last SOFT_EVERY seconds."""
        now = self.clock()
        with self._lock:
            return self._due(path, keys, now)

    def _due(self, path, keys, now):
        return any(now - self._flushed[(path, k)] >= SOFT_EVERY
                   if (path, k) in self._flushed else True for k in keys)

    def claim_due(self, path, keys):
        """due(), and when it is True, the turn to write: keys count as written now. Of the
        stores of this process whose soft changes of a key come due at once, one writes them
        and the others' wait SOFT_EVERY seconds, as one store's would (they go with that store's
        next write, or at exit)."""
        now = self.clock()
        with self._lock:
            if not self._due(path, keys, now):
                return False
            for key in keys:
                self._flushed[(path, key)] = now
            return True

    def written(self, path, keys):
        """Note that the soft changes of keys of the file at path were just written (by a
        store's own write): the next ones wait SOFT_EVERY seconds."""
        now = self.clock()
        with self._lock:
            for key in keys:
                self._flushed[(path, key)] = now

    def waiting(self, path=None):
        """{path: [keys]} of the soft changes that wait, or the keys of one path."""
        with self._lock:
            if path is not None:
                return sorted(self._pending.get(path, {}))
            return {p: sorted(keys) for p, keys in self._pending.items()}

    def flush(self, path, *, timeout=None, stale_after=None, sleep=time.sleep):
        """Write the soft changes that wait for the file at path, under its lock, with the merge
        rules of a write (see _apply_soft). A file that cannot be read or locked keeps them
        waiting (one WARNING), and is never replaced by them alone. True when nothing waits
        any more."""
        with self._lock:
            pending = self._pending.pop(path, None)
            if not pending:
                return True
            now = self.clock()
            for key in pending:
                self._flushed[(path, key)] = now
            writer = self._writers.get(path)
        if writer is None and not os.path.exists(path):
            return True     # the store file is gone: soft changes alone never make one
        try:
            if writer is not None:
                writer(pending)
            else:
                with _locked(path, timeout=(SelectorStore.lock_timeout if timeout is None
                                            else timeout),
                             stale_after=(SelectorStore.lock_stale_after if stale_after is None
                                          else stale_after), sleep=sleep):
                    data, _native = _read(path, sleep=sleep)
                    if _apply_soft(data, pending):
                        _atomic_write(path, json.dumps(data, separators=(',', ':')), sleep)
        except Exception as exc:
            for key, delta in pending.items():
                self.record(path, key, delta['rows'], delta['meta'], delta.get('replaced'))
            if path not in self._failed:
                self._failed.add(path)
                logger.warning('mendwright could not save the pages, fingerprints and last '
                               'successes of %d key(s) to %s yet (%s); they wait in memory and '
                               'are saved later, or when the process exits.', len(pending),
                               path, type(exc).__name__ + (': ' + str(exc) if str(exc) else ''))
            return False
        self._failed.discard(path)
        return True

    def clear(self):
        """Forget every soft change that waits and when each key was written: for tests,
        whose store files are temporary."""
        with self._lock:
            self._pending.clear()
            self._flushed.clear()
            self._failed.clear()
            self._writers.clear()

    def flush_all(self):
        """flush() every file with soft changes that wait. Runs when the process exits."""
        with self._lock:
            paths = list(self._pending)
        for path in paths:
            try:
                self.flush(path)
            except Exception:
                pass

    def _hook_exit(self):
        """Flush at exit: atexit, and multiprocessing's own exit hook, which a child process
        of multiprocessing runs and atexit does not."""
        import atexit
        atexit.register(self.flush_all)
        try:
            from multiprocessing import util
            util.Finalize(None, self.flush_all, exitpriority=20)
        except Exception:
            pass


# The soft changes of every store of this process.
SOFT = SoftWrites()


class _FileWrites:
    """The writes of one store file by the stores of this process (see
    SelectorStore._persist_override and _persist_keys): one at a time, under lock, so a store
    waits on it rather than polling the file's lock as every thread did; and count, the writes
    that ended, so a store that waited sees whether another one wrote meanwhile and looks at the
    file again before it writes the same. Across processes the file's lock keeps writers apart."""

    def __init__(self):
        self.lock = threading.RLock()
        self.count = 0


_WRITERS = {}
_WRITERS_LOCK = threading.Lock()


def _writes(name):
    """The _FileWrites of the store named name (see SelectorStore._turns)."""
    with _WRITERS_LOCK:
        found = _WRITERS.get(name)
        if found is None:
            found = _WRITERS[name] = _FileWrites()
        return found


def flush_all():
    """Write every soft change of this process that waits (see SOFT_EVERY) now. It also runs
    when the process exits; call it before os._exit() or when a long-lived process should save
    its stores' pages, fingerprints and last successes at a point of your choosing."""
    SOFT.flush_all()


# The statuses of a list heal that waits: for a person to check it, or for mendwright fix.
LIST_WAITING = ('proposed', 'accepted')
# How many proposed and accepted list heals a key keeps: an accepted one, then the newest.
MAX_LIST_HEALS = 3


def evidence_of(c):
    """A candidate's saved proposal (see lists.proposal_evidence) when it has one this
    version reads: a dict with a str id, a str at and an int count. Else None."""
    proposal = c.get('proposal') if isinstance(c, dict) else None
    if (isinstance(proposal, dict) and isinstance(proposal.get('id'), str)
            and isinstance(proposal.get('at'), str) and isinstance(proposal.get('count'), int)
            and not isinstance(proposal.get('count'), bool)):
        return proposal
    return None


def _is_list_heal(c):
    """True for a list heal that waits (proposed or accepted)."""
    return c.get('list') is True and c.get('status') in LIST_WAITING


def _proposal_order(c):
    """Newest proposal first, then the newest added; rows with no proposal last."""
    evidence = evidence_of(c)
    return (evidence is not None, evidence['at'] if evidence else '', c.get('addedAt') or '')


class SelectorStore:
    # Seconds to wait for another process's lock on the override file, and the age after
    # which a leftover lock file is treated as abandoned.
    lock_timeout = 15.0
    lock_stale_after = 10.0

    # Seconds a lookup lets pass before it looks at the file again for a key it is about to
    # use through a saved selector, or that keeps missing (see refresh() and resolver.resolve).
    refresh_every = 1.0

    def __init__(self, data, override_path, destructive_keys=frozenset(), seed=None,
                 override=None, *, read_only=False, file_version=None, writes=True):
        self._data = data
        # read_only: changes stay in memory, and the file is never locked, read or written
        # again (see _persist_override).
        self._read_only = bool(read_only)
        # writes=False: the file is read, and refresh() takes in what other workers save, but
        # it is never written, nor its key records: changes stay in memory (see writes).
        self._writes = bool(writes)
        # The version (see file_version) of the override file every key in memory was last
        # merged with: the file load_store() read, or the one this store last wrote. key ->
        # the version refresh() last merged that key with, when newer.
        self._version = file_version
        self._key_versions = {}
        # key -> when refresh() last looked at the file for it (self._clock), for max_age.
        self._looked = {}
        # (key, xpath) of the heals that came in from the file after this store read it: a
        # heal another worker saved meanwhile (see arrived()).
        self._arrived = set()
        # Held while a refresh or a write merges the file into memory: two threads may share
        # a store passed to open(store=...).
        self._mutex = threading.RLock()
        # Keys whose soft changes (see SOFT_EVERY) wait for this store's next write.
        self._soft_waiting = set()
        # True after a write failed, until one succeeds: memory then holds changes the file
        # lacks, so no write is skipped because the file holds the change at hand (_file_holds).
        self._unsaved = False
        self._override_path = Path(_expand(override_path))
        # A key written as a get_by_* call in another spelling marks the call's key too. One
        # written on an object, such as page.get_by_role(...), does not, and is logged.
        destructive_keys = frozenset(destructive_keys)
        for key in sorted(destructive_keys, key=str):
            getby.warn_on_object('destructive_keys=', key)
        self._destructive_keys = destructive_keys | {getby.canonical_key(k)
                                                     for k in destructive_keys}
        # Injected in tests: the monotonic clock and sleep for lock waits and write
        # retries, and the wall clock compared with a lock file's mtime.
        self._clock = time.monotonic
        self._sleep = time.sleep
        self._wallclock = time.time
        self._warned = False
        # Per thread: how many batch() blocks are open, and whether a write waits for them.
        self._batch = threading.local()
        # Keys whose expectedRole is a guess made with guess_role(): used, never saved.
        self._guessed = set()
        # key -> the candidate record_success() last marked in this process.
        self._last_success = {}
        # key -> selectors set_code_candidates() put first, in order. In memory only.
        self._preferred = {}
        # (key, xpath) of the list heals set_code_candidates() took over as code in this
        # process: the file's "list", proposal and acceptedAt are not taken back for them.
        self._delisted = set()
        # (key, xpath) pairs mutated locally this session (success/quarantine/add).
        self._dirty = set()
        # Pristine seed state {(key, xpath): (status, lastSucceededAt)}. The override file
        # persists ONLY candidates that are override-origin (source != 'seed'), mutated this
        # session, OR whose current state DIFFERS from the seed (so a quarantine persisted in
        # a prior session is not dropped on a later persist, which would fail open). An
        # untouched seed row is never written, so a candidate removed from a future seed can't
        # resurrect and a publish-side quarantine can't be flipped back to active.
        self._seed_state = {}
        for skey, sentry in (seed or {}).get('selectors', {}).items():
            for sc in sentry.get('candidates', []):
                self._seed_state[(skey, sc.get('xpath'))] = _row_state(sc)
        # A list heal that waited and that the seed file holds now is the seed's row (see
        # merge): the file's "list", proposal and acceptedAt are not taken back for it.
        for okey, oentry in (override or {}).get('selectors', {}).items():
            for oc in (oentry.get('candidates') or () if isinstance(oentry, dict) else ()):
                if (isinstance(oc, dict) and _is_list_heal(oc)
                        and (okey, oc.get('xpath')) in self._seed_state):
                    self._delisted.add((okey, oc['xpath']))
        # Keys whose destructive flag nothing can clear: the seed's flags (or, with no seed
        # given, the flags in data). destructive_keys is checked as well, in meta().
        source = seed if seed is not None else data
        self._locked = {k for k, e in (source or {}).get('selectors', {}).items()
                        if isinstance(e, dict) and e.get('destructive')}
        # Keys whose role or expected text the seed (or data, with no seed) already sets;
        # saved values never replace those.
        self._seed_role = {k for k, e in (source or {}).get('selectors', {}).items()
                           if isinstance(e, dict) and _known_role(e.get('expectedRole'))}
        self._seed_text = {k for k, e in (source or {}).get('selectors', {}).items()
                           if isinstance(e, dict) and e.get('expectedText')}
        # Metadata saved through set_meta(), per key, exactly as written to the override.
        # `override` is the parsed override file; load_store() passes it.
        self._meta = {}
        for key, entry in (override or {}).get('selectors', {}).items():
            fields = _meta_fields(entry)
            if fields:
                self._meta[key] = fields
                self._overlay_meta(key, fields)
        # What the override file held when this store last read or wrote it: candidate
        # statuses and saved metadata. A write compares memory and the file against it to
        # tell which side changed (a three-way merge).
        self._base = {}
        self._base_meta = {}
        self._remember_base(override or {})

    def _overlay_meta(self, key, fields):
        """Apply saved metadata to a loaded entry. A known role and non-empty expected
        text from the seed win over saved ones. The destructive flag is ORed with the
        locked flags in meta()."""
        e = self._data['selectors'].get(key)
        if not isinstance(e, dict):
            return
        if 'expectedRole' in fields and key not in self._seed_role:
            e['expectedRole'] = fields['expectedRole']
            self._guessed.discard(key)
        if 'expectedText' in fields and key not in self._seed_text:
            e['expectedText'] = list(fields['expectedText'])
        if 'destructive' in fields:
            e['destructive'] = fields['destructive']
        for name in ('lastPage', 'pages', 'steps', 'intent', 'fingerprint', 'listFingerprint',
                     'lastUrl', 'shadowHosts'):
            if name in fields:
                e[name] = (list(fields[name]) if name in ('pages', 'steps', 'shadowHosts')
                           else fields[name])

    def _drop_meta(self, key, names):
        """Take saved metadata off a loaded entry when another process removed it, for the
        fields that can be removed (shadowHosts: the element is on the page now)."""
        e = self._data['selectors'].get(key)
        if isinstance(e, dict) and 'shadowHosts' in names:
            e.pop('shadowHosts', None)

    def is_locked_destructive(self, key):
        """True when the seed or destructive_keys marks key destructive. set_meta() cannot
        clear such a flag."""
        return key in self._destructive_keys or key in self._locked

    def destructive_choice(self, key):
        """The destructive flag set_meta() saved for key (True or False), or None when it
        was never set. Tells an explicit destructive=False apart from the default."""
        return self._meta.get(key, {}).get('destructive')

    def meta(self, key):
        e = self._data['selectors'][key]
        return {'expectedRole': e.get('expectedRole', 'unknown'),
                'expectedText': e.get('expectedText', []),
                'destructive': bool(e.get('destructive')) or self.is_locked_destructive(key),
                'stage': e.get('stage'), 'action': e.get('action'),
                'maxCandidates': e.get('maxCandidates', 5),
                'lastPage': e['lastPage'] if isinstance(e.get('lastPage'), str) else None,
                'pages': _pages(e.get('pages')),
                'steps': _pages(e.get('steps')),
                'intent': e['intent'] if isinstance(e.get('intent'), str) and e['intent'] else None,
                'fingerprint': e['fingerprint'] if _valid_fingerprint(e.get('fingerprint')) else None,
                'listFingerprint': (e['listFingerprint']
                                    if _valid_list_fingerprint(e.get('listFingerprint')) else None)}

    def shadow_hosts(self, key):
        """Where key's element was the last time it matched: the selectors of the shadow
        hosts around it, outermost first (None for a host nothing tells apart), or [] for the
        page or when nothing was recorded (see mendwright.shadow). Not part of meta(), which
        keeps the keys 0.9.0 gave."""
        e = self._data['selectors'].get(key)
        return list(saved_hosts(e.get('shadowHosts'))) if isinstance(e, dict) else []

    def _by_recency(self, key):
        e = self._data['selectors'].get(key)
        if not e:
            return []
        active = [c for c in e.get('candidates', []) if c.get('status', 'active') == 'active']
        return sorted(active, key=lambda c: (c.get('lastSucceededAt') or ''), reverse=True)

    def candidates(self, key):
        """Active candidates, most recently succeeded first. Selectors passed to
        set_code_candidates() in this process come first, in the order given."""
        ranked = self._by_recency(key)
        preferred = self._preferred.get(key)
        if preferred:
            order = {x: i for i, x in enumerate(preferred)}
            first = sorted((c for c in ranked if c['xpath'] in order), key=lambda c: order[c['xpath']])
            ranked = first + [c for c in ranked if c['xpath'] not in order]
        return ranked

    def set_code_candidates(self, key, selectors, *, now=None):
        """Make selectors (in order) the selectors the code currently passes for key.

        Each one becomes an active candidate with source 'code' (a healed or retired
        candidate with the same text is taken over and made active again; a list heal taken
        over drops its "list" mark, proposal and acceptedAt, as mendwright fix put it in the
        code, and a heal inside a shadow root its shadowHosts, so its Playwright chain runs as
        written; an accepted list heal whose CSS form is passed is superseded, for the same
        reason). Older 'code' candidates that are no longer passed get status 'superseded':
        they stay in the file but are not resolved. Seed and heal candidates stay as
        fallbacks. For the rest of this process, candidates() lists these selectors first,
        in this order. The override file is written only when something changed."""
        entry = self._entry(key)
        wanted = list(dict.fromkeys(selectors))
        changed = False
        for xpath in wanted:
            c = self._find(key, xpath)
            if c is None:
                entry['candidates'].append({'xpath': xpath, 'source': 'code',
                                            'addedAt': now or _now_iso(),
                                            'lastSucceededAt': None, 'status': 'active'})
            elif c.get('source') == 'heal' or c.get('status', 'active') != 'active':
                if c.get('source') == 'heal':
                    c['source'] = 'code'
                if c.get('shadowHosts'):
                    # A heal inside a shadow root that mendwright fix wrote into the code as
                    # its Playwright chain: the code's selector runs as Playwright runs it.
                    c['shadowHosts'] = []
                if c.get('list') is True and (c.get('status') in LIST_WAITING
                                              or 'proposal' in c or 'acceptedAt' in c):
                    for name in ('list', 'proposal', 'acceptedAt'):
                        c.pop(name, None)
                    self._delisted.add((key, xpath))
                c['status'] = 'active'
            else:
                continue
            self._dirty.add((key, xpath))
            changed = True
        for c in entry['candidates']:
            if (c.get('source') == 'code' and c['xpath'] not in wanted
                    and c.get('status', 'active') == 'active'):
                c['status'] = 'superseded'
                self._dirty.add((key, c['xpath']))
                changed = True
            elif (c.get('list') is True and c.get('status') == 'accepted'
                  and c['xpath'] not in wanted and c.get('css') in wanted):
                # mendwright fix wrote the list heal's CSS form into the code: done with it.
                c['status'] = 'superseded'
                self._dirty.add((key, c['xpath']))
                changed = True
        self._preferred[key] = wanted
        if changed:
            self._persist_keys((key,))

    def _entry(self, key):
        return self._data['selectors'].setdefault(key, {
            'expectedRole': 'unknown', 'expectedText': [], 'destructive': key in self._destructive_keys,
            'maxCandidates': 5, 'stage': None, 'action': None, 'candidates': []})

    def set_meta(self, key, *, role=None, text=None, destructive=None, stage=None, action=None,
                 intent=None, fingerprint=None, list_fingerprint=None, shadow_hosts=None):
        """Set a key's metadata. None leaves a field as it is.

        role, text, destructive, intent, fingerprint, list_fingerprint and shadow_hosts are
        saved in the override file, so the next process sees them; the file is written only
        when one of them changes. destructive=False clears a flag an earlier set_meta()
        saved, but never one from the seed or from destructive_keys: meta() still reports
        those keys as destructive. intent is a description of the element in words;
        fingerprint a dict from mendwright.fingerprint and list_fingerprint one from
        mendwright.lists (anything else raises ValueError). shadow_hosts is where the
        element was found: the selectors of the shadow hosts around it, outermost first (None
        for a host nothing tells apart), or [] for the page, which removes the field (see
        mendwright.shadow). stage and action stay in memory."""
        e = self._entry(key)
        changed = {}
        cleared = []
        if shadow_hosts is not None:
            if not isinstance(shadow_hosts, (list, tuple)):
                raise ValueError('mendwright: set_meta(shadow_hosts=...) takes a list of host '
                                 'selectors, got ' + type(shadow_hosts).__name__ + '.')
            hosts = list(saved_hosts(list(shadow_hosts)))
            if hosts:
                e['shadowHosts'] = hosts
                changed['shadowHosts'] = hosts
            else:
                e.pop('shadowHosts', None)
                cleared.append('shadowHosts')
        if intent is not None:
            if not isinstance(intent, str) or not intent.strip():
                raise ValueError('mendwright: intent must be a non-empty string, got '
                                 + repr(intent) + '. Describe the element in words, for '
                                 "example intent='the terms checkbox in the signup form'.")
            # One line: the prompt gives the model one 'intent:' line, so a line break in
            # it must not start what looks like another field.
            e['intent'] = ' '.join(intent.split())
            changed['intent'] = e['intent']
        if fingerprint is not None:
            if not _valid_fingerprint(fingerprint):
                raise ValueError('mendwright: set_meta(fingerprint=...) takes a fingerprint '
                                 'from mendwright.fingerprint, got ' + type(fingerprint).__name__
                                 + '. Build one with mendwright.fingerprint.of_element().')
            e['fingerprint'] = fingerprint
            changed['fingerprint'] = fingerprint
        if list_fingerprint is not None:
            if not _valid_list_fingerprint(list_fingerprint):
                raise ValueError('mendwright: set_meta(list_fingerprint=...) takes a list '
                                 'fingerprint from mendwright.lists, got '
                                 + type(list_fingerprint).__name__ + '. Build one with '
                                 'mendwright.lists.of_elements().')
            e['listFingerprint'] = list_fingerprint
            changed['listFingerprint'] = list_fingerprint
        if role is not None:
            e['expectedRole'] = role
            changed['expectedRole'] = role
            self._guessed.discard(key)
        if text is not None:
            e['expectedText'] = list(text) if isinstance(text, (list, tuple)) else [text]
            changed['expectedText'] = list(e['expectedText'])
        if destructive is not None:
            e['destructive'] = bool(destructive)
            changed['destructive'] = bool(destructive)
        if stage is not None:
            e['stage'] = stage
        if action is not None:
            e['action'] = action
        saved = self._meta.get(key, {})
        diff = ({f for f, v in changed.items() if saved.get(f) != v}
                | {f for f in cleared if f in saved})
        if diff:
            self._meta[key] = {f: v for f, v in dict(saved, **changed).items()
                               if f not in cleared}
            if diff <= {'fingerprint', 'listFingerprint'}:
                # A new fingerprint can wait (see SOFT_EVERY); a first one cannot, and neither
                # can a list fingerprint that now says where the list sits.
                first = any(f not in saved for f in diff) or (
                    'listFingerprint' in diff and not _placed_list(saved.get('listFingerprint'))
                    and _placed_list(changed['listFingerprint']))
                self._persist_soft(key, at_once=first)
            else:
                self._persist_keys((key,))

    def set_last_page(self, key, path):
        """Record that key's element was just found on the page ``path`` (a normalized
        path, see mendwright.flow.page_path). lastPage becomes path, and pages keeps the
        last few distinct paths, most recent first. Saved in the override file, which is
        written only when the set of pages changes: a key used on two pages in turn, such
        as a search box in the header, does not rewrite the file on every call, and its
        new order is saved with the next write. A path of None or '' is ignored."""
        if not isinstance(path, str) or not path:
            return
        e = self._entry(key)
        pages = [path] + [p for p in (e.get('pages') or ()) if p != path]
        pages = pages[:MAX_PAGES]
        e['lastPage'] = path
        e['pages'] = pages
        saved = self._meta.get(key, {})
        if saved.get('lastPage') != path or saved.get('pages') != pages:
            self._meta[key] = dict(saved, lastPage=path, pages=list(pages))
            if set(saved.get('pages') or ()) != set(pages):
                self._persist_soft(key, at_once=not saved.get('pages'))

    def set_last_url(self, key, url):
        """Record the address of the page key's element was just found on: ``url`` as
        mendwright.flow.page_url gives it; anything else is ignored. lastUrl is saved in the
        override file, which is written at once only when the key had no saved lastUrl. A
        new address (another page, or a test app on another port) changes it in memory and
        is saved with the next write, as a new page order is in set_last_page()."""
        if not _valid_url(url):
            return
        e = self._entry(key)
        e['lastUrl'] = url
        saved = self._meta.get(key, {})
        if saved.get('lastUrl') != url:
            self._meta[key] = dict(saved, lastUrl=url)
            if 'lastUrl' not in saved:
                self._persist_soft(key, at_once=True)

    def last_url(self, key):
        """The address key's element was last found on (see set_last_url), or None. It is
        what the loaded entry holds: a merge with a file another process wrote without it
        (mendwright 0.8.0) leaves it here, so this process does not record it again."""
        e = self._data['selectors'].get(key)
        value = e.get('lastUrl') if isinstance(e, dict) else None
        return value if _valid_url(value) else None

    @property
    def read_only(self):
        """True for a store that never writes its file (see load_store(read_only=True))."""
        return self._read_only

    @property
    def writes(self):
        """Whether this store writes its file and its key records. False for a read-only
        worker (load_store(writes=False), MENDWRIGHT_STORE_READ_ONLY=1): it takes in the heals
        and quarantines other workers save, as any store, and keeps its own changes in memory,
        for this process only. A read_only store writes nothing either."""
        return self._writes and not self._read_only

    @writes.setter
    def writes(self, value):
        self._writes = bool(value)

    def snapshot(self):
        """An independent copy of this store in memory, with read_only=True: what is done
        to it (a heal applied, a quarantine) never reaches this store or any file. It keeps
        the destructive flags, the seed's roles and texts, the role guesses, the order
        set_code_candidates() gave and the list heals it took over as code, and starts with no
        batch, no unsaved change and no success recorded in this process."""
        clone = copy.copy(self)
        for name in ('_data', '_meta', '_base', '_base_place', '_base_meta', '_base_index'):
            setattr(clone, name, copy.deepcopy(getattr(self, name, {})))
        for name in ('_locked', '_seed_role', '_seed_text', '_guessed', '_delisted',
                     '_arrived'):
            setattr(clone, name, set(getattr(self, name, ())))
        clone._preferred = {k: list(v) for k, v in self._preferred.items()}
        clone._seed_state = dict(self._seed_state)
        clone._key_versions = dict(getattr(self, '_key_versions', {}))
        clone._looked = {}
        clone._mutex = threading.RLock()
        clone._soft_waiting = set()
        clone._unsaved = False
        clone._batch = threading.local()
        clone._dirty = set()
        clone._last_success = {}
        clone._read_only = True
        return clone

    def set_last_step(self, key, step):
        """Record that key's element was just found while run_steps() ran the step named
        ``step`` (see mendwright.steps). steps keeps the last few distinct names, most
        recent first, and is saved in the override file, which is written only when the set
        of names changes, as for set_last_page(). A name of None or '' is ignored."""
        if not isinstance(step, str) or not step:
            return
        e = self._entry(key)
        steps = ([step] + [s for s in _pages(e.get('steps')) if s != step])[:MAX_STEPS]
        e['steps'] = steps
        saved = self._meta.get(key, {})
        if saved.get('steps') != steps:
            self._meta[key] = dict(saved, steps=list(steps))
            if set(saved.get('steps') or ()) != set(steps):
                self._persist_soft(key, at_once=not saved.get('steps'))

    def guess_role(self, key, role, *, replace=False):
        """Use role as key's expectedRole for now, without saving it. Only replaces an
        unknown role or an earlier guess, unless replace=True."""
        e = self._entry(key)
        if role and (replace or key in self._guessed or not _known_role(e.get('expectedRole'))):
            e['expectedRole'] = role
            self._guessed.add(key)

    def role_is_guess(self, key):
        """True when key's expectedRole is unknown or only a guess_role() guess."""
        e = self._data['selectors'].get(key) or {}
        return key in self._guessed or not _known_role(e.get('expectedRole'))

    def last_success(self, key):
        """The candidate record_success() last marked for key in this process, or None.
        After resolve() this is the candidate that matched."""
        return self._last_success.get(key)

    def status(self, key, xpath):
        """The status of one stored candidate ('active', 'quarantined', 'superseded', and
        for a list heal 'proposed' or 'accepted'), or None when the key has no such
        candidate."""
        entry = self._data['selectors'].get(key)
        for c in (entry or {}).get('candidates', []):
            if c.get('xpath') == xpath:
                return c.get('status', 'active')
        return None

    def quarantined(self, key, frame_xpath=None, shadow_hosts=None):
        """The selectors of key's quarantined candidates, in stored order. apply() uses them
        to refuse a proposal that selects the same element under another spelling.
        frame_xpath and shadow_hosts limit them to one place: frame_xpath '' for the page or
        an iframe's selector for that iframe, and shadow_hosts the hosts of a shadow root
        (None or [] for none). None for both lists them all."""
        entry = self._data['selectors'].get(key)
        whole = frame_xpath is None and shadow_hosts is None
        hosts = tuple(saved_hosts(list(shadow_hosts or ())))

        def here(c):
            return whole or ((c.get('frameXPath') or '') == (frame_xpath or '')
                             and saved_hosts(c.get('shadowHosts')) == hosts)
        return [c['xpath'] for c in (entry or {}).get('candidates', [])
                if c.get('status') == 'quarantined' and c.get('xpath') and here(c)]

    def _find(self, key, xpath):
        for c in self._entry(key)['candidates']:
            if c['xpath'] == xpath:
                return c
        return None

    def record_success(self, key, xpath, *, now=None):
        c = self._find(key, xpath)
        if c is None or c.get('status', 'active') != 'active':
            # Quarantined or superseded since the lookup listed it (another worker's change
            # taken in meanwhile): a success here never undoes that, whatever the row's source.
            return False
        if c.get('lastSucceededAt') is None and c.get('source') != 'seed':
            # A first success is written at once, unless another worker already recorded
            # one: then the file has it, and this one waits like any other (SOFT_EVERY).
            self.refresh(key)
            if c.get('status', 'active') != 'active':
                return False        # another worker quarantined it meanwhile
        # A change in the recency order is worth a write, and so is the first success of a
        # code or heal candidate (else mendwright report says 'last success: never' for a
        # selector that matches on every run). Seed rows are left out: writing one pins it
        # active in the override, so a quarantine published to the seed later would not
        # apply. The code-first order of set_code_candidates() is left out too, or a healed
        # fallback behind a broken code selector would rewrite the file on every resolve.
        # Returns True when the recency order changed.
        top = self._by_recency(key)[:1]
        was_top = bool(top) and top[0] is c
        first = c.get('lastSucceededAt') is None and c.get('source') != 'seed'
        self._last_success[key] = c
        c['lastSucceededAt'] = now or _now_iso()
        c['status'] = 'active'
        self._dirty.add((key, xpath))
        if not was_top or first:
            # A new order of the last successes can wait (see SOFT_EVERY); a first one cannot.
            self._persist_soft(key, {xpath: c['lastSucceededAt']}, at_once=first)
        elif (key in self._soft_waiting and not getattr(self._batch, 'depth', 0)
              and SOFT.claim_due(self._soft_path(), (key,))):
            self._persist_keys((key,))    # what waited for this key is due now
        return not was_top

    def add_candidate(self, key, xpath, *, source='heal', frame_xpath=None, css=None,
                      list_heal=None, shadow_hosts=None, get_by=None, now=None):
        """Add a candidate selector for key, or update the one with the same text.

        frame_xpath is the selector of the iframe the candidate runs in: '' moves an
        existing candidate to the page, None leaves it where it is. shadow_hosts are the
        selectors of the shadow hosts of the open shadow root it runs in, outermost first
        (see mendwright.shadow): [] moves it out of one, None leaves it. css is a CSS
        selector checked to match the same element as a healed XPath (see mendwright.css),
        saved for mendwright fix and never run: '' drops one saved before, None leaves it.
        get_by is the heal's get_by twin, a get_by_* call checked to match the healed element
        and nothing else (see client.get_by_twin), saved as getBy for mendwright fix and never
        run, with the same '' and None. list_heal True marks a heal of a list, saved as
        "list": true, as a build of 0.8.0 before its release did: list lookups use no heal
        (see Healer.locator_all), and mendwright fix writes none of these back. False clears
        the mark, None leaves it."""
        entry = self._entry(key)
        found = self._find(key, xpath)
        hosts = None if shadow_hosts is None else list(saved_hosts(list(shadow_hosts)))
        if not found:
            cand = {'xpath': xpath, 'source': source, 'addedAt': now or _now_iso(),
                    'lastSucceededAt': None, 'status': 'active'}
            if frame_xpath:
                cand['frameXPath'] = frame_xpath
            if hosts:
                cand['shadowHosts'] = hosts
            if css:
                cand['css'] = css
            if get_by:
                cand['getBy'] = get_by
            if list_heal:
                cand['list'] = True
            entry['candidates'].append(cand)
        else:
            # An empty value stays as a key, so _absorb() does not take an old one back
            # from the file; _payload() writes no empty css or shadowHosts.
            if frame_xpath is not None and (found.get('frameXPath') or '') != frame_xpath:
                found['frameXPath'] = frame_xpath
            if hosts is not None and list(saved_hosts(found.get('shadowHosts'))) != hosts:
                found['shadowHosts'] = hosts
            if css is not None and (found.get('css') or '') != css:
                found['css'] = css
            if get_by is not None and (found.get('getBy') or '') != get_by:
                found['getBy'] = get_by
            if list_heal:
                found['list'] = True
            elif list_heal is not None:
                found.pop('list', None)
        self._dirty.add((key, xpath))
        self._persist_keys((key,))

    def list_heals(self, key):
        """Copies of key's list heals that wait (proposed or accepted, see
        propose_list_heal), the newest proposal first; rows with no proposal last."""
        entry = self._data['selectors'].get(key) or {}
        rows = [copy.deepcopy(c) for c in entry.get('candidates', []) if _is_list_heal(c)]
        return sorted(rows, key=_proposal_order, reverse=True)

    def propose_list_heal(self, key, xpath, *, css=None, proposal, now=None):
        """Save xpath as a list heal proposed for key (see heal.locator_all(nonempty=True)),
        with the evidence a person checks it by (proposal, see lists.proposal_evidence) and
        its CSS form css, and return a copy of the row. The row is inactive ("status":
        "proposed", "list": true): no lookup tries it, and only edit_list_heal() accepts it.
        A row with the same XPath that was proposed or superseded is proposed again with the
        new evidence; an active heal (a list heal of a build of 0.8.0 before its release) is
        made a proposal too. Raises ValueError for a row that is accepted or quarantined, or
        an active one from code or a seed file: those are never proposed."""
        if not isinstance(proposal, dict):
            raise ValueError('mendwright: propose_list_heal() takes the proposal as a dict')
        entry = self._entry(key)
        c = self._find(key, xpath)
        if c is None:
            c = {'xpath': xpath, 'source': 'heal', 'addedAt': now or _now_iso(),
                 'lastSucceededAt': None, 'status': 'proposed', 'list': True}
            if css:
                c['css'] = css
            c['proposal'] = copy.deepcopy(proposal)
            entry['candidates'].append(c)
        else:
            status = c.get('status', 'active')
            if status in ('accepted', 'quarantined') or (
                    status == 'active' and c.get('source') != 'heal'):
                raise ValueError('mendwright: ' + repr(xpath) + ' is ' + status + ' for '
                                 + repr(key) + ', so it is not proposed again')
            if c.get('source') != 'seed':
                c['source'] = 'heal'
            c['status'] = 'proposed'
            c['list'] = True
            c['proposal'] = copy.deepcopy(proposal)
            # An empty css stays as a key, so _absorb() does not take an old one back.
            c['css'] = css or ''
            self._delisted.discard((key, xpath))
            # Proposed now, whatever the file says another process made it since (see
            # _merge_status): an acceptance or a quarantine there still wins.
            self._base.pop((key, xpath), None)
        self._dirty.add((key, xpath))
        self._persist_override()
        return copy.deepcopy(c)

    def reload_list_heals(self, key):
        """Take in what another process did to key's list heals in the override file since
        this store read or wrote it, such as mendwright lists accept or reject: the statuses
        'accepted', 'quarantined' and 'superseded', and acceptedAt, merged as a write merges
        them (see _absorb), without writing. So a store loaded before a person accepted or
        rejected a list heal doesn't propose it again. Nothing when the file can't be read."""
        entry = self._data['selectors'].get(key)
        if not entry or not any(_is_list_heal(c) for c in entry.get('candidates', [])):
            return
        try:
            disk, _native = self._disk()
        except Exception:
            return
        theirs = (disk.get('selectors') or {}).get(key)
        if not isinstance(theirs, dict):
            return
        mine = {c['xpath']: c for c in entry.get('candidates', []) if _is_list_heal(c)}
        for c in theirs.get('candidates') or ():
            if not isinstance(c, dict) or c.get('xpath') not in mine:
                continue
            local = mine[c['xpath']]
            status = _merge_status(local.get('status', 'active'), c.get('status', 'active'),
                                   self._base.get((key, c['xpath'])))
            if status == local['status'] or status not in ('accepted', 'quarantined',
                                                           'superseded'):
                continue
            local['status'] = status
            if status == c.get('status'):
                self._base[(key, c['xpath'])] = status      # as the file has it now
            if isinstance(c.get('acceptedAt'), str) and c['acceptedAt'] and not (
                    isinstance(local.get('acceptedAt'), str) and local['acceptedAt']):
                local['acceptedAt'] = c['acceptedAt']

    def retire_list_heals(self, key, selector, *, now=None):
        """End key's list heals that the selector ``selector``, which just matched, was
        replaced by: those whose proposal names it among the selectors that matched nothing
        (proposal['was']['selectors']), and those with no proposal (a 0.8.0 write dropped
        it) when selector is one of the key's selectors from code or a seed file. A proposed
        one becomes 'superseded', an accepted one 'quarantined': the list was not broken.
        Returns (xpath, old status, new status) for each row it changed, and writes the
        override file only then."""
        entry = self._data['selectors'].get(key)
        if not entry:
            return []
        own = None
        changes = []
        for c in entry.get('candidates', []):
            if not _is_list_heal(c):
                continue
            evidence = evidence_of(c)
            was = (evidence.get('was') or {}) if evidence else None
            if isinstance(was, dict) and isinstance(was.get('selectors'), list):
                hit = selector in was['selectors']
            else:
                if own is None:
                    own = {o['xpath'] for o in self.candidates(key)
                           if o.get('source') in ('code', 'seed')}
                hit = selector in own
            if not hit:
                continue
            old = c['status']
            c['status'] = 'superseded' if old == 'proposed' else 'quarantined'
            self._dirty.add((key, c['xpath']))
            changes.append((c['xpath'], old, c['status']))
        if changes:
            self._persist_override()
        return changes

    def quarantine(self, key, xpath):
        c = self._find(key, xpath)
        if c is not None:
            c['status'] = 'quarantined'
            self._dirty.add((key, xpath))
            self._persist_override()

    def _is_delta(self, key, c):
        """True if candidate c is a local override to persist: override-origin, mutated
        this session, OR diverged from the pristine seed (prior-session delta)."""
        if c.get('source') != 'seed':
            return True
        xpath = c.get('xpath')
        if (key, xpath) in self._dirty:
            return True
        seed = self._seed_state.get((key, xpath))
        if seed is None:
            # Untouched seed row (direct construction with no pristine ref), or a row the
            # current seed no longer has: do NOT persist, it must not resurrect.
            return False
        return _row_state(c) != seed

    # --- writing the override file -------------------------------------------------
    #
    # Several processes may share one override file (pytest-xdist workers, parallel jobs).
    # Each write takes a lock file, re-reads the file, takes in what other processes
    # wrote for candidates and metadata this store has not touched, and atomically
    # replaces the file. What this store changed itself wins. If the file cannot be
    # written, the store logs one warning and keeps working in memory.
    #
    # Many workers often save the same thing: each registers the code's selector and role of a
    # key the store never saw, records the first success or page of a heal they all adopted.
    # The first one writes it. For the others, a change another worker saved already is no
    # reason to write (_persist_keys looks at the file first, _file_holds), and a write that
    # would change nothing is not made (_persist_override compares under the lock).

    @contextlib.contextmanager
    def batch(self):
        """Write the override file once, when the outermost batch of this thread ends, for
        every change made in it, instead of once per change: a lookup that saves its
        selector, its first success, its page and its list fingerprint costs one write.
        Changes made in other threads are written as they come."""
        state = self._batch
        state.depth = getattr(state, 'depth', 0) + 1
        try:
            yield self
        finally:
            state.depth -= 1
            if not state.depth:
                soft, state.soft = getattr(state, 'soft', set()), set()
                maybe, state.maybe = getattr(state, 'maybe', set()), set()
                succeeded, state.succeeded = getattr(state, 'succeeded', set()), set()
                if getattr(state, 'wanted', False):
                    state.wanted = False
                    self._persist_override()
                elif maybe and self._persist_keys(maybe, succeeded):
                    pass        # the soft changes went with it
                elif soft and SOFT.claim_due(self._soft_path(), soft):
                    self._persist_keys(soft)

    def _soft_path(self):
        """The path SOFT keeps this store's soft changes under: the file's absolute path."""
        return os.path.abspath(str(self._override_path))

    # Where the store's file is read and written. A store kept elsewhere than a local file
    # (mendwright.remote) gives its own: these four, _soft_path(), _persist_override() and
    # _persist_failed().

    def _latest(self, known, max_age=0.0):
        """(data, version) of the override file now, for refresh() and _file_holds(): version
        None when there is no file, and data None when version is one of known (the file did
        not change since; it is not read) or the file is not schema 2. The file is parsed once
        per version per process (parsed_override). Raises when it cannot be read. max_age is
        for a store whose look costs a request: a look another store of the process made that
        recently will do (a file's costs one stat, so it looks every time)."""
        version = stat_version(self._override_path)
        if version is None or version in known:
            return None, version
        return parsed_override(self._override_path, version, sleep=self._sleep)

    def _disk(self):
        """(data, native) of the override file now, read afresh (see _read). Raises when it
        cannot be read."""
        return _read(self._override_path, self._destructive_keys, sleep=self._sleep)

    def _turns(self):
        """The _FileWrites of this store's file: one writer of it at a time in this process."""
        return _writes(os.path.normcase(os.path.abspath(str(self._override_path))))

    def _persist_soft(self, key, rows=None, *, at_once=False):
        """A change of key's last successes, pages, steps, address or fingerprints (see
        SOFT_EVERY). at_once=True (what the file does not have yet) writes this store now, or at
        the end of the batch, as any change. Otherwise the store is written when the key's soft
        changes are due (the first in this process: at once), and else the change waits in
        memory for the store's next write. rows {xpath: lastSucceededAt} and the key's soft
        metadata also go to SOFT, which writes them at exit if this store is gone before it
        wrote them."""
        if not self.writes:
            return
        # Only what this store changed since it last read or wrote the file, and the
        # fingerprints it replaced: a flush never writes back a value this process only read,
        # over one another process saved since (see _apply_soft).
        base = getattr(self, '_base_meta', {}).get(key, {})
        meta = {f: v for f, v in self._meta.get(key, {}).items()
                if f in SOFT_META and v != base.get(f)}
        replaced = {f: base.get(f) for f in ('fingerprint', 'listFingerprint') if f in meta}
        path = self._soft_path()
        SOFT.record(path, key, rows, meta, replaced)
        self._soft_waiting.add(key)
        state = self._batch
        if getattr(state, 'depth', 0):
            if at_once:
                state.maybe = getattr(state, 'maybe', set()) | {key}
                state.succeeded = getattr(state, 'succeeded', set()) | set(rows or ())
            state.soft = getattr(state, 'soft', set()) | {key}
            return
        # A first success that another worker recorded meanwhile (rows) waits like any other.
        if at_once and self._persist_keys((key,), succeeded=tuple(rows or ())):
            return
        if SOFT.claim_due(path, (key,)):
            self._persist_keys((key,))

    def flush(self):
        """Write now the soft changes that wait (see SOFT_EVERY): this store's own, and those
        other stores of this process left for its file. True when nothing waits any more."""
        if not self.writes:
            return True
        if self._soft_waiting:
            self._persist_override()
        return SOFT.flush(self._soft_path(), sleep=self._sleep)

    def _persist_override(self):
        if not self.writes:
            return      # no lock file, no read, no write (see read_only and writes)
        state = self._batch
        if getattr(state, 'depth', 0):
            state.wanted = True     # written when the batch ends (see batch)
            return
        writes = self._turns()
        try:
            with writes.lock:
                try:
                    with self._locked_file(), self._mutex:
                        # A file that became unreadable raises here, so it is never replaced.
                        disk, native = _read(self._override_path, self._destructive_keys,
                                             sleep=self._sleep)
                        self._absorb(disk)
                        payload = self._payload()
                        if not (native and _same_store(payload, disk)):
                            # Unless the file holds all of it already (another worker wrote
                            # the same), and is schema 2: a schema-1 file is converted.
                            self._write(json.dumps(payload, separators=(',', ':')))
                        self._remember_base(payload)
                        # Every key in memory is the file now (the lock keeps other writers
                        # out).
                        self._version = stat_version(self._override_path)
                        self._key_versions = {}
                        self._unsaved = False
                finally:
                    writes.count += 1
            # The soft changes that waited went with this write.
            if self._soft_waiting:
                SOFT.written(self._soft_path(), self._soft_waiting)
                self._soft_waiting = set()
        except Exception as exc:
            self._unsaved = True
            self._persist_failed(exc)

    def _persist_keys(self, keys, succeeded=()):
        """Write this store for a change of keys, unless the override file holds it already:
        another worker saved the same selector, role, page, success or heal (_file_holds;
        succeeded are the selectors whose first success is the change). A store looks at the
        file before it waits for this process's turn to write and again when it has it, so of
        the stores of a process that save one change at once, the first writes it. In a batch,
        the look is made when the batch ends. False when nothing was written for it."""
        if not self.writes:
            return False
        state = self._batch
        if getattr(state, 'depth', 0):
            state.maybe = getattr(state, 'maybe', set()) | set(keys)
            return True
        if self._file_holds(keys, succeeded):
            return False
        writes = self._turns()
        for _ in range(8):
            seen = writes.count
            with writes.lock:
                if writes.count == seen:
                    # No other store of this process wrote the file while this one waited.
                    self._persist_override()
                    return True
            # One did: look at the file again, out of the lock, in parallel with the others.
            if self._file_holds(keys, succeeded):
                return False
        self._persist_override()
        return True

    def _file_holds(self, keys, succeeded=()):
        """True when the override file holds what a write of this store would save for keys,
        as another worker saved it (see _same_entry). Each key is merged from the file into
        memory first, as refresh() merges it. False when the file did not change since this
        store last read or wrote it (it lacks this store's change then), when it cannot be
        read, and after a write of this store failed (_unsaved): that write is made again."""
        if self._unsaved:
            return False
        keys = list(keys)
        known = tuple(self._key_versions.get(k, self._version) for k in keys)
        try:
            data, version = self._latest(known)
        except Exception:
            return False
        if version is None or version in known or data is None:
            return False        # (data None: schema 1, which the next write converts)
        theirs = data.get('selectors') or {}
        with self._mutex:
            for key in keys:
                entry = theirs.get(key)
                if isinstance(entry, dict):
                    entry = copy.deepcopy(entry)    # the parse is shared by every store
                    self._absorb_key(key, entry)
                    self._remember_key_base(key, entry)
                    self._key_versions[key] = version
            return all(_same_entry(self._payload_entry(k), theirs.get(k), succeeded)
                       for k in keys)

    # --- reading what other workers saved (read-through) ---------------------------
    #
    # A store reads the override file when it loads, and again under the lock each time it
    # writes. In between, refresh(key) takes in what other workers (threads with stores of
    # their own, processes, machines sharing the file) saved for one key: their heals, their
    # quarantines, a destructive flag. A lookup calls it before it uses a selector a heal or
    # a seed file saved, about once a second while a key keeps missing, and a heal calls it
    # before its fingerprint tier and before asking a model (see resolver.resolve and
    # HealCoordinator.try_heal).

    def refresh(self, key, *, max_age=0.0):
        """Take in what the override file holds for key now, if it changed since this store
        last read or wrote it, with the merge rules of a write (see _absorb): a quarantine
        wins, then an acceptance; destructive True wins; the newest lastSucceededAt wins.
        Returns True when key's candidates or metadata changed in memory.

        Cheap when nothing changed: one stat of the file. When it did, the file is parsed
        once per version per process however many stores refresh from it (parsed_override),
        and only key is merged. max_age > 0 skips the look entirely when this store looked for
        key less than max_age seconds ago. A read-only store, a file that is gone, and one
        that cannot be read (it is never taken for an empty store) leave memory as it is."""
        if self._read_only:
            return False
        if max_age:
            now = self._clock()
            last = self._looked.get(key)
            if last is not None and now - last < max_age:
                return False
            self._looked[key] = now
        known = self._key_versions.get(key, self._version)
        try:
            data, version = self._latest((known,), max_age)
        except Exception:
            return False        # unreadable now: keep what memory holds
        if version is None or version == known:
            return False
        entry = (data or {}).get('selectors', {}).get(key) if data is not None else None
        with self._mutex:
            changed = False
            if isinstance(entry, dict):
                entry = copy.deepcopy(entry)    # the parse is shared by every store
                changed = self._absorb_key(key, entry)
                self._remember_key_base(key, entry)
            self._key_versions[key] = version
        return changed

    def arrived(self, key, xpath):
        """True for a heal of key that this store took in from the file after it read it
        (refresh() or a write's merge): one another worker saved meanwhile, which a lookup
        here has not checked or used yet (see Healer's adoption)."""
        return (key, xpath) in self._arrived

    def _persist_failed(self, exc):
        detail = type(exc).__name__ + (': ' + str(exc) if str(exc) else '')
        if self._warned:
            logger.debug('mendwright: still cannot save %s (%s)', self._override_path, detail)
            return
        self._warned = True
        if isinstance(exc, StoreReadError):
            logger.warning(
                'mendwright is not saving selector changes to %s, because the file there '
                'cannot be read now: %s It keeps working in memory, and leaves the file as it '
                'is. Fix or delete the file.', self._override_path, str(exc))
            return
        logger.warning(
            'mendwright could not save selector changes to %s (%s). It keeps working in '
            'memory, but changes made now are lost when the process ends. Point it at a '
            'writable file: store_path= or MENDWRIGHT_STORE for mendwright.open(), or the '
            'override path passed to load_store().', self._override_path, detail)

    def _locked_file(self):
        """The lock on the override file for a write (see _locked)."""
        return _locked(self._override_path, timeout=self.lock_timeout,
                       stale_after=self.lock_stale_after, clock=self._clock, sleep=self._sleep,
                       wallclock=self._wallclock)

    def _remember_base(self, data):
        """Record the candidate statuses, places (frameXPath, css) and metadata of an
        override file's content."""
        self._base = {}
        self._base_place = {}
        self._base_meta = {}
        self._base_index = {}
        for key, entry in (data.get('selectors') or {}).items():
            if isinstance(entry, dict):
                self._remember_key_base(key, entry)

    def _remember_key_base(self, key, entry):
        """Record one key's part of the merge base (see _remember_base): what the file holds
        for key now, in place of what it held before."""
        index = getattr(self, '_base_index', None)
        if index is None:
            index = self._base_index = {}
        for xpath in index.pop(key, ()):
            self._base.pop((key, xpath), None)
            self._base_place.pop((key, xpath), None)
        xpaths = set()
        for c in entry.get('candidates') or ():
            if isinstance(c, dict) and isinstance(c.get('xpath'), str):
                self._base[(key, c['xpath'])] = c.get('status', 'active')
                self._base_place[(key, c['xpath'])] = _place(c)
                xpaths.add(c['xpath'])
        index[key] = xpaths
        self._base_meta[key] = _meta_fields(entry)

    def _key_state(self, key):
        """What refresh() compares to tell whether a merge changed key in memory."""
        e = self._data['selectors'].get(key)
        if not isinstance(e, dict):
            return None
        rows = [(c.get('xpath'), c.get('source'), c.get('status', 'active'),
                 c.get('lastSucceededAt')) + _place(c)
                for c in e.get('candidates', []) if isinstance(c, dict)]
        return rows, dict(self._meta.get(key, {}))

    def _absorb(self, disk):
        """Take in what other processes wrote to the override file since this store last
        read or wrote it, with a three-way merge against that earlier content:

        - a value only the other side changed is taken from the file;
        - a value only this store changed is kept;
        - when both changed a status, 'quarantined' wins (a quarantine is never undone by
          a stale view), otherwise this store's;
        - when both changed the destructive flag, True wins;
        - lastSucceededAt is the newer of the two;
        - a candidate's frameXPath, css, shadowHosts and getBy follow the same rule as a
          value: the file's when only the other side changed them, this store's otherwise;
        - a list heal's proposal is the one with the later 'at' (the file's when this store
          has none), and its acceptedAt the file's when this store has none, but not for a
          list heal this store took over as code (see set_code_candidates).
        New keys and candidates from the file are added."""
        for key, entry in (disk.get('selectors') or {}).items():
            if isinstance(entry, dict):
                self._absorb_key(key, entry)

    def _absorb_key(self, key, entry):
        """_absorb() for one key and its entry in the file. Returns True when key's
        candidates or metadata changed in memory. A heal it adds is remembered as arrived."""
        before = self._key_state(key)
        mine = self._data['selectors'].get(key)
        if mine is None:
            mine = self._entry(key)
            mine['candidates'] = []
        for c in entry.get('candidates') or ():
            if not isinstance(c, dict) or not isinstance(c.get('xpath'), str):
                continue
            xpath = c['xpath']
            local = self._find(key, xpath)
            if local is None:
                mine['candidates'].append(dict(c))
                if c.get('source') == 'heal':
                    self._arrived.add((key, xpath))
                continue
            local['status'] = _merge_status(local.get('status', 'active'),
                                            c.get('status', 'active'),
                                            self._base.get((key, xpath)))
            newest = max(local.get('lastSucceededAt') or '', c.get('lastSucceededAt') or '')
            local['lastSucceededAt'] = newest or None
            _merge_place(local, c, getattr(self, '_base_place', {}).get((key, xpath)))
            if (key, xpath) in self._delisted:
                continue                 # taken over as code here: no list heal any more
            if c.get('list') is True:
                local['list'] = True     # another process healed a list with it
            theirs = evidence_of(c)
            if theirs is not None:
                mine_evidence = evidence_of(local)
                if mine_evidence is None or theirs['at'] > mine_evidence['at']:
                    local['proposal'] = copy.deepcopy(theirs)
            if isinstance(c.get('acceptedAt'), str) and c['acceptedAt'] and not (
                    isinstance(local.get('acceptedAt'), str) and local['acceptedAt']):
                local['acceptedAt'] = c['acceptedAt']
        merged = _merge_meta(self._meta.get(key, {}), _meta_fields(entry),
                             self._base_meta.get(key, {}))
        if merged != self._meta.get(key, {}):
            gone = set(self._meta.get(key, {})) - set(merged)
            self._meta[key] = merged
            self._overlay_meta(key, merged)
            self._drop_meta(key, gone)
        return self._key_state(key) != before

    def _payload(self):
        """The override file's content: per key, the candidates worth keeping (see
        _is_delta) and the metadata saved with set_meta(). Active candidates are capped at
        maxCandidates, most recently succeeded first. Inactive ones (quarantined,
        superseded) get their own cap, except a quarantined seed row, which is always
        kept so the seed cannot bring it back. List heals that wait (proposed, accepted)
        are kept apart from them, at most MAX_LIST_HEALS: an accepted one first, then the
        newest proposals."""
        selectors = {}
        for key in self._data['selectors']:
            entry = self._payload_entry(key)
            if entry is not None:
                selectors[key] = entry
        return {'schema': 2, 'selectors': selectors}

    def _payload_entry(self, key):
        """What the override file holds for key after a write of this store (see _payload),
        or None when it holds nothing for key."""
        e = self._data['selectors'].get(key)
        if not isinstance(e, dict):
            return None
        cap = e.get('maxCandidates', 5)
        persistable = [c for c in e.get('candidates', []) if self._is_delta(key, c)]

        def recency(c):
            return c.get('lastSucceededAt') or c.get('addedAt') or ''

        active = sorted((c for c in persistable if c.get('status', 'active') == 'active'),
                        key=lambda c: c.get('lastSucceededAt') or '', reverse=True)[:cap]
        inactive = [c for c in persistable if c.get('status', 'active') != 'active']
        # An acceptance is a person's choice: newer proposals never push it out.
        waiting = sorted((c for c in inactive if c.get('status') in LIST_WAITING),
                         key=lambda c: (c.get('status') == 'accepted',) + _proposal_order(c),
                         reverse=True)[:MAX_LIST_HEALS]
        inactive = [c for c in inactive if c.get('status') not in LIST_WAITING]
        pinned = [c for c in inactive if c.get('source') == 'seed']
        others = sorted((c for c in inactive if c.get('source') != 'seed'),
                        key=recency, reverse=True)[:cap]
        overlay = []
        for c in active + pinned + others + waiting:
            item = {k: c[k] for k in ('xpath', 'source', 'addedAt', 'lastSucceededAt', 'status') if k in c}
            if 'frameXPath' in c:
                item['frameXPath'] = c['frameXPath']
            if isinstance(c.get('shadowHosts'), list) and c['shadowHosts']:
                item['shadowHosts'] = list(c['shadowHosts'])
            if isinstance(c.get('css'), str) and c['css']:
                item['css'] = c['css']
            if isinstance(c.get('getBy'), str) and c['getBy']:
                item['getBy'] = c['getBy']
            if c.get('list') is True:
                item['list'] = True
            evidence = evidence_of(c)
            if evidence is not None:
                item['proposal'] = evidence
            if isinstance(c.get('acceptedAt'), str) and c['acceptedAt']:
                item['acceptedAt'] = c['acceptedAt']
            overlay.append(item)
        saved = {f: v for f, v in self._meta.get(key, {}).items() if f in META_FIELDS}
        if overlay or saved:
            return dict(saved, candidates=overlay)
        return None

    def _write(self, payload):
        """Write payload to the override file atomically (see _atomic_write)."""
        _atomic_write(self._override_path, payload, self._sleep)


def _same_row(mine, theirs, succeeded=False):
    """Whether two rows of one selector say the same, whatever addedAt each gives it: two
    workers that added one selector each gave it the time they added it. succeeded: the change
    at hand is the row's first success, which theirs holds when it has any success of the row
    (a later one waits like any other, see SOFT_EVERY)."""
    left_out = ('addedAt', 'lastSucceededAt') if succeeded else ('addedAt',)
    if succeeded and not theirs.get('lastSucceededAt'):
        return False
    return ({k: v for k, v in mine.items() if k not in left_out}
            == {k: v for k, v in theirs.items() if k not in left_out})


def _same_entry(mine, theirs, succeeded=()):
    """Whether theirs, a key's entry in the override file, holds what a write would save for
    the key: mine, from SelectorStore._payload_entry (None for nothing). The same metadata, and
    the same rows by selector, in any order (rows that tie on their last success sort either
    way) and whatever addedAt each gives them (see _same_row). succeeded: the selectors whose
    first success is the change at hand."""
    if mine is None or theirs is None:
        return mine is None and theirs is None
    if not isinstance(theirs, dict):
        return False
    if ({k: v for k, v in mine.items() if k != 'candidates'}
            != {k: v for k, v in theirs.items() if k != 'candidates'}):
        return False
    rows, others = mine.get('candidates') or [], theirs.get('candidates')
    if not isinstance(others, list) or len(others) != len(rows):
        return False
    found = {}
    for c in others:
        if not isinstance(c, dict) or not isinstance(c.get('xpath'), str) or c['xpath'] in found:
            return False
        found[c['xpath']] = c
    return all(c['xpath'] in found and _same_row(c, found[c['xpath']], c['xpath'] in succeeded)
               for c in rows)


def _same_store(payload, disk):
    """Whether disk, the override file's content, holds what writing payload would save (see
    _same_entry), so the write would change nothing."""
    mine, theirs = payload.get('selectors') or {}, disk.get('selectors') or {}
    return mine.keys() == theirs.keys() and all(_same_entry(mine[k], theirs[k]) for k in mine)


def _merge_status(mine, theirs, base):
    """One candidate's status after a three-way merge (see SelectorStore._absorb). When
    both sides changed it, 'quarantined' wins, then 'accepted' (a person's acceptance is
    never undone by a process that loaded the store before it), else this store's."""
    if mine == theirs:
        return mine
    if base is not None and mine == base:
        return theirs          # only the file changed
    if base is not None and theirs == base:
        return mine            # only this store changed
    if 'quarantined' in (mine, theirs):
        return 'quarantined'
    if 'accepted' in (mine, theirs):
        return 'accepted'
    return mine


def _merge_meta(mine, theirs, base):
    """Saved metadata after a three-way merge (see SelectorStore._absorb). When both sides
    recorded pages (or steps) for a key, both sets are kept (this store's first), so a page
    another process found the key on is not lost."""
    out = {}
    for field in META_FIELDS:
        m, t, b = mine.get(field), theirs.get(field), base.get(field)
        if m == b:
            value = t
        elif t == b:
            value = m
        elif field == 'destructive':
            value = bool(m) or bool(t)
        elif field == 'pages':
            value = list(dict.fromkeys(_pages(m) + _pages(t)))[:MAX_PAGES] or None
        elif field == 'steps':
            value = list(dict.fromkeys(_pages(m) + _pages(t)))[:MAX_STEPS] or None
        else:
            value = m
        if value is not None:
            out[field] = value
    return out


# --- mendwright lists accept / reject ----------------------------------------------------------
#
# The command line edits the override file in place, under the lock every store write takes:
# it changes the status of the rows it is about and nothing else, and never rebuilds the file
# through a SelectorStore, whose write leaves out seed rows it did not load (a quarantine a
# seed row has in the file would be dropped, and the seed would bring the selector back).

class ListHealError(Exception):
    """mendwright lists accept or reject could not do what it was asked. The message says
    why and what to do; the command line prints it after 'mendwright lists: '."""


@dataclasses.dataclass
class ListHealEdit:
    """What edit_list_heal() did: the key, 'accept' or 'reject', the row's XPath, its
    proposal's id (None for one without), a copy of the row after the edit, and the XPaths
    of the key's other list heals an accept superseded."""
    key: str
    action: str
    xpath: str
    id: str | None
    row: dict
    superseded: list = dataclasses.field(default_factory=list)


def _ids(rows):
    return [evidence_of(c)['id'] for c in rows if evidence_of(c) is not None]


def _and(items):
    items = list(items)
    return ', '.join(items[:-1]) + ' and ' + items[-1] if len(items) > 1 else ''.join(items)


def _pick(key, action, proposal_id, proposed, accepted, flag, seeded=False):
    """The row of key that edit_list_heal() changes, or ListHealError. seeded: the key's
    selectors come from a seed file, so an accepted one waits for a person, not mendwright
    fix."""
    from .cli import _when, command_key
    word, note = command_key(key)
    if proposal_id is not None:
        # reject also takes an accepted one by its id.
        for c in proposed + (accepted if action == 'reject' else []):
            if (evidence_of(c) or {}).get('id') == proposal_id:
                return c
    elif action == 'reject':
        waiting = proposed or accepted
        if len(waiting) == 1:
            return waiting[0]
        if len(waiting) > 1:
            raise ListHealError(repr(key) + ' has ' + str(len(waiting)) + ' list heals waiting: '
                                'give the id of the one to reject (' + ', '.join(_ids(waiting))
                                + ').')
    if not proposed:
        message = repr(key) + ' has no list heal to check.'
        if accepted:
            when = accepted[0].get('acceptedAt')
            message += (' Its list heal was accepted on ' + (_when(when) if when else 'date unknown')
                        + (' and waits for you to put it in the seed file its selectors come '
                           'from' if seeded else ' and waits for mendwright fix --apply')
                        + '; to drop it, run: mendwright lists reject ' + word + flag + note)
        raise ListHealError(message)
    ids = _ids(proposed)
    if not ids:
        raise ListHealError('the list heal of ' + repr(key) + ' has no saved evidence, because '
                            'an older mendwright wrote the store after it was found. Run the '
                            'test again with mendwright 0.9.0 to find it anew.')
    message = repr(key) + ' has no list heal with id ' + str(proposal_id) + '.'
    if len(ids) == 1:
        message += ' It changed since you looked: run mendwright lists' + flag + ' to see it again.'
    else:
        message += (' The ones waiting are ' + _and(ids) + ': run mendwright lists' + flag
                    + ' to see them.')
    raise ListHealError(message)


def edit_list_heal(path, key, action, proposal_id=None, *, now=None):
    """Accept or reject a list heal of key in the override file at path (mendwright lists
    accept KEY ID, mendwright lists reject KEY [ID]). Returns a ListHealEdit; raises
    ListHealError, with the message to print, when it cannot.

    accept takes the proposed row whose proposal id is proposal_id, makes it 'accepted' with
    acceptedAt now, and makes the key's other proposed or accepted list heals 'superseded'.
    reject takes the proposed row with that id, or with no id the key's only proposed row,
    else its only accepted one, and quarantines it, so the list gate refuses it and every
    selector that takes its elements. The file is read and written under its lock, and only
    those rows change: every other key and row stays as it is on disk. The journal next to
    the file gets heal.accepted, or heal.quarantine."""
    if action not in ('accept', 'reject'):
        raise ValueError('mendwright: edit_list_heal() takes accept or reject, got ' + repr(action))
    from .cli import _from_seed, store_flag
    from .journal import Journal
    path = Path(_expand(path))
    shown = str(path)
    flag = store_flag(shown)
    if not path.is_file():
        raise ListHealError('no store at ' + shown + '.')
    try:
        with _locked(path, timeout=SelectorStore.lock_timeout,
                     stale_after=SelectorStore.lock_stale_after):
            data, native = _read(path)
            if not native:
                if data.get('selectors'):
                    raise ListHealError(shown + ' was written by an older mendwright (schema 1). '
                                        'Run your tests once with this version to convert it.')
                raise ListHealError('no key ' + repr(key) + ' in ' + shown + '. Run mendwright '
                                    'lists' + flag + ' to see the keys with a list heal to check.')
            entry = data['selectors'].get(key)
            if not isinstance(entry, dict):
                raise ListHealError('no key ' + repr(key) + ' in ' + shown + '. Run mendwright '
                                    'lists' + flag + ' to see the keys with a list heal to check.')
            rows = [c for c in entry.get('candidates') or ()
                    if isinstance(c, dict) and c.get('list') is True
                    and isinstance(c.get('xpath'), str)]
            proposed = sorted((c for c in rows if c.get('status') == 'proposed'),
                              key=_proposal_order, reverse=True)
            accepted = sorted((c for c in rows if c.get('status') == 'accepted'),
                              key=_proposal_order, reverse=True)
            target = _pick(key, action, proposal_id, proposed, accepted, flag,
                           _from_seed(entry.get('candidates') or ()))
            superseded = []
            if action == 'accept':
                target['status'] = 'accepted'
                target['acceptedAt'] = now or _now_iso()
                for c in proposed + accepted:
                    if c is not target:
                        c['status'] = 'superseded'
                        superseded.append(c['xpath'])
            else:
                target['status'] = 'quarantined'
            _atomic_write(path, json.dumps(data, separators=(',', ':')))
    except TimeoutError:
        raise ListHealError('could not save the change to ' + shown + ': the lock file '
                            + shown + '.lock is held by another process. Try again when your '
                            'tests have finished.') from None
    except (OSError, StoreReadError) as exc:
        if isinstance(exc, StoreReadError):
            raise ListHealError(str(exc)[len('mendwright: '):] if str(exc).startswith(
                'mendwright: ') else str(exc)) from None
        raise ListHealError('could not save the change to ' + shown + ' (' + type(exc).__name__
                            + ': ' + str(exc) + ').') from None
    evidence = evidence_of(target)
    journal = Journal(path.parent / 'journal.jsonl')
    if action == 'accept':
        journal.accepted(key, xpath=target['xpath'], id=evidence['id'] if evidence else None,
                         list=True)
    else:
        journal.quarantine(key, xpath=target['xpath'], list=True,
                           reason='rejected with mendwright lists reject')
    return ListHealEdit(key, action, target['xpath'], evidence['id'] if evidence else None,
                        copy.deepcopy(target), superseded)


def _expand(path):
    """A path with '~' expanded, so '~/.myapp/overrides.json' means the home directory
    rather than a folder named '~' in the current directory."""
    return os.path.expanduser(os.fspath(path))


def _call_keys(data, path):
    """A seed's data with each key written as a get_by_* call in another spelling than
    mendwright's renamed to the call's key (getby.canonical_key), so its flags and
    candidates apply to the call. Raises StoreReadError when two keys name one call."""
    selectors = data.get('selectors')
    if not isinstance(selectors, dict):
        return data
    renamed, spelled = {}, {}
    for key, entry in selectors.items():
        getby.warn_on_object(str(path), key)
        canonical = getby.canonical_key(key)
        if canonical in spelled:
            raise StoreReadError(getby.two_spellings_message(str(path), spelled[canonical], key))
        spelled[canonical] = key
        renamed[canonical] = entry
    if renamed.keys() == selectors.keys():
        return data
    return dict(data, selectors=renamed)


def _is_remote(override_path):
    """True for a store URL (see backends.url.is_store_url) or a StoreBackend object."""
    from .backends.url import is_store_url
    if is_store_url(override_path):
        return True
    return all(callable(getattr(override_path, name, None))
               for name in ('get', 'put', 'delete', 'describe'))


def load_store(seed_path, override_path, destructive_keys=frozenset(), *, read_only=False,
               writes=True):
    """A SelectorStore over a read-only seed file and a writable override file.

    seed_path=None means no seed. A seed path that does not exist raises
    FileNotFoundError: its destructive flags would silently be lost. A missing override
    file is an empty store, created on the first write. A seed or override file that
    exists but is not valid JSON, or not a selector file, raises StoreReadError (a
    ValueError) naming the file, and so does a seed with two keys for one get_by_* call.
    Relative paths are read from the current directory, and '~' is expanded.
    read_only=True gives a store that never writes the override file: changes stay in
    memory (`mendwright check` reads its store this way).

    writes=False gives a store for a read-only worker (see SelectorStore.writes): it reads
    the override file and takes in what other workers save, and never writes it.

    override_path may also be a store URL (s3://bucket/prefix/, https://host/path/, memory:)
    or a StoreBackend: the override file is then overrides.json there (see
    mendwright.remote.load_remote_store), and the seed file stays a local file."""
    if _is_remote(override_path):
        from .remote import load_remote_store
        return load_remote_store(seed_path, override_path, destructive_keys, read_only=read_only,
                                 writes=writes)
    override_path = _expand(override_path)
    if seed_path is None:
        seed = _empty()
    else:
        seed_path = _expand(seed_path)
        if _read_text(seed_path) is None:
            raise FileNotFoundError(
                'mendwright: seed file ' + seed_path + ' does not exist (relative paths are '
                'read from ' + os.getcwd() + '). Pass an absolute path, or seed_path=None '
                'for no seed.')
        seed = _call_keys(_read_schema2(seed_path, destructive_keys), seed_path)
    override, native, version = _read_versioned(override_path, destructive_keys)
    # Saved metadata is read only from a schema-2 override. A schema-1 file migrated here
    # has inferred metadata, not metadata someone set.
    return SelectorStore(merge(seed, override), override_path, destructive_keys, seed=seed,
                         override=override if native else None, read_only=read_only,
                         file_version=version if native else None, writes=writes)
