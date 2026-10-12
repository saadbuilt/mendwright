#!/usr/bin/env python3
"""Build docs/assets/demo.svg, an animated terminal recording of `mendwright demo`.

Standard library only, and deterministic. The text comes from running the real
command, so run this again after any change to the demo output:

    python docs/charts/build_demo_svg.py                   # write docs/assets/demo.svg
    python docs/charts/build_demo_svg.py --check           # exit 1 if demo.svg is out of date
    python docs/charts/build_demo_svg.py --social-preview  # also write social-preview.png

Where the text comes from:

- `python -m mendwright demo` runs from the repository root with NO_COLOR=1, and with
  every provider and MENDWRIGHT_* variable removed. Its lines are used as printed. A
  line longer than WRAP_COLS is wrapped at a space outside quotes and brackets, so a
  selector is never split.
- The colours are the ones the CLI uses on a terminal. The script also runs
  mendwright.demo.run(color=True), reads which words it wraps in ANSI codes (bold,
  dim, red, green, yellow) and colours the same words here.
- The recording starts at `$ mendwright demo`. The README shows the pip command above
  the image, so it isn't repeated here.

The animation is CSS inside the SVG, so it plays where the SVG is shown as an <img>
(GitHub, PyPI). The command is typed in under a second, the output lines appear 0.25 s
apart, and the healed selector shows at about 3 s. The last frame then holds for the rest
of the cycle (more than 7 s) and the loop cuts straight back to the start, with no fade
to an empty window. With prefers-reduced-motion, or in a viewer without CSS animation,
the image shows the last frame.

--social-preview also writes docs/assets/social-preview.png (1280x640, for the repo's
Settings page). It has its own pip line. It needs Playwright and Chromium; nothing else
in this script does. The PNG uses the fonts of the machine that renders it (Segoe UI and
Consolas on Windows), so it is not checked by --check.

Exit codes: 0 for success, 1 when --check finds demo.svg out of date, 2 when the demo
fails or its output cannot be laid out (the reason goes to stderr).
"""

import argparse
import base64
import os
import re
import struct
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
ASSETS = HERE.parent / 'assets'
SVG_PATH = ASSETS / 'demo.svg'
PNG_PATH = ASSETS / 'social-preview.png'
LOGO_PATH = ASSETS / 'logo-dark.svg'

DEMO_COMMAND = 'mendwright demo'

# Variables removed before running the demo. The demo needs none of them, and
# MENDWRIGHT_DISABLE would add a note line to its output.
STRIP_PREFIXES = ('ANTHROPIC_', 'OPENAI_', 'GEMINI_', 'OLLAMA_', 'LITELLM_', 'BEDROCK_',
                  'AWS_', 'MENDWRIGHT_', 'NO_COLOR')

# Layout. Columns are sized for the widest common monospace fonts (Menlo, DejaVu Sans
# Mono and Courier New are about 0.61em per character; Consolas is 0.55em).
FONT = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
WIDTH = 900
FONT_SIZE = 13
CHAR_EM = 0.61
WRAP_COLS = 106
PAD_X = 20
TITLE_H = 32
LINE_H = 20
TOP = TITLE_H + 26     # baseline of the first line
BOTTOM = 18

# The window uses the dark surface of the comparison charts. The text colours are close
# to GitHub's dark theme, and each one is checked for 4.5:1 contrast against BG before
# anything is written.
BG = '#1a1a19'
TITLE_BG = '#232321'
FG = '#d6d5ce'
COLORS = {
    'b': '#ffffff',   # bold (SGR 1)
    'd': '#9c9a92',   # dim (SGR 2)
    'r': '#ff7b72',   # red (SGR 31)
    'g': '#56d364',   # green (SGR 32)
    'y': '#e3b341',   # yellow (SGR 33)
    'p': '#79c0ff',   # the $ of a prompt
    'c': '#ffffff',   # a typed command
}
SGR_CLASS = {'1': 'b', '2': 'd', '31': 'r', '32': 'g', '33': 'y'}

# Timing, in seconds. The healed selector shows at about 3 s, and everything is on screen
# by about 4.5 s. The rest of the cycle holds that last frame, then the loop restarts.
CYCLE = 14.0
START = 0.2           # the prompt appears
TYPE_START = 0.35     # the first typed character
TYPE_DELAY = 0.04     # per typed character
RUN_DELAY = 0.3       # after the last typed character, before the first output line
LINE_DELAY = 0.25     # between output lines
MIN_HOLD = 7.0        # the last frame must stay on screen at least this long

SGR = re.compile(r'\x1b\[(\d+)m')


def fail(message):
    sys.stderr.write('build_demo_svg: ' + message + '\n')
    sys.exit(2)


# --- capture -------------------------------------------------------------------------

def clean_env():
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(STRIP_PREFIXES)}
    env['NO_COLOR'] = '1'
    env['PYTHONPATH'] = str(ROOT)
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def run_python(args):
    proc = subprocess.run([sys.executable, *args], cwd=ROOT, env=clean_env(),
                          capture_output=True, text=True, encoding='utf-8', timeout=300)
    if proc.returncode != 0:
        fail('`python ' + ' '.join(args) + '` exited ' + str(proc.returncode) + '.\n'
             + proc.stdout + proc.stderr)
    return proc.stdout.replace('\r\n', '\n')


COLOR_RUN = ('import sys\n'
             'from mendwright.demo import run\n'
             'sys.exit(run(color=True, stream=sys.stdout))\n')


def capture():
    """(plain lines, ANSI lines) from the real command."""
    plain = run_python(['-m', 'mendwright', 'demo']).rstrip('\n').split('\n')
    ansi = run_python(['-c', COLOR_RUN]).rstrip('\n').split('\n')
    return plain, ansi


def ansi_spans(line):
    """[(text, class)] for each coloured run of an ANSI output line, in order."""
    spans, cls, pos = [], None, 0
    for m in SGR.finditer(line):
        text = line[pos:m.start()].strip()
        if cls and text:
            spans.append((text, cls))
        code = m.group(1)
        if code != '0' and code not in SGR_CLASS:
            fail('the demo uses ANSI code ' + code + ', which has no colour here. Add it '
                 'to SGR_CLASS and COLORS.')
        cls = None if code == '0' else SGR_CLASS[code]
        pos = m.end()
    if cls and line[pos:].strip():
        spans.append((line[pos:].strip(), cls))
    return spans


def coloured(plain, ansi):
    """The plain lines as [(text, class or None)] segments, coloured like the ANSI run.

    The words of each line must match. Spacing may differ: on a terminal the 'refused'
    label is followed by one space, in piped output by two. The piped spacing is kept."""
    if len(plain) != len(ansi):
        fail('the plain and coloured demo runs printed %d and %d lines.' % (len(plain),
                                                                            len(ansi)))
    lines = []
    for p_line, a_line in zip(plain, ansi, strict=True):
        if SGR.sub('', a_line).split() != p_line.split():
            fail('the plain and coloured demo runs differ:\n  ' + p_line + '\n  '
                 + SGR.sub('', a_line))
        segs, pos = [], 0
        for text, cls in ansi_spans(a_line):
            # As a whole word, so the status 'ok' is not found inside 'cookies'.
            found = re.compile(r'(?<!\w)' + re.escape(text) + r'(?!\w)').search(p_line, pos)
            at = found.start() if found else -1
            if at < 0:
                fail('cannot find the coloured text ' + repr(text) + ' in: ' + p_line)
            if at > pos:
                segs.append((p_line[pos:at], None))
            segs.append((text, cls))
            pos = at + len(text)
        if pos < len(p_line):
            segs.append((p_line[pos:], None))
        lines.append(segs)
    return lines


# --- wrapping ------------------------------------------------------------------------

def break_points(text):
    """Indexes of spaces where a line may wrap: after the indent, outside quotes, ()
    and []."""
    start = len(text) - len(text.lstrip(' '))
    points, depth, quoted = [], 0, False
    for i, ch in enumerate(text):
        if ch == "'":
            quoted = not quoted
        elif quoted:
            continue
        elif ch in '([':
            depth += 1
        elif ch in ')]':
            depth -= 1
        elif ch == ' ' and depth == 0 and i > start and text[i - 1] != ' ':
            points.append(i)
    return points


def split_segs(segs, at):
    head, tail, pos = [], [], 0
    for text, cls in segs:
        if pos + len(text) <= at:
            head.append((text, cls))
        elif pos >= at:
            tail.append((text, cls))
        else:
            head.append((text[:at - pos], cls))
            tail.append((text[at - pos:], cls))
        pos += len(text)
    return head, tail


def wrap(segs, limit):
    """One line as a list of lines no longer than limit. A continuation is indented to
    where the text after the label starts ('      model    ' is 15 columns)."""
    text = ''.join(t for t, _ in segs)
    if len(text) <= limit:
        return [segs]
    cut = max((p for p in break_points(text) if p <= limit), default=None)
    if cut is None:
        fail('cannot wrap this line at %d columns without splitting a quoted string: %s'
             % (limit, text))
    label = re.match(r' +\S+ +', text)
    indent = len(label.group(0)) if label else 2
    head, tail = split_segs(segs, cut)
    while tail and not tail[0][0].strip():
        tail.pop(0)
    if tail:
        tail[0] = (tail[0][0].lstrip(' '), tail[0][1])
    return [head] + wrap([(' ' * indent, None)] + tail, limit)


# --- the SVG -------------------------------------------------------------------------

def luminance(hex_color):
    def channel(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check_palette():
    for name, color in [('text', FG)] + sorted(COLORS.items()):
        ratio = contrast(color, BG)
        if ratio < 4.5:
            fail('colour %s (%s) has %.2f:1 contrast on %s; it needs 4.5:1.'
                 % (name, color, ratio, BG))


def pct(seconds):
    return '%.3f%%' % (seconds / CYCLE * 100)


class Timeline:
    """One @keyframes rule per start time. CSS animation-delay only applies to the
    first loop, so each element gets its own keyframes over the full cycle instead."""

    def __init__(self):
        self.rules = {}

    def cls(self, start, prop):
        """The class name that shows an element from `start` seconds. prop is 'opacity'
        for a <text> or <g>, and 'fill-opacity' for a <tspan>."""
        key = (round(start, 3), prop)
        if key not in self.rules:
            self.rules[key] = '%s%d' % ('o' if prop == 'opacity' else 'f', len(self.rules))
        return self.rules[key]

    def css(self):
        """Hidden until `start`, then shown to the end of the cycle. The loop restarts from
        the full final frame with a hard cut, so the image never ends on an empty window."""
        out = []
        for (start, prop), name in self.rules.items():
            hidden = '0%%,%s{%s:0}' % (pct(start), prop)
            shown = '%s,100%%{%s:1}' % (pct(start + 0.002), prop)
            out.append('@keyframes %s{%s%s}' % (name, hidden, shown))
            out.append('.%s{animation:%s %gs linear infinite}' % (name, name, CYCLE))
        return '\n'.join(out)


def tspans(segs):
    out = []
    for text, cls in segs:
        if cls:
            out.append('<tspan class="%s">%s</tspan>' % (cls, escape(text)))
        else:
            out.append(escape(text))
    return ''.join(out)


def text_el(row, body, anim=None):
    y = TOP + row * LINE_H
    klass = ' class="%s"' % anim if anim else ''
    return '<text x="%d" y="%d"%s xml:space="preserve">%s</text>' % (PAD_X, y, klass, body)


def typed(command, start, timeline):
    """The command as one tspan per character, each shown at its own moment."""
    parts = []
    for i, ch in enumerate(command):
        anim = timeline.cls(start + i * TYPE_DELAY, 'fill-opacity')
        parts.append('<tspan class="c %s">%s</tspan>' % (anim, escape(ch)))
    return ''.join(parts), start + (len(command) - 1) * TYPE_DELAY


def build_svg(lines):
    check_palette()
    timeline = Timeline()
    els = []
    row = 0
    prompt = '<tspan class="p">$</tspan> '

    # $ mendwright demo
    cmd, end = typed(DEMO_COMMAND, TYPE_START, timeline)
    els.append(text_el(row, '<tspan class="p %s">$</tspan> ' % timeline.cls(START, 'fill-opacity')
                       + cmd))
    row += 1
    t = end + RUN_DELAY

    transcript = ['$ ' + DEMO_COMMAND]
    shown = list(transcript)
    for segs in lines:
        anim = timeline.cls(t, 'opacity')
        transcript.append(''.join(s for s, _ in segs))
        for part in wrap(segs, WRAP_COLS):
            text = ''.join(s for s, _ in part)
            if text.strip():
                els.append(text_el(row, tspans(part), anim))
            shown.append(text)
            row += 1
        t += LINE_DELAY

    # The prompt comes back, with a blinking cursor.
    anim = timeline.cls(t, 'opacity')
    char_w = FONT_SIZE * 0.6
    cursor_y = TOP + row * LINE_H - FONT_SIZE + 2
    els.append('<g class="%s">%s<rect class="cursor" x="%.1f" y="%d" width="%.1f" '
               'height="%d" fill="%s"/></g>' % (
                   anim, text_el(row, prompt), PAD_X + 2 * char_w, cursor_y, char_w,
                   FONT_SIZE + 2, FG))
    row += 1
    end_time = t

    height = TOP + (row - 1) * LINE_H + BOTTOM
    widest = max(len(line) for line in shown)
    if PAD_X * 2 + widest * FONT_SIZE * CHAR_EM > WIDTH:
        fail('a line of %d columns does not fit in %d px.' % (widest, WIDTH))
    if end_time > CYCLE - MIN_HOLD:
        fail('the output takes %.1f s, which leaves less than %g s to read the last frame.'
             % (end_time, MIN_HOLD))

    style = '\n'.join([
        'text{fill:%s;white-space:pre}' % FG,
        ''.join('.%s{fill:%s}' % (k, v) for k, v in COLORS.items()),
        '.b{font-weight:700}',
        timeline.css(),
        '@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}',
        '.cursor{animation:blink 1.1s step-end infinite}',
        '@media (prefers-reduced-motion:reduce){*{animation:none!important}}',
    ])
    desc = 'A terminal. ' + ' / '.join(line.strip() for line in transcript if line.strip())
    r = 10
    head = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
        'role="img" aria-labelledby="title desc">' % (WIDTH, height, WIDTH, height),
        '<title id="title">mendwright demo in a terminal</title>',
        '<desc id="desc">%s</desc>' % escape(desc),
        '<style>\n%s\n</style>' % style,
        '<rect x="0.5" y="0.5" width="%d" height="%d" rx="%d" fill="%s" stroke="#ffffff" '
        'stroke-opacity="0.14"/>' % (WIDTH - 1, height - 1, r, BG),
        '<path d="M1 %d V%d A%d %d 0 0 1 %d 1 H%d A%d %d 0 0 1 %d %d V%d Z" fill="%s"/>' % (
            TITLE_H, r + 1, r - 1, r - 1, r, WIDTH - r, r - 1, r - 1, WIDTH - 1, r + 1,
            TITLE_H, TITLE_BG),
        '<line x1="1" y1="%d.5" x2="%d" y2="%d.5" stroke="#ffffff" stroke-opacity="0.08"/>'
        % (TITLE_H, WIDTH - 1, TITLE_H),
        '<circle cx="20" cy="16" r="6" fill="#ff5f57"/>',
        '<circle cx="40" cy="16" r="6" fill="#febc2e"/>',
        '<circle cx="60" cy="16" r="6" fill="#28c840"/>',
        '<g font-family="%s" font-size="%d">' % (escape(FONT, {'"': '&quot;'}), FONT_SIZE),
    ]
    return '\n'.join(head + els + ['</g>', '</svg>']) + '\n'


# --- the social preview --------------------------------------------------------------

SOCIAL_FIRST = '      -- v2'      # the excerpt runs from this line ...
SOCIAL_LAST = '      refused'    # ... to this one
SOCIAL_COLS = 112


def dedent(lines):
    """Remove the indent all lines share. Each line's indent is its first segment."""
    def indent(part):
        text = ''.join(t for t, _ in part)
        return len(text) - len(text.lstrip(' '))
    n = min(indent(part) for part in lines if ''.join(t for t, _ in part).strip())
    return [[(part[0][0][n:], part[0][1])] + part[1:] for part in lines]


def social_html(lines):
    plain = [''.join(t for t, _ in segs) for segs in lines]
    try:
        first = next(i for i, s in enumerate(plain) if s.startswith(SOCIAL_FIRST))
        last = next(i for i, s in enumerate(plain) if s.startswith(SOCIAL_LAST))
    except StopIteration:
        fail('the demo output no longer has the lines the social preview shows.')
    parts = [part for segs in lines[first:last + 1] for part in wrap(segs, SOCIAL_COLS)]
    rows = [tspans(part).replace('tspan', 'span') for part in dedent(parts)]
    logo = base64.b64encode(LOGO_PATH.read_bytes()).decode('ascii')
    colors = ''.join('.%s{color:%s}' % (k, v) for k, v in COLORS.items())
    return '''<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0}
html,body{width:1280px;height:640px;overflow:hidden}
body{background:%(bg)s;color:%(fg)s;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
  position:relative}
.top{position:absolute;left:72px;top:50px;right:72px}
.logo{height:56px;display:block}
h1{margin-top:22px;font-size:60px;line-height:1.08;font-weight:650;color:#ffffff;
  letter-spacing:-0.8px}
.pip{margin-top:22px;font:500 26px/1 %(mono)s;color:%(fg)s}
.term{position:absolute;left:72px;right:72px;bottom:52px;background:#232321;
  border:1px solid rgba(255,255,255,.12);border-radius:12px;overflow:hidden}
.bar{height:30px;border-bottom:1px solid rgba(255,255,255,.08);padding:10px 14px}
.bar i{display:inline-block;width:11px;height:11px;border-radius:50%%;margin-right:8px}
pre{padding:14px 24px 16px;font:16px/1.5 %(mono)s;color:%(fg)s;white-space:pre}
%(colors)s .b{font-weight:700}
</style></head><body>
<div class="top">
<img class="logo" alt="mendwright" src="data:image/svg+xml;base64,%(logo)s">
<h1>Self-healing selectors for<br>Playwright and Selenium</h1>
<div class="pip"><span class="p">$</span> pip install mendwright</div>
</div>
<div class="term">
<div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i
 style="background:#28c840"></i></div>
<pre>%(rows)s</pre>
</div>
</body></html>
''' % {'bg': BG, 'fg': FG, 'mono': FONT, 'colors': colors, 'logo': logo,
       'rows': '\n'.join(rows)}


def png_size(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        fail('the screenshot is not a PNG.')
    return struct.unpack('>II', data[16:24])


def build_social_preview(lines):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        fail('--social-preview needs Playwright. Install it with: pip install playwright, '
             'then: python -m playwright install chromium')
    html = social_html(lines)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 640},
                                device_scale_factor=1)
        page.set_content(html)
        page.wait_for_load_state('load')
        data = page.screenshot(type='png')
        browser.close()
    if png_size(data) != (1280, 640):
        fail('the social preview is %dx%d, not 1280x640.' % png_size(data))
    if len(data) >= 1_000_000:
        fail('the social preview is %d bytes; GitHub needs it under 1 MB.' % len(data))
    PNG_PATH.write_bytes(data)
    print('wrote %s (%d bytes)' % (PNG_PATH.relative_to(ROOT).as_posix(), len(data)))


# --- main ----------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--check', action='store_true',
                        help='write nothing; exit 1 if demo.svg is out of date')
    parser.add_argument('--social-preview', action='store_true',
                        help='also write social-preview.png (needs Playwright)')
    args = parser.parse_args(argv)

    plain, ansi = capture()
    lines = coloured(plain, ansi)
    svg = build_svg(lines)

    if args.check:
        current = SVG_PATH.read_text(encoding='utf-8').replace('\r\n', '\n') \
            if SVG_PATH.exists() else ''
        if current != svg:
            sys.stderr.write('stale: ' + SVG_PATH.relative_to(ROOT).as_posix()
                             + '. Run: python docs/charts/build_demo_svg.py\n')
            return 1
        return 0

    with open(SVG_PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(svg)
    print('wrote %s (%d bytes)' % (SVG_PATH.relative_to(ROOT).as_posix(),
                                   len(svg.encode('utf-8'))))
    if args.social_preview:
        build_social_preview(lines)
    return 0


if __name__ == '__main__':
    sys.exit(main())
