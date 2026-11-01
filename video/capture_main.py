"""Record the runs the main cut shows, with the mendwright installed here, into capture_main.json.

python capture_main.py --source PATH [--out capture_main.json]

PATH is the mendwright checkout the installed package was built from: its git revision is
recorded, and the installed __init__.py must be byte for byte the checkout's. The local model
must be in Ollama (mendwright model install), and Playwright's Chromium installed.

Every page is the made-up shop (pages.json, the steps pages of capture_steps.json, and
the two popups below over the redesign), in black and white: each page is drawn once in
Chromium and every color is set to black or white (see BW_JS), and the runs and the video
use those pages, which the JSON keeps. They are served at https://northwind.example by
page.route(), so nothing reaches the network and the store saves the page's address.
Each run gets the store it needs in a temporary folder:

  A_plain_v1             plain Playwright on today's page
  B_plain_banner         plain Playwright on today's page with a cookie banner: the click times out
  C_mendwright_banner    mendwright.open(page, cookies="reject") on the banner page, a fresh store
  M_mendwright_v1        mendwright.open(page) on today's page, a fresh store: it remembers
  D_plain_v2             plain Playwright on the redesign: the click times out
  D_mendwright_v2        mendwright on the redesign, with M's store: three repairs
  E_mendwright_v2_again  the same again, with D's store: the saved repairs
  popups                 mendwright.open(page, dismiss=True), a fresh store each, on the redesign
                         with an offer (Get my code / No thanks) and then with a question
                         (Keep cart / No thanks) over the email field; neither closes on Escape
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


# Two popups nobody registered, over the redesign's email field. Neither closes on Escape,
# and both have a "No thanks". The offer reads as a promotion (% off): mendwright may decline
# it with "No thanks", and never clicks "Get my code". The question has a Keep button, so it
# asks something: there "No thanks" would be an answer (throw the cart away), and mendwright
# presses Escape and nothing more. The page notes each button clicked in window.mwClicked.
_NOTE = "(window.mwClicked = window.mwClicked || []).push(this.textContent.trim())"
_OVERLAY = 'position:fixed;inset:0;background:rgba(18,16,40,.52);display:flex;align-items:center;justify-content:center'
_CARD = 'background:#fff;border-radius:18px;padding:28px 30px;width:440px;box-shadow:0 20px 60px rgba(0,0,0,.28)'
_BTN = 'width:100%;padding:13px;border:0;border-radius:10px;font-size:16px;font-weight:600'
POPUPS = {
    'offer': ('<div id="offer" role="dialog" aria-modal="true" aria-labelledby="offer-t" style="%s">'
              '<div class="pop-card" style="%s">'
              '<div id="offer-t" style="font-size:25px;font-weight:700;margin:0 0 8px">Get 15%% off your first order</div>'
              '<div style="color:#57606a;font-size:15px;line-height:1.45;margin-bottom:16px">Enter your email and we will send you a one-time code.</div>'
              '<input type="email" placeholder="Your email" style="width:100%%;padding:12px;border:1px solid #d0d7de;border-radius:9px;font-size:16px;margin-bottom:12px">'
              '<button type="button" class="pop-yes" onclick="%s" style="%s;background:#3b2a86;color:#fff">Get my code</button>'
              '<button type="button" class="pop-no" onclick="%s;document.getElementById(\'offer\').remove()" style="%s;margin-top:8px;background:none;color:#57606a;font-weight:400">No thanks</button>'
              '</div></div>') % (_OVERLAY, _CARD, _NOTE, _BTN, _NOTE, _BTN),
    'question': ('<div id="ask" role="dialog" aria-modal="true" aria-labelledby="ask-t" style="%s">'
                 '<div class="pop-card" style="%s">'
                 '<div id="ask-t" style="font-size:25px;font-weight:700;margin:0 0 8px">Restore your saved cart?</div>'
                 '<div style="color:#57606a;font-size:15px;line-height:1.45;margin-bottom:18px">You left 2 items in your cart last time.</div>'
                 '<div style="display:flex;gap:10px">'
                 '<button type="button" class="ask-keep" onclick="%s;document.getElementById(\'ask\').remove()" style="%s;background:#3b2a86;color:#fff">Keep cart</button>'
                 '<button type="button" class="ask-no" onclick="%s;document.getElementById(\'ask\').remove()" style="%s;background:#fff;color:#3b2a86;border:1px solid #d0d7de">No thanks</button>'
                 '</div></div></div>') % (_OVERLAY, _CARD, _NOTE, _BTN, _NOTE, _BTN),
}

# Black and white: every element's background is set to black or white (a see-through one
# keeps its alpha), its text to white on black and to black or grey on white, its borders to
# black or grey, and shadows, gradients and the checkbox's accent color go. What the page
# says and how it is built stay as they were.
BW_JS = r"""() => {
  const parse = c => { const m = /rgba?\(([^)]+)\)/.exec(c); if (!m) return null;
    const v = m[1].split(',').map(parseFloat); return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 }; };
  const lum = c => (0.2126 * c.r + 0.7152 * c.g + 0.0722 * c.b) / 255;
  const set = (e, k, v) => e.style.setProperty(k, v, 'important');
  const dark = new Map();
  for (const e of document.body.querySelectorAll('*')) {
    const s = getComputedStyle(e), bg = parse(s.backgroundColor), fg = parse(s.color);
    let d = dark.get(e.parentElement) || false;
    if (bg && bg.a > 0) {
      const isDark = lum(bg) < 0.5;
      if (bg.a >= 0.99) { set(e, 'background-color', isDark ? '#000' : '#fff'); d = isDark; }
      else set(e, 'background-color', isDark ? 'rgba(0,0,0,' + bg.a + ')' : 'rgba(255,255,255,' + bg.a + ')');
    }
    dark.set(e, d);
    set(e, 'background-image', 'none');
    set(e, 'box-shadow', 'none');
    set(e, 'color', d ? '#fff' : (fg && lum(fg) > 0.45 ? '#6b6b6b' : '#000'));
    for (const side of ['top', 'right', 'bottom', 'left'])
      if (parseFloat(s['border-' + side + '-width']) > 0) {
        const b = parse(s['border-' + side + '-color']);
        set(e, 'border-' + side + '-color', b && lum(b) < 0.5 ? '#000' : '#b5b5b5');
      }
    if (e.tagName === 'INPUT') set(e, 'accent-color', '#000');
  }
  document.body.style.setProperty('background-color', '#fff', 'important');
  document.body.style.setProperty('color', '#000', 'important');
  return '<!doctype html>' + document.documentElement.outerHTML;
}"""


def black_and_white(p, pages):
    """pages ({name: html}, nested dicts too) in black and white, drawn in Chromium once each."""
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': VIEWPORT[0], 'height': VIEWPORT[1]})

    def bw(x):
        if isinstance(x, dict):
            return {k: bw(v) for k, v in x.items()}
        page.set_content(x)
        return page.evaluate(BW_JS)
    out = bw(pages)
    browser.close()
    return out


def shop_pages(p):
    """Today's page with and without its cookie banner, the redesign (pages.json), and the
    redesign with each popup over it, in black and white."""
    raw = load('pages.json')
    v2 = raw['v2']
    pages = {'v1_banner': raw['v1'], 'v2': v2}
    for name, popup in POPUPS.items():
        assert v2.count('</body>') == 1
        pages[name] = v2.replace('</body>', popup + '</body>')
    pages = black_and_white(p, pages)
    banner = pages['v1_banner']
    i = banner.index('<div id="cc-wrap"')
    pages['v1'] = banner[:i] + '</body></html>'
    assert 'cc-wrap' not in pages['v1'] and banner.endswith('</body></html>')
    return pages


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


def popup_runs(p, tmp, pages):
    """The offer, then the question: the same two lines on each, a fresh store each.
        heal = mendwright.open(page, dismiss=True)
        heal.page.click("input[name=email]")"""
    out = {'open_kwargs': {'dismiss': True}, 'click': 'input[name=email]', 'click_kwargs': {}}
    for name, root in (('offer', '#offer'), ('question', '#ask')):
        store = os.path.join(tmp, 'popup-' + name, '.mendwright', 'overrides.json')
        b, page = browser_page(p, {'/checkout': pages[name]})
        page.goto(ORIGIN + '/checkout')
        fresh = not os.path.exists(store)
        heal = mendwright.open(page, dismiss=True, store_path=store)
        n = len(LOG.lines)
        t0 = time.perf_counter()
        rec = {'name': name, 'store_fresh': fresh}
        try:
            heal.page.click('input[name=email]')
            rec['ok'] = True
        except Exception as e:
            rec.update(ok=False, error_type=type(e).__name__, error=str(e), error_line=error_line(e))
        t1 = time.perf_counter()
        got = LOG.lines[n:]
        rec.update(seconds=round(t1 - t0, 3), log=[lv + ' ' + m for _, lv, m in got], log_at=[round(t - t0, 3) for t, _, _ in got],
                   popup_up_after=page.locator(root).count() > 0,
                   clicked=page.evaluate('() => window.mwClicked || []'),
                   handled=[str(x) for x in heal.handled_interruptions])
        out[name] = rec
        b.close()
    return out


def steps_runs(p, tmp):
    st = load('capture_steps.json')
    out = {'pages': black_and_white(p, st['pages']), 'open_kwargs': {}, 'steps': [[n, w, k] for n, w, k in STEPS]}
    for flow in ('today', 'switched', 'extra'):
        b, page = browser_page(p, out['pages'][flow])
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
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        pages = shop_pages(p)
        data['pages'] = {k: pages[k] for k in ('v1_banner', 'v2', 'offer', 'question')}
        data.update(main_runs(p, tmp, pages))
        data['popups'] = popup_runs(p, tmp, pages)
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
