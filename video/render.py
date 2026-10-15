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


def flow_data():
    """The newsletter popup (clip B, montage M2), from the real run in capture_flow.json."""
    fl = load('capture_flow.json')
    f1 = fl['F1']
    check(all(s['ok'] for s in f1['steps']), 'popup act: a step failed')
    check(f1['popup_up_after'] is False and f1['sign_up'] == 'never clicked', 'popup act: wrong outcome')
    check(f1.get('store_fresh') is True, 'popup act: the run did not start from a fresh store')
    sm = load('capture_smart.json')['upsell']
    check([f1['open_kwargs'], f1['click'], f1['click_kwargs']] == [sm['open_kwargs'], sm['click'], sm['click_kwargs']],
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


def steps_data():
    """Steps in any order (clip D, montage M3), from capture_steps.json."""
    st = load('capture_steps.json')
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


def main_data():
    """The main story (B1..B6) and the banner runs, from capture.json."""
    cap = load('capture.json')
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


CAPTURES = ('capture.json', 'capture_flow.json', 'capture_smart.json', 'capture_steps.json', 'capture_selenium.json')


def provenance():
    """Every run on screen ran the same mendwright: one version, one source file."""
    seen = {name: {k: load(name).get(k) for k in ('version', 'git_rev', 'init_sha256')} for name in CAPTURES}
    first = next(iter(seen.values()))
    check(all(v == first for v in seen.values()), 'the captures ran different mendwright code: %s' % seen)
    check(first['version'] == '0.7.0', 'the captures ran mendwright %s, not 0.7.0' % first['version'])
    return first


def build(cut):
    data = main_data()
    data['provenance'] = provenance()
    data['F'] = flow_data()
    data['SM'] = smart_data()
    data['ST'] = steps_data()
    data['SEL'] = sel_data()
    inter = local_inter()
    data['pages'] = fonted(data['pages'], inter)
    for k in ('F', 'SM', 'ST'):
        data[k]['pages'] = fonted(data[k]['pages'], inter)
    css = open(os.path.join(HERE, 'compose.css'), encoding='utf-8').read()
    js = ''.join('<script>' + open(os.path.join(HERE, name), encoding='utf-8').read() + '</script>'
                 for name in ('compose.js', 'compose_steps.js', 'compose_scenes.js'))
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
