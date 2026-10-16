"""Two-layer selector store: a read-only seed file and a writable override file. Each key
holds an ordered list of candidate selectors, so old selectors are kept and a page that
reverts, or serves an A/B variant, still matches without a model call. The store never
writes to the seed file.

Tool-agnostic: the set of destructive keys is injected (``destructive_keys``) rather
than hardcoded, so the store can serve any tool.

Per-key metadata set with ``set_meta`` (expectedRole, expectedText, destructive, intent,
fingerprint and listFingerprint) is saved in the override file next to the candidates, so the next process
sees it, and so is where the key's element was last found (``set_last_page``: lastPage
and pages) and the steps of run_steps() it was found during (``set_last_step``: steps).
The file stays schema 2; the fields are optional. A key is destructive when the seed says
so, when it is in ``destructive_keys``, or when ``set_meta`` marked it.
``set_meta(destructive=False)`` only clears the last of these.

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

from .fingerprint import valid as _valid_fingerprint
from .lists import valid as _valid_list_fingerprint
from .roles import infer_role  # noqa: F401  (re-exported: mendwright.store.infer_role)
from .selectors import frame_spelling

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


_PLACE_FIELDS = ('frameXPath', 'css')


def _place(c):
    """(frameXPath, css) of a candidate, '' for either one it doesn't have."""
    return tuple(c.get(field) or '' for field in _PLACE_FIELDS)


def _row_state(c):
    """What a seed row is compared on to tell whether this store changed it."""
    return (c.get('status', 'active'), c.get('lastSucceededAt')) + _place(c)


def _merge_place(local, theirs, base):
    """Three-way merge of a candidate's frameXPath and css (see SelectorStore._absorb).
    base is the pair the store last read or wrote, or None when it never saw the row."""
    for i, field in enumerate(_PLACE_FIELDS):
        if base is None:
            if field in theirs and field not in local:
                local[field] = theirs[field]
        elif (local.get(field) or '') == base[i] and (theirs.get(field) or '') != base[i]:
            local[field] = theirs.get(field) or ''


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
    sleep = sleep or time.sleep
    delay = 0.01
    for attempt in range(_READ_TRIES):
        try:
            return Path(path).read_text(encoding='utf-8-sig')
        except (FileNotFoundError, NotADirectoryError):
            return None
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
    return None  # not reached


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
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except (FileExistsError, PermissionError) as exc:
            # On Windows a lock file that is being deleted gives PermissionError for a
            # moment. With no lock file there, it is a real permission problem.
            if isinstance(exc, PermissionError) and not lock.exists():
                raise
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
    text = _read_text(path, sleep)
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
        return _respell_frame_heals(data), True
    if isinstance(data, dict) and data.get('schema') in (None, 1) and isinstance(selectors, list):
        return migrate_schema1(data, destructive_keys), False
    raise StoreReadError(
        'mendwright: ' + str(path) + ' is not a selector file (expected {"schema": 2, '
        '"selectors": {...}}). Point mendwright at the right file, or delete this one.')


# Metadata fields saved in the override file: the ones set_meta() sets, the pages
# set_last_page() records and the steps set_last_step() records. intent is what the element
# is for, in words (locator(intent=)); fingerprint is what the element looked like the last
# time it matched (see mendwright.fingerprint), and listFingerprint what the items of a
# list looked like the last time it matched two or more (see mendwright.lists).
META_FIELDS = ('expectedRole', 'expectedText', 'destructive', 'lastPage', 'pages', 'intent',
               'fingerprint', 'steps', 'listFingerprint')

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
    return out


def _pages(value):
    """A saved 'pages' list, or [] when it is missing or malformed."""
    if isinstance(value, list) and all(isinstance(p, str) and p for p in value):
        return list(value)[:MAX_PAGES]
    return []


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

    def __init__(self, data, override_path, destructive_keys=frozenset(), seed=None,
                 override=None):
        self._data = data
        self._override_path = Path(_expand(override_path))
        self._destructive_keys = frozenset(destructive_keys)
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
        for name in ('lastPage', 'pages', 'steps', 'intent', 'fingerprint', 'listFingerprint'):
            if name in fields:
                e[name] = list(fields[name]) if name in ('pages', 'steps') else fields[name]

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
        code; an accepted list heal whose CSS form is passed is superseded, for the same
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
            self._persist_override()

    def _entry(self, key):
        return self._data['selectors'].setdefault(key, {
            'expectedRole': 'unknown', 'expectedText': [], 'destructive': key in self._destructive_keys,
            'maxCandidates': 5, 'stage': None, 'action': None, 'candidates': []})

    def set_meta(self, key, *, role=None, text=None, destructive=None, stage=None, action=None,
                 intent=None, fingerprint=None, list_fingerprint=None):
        """Set a key's metadata. None leaves a field as it is.

        role, text, destructive, intent, fingerprint and list_fingerprint are saved in the
        override file, so the next process sees them; the file is written only when one of
        them changes. destructive=False clears a flag an earlier set_meta() saved, but never
        one from the seed or from destructive_keys: meta() still reports those keys as
        destructive. intent is a description of the element in words; fingerprint a dict
        from mendwright.fingerprint and list_fingerprint one from mendwright.lists (anything
        else raises ValueError). stage and action stay in memory."""
        e = self._entry(key)
        changed = {}
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
        if any(saved.get(f) != v for f, v in changed.items()):
            self._meta[key] = dict(saved, **changed)
            self._persist_override()

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
                self._persist_override()

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
                self._persist_override()

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

    def quarantined(self, key, frame_xpath=None):
        """The selectors of key's quarantined candidates, in stored order. apply() uses them
        to refuse a proposal that selects the same element under another spelling.
        frame_xpath limits them to one place: '' for the page, an iframe's selector for that
        iframe. None lists them all."""
        entry = self._data['selectors'].get(key)
        return [c['xpath'] for c in (entry or {}).get('candidates', [])
                if c.get('status') == 'quarantined' and c.get('xpath')
                and (frame_xpath is None or (c.get('frameXPath') or '') == frame_xpath)]

    def _find(self, key, xpath):
        for c in self._entry(key)['candidates']:
            if c['xpath'] == xpath:
                return c
        return None

    def record_success(self, key, xpath, *, now=None):
        c = self._find(key, xpath)
        if c is None:
            return False
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
            self._persist_override()
        return not was_top

    def add_candidate(self, key, xpath, *, source='heal', frame_xpath=None, css=None,
                      list_heal=None, now=None):
        """Add a candidate selector for key, or update the one with the same text.

        frame_xpath is the selector of the iframe the candidate runs in: '' moves an
        existing candidate to the page, None leaves it where it is. css is a CSS selector
        checked to match the same element as a healed XPath (see mendwright.css), saved for
        mendwright fix and never run: '' drops one saved before, None leaves it. list_heal
        True marks a heal of a list, saved as "list": true, as a build of 0.8.0 before its
        release did: list lookups use no heal (see Healer.locator_all), and mendwright fix
        writes none of these back. False clears the mark, None leaves it."""
        entry = self._entry(key)
        found = self._find(key, xpath)
        if not found:
            cand = {'xpath': xpath, 'source': source, 'addedAt': now or _now_iso(),
                    'lastSucceededAt': None, 'status': 'active'}
            if frame_xpath:
                cand['frameXPath'] = frame_xpath
            if css:
                cand['css'] = css
            if list_heal:
                cand['list'] = True
            entry['candidates'].append(cand)
        else:
            # An empty value stays as a key, so _absorb() does not take an old one back
            # from the file; _payload() writes no empty css.
            if frame_xpath is not None and (found.get('frameXPath') or '') != frame_xpath:
                found['frameXPath'] = frame_xpath
            if css is not None and (found.get('css') or '') != css:
                found['css'] = css
            if list_heal:
                found['list'] = True
            elif list_heal is not None:
                found.pop('list', None)
        self._dirty.add((key, xpath))
        self._persist_override()

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
            disk, _native = _read(self._override_path, self._destructive_keys, sleep=self._sleep)
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
            if not state.depth and getattr(state, 'wanted', False):
                state.wanted = False
                self._persist_override()

    def _persist_override(self):
        state = self._batch
        if getattr(state, 'depth', 0):
            state.wanted = True     # written when the batch ends (see batch)
            return
        try:
            with self._locked_file():
                # A file that became unreadable raises here, so it is never replaced.
                disk, _native = _read(self._override_path, self._destructive_keys,
                                      sleep=self._sleep)
                self._absorb(disk)
                payload = self._payload()
                self._write(json.dumps(payload, separators=(',', ':')))
                self._remember_base(payload)
        except Exception as exc:
            self._persist_failed(exc)

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
        for key, entry in (data.get('selectors') or {}).items():
            if not isinstance(entry, dict):
                continue
            for c in entry.get('candidates') or ():
                if isinstance(c, dict) and isinstance(c.get('xpath'), str):
                    self._base[(key, c['xpath'])] = c.get('status', 'active')
                    self._base_place[(key, c['xpath'])] = _place(c)
            self._base_meta[key] = _meta_fields(entry)

    def _absorb(self, disk):
        """Take in what other processes wrote to the override file since this store last
        read or wrote it, with a three-way merge against that earlier content:

        - a value only the other side changed is taken from the file;
        - a value only this store changed is kept;
        - when both changed a status, 'quarantined' wins (a quarantine is never undone by
          a stale view), otherwise this store's;
        - when both changed the destructive flag, True wins;
        - lastSucceededAt is the newer of the two;
        - a candidate's frameXPath and css follow the same rule as a value: the file's when
          only the other side changed them, this store's otherwise;
        - a list heal's proposal is the one with the later 'at' (the file's when this store
          has none), and its acceptedAt the file's when this store has none, but not for a
          list heal this store took over as code (see set_code_candidates).
        New keys and candidates from the file are added."""
        for key, entry in (disk.get('selectors') or {}).items():
            if not isinstance(entry, dict):
                continue
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
                self._meta[key] = merged
                self._overlay_meta(key, merged)

    def _payload(self):
        """The override file's content: per key, the candidates worth keeping (see
        _is_delta) and the metadata saved with set_meta(). Active candidates are capped at
        maxCandidates, most recently succeeded first. Inactive ones (quarantined,
        superseded) get their own cap, except a quarantined seed row, which is always
        kept so the seed cannot bring it back. List heals that wait (proposed, accepted)
        are kept apart from them, at most MAX_LIST_HEALS: an accepted one first, then the
        newest proposals."""
        selectors = {}
        for key, e in self._data['selectors'].items():
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
                if isinstance(c.get('css'), str) and c['css']:
                    item['css'] = c['css']
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
                selectors[key] = dict(saved, candidates=overlay)
        return {'schema': 2, 'selectors': selectors}

    def _write(self, payload):
        """Write payload to the override file atomically (see _atomic_write)."""
        _atomic_write(self._override_path, payload, self._sleep)


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


def _pick(key, action, proposal_id, proposed, accepted, flag):
    """The row of key that edit_list_heal() changes, or ListHealError."""
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
                        + ' and waits for mendwright fix --apply; to drop it, run: mendwright '
                        'lists reject ' + word + flag + note)
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
    from .cli import store_flag
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
            target = _pick(key, action, proposal_id, proposed, accepted, flag)
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


def load_store(seed_path, override_path, destructive_keys=frozenset()):
    """A SelectorStore over a read-only seed file and a writable override file.

    seed_path=None means no seed. A seed path that does not exist raises
    FileNotFoundError: its destructive flags would silently be lost. A missing override
    file is an empty store, created on the first write. A seed or override file that
    exists but is not valid JSON, or not a selector file, raises StoreReadError (a
    ValueError) naming the file. Relative paths are read from the current directory, and
    '~' is expanded."""
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
        seed = _read_schema2(seed_path, destructive_keys)
    override, native = _read(override_path, destructive_keys)
    # Saved metadata is read only from a schema-2 override. A schema-1 file migrated here
    # has inferred metadata, not metadata someone set.
    return SelectorStore(merge(seed, override), override_path, destructive_keys, seed=seed,
                         override=override if native else None)
