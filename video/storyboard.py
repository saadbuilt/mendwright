"""Storyboards for review: stills at every beat's key moment, as a contact sheet.

python storyboard.py main A B C D

For each cut: render.py --cut X --beats writes stills_X/NN_tTT.TT_name.png (1920x1080) and
beats.json; this script lays them out 3 across at 640x360, each labelled with its time and
beat name, into storyboard_X.png. Below them come 1:1 crops of the code panel at the moments
where its small cues matter (token underlines, code tags), so they can be checked at full
size. Only stills are rendered here: never a video.
"""
import json
import os
import re
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, LABEL, COLS, GAP = 640, 360, 44, 3, 12
# The cut's PACE, from compose.js: the crop times below are story times, shown at t / PACE.
PACE = float(re.search(r'const PACE = ([0-9.]+);', open(os.path.join(HERE, 'compose.js'), encoding='utf-8').read()).group(1))
# (story time, region (x, y, w, h) at 1920x1080, label): 1:1 crops added under the beats
CODE = (40, 56, 640, 400)
CROPS = {
    'main': [(15.6, CODE, 'code: three selectors broken (red wavy)'), (30.5, CODE, 'code: #accept-terms from the model (purple)'),
             (40.8, CODE, 'code: blue / purple / blue repairs'), (46.0, CODE, 'code: all saved (green)')],
    'A': [(22.4, CODE, 'code: Selenium + mendwright')],
    'B': [(3.0, CODE, 'code: one script for both popups')],
    'C': [(13.6, CODE, 'code: interrupt line, heal.page tag')],
    'D': [(3.2, CODE, 'code: open(), three steps, run_steps()')],
}


def font(size, bold=False):
    for name in (('segoeuib.ttf' if bold else 'segoeui.ttf'), 'DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf', 'arial.ttf'):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def sheet(cut):
    d = os.path.join(HERE, 'stills_' + cut)
    beats = json.load(open(os.path.join(d, 'beats.json'), encoding='utf-8'))
    files = sorted(f for f in os.listdir(d) if f[:2].isdigit() and f.endswith('.png'))
    assert len(files) == len(beats), (len(files), len(beats))
    rows = -(-len(files) // COLS)
    head = 64
    crops = CROPS.get(cut, [])
    CH = max([r[3] for _, r, _ in crops] or [0])
    crop_rows = -(-len(crops) // COLS)
    out = Image.new('RGB', (COLS * W + (COLS + 1) * GAP, head + rows * (H + LABEL + GAP) + GAP
                            + (crop_rows * (CH + LABEL + GAP) + 56 if crops else 0)), (14, 16, 22))
    dr = ImageDraw.Draw(out)
    dur = json.load(open(os.path.join(d, 'duration.json')))['duration'] if os.path.exists(os.path.join(d, 'duration.json')) else None
    dr.text((GAP, 16), 'cut %s%s · %d beats' % (cut, ' · %.1f s' % dur if dur else '', len(files)), fill=(238, 242, 247), font=font(28, True))
    f1, f2 = font(22, True), font(22)
    for i, (f, b) in enumerate(zip(files, beats)):
        x = GAP + (i % COLS) * (W + GAP)
        y = head + (i // COLS) * (H + LABEL + GAP)
        im = Image.open(os.path.join(d, f)).convert('RGB').resize((W, H), Image.LANCZOS)
        out.paste(im, (x, y))
        dr.rectangle([x, y + H, x + W, y + H + LABEL], fill=(28, 32, 44))
        t = '%5.1f s' % b['t']
        dr.text((x + 12, y + H + 9), t, fill=(242, 193, 78), font=f1)
        dr.text((x + 100, y + H + 9), b['name'], fill=(238, 242, 247), font=f2)
    y0 = head + rows * (H + LABEL + GAP) + GAP
    if crops:
        dr.text((GAP, y0 + 10), 'code panel, 1:1', fill=(238, 242, 247), font=font(26, True))
        y0 += 56
    for i, (st, (cx, cy, cw, ch), label) in enumerate(crops):
        t = float('%.2f' % (st / PACE))      # the still's time in the video, as render.py named it
        x = GAP + (i % COLS) * (W + GAP)
        y = y0 + (i // COLS) * (CH + LABEL + GAP)
        im = Image.open(os.path.join(d, 't%06.2f.png' % t)).convert('RGB').crop((cx, cy, cx + cw, cy + ch))
        out.paste(im, (x, y))
        dr.rectangle([x, y + ch, x + W, y + ch + LABEL], fill=(28, 32, 44))
        dr.text((x + 12, y + ch + 9), '%5.1f s' % t, fill=(242, 193, 78), font=f1)
        dr.text((x + 100, y + ch + 9), label, fill=(238, 242, 247), font=f2)
    path = os.path.join(HERE, 'storyboard_%s.png' % cut)
    out.save(path, optimize=True)
    print(path, out.size)
    return path


def main():
    cuts = sys.argv[1:] or ['main', 'A', 'B', 'C', 'D']
    for cut in cuts:
        d = os.path.join(HERE, 'stills_' + cut)
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith('.png'):
                    os.remove(os.path.join(d, f))
        extra = ['--stills'] + ['%.2f' % (t / PACE) for t, _, _ in CROPS.get(cut, [])] if CROPS.get(cut) else []
        r = subprocess.run([sys.executable, os.path.join(HERE, 'render.py'), '--cut', cut, '--beats'] + extra,
                           capture_output=True, text=True, encoding='utf-8')
        print(r.stdout.strip())
        if r.returncode:
            print(r.stderr[-3000:])
            raise SystemExit('render.py failed for cut ' + cut)
        dur = [l for l in r.stdout.splitlines() if l.startswith('cut %s:' % cut)]
        if dur:
            json.dump({'duration': float(dur[0].split(':')[1].split()[0])}, open(os.path.join(d, 'duration.json'), 'w'))
        sheet(cut)


if __name__ == '__main__':
    main()
