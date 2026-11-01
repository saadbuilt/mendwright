"""Build compose_<cut>.html from the real runs in the capture JSON and render it frame by frame.

One composition, five cuts: main (the README video) and the clips A..D.

python render.py --cut main --stills 5.0 12.5 ...   # PNGs of single moments, for review
python render.py --cut main --beats                 # PNGs at every beat's key moment
python render.py --cut A --video [out.mp4] [--fps 60] [--segment I N]

Before anything is drawn, window.audit() checks the cut's timeline against the pacing
rules (one new text per 1.2 s, caption durations, holds after results, ...) and the layout
(nothing cropped or overlapping); any finding aborts the render.
"""
import argparse
import base64
import importlib.util
import json
import os
import re
import subprocess
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
CUTS = {'main': 'mendwright-main.mp4', 'A': 'clip-cookies.mp4', 'B': 'clip-popups.mp4',
        'C': 'clip-interrupt.mp4', 'D': 'clip-steps.mp4'}


def demo_pages():
    """The demo shop: pages.json on the video branch, else the capture script's pages."""
    path = os.path.join(HERE, 'pages.json')
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    spec = importlib.util.spec_from_file_location('cap', os.path.join(HERE, 'capture.py'))
    cap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cap)
    return {'v1': cap.V1_BANNER, 'v2': cap.V2}


DRAFT = os.environ.get('MW_DRAFT') == '1'     # local layout checks only: never for a render

# The video renders with Inter and Cascadia Code: the CI runner installs both. Locally, a folder
# of Inter .ttf files (MW_INTER_DIR) stands in for the installed font, so layout checks here see
# the same text widths as the render; without one, Segoe UI falls back.
PAGE_FONT = '<style>body,button,input{font-family:Inter,"Inter Variable","Segoe UI",system-ui,sans-serif}</style>'
INTER_DIR = os.environ.get('MW_INTER_DIR', '')
INTER_FILES = [('Regular', 400), ('Medium', 500), ('SemiBold', 600), ('Bold', 700)]


def local_inter():
    if os.environ.get('MW_FONTS') == 'system' or not os.path.isdir(INTER_DIR):
        return ''
    if not all(os.path.exists(os.path.join(INTER_DIR, 'Inter-%s.ttf' % n)) for n, _ in INTER_FILES):
        return ''
    base = 'file:///' + INTER_DIR.replace(os.sep, '/').replace(' ', '%20') + '/'
    return '<style>' + ''.join("@font-face{font-family:Inter;src:url('%sInter-%s.ttf');font-weight:%d}" % (base, n, w)
                               for n, w in INTER_FILES) + '</style>'


def page_font(page, extra=''):
    """The shop pages get Inter too, as on the video branch (pages.json already has it)."""
    if PAGE_FONT not in page:
        assert '</head>' in page
        page = page.replace('</head>', PAGE_FONT + '</head>', 1)
    return page.replace('</head>', extra + '</head>', 1) if extra else page


def fonted(pages, extra):
    if isinstance(pages, str):
        return page_font(pages, extra)
    return {k: fonted(v, extra) for k, v in pages.items()}


def check(ok, what):
    if not ok:
        if DRAFT:
            print('DRAFT, not verified:', what)
        else:
            raise AssertionError(what)


def load(name):
    return json.load(open(os.path.join(HERE, name), encoding='utf-8'))


def flow_data(fl, upsell=None):
    """The newsletter popup (clip B, montage M2): capture_flow.json, or capture_main.json's 'flow'."""
    f1 = fl['F1']
    check(all(s['ok'] for s in f1['steps']), 'popup act: a step failed')
    check(f1['popup_up_after'] is False and f1['sign_up'] == 'never clicked', 'popup act: wrong outcome')
    check(f1.get('store_fresh') is True, 'popup act: the run did not start from a fresh store')
    if upsell is not None:
        check([f1['open_kwargs'], f1['click'], f1['click_kwargs']] == [upsell['open_kwargs'], upsell['click'], upsell['click_kwargs']],
              'clip B: the newsletter and upsell runs ran different scripts')
    return {'F1': f1, 'pages': fl['pages']}


def smart_data():
    """The upsell and the order popup (clips B and C), from capture_smart.json."""
    sm = load('capture_smart.json')
    check(sm['upsell']['ok'] and sm['upsell']['page']['clicked'] == ['No thanks'], 'upsell: wrong outcome')
    a = sm['order_alone']['page']
    check(a['clicked'] == [] and not a['cancelled'] and a['popup_up'], 'order popup: mendwright clicked something')
    r = sm['order_registered']
    check(r['ok'] and r['page']['clicked'] == ['Continue with order'] and not r['page']['cancelled'], 'order + interrupt: wrong outcome')
    return sm


def steps_data(st):
    """Steps in any order (clip D, montage M3): capture_steps.json, or capture_main.json's 'steps'."""
    check(not st.get('placeholder'), 'steps act: capture_steps.json is a placeholder')
    check(st['switched']['ok'] and st['switched']['ran'][0] == 'payment', 'steps act: switched run')
    check(st['today']['ok'], 'steps act: the script does not run on today\'s site')
    check(not st['extra']['ok'], 'steps act: the unknown step did not stop the run')
    return st


def sel_data():
    """The Selenium beat of clip A, from capture_selenium.json."""
    sl = load('capture_selenium.json')
    plain, mw = sl['plain'][0], sl['mendwright']
    assert not plain['ok'] and all(s['ok'] for s in mw) and not sl['banner_up_after'] and sl['terms_checked']
    log = mw[0]['log'][0]
    return {'plain_type': plain['error_type'], 'plain_line': plain['error_line'],
            'log': log.split(' ', 1)[1] if log.startswith(('INFO ', 'WARNING ')) else log,
            'mendwright': mw, 'mendwright_seconds': sl['mendwright_seconds'],
            'banner_up_after': sl['banner_up_after'], 'terms_checked': sl['terms_checked']}


def main_data(cap):
    """The main story (B1..B6) and the banner runs: capture.json (the clips), capture_main.json (the main cut)."""
    secs = lambda k: [s['seconds'] for s in cap[k]]
    M, Dm, E = cap['M_mendwright_v1'], cap['D_mendwright_v2'], cap['E_mendwright_v2_again']
    check(all(s['ok'] for s in M + Dm + E), 'main: a step failed')
    script = [[s['verb'], s['arg']] for s in Dm]
    check(script == [[s['verb'], s['arg']] for s in M] == [[s['verb'], s['arg']] for s in E], 'main: the runs ran different scripts')
    check(len(cap['M_fingerprints']) == 3 and all(cap['M_fingerprints'].values()), 'M: not all three elements were remembered')
    check(cap['M_heals'] == [], 'M: today\'s run repaired something')
    check([h['source'] for h in cap['D_heals']] == ['fingerprint', 'model', 'fingerprint'], 'D: the repairs came from elsewhere')
    check(all(len(s['log_at']) == 1 for i, s in enumerate(Dm) if i != 1), 'D: a repair without its log line')
    check(cap['E_heals'] == [], 'E: the next run repaired something')
    check(cap['B_banner_up_after'] and not cap['C_banner_up_after'], 'banner runs: wrong outcome')
    check(dict(cap['D_checked'])['opt-91c4'] and not dict(cap['D_checked'])['opt-77a1'], 'D: the wrong checkbox')
    notes = {n['key']: n for n in cap['D_fingerprint_notes']}
    check('two elements score alike' in notes['#accept-terms']['note'] and not notes['#accept-terms']['applied'],
          'D: the fingerprint tier did not defer #accept-terms to the model for two alike elements')
    dlogs = [(s['log'][0] if s['log'] else '') for s in Dm]
    m = re.search(r'LocalModelProvider, ([0-9.]+)s', dlogs[2])
    win = cap['D_winners']
    B = cap['B_plain_banner'][0]
    check(not B['ok'] and 'intercepts pointer events' in B['error'], 'B: the banner did not take the click')
    Cr = cap['C_mendwright_banner']
    return {
        'pages': demo_pages(),
        'script': script,
        'M': {'steps': secs('M_mendwright_v1'), 'seconds': cap['M_seconds'], 'fingerprints': cap['M_fingerprints']},
        'Dsteps': [{'seconds': s['seconds'], 'log_at': s['log_at']} for s in Dm],
        'D_seconds': cap['D_seconds'], 'D_timeout_ms': cap['D_timeout_ms'], 'D_heals': cap['D_heals'],
        'D_logs': dlogs,
        'Esteps': secs('E_mendwright_v2_again'), 'E_seconds': cap['E_seconds'], 'E_heals': cap['E_heals'],
        'xpaths': {'email': win['#email']['xpath'], 'terms': win['#accept-terms']['xpath'], 'cont': win['#continue']['xpath']},
        'model_seconds': m.group(1), 'model_bytes': cap['model']['bytes'],
        'B': {'seconds': B['seconds'], 'error_type': B['error_type'], 'error': B['error'], 'error_line': B['error_line']},
        'D_fingerprint_notes': cap['D_fingerprint_notes'], 'D_proposed': cap['D_proposed'], 'D_checked': cap['D_checked'],
        'B_banner_up_after': cap['B_banner_up_after'], 'C_banner_up_after': cap['C_banner_up_after'],
        'C_ok': all(s['ok'] for s in Cr),
        'Crun': {'steps': secs('C_mendwright_banner'), 'log': Cr[0]['log'], 'log_at': Cr[0]['log_at'], 'seconds': cap['C_seconds']},
    }


def popup_data(cm):
    """The popup beat of the main cut: an offer and a question nobody registered, over the
    redesign (capture_main.json's 'popups', on its black-and-white pages)."""
    p = cm['popups']
    o, q = p['offer'], p['question']
    check(p['open_kwargs'] == {'dismiss': True} and p['click'] == 'input[name=email]' and p['click_kwargs'] == {},
          'popups: not the script the beat shows')
    check(o['ok'] and o['clicked'] == ['No thanks'] and not o['popup_up_after'] and o['store_fresh'],
          'popups: the offer was not declined with No thanks')
    check(any('dismissed a popup' in line and '“No thanks”' in line for line in o['log']), 'popups: no log line for the declined offer')
    check(not q['ok'] and q['clicked'] == [] and q['popup_up_after'] and q['store_fresh'],
          'popups: the question was answered, or did not stay')
    return dict(p, pages={'offer': cm['pages']['offer'], 'question': cm['pages']['question']})


def team_data(t, main):
    """The team beats of the main cut (the pull request, mendwright check, TypeScript), from capture_team.json."""
    # the pytest test is the script the main cut shows, in a test function
    path = 'tests/test_checkout.py'
    rows = {'click': 'page.click(%s)', 'check': 'page.check(%s)', 'type': 'page.keyboard.type(%s)'}
    shown = ['page = mendwright.open(page).page'] + [rows[v] % json.dumps(a) for v, a in main['script']]
    body = [line.strip() for line in t['files'][path].splitlines() if line.startswith('    ')]
    check(body == shown, 'team: the pytest test is not the script the main cut shows: %s' % body)
    heals = {h['key']: h['new'] for h in main['D_heals']}
    # pytest on the redesign: the same three repairs, each confirmed by the passing test
    v2 = t['pytest_v2']
    check(t['pytest_v1']['exit'] == 0 and v2['exit'] == 0, 'team: a pytest run failed')
    check('3 selectors healed in 1 test (3 confirmed by passing tests)' in v2['stdout'], 'team: pytest did not confirm the three repairs')
    applied = {e['key']: e['xpath'] for e in t['journal_v2'] if e['event'] == 'heal.applied'}
    check(applied == heals, 'team: the pytest run repaired differently from the main story: %s' % applied)
    canary = {e['key']: e for e in t['journal_v2'] if e['event'] == 'heal.canary'}
    check(sorted(canary) == sorted(heals) and all(e['success'] and e['by'] == 'test' and e['xpath'] == heals[k] for k, e in canary.items()),
          'team: not every repair was confirmed by the passing test')
    # mendwright fix: every repair written back, each confirmed by a test
    fj = json.loads(t['fix_json']['stdout'])
    wb = fj['written_back']
    check(len(wb) == len(heals) and all(w['confirmed'] and w['confirmed_by']['by'] == 'test' for w in wb) and not fj['left_alone'],
          'team: mendwright fix does not write back every repair as confirmed by a test')
    test_ids = {w['confirmed_by']['test'] for w in wb}
    check(len(test_ids) == 1, 'team: more than one test confirmed the repairs')
    check(t['pr_title'] == 'mendwright fix: write back %d healed selectors' % len(wb), 'team: the pull request title')
    md = t['fix_md']['stdout']
    check('### Selectors written back (%d)' % len(wb) in md, 'team: the pull request does not list the selectors written back')
    out = t['fix_diff']['stdout'].splitlines()
    i = next(k for k, line in enumerate(out) if line.startswith('@@ '))
    j = out.index('', i)
    hunk = out[i:j]
    check(out[i - 2:i] == ['--- a/' + path, '+++ b/' + path], 'team: the diff is not of ' + path)
    # the diff is what --apply wrote: the file after it holds the + lines and none of the - lines
    after = t['test_after_fix'].splitlines()
    check(all(line[1:] in after for line in hunk if line[0] == '+') and not any(line[1:] in after for line in hunk if line[0] == '-'),
          'team: the diff is not what mendwright fix --apply wrote')
    # mendwright check, before the run: three keys broke, two a run repairs from memory
    ck = t['check']
    check(ck['exit'] == 1 and 'Exit 1: 3 keys broke.' in ck['stdout'] and '2 would heal from memory, 1 needs a model.' in ck['stdout'],
          'team: mendwright check did not report the three broken keys')
    check(all(u == 'https://northwind.example/checkout' for u in t['last_url_after_v1'].values()), 'team: the store has another page')
    # TypeScript: passed with the saved repairs (its actions used them; the page has none of the old selectors)
    ts = t['ts']
    check(ts['exit'] == 0 and '1 passed' in ts['stdout'], 'team: the TypeScript run failed')
    check(not t['ts_journal'], 'team: the TypeScript run repaired something')
    acts = [a for a in t['ts_actions'] if a['method'] != 'goto']
    keys = [a for v, a in main['script'] if v != 'type']
    check([a['selector'] for a in acts] == ['xpath=' + heals[k] for k in keys], 'team: the TypeScript run did not use the saved repairs')
    check(not any(t['ts_old_selectors_on_v2'].values()), 'team: the redesign still has an old selector')
    # the TypeScript run's page was at the shop's address, as in every other run
    pages = [u for u in t['ts_urls'] if u.endswith('/checkout')]
    check(len(pages) == 1 and pages[0].split('//', 1)[1] == 'northwind.example/checkout', 'team: the TypeScript run was at %s' % pages)
    check(t['pages']['v2'] == main['pages']['v2'], 'team: the TypeScript run had another page than the main cut shows')
    return {'title': t['pr_title'], 'n': len(wb), 'test': test_ids.pop(), 'diff': hunk, 'diffFile': path,
            'fixCmd': 'mendwright ' + ' '.join(t['fix_md']['cmd'][3:]), 'checkCmd': 'mendwright ' + ' '.join(ck['cmd'][3:]),
            'checkOut': ck['stdout'], 'tsCmd': ' '.join(ts['cmd']), 'tsOut': ts['stdout'], 'spec': t['files']['checkout.spec.ts'],
            'tsUrl': pages[0].split('//', 1)[1], 'jsVersion': t['js_version']}


# The clips show runs of mendwright 0.7.0; the main cut, runs of 0.10.1 (capture_main.py and
# capture_team.py). Within a cut, every run ran the same mendwright: one version, one source.
CAPTURES = {'clips': (('capture.json', 'capture_flow.json', 'capture_smart.json', 'capture_steps.json', 'capture_selenium.json'), '0.7.0'),
            'main': (('capture_main.json', 'capture_team.json'), '0.10.1')}


def provenance(which):
    names, version = CAPTURES[which]
    seen = {name: {k: load(name).get(k) for k in ('version', 'git_rev', 'init_sha256')} for name in names}
    first = next(iter(seen.values()))
    check(all(v == first for v in seen.values()), 'the captures ran different mendwright code: %s' % seen)
    check(first['version'] == version, 'the captures ran mendwright %s, not %s' % (first['version'], version))
    return first


def build(cut):
    if cut == 'main':
        cm = load('capture_main.json')
        data = main_data(cm)
        data['pages'] = {'v1': cm['pages']['v1_banner'], 'v2': cm['pages']['v2']}   # the pages the runs had
        data['provenance'] = provenance('main')
        data['P'] = popup_data(cm)
        data['ST'] = steps_data(cm['steps'])
        data['T'] = team_data(load('capture_team.json'), data)
        fkeys = ('P', 'ST')
    else:
        data = main_data(load('capture.json'))
        data['provenance'] = provenance('clips')
        data['F'] = flow_data(load('capture_flow.json'), load('capture_smart.json')['upsell'])
        data['SM'] = smart_data()
        data['ST'] = steps_data(load('capture_steps.json'))
        data['SEL'] = sel_data()
        fkeys = ('F', 'SM', 'ST')
    inter = local_inter()
    data['pages'] = fonted(data['pages'], inter)
    data['fontHead'] = PAGE_FONT + inter         # for the pages compose.js builds (the pull request, the terminal)
    for k in fkeys:
        data[k]['pages'] = fonted(data[k]['pages'], inter)
    css = open(os.path.join(HERE, 'compose.css'), encoding='utf-8').read()
    js = ''.join('<script>' + open(os.path.join(HERE, name), encoding='utf-8').read() + '</script>'
                 for name in ('compose.js', 'compose_steps.js', 'compose_team.js', 'compose_scenes.js'))
    page = ('<!doctype html><html><head><meta charset="utf-8">' + inter + '<style>' + css + '</style></head><body>'
            '<script>window.CUT = ' + json.dumps(cut) + '; window.DATA = ' + json.dumps(data).replace('</', '<' + chr(92) + '/')
            + ';</script>' + js + '</body></html>')
    out = os.path.join(HERE, 'compose_%s.html' % cut)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(page)
    return out


def open_page(p, cut):
    browser = p.chromium.launch(args=['--force-color-profile=srgb', '--font-render-hinting=none', '--allow-file-access-from-files'])
    print('fonts:', 'Inter from ' + INTER_DIR if local_inter() else 'installed (Inter on CI; Segoe UI fallback locally)')
    page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('file:///' + build(cut).replace('\\', '/'))
    try:
        page.wait_for_function('window.ready === true', timeout=30000)
    except Exception:
        raise SystemExit('the composition did not get ready: %s' % errors)
    page.wait_for_timeout(300)
    problems = page.evaluate('window.audit()')
    if problems:
        print('audit of cut %s:' % cut)
        for x in problems:
            print('  -', x)
        if not DRAFT:
            raise SystemExit('audit failed: %d findings' % len(problems))
    else:
        print('audit of cut %s: clean' % cut)
    return browser, page, errors


def main():
    for stream in (sys.stdout, sys.stderr):      # findings quote the captions: print them as UTF-8
        stream.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--cut', default='main', choices=sorted(CUTS))
    ap.add_argument('--stills', nargs='*', type=float)
    ap.add_argument('--beats', action='store_true', help='stills at every beat key moment, into stills_<cut>/')
    ap.add_argument('--video', nargs='?', const='', help='render the cut (default name: %s)' % ', '.join(CUTS.values()))
    ap.add_argument('--fps', type=int, default=60)
    ap.add_argument('--start', type=float, default=0)
    ap.add_argument('--end', type=float, default=None)
    ap.add_argument('--frames', nargs=2, type=int, help='render frames [a, b) only')
    ap.add_argument('--segment', nargs=2, type=int, metavar=('I', 'N'), help='render part I of N')
    a = ap.parse_args()
    with sync_playwright() as p:
        browser, page, errors = open_page(p, a.cut)
        cdp = page.context.new_cdp_session(page)
        print('cut %s: %.1f s' % (a.cut, page.evaluate('window.DURATION')))
        outdir = os.path.join(HERE, 'stills_' + a.cut)
        if a.stills or a.beats:
            os.makedirs(outdir, exist_ok=True)
            shots = [(t, 't%06.2f' % t) for t in (a.stills or [])]
            if a.beats:
                beats = page.evaluate('window.BEATS')
                for i in range(len(beats)):
                    b = beats[i]
                    shots.append((b['t'], '%02d_t%05.2f_%s' % (i + 1, b['t'], re.sub(r'[^A-Za-z0-9]+', '-', b['name']).strip('-'))))
                json.dump(beats, open(os.path.join(outdir, 'beats.json'), 'w', encoding='utf-8'), indent=1, ensure_ascii=False)
            for t, name in shots:
                page.evaluate('t => render(t)', t)
                page.screenshot(path=os.path.join(outdir, name + '.png'))
            print('stills done', errors)
            assert not errors, errors
        if a.video is not None:
            out = a.video or CUTS[a.cut]
            dur = page.evaluate('window.DURATION') if a.end is None else a.end
            n0, n1 = (a.frames if a.frames else (int(a.start * a.fps), int(round(dur * a.fps))))
            if a.segment:
                total, (k, n) = int(round(dur * a.fps)), a.segment
                step = -(-total // n)
                n0, n1 = k * step, min(total, (k + 1) * step)
            ff = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(a.fps),
                                   '-c:v', 'png', '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-tune', 'animation',
                                   '-crf', '20', '-threads', '4', '-g', '120', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-movflags', '+faststart',
                                   out], stdin=subprocess.PIPE)
            for i in range(n0, n1):
                page.evaluate('t => render(t)', i / a.fps)
                shot = cdp.send('Page.captureScreenshot', {'format': 'png', 'optimizeForSpeed': True})
                ff.stdin.write(base64.b64decode(shot['data']))
                if i % 300 == 0:
                    print('frame', i, '/', n1, flush=True)
            ff.stdin.close()
            assert ff.wait() == 0, 'ffmpeg failed'
            print('video done', out, n1 - n0, 'frames', errors)
            assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
