"""Build compose.html from the real runs in capture.json and render it frame by frame.

python video/render.py --stills 5.0 12.5 ...           # PNGs of single moments, for review
python video/render.py --video out.mp4                   # the whole video
python video/render.py --video seg.mp4 --segment 3 10    # part 3 of 10 (for parallel jobs)

Every selector, log line, error and timing the video shows comes from capture.json, which
holds real runs of mendwright against the demo pages in pages.json.
"""
import argparse
import base64
import html
import json
import math
import os
import re
import subprocess

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))


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
    out = ['<b>TimeoutError</b>: ' + html.escape(head), 'Call log:']
    out += [html.escape(l.rstrip()) for l in keep]
    out.append('    …')
    out.append(html.escape(counts[-1].rstrip()))
    out.append(html.escape(first_intercept.rstrip()))
    return '\n'.join(out)


def flow_data():
    """What the flow act shows, from the real runs in capture_flow.json."""
    fl = json.load(open(os.path.join(HERE, 'capture_flow.json'), encoding='utf-8'))
    msg = lambda line: line.split(' ', 1)[1] if line.split(' ', 1)[0] in ('INFO', 'WARNING', 'ERROR') else line
    f1 = {s['step']: s for s in fl['F1']['steps']}
    f3 = {s['step']: s for s in fl['F3']['steps']}
    assert all(s['ok'] for s in fl['F1']['steps']), fl['F1']
    assert fl['F1']['popup_up_after'] is False and fl['F1']['sign_up'] == 'never clicked'
    promo = next(msg(l) for l in f1['promo']['log'] if 'promo' in l)
    dismiss = next(msg(l) for l in f1['#email']['log'] if 'popup' in l.lower() or 'dismiss' in l.lower())
    guard = f3['#continue']
    assert not guard['ok'] and f3['#email']['ok'], fl['F3']
    gm = guard['error'].split('-> ', 1)[-1].strip()   # the reason, after the summary line
    short = gm[:gm.index('so mendwright did not heal it.') + len('so mendwright did not heal it.')] if 'so mendwright did not heal it.' in gm else gm
    return {
        'goto_s': f1['goto']['seconds'], 'promo_log': promo, 'dismiss_log': dismiss,
        'email_s': f1['#email']['seconds'], 'cont_s': f1['#continue']['seconds'],
        'popup_label': '“Get 10% off your first order”',
        'F3': {'goto_s': f3['goto']['seconds'], 'email_s': f3['#email']['seconds'],
               'guard_type': guard['error_type'], 'guard_msg': gm, 'guard_short': short},
        'pages': {'v2_nl': fl['pages']['v2_nl'], 'shipping': fl['pages']['shipping']},
        'placeholder': fl.get('placeholder', False),
    }


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
    pages = json.load(open(os.path.join(HERE, 'pages.json'), encoding='utf-8'))
    secs = lambda k: [s['seconds'] for s in cap[k]]
    dlogs = [(s['log'][0] if s['log'] else '') for s in cap['D_mendwright_v2']]
    m = re.search(r'LocalModelProvider, ([0-9.]+)s', dlogs[2])
    win = cap['D_winners']
    data = {
        'pages': pages,
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
    data['SEL'] = sel_data()
    assert not data['F']['placeholder'], 'capture_flow.json is a placeholder: run capture_flow.py'
    assert cap['B_banner_up_after'] and not cap['C_banner_up_after']
    assert [h['source'] for h in cap['D_heals']] == ['fingerprint', 'model', 'fingerprint']
    css = open(os.path.join(HERE, 'compose.css'), encoding='utf-8').read()
    js = open(os.path.join(HERE, 'compose.js'), encoding='utf-8').read()
    js_flow = open(os.path.join(HERE, 'compose_flow.js'), encoding='utf-8').read()
    js_sel = open(os.path.join(HERE, 'compose_sel.js'), encoding='utf-8').read()
    page = ('<!doctype html><html><head><meta charset="utf-8"><style>' + css + '</style></head><body>'
            '<script>window.DATA = ' + json.dumps(data) + ';</script><script>' + js + '</script>'
            '<script>' + js_flow + '</script><script>' + js_sel + '</script></body></html>')
    out = os.path.join(HERE, 'compose.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(page)
    return out


def open_page(p):
    browser = p.chromium.launch(args=['--force-color-profile=srgb', '--font-render-hinting=none'])
    page = browser.new_page(viewport={'width': 1920, 'height': 1080}, device_scale_factor=1)
    page.goto('file://' + ('/' if os.name == 'nt' else '') + build().replace('\\', '/'))
    page.wait_for_function('window.ready === true', timeout=30000)
    page.evaluate('document.fonts.ready')
    page.wait_for_timeout(300)
    return browser, page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stills', nargs='*', type=float)
    ap.add_argument('--video')
    ap.add_argument('--fps', type=int, default=60)
    ap.add_argument('--segment', nargs=2, type=int, metavar=('I', 'N'), help='render part I of N')
    a = ap.parse_args()
    with sync_playwright() as p:
        browser, page = open_page(p)
        cdp = page.context.new_cdp_session(page)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        if a.stills:
            os.makedirs('stills', exist_ok=True)
            for t in a.stills:
                page.evaluate('t => render(t)', t)
                page.screenshot(path=os.path.join('stills', 't%05.2f.png' % t))
            print('stills done', errors)
        if a.video:
            total = int(round(page.evaluate('window.DURATION') * a.fps))
            n0, n1 = 0, total
            if a.segment:
                i, n = a.segment
                step = math.ceil(total / n)
                n0, n1 = i * step, min(total, (i + 1) * step)
            ff = subprocess.Popen(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', str(a.fps),
                                   '-c:v', 'png', '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-tune', 'animation',
                                   '-crf', '20', '-g', '120', '-pix_fmt', 'yuv420p', '-profile:v', 'high',
                                   '-movflags', '+faststart', a.video], stdin=subprocess.PIPE)
            for i in range(n0, n1):
                page.evaluate('t => render(t)', i / a.fps)
                shot = cdp.send('Page.captureScreenshot', {'format': 'png', 'optimizeForSpeed': True})
                ff.stdin.write(base64.b64decode(shot['data']))
                if (i - n0) % 300 == 0:
                    print('frame', i, 'of', n0, '..', n1, flush=True)
            ff.stdin.close()
            assert ff.wait() == 0, 'ffmpeg failed'
            print('video done', a.video, n1 - n0, 'frames', errors)
            assert not errors, errors
        browser.close()


if __name__ == '__main__':
    main()
