"""What one run did, from the events of journal.jsonl, for `mendwright notify`.

- select() picks the events of one run: those whose ``run`` is the run id (gh-<digits>
  without an attempt picks the highest attempt in the journal), those since a time, or all.
  It never falls back to the newest run in the journal, which would report an old run as
  this one.
- summarize() adds them up: the selectors repaired and how, the interruptions and popups
  handled, what needs a human, and what was not repaired on purpose (dry run,
  MENDWRIGHT_DISABLE=1).
- message() writes the summary as a few short lines, in mendwright's own words.

Pure functions: nothing here reads a file, the environment or the clock. What a summary
holds, and so what a message can say, is counts, codes, mendwright's phrases and key names.
No selector, no reason text, no popup text, page path or node id reaches it, whatever the
events hold.
"""

import re
from datetime import datetime, timezone

from ._md import visible

# The phrase each code gets after its subject, and what the subject is: 'selector' (the key),
# 'interruption' (the key of heal.interrupt()), 'popup' (the step a popup was in front of),
# 'step' (the name of heal.step()) or 'run_steps' (no key). The reason a heal.declined
# event carries is never used: it can quote the page.
PHRASES = {
    'destructive': ('selector', 'is marked destructive, so mendwright did not repair it'),
    'page_guard': ('selector', 'was never found on this page before, so the flow may have '
                               'changed'),
    'role_unknown': ('selector', 'has no known role, so a repair could not be checked'),
    'no_provider': ('selector', 'was not found from its fingerprint, and no model is set up'),
    'not_here': ('selector', 'could not be repaired: the model found no element on this page '
                             'that fits'),
    'no_proposal': ('selector', 'could not be repaired: the model gave no usable selector'),
    'gate': ('selector', 'could not be repaired: no proposed selector passed the checks'),
    'rate_limit': ('selector', 'was not repaired: the hourly heal limit was reached'),
    'guard': ('selector', 'was not repaired: guard= blocked heals'),
    'list_empty': ('selector', 'matched nothing as a list: it is empty, or its selector broke'),
    'list_not_found': ('selector', 'matched nothing as a list, and mendwright found no list it '
                                   'could propose'),
    'list_proposed': ('selector', 'matched nothing as a list, and mendwright found one for you '
                                  'to check with mendwright lists'),
    'list_waits_fix': ('selector', 'matched nothing as a list, and the list heal accepted for it '
                                   'waits for mendwright fix or its seed file'),
    'get_by': ('selector', 'could not be repaired from its fingerprint, and mendwright asks no '
                           'model for a get_by_* call'),
    'frame': ('selector', 'is inside an iframe mendwright did not look into'),
    'shadow': ('selector', 'is inside a shadow root, where mendwright could not repair it'),
    'quarantined': ('selector', 'had a repair that was wrong, so it was quarantined'),
    'interruption_loop': ('interruption', 'came back each time it was handled'),
    'popup_loop': ('popup', 'came back each time it was dismissed'),
    'popup_failed': ('popup', 'could not be dismissed'),
    'run_failed': ('interruption', 'could not be handled: its run= function raised an error'),
    'step_missing': ('step', 'never showed, so the site may have dropped it'),
    'step_again': ('step', 'showed again after it ran'),
    'step_moved': ('step', 'showed on another page than before'),
    'no_step': ('run_steps', 'stopped: no registered step showed'),
    'step_cap': ('run_steps', 'stopped: it ran more steps than it allows'),
    'other': ('selector', 'was not repaired'),
}
# heal.declined codes that are declines on purpose: counted, never a thing for a human.
ON_PURPOSE = ('dry_run', 'disabled')
# The subject of an entry when --detail counts names no key.
_NO_KEY = {'selector': 'a selector', 'interruption': 'an interruption', 'popup': 'a popup',
           'step': 'a step', 'run_steps': 'run_steps()'}
_STOP_CODES = ('interruption_loop', 'popup_loop', 'step_cap', 'step_again', 'step_moved',
               'no_step')

MAX_KEYS = 10           # keys (or entries) one line names, then "and N more"
MAX_KEY = 80            # characters of one key
MAX_TEXT = 3000         # characters of the whole message
MAX_TITLE = 80

_GH = re.compile(r'gh-([0-9]+)')
_GH_ATTEMPT = re.compile(r'gh-([0-9]+)\.([0-9]+)')
_SPACE = re.compile(r'\s+')


# --- which events are the run ------------------------------------------------------------

def parse_time(text):
    """An ISO 8601 time with a time zone (Z allowed) as an aware datetime. Raises ValueError
    for anything else, a time without a zone included."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError('not a time')
    value = text.strip()
    if value[-1:] in ('Z', 'z'):
        value = value[:-1] + '+00:00'
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('a time without a time zone')
    return parsed


def _event_time(event):
    try:
        return parse_time(event.get('ts'))
    except (ValueError, TypeError):
        return None


def select(events, *, run=None, since=None, all=False):
    """(the events of one run, a label for it such as 'run gh-11893412207.1').

    all: every event. since (an aware datetime, or text parse_time() reads): events whose ts
    is at or after it; lines with a ts that does not parse are left out. run: events whose
    run is that id; gh-<digits> picks the highest attempt gh-<digits>.<n> present, and the
    events whose run is gh-<digits> itself only when there is no attempt. An id that matches
    nothing gives no events: a run in which mendwright recorded nothing."""
    if all:
        return [e for e in events if isinstance(e, dict)], 'the whole journal'
    if since is not None:
        start = since if isinstance(since, datetime) else parse_time(since)
        picked = []
        for event in events:
            when = _event_time(event) if isinstance(event, dict) else None
            if when is not None and when >= start:
                picked.append(event)
        return picked, 'the events since ' + start.astimezone(timezone.utc).isoformat()
    if not isinstance(run, str) or not run:
        raise ValueError('select() needs run=, since= or all=True')
    whole = _GH.fullmatch(run)
    if whole is None:
        return ([e for e in events if isinstance(e, dict) and e.get('run') == run],
                'run ' + run)
    number = int(whole.group(1))
    exact, attempts = [], {}
    for event in events:
        value = event.get('run') if isinstance(event, dict) else None
        if not isinstance(value, str):
            continue
        if value == run:
            exact.append(event)
            continue
        found = _GH_ATTEMPT.fullmatch(value)
        if found and int(found.group(1)) == number:
            attempts.setdefault(int(found.group(2)), []).append(event)
    if attempts:
        best = max(attempts)
        return attempts[best], 'run gh-' + str(number) + '.' + str(best)
    return exact, 'run ' + run


# --- what they add up to -------------------------------------------------------------------

def _tier(event):
    source, provider = event.get('source'), event.get('provider')
    if 'fingerprint' in (source, provider):
        return 'fingerprint'
    if 'frame' in (source, provider):
        return 'frame'
    if 'shadow' in (source, provider):
        return 'shadow'       # the key's own selector, found inside a shadow root: no model
    return 'model'


def _text(value):
    return value if isinstance(value, str) and value else None


def summarize(events):
    """What the events of one run add up to, as a dict (see the module's docstring):

    repaired: total, fingerprint, model, frame, shadow, confirmed, not_confirmed, quarantined and
      keys (sorted), over the distinct (key, xpath) of heal.applied. A heal is quarantined
      when a failed canary or a heal.quarantine names it, else confirmed when a canary
      success does, else not confirmed.
    handled: cookie_banners, popups and interruptions handled.
    needs_human: [{'key', 'code', 'about'}], one per thing, in the order each first showed,
      the latest code winning; 'about' is the subject (see PHRASES). A selector's entry is
      dropped when a later heal.applied for its key is not quarantined.
    not_repaired_on_purpose: dry_run and disabled, as numbers of keys.
    changed: whether anything was repaired or needs a human."""
    applied = {}            # (key, xpath) -> tier, in the order first applied
    applied_at = []         # (index, key, xpath)
    good, bad = set(), set()
    refused_by = {}         # (key, xpath) -> the by of its latest canary
    handled = {'cookie_banners': 0, 'popups': 0, 'interruptions': 0}
    needs = {}              # (about, key) -> {'key', 'code', 'about', 'at'}
    on_purpose = {code: set() for code in ON_PURPOSE}

    def need(about, key, code, at):
        ident = (about, key)
        if ident in needs:
            needs[ident].update(code=code, at=at)
        else:
            needs[ident] = {'key': key, 'code': code, 'about': about, 'at': at}

    for at, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        name, key = event.get('event'), _text(event.get('key'))
        xpath = _text(event.get('xpath'))
        if name == 'heal.applied' and key and xpath:
            applied.setdefault((key, xpath), _tier(event))
            applied_at.append((at, key, xpath))
        elif name == 'heal.canary' and key and xpath:
            (good if event.get('success') is True else bad).add((key, xpath))
            refused_by[(key, xpath)] = event.get('by')
        elif name == 'heal.quarantine' and key:
            if xpath:
                bad.add((key, xpath))
            if not event.get('list'):
                # A heal of a key marked destructive after it was made is quarantined
                # however right it was: say that, not that the repair was wrong. The canary
                # says so for a heal made in this process, the heal.quarantine (by) for one
                # saved before.
                code = ('destructive' if 'destructive' in (refused_by.get((key, xpath)),
                                                           event.get('by'))
                        else 'quarantined')
                need('selector', key, code, at)
        elif name == 'heal.declined' and key:
            code = _text(event.get('code'))
            if code in ON_PURPOSE:
                on_purpose[code].add(key)
                continue
            if code not in PHRASES or PHRASES[code][0] != 'selector':
                code = 'other'
            need('selector', key, code, at)
        elif name == 'heal.held' and key:
            need('selector', key, 'list_proposed', at)
        elif name == 'flow.stop':
            code = _text(event.get('code'))
            if code not in _STOP_CODES:
                continue
            about = PHRASES[code][0]
            subject = _text(event.get('before')) if about == 'popup' else key
            if about == 'run_steps':
                subject = None
            need(about, subject, code, at)
        elif name == 'flow.dismiss':
            outcome = event.get('outcome')
            if outcome == 'dismissed':
                handled['popups'] += 1
            elif outcome in ('failed', 'lost'):
                need('popup', _text(event.get('before')), 'popup_failed', at)
        elif name == 'flow.interrupt' and key:
            if event.get('outcome') == 'failed':
                need('interruption', key, 'run_failed', at)
            elif key == 'cookie_banner' and event.get('preset'):
                handled['cookie_banners'] += 1
            else:
                handled['interruptions'] += 1
        elif name == 'flow.step' and key and event.get('outcome') == 'missing':
            need('step', key, 'step_missing', at)

    quarantined = {ident for ident in applied if ident in bad}
    confirmed = {ident for ident in applied if ident in good and ident not in bad}
    tiers = list(applied.values())
    entries = []
    for entry in needs.values():                # in the order each first showed
        if entry['about'] == 'selector' and any(
                at > entry['at'] and key == entry['key'] and (key, xpath) not in quarantined
                for at, key, xpath in applied_at):
            continue        # healed since, and that heal held
        entries.append({'key': entry['key'], 'code': entry['code'], 'about': entry['about']})
    repaired = {'total': len(applied), 'fingerprint': tiers.count('fingerprint'),
                'model': tiers.count('model'), 'frame': tiers.count('frame'),
                'shadow': tiers.count('shadow'),
                'confirmed': len(confirmed), 'quarantined': len(quarantined),
                'not_confirmed': len(applied) - len(confirmed) - len(quarantined),
                'keys': sorted({key for key, _ in applied})}
    return {'repaired': repaired, 'handled': handled, 'needs_human': entries,
            'not_repaired_on_purpose': {code: len(keys) for code, keys in on_purpose.items()},
            'changed': bool(applied) or bool(entries), 'events': len(events)}


# --- the message -----------------------------------------------------------------------------

def _plural(n, one, many=None):
    return str(n) + ' ' + (one if n == 1 else (many or one + 's'))


def _cut(text, limit):
    return text if len(text) <= limit else text[:limit - 1] + '…'


def key_text(key):
    """A key as a summary shows it: control and bidirectional characters written out (see
    mendwright._md.visible), whitespace collapsed, cut to MAX_KEY characters."""
    return _cut(_SPACE.sub(' ', visible(key)).strip(), MAX_KEY) or '(empty)'


def shown_key(key):
    """A key as a message names it: key_text() in backticks, a backtick in it written as '."""
    return '`' + key_text(key).replace('`', "'") + '`'


def title_text(title):
    """The title as a message shows it: visible, on one line, cut to MAX_TITLE characters."""
    return _cut(_SPACE.sub(' ', visible(title or '')).strip(), MAX_TITLE)


def phrase(entry):
    """The phrase of one needs-human entry, after its subject."""
    return PHRASES.get(entry.get('code'), PHRASES['other'])[1]


def subject(entry, *, detail='keys'):
    """Who or what one needs-human entry is about: `pay`, interruption `cookies`, a popup
    in front of `email`, step `gift` or run_steps(); with detail='counts', no key."""
    about, key = entry.get('about') or 'selector', entry.get('key')
    if detail == 'counts' or (about != 'run_steps' and not key):
        return _NO_KEY.get(about, 'a selector')
    if about == 'run_steps':
        return 'run_steps()'
    shown = shown_key(key)
    return {'selector': shown, 'interruption': 'interruption ' + shown,
            'popup': 'a popup in front of ' + shown, 'step': 'step ' + shown}[about]


def _listed(items, shown, sep):
    out = sep.join(items[:shown])
    if len(items) > shown:
        out += (sep if out else '') + 'and ' + str(len(items) - shown) + ' more'
    return out


def _repaired_line(r, detail, keys_shown):
    if not r['total']:
        return ''
    parts = []
    if r['fingerprint']:
        parts.append(str(r['fingerprint']) + (' from a fingerprint' if r['fingerprint'] == 1
                                              else ' from fingerprints'))
    if r['model']:
        parts.append(str(r['model']) + ' by the model')
    if r['frame']:
        parts.append(str(r['frame']) + ' found inside an iframe')
    if r.get('shadow'):
        parts.append(str(r['shadow']) + ' found inside a shadow root')
    states = []
    for name, label in (('confirmed', 'confirmed'), ('not_confirmed', 'not confirmed'),
                        ('quarantined', 'quarantined')):
        if r[name]:
            states.append(str(r[name]) + ' ' + label)
    line = _plural(r['total'], 'selector') + ' repaired: ' + ', '.join(parts) + '. '
    line += ', '.join(states)
    if detail == 'keys' and r['keys'] and keys_shown > 0:
        line += ': ' + _listed([shown_key(k) for k in r['keys']], keys_shown, ', ')
    return line + '.'


def _handled_line(h):
    parts = []
    if h['cookie_banners']:
        parts.append(_plural(h['cookie_banners'], 'cookie banner'))
    if h['popups']:
        parts.append(_plural(h['popups'], 'popup'))
    if h['interruptions']:
        parts.append(_plural(h['interruptions'], 'other interruption' if parts
                             else 'interruption'))
    return 'Handled: ' + ', '.join(parts) + '.' if parts else ''


def _needs_line(summary, detail, shown):
    entries = summary['needs_human']
    if not entries:
        return 'Nothing needs a human.' if summary['repaired']['total'] else ''
    head = str(len(entries)) + (' needs' if len(entries) == 1 else ' need') + ' a human'
    if shown <= 0:
        return head + '.'
    items = [subject(e, detail=detail) + ' ' + phrase(e) for e in entries]
    return head + ': ' + _listed(items, shown, '; ') + '.'


def _purpose_line(p):
    parts = []
    if p.get('dry_run'):
        parts.append(str(p['dry_run']) + ' in dry run')
    if p.get('disabled'):
        parts.append(str(p['disabled']) + ' with MENDWRIGHT_DISABLE=1')
    return 'Not repaired on purpose: ' + ', '.join(parts) + '.' if parts else ''


def tests_line(summary, tests):
    if not tests:
        return ''
    if tests == 'success':
        return 'The tests passed.'
    if tests == 'cancelled':
        return 'The tests were cancelled.'
    if tests in ('failure', 'timed_out', 'startup_failure'):
        if not summary['needs_human']:
            return ('The tests failed. The journal records no heal or flow problem, so the cause '
                    'is something else.')
        return 'The tests failed.'
    return 'The tests ended as ' + tests + '.'


def slack_escape(text):
    """Text Slack shows as it is: &, < and > as entities, which also stops <!channel> and
    <@U123> from notifying anyone."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def message(summary, *, detail='keys', title=None, tests=None, pr_url=None, run_url=None,
            slack=False, limit=MAX_TEXT):
    """The summary as lines of text, at most ``limit`` characters (the needs-human line and
    then the repaired line name fewer keys until it fits). detail='counts' names no key.
    slack=True writes &, < and > as entities."""
    name = title_text(title)
    first = 'mendwright' + (', ' + name if name else '') + (
        ': the site changed.' if summary['changed'] else ': nothing changed.')

    def build(needs_shown, keys_shown):
        lines = [first, _repaired_line(summary['repaired'], detail, keys_shown),
                 _handled_line(summary['handled']),
                 _needs_line(summary, detail, needs_shown),
                 _purpose_line(summary['not_repaired_on_purpose']),
                 tests_line(summary, tests),
                 'Pull request: ' + pr_url if pr_url else '',
                 'Run: ' + run_url if run_url else '']
        text = '\n'.join(line for line in lines if line)
        return slack_escape(text) if slack else text

    needs_shown, keys_shown = MAX_KEYS, MAX_KEYS
    text = build(needs_shown, keys_shown)
    while len(text) > limit and (needs_shown > 0 or keys_shown > 0):
        if needs_shown > 0:
            needs_shown -= 1
        else:
            keys_shown -= 1
        text = build(needs_shown, keys_shown)
    return _cut(text, limit)
