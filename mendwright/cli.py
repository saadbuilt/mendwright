"""The `mendwright` command (also `python -m mendwright`).

    mendwright demo      watch a broken selector get healed, offline
    mendwright doctor    check the install, browser libraries, provider and store
    mendwright report    list the selectors and heals in an override store
    mendwright --version

main(argv=None) returns the exit code instead of exiting, so it can be called from tests.
Nothing here imports a browser library or calls a model, except `mendwright demo --live`,
which asks the provider from the environment.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import sys
import textwrap
from urllib.parse import urlsplit

from .demo._story import BROWSERS, write_line

DEFAULT_STORE_PATH = './.mendwright/overrides.json'  # the same default mendwright.open() uses

BROWSER_LIBRARIES = ('playwright', 'selenium', 'patchright', 'camoufox')

# Variables auto_provider() and the providers read. doctor prints their names, never values.
PROVIDER_VARS = (
    'MENDWRIGHT_PROVIDER', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL',
    'OPENAI_API_KEY', 'OPENAI_MODEL', 'OPENAI_BASE_URL', 'GEMINI_API_KEY', 'GEMINI_MODEL',
    'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_KEY', 'MENDWRIGHT_AUTH_HEADER', 'OLLAMA_MODEL',
    'OLLAMA_HOST',
)

# MENDWRIGHT_* variables whose value is safe to print. Anything else shows as hidden.
_SHOWN_VALUES = ('MENDWRIGHT_STORE', 'MENDWRIGHT_TIMEOUT_MS', 'MENDWRIGHT_DRY_RUN',
                 'MENDWRIGHT_DISABLE', 'MENDWRIGHT_PROVIDER', 'MENDWRIGHT_AUTH_HEADER',
                 'MENDWRIGHT_FINGERPRINT')
_SECRET_WORDS = ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'PASS', 'AUTH', 'CREDENTIAL')

REPORT_FORMATS = ('text', 'md', 'json')
LAST_HEALS = 10


def _version():
    from . import __version__
    return __version__


def _out(text=''):
    write_line(text, sys.stdout)


def _err(text):
    write_line(text, sys.stderr)


def _wrap(text, indent='  ', width=88):
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent,
                         break_long_words=False, break_on_hyphens=False)


# --- doctor ----------------------------------------------------------------------------

def _scrub(text):
    """text with the value of every secret-looking environment variable masked."""
    if not isinstance(text, str):
        return text
    for name, value in os.environ.items():
        upper = name.upper()
        if value and len(value) >= 4 and any(word in upper for word in _SECRET_WORDS):
            text = text.replace(value, '***')
    return text


def _origin(url):
    """scheme://host[:port] of a URL, without user info, path or query."""
    try:
        parts = urlsplit(url if '://' in url else 'https://' + url)
        host = parts.hostname or ''
        port = ':' + str(parts.port) if parts.port else ''
        return (parts.scheme + '://' + host + port) if host else '(set, not a URL)'
    except Exception:
        return '(set, not a URL)'


def _endpoint(url):
    """scheme://host[:port]/path of a provider's base URL, without user info or query.
    None when there is no URL. The host is not a secret, and shows where the page goes."""
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url if '://' in url else 'https://' + url)
        host = parts.hostname or ''
        port = ':' + str(parts.port) if parts.port else ''
        return (parts.scheme + '://' + host + port + parts.path.rstrip('/')) if host else None
    except Exception:
        return None


def _dist_version(name):
    try:
        return importlib.metadata.version(name)
    except Exception:
        return None


def _browser_libraries():
    """Which browser libraries are on the import path. Found with find_spec, so nothing
    is imported or launched."""
    rows = []
    for name in BROWSER_LIBRARIES:
        try:
            found = importlib.util.find_spec(name) is not None
        except Exception:
            found = False
        rows.append({'name': name, 'installed': found,
                     'version': _dist_version(name) if found else None})
    return rows


def _flag_problem(name):
    from .facade import _env_flag
    try:
        return None, _env_flag(name)
    except ValueError as exc:
        return str(exc), None


def _fingerprint_info():
    """Whether mendwright.open() runs the fingerprint tier, and what decides it."""
    _problem, disabled = _flag_problem('MENDWRIGHT_DISABLE')
    problem, env = _flag_problem('MENDWRIGHT_FINGERPRINT')
    if disabled:
        return {'enabled': False, 'source': 'MENDWRIGHT_DISABLE'}
    if problem is None and env is not None:
        return {'enabled': bool(env), 'source': 'MENDWRIGHT_FINGERPRINT'}
    return {'enabled': True, 'source': 'default'}


def _provider_info():
    """Which provider auto_provider() picks, or why none is configured. No key values."""
    from .providers import GatewayProvider, auto_provider
    from .providers.detect import _SELECT_ENV

    env_set = [name for name in PROVIDER_VARS if os.environ.get(name)]
    forced = (os.environ.get('MENDWRIGHT_PROVIDER') or '').strip()
    if forced:
        selected_by = ['MENDWRIGHT_PROVIDER']
    else:
        selected_by = []
        for _, names in _SELECT_ENV:
            hits = [n for n in names if os.environ.get(n)]
            if hits:
                selected_by = hits
                break
    info = {'configured': False, 'name': None, 'model': None, 'selected_by': selected_by,
            'env_set': env_set, 'reason': None, 'gateway': None, 'endpoint': None,
            'disabled': False}
    problem, disabled = _flag_problem('MENDWRIGHT_DISABLE')
    info['disabled'] = bool(disabled)
    try:
        provider = auto_provider()
    except Exception as exc:
        info['reason'] = _scrub(str(exc).strip() or type(exc).__name__)
        return info
    info['configured'] = True
    info['name'] = type(provider).__name__
    if getattr(provider, '_ollama', False) is True:
        info['name'] = 'Ollama (OpenAI-compatible)'
    model = getattr(provider, 'model', None)
    info['model'] = model if isinstance(model, str) and model else None
    if isinstance(provider, GatewayProvider):
        if os.environ.get('MENDWRIGHT_GATEWAY'):
            info['gateway'] = _origin(os.environ['MENDWRIGHT_GATEWAY'])
    else:
        info['endpoint'] = _endpoint(getattr(provider, 'base_url', None))
    return info


def _nearest_existing(folder):
    current = folder
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current or None


def _store_info():
    """The store path open() would use, and whether its folder can be written. Creates
    nothing."""
    env_value = os.environ.get('MENDWRIGHT_STORE')
    raw = env_value or DEFAULT_STORE_PATH
    path = os.path.abspath(os.path.expanduser(raw))
    folder = os.path.dirname(path)
    info = {'path': path, 'source': 'MENDWRIGHT_STORE' if env_value else 'default',
            'exists': os.path.isfile(path), 'folder': folder,
            'folder_exists': os.path.isdir(folder), 'writable': False, 'checked': folder,
            'keys': None, 'journal': os.path.isfile(os.path.join(folder, 'journal.jsonl')),
            'error': None}
    checked = folder if info['folder_exists'] else _nearest_existing(folder)
    info['checked'] = checked
    info['writable'] = bool(checked) and os.access(checked, os.W_OK)
    if info['exists']:
        try:
            with open(path, encoding='utf-8-sig') as handle:
                data = json.loads(handle.read())
            selectors = data.get('selectors') if isinstance(data, dict) else None
            info['keys'] = len(selectors) if isinstance(selectors, (dict, list)) else 0
        except Exception as exc:
            info['error'] = 'could not read it (' + type(exc).__name__ + ')'
    return info


def _env_info():
    out = {}
    for name in sorted(os.environ):
        if not name.upper().startswith('MENDWRIGHT_'):
            continue
        value = os.environ[name]
        if name.upper() == 'MENDWRIGHT_GATEWAY':
            out[name] = _origin(value)
        elif name.upper() in _SHOWN_VALUES:
            out[name] = value
        else:
            out[name] = '(set, value hidden)'
    return out


def _settings_problems():
    from .facade import _env_timeout
    problems = []
    for name in ('MENDWRIGHT_DRY_RUN', 'MENDWRIGHT_DISABLE', 'MENDWRIGHT_FINGERPRINT'):
        problem, _ = _flag_problem(name)
        if problem:
            problems.append(problem)
    try:
        _env_timeout()
    except ValueError as exc:
        problems.append(str(exc))
    return problems


def collect_doctor():
    """Everything `mendwright doctor` reports, as a dict (the --json output)."""
    import lxml
    import lxml.etree
    from . import __file__ as package_file
    libxml2 = '.'.join(str(p) for p in getattr(lxml.etree, 'LIBXML_VERSION', ()))
    return {
        'mendwright': {'version': _version(), 'path': os.path.dirname(package_file)},
        'python': {'version': sys.version.split()[0], 'executable': sys.executable},
        'lxml': {'version': getattr(lxml, '__version__', None), 'libxml2': libxml2 or None},
        'browser_libraries': _browser_libraries(),
        'provider': _provider_info(),
        'fingerprint': _fingerprint_info(),
        'store': _store_info(),
        'env': _env_info(),
        'problems': [_scrub(p) for p in _settings_problems()],
    }


def _doctor_text(d):
    lines = []
    m, py, lx = d['mendwright'], d['python'], d['lxml']
    lines.append('mendwright ' + m['version'] + '  (' + m['path'] + ')')
    lines.append('Python ' + py['version'] + '  (' + py['executable'] + ')')
    lines.append('lxml ' + str(lx['version'])
                 + ('  (libxml2 ' + lx['libxml2'] + ')' if lx['libxml2'] else ''))
    lines.append('')

    lines.append('Browser libraries (looked up only, nothing imported or launched)')
    for row in d['browser_libraries']:
        state = (row['version'] or 'installed') if row['installed'] else 'not installed'
        lines.append('  ' + row['name'].ljust(11) + ' ' + state)
    if not any(row['installed'] for row in d['browser_libraries']):
        lines.append('  mendwright needs one of these to drive a real page. Install one with: '
                     'pip install "mendwright[playwright]"')
    lines.append('')

    p = d['provider']
    lines.append('Model provider')
    if p['configured']:
        text = p['name'] + (', model ' + p['model'] if p['model'] else '')
        if p['gateway']:
            text += ', gateway ' + p['gateway'] + ' (the model is set on the gateway)'
        lines.append('  ' + text)
        if p.get('endpoint'):
            lines.append('  endpoint: ' + p['endpoint'] + ' (heal requests go here)')
        if p['selected_by']:
            lines.append('  chosen because ' + ' and '.join(p['selected_by'])
                         + (' is' if len(p['selected_by']) == 1 else ' are') + ' set')
    else:
        if d.get('fingerprint', {}).get('enabled'):
            lines.append('  none configured, so only fingerprint healing (no model call) can '
                         'repair a broken selector.')
        else:
            lines.append('  none configured, so a broken selector is reported but not repaired.')
        lines.append(_wrap('Why: ' + p['reason']))
        lines.append('  `mendwright demo` works without one (it uses a scripted reply).')
    if p['env_set']:
        lines.append('  provider variables set: ' + ', '.join(p['env_set']) + ' (values not shown)')
    if p['disabled']:
        lines.append('  MENDWRIGHT_DISABLE is on, so mendwright.open() heals nothing and calls '
                     'no provider. Unset it to heal again.')
    lines.append('')

    f = d.get('fingerprint') or {}
    lines.append('Fingerprint healing (no model)')
    if f.get('enabled'):
        lines.append(_wrap('on: each time a selector matches, mendwright records what the '
                           'element looks like, and when the selector breaks it looks for that '
                           'element first, before any model call. Set MENDWRIGHT_FINGERPRINT=0, '
                           'or pass fingerprint=False to mendwright.open(), to turn it off.'))
    elif f.get('source') == 'MENDWRIGHT_DISABLE':
        lines.append('  off, because MENDWRIGHT_DISABLE is on. Unset it to heal again.')
    else:
        lines.append(_wrap('off (MENDWRIGHT_FINGERPRINT=0), so only a model provider can repair '
                           'a broken selector. Unset MENDWRIGHT_FINGERPRINT to turn it on.'))
    lines.append('')

    s = d['store']
    lines.append('Store (where mendwright.open() saves heals)')
    origin = ('from MENDWRIGHT_STORE' if s['source'] == 'MENDWRIGHT_STORE'
              else 'default, relative to this folder; set MENDWRIGHT_STORE to change it')
    lines.append('  ' + s['path'])
    lines.append('  (' + origin + ')')
    if s['exists']:
        if s['error']:
            lines.append('  exists, but ' + s['error'] + '. Fix or delete the file.')
        else:
            lines.append('  exists, ' + str(s['keys']) + ' key(s)'
                         + (', journal.jsonl next to it' if s['journal'] else '')
                         + '. See it with: mendwright report')
    elif s['folder_exists']:
        lines.append('  no store yet; it is created the first time mendwright.open() records '
                     'a selector')
    else:
        lines.append('  the folder does not exist yet; it is created the first time '
                     'mendwright.open() records a selector')
    if s['writable']:
        lines.append('  ' + str(s['checked']) + ' is writable')
    else:
        lines.append('  ' + str(s['checked'] or s['folder']) + ' is NOT writable, so heals '
                     'stay in memory. Set MENDWRIGHT_STORE to a writable path.')
    lines.append('')

    lines.append('Environment')
    if d['env']:
        for name, value in d['env'].items():
            lines.append('  ' + name + '=' + value)
    else:
        lines.append('  no MENDWRIGHT_* variables are set')
    if d['problems']:
        lines.append('')
        lines.append('Problems')
        for problem in d['problems']:
            lines.append(_wrap(problem))
    return lines


def cmd_doctor(args):
    data = collect_doctor()
    if args.json:
        _out(json.dumps(data, indent=2, sort_keys=True))
    else:
        for line in _doctor_text(data):
            _out(line)
    return 0


# --- report ----------------------------------------------------------------------------

class ReportError(Exception):
    pass


def default_store_path():
    return os.path.abspath(os.path.expanduser(os.environ.get('MENDWRIGHT_STORE')
                                              or DEFAULT_STORE_PATH))


def _read_store(path):
    try:
        with open(path, encoding='utf-8-sig') as handle:
            text = handle.read()
    except OSError as exc:
        raise ReportError('could not read ' + path + ' (' + (exc.strerror or str(exc))
                          + '). Check the path and its permissions.') from None
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportError(path + ' is not valid JSON (' + str(exc) + '). Fix the file, or '
                          'delete it to start an empty store.') from None
    if not isinstance(data, dict):
        data = {}
    selectors = data.get('selectors')
    if data.get('schema') == 2 and isinstance(selectors, dict):
        return data
    if isinstance(selectors, list):
        from .store import migrate_schema1
        return migrate_schema1(data)
    raise ReportError(path + ' is not a mendwright store (expected {"schema": 2, '
                      '"selectors": {...}}). Pass the overrides.json file with --store.')


def _read_journal(path):
    """The journal's events, oldest first. Unreadable lines are skipped, and a read that
    fails part way (another process holding the file) keeps the events read so far."""
    events = []
    try:
        handle = open(path, encoding='utf-8', errors='replace')
    except OSError:
        return None
    with handle:
        try:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and isinstance(event.get('event'), str):
                    events.append(event)
        except OSError:
            pass
    return events


_COUNTED = (('requested', 'heal.requested'), ('applied', 'heal.applied'),
            ('rejected', 'heal.rejected'), ('quarantined', 'heal.quarantine'))


def collect_report(path):
    """The report for the store at ``path`` as a dict (the --format json output).
    Raises ReportError when the store cannot be read."""
    data = _read_store(path)
    keys = []
    for key in sorted(data['selectors']):
        entry = data['selectors'][key]
        if not isinstance(entry, dict):
            continue
        candidates = []
        for c in entry.get('candidates') or []:
            if not isinstance(c, dict) or not c.get('xpath'):
                continue
            row = {'selector': str(c['xpath']), 'source': c.get('source') or 'unknown',
                   'status': c.get('status') or 'active',
                   'last_success': c.get('lastSucceededAt'), 'added': c.get('addedAt')}
            if c.get('frameXPath'):
                row['frame'] = str(c['frameXPath'])
            candidates.append(row)
        keys.append({'key': key, 'role': entry.get('expectedRole'),
                     'destructive': entry.get('destructive') is True,
                     'candidates': candidates})

    journal_path = os.path.join(os.path.dirname(path), 'journal.jsonl')
    events = _read_journal(journal_path)
    journal = {'path': journal_path, 'exists': events is not None,
               'counts': {name: 0 for name, _ in _COUNTED}, 'per_key': {}, 'last_applied': []}
    for event in events or ():
        for name, event_name in _COUNTED:
            if event['event'] == event_name:
                journal['counts'][name] += 1
                per = journal['per_key'].setdefault(str(event.get('key')),
                                                    {n: 0 for n, _ in _COUNTED})
                per[name] += 1
    applied = [e for e in events or () if e['event'] == 'heal.applied']
    for e in reversed(applied[-LAST_HEALS:]):
        old = e.get('old')
        journal['last_applied'].append({
            'when': e.get('ts'), 'key': e.get('key'),
            'old': [str(o) for o in old] if isinstance(old, list) else [],
            'new': e.get('xpath'), 'provider': e.get('provider'), 'model': e.get('model'),
            'latency_ms': e.get('latency_ms')})
    return {'store': path, 'keys': keys, 'journal': journal}


def _when(ts):
    """'2026-10-10T09:30:00+00:00' -> '2026-10-10 09:30:00 UTC'."""
    if not isinstance(ts, str) or not ts:
        return 'never'
    text = ts.replace('T', ' ')
    for suffix in ('+00:00', 'Z'):
        if text.endswith(suffix):
            return text[:-len(suffix)] + ' UTC'
    return text


def _role_note(k):
    parts = [k['role'] or 'role unknown']
    if k['destructive']:
        parts.append('destructive, never auto-healed')
    return ', '.join(parts)


def _counts_sentence(counts):
    return ', '.join(str(counts[name]) + ' ' + name for name, _ in _COUNTED)


def _who(heal):
    who = heal.get('provider') or 'unknown provider'
    if heal.get('model'):
        who += ', ' + str(heal['model'])
    return who


def _old_text(heal):
    old = heal['old']
    if not old:
        return '(none)'
    return old[0] + (' (+' + str(len(old) - 1) + ' more)' if len(old) > 1 else '')


def _report_text(r):
    lines = ['Store: ' + r['store']]
    total = sum(len(k['candidates']) for k in r['keys'])
    lines.append(str(len(r['keys'])) + ' key(s), ' + str(total) + ' selector(s)')
    for k in r['keys']:
        lines.append('')
        lines.append(k['key'] + '  (' + _role_note(k) + ')')
        per = r['journal']['per_key'].get(k['key'])
        for c in k['candidates']:
            frame = '  in frame ' + c['frame'] if c.get('frame') else ''
            lines.append('  ' + c['status'].ljust(11) + ' ' + c['source'].ljust(4) + '  '
                         + c['selector'] + frame + '  (last success: ' + _when(c['last_success'])
                         + ')')
        if per:
            lines.append('  heals: ' + _counts_sentence(per))
    if not r['keys']:
        lines.append('The store has no keys yet.')
    lines.append('')
    j = r['journal']
    if not j['exists']:
        lines.append('No journal.jsonl next to the store, so there are no heal counts.')
        return lines
    lines.append('Heals (journal.jsonl): ' + _counts_sentence(j['counts']))
    if j['last_applied']:
        lines.append('Last applied heals, newest first:')
        for h in j['last_applied']:
            lines.append('  ' + _when(h['when']) + '  ' + str(h['key']) + '  (' + _who(h) + ')')
            lines.append('    ' + _old_text(h) + ' -> ' + str(h['new']))
    return lines


def _md_code(text):
    """text as a Markdown code span that is safe inside a table cell."""
    text = ' '.join(str(text).split()).replace('|', '\\|')
    if '`' in text:
        return '`` ' + text + ' ``'
    return '`' + text + '`'


def _md_cell(text):
    return ' '.join(str(text).split()).replace('|', '\\|')


def _report_md(r):
    total = sum(len(k['candidates']) for k in r['keys'])
    lines = ['## mendwright heal report', '',
             'Store: ' + _md_code(r['store']) + ' with ' + str(len(r['keys'])) + ' key(s) and '
             + str(total) + ' selector(s).', '']
    if r['keys']:
        lines.append('| Key | Selector | Source | Status | Last success |')
        lines.append('| --- | --- | --- | --- | --- |')
        for k in r['keys']:
            first = True
            label = _md_code(k['key']) + ' (' + _md_cell(_role_note(k)) + ')'
            for c in k['candidates'] or [None]:
                cell = label if first else ''
                first = False
                if c is None:
                    lines.append('| ' + cell + ' | (none) | | | |')
                    continue
                status = c['status'] if c['status'] == 'active' else '**' + c['status'] + '**'
                selector = _md_code(c['selector'])
                if c.get('frame'):
                    selector += ' in frame ' + _md_code(c['frame'])
                lines.append('| ' + cell + ' | ' + selector + ' | ' + _md_cell(c['source'])
                             + ' | ' + status + ' | ' + _md_cell(_when(c['last_success'])) + ' |')
    else:
        lines.append('The store has no keys yet.')
    lines.append('')
    j = r['journal']
    if not j['exists']:
        lines.append('No `journal.jsonl` next to the store, so there are no heal counts.')
        return lines
    c = j['counts']
    lines.append('### Heals')
    lines.append('')
    lines.append('| Requested | Applied | Rejected | Quarantined |')
    lines.append('| ---: | ---: | ---: | ---: |')
    lines.append('| ' + ' | '.join(str(c[name]) for name, _ in _COUNTED) + ' |')
    if j['last_applied']:
        lines.append('')
        lines.append('Last applied heals, newest first:')
        lines.append('')
        lines.append('| When | Key | Old | New | Provider |')
        lines.append('| --- | --- | --- | --- | --- |')
        for h in j['last_applied']:
            lines.append('| ' + _md_cell(_when(h['when'])) + ' | ' + _md_code(h['key']) + ' | '
                         + (_md_code(_old_text(h)) if h['old'] else '(none)') + ' | '
                         + _md_code(h['new']) + ' | ' + _md_cell(_who(h)) + ' |')
    return lines


def cmd_report(args):
    path = (os.path.abspath(os.path.expanduser(args.store)) if args.store
            else default_store_path())
    if not os.path.isfile(path):
        if args.store:
            hint = 'Check the path given to --store; the file is created by the first heal'
        else:
            hint = ('Run your tests with mendwright first, or pass --store PATH (the default is '
                    'MENDWRIGHT_STORE, else ./.mendwright/overrides.json)')
        _err('mendwright report: no store at ' + path + '. ' + hint + '.')
        return 1
    try:
        report = collect_report(path)
    except ReportError as exc:
        _err('mendwright report: ' + str(exc))
        return 1
    if args.format == 'json':
        _out(json.dumps(report, indent=2))
    else:
        for line in (_report_md(report) if args.format == 'md' else _report_text(report)):
            _out(line)
    return 0


# --- demo ------------------------------------------------------------------------------

def cmd_demo(args):
    from .demo import run
    return run(browser=args.browser, keep=args.keep, slow=args.slow, live=args.live)


# --- wiring ----------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog='mendwright',
        description='Self-healing selectors for Playwright and Selenium. When a selector '
                    'breaks, a model suggests a new one, mendwright checks it against the '
                    'page, and the step is retried.',
        epilog='Run "mendwright <command> --help" for the options of one command.')
    parser.add_argument('-V', '--version', action='store_true',
                        help='print the version and exit')
    sub = parser.add_subparsers(dest='command', metavar='<command>')

    demo = sub.add_parser(
        'demo', help='watch a broken selector get healed (offline, no key needed)',
        description='A redesign breaks two selectors on a checkout page. mendwright heals the '
                    'checkbox and refuses to heal the pay button. Runs offline with a stand-in '
                    'page and a scripted model reply (no API call); --live asks the model '
                    'provider from the environment instead. Writes only to a temp folder, '
                    'which is removed afterwards.')
    demo.add_argument('--browser', nargs='?', const='chromium', choices=BROWSERS,
                      metavar='{chromium,firefox,webkit}',
                      help='run the same story in a real headless Playwright browser '
                           '(default: chromium). Needs Playwright and that browser installed.')
    demo.add_argument('--keep', action='store_true',
                      help='keep the temp store and journal, and print where they are')
    demo.add_argument('--slow', action='store_true',
                      help='pause between lines, for recording a terminal session')
    demo.add_argument('--live', action='store_true',
                      help='ask the model provider from the environment (the one '
                           'mendwright doctor shows) instead of the scripted reply. Makes a '
                           'real API call and sends the demo page to that provider.')
    demo.set_defaults(handler=cmd_demo)

    doctor = sub.add_parser(
        'doctor', help='check the install, browser libraries, provider and store',
        description='Show versions, which browser libraries are installed (without importing '
                    'or launching them), which model provider mendwright.open() would use, '
                    'and where heals are stored. Never prints key values. Always exits 0.')
    doctor.add_argument('--json', action='store_true', help='print the result as JSON')
    doctor.set_defaults(handler=cmd_doctor)

    report = sub.add_parser(
        'report', help='list the selectors and heals in an override store',
        description='List each key in the store with its selectors (source, status, last '
                    'success), and the heal counts from journal.jsonl next to it. '
                    '--format md pastes into $GITHUB_STEP_SUMMARY.')
    report.add_argument('--store', metavar='PATH',
                        help='the override store file (default: MENDWRIGHT_STORE, else '
                             './.mendwright/overrides.json)')
    report.add_argument('--format', choices=REPORT_FORMATS, default='text',
                        help='output format (default: text)')
    report.set_defaults(handler=cmd_report)
    return parser


def main(argv=None) -> int:
    """Run the command line with ``argv`` (default: sys.argv[1:]) and return the exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help, or a usage error argparse already printed
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 2)
    if args.version:
        _out('mendwright ' + _version())
        return 0
    handler = getattr(args, 'handler', None)
    if handler is None:
        parser.print_help()
        return 0
    try:
        return handler(args)
    except KeyboardInterrupt:
        _err('mendwright: interrupted.')
        return 130
