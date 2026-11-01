"""Record the runs the main cut shows, with the mendwright installed here, into capture_main.json.

python capture_main.py --source PATH [--out capture_main.json]

PATH is the mendwright checkout the installed package was built from: its git revision is
recorded, and the installed __init__.py must be byte for byte the checkout's. The local model
must be in Ollama (mendwright model install), and Playwright's Chromium installed.

The pages are the made-up shop of pages.json, today's checkout and its redesign, each with
an order summary beside the form (in the same grid as the form; the form's own markup and
the elements around it are pages.json's), and the redesign with a popup: a promotion that
covers the email field, and a dialog that asks to confirm the email address once the terms
are accepted. Each page is drawn once in Chromium in black and white (see BW_JS), and the
runs and the video use those pages, which the JSON keeps. They are served at
https://northwind.example by page.route(), so nothing reaches the network and the store
saves the page's address.

  M_mendwright_v1        mendwright.open(page) on today's page, a fresh store: it remembers
  D_mendwright_v2        on the redesign, with M's store: three repairs
  E_mendwright_v2_again  the same again, with D's store: the saved repairs
  popups.offer           OFFER_SCRIPT on the redesign with the promotion, with D's store
  popups.confirm         CONFIRM_SCRIPT on the redesign with the confirm dialog, with D's store

The main script is the one the main cut shows (D.script); each step's time and the log
lines it wrote, with when, are kept. The two popup scripts are run as written (exec), and the
JSON keeps their text. Nothing here edits a result: the JSON is what the runs did.
Before the runs, Ollama is asked to load the model (it keeps it loaded between heals), so
the model's time in a log line is its answer, not Ollama reading 1 GB from disk.
"""
import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
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

# The popup beats' scripts, run as written. The page the main script has is `page`.
OFFER_SCRIPT = '''page = mendwright.open(
    page, dismiss=True).page
page.click("#email")
'''
CONFIRM_SCRIPT = '''heal = mendwright.open(page)

def confirm_email(page):
    page.fill("#confirm-email", "ada@example.com")
    page.click("#confirm-email-ok")

heal.interrupt("confirm_email",
    "#confirm-email-dialog",
    run=confirm_email)
page = heal.page
page.click("#email")
page.keyboard.type("ada@example.com")
page.check("#accept-terms")
page.click("#continue")
'''


def load(name):
    with open(os.path.join(HERE, name), encoding='utf-8') as f:
        return json.load(f)


# ---------- the pages ----------
# An order summary for each version of the checkout, in its own style. It goes in the grid of
# the form's container, as a sibling of the form card, so the form and its ancestors are the
# same as in pages.json (the fingerprint reads up to 4 ancestors).
SUMMARY_V1 = '''<style>.wrap{display:grid;grid-template-columns:470px 280px;column-gap:28px;align-items:start}
.wrap h1,.wrap .sub,.wrap .card{grid-column:1}
.sum{grid-column:2;grid-row:3;background:#fff;border:1px solid #d8d2c4;border-radius:10px;padding:20px 22px}
.sum h2{font-size:16px;margin:0 0 14px}
.item{display:flex;gap:12px;align-items:center;margin:0 0 16px;font-size:15px}
.item .mt{color:#57606a;font-size:13.5px;margin-top:2px} .item .pr{margin-left:auto;font-weight:600}
.ln{display:flex;justify-content:space-between;font-size:15px;margin:0 0 9px;color:#3d434b}
.tot{border-top:1px solid #e3ddd0;padding-top:12px;margin:12px 0 0;font-weight:700;color:#1f2328}
.top{display:flex;justify-content:space-between;align-items:center} .top span{font-size:14px;font-weight:400;letter-spacing:0}</style>'''
ASIDE_V1 = '''<aside class="sum"><h2>Order summary</h2>
  <div class="item"><div><div>Trail jacket</div><div class="mt">Size M</div></div><div class="pr">$129.00</div></div>
  <div class="ln"><span>Subtotal</span><span>$129.00</span></div>
  <div class="ln"><span>Shipping</span><span>Free</span></div>
  <div class="ln tot"><span>Total</span><span>$129.00</span></div>
</aside>'''
SUMMARY_V2 = '''<style>.x8k2{display:grid;grid-template-columns:500px 270px;column-gap:28px;align-items:start}
.x8k2 h2,.x8k2 .f9a1{grid-column:1}
.s4m{grid-column:2;grid-row:2;background:#fff;border:1px solid #e6e8ee;border-radius:14px;padding:20px 22px}
.s4m h3{font-size:15px;margin:0 0 14px;color:#57606a;font-weight:600}
.s4m .item{display:flex;gap:12px;align-items:center;margin:0 0 16px;font-size:15px}
.s4m .mt{color:#6e7781;font-size:13.5px;margin-top:2px} .s4m .pr{margin-left:auto;font-weight:600}
.s4m .ln{display:flex;justify-content:space-between;font-size:15px;margin:0 0 9px;color:#57606a}
.s4m .tot{border-top:1px solid #e6e8ee;padding-top:12px;margin:12px 0 0;font-weight:700;color:#16181d}</style>'''
ASIDE_V2 = '''<aside class="s4m"><h3>Your order</h3>
  <div class="item"><div><div>Trail jacket</div><div class="mt">Size M</div></div><div class="pr">$129.00</div></div>
  <div class="ln"><span>Subtotal</span><span>$129.00</span></div>
  <div class="ln"><span>Shipping</span><span>Free</span></div>
  <div class="ln tot"><span>Total</span><span>$129.00</span></div>
</aside>'''

# Two popups over the redesign. Neither closes on Escape. The promotion covers the page when
# it loads; it has an email field, Subscribe and No thanks. The confirm dialog opens when the
# terms are accepted, over the Continue button; Confirm closes it when its email is the one in
# the form. The page notes what was clicked: window.mwClicked, mwConfirmed and mwContinued.
_OVERLAY = 'position:fixed;inset:0;background:rgba(22,24,29,.5);display:flex;align-items:center;justify-content:center'
_CARD = 'background:#fff;border-radius:14px;padding:26px 28px;width:420px;box-shadow:0 20px 60px rgba(0,0,0,.25)'
_FIELD = 'width:100%;padding:12px;border:1px solid #d0d7de;border-radius:8px;font-size:16px;margin:0 0 12px'
_PRIMARY = 'width:100%;padding:13px;border:0;border-radius:8px;font-size:16px;font-weight:600;background:#16181d;color:#fff'
_NOTE = "(window.mwClicked = window.mwClicked || []).push(this.textContent.trim())"
OFFER = ('<div id="offer" role="dialog" aria-modal="true" aria-labelledby="offer-t" style="%s">'
         '<div class="pop-card" style="%s">'
         '<div style="font-size:12.5px;letter-spacing:.08em;text-transform:uppercase;color:#6e7781;font-weight:600">Northwind newsletter</div>'
         '<div id="offer-t" style="font-size:23px;font-weight:700;margin:6px 0 8px">Get 15%% off your first order</div>'
         '<div style="color:#57606a;font-size:15px;line-height:1.45;margin:0 0 16px">Sign up for early access to sales and new arrivals.</div>'
         '<input type="email" placeholder="Email address" style="%s">'
         '<button type="button" class="pop-yes" onclick="%s" style="%s">Subscribe</button>'
         '<button type="button" class="pop-no" onclick="%s;document.getElementById(\'offer\').remove()" '
         'style="width:100%%;margin-top:6px;padding:10px;border:0;background:none;color:#57606a;font-size:15px">No thanks</button>'
         '</div></div>') % (_OVERLAY, _CARD, _FIELD, _NOTE, _PRIMARY, _NOTE)
CONFIRM = ('<div id="confirm-email-dialog" role="dialog" aria-modal="true" aria-labelledby="ce-t" style="%s;display:none">'
           '<div class="pop-card" style="%s">'
           '<div id="ce-t" style="font-size:22px;font-weight:700;margin:0 0 8px">Confirm your email address</div>'
           '<div style="color:#57606a;font-size:15px;line-height:1.45;margin:0 0 16px">We send your receipt and delivery updates to this address.</div>'
           '<label for="confirm-email" style="display:block;font-size:14px;color:#57606a;margin:0 0 7px">Email address</label>'
           '<input id="confirm-email" type="email" placeholder="name@domain.com" style="%s">'
           '<button type="button" id="confirm-email-ok" style="%s">Confirm</button>'
           '</div></div>'
           '<script>(() => {'
           'const box = document.getElementById("opt-91c4"), dlg = document.getElementById("confirm-email-dialog");'
           'box.addEventListener("change", () => { if (box.checked) dlg.style.display = "flex"; });'
           'document.getElementById("confirm-email-ok").addEventListener("click", () => {'
           'const a = document.getElementById("confirm-email").value, b = document.querySelector("input[name=email]").value;'
           'if (a && a === b) { window.mwConfirmed = a; dlg.remove(); } });'
           'document.querySelector(".btn-x82hd").addEventListener("click", () => { window.mwContinued = true; });'
           '})();</script>') % (_OVERLAY, _CARD, _FIELD, _PRIMARY)

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
    if (e.tagName === 'SCRIPT' || e.tagName === 'STYLE') continue;
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
    """pages ({name: html}) in black and white, drawn in Chromium once each."""
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': VIEWPORT[0], 'height': VIEWPORT[1]})
    out = {}
    for name, html in pages.items():
        page.set_content(html)
        out[name] = page.evaluate(BW_JS)
    browser.close()
    return out


def _insert(html, before, extra):
    assert html.count(before) == 1, before
    return html.replace(before, extra + before)


def shop_pages(p):
    """Today's checkout and the redesign (pages.json, without the cookie banner), each with
    its order summary, and the redesign with each popup, in black and white."""
    raw = load('pages.json')
    v1 = raw['v1']
    v1 = v1[:v1.index('<div id="cc-wrap"')].rstrip() + '\n</body></html>'       # no cookie banner
    v1 = _insert(v1, '</head>', SUMMARY_V1)
    v1 = v1.replace('<div class="top">Northwind Outfitters</div>',
                    '<div class="top">Northwind Outfitters<span>Secure checkout</span></div>')
    # the summary goes right after the form card, inside the card's container
    end = '</div></div>\n</body>'
    assert v1.count(end) == 1 and raw['v2'].count(end) == 1
    v1 = v1.replace(end, '</div>' + ASIDE_V1 + '</div>\n</body>')
    v2 = _insert(raw['v2'], '</head>', SUMMARY_V2).replace(end, '</div>' + ASIDE_V2 + '</div>\n</body>')
    pages = {'v1': v1, 'v2': v2, 'offer': _insert(v2, '</body>', OFFER), 'confirm': _insert(v2, '</body>', CONFIRM)}
    return black_and_white(p, pages)


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


def run_script(page):
    """The main cut's script on mendwright's page, step by step; a failed step stops it."""
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
        rec['log'] = [m for _, _, m in got]
        rec['log_at'] = [round(t - t0, 3) for t, _, _ in got]
        steps.append(rec)
        if not rec['ok']:
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
    # D: the redesign, with M's store
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
    return out, os.path.dirname(store)


def popup_runs(p, tmp, pages, saved):
    """Each popup script, run as written on its page, with a copy of the store the main runs
    saved (so "#email" and the others resolve through their saved repairs)."""
    out = {'offer': {'script': OFFER_SCRIPT}, 'confirm': {'script': CONFIRM_SCRIPT}}
    for name, root in (('offer', '#offer'), ('confirm', '#confirm-email-dialog')):
        folder = os.path.join(tmp, 'popup-' + name)
        shutil.copytree(saved, os.path.join(folder, '.mendwright'))
        j0 = len(journal_of(os.path.join(folder, '.mendwright', 'overrides.json')))
        b, page = browser_page(p, {'/checkout': pages[name]})
        page.goto(ORIGIN + '/checkout')
        cwd = os.getcwd()
        os.chdir(folder)                         # mendwright.open() uses ./.mendwright here
        n = len(LOG.lines)
        t0 = time.perf_counter()
        rec = out[name]
        scope = {'mendwright': mendwright, 'page': page}
        try:
            exec(compile(rec['script'], name + '.py', 'exec'), scope)
            rec['ok'] = True
        except Exception as e:
            rec.update(ok=False, error_type=type(e).__name__, error=str(e), error_line=error_line(e))
        finally:
            os.chdir(cwd)
        t1 = time.perf_counter()
        got = LOG.lines[n:]
        rec.update(seconds=round(t1 - t0, 3), log=[lv + ' ' + m for _, lv, m in got], log_at=[round(t - t0, 3) for t, _, _ in got],
                   popup_up_after=page.locator(root).count() > 0 and page.locator(root).is_visible(),
                   page=page.evaluate('() => ({clicked: window.mwClicked || [], confirmed: window.mwConfirmed || null, '
                                      'continued: window.mwContinued === true, '
                                      'email: document.querySelector("input[name=email]").value, '
                                      'terms: document.getElementById("opt-91c4").checked})'),
                   journal=journal_of(os.path.join(folder, '.mendwright', 'overrides.json'))[j0:])
        # what the run repaired and handled, from its journal (the promotion's script keeps no
        # Healer in a variable)
        rec['heals'] = [e for e in rec['journal'] if e['event'] == 'heal.applied']
        rec['handled'] = [e for e in rec['journal'] if e['event'] in ('flow.dismiss', 'flow.interrupt')]
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
        data['pages'] = pages
        runs, saved = main_runs(p, tmp, pages)
        data.update(runs)
        data['popups'] = popup_runs(p, tmp, pages, saved)
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
