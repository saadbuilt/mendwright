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
``set_meta(destructive=False)`` only clears the last of these. A key of ``destructive_keys``
or of a seed file written as a get_by_* call in another spelling than mendwright's, such as
get_by_role('button', name='Pay'), stands for the call's key (getby.canonical_key).

Only a missing file reads as empty. A seed or override file that exists but is not
valid JSON (a hand edit, a merge conflict) or is not a selector file raises
StoreReadError: reading it as empty would drop its destructive flags and quarantines,
and the next write would replace the file. A read that Windows refuses for a moment
(another process replacing the file) is retried briefly.
"""
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import getby
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
    keys."""
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


class SelectorStore:
    # Seconds to wait for another process's lock on the override file, and the age after
    # which a leftover lock file is treated as abandoned.
    lock_timeout = 15.0
    lock_stale_after = 10.0

    def __init__(self, data, override_path, destructive_keys=frozenset(), seed=None,
                 override=None):
        self._data = data
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
        candidate with the same text is taken over and made active again). Older 'code'
        candidates that are no longer passed get status 'superseded': they stay in the
        file but are not resolved. Seed and heal candidates stay as fallbacks. For the rest
        of this process, candidates() lists these selectors first, in this order. The
        override file is written only when something changed."""
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
        """The status of one stored candidate ('active', 'quarantined', 'superseded'), or
        None when the key has no such candidate."""
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

    @contextlib.contextmanager
    def _locked_file(self):
        lock = self._override_path.with_name(self._override_path.name + '.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = self._clock() + self.lock_timeout
        delay = 0.005
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except (FileExistsError, PermissionError) as exc:
                # On Windows a lock file that is being deleted gives PermissionError for a
                # moment. With no lock file there, it is a real permission problem.
                if isinstance(exc, PermissionError) and not lock.exists():
                    raise
                if self._break_if_stale(lock):
                    continue
                if self._clock() >= deadline:
                    raise TimeoutError('the lock file ' + str(lock) + ' is held by another '
                                       'process') from None
                self._sleep(delay)
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

    def _break_if_stale(self, lock):
        """Remove a lock file older than lock_stale_after seconds (its owner most likely
        died while writing). True if it was removed."""
        try:
            age = self._wallclock() - os.stat(str(lock)).st_mtime
        except OSError:
            return False
        if age <= self.lock_stale_after:
            return False
        try:
            os.unlink(str(lock))
        except FileNotFoundError:
            pass
        except OSError:
            return False
        logger.warning('mendwright: removed a stale lock file %s (%.0f seconds old)', lock, age)
        return True

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
          only the other side changed them, this store's otherwise.
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
                if c.get('list') is True:
                    local['list'] = True     # another process healed a list with it
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
        kept so the seed cannot bring it back."""
        selectors = {}
        for key, e in self._data['selectors'].items():
            cap = e.get('maxCandidates', 5)
            persistable = [c for c in e.get('candidates', []) if self._is_delta(key, c)]

            def recency(c):
                return c.get('lastSucceededAt') or c.get('addedAt') or ''

            active = sorted((c for c in persistable if c.get('status', 'active') == 'active'),
                            key=lambda c: c.get('lastSucceededAt') or '', reverse=True)[:cap]
            inactive = [c for c in persistable if c.get('status', 'active') != 'active']
            pinned = [c for c in inactive if c.get('source') == 'seed']
            others = sorted((c for c in inactive if c.get('source') != 'seed'),
                            key=recency, reverse=True)[:cap]
            overlay = []
            for c in active + pinned + others:
                item = {k: c[k] for k in ('xpath', 'source', 'addedAt', 'lastSucceededAt', 'status') if k in c}
                if 'frameXPath' in c:
                    item['frameXPath'] = c['frameXPath']
                if isinstance(c.get('css'), str) and c['css']:
                    item['css'] = c['css']
                if c.get('list') is True:
                    item['list'] = True
                overlay.append(item)
            saved = {f: v for f, v in self._meta.get(key, {}).items() if f in META_FIELDS}
            if overlay or saved:
                selectors[key] = dict(saved, candidates=overlay)
        return {'schema': 2, 'selectors': selectors}

    def _write(self, payload):
        """Write payload to a temp file next to the override, then os.replace() it in.
        On Windows the replace fails while another process has the file open for
        reading, so it is retried briefly."""
        parent = self._override_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.sel-', suffix='.json', dir=str(parent))
        try:
            os.chmod(tmp, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
            delay = 0.01
            for attempt in range(8):
                try:
                    os.replace(tmp, str(self._override_path))
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    self._sleep(delay)
                    delay = min(delay * 2, 0.2)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def _merge_status(mine, theirs, base):
    """One candidate's status after a three-way merge (see SelectorStore._absorb)."""
    if mine == theirs:
        return mine
    if base is not None and mine == base:
        return theirs          # only the file changed
    if base is not None and theirs == base:
        return mine            # only this store changed
    if 'quarantined' in (mine, theirs):
        return 'quarantined'
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


def load_store(seed_path, override_path, destructive_keys=frozenset()):
    """A SelectorStore over a read-only seed file and a writable override file.

    seed_path=None means no seed. A seed path that does not exist raises
    FileNotFoundError: its destructive flags would silently be lost. A missing override
    file is an empty store, created on the first write. A seed or override file that
    exists but is not valid JSON, or not a selector file, raises StoreReadError (a
    ValueError) naming the file, and so does a seed with two keys for one get_by_* call.
    Relative paths are read from the current directory, and '~' is expanded."""
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
    override, native = _read(override_path, destructive_keys)
    # Saved metadata is read only from a schema-2 override. A schema-1 file migrated here
    # has inferred metadata, not metadata someone set.
    return SelectorStore(merge(seed, override), override_path, destructive_keys, seed=seed,
                         override=override if native else None)
