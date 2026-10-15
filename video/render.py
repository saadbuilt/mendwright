"""Build compose.html from the real runs in capture.json and render it frame by frame.

python render.py --stills 5.0 12.5 ...   # PNGs of single moments, for review
python render.py --video out.mp4 [--fps 60]
"""
import argparse
import base64
import html
import importlib.util
import json
import os
import re
import subprocess

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))


def demo_pages():
    """The demo shop: pages.json on the video branch, else the capture script's pages."""
    path = os.path.join(HERE, 'pages.json')
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    spec = importlib.util.spec_from_file_location('cap', os.path.join(HERE, 'capture.py'))
    cap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cap)
    return {'v1': cap.V1_BANNER, 'v2': cap.V2}


def error_html(err):
    """The real Playwright error, trimmed to the lines that tell the story."""
    lines = err.splitlines()
    head = lines[0]
    log = [l for l in lines[1:] if l.strip()]
    keep = []
    for l in log:
        s = l.strip()
        if s.startswith('- waiting for locator') or s.startswith('- locator resolved'):
            keep.append(l)
        elif s.startswith('- attempting click action') and not any('attempting' in k for k in keep):
            keep.append(l)
    first_intercept = next(l for l in log if 'intercepts pointer events' in l)
    keep.append(first_intercept)
    keep.append(next(l for l in log if 'retrying click action' in l))
    counts = [l for l in log if re.match(r'\s*\d+ × waiting for element', l)]
    last = counts[-1]
    out = ['<b>TimeoutError</b>: ' + html.escape(head), 'Call log:']
    for l in keep:
        out.append(html.escape(l.rstrip()))
    out.append('    …')
    out.append(html.escape(last.rstrip()))
    out.append(html.escape(first_intercept.rstrip()))
    return '\n'.join(out)


def flow_data():
    """What the popup act shows, from the real run in capture_flow.json."""
    fl = json.load(open(os.path.join(HERE, 'capture_flow.json'), encoding='utf-8'))
    f1 = fl['F1']
    check(all(s['ok'] for s in f1['steps']), 'popup act: a step failed')
    check(f1['popup_up_after'] is False and f1['sign_up'] == 'never clicked', 'popup act: wrong outcome')
    return {'F1': f1, 'pages': fl['pages']}


def smart_data():
    """What the judgement act shows, from the real runs in capture_smart.json."""
    sm = json.load(open(os.path.join(HERE, 'capture_smart.json'), encoding='utf-8'))
    check(sm['upsell']['ok'] and sm['upsell']['page']['clicked'] == ['No thanks'], 'upsell: wrong outcome')
    a = sm['order_alone']['page']
    check(a['clicked'] == [] and not a['cancelled'] and a['popup_up'], 'order popup: mendwright clicked something')
    r = sm['order_registered']
    check(r['ok'] and r['page']['clicked'] == ['Continue with order'] and not r['page']['cancelled'], 'order + interrupt: wrong outcome')
    z = sm['zip_registered']
    check(z['ok'] and z['page']['zip'] == '10115', 'zip: wrong outcome')
    return sm


def steps_data():
    """What the steps act shows, from capture_steps.json (once the feature exists)."""
    path = os.path.join(HERE, 'capture_steps.json')
    if not os.path.exists(path):
        return None
    st = json.load(open(path, encoding='utf-8'))
    check(not st.get('placeholder'), 'steps act: capture_steps.json is a placeholder')
    check(st['switched']['ok'] and st['switched']['ran'][0] == 'payment', 'steps act: switched run')
    check(not st['extra']['ok'], 'steps act: the unknown step did not stop the run')
    return st


DRAFT = os.environ.get('MW_DRAFT') == '1'     # local layout checks only: never for a render


def check(ok, what):
    if not ok:
        if DRAFT:
            print('DRAFT, not verified:', what)
        else:
            raise AssertionError(what)


def sel_data():
    """What the Selenium beat shows, from the real runs in capture_selenium.json."""
    sl = json.load(open(os.path.join(HERE, 'capture_selenium.json'), encoding='utf-8'))
    plain, mw = sl['plain'][0], sl['mendwright']
    assert not plain['ok'] and all(s['ok'] for s in mw) and not sl['banner_up_after'] and sl['terms_checked']
    msg = re.sub(r' style="[^"]*$', ' \u2026>', plain['error'])
    msg = msg.replace('Message: ', '', 1)
    msg = re.sub(r'(<input id="email")[^>]*>', r'\1 …>', msg)
    log = mw[0]['log'][0]
    return {'plain_type': plain['error_type'], 'plain_msg': msg,
            'log': log.split(' ', 1)[1] if log.startswith(('INFO ', 'WARNING ')) else log,
            'mendwright': mw}


def build():
    cap = json.load(open(os.path.join(HERE, 'capture.json'), encoding='utf-8'))
    secs = lambda k: [s['seconds'] for s in cap[k]]
    dlogs = [(s['log'][0] if s['log'] else '') for s in cap['D_mendwright_v2']]
    m = re.search(r'LocalModelProvider, ([0-9.]+)s', dlogs[2])
    win = cap['D_winners']
    data = {
        'pages': demo_pages(),
        'A': secs('A_plain_v1'), 'C': secs('C_mendwright_banner'), 'D': secs('D_mendwright_v2'),
        'E': secs('E_mendwright_v2_again'), 'E_total': '%.2f' % cap['E_seconds'],
        'C_log': cap['C_mendwright_banner'][0]['log'][0],
        'D_logs': dlogs,
        'model_seconds': m.group(1),
        'model_pick_label': 'I agree to the purchase terms',
        'xpaths': {'email': win['#email']['xpath'], 'terms': win['#accept-terms']['xpath'],
                   'cont': win['#continue']['xpath']},
        'B_error_lines': error_html(cap['B_plain_banner'][0]['error']),
    }
    data['F'] = flow_data()
    data['SM'] = smart_data()
    data['ST'] = steps_data()
    data['SEL'] = sel_data()
    assert cap['B_banner_up_after'] and not cap['C_banner_up_after']
    assert [h['source'] for h in cap['D_heals']] == ['fingerprint', 'model', 'fingerprint']
    css = open(os.path.join(HERE, 'compose.css'), encoding='utf-8').read()
    js = open(os.path.join(HERE, 'compose.js'), encoding='utf-8').read()
    js_more = ''.join('<script>' + open(os.path.join(HERE, name), encoding='utf-8').read() + '</script>'
                      for name in ('compose_steps.js', 'compose_scenes.js') if os.path.exists(os.path.join(HERE, name)))
    page = ('<!doctype html><html><head><meta charset="utf-8"><style>' + css + '</style></head><body>'
            '<script>window.DATA = ' + json.dumps(data).replace('</', '<' + chr(92) + '/') + ';</script><script>' + js + '</script>'
            + js_more + '</body></html>')
    out = os.path.join(HERE, 'compose.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(page)
    return out


def open_page(p):
    browser = p.chromium.launch(args=['--force-color-profile=srgb', '--font-render-hinting=none'])
    page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
    page.goto('file:///' + build().replace('\\', '/'))
    page.wait_for_function('window.ready === true', timeout=20000)
    page.wait_for_timeout(300)
    return browser, page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stills', nargs='*', type=float)
    ap.add_argument('--video')
    ap.add_argument('--fps', type=int, default=60)
    ap.add_argument('--start', type=float, default=0)
    ap.add_argument('--end', type=float, default=None)
    ap.add_argument('--frames', nargs=2, type=int, help='render frames [a, b) only')
    ap.add_argument('--segment', nargs=2, type=int, metavar=('I', 'N'), help='render part I of N')
    a = ap.parse_args()
    with sync_playwright() as p:
        browser, page = open_page(p)
        cdp = page.context.new_cdp_session(page)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        if a.stills:
            os.makedirs(os.path.join(HERE, 'stills'), exist_ok=True)
            for t in a.stills:
                page.evaluate('t => render(t)', t)
                page.screenshot(path=os.path.join(HERE, 'stills', 't%05.2f.png' % t))
            print('stills done', errors)
        if a.video:
            dur = page.evaluate('window.DURATION') if a.end is None else a.end
            n0, n1 = (a.frames if a.frames else (int(a.start * a.fps), int(round(dur * a.fps))))
            if a.segment:
                total, (k, n) = int(round(dur * a.fps)), a.segment
                step = -(-total // n)
                n0, n1 = k * step, min(total, (k + 1) * step)
            ff = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(a.fps),
                                   '-c:v', 'png', '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-tune', 'animation',
                                   '-crf', '20', '-threads', '4', '-g', '120', '-pix_fmt', 'yuv420p', '-profile:v', 'high', '-movflags', '+faststart',
                                   a.video], stdin=subprocess.PIPE)
            for i in range(n0, n1):
                page.evaluate('t => render(t)', i / a.fps)
                shot = cdp.send('Page.captureScreenshot', {'format': 'png', 'optimizeForSpeed': True})
                ff.stdin.write(base64.b64decode(shot['data']))
                if i % 300 == 0:
                    print('frame', i, '/', n1, flush=True)
            ff.stdin.close()
            assert ff.wait() == 0, 'ffmpeg failed'
            print('video done', a.video, n1 - n0, 'frames', errors)
            assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
