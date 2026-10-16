"""Drift: whether the selectors saved for each key still find their elements on a page, and
what a run would do about the ones that don't, without acting on the page, saving anything
or asking a model. `mendwright check` runs it on the page where each key was last found
(see mendwright.cli and mendwright.drift_session), and heal.drift() on the page a script
has reached.

- probe_page() is the engine. It looks at a key's saved selectors the way a run does, with
  count() and is_visible() only, and reads the role of the element one resolves to with
  describe(). For a key that misses, it asks "would a run heal this?" by running the run's
  own HealCoordinator.try_heal() on a read-only copy of the store (SelectorStore.snapshot),
  with a provider that raises before any model call (_PreviewProvider), a journal in
  memory and no log. So a key gets the run's own decision and the run's own words, and
  nothing reaches the store, the journal, the page or a model.
- Each key gets one verdict (VERDICTS): it still resolves ('ok'), only through a saved heal
  ('ok-heal'), to an element of another control role ('wrong-role'), a run would heal it
  from memory or follow it into an iframe ('would-heal'), a run would ask a model
  ('needs-model'), a run would refuse it ('refused'), or it could not be judged here
  ('list-missing', 'needs-steps', 'optional', 'unreachable', 'skipped'), with the reason.

Nothing here imports a browser library.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
import textwrap
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from .coordinator import HealCoordinator, _list_missing_message
from .drivers.base import driver_describe, driver_framed
from .facade import _frame_message, latest_success
from .flow import page_path, page_url
from .gate import live_reason
from .journal import _BaseJournal
from .lists import valid as _valid_list
from .resolver import _require_visible, _split_runnable
from .roles import GATE_ROLES
from .selectors import InvalidSelector

logger = logging.getLogger('mendwright')

# What each verdict counts as, and its label in the reports.
COUNTS_AS = {
    'ok': 'ok', 'ok-heal': 'ok', 'wrong-role': 'broken', 'refused': 'broken',
    'would-heal': 'broken', 'needs-model': 'broken', 'list-missing': 'unchecked',
    'needs-steps': 'unchecked', 'optional': 'none', 'unreachable': 'unchecked',
    'skipped': 'unchecked',
}
VERDICTS = tuple(COUNTS_AS)
LABELS = {
    'ok': 'ok', 'ok-heal': 'ok (heal)', 'wrong-role': 'wrong role', 'refused': 'refused',
    'would-heal': 'would heal', 'needs-model': 'needs model', 'list-missing': 'list empty?',
    'needs-steps': 'needs steps', 'optional': 'optional', 'unreachable': 'not checked',
    'skipped': 'not checked',
}
FAIL_ON = ('drift', 'unhealable')

# Roles a wrong-role verdict is about: a control a run acts on. Another tag on a key read
# from (a span that became a div) is reported, not counted.
_CONTROL_ROLES = frozenset(GATE_ROLES) | {'textarea'}

# The reasoning the fingerprint tier journals with heal.proposed (see
# HealCoordinator._fingerprint_tier), which the preview reads its score from.
_SCORES = re.compile(r'fingerprint score (\d+(?:\.\d+)?), next best (\d+(?:\.\d+)?)')


def same_page(last_url, last_page):
    """Whether a key's saved address (lastUrl, see mendwright.flow.page_url) is on its saved
    page (lastPage, see mendwright.flow.page_path): their paths have as many non-empty
    segments, and each segment of the address is '*' or the page's segment once lower-cased.
    A root address is on the page None. False for anything that is not an address."""
    if not isinstance(last_url, str):
        return False
    try:
        path = urlsplit(last_url).path
    except ValueError:
        return False
    url_parts = [part for part in path.split('/') if part]
    page_parts = [part for part in (last_page or '').split('/') if part]
    if len(url_parts) != len(page_parts):
        return False
    return all(u == '*' or u.lower() == p for u, p in zip(url_parts, page_parts, strict=True))


# --- results -------------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectorProbe:
    """What one of a key's saved selectors matched, in the order a run tries them.
    matches is count() in the last poll (None when it was not tried or cannot run), visible
    whether that one match is visible (None when it was not asked), and result one of
    'one visible element', 'one hidden checkbox', 'no match', 'matches 3 elements', 'one
    hidden element', 'cannot run: <reason>' and 'not tried'."""
    selector: str
    source: str
    frame: str | None
    matches: int | None
    visible: bool | None
    result: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class KeyDrift:
    """One key's verdict (see VERDICTS) and why, in one paragraph (reason).

    detail is 'fingerprint' or 'frame' for 'would-heal', and the reason code for
    'skipped' and 'unreachable'. counts_as is 'ok', 'broken', 'unchecked' or 'none'. used is
    the selector a run would use, when one resolves; heal, for 'would-heal', what a run
    would heal it to ({'source', 'selector', 'frame', 'score', 'runner_up'}); provider, for
    'needs-model', the provider a run would ask; confirmed, for 'ok-heal', the last canary
    of that heal in the journal (None when there is none)."""
    key: str
    page: str | None
    verdict: str
    detail: str | None
    counts_as: str
    role: str | None
    live_role: str | None
    destructive: bool
    used: str | None
    heal: dict | None
    provider: str | None
    dry_run: bool
    confirmed: bool | None
    reason: str
    probes: tuple = ()

    def fails(self, *, fail_on: str = 'drift', strict: bool = False) -> bool:
        """Whether this key fails a check: under fail_on='drift' every key that counts as
        broken, under 'unhealable' only what a run can't get past alone (a wrong role, a
        refusal, a heal while dry run is on). strict=True also fails a key that resolves
        only through a saved heal, as --mendwright-strict would."""
        _check_fail_on(fail_on)
        verdict = self.verdict
        if verdict == 'ok-heal':
            return bool(strict)
        if verdict in ('wrong-role', 'refused'):
            return True
        if verdict == 'would-heal':
            return fail_on == 'drift' or self.dry_run
        if verdict == 'needs-model':
            return fail_on == 'drift'
        return False

    def as_dict(self, *, fail_on: str = 'drift', strict: bool = False) -> dict:
        out = dataclasses.asdict(self)
        out['probes'] = [p.as_dict() if hasattr(p, 'as_dict') else dict(p) for p in self.probes]
        if strict and self.verdict == 'ok-heal':
            out['counts_as'] = 'broken'
        out['fails'] = self.fails(fail_on=fail_on, strict=strict)
        return out


@dataclass(frozen=True)
class DriftReport:
    """What probe_page() found on one page: page is its page_path() ('/' for the site
    root), url its page_url(), keys a KeyDrift per key in the order checked, notes what
    was seen on the page that changes no verdict."""
    page: str | None
    url: str | None
    keys: tuple
    notes: tuple = ()

    @property
    def ok(self) -> bool:
        """True when at least one key was checked, none fails under fail_on='drift' and
        none could not be checked. A page where no key was found is not ok."""
        return bool(self.keys) and not self.failed() and not self.unchecked

    @property
    def broken(self) -> tuple:
        return tuple(k for k in self.keys if k.counts_as == 'broken')

    @property
    def unchecked(self) -> tuple:
        return tuple(k for k in self.keys if k.counts_as == 'unchecked')

    def failed(self, *, fail_on: str = 'drift', strict: bool = False) -> tuple:
        """The keys that fail under fail_on and strict (see KeyDrift.fails)."""
        _check_fail_on(fail_on)
        return tuple(k for k in self.keys if k.fails(fail_on=fail_on, strict=strict))

    def text(self) -> str:
        """The report of this page, as `mendwright check` prints a page."""
        where = self.url or self.page or 'this page'
        if not self.keys:
            return 'No key in the store was found on ' + (self.page or where) + '.\n'
        lines = [where]
        lines += ['  ' + note for note in self.notes]
        lines += _key_lines(self.keys)
        return '\n'.join(lines) + '\n'

    def as_dict(self) -> dict:
        return {'page': self.page, 'url': self.url, 'notes': list(self.notes),
                'ok': self.ok, 'keys': [k.as_dict() for k in self.keys]}

    def summary(self) -> str:
        """The INFO line heal.drift() logs: 'drift on /signup: 6 keys, 4 ok, 1 refused'."""
        where = self.page or self.url or 'this page'
        if not self.keys:
            return 'drift on ' + where + ': no key in the store was found on this page'
        count = len(self.keys)
        return ('drift on ' + where + ': ' + str(count) + ' key' + ('s' if count != 1 else '')
                + ', ' + counts_sentence(self.keys))


def _check_fail_on(fail_on):
    if fail_on not in FAIL_ON:
        raise ValueError("mendwright: fail_on takes 'drift' or 'unhealable', got "
                         + repr(fail_on) + '.')


def _wrapped(text, prefix, width=88):
    return textwrap.fill(text, width=width, initial_indent=prefix,
                         subsequent_indent=' ' * len(prefix), break_long_words=False,
                         break_on_hyphens=False)


def _capital(text):
    return text[:1].upper() + text[1:] if text else text


def _key_lines(keys, quiet=False):
    shown = [k for k in keys if not (quiet and k.verdict == 'ok')]
    if not shown:
        return []
    width = min(28, max(len(k.key) for k in shown))
    lines = []
    for k in shown:
        prefix = '  ' + LABELS[k.verdict].ljust(14) + ' ' + k.key.ljust(width) + '  '
        lines.append(_wrapped(k.reason, prefix))
    return lines


# How the summaries count each kind of verdict: (verdict, detail or None, one, several).
_COUNTED = (
    ('ok', None, 'ok', 'ok'),
    ('ok-heal', None, 'ok through a saved heal', 'ok through a saved heal'),
    ('wrong-role', None, 'wrong role', 'wrong role'),
    ('would-heal', 'fingerprint', 'would heal from memory', 'would heal from memory'),
    ('would-heal', 'frame', 'would heal into an iframe', 'would heal into an iframe'),
    ('needs-model', None, 'needs a model', 'need a model'),
    ('refused', None, 'refused', 'refused'),
    ('list-missing', None, 'list may be empty', 'lists may be empty'),
    ('needs-steps', None, 'needs its steps', 'need their steps'),
    ('optional', None, 'optional', 'optional'),
    ('not checked', None, 'not checked', 'not checked'),
)


def counts_sentence(keys) -> str:
    """'1 ok, 1 would heal from memory, 2 not checked' for these KeyDrifts."""
    parts = []
    for verdict, detail, one, several in _COUNTED:
        if verdict == 'not checked':
            n = sum(1 for k in keys if k.verdict in ('unreachable', 'skipped'))
        else:
            n = sum(1 for k in keys if k.verdict == verdict
                    and (detail is None or k.detail == detail))
        if n:
            parts.append(str(n) + ' ' + (one if n == 1 else several))
    return ', '.join(parts) if parts else 'none'


# --- the preview: the run's own heal decision, on a copy --------------------------------------

class _ModelWanted(Exception):
    """A run would ask the model here (_PreviewProvider.propose)."""


class _PreviewProvider:
    """Stands in for the run's provider in a preview: it names that provider and copies
    its unavailable_reason and unavailable_summary (a provider that is not configured, or
    MENDWRIGHT_DISABLE's stand-in), so try_heal() declines as the run does. propose() raises
    _ModelWanted before anything is sent anywhere, and report_outcome() is never called."""

    def __init__(self, real, name=None, model=None):
        self.name = name or provider_name(real)
        self.model = model if model is not None else _provider_model(real)
        for attr in ('unavailable_reason', 'unavailable_summary'):
            value = getattr(real, attr, None)
            if isinstance(value, str):
                setattr(self, attr, value)

    def propose(self, bundle):
        raise _ModelWanted(self.name)

    def report_outcome(self, key, xpath, success):
        raise AssertionError('mendwright: a drift preview reported a heal outcome')


def provider_name(provider):
    """How the reports name a provider: its display_name, or its class name."""
    name = getattr(provider, 'display_name', None)
    return name if isinstance(name, str) and name else type(provider).__name__


def _provider_model(provider):
    model = getattr(provider, 'model', None)
    return model if isinstance(model, str) and model else None


class _PreviewCoordinator(HealCoordinator):
    """The run's HealCoordinator, for a preview on a snapshot of the store. It changes one
    thing: it keeps the note the fingerprint tier gives when it does not heal (tier_note),
    which a needs-model verdict quotes. Everything else, try_heal() and apply() among it,
    is the run's own code."""

    tier_note = None

    def _fingerprint_tier(self, selector_key, meta, broken_candidates, html, started):
        applied, note, requested = super()._fingerprint_tier(
            selector_key, meta, broken_candidates, html, started)
        self.tier_note = note
        return applied, note, requested


class _MemoryJournal(_BaseJournal):
    """A journal that keeps its events in a list, for the preview: nothing reaches a file."""

    def __init__(self):
        self.events = []

    def event(self, name, /, **fields):
        self.events.append(dict(fields, event=name))


@dataclass(frozen=True)
class Frames:
    """How the engine reaches iframes: frame_matches(selector) and framed(frame_selector),
    as a sync driver has them, or None. The async healer passes ones that run its async
    driver's methods on the event loop (see AsyncHealer.drift)."""
    frame_matches: Callable | None = None
    framed: Callable | None = None

    @classmethod
    def of(cls, driver):
        finder = getattr(driver, 'frame_matches', None)
        return cls(finder if callable(finder) else None,
                   lambda frame_selector: driver_framed(driver, frame_selector))


# --- the engine ------------------------------------------------------------------------------

class _Key:
    """What the engine knows about one key while it looks at the page."""

    def __init__(self, store, key):
        self.key = key
        self.meta = store.meta(key)
        self.is_list = _valid_list(self.meta.get('listFingerprint'))
        rows = list(store.candidates(key))
        if self.is_list:
            # As Healer._list_runnable: no heal and nothing inside an iframe.
            rows = [c for c in rows if not c.get('frameXPath') and c.get('source') != 'heal']
        else:
            # The code's selectors first: in a run's own process candidates() has the
            # code's order already, and elsewhere this is closest to it.
            rows = ([c for c in rows if c.get('source') == 'code']
                    + [c for c in rows if c.get('source') != 'code'])
        self.rows = rows
        # The role a run's locator() works with: the saved one, or, while that is unknown or
        # a guess, the one it guesses from its first selector (Healer._apply_meta).
        self.run_role = _run_role(store, key, self.meta)
        self.require_visible = (_require_visible(store, key) if self.run_role is None
                                else self.run_role != 'checkbox')
        self.probes = [{'selector': str(c.get('xpath')), 'source': c.get('source') or 'unknown',
                        'frame': c.get('frameXPath') or None, 'matches': None,
                        'visible': None, 'result': 'not tried'} for c in rows]
        self.runnable = []
        self.errors = []
        for index, row in enumerate(rows):
            runnable, errors = _split_runnable(key, [row])
            if runnable:
                self.runnable.append(index)
            else:
                self._cannot_run(index, errors[0])
        self.used = None        # index of the row a run would use
        self.result = None      # the KeyDrift, once decided

    def _cannot_run(self, index, error):
        reason = getattr(error, 'reason', None) or str(error)
        self.probes[index].update(matches=None, visible=None, result='cannot run: ' + reason)
        self.errors.append(reason)

    @property
    def resolved(self):
        return self.used is not None

    def frame_chain(self, index):
        frame = self.probes[index]['frame']
        return (frame,) if frame else ()


def _run_role(store, key, meta):
    """The role a run guesses for key from its top selector when the store knows none
    (see Healer._apply_meta), else None."""
    from .roles import infer_role
    guess = getattr(store, 'role_is_guess', None)
    try:
        if not (callable(guess) and guess(key)):
            return None
        top = list(store.candidates(key))[:1]
        role = infer_role(top[0]['xpath']) if top else 'unknown'
    except Exception:
        return None
    return role if role and role != 'unknown' else None


def _look(driver, k):
    """One poll of key k: try its runnable selectors in order until one resolves, as
    resolver.resolve() does, with count() and is_visible() only."""
    resolved = None
    for index in list(k.runnable):
        probe = k.probes[index]
        if resolved is not None:
            probe.update(matches=None, visible=None, result='not tried')
            continue
        chain = k.frame_chain(index)
        try:
            count = driver.count(probe['selector'], chain)
        except InvalidSelector as exc:
            k.runnable.remove(index)
            k._cannot_run(index, exc)
            continue
        except Exception:
            count = 0
        count = count if isinstance(count, int) and not isinstance(count, bool) else 0
        probe.update(matches=count, visible=None)
        if k.is_list:
            if count:
                probe['result'] = 'matches ' + _elements(count)
                resolved = index
            else:
                probe['result'] = 'no match'
            continue
        if count == 0:
            probe['result'] = 'no match'
        elif count > 1:
            probe['result'] = 'matches ' + _elements(count)
        else:
            try:
                visible = bool(driver.is_visible(probe['selector'], chain))
            except Exception:
                visible = False
            probe['visible'] = visible
            if visible:
                probe['result'] = 'one visible element'
                resolved = index
            elif not k.require_visible:
                probe['result'] = 'one hidden checkbox'      # resolves, as in a run
                resolved = index
            else:
                probe['result'] = 'one hidden element'
    k.used = resolved


def _elements(count):
    return str(count) + (' element' if count == 1 else ' elements')


def _sleeper(core):
    """How the engine waits between polls on core's page: page.wait_for_timeout on sync
    Playwright (as Healer._pause does), else time.sleep."""
    from .drivers.playwright_sync import PlaywrightSyncDriver
    wait = getattr(core._page, 'wait_for_timeout', None)
    if isinstance(core._driver, PlaywrightSyncDriver) and callable(wait):
        return lambda ms: wait(max(ms, 1))
    return lambda ms: time.sleep(ms / 1000.0)


def _url_of(driver):
    try:
        url = driver.url()
    except Exception:
        return ''
    return url if isinstance(url, str) else ''


def probe_page(core, keys, *, driver=None, frames=None, timeout_ms=None, poll_ms=200,
               sleep=None, fresh=False, gate=None, skips=None, canaries=None, optional=(),
               opener='mendwright.open', who='heal.drift()', url=None,
               clock=time.monotonic) -> DriftReport:
    """Look at each key's saved selectors on the page core (a sync Healer; the async
    healer passes its own core) is on, and give each one a verdict (see KeyDrift).

    driver is the driver to look through (core's own, or the async healer's SyncBridge),
    frames how iframes are reached (Frames.of(driver) by default). One wait for the page:
    every key is polled until it resolves or timeout_ms has passed since the start, with
    sleep(poll_ms) between polls. fresh=True says the page was loaded without the steps
    before it (`mendwright check`), so a key found during run_steps() that misses is
    'needs-steps'. gate(all_missed), when given, runs once after the poll and returns
    (detail, reason) when the page is not the one to judge (a redirect, a sign-in page), and
    every key that missed then gets 'unreachable'. skips maps a key to (reason, ts) of its
    last flow.skip journal event, canaries (key, selector) to its last heal canary, and
    optional are keys registered with heal.interrupt(): a key a run skips is 'optional'.
    opener names the open() a message tells to use, and who what the texts call this check.

    Calls only count, is_visible, describe, url, outer_html, frame_matches and framed on the
    page (and, in a preview, the read-only probes of the fingerprint tier and of apply()).
    Writes nothing anywhere, acts on nothing and asks no model."""
    store, coordinator = core.store, core.coordinator
    driver = core._driver if driver is None else driver
    frames = Frames.of(driver) if frames is None else frames
    sleep = _sleeper(core) if sleep is None else sleep
    timeout_ms = core.timeout_ms if timeout_ms is None else timeout_ms
    skips, canaries, optional = skips or {}, canaries or {}, set(optional or ())
    if url is None:
        url = _url_of(driver)
    dry_run = bool(getattr(coordinator, 'dry_run', False))
    work = [_Key(store, key) for key in keys]

    for k in work:
        if not k.rows:
            _decide(k, 'skipped', 'no-selector', 'it has no active selector in the store: each '
                    'one is quarantined or superseded. A run uses the selector in your code, '
                    'which ' + who + " doesn't know.", store, dry_run)
        elif not k.runnable:
            _unrunnable(k, store, dry_run)

    # The poll: one wait for the page.
    started = clock()
    while True:
        waiting = [k for k in work if k.result is None and not k.resolved]
        for k in waiting:
            _look(driver, k)
            if not k.runnable and not k.resolved:
                _unrunnable(k, store, dry_run)
        waiting = [k for k in work if k.result is None and not k.resolved]
        if not waiting or (clock() - started) * 1000.0 >= timeout_ms:
            break
        sleep(poll_ms)

    for k in work:
        if k.result is None and k.resolved:
            _resolved(driver, store, k, canaries, who, dry_run)

    probed = [k for k in work if k.probes and (k.resolved or k.runnable)]
    missed = [k for k in work if k.result is None]
    notes = []
    if missed and gate is not None:
        stopped = gate(not any(k.resolved for k in probed))
        if stopped:
            detail, reason = stopped
            for k in missed:
                _decide(k, 'unreachable', detail, reason, store, dry_run)
            missed = []
    if fresh and probed and not any(k.resolved for k in probed) and missed:
        count = len(probed)
        notes.append("none of this page's " + str(count) + ' key' + ('s' if count != 1 else '')
                     + ' matched. A redesign, an error page and a page that needs a sign-in or '
                     'earlier steps all look like this, and ' + who + " can't tell them apart.")

    preview = _Preview(core, driver, frames, url, opener, who)
    for k in missed:
        _miss(k, store, coordinator, preview, skips, optional, fresh, who, dry_run)

    address = page_url(url)
    page = page_path(url) or (_root_page(address) if address else None)
    return DriftReport(page=page, url=address, keys=tuple(k.result for k in work),
                       notes=tuple(notes))


def _root_page(address):
    try:
        return '/' if urlsplit(address).path in ('', '/') else None
    except ValueError:
        return None


def key_page(store, key):
    """The page a key belongs to in the reports: its lastPage, '/' for one found on the
    site root, else None."""
    try:
        page = store.meta(key).get('lastPage')
    except Exception:
        page = None
    if page:
        return page
    address = _last_url(store, key)
    return '/' if address and _root_page(address) else None


def _last_url(store, key):
    getter = getattr(store, 'last_url', None)
    try:
        return getter(key) if callable(getter) else None
    except Exception:
        return None


def _known_role(store, key, meta):
    guess = getattr(store, 'role_is_guess', None)
    role = meta.get('expectedRole')
    known = bool(role) and role != 'unknown'
    try:
        if callable(guess) and guess(key):
            known = False
    except Exception:
        pass
    return role if known else None


def _decide(k, verdict, detail, reason, store, dry_run, *, used=None, live_role=None,
            heal=None, provider=None, confirmed=None, counts_as=None):
    k.result = KeyDrift(
        key=k.key, page=key_page(store, k.key), verdict=verdict, detail=detail,
        counts_as=counts_as or COUNTS_AS[verdict], role=_known_role(store, k.key, k.meta),
        live_role=live_role, destructive=bool(k.meta.get('destructive')), used=used, heal=heal,
        provider=provider, dry_run=dry_run, confirmed=confirmed, reason=reason,
        probes=tuple(SelectorProbe(**p) for p in k.probes))


def _unrunnable(k, store, dry_run):
    first = k.errors[0] if k.errors else 'the driver cannot run them'
    _decide(k, 'skipped', 'unrunnable', 'none of its selectors can run here: '
            + first.rstrip('.') + '.', store, dry_run)


def _article(word):
    return ('an ' if word[:1].lower() in 'aeiou' else 'a ') + word


def _resolved(driver, store, k, canaries, who, dry_run):
    """The verdict of a key one of whose selectors resolved (see probe_page)."""
    probe = k.probes[k.used]
    used, chain = probe['selector'], k.frame_chain(k.used)
    if k.is_list:
        count = probe['matches']
        lfp = k.meta.get('listFingerprint') or {}
        had = lfp.get('count') if isinstance(lfp, dict) else None
        text = used + ' matches ' + _elements(count)
        if isinstance(had, int) and not isinstance(had, bool):
            text += ' (' + str(had) + ' when its list fingerprint was recorded)'
        _decide(k, 'ok', None, text + '.', store, dry_run, used=used)
        return
    row = k.rows[k.used]
    healed = row.get('source') == 'heal'
    if healed and k.meta.get('destructive'):
        # What Healer._check_healed_target does in a run, without the quarantine.
        _decide(k, 'refused', None, 'it matches only through ' + used + ', a selector a heal '
                "saved, and '" + k.key + "' is marked as a destructive control: a run "
                'quarantines that heal and raises AttentionRequired. ' + who + ' quarantined '
                'nothing. Fix the selector in your code.', store, dry_run, used=used)
        return
    try:
        described = driver_describe(driver, used, chain)
    except Exception:
        described = None
    live = (described or {}).get('role') or None
    expected = _known_role(store, k.key, k.meta)
    hidden = probe['result'] == 'one hidden checkbox'
    shown = 'one hidden checkbox' if hidden else 'one visible ' + (live or expected or 'element')
    note = ''
    if expected and live and live != expected:
        if live in _CONTROL_ROLES or expected in _CONTROL_ROLES:
            _decide(k, 'wrong-role', None, used + ' matches ' + ('one hidden element' if hidden
                    else 'one visible element') + ', but it is ' + _article(live) + ", and '"
                    + k.key + "' is " + _article(expected) + '. A run acts on it all the same: '
                    'mendwright compares roles only for heals. Check the selector in your '
                    'code.', store, dry_run, used=used, live_role=live)
            return
        note = (' Its tag changed from ' + expected + ' to ' + live + ", which isn't "
                'counted: neither is a control.')
    elif described is None:
        if callable(getattr(driver, 'describe', None)):
            note = ' Its role could not be read.'
        else:
            note = " Its role was not read: this driver can't describe elements."
    elif expected is None:
        note = " Its role isn't known yet, so it wasn't compared."
    if healed:
        before = k.probes[:k.used]
        added = row.get('addedAt')
        when = added[:10] if isinstance(added, str) and added else 'an unknown date'
        text = ('The selectors before it miss (' + before[0]['selector'] + ': '
                + before[0]['result'] + '); the heal' if before else 'The heal')
        text += (' saved on ' + when + ', ' + used + ', matches ' + shown + ', and a run uses it. '
                 'Run mendwright fix to put it in your code.')
        confirmed = canaries.get((k.key, used))
        if confirmed is not True:
            text += ' No passing test confirmed it, so check that it finds the right element.'
        _decide(k, 'ok-heal', None, text + note, store, dry_run, used=used, live_role=live,
                confirmed=confirmed)
        return
    _decide(k, 'ok', None, used + ' matches ' + shown + '.' + note, store, dry_run, used=used,
            live_role=live)


_RESULT_WORDS = {'no match': 'matches nothing', 'one hidden element': 'matches one hidden element',
                 'one visible element': 'matches one visible element',
                 'one hidden checkbox': 'matches one hidden checkbox'}


def _miss_text(k):
    """'#news matches nothing (and its 2 other selectors miss)': the first selector a run
    tries, what it matched, and how many others there are."""
    first = k.probes[0]
    result = first['result']
    if result.startswith('cannot run: '):
        said = 'cannot run (' + result[len('cannot run: '):].rstrip('.') + ')'
    else:
        said = _RESULT_WORDS.get(result, result)
    text = first['selector'] + ' ' + said
    others = len(k.probes) - 1
    if others == 1:
        text += ' (and its other selector misses)'
    elif others > 1:
        text += ' (and its ' + str(others) + ' other selectors miss)'
    return text


def _miss(k, store, coordinator, preview, skips, optional, fresh, who, dry_run):
    """The verdict of a key none of whose selectors resolved (see probe_page)."""
    miss = _miss_text(k)
    if k.key in optional:
        _decide(k, 'optional', None, miss + ', and it is registered with heal.interrupt(): a '
                'run skips it when it is missing. Not counted.', store, dry_run)
        return
    if k.key in skips:
        reason, ts = skips[k.key]
        when = ', ' + ts[:10] if isinstance(ts, str) and ts else ''
        _decide(k, 'optional', None, miss + ', and earlier runs skipped it (' + str(reason)
                + when + '): a run skips it when it is missing. Not counted.', store, dry_run)
        return
    if k.is_list:
        if getattr(coordinator, 'fingerprint', False):
            lfp = k.meta.get('listFingerprint') or {}
            _decide(k, 'list-missing', None, _list_missing_message(
                k.key, lfp.get('count') if isinstance(lfp, dict) else None) + ' ' + who
                + " can't tell an empty list from a broken one either.", store, dry_run)
        else:
            _decide(k, 'list-missing', None, miss + ': a run gets [] at once, since the '
                    'fingerprint tier is off.', store, dry_run, counts_as='none')
        return
    steps = k.meta.get('steps') or ()
    if fresh and steps:
        _decide(k, 'needs-steps', None, miss + ". It was found during step '" + steps[0]
                + "' of run_steps(), and " + who + ' loaded this page without the steps before '
                "it, so the miss may be the page's state, not drift. Check it with "
                'heal.drift() inside your flow.', store, dry_run)
        return
    preview.judge(k, miss, dry_run)


class _Preview:
    """The heal preview of one page (see the module docs): the run's own try_heal() and
    frame follow, on one snapshot of the store and the page's HTML captured once, both made
    for the first key that needs them."""

    def __init__(self, core, driver, frames, url, opener, who):
        self.core, self.driver, self.frames = core, driver, frames
        self.url, self.opener, self.who = url, opener, who
        self.shadow = None
        self.snapshot = None
        self.selenium = type(core._driver).__name__ == 'SeleniumDriver'

    def _ready(self):
        if self.shadow is not None:
            return True
        take = getattr(self.core.store, 'snapshot', None)
        if not callable(take):
            return False
        coordinator = self.core.coordinator
        snapshot = take()
        try:
            html = self.driver.outer_html() or ''
        except Exception:
            html = ''
        shadow = _PreviewCoordinator(
            snapshot, provider=_PreviewProvider(coordinator.provider), driver=self.driver,
            html_provider=lambda: html, inventory_provider=lambda: [], guard=coordinator.guard,
            dry_run=False, guardrails=None, journal=_MemoryJournal(), log=lambda line: None,
            fingerprint=coordinator.fingerprint,
            heal_across_pages=coordinator.heal_across_pages, entry=coordinator.entry)
        shadow.disabled = bool(getattr(coordinator, 'disabled', False))
        self.snapshot, self.shadow = snapshot, shadow
        return True

    def judge(self, k, miss, dry_run):
        store = self.core.store
        if not self._ready():
            _decide(k, 'skipped', 'no-preview', miss + ', and this store has no snapshot(), so '
                    + self.who + " can't look for a heal without saving one.", store, dry_run)
            return
        if k.run_role is not None:
            self.snapshot.guess_role(k.key, k.run_role)     # as the run's locator() does
        if self._frame(k, miss, dry_run):
            return
        self._heal(k, miss, dry_run)

    def _refused(self, k, miss, reason, dry_run):
        _decide(k, 'refused', None, miss + '. A run would not heal it: ' + reason,
                self.core.store, dry_run)

    def _frame(self, k, miss, dry_run):
        """Healer._follow_into_frame(), without saving: True when it decided the key."""
        core, shadow, finder = self.core, self.shadow, self.frames.frame_matches
        if finder is None:
            return False
        matches = []
        for selector in core._frame_candidates(k.key):
            try:
                found = finder(selector)
            except Exception:
                continue
            if found:
                matches.append((selector, found))
        if not matches:
            return False
        plan, refused = None, None
        for selector, found in matches:
            frame, alt = core._frame_plan(k.key, selector, found, coordinator=shadow)
            if frame is not None:
                plan = (selector, found, frame, alt)
                break
            if refused is None:
                refused = (selector, found, alt)
        if plan is None:
            self._refused(k, miss, _frame_message(k.key, refused[0], refused[1], self.opener,
                                                  refused[2], self.selenium), dry_run)
            return True
        selector, found, frame, alt = plan
        moved = shadow._flow_change(k.key, core._flow_context(k.key, self.url, quiet=True))
        if moved:
            self._refused(k, miss, moved, dry_run)
            return True
        why = None
        if not shadow.guard():
            why = 'a transaction guard is currently blocking heals'
        else:
            live = live_reason(self.driver, alt, self.snapshot.meta(k.key), (frame,))
            if live:
                why = "its element there failed mendwright's checks (" + live + ')'
        if why:
            self._refused(k, miss, _frame_message(k.key, selector, found, self.opener, why,
                                                  self.selenium), dry_run)
            return True
        text = (miss + ' on the page, but it matches one element inside the iframe ' + frame
                + '. A run looks it up there from now on, which counts as a heal '
                '(--mendwright-strict fails on it). Nothing was saved.')
        if dry_run:
            text += ' ' + _DRY_RUN_NOTE
        _decide(k, 'would-heal', 'frame', text, core.store, dry_run,
                heal={'source': 'frame', 'selector': alt, 'frame': frame, 'score': None,
                      'runner_up': None})
        return True

    def _heal(self, k, miss, dry_run):
        """The run's try_heal(), on the snapshot (see Healer._heal and _heal_scope)."""
        core, shadow, snapshot = self.core, self.shadow, self.snapshot
        key = k.key
        context = core._flow_context(key, self.url, quiet=True)
        scope = None
        best = latest_success(snapshot, key)
        frame = best.get('frameXPath') if best else None
        if isinstance(frame, str) and frame and self.frames.framed is not None:
            try:
                framed = self.frames.framed(frame)
            except Exception:
                framed = None
            if framed is not None:
                scope = (frame, framed)
        shadow.tier_note = None
        shadow.heal_reasons.pop(key, None)
        start = len(shadow.journal.events)
        meta, broken = snapshot.meta(key), list(snapshot.candidates(key))
        provider = shadow.provider
        try:
            if scope is None:
                applied = shadow.try_heal(key, meta, broken, context)
            else:
                with shadow.scoped(scope[1], scope[0]):
                    applied = shadow.try_heal(key, meta, broken, context)
        except _ModelWanted:
            note = shadow.tier_note if shadow.fingerprint else 'the fingerprint tier is off'
            asked = provider.name + (', model ' + provider.model if provider.model else '')
            _decide(k, 'needs-model', None, miss + ', and ' + (note or 'the fingerprint tier did '
                    'not heal it') + '. A run would ask ' + asked + '. ' + self.who
                    + ' asks no model.', core.store, dry_run, provider=provider.name)
            return
        except Exception as exc:
            first = (str(exc).strip().splitlines() or [''])[0][:200]
            self._refused(k, miss, 'the heal preview raised ' + type(exc).__name__
                          + (': ' + first if first else '') + ', so a run would likely stop '
                          'here too.', dry_run)
            return
        if not applied:
            self._refused(k, miss, shadow.heal_reasons.get(key) or 'it declined the heal.',
                          dry_run)
            return
        score = runner_up = None
        for event in shadow.journal.events[start:]:
            if event.get('event') == 'heal.proposed' and event.get('key') == key:
                found = _SCORES.search(str(event.get('reasoning') or ''))
                if found:
                    score, runner_up = float(found.group(1)), float(found.group(2))
        how = ('fingerprint score ' + format(score, '.2f') + ', next best '
               + format(runner_up, '.2f')) if score is not None else 'its fingerprint'
        text = (miss + '. A run would heal it from memory to ' + applied + ' (' + how + '), '
                'which passed every check of a heal on this page. Nothing was saved.')
        if dry_run:
            text += ' ' + _DRY_RUN_NOTE
        _decide(k, 'would-heal', 'fingerprint', text, core.store, dry_run,
                heal={'source': 'fingerprint', 'selector': applied,
                      'frame': scope[0] if scope else None, 'score': score,
                      'runner_up': runner_up})


_DRY_RUN_NOTE = 'Dry run is on, so a run would check this heal and not apply it.'


# --- heal.drift() ------------------------------------------------------------------------------

def store_keys(store):
    """The keys a store has, in its order. [] for a store that cannot list them."""
    data = getattr(store, '_data', None)
    selectors = data.get('selectors') if isinstance(data, dict) else None
    if isinstance(selectors, dict):
        return list(selectors)
    keys = getattr(store, 'keys', None)
    try:
        return list(keys()) if callable(keys) else []
    except Exception:
        return []


def drift_keys(store, keys, timeout_ms, poll_ms):
    """heal.drift()'s arguments, checked before anything touches the page: the keys as a
    list without duplicates (None stays None). Raises TypeError or ValueError."""
    if isinstance(timeout_ms, bool) or not (timeout_ms is None or isinstance(timeout_ms, int)):
        raise TypeError('mendwright: heal.drift() takes timeout_ms= as a whole number of '
                        'milliseconds, got ' + repr(timeout_ms) + '.')
    if isinstance(poll_ms, bool) or not isinstance(poll_ms, int):
        raise TypeError('mendwright: heal.drift() takes poll_ms= as a whole number of '
                        'milliseconds, got ' + repr(poll_ms) + '.')
    if timeout_ms is not None and timeout_ms < 0:
        raise ValueError('mendwright: heal.drift() takes timeout_ms=0 or more, got '
                         + repr(timeout_ms) + '.')
    if poll_ms < 1:
        raise ValueError('mendwright: heal.drift() takes poll_ms=1 or more, got '
                         + repr(poll_ms) + '.')
    if keys is None:
        return None
    if isinstance(keys, (str, bytes)):
        raise TypeError("mendwright: heal.drift() takes a list of keys, such as "
                        "heal.drift(['email']), not one string.")
    try:
        items = list(keys)
    except TypeError:
        raise TypeError('mendwright: heal.drift() takes a list of keys, such as '
                        "heal.drift(['email']), got " + type(keys).__name__ + '.') from None
    bad = [item for item in items if not isinstance(item, str)]
    if bad:
        raise TypeError('mendwright: heal.drift() takes key names as strings, got '
                        + repr(bad[0]) + '.')
    items = list(dict.fromkeys(items))
    known = set(store_keys(store))
    unknown = [item for item in items if item not in known]
    if unknown:
        raise ValueError("mendwright: heal.drift() got keys the store doesn't have: "
                         + ', '.join(repr(u) for u in unknown) + '. Check the names, or look the '
                         'key up once with heal.locator() first.')
    return items


def keys_on_page(store, url):
    """The keys recorded on the page at url: those whose lastPage or one of whose pages is
    its page_path(), or, on the site root, those whose last_url() is that root."""
    here = page_path(url)
    out = []
    if here is None:
        root = page_url(url)
        if not root or _root_page(root) is None:
            return []
        return sorted(k for k in store_keys(store) if _last_url(store, k) == root)
    for key in store_keys(store):
        try:
            meta = store.meta(key)
        except Exception:
            continue
        if meta.get('lastPage') == here or here in (meta.get('pages') or ()):
            out.append(key)
    return sorted(out)


def journal_evidence(events):
    """(skips, canaries) from journal events: per key, (reason, ts) of its last flow.skip;
    per (key, selector), whether its last heal.canary was a success."""
    from .fixback import _canary_state
    skips = {}
    for event in events or ():
        if event.get('event') == 'flow.skip' and isinstance(event.get('key'), str):
            skips[event['key']] = (event.get('reason') or 'skipped', event.get('ts'))
    return skips, _canary_state(events)


def store_journal(store):
    """The events of journal.jsonl next to a SelectorStore's file, oldest first, or []."""
    from .cli import _read_journal
    from .store import SelectorStore
    path = getattr(store, '_override_path', None)
    if not isinstance(store, SelectorStore) or path is None:
        return []
    return _read_journal(os.path.join(os.path.dirname(os.fspath(path)) or '.',
                                      'journal.jsonl')) or []
