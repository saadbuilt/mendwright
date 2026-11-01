"""Record the runs the main cut shows, with the mendwright installed here, into capture_main.json.

python capture_main.py --source PATH [--out capture_main.json]

PATH is the mendwright checkout the installed package was built from: its git revision is
recorded, and the installed __init__.py must be byte for byte the checkout's. The local model
must be in Ollama (mendwright model install), and Playwright's Chromium installed.

Every page is the made-up shop (pages.json, and the popup and steps pages of
capture_flow.json and capture_steps.json), served at https://northwind.example by
page.route(), so nothing reaches the network and the store saves the page's address.
Each run gets the store it needs in a temporary folder:

  A_plain_v1             plain Playwright on today's page
  B_plain_banner         plain Playwright on today's page with a cookie banner: the click times out
  C_mendwright_banner    mendwright.open(page, cookies="reject") on the banner page, a fresh store
  M_mendwright_v1        mendwright.open(page) on today's page, a fresh store: it remembers
  D_plain_v2             plain Playwright on the redesign: the click times out
  D_mendwright_v2        mendwright on the redesign, with M's store: three repairs
  E_mendwright_v2_again  the same again, with D's store: the saved repairs
  F1                     mendwright.open(page, dismiss=True) on the redesign with a newsletter popup
  steps                  heal.step() and heal.run_steps() on today's, the switched and the extra flow

The script is the one the main cut shows (D.script); each step's time and the log lines it
wrote, with when, are kept. Nothing here edits a result: the JSON is what the runs did.
Before the runs, Ollama is asked to load the model (it keeps it loaded between heals), so
the model's time in a log line is its answer, not Ollama reading 1 GB from disk.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

import mendwright
from mendwright.coordinator import HealCoordinator
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ORIGIN = 'https://northwind.example'
VIEWPORT = [1080, 736]
MODEL = 'mendwright-selector'
# the main cut's script, step by step
SCRIPT = [('click', '#email'), ('type', 'ada@example.com'), ('check', '#accept-terms'), ('click', '#continue')]
# the steps act: (name, when, kwargs) and what each step does
STEPS = [('shipping', '#ship-name', {}), ('payment', '#card-number', {}), ('review', 'text="Place order"', {'last': True})]


def load(name):
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        return json.load(f)


def shop_pages():
    """Today's page with and without its cookie banner, and the redesign (pages.json)."""
    p = load('pages.json')
    banner = p['v1']
    i = banner.index('<div id="cc-wrap"')
    plain = banner[:i].rstrip() + '\n</body></html>'
    assert 'cc-wrap' not in plain and '</body></html>' in banner[i:]
    return {'v1': plain, 'v1_banner': banner, 'v2': p['v2']}


class Lines(logging.Handler):
    """mendwright's log lines, each with the time it came."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append((time.perf_counter(), record.levelname, record.getMessage()))


LOG = Lines()
logging.getLogger('mendwright').addHandler(LOG)
logging.getLogger('mendwright').setLevel(logging.INFO)

# The fingerprint tier's own verdict for each key the D run healed: what it applied, or why not.
NOTES = []
_tier = HealCoordinator._fingerprint_tier


def _noted(self, selector_key, *a, **k):
    out = _tier(self, selector_key, *a, **k)
    NOTES.append({'key': selector_key, 'applied': out[0] or None, 'note': out[1]})
    return out


HealCoordinator._fingerprint_tier = _noted


def browser_page(p, routes):
    """A fresh browser and page; routes maps a path of northwind.example to its HTML."""
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': VIEWPORT[0], 'height': VIEWPORT[1]})

    def serve(route):
        path = route.request.url[len(ORIGIN):].split('?')[0] or '/'
        if path in routes:
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=routes[path])
        else:
            route.fulfill(status=404, content_type='text/plain', body='not found')

    page.route(ORIGIN + '/**', serve)
    return browser, page


def error_line(e):
    return traceback.format_exception_only(type(e), e)[-1].strip().split('\n')[0]


def run_script(page, *, plain_level=False, stop=True):
    """The main cut's script on page (mendwright's or the plain one), step by step."""
    steps = []
    for verb, arg in SCRIPT:
        n = len(LOG.lines)
        t0 = time.perf_counter()
        rec = {'verb': verb, 'arg': arg}
        try:
            if verb == 'type':
                page.keyboard.type(arg)
            else:
                getattr(page, verb)(arg)
            rec['ok'] = True
        except Exception as e:      # a failed step is a result: keep it, stop the script
            rec.update(ok=False, error_type=type(e).__name__, error=str(e), error_line=error_line(e))
        t1 = time.perf_counter()
        rec['seconds'] = round(t1 - t0, 3)
        got = LOG.lines[n:]
        rec['log'] = [(lv + ' ' + m) if plain_level else m for _, lv, m in got]
        rec['log_at'] = [round(t - t0, 3) for t, _, _ in got]
        steps.append(rec)
        if not rec['ok'] and stop:
            break
    return steps


def total(steps):
    return round(sum(s['seconds'] for s in steps), 3)


def store_of(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)['selectors']


def journal_of(path):
    jp = os.path.join(os.path.dirname(path), 'journal.jsonl')
    if not os.path.exists(jp):
        return []
    with open(jp, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def heals_of(heal):
    return [{'key': h.key, 'old': str(h.old), 'new': h.new, 'source': h.source} for h in heal.heals]


def winners(sel):
    out = {}
    for key, entry in sel.items():
        heals = [c for c in entry['candidates'] if c.get('source') == 'heal' and c.get('status') == 'active']
        if heals:
            out[key] = max(heals, key=lambda c: c.get('lastSucceededAt') or '')
    return out


def warm_model():
    """Have Ollama load the model now: a request with no prompt only loads it."""
    body = json.dumps({'model': MODEL, 'prompt': '', 'keep_alive': '15m'}).encode()
    req = urllib.request.Request('http://127.0.0.1:11434/api/generate', data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=300) as r:
        r.read()


def model_info():
    with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=10) as r:
        tags = json.load(r)['models']
    m = next(t for t in tags if t['name'].split(':')[0] == MODEL)
    return {'name': m['name'], 'bytes': m['size'], 'parameter_size': m.get('details', {}).get('parameter_size')}


def main_runs(p, tmp, pages):
    out = {}
    url = ORIGIN + '/checkout'
    # A: plain Playwright, today's page
    b, page = browser_page(p, {'/checkout': pages['v1']})
    page.goto(url)
    out['A_plain_v1'] = run_script(page)
    b.close()
    # B: plain Playwright, the banner takes the click
    b, page = browser_page(p, {'/checkout': pages['v1_banner']})
    page.goto(url)
    out['B_plain_banner'] = run_script(page)
    out['B_banner_up_after'] = page.locator('#cc-wrap').count() > 0
    b.close()
    # C: mendwright rejects the banner
    b, page = browser_page(p, {'/checkout': pages['v1_banner']})
    page.goto(url)
    heal = mendwright.open(page, cookies='reject', store_path=os.path.join(tmp, 'C', '.mendwright', 'overrides.json'))
    out['C_mendwright_banner'] = run_script(heal.page)
    out['C_seconds'] = total(out['C_mendwright_banner'])
    out['C_banner_up_after'] = page.locator('#cc-wrap').count() > 0
    out['C_heals'] = heals_of(heal)
    b.close()
    # M: today's page, a fresh store
    store = os.path.join(tmp, 'main', '.mendwright', 'overrides.json')
    b, page = browser_page(p, {'/checkout': pages['v1']})
    page.goto(url)
    heal = mendwright.open(page, store_path=store)
    out['M_mendwright_v1'] = run_script(heal.page)
    out['M_seconds'] = total(out['M_mendwright_v1'])
    out['M_heals'] = heals_of(heal)
    sel = store_of(store)
    out['M_fingerprints'] = {k: bool(sel.get(k, {}).get('fingerprint')) for _, k in SCRIPT if k.startswith('#')}
    b.close()
    # D: plain Playwright on the redesign
    b, page = browser_page(p, {'/checkout': pages['v2']})
    page.goto(url)
    out['D_plain_v2'] = run_script(page)
    b.close()
    # D: mendwright on the redesign, with M's store
    b, page = browser_page(p, {'/checkout': pages['v2']})
    page.goto(url)
    heal = mendwright.open(page, store_path=store)
    out['D_timeout_ms'] = heal.timeout_ms
    NOTES.clear()
    j0 = len(journal_of(store))
    out['D_mendwright_v2'] = run_script(heal.page)
    out['D_seconds'] = total(out['D_mendwright_v2'])
    out['D_heals'] = heals_of(heal)
    out['D_fingerprint_notes'] = list(NOTES)
    events = journal_of(store)[j0:]
    proposed = {}
    for e in events:
        if e['event'] == 'heal.proposed':
            proposed[e['key']] = {'key': e['key'], 'xpath': e['xpath'], 'reasoning': e.get('reasoning')}
    out['D_proposed'] = list(proposed.values())
    out['D_journal'] = events
    out['D_winners'] = winners(store_of(store))
    out['D_checked'] = page.evaluate("() => [...document.querySelectorAll('input[type=checkbox]')].map(c => [c.id, c.checked])")
    out['D_email_value'] = page.evaluate("() => document.querySelector('input[name=email]').value")
    b.close()
    # E: the next run, with D's store
    b, page = browser_page(p, {'/checkout': pages['v2']})
    page.goto(url)
    heal = mendwright.open(page, store_path=store)
    j0 = len(journal_of(store))
    out['E_mendwright_v2_again'] = run_script(heal.page)
    out['E_seconds'] = total(out['E_mendwright_v2_again'])
    out['E_heals'] = heals_of(heal)
    out['E_journal'] = journal_of(store)[j0:]
    out['E_winners'] = winners(store_of(store))
    b.close()
    return out


def popup_run(p, tmp):
    """F1: a newsletter popup nobody registered, dismiss=True, a fresh store."""
    fl = load('capture_flow.json')
    store = os.path.join(tmp, 'F1', '.mendwright', 'overrides.json')
    b, page = browser_page(p, {'/checkout': fl['pages']['v2_nl']})
    page.goto(ORIGIN + '/checkout')
    fresh = not os.path.exists(store)
    heal = mendwright.open(page, dismiss=True, store_path=store)
    n = len(LOG.lines)
    t0 = time.perf_counter()
    step = {'step': 'input[name=email]'}
    try:
        heal.page.click('input[name=email]')
        step['ok'] = True
    except Exception as e:
        step.update(ok=False, error_type=type(e).__name__, error=str(e))
    t1 = time.perf_counter()
    got = LOG.lines[n:]
    step.update(seconds=round(t1 - t0, 3), log=[lv + ' ' + m for _, lv, m in got], log_at=[round(t - t0, 3) for t, _, _ in got])
    f1 = {'steps': [step], 'seconds': step['seconds'], 'open_kwargs': {'dismiss': True}, 'click': 'input[name=email]',
          'click_kwargs': {}, 'store_fresh': fresh,
          'popup_up_after': page.locator('#nl').count() > 0,
          'sign_up': 'clicked' if page.evaluate('() => window.mwSignedUp === true') else 'never clicked'}
    b.close()
    return {'F1': f1, 'pages': {'v2_nl': fl['pages']['v2_nl']}}


def steps_runs(p, tmp):
    st = load('capture_steps.json')
    out = {'pages': st['pages'], 'open_kwargs': {}, 'steps': [[n, w, k] for n, w, k in STEPS]}
    for flow in ('today', 'switched', 'extra'):
        b, page = browser_page(p, st['pages'][flow])
        visited = []
        page.on('framenavigated', lambda fr: fr == page.main_frame and visited.append(fr.url[len(ORIGIN):]))
        heal = mendwright.open(page, store_path=os.path.join(tmp, 'steps-' + flow, '.mendwright', 'overrides.json'))

        def shipping(pg):
            pg.fill('#ship-name', 'Ada Lovelace')
            pg.click('#next')

        def payment(pg):
            pg.fill('#card-number', '4242 4242 4242 4242')
            pg.click('#next')

        def review(pg):
            pg.click('text="Place order"')

        for (name, when, kw), fn in zip(STEPS, (shipping, payment, review)):
            heal.step(name, when, fn, **kw)
        heal.page.goto(ORIGIN + '/checkout')
        n = len(LOG.lines)
        t0 = time.perf_counter()
        rec = {'flow': flow}
        try:
            rec['ran'] = heal.run_steps()
            rec['ok'] = True
        except Exception as e:
            rec.update(ok=False, error_type=type(e).__name__, error=str(e), error_line=error_line(e))
        t1 = time.perf_counter()
        got = LOG.lines[n:]
        rec.update(seconds=round(t1 - t0, 3), timeout_ms=heal.timeout_ms, log=[lv + ' ' + m for _, lv, m in got],
                   log_at=[round(t - t0, 3) for t, _, _ in got], visited=visited, final_url=page.url,
                   done=page.url.endswith('/checkout/done'))
        out[flow] = rec
        b.close()
    return out


def provenance(source):
    def sha(path):
        with open(path, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    installed = sha(mendwright.__file__)
    checkout = sha(os.path.join(source, 'mendwright', '__init__.py'))
    if installed != checkout:
        raise SystemExit('the installed mendwright is not the checkout at ' + source)
    git = lambda *a: subprocess.run(['git', '-C', source] + list(a), capture_output=True, text=True).stdout.strip()
    return {'version': mendwright.__version__, 'git_rev': git('rev-parse', 'HEAD'),
            'git_dirty': bool(git('status', '--porcelain')), 'init_sha256': installed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True, help='the mendwright checkout the installed package was built from')
    ap.add_argument('--out', default=os.path.join(HERE, 'capture_main.json'))
    a = ap.parse_args()
    os.environ['MENDWRIGHT_LOCAL_MODEL'] = MODEL
    for k in ('MENDWRIGHT_STORE', 'MENDWRIGHT_TIMEOUT_MS', 'MENDWRIGHT_DISABLE', 'MENDWRIGHT_DRY_RUN', 'ANTHROPIC_API_KEY',
              'OPENAI_API_KEY', 'GEMINI_API_KEY', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_PROVIDER'):
        os.environ.pop(k, None)
    data = dict(provenance(a.source), viewport=VIEWPORT, model=model_info())
    warm_model()
    pages = shop_pages()
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        data.update(main_runs(p, tmp, pages))
        data['flow'] = popup_run(p, tmp)
        data['steps'] = steps_runs(p, tmp)
    write(a.out, data)


def local_bits(text):
    """What in text would name this machine: the folder of home folders, or the user's name."""
    home = os.path.expanduser('~')
    parent, user = os.path.basename(os.path.dirname(home)), os.path.basename(home)
    return [p for p in (re.escape(parent) + r'[\\/]', r'\b' + re.escape(user) + r'\b') if re.search(p, text)]


def write(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if local_bits(text):
        raise SystemExit('a local path is in the capture: %s' % local_bits(text))
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text + '\n')
    print('wrote', os.path.basename(path))


if __name__ == '__main__':
    main()
