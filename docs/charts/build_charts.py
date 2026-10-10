#!/usr/bin/env python3
"""Build the comparison charts in docs/assets/ from docs/charts/data.json.

Standard library only, and deterministic: the same data.json always gives
byte-identical SVG files. Run it from anywhere:

    python docs/charts/build_charts.py           # write the SVG files
    python docs/charts/build_charts.py --check   # exit 1 if any SVG is out of date

Every chart is written twice, as <name>-light.svg and <name>-dark.svg, so the
README can pick one with <picture> and prefers-color-scheme. The files are shown
as static images on GitHub and PyPI, so every value is printed on its mark.

Colors come from the dataviz reference palette. Bars and "yes" marks use
categorical slot 1, the split bar adds slot 2, and both pairs were checked with
the palette validator in light and dark mode.

The script also checks the data before drawing: parts must add up to their
totals, and every matrix cell needs a known state and a source. Cells marked
"pending-verification" are listed on stderr so they are not forgotten.
"""

import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
DATA = HERE / 'data.json'
ASSETS = HERE.parent / 'assets'

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
WIDTH = 800
PAD = 24

THEMES = {
    'light': {
        'surface': '#fcfcfb', 'ink': '#0b0b0b', 'ink2': '#52514e', 'muted': '#898781',
        'grid': '#e1e0d9', 'axis': '#c3c2b7', 'border': '#0b0b0b', 'border_op': '0.1',
        'accent': '#2a78d6', 'series2': '#eb6834', 'wash_op': '0.07',
        'good': '#0ca30c', 'critical': '#d03b3b', 'on_mark': '#ffffff',
    },
    'dark': {
        'surface': '#1a1a19', 'ink': '#ffffff', 'ink2': '#c3c2b7', 'muted': '#898781',
        'grid': '#2c2c2a', 'axis': '#383835', 'border': '#ffffff', 'border_op': '0.1',
        'accent': '#3987e5', 'series2': '#d95926', 'wash_op': '0.13',
        'good': '#0ca30c', 'critical': '#d03b3b', 'on_mark': '#ffffff',
    },
}


# ---------------------------------------------------------------- text metrics
# Helvetica advance widths (1/1000 em). Segoe UI is narrower and SF is close to
# Helvetica, so these plus SLACK keep text inside its box on all three systems.

def _widths(spec):
    table = {}
    for chars, width in spec:
        for ch in chars:
            table[ch] = width
    return table


_REGULAR = _widths([
    (" !,./:;I[\\]ft", 278), ("'`ijl", 222), ("()-r", 333), ('"', 355), ('*', 389),
    ('^', 469), ('Jcksvxyz', 500), ('#$0123456789?_abdeghnopquL', 556),
    ('+<=>~', 584), ('FTZ', 611), ('&ABEKPSVXY', 667), ('CDHNRUw', 722),
    ('GOQ', 778), ('Mm', 833), ('%', 889), ('W', 944), ('@', 1015), ('|', 260),
    ('{}', 334),
])
_BOLD = _widths([
    (" ,./\\ijl'", 278), ('!:;()-[]ft`', 333), ('r*{}', 389), ('"', 474), ('z', 500),
    ('#$0123456789_acekosvxyJ', 556), ('+<=>^~', 584), ('?bdghnpquLFTZ', 611),
    ('EPSVXY', 667), ('&ABCDHKNRU', 722), ('GOQw', 778), ('M', 833), ('m%', 889),
    ('W', 944), ('@', 975), ('|', 280),
])
SLACK = 1.08


def text_width(s, size, bold=False):
    table = _BOLD if bold else _REGULAR
    return sum(table.get(ch, 620 if bold else 600) for ch in s) * size / 1000 * SLACK


def wrap(s, size, max_w, bold=False):
    """Greedy word wrap. A single word that is too wide is split after a hyphen."""
    words = []
    for word in s.split():
        while text_width(word, size, bold) > max_w and '-' in word[:-1]:
            cut = word.index('-') + 1
            words.append(word[:cut])
            word = word[cut:]
        words.append(word)
    lines, line = [], ''
    for word in words:
        joiner = '' if line.endswith('-') else ' '
        trial = (line + joiner + word) if line else word
        if line and text_width(trial, size, bold) > max_w:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    return lines


# ---------------------------------------------------------------- svg helpers

def num(v):
    """Coordinates with at most 2 decimals and no trailing zeros."""
    s = ('%.2f' % v).rstrip('0').rstrip('.')
    return '0' if s in ('-0', '') else s


def attr(s):
    return escape(str(s), {'"': '&quot;'})


class Svg:
    def __init__(self, theme):
        self.t = THEMES[theme]
        self.parts = []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=13, bold=False, fill='ink', anchor='start', cls=None):
        weight = ' font-weight="600"' if bold else ''
        anchor_attr = '' if anchor == 'start' else ' text-anchor="%s"' % anchor
        cls_attr = ' class="%s"' % cls if cls else ''
        self.add('<text x="%s" y="%s" font-size="%s"%s fill="%s"%s%s>%s</text>' % (
            num(x), num(y), size, weight, self.t[fill], anchor_attr, cls_attr, escape(s)))

    def pair(self, x, y, strong, quiet, size=13):
        """A bold name followed by a quieter descriptor, spaced by the browser."""
        quiet_part = ('<tspan dx="8" fill="%s">%s</tspan>' % (self.t['ink2'], escape(quiet))
                      if quiet else '')
        self.add('<text x="%s" y="%s" font-size="%s"><tspan font-weight="600" fill="%s">%s</tspan>'
                 '%s</text>' % (num(x), num(y), size, self.t['ink'], escape(strong), quiet_part))

    def lines(self, x, y, lines, size=13, bold=False, fill='ink', anchor='start', step=None):
        step = step or round(size * 1.35)
        for i, line in enumerate(lines):
            self.text(x, y + i * step, line, size, bold, fill, anchor)
        return y + (len(lines) - 1) * step

    def rect(self, x, y, w, h, fill, rx=0, extra=''):
        r = ' rx="%s"' % num(rx) if rx else ''
        self.add('<rect x="%s" y="%s" width="%s" height="%s"%s fill="%s"%s/>' % (
            num(x), num(y), num(w), num(h), r, fill, extra))

    def line(self, x1, y1, x2, y2, stroke, width=1, extra=''):
        self.add('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"%s/>' % (
            num(x1), num(y1), num(x2), num(y2), stroke, num(width), extra))

    def render(self, height, title, desc, width=WIDTH, card=True):
        t = self.t
        head = [
            '<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s" '
            'role="img" aria-labelledby="title desc" font-family="%s">' % (
                width, num(height), width, num(height), attr(FONT)),
            '<title id="title">%s</title>' % escape(title),
            '<desc id="desc">%s</desc>' % escape(desc),
        ]
        if card:
            head.append(
                '<rect x="0.5" y="0.5" width="%s" height="%s" rx="12" fill="%s" stroke="%s" '
                'stroke-opacity="%s"/>' % (num(width - 1), num(height - 1), t['surface'],
                                           t['border'], t['border_op']))
        return '\n'.join(head + self.parts + ['</svg>']) + '\n'


def bar_path(x, y, w, h, round_end=True):
    """A bar that is square at the baseline and rounded (4px) at the data end."""
    r = min(4.0, w / 2, h / 2) if round_end else 0
    if r <= 0:
        return 'M%s %sh%sv%sh%sz' % (num(x), num(y), num(w), num(h), num(-w))
    return ('M%s %sH%sA%s %s 0 0 1 %s %sV%sA%s %s 0 0 1 %s %sH%sz' % (
        num(x), num(y), num(x + w - r), num(r), num(r), num(x + w), num(y + r),
        num(y + h - r), num(r), num(r), num(x + w - r), num(y + h), num(x)))


def header(svg, title, subtitle, y=PAD):
    svg.text(PAD, y + 18, title, size=18, bold=True)
    sub = wrap(subtitle, 13, WIDTH - 2 * PAD)
    last = svg.lines(PAD, y + 42, sub, size=13, fill='ink2', step=18)
    return last + 22


def footer(svg, y, notes):
    """Small print under a chart: one paragraph per note, a little space between them."""
    y += 12
    for note in notes:
        last = svg.lines(PAD, y, wrap(note, 12, WIDTH - 2 * PAD), size=12, fill='ink2', step=17)
        y = last + 22
    return y - 22 + PAD - 4


def fmt_mb(v, decimals):
    return '{:,.{d}f} MB'.format(v, d=decimals)


def decimals_of(v):
    s = repr(float(v))
    return min(2, len(s.split('.')[1].rstrip('0'))) if '.' in s else 0


# ---------------------------------------------------------------- bar charts

def hbar_rows(svg, rows, y, vmax, label_room):
    """Rows with the name above a horizontal bar and the value at the bar's tip.

    Each row: name, scope, segments [(value, color)], label, detail (optional).
    A zero value gets no bar, only the baseline tick and its label.
    """
    t = svg.t
    x0 = PAD
    max_w = WIDTH - 2 * PAD - label_room
    bar_h = 18
    for row in rows:
        svg.pair(x0, y + 14, row['name'], row.get('scope'))
        top = y + 24
        svg.line(x0, top - 3, x0, top + bar_h + 3, t['muted'])   # baseline tick, visible at 0
        x = x0
        segs = [s for s in row['segments'] if s[0] > 0]
        for i, (value, color) in enumerate(segs):
            w = max(2.0, value / vmax * max_w)
            last = i == len(segs) - 1
            gap = 1 if len(segs) > 1 else 0          # 2px surface gap between segments
            x_start = x + (gap if i > 0 else 0)
            w_draw = w - (gap if i > 0 else 0) - (gap if not last else 0)
            svg.add('<path class="bar" d="%s" fill="%s"/>' % (
                bar_path(x_start, top, w_draw, bar_h, round_end=last), t[color]))
            x += w
        svg.text(x + 8, top + bar_h / 2 + 4.5, row['label'], size=13, bold=True)
        y = top + bar_h
        if row.get('detail'):
            for line in wrap(row['detail'], 12, WIDTH - 2 * PAD):
                svg.text(x0, y + 17, line, size=12, fill='ink2')
                y += 17
        y += 22
    return y - 6


def chart_services(data, theme):
    d = data['services']
    svg = Svg(theme)
    y = header(svg, d['title'], d['subtitle'])
    rows = []
    for r in d['rows']:
        rows.append({
            'name': r['tool'], 'scope': r['scope'],
            'segments': [(r['value'], 'accent')],
            'label': str(r['value']), 'detail': r['detail'],
        })
    y = hbar_rows(svg, rows, y, vmax=max(r['value'] for r in d['rows']), label_room=40)
    y = footer(svg, y, d['notes'] + [
        'Sources: Healenium compose files and the robotframework-heal and mendwright READMEs, '
        'checked %s.' % data['date'],
        'Data and links: docs/charts/data.json'])
    desc = '; '.join('%s: %d' % (r['tool'], r['value']) for r in d['rows'])
    return svg.render(y, d['title'], 'Bar chart. ' + desc + '.')


def chart_download(data, theme):
    d = data['download']
    svg = Svg(theme)
    y = header(svg, d['title'], d['subtitle'] + ' MB = 1,000,000 bytes.')
    rows = []
    for r in d['rows']:
        rows.append({
            'name': r['tool'], 'scope': r['version'],
            'segments': [(r['mb'], 'accent')],
            'label': '%s, %d packages' % (fmt_mb(r['mb'], 2), r['packages']),
        })
    y = hbar_rows(svg, rows, y, vmax=max(r['mb'] for r in d['rows']), label_room=165)
    linux = d['rows'][0]['linux_x86_64']
    notes = [
        'Resolved with pip install --dry-run on CPython 3.12, Windows x64, on %s. Sizes are '
        'the wheel files listed on PyPI.' % data['date'],
        'Not counted: playwright, greenlet, pyee, patchright, selenium, robotframework, '
        'robotframework-browser, grpcio, Appium libraries, and the Chrome that stagehand and '
        'browser-use drive. On Linux x86_64, mendwright and lxml come to %s.'
        % fmt_mb(linux['mb'], 2),
        'Data and sources: docs/charts/data.json',
    ]
    y = footer(svg, y, notes)
    desc = '; '.join('%s %s: %s MB, %d packages' % (r['tool'], r['version'], r['mb'], r['packages'])
                     for r in d['rows'])
    return svg.render(y, d['title'], 'Bar chart. ' + desc + '.')


def chart_infra(data, theme):
    d = data['infra']
    svg = Svg(theme)
    t = svg.t
    y = header(svg, d['title'], d['subtitle'])
    # Legend: two series, so a legend is required.
    x = PAD
    colors = {'tool': 'accent', 'grid': 'series2'}
    for s in d['series']:
        svg.rect(x, y - 3, 12, 12, t[colors[s['id']]], rx=3)
        svg.text(x + 18, y + 7.5, s['label'], size=13, fill='ink2')
        x += 18 + text_width(s['label'], 13) + 24
    y += 26
    wheels = data['download']['rows'][0]['linux_x86_64']['parts_bytes']
    details = {
        'mendwright': ' + '.join('%s %s' % (name.split('-')[0], fmt_mb(size / 1e6, 2))
                                 for name, size in wheels.items()) + ' (wheel files)',
    }
    rows = []
    for r in d['rows']:
        segs = [(s['mb'], colors[s['series']]) for s in r['segments']]
        dec = decimals_of(r['total_mb'])
        if r['tool'] in details:
            detail = details[r['tool']]
        elif len(r['segments']) > 1:
            detail = 'Healenium services %s + Selenium hub and Chromium node %s' % (
                fmt_mb(r['segments'][0]['mb'], 1), fmt_mb(r['segments'][1]['mb'], 1))
        else:
            detail = ' + '.join('%s %s' % (i['image'].split('/')[-1].split(':')[0], fmt_mb(i['mb'], 1))
                                for i in r['images'])
        rows.append({'name': r['tool'], 'scope': r['scope'], 'segments': segs,
                     'label': fmt_mb(r['total_mb'], dec), 'detail': detail})
    y = hbar_rows(svg, rows, y, vmax=max(r['total_mb'] for r in d['rows']), label_room=90)
    full = d['shipped_compose']['total_mb']
    notes = [
        'Upper bounds: base layers shared between images are counted once per image. Client '
        'libraries and browsers are not counted, except the Chromium node image.',
        'The shipped Healenium-Proxy compose file, with Edge and Firefox nodes as well, comes '
        'to %s.' % fmt_mb(full, 1),
        'Docker Hub API, linux/amd64, %s. Data and sources: docs/charts/data.json' % data['date'],
    ]
    y = footer(svg, y, notes)
    desc = '; '.join('%s: %s MB' % (r['tool'], r['total_mb']) for r in d['rows'])
    return svg.render(y, d['title'], 'Bar chart. ' + desc + '.')


# ---------------------------------------------------------------- feature matrix

def draw_mark(svg, state, cx, cy):
    t = svg.t
    if state == 'yes':
        svg.add('<circle class="mark" cx="%s" cy="%s" r="7" fill="%s"/>' % (num(cx), num(cy), t['accent']))
    elif state == 'partial':
        svg.add('<circle class="mark" cx="%s" cy="%s" r="6.25" fill="none" stroke="%s" '
                'stroke-width="1.5"/>' % (num(cx), num(cy), t['accent']))
        svg.add('<path class="mark" d="M%s %sA6.25 6.25 0 0 0 %s %sZ" fill="%s"/>' % (
            num(cx), num(cy - 6.25), num(cx), num(cy + 6.25), t['accent']))
    elif state == 'no':
        svg.add('<circle class="mark" cx="%s" cy="%s" r="6.25" fill="none" stroke="%s" '
                'stroke-width="1.5"/>' % (num(cx), num(cy), t['muted']))
    elif state == 'unknown':
        svg.text(cx, cy + 5, '?', size=15, bold=True, fill='ink2', anchor='middle')
    elif state == 'na':
        svg.text(cx, cy + 4, 'n/a', size=12, fill='ink2', anchor='middle')


def chart_features(data, theme):
    d = data['features']
    svg = Svg(theme)
    t = svg.t
    y = header(svg, d['title'], d['subtitle'] + ' Checked %s.' % data['date'])
    # Legend: shape carries the meaning, color repeats it.
    legend = [('yes', 'yes'), ('partial', 'partial or opt-in'), ('no', 'no'),
              ('unknown', 'not documented'), ('na', 'does not apply')]
    x = PAD
    for state, label in legend:
        mark_w = 22 if state == 'na' else 14
        draw_mark(svg, state, x + mark_w / 2, y + 3)
        svg.text(x + mark_w + 7, y + 7.5, label, size=13, fill='ink2')
        x += mark_w + 7 + text_width(label, 13) + 20
    y += 30

    label_w = 196
    tools = d['tools']
    col_w = (WIDTH - 2 * PAD - label_w) / len(tools)
    cx = [PAD + label_w + col_w * (i + 0.5) for i in range(len(tools))]

    # Column headers
    head_top = y
    head_lines = []
    for tool in tools:
        name = wrap(tool['label'], 13, col_w - 8, bold=True)
        sub = wrap(tool['sub'], 12, col_w - 8) if tool['sub'] else []
        head_lines.append((name, sub))
    head_h = max(len(n) * 17 + len(s) * 16 for n, s in head_lines) + 12
    wash_at = len(svg.parts)   # the column wash goes under everything drawn from here
    for i, (name, sub) in enumerate(head_lines):
        yy = y + 14
        for line in name:
            svg.text(cx[i], yy, line, size=13, bold=True, anchor='middle')
            yy += 17
        for line in sub:
            svg.text(cx[i], yy - 1, line, size=12, fill='ink2', anchor='middle')
            yy += 16
    y += head_h
    svg.line(PAD, y, WIDTH - PAD, y, t['axis'])

    # Rows
    for r_i, row in enumerate(d['rows']):
        label_lines = wrap(row['label'], 13, label_w - 14)
        notes = {tool['id']: wrap(row['cells'][tool['id']]['note'], 12, col_w - 8)
                 if row['cells'][tool['id']]['note'] else [] for tool in tools}
        n_note = max(len(v) for v in notes.values())
        h = max(40 + 14 * n_note, 24.5 + 16 * (len(label_lines) - 1) + 14)
        mark_y = y + 20
        svg.lines(PAD, mark_y + 4.5, label_lines, size=13, step=16)
        for i, tool in enumerate(tools):
            cell = row['cells'][tool['id']]
            draw_mark(svg, cell['state'], cx[i], mark_y)
            for k, line in enumerate(notes[tool['id']]):
                svg.text(cx[i], mark_y + 25 + 14 * k, line, size=12, fill='ink2', anchor='middle')
        y += h
        if r_i < len(d['rows']) - 1:
            svg.line(PAD, y, WIDTH - PAD, y, t['grid'])
    # Emphasis: a light wash behind the mendwright column, under its marks and text.
    svg.parts.insert(wash_at, '<rect x="%s" y="%s" width="%s" height="%s" rx="8" fill="%s" '
                     'fill-opacity="%s"/>' % (num(PAD + label_w + 2), num(head_top - 4),
                                              num(col_w - 4), num(y - head_top + 4),
                                              t['accent'], t['wash_op']))

    y = footer(svg, y + 6, [
        'A note under a mark gives the limit or the evidence. "per README" means the project '
        'says so; it was not tested here.',
        'Healenium means the open source edition, Web and Proxy. Healenium Pro adds an LLM '
        'and Playwright.',
        "Each cell's source is linked in docs/charts/data.json.",
    ])
    desc = 'Dot matrix of %d features for %s.' % (
        len(d['rows']), ', '.join(tl['label'] for tl in tools))
    return svg.render(y, d['title'], desc)


# ---------------------------------------------------------------- flow diagram

def chart_flow(data, theme):
    svg = Svg(theme)
    t = svg.t
    title = 'How a heal works'
    y = header(svg, title, 'What mendwright does when a stored selector stops matching.')
    svg.add('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
            'markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10z" fill="%s"/>'
            '</marker></defs>' % t['muted'])

    spine_x, spine_w = PAD, 340
    exit_x = 470
    exit_w = WIDTH - PAD - exit_x
    mid = spine_x + spine_w / 2
    gap = 26

    def box(x, w, top, h, title_text, sub=(), kind='step', icon=None):
        """kind: step | decision | pill | exit. Text is centred unless an icon is set."""
        rx = h / 2 if kind == 'pill' else 10
        stroke = t['accent'] if kind == 'decision' else t['axis']
        sw = '1.5' if kind == 'decision' else '1'
        svg.add('<g class="node">')
        svg.add('<rect x="%s" y="%s" width="%s" height="%s" rx="%s" fill="%s" stroke="%s" '
                'stroke-width="%s"/>' % (num(x), num(top), num(w), num(h), num(rx),
                                         t['surface'], stroke, sw))
        block_h = 18 + (len(sub) * 17 if sub else 0)
        ty = top + (h - block_h) / 2 + 13
        if icon:
            ix, iy = x + 22, top + h / 2
            color = t['good'] if icon == 'check' else t['critical']
            svg.add('<circle cx="%s" cy="%s" r="9" fill="%s"/>' % (num(ix), num(iy), color))
            if icon == 'bang':
                svg.line(ix, iy - 4.5, ix, iy + 1, t['on_mark'], 2, ' stroke-linecap="round"')
                svg.add('<circle cx="%s" cy="%s" r="1.2" fill="%s"/>' % (num(ix), num(iy + 4.3), t['on_mark']))
            elif icon == 'bar':
                svg.line(ix - 4.5, iy, ix + 4.5, iy, t['on_mark'], 2, ' stroke-linecap="round"')
            elif icon == 'check':
                svg.add('<path d="M%s %sl3 3l5.5 -6" fill="none" stroke="%s" stroke-width="2" '
                        'stroke-linecap="round" stroke-linejoin="round"/>' % (
                            num(ix - 4.5), num(iy + 0.2), t['on_mark']))
            tx, anchor = x + 42, 'start'
        else:
            tx, anchor = x + w / 2, 'middle'
        svg.text(tx, ty, title_text, size=14, bold=True, anchor=anchor)
        for k, line in enumerate(sub):
            svg.text(tx, ty + 18 + 17 * k, line, size=12, fill='ink2', anchor=anchor)
        svg.add('</g>')
        return top + h

    def down(y1, y2, label=None):
        svg.add('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.5" '
                'marker-end="url(#arrow)"/>' % (num(mid), num(y1), num(mid), num(y2 - 1), t['muted']))
        if label:
            svg.text(mid + 10, (y1 + y2) / 2 + 4, label, size=12, fill='ink2')

    def across(yc, label):
        x1, x2 = spine_x + spine_w, exit_x
        svg.add('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="1.5" '
                'marker-end="url(#arrow)"/>' % (num(x1), num(yc), num(x2 - 1), num(yc), t['muted']))
        svg.text((x1 + x2) / 2, yc - 8, label, size=12, fill='ink2', anchor='middle')

    def exit_box(yc, title_text, sub, icon):
        h = 58 if len(sub) < 2 else 70
        box(exit_x, exit_w, yc - h / 2, h, title_text, sub, kind='exit', icon=icon)

    top = y + 4
    b = box(spine_x, spine_w, top, 40, 'A stored selector stops matching', kind='pill')
    down(b, b + gap)
    top = b + gap
    b = box(spine_x, spine_w, top, 58, 'Refuse before asking the model?',
            ['destructive, role unknown or hourly limit used up'], kind='decision')
    across(top + 29, 'yes')
    exit_box(top + 29, 'Stop and raise AttentionRequired', ['The model is not called.'], 'bang')
    down(b, b + gap, 'no')
    top = b + gap
    b = box(spine_x, spine_w, top, 58, 'Capture the page', ['HTML snapshot, redacted first'])
    down(b, b + gap)
    top = b + gap
    b = box(spine_x, spine_w, top, 58, 'The model proposes an XPath',
            ['from the provider you configured'])
    down(b, b + gap)
    top = b + gap
    checks = ['not a brittle index path',
              'not quarantined or superseded for this key',
              'exactly one match in the snapshot',
              'the expected role',
              'no destructive word in a button or link label',
              'unique, visible, same role and label on the live page']
    h = 34 + 19 * len(checks) + 10
    svg.add('<g class="node">')
    svg.add('<rect x="%s" y="%s" width="%s" height="%s" rx="10" fill="%s" stroke="%s" '
            'stroke-width="1.5"/>' % (num(spine_x), num(top), num(spine_w), num(h),
                                      t['surface'], t['accent']))
    svg.text(spine_x + 20, top + 25, 'Local checks, all must pass', size=14, bold=True)
    for k, item in enumerate(checks):
        cy = top + 47 + 19 * k
        svg.add('<circle cx="%s" cy="%s" r="2.5" fill="%s"/>' % (num(spine_x + 25), num(cy - 4), t['accent']))
        svg.text(spine_x + 36, cy, item, size=13, fill='ink2')
    svg.add('</g>')
    b = top + h
    across(top + h / 2, 'fail')
    exit_box(top + h / 2, 'One more try, with the reason',
             ['If that fails too: AttentionRequired,', 'with the reason. Nothing is saved.'], 'bang')
    down(b, b + gap, 'pass')
    top = b + gap
    b = box(spine_x, spine_w, top, 58, 'Save it and retry the step',
            ['in .mendwright/overrides.json'])
    across(top + 29, 'action fails')
    exit_box(top + 29, 'Quarantine the healed selector', ['so it is not used again'], 'bar')
    down(b, b + gap, 'action works')
    top = b + gap
    b = box(spine_x, spine_w, top, 40, 'The step continues', kind='pill')
    y = footer(svg, b + 4, ['Code: mendwright/facade.py, coordinator.py and gate.py.'])
    desc = ('Flow: a selector misses; a destructive control, an unknown role or a used-up hourly '
            'limit stops with AttentionRequired before any model call; otherwise the page is '
            'captured and redacted, a model proposes an XPath, and local checks run (not a '
            'brittle index path, not quarantined or superseded for this key, exactly one match, '
            'expected role, no destructive word in a button or link label, unique, visible, same '
            'role and label on the live page); a failure gives the model the reason and one more try, and a '
            'second failure raises AttentionRequired with nothing saved; a pass is saved and the '
            'step retried; if the action then fails the healed selector is quarantined.')
    return svg.render(y, title, desc)


# ---------------------------------------------------------------- logo

def logo(data, theme):
    """Wordmark plus a small mark: a selector bracket pair with a stitched seam."""
    t = THEMES[theme]
    # The wordmark is live text in the system font. Its natural width is about 146px in
    # Segoe UI and 149px in Arial or Helvetica, so a fixed textLength of 148 (spacing only,
    # glyphs untouched) keeps the logo the same width everywhere.
    w, h = 210, 48
    word = 'mendwright'
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" '
        'role="img" aria-labelledby="title" font-family="%s">' % (w, h, w, h, attr(FONT)),
        '<title id="title">mendwright</title>',
        # Mark: [ and ] with a seam between them, crossed by three stitches.
        '<g fill="none" stroke-linecap="round" stroke-linejoin="round">',
        '<path d="M14 8H7V40H14M34 8H41V40H34" stroke="%s" stroke-width="4"/>' % t['ink'],
        '<path d="M13 24H35" stroke="%s" stroke-width="2.5"/>' % t['accent'],
        '<path d="M17 19L19 29M23 19L25 29M29 19L31 29" stroke="%s" stroke-width="2.5"/>' % t['accent'],
        '</g>',
        '<text x="58" y="33.5" font-size="27" font-weight="600" letter-spacing="-0.4" '
        'textLength="148" lengthAdjust="spacing" fill="%s">%s</text>' % (t['ink'], word),
        '</svg>',
    ]
    return '\n'.join(parts) + '\n'


# ---------------------------------------------------------------- checks

class DataError(Exception):
    pass


def check_data(data):
    def need(cond, msg):
        if not cond:
            raise DataError(msg)

    for r in data['services']['rows']:
        need(r['value'] == len(r['parts']), 'services: %s value does not match its parts' % r['tool'])
        need(r.get('source'), 'services: %s has no source' % r['tool'])
    for r in data['download']['rows']:
        need(r.get('source'), 'download: %s has no source' % r['tool'])
        if 'bytes' in r:
            need(sum(r['parts_bytes'].values()) == r['bytes'], 'download: %s parts' % r['tool'])
            need(round(r['bytes'] / 1e6, 2) == r['mb'], 'download: %s MB rounding' % r['tool'])
            lin = r['linux_x86_64']
            need(sum(lin['parts_bytes'].values()) == lin['bytes'], 'download: %s linux parts' % r['tool'])
            need(round(lin['bytes'] / 1e6, 2) == lin['mb'], 'download: %s linux MB' % r['tool'])
    infra = data['infra']
    for r in infra['rows']:
        seg_total = sum(s['mb'] for s in r['segments'])
        need(abs(seg_total - r['total_mb']) < 0.05, 'infra: %s segments do not add up' % r['tool'])
        for s in r['segments']:
            imgs = [i for i in r.get('images', []) if i.get('series', 'tool') == s['series']]
            if imgs:
                need(abs(sum(i['mb'] for i in imgs) - s['mb']) < 0.05,
                     'infra: %s %s images do not add up' % (r['tool'], s['series']))
    need(infra['rows'][0]['total_mb'] == data['download']['rows'][0]['linux_x86_64']['mb'],
         'infra: mendwright must use the Linux wheel total')
    proxy = infra['rows'][-1]
    full = proxy['total_mb'] + sum(a['mb'] for a in infra['shipped_compose']['adds'])
    need(abs(full - infra['shipped_compose']['total_mb']) < 0.05, 'infra: shipped compose total')
    feats = data['features']
    states = set(feats['states'])
    for row in feats['rows']:
        for tool in feats['tools']:
            cell = row['cells'].get(tool['id'])
            need(cell is not None, 'features: %s/%s missing' % (row['id'], tool['id']))
            need(cell['state'] in states, 'features: %s/%s bad state' % (row['id'], tool['id']))
            need(cell.get('source'), 'features: %s/%s has no source' % (row['id'], tool['id']))


def pending(data):
    out = []
    for row in data['features']['rows']:
        for tool_id, cell in row['cells'].items():
            if cell.get('status'):
                out.append('%s: features/%s/%s. %s' % (cell['status'], row['id'], tool_id,
                                                       cell.get('status_note', '')))
    for r in data['download']['rows']:
        if r.get('status'):
            out.append('%s: download/%s' % (r['status'], r['tool']))
    return out


# ---------------------------------------------------------------- main

CHARTS = [
    ('compare-services', chart_services),
    ('compare-download', chart_download),
    ('compare-infra', chart_infra),
    ('feature-matrix', chart_features),
    ('how-it-works', chart_flow),
    ('logo', logo),
]


def build(data):
    out = {}
    for name, fn in CHARTS:
        for theme in ('light', 'dark'):
            out['%s-%s.svg' % (name, theme)] = fn(data, theme)
    return out


def main(argv):
    check_only = '--check' in argv
    data = json.loads(DATA.read_text(encoding='utf-8'))
    try:
        check_data(data)
    except DataError as e:
        print('data.json problem: %s. Fix docs/charts/data.json and run again.' % e, file=sys.stderr)
        return 2
    files = build(data)
    stale = []
    for name, text in files.items():
        path = ASSETS / name
        if check_only:
            current = path.read_text(encoding='utf-8') if path.exists() else None
            if current != text:
                stale.append(name)
        else:
            ASSETS.mkdir(parents=True, exist_ok=True)
            with open(path, 'w', encoding='utf-8', newline='\n') as fh:
                fh.write(text)
    for line in pending(data):
        print(line, file=sys.stderr)
    if check_only and stale:
        print('Out of date: %s. Run python docs/charts/build_charts.py and commit the result.'
              % ', '.join(stale), file=sys.stderr)
        return 1
    if not check_only:
        print('Wrote %d files to %s' % (len(files), ASSETS))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
