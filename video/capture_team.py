"""Record the team beats of the main cut with the mendwright installed here, into capture_team.json.

python capture_team.py --source PATH --js JSDIR [--out capture_team.json]

PATH is the mendwright checkout the installed package was built from (as for
capture_main.py), JSDIR a copy of its js/ folder with `npm ci && npm run build` done.
The local model must be in Ollama, and Playwright's Chromium installed for both packages.

In a temporary project, shop/, with the main cut's script as a pytest test
(tests/test_checkout.py) and a tests/conftest.py whose page fixture serves the shop at
https://northwind.example:

  pytest_v1   pytest on today's page, a fresh store: mendwright remembers the three elements
  pytest_v2   pytest on the redesign: three repairs, each confirmed by the passing test
  fix         mendwright fix (the diff), --format json, and --apply --format md on a copy,
              which is what mendwright's GitHub Action puts in the pull request, with the
              Action's own title for it
  check       mendwright check --origin, in a copy of the project whose store is as it was
              after pytest_v1, with the shop's pages loaded from the redesign served on
              127.0.0.1: what broke, and what a run would repair from memory
  ts          Playwright Test with the TypeScript package (import { test } from
              'mendwright/test') on the redesign, with the store pytest_v2 saved. Its
              Chromium resolves northwind.example to that server, and doesn't first try the
              address over HTTPS, which that server doesn't serve (launch options in the
              config, not in the spec), so the page is at the shop's address; its trace names
              the selector each action used and the address of the page

Local paths in the outputs (the store's path, pytest's rootdir) are written as <project>
and <js>; nothing else is changed.
"""
import argparse
import http.server
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture_main import MODEL, VIEWPORT, provenance, shop_pages, warm_model, write  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
RUN_ID = 'ts-1'

# The main cut's script as a team's pytest test: the lines the editor shows, in a test function.
TEST = '''import mendwright


def test_checkout(page):
    page = mendwright.open(page).page
    page.click("#email")
    page.keyboard.type("ada@example.com")
    page.check("#accept-terms")
    page.click("#continue")
'''
CONFTEST = '''import json
import os

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture
def page():
    """The shop at https://northwind.example, served by page.route(): SHOP_PAGES[SHOP_VERSION]."""
    with open(os.environ["SHOP_PAGES"], encoding="utf-8") as f:
        html = json.load(f)[os.environ["SHOP_VERSION"]]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(viewport={"width": %d, "height": %d})
        pg.route("https://northwind.example/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        pg.goto("https://northwind.example/checkout")
        yield pg
        browser.close()
''' % tuple(VIEWPORT)
# The same steps with the TypeScript package for Playwright Test. Its page fixture heals.
SPEC = """import { test } from 'mendwright/test';

test('checkout', async ({ page }) => {
  await page.goto('/checkout');
  await page.fill('#email', 'ada@example.com');
  await page.check('#accept-terms');
  await page.click('#continue');
});
"""
PW_CONFIG = """import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  reporter: 'list',
  use: {
    baseURL: 'http://northwind.example', viewport: { width: %d, height: %d }, trace: 'on',
    launchOptions: { args: ['--host-resolver-rules=MAP northwind.example 127.0.0.1:%d', '--disable-features=HttpsUpgrades'] },
  },
});
""" % (VIEWPORT[0], VIEWPORT[1], PORT)

SITE = {'html': ''}


class Shop(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split('?')[0] == '/checkout':
            body = SITE['html'].encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
        else:
            body = b'not found'
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def scrub(text, places):
    for path, name in sorted(places, key=lambda x: -len(x[0])):     # the longest path first
        for form in {path, path.replace('\\', '/'), path.replace('\\', '\\\\'), os.path.normcase(path)}:
            text = text.replace(form, name)
    return text


def run(cmd, cwd, env, places):
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    return {'cmd': [os.path.basename(c) if i == 0 else scrub(c, places) for i, c in enumerate(cmd)], 'exit': r.returncode,
            'seconds': round(time.perf_counter() - t0, 3),
            'stdout': scrub(r.stdout, places), 'stderr': scrub(r.stderr, places)}


def journal(store_dir):
    p = os.path.join(store_dir, 'journal.jsonl')
    with open(p, encoding='utf-8') as f:
        return [json.loads(x) for x in f if x.strip()]


def store(store_dir):
    with open(os.path.join(store_dir, 'overrides.json'), encoding='utf-8') as f:
        return json.load(f)['selectors']


def action_title(source, n):
    spec = importlib.util.spec_from_file_location('mendwright_action', os.path.join(source, 'action', 'mendwright_action.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.title_for(n)


def trace_urls(results):
    """Every page address the Playwright Test run's trace names."""
    seen = set()

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if isinstance(v, str) and k.lower().endswith('url') and v.startswith('http'):
                    seen.add(v)
                else:
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    for root, _, files in os.walk(results):
        for f in files:
            if f == 'trace.zip':
                with zipfile.ZipFile(os.path.join(root, f)) as z:
                    for name in z.namelist():
                        if name.endswith('.trace'):
                            for line in z.read(name).decode('utf-8').splitlines():
                                walk(json.loads(line))
    return sorted(seen)


def trace_actions(results):
    """The page actions of the Playwright Test run, from its trace: method and selector."""
    out = []
    for root, _, files in os.walk(results):
        for f in files:
            if f != 'trace.zip':
                continue
            with zipfile.ZipFile(os.path.join(root, f)) as z:
                for name in z.namelist():
                    if not name.endswith('.trace'):
                        continue
                    for line in z.read(name).decode('utf-8').splitlines():
                        e = json.loads(line)
                        if e.get('type') == 'before' and e.get('method') in ('click', 'fill', 'check', 'goto'):
                            p = e.get('params', {})
                            out.append({'method': e['method'], 'selector': p.get('selector'), 'url': p.get('url')})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--js', required=True, help='a built copy of the checkout\'s js/ folder')
    ap.add_argument('--out', default=os.path.join(HERE, 'capture_team.json'))
    a = ap.parse_args()
    js = os.path.abspath(a.js)
    with open(os.path.join(js, 'package.json'), encoding='utf-8') as f:
        js_version = json.load(f)['version']
    data = dict(provenance(a.source), js_version=js_version, model_name=MODEL, port=PORT,
                files={'tests/test_checkout.py': TEST, 'tests/conftest.py': CONFTEST, 'checkout.spec.ts': SPEC,
                       'playwright.config.ts': PW_CONFIG})
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        pages = shop_pages(p)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('MENDWRIGHT_', 'ANTHROPIC_', 'OPENAI_', 'GEMINI_'))}
    env = {k: v for k, v in env.items() if k not in ('FORCE_COLOR', 'NO_COLOR', 'PY_COLORS')}
    env.update(MENDWRIGHT_LOCAL_MODEL=MODEL, PYTHONIOENCODING='utf-8', PYTHONUTF8='1', NO_COLOR='1')
    warm_model()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), Shop)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tmp = tempfile.mkdtemp()
    try:
        shop = os.path.join(tmp, 'shop')
        os.makedirs(shop)
        places = [(shop, '<project>'), (js, '<js>'), (tmp, '<tmp>')]
        os.makedirs(os.path.join(shop, 'tests'))
        for name in ('tests/test_checkout.py', 'tests/conftest.py'):
            with open(os.path.join(shop, name), 'w', encoding='utf-8', newline='\n') as f:
                f.write(data['files'][name])
        pj = os.path.join(tmp, 'pages.json')
        with open(pj, 'w', encoding='utf-8') as f:
            json.dump(pages, f)
        data['pages'] = {'v2': pages['v2']}
        sd = os.path.join(shop, '.mendwright')
        pytest = [sys.executable, '-m', 'pytest', 'tests/test_checkout.py', '-p', 'no:cacheprovider']
        # pytest on today's page, then on the redesign
        data['pytest_v1'] = run(pytest, shop, dict(env, SHOP_PAGES=pj, SHOP_VERSION='v1'), places)
        nightly = os.path.join(tmp, 'nightly')
        shutil.copytree(shop, nightly)
        data['last_url_after_v1'] = {k: v.get('lastUrl') for k, v in store(os.path.join(nightly, '.mendwright')).items()}
        data['pytest_v2'] = run(pytest, shop, dict(env, SHOP_PAGES=pj, SHOP_VERSION='v2'), places)
        data['journal_v2'] = journal(sd)
        # mendwright fix: the diff, the JSON, and what the Action's pull request says
        mw = [sys.executable, '-m', 'mendwright', 'fix']
        data['fix_diff'] = run(mw, shop, env, places)
        data['fix_json'] = run(mw + ['--format', 'json'], shop, env, places)
        applied = os.path.join(tmp, 'shop-applied')
        shutil.copytree(shop, applied)
        data['fix_md'] = run(mw + ['--apply', '--format', 'md'], applied, env, places + [(applied, '<project>')])
        with open(os.path.join(applied, 'tests', 'test_checkout.py'), encoding='utf-8') as f:
            data['test_after_fix'] = f.read()
        written = len(json.loads(data['fix_json']['stdout'])['written_back'])
        data['pr_title'] = action_title(a.source, written)
        # mendwright check: the store as it was after today's run, against the redesign
        SITE['html'] = pages['v2']
        data['check'] = run([sys.executable, '-m', 'mendwright', 'check', '--origin',
                             'https://northwind.example=http://127.0.0.1:%d' % PORT],
                            nightly, env, places + [(nightly, '<project>')])
        # the TypeScript package, with the store pytest_v2 saved
        vt = os.path.join(js, 'video-ts')
        shutil.rmtree(vt, ignore_errors=True)
        os.makedirs(vt)
        for name in ('checkout.spec.ts', 'playwright.config.ts'):
            with open(os.path.join(vt, name), 'w', encoding='utf-8', newline='\n') as f:
                f.write(data['files'][name])
        n0 = len(journal(sd))
        npx = shutil.which('npx') or 'npx'
        data['ts'] = run([npx, 'playwright', 'test', '-c', 'playwright.config.ts', '--output', 'results'], vt,
                         dict({k: v for k, v in env.items() if k != 'NO_COLOR'}, FORCE_COLOR='0',
                              MENDWRIGHT_STORE=os.path.join(sd, 'overrides.json'), MENDWRIGHT_RUN_ID=RUN_ID), places)
        data['ts']['cmd'] = ['npx', 'playwright', 'test']
        data['ts_run_id'] = RUN_ID
        data['ts_journal'] = journal(sd)[n0:]
        data['ts_actions'] = trace_actions(os.path.join(vt, 'results'))
        data['ts_urls'] = trace_urls(os.path.join(vt, 'results'))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)
    # the redesign has none of the script's selectors: what the TypeScript run found, it found
    # through the saved repairs
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.set_content(pages['v2'])
        data['ts_old_selectors_on_v2'] = {s: pg.locator(s).count() for s in ('#email', '#accept-terms', '#continue')}
        b.close()
    write(a.out, data)


if __name__ == '__main__':
    main()
