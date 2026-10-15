"""`mendwright report --format html`: the report as one self-contained web page.

The page has inline CSS and no scripts, fonts, images or links, so it opens from a CI
artifact or an email attachment and loads nothing from the network. It follows the
reader's light or dark setting (prefers-color-scheme) and re-flows to a stacked layout on
a phone. Every value that comes from the store or the journal (keys, selectors, page text
quoted by a model, provider names) is escaped, control characters are replaced, and the
whole document is plain ASCII (other characters are written as numeric character
references), so it is safe to print on any console and to serve under any encoding.

render_html() takes what `mendwright report` already gathers (collect_report) and the raw
journal events, oldest first. It reads no clock, so the same input gives the same bytes.
"""

from __future__ import annotations

import html
import re

LAST_HEALS = 25
LAST_FLOW = 50

_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
_FLOW_EVENTS = ('flow.interrupt', 'flow.skip', 'flow.page_guard', 'flow.dismiss')


def h(value):
    """value as text that is safe inside HTML, in an attribute or between tags."""
    text = value if isinstance(value, str) else str(value)
    text = text.encode('utf-8', 'replace').decode('utf-8')      # lone surrogates
    return html.escape(_CONTROL.sub('�', text), quote=True)


def _when(ts):
    """'2026-10-10T09:30:00+00:00' -> '2026-10-10 09:30:00 UTC'; 'never' when missing."""
    if not isinstance(ts, str) or not ts:
        return 'never'
    text = ts.replace('T', ' ')
    for suffix in ('+00:00', 'Z'):
        if text.endswith(suffix):
            return text[:-len(suffix)] + ' UTC'
    return text


def _latency(ms):
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return '-'
    return ('%.1f s' % (ms / 1000.0)) if ms >= 1000 else ('%d ms' % ms)


def _plural(n, word):
    return str(n) + ' ' + word + ('' if n == 1 else 's')


def _code(value):
    return '<code>' + h(value) + '</code>'


def _badge(text, kind):
    return '<span class="badge ' + kind + '">' + h(text) + '</span>'


def _cell(label, content, css=''):
    return '<td data-label="' + h(label) + '"' + (' class="' + css + '"' if css else '') + '>' \
        + content + '</td>'


def _table(headers, rows, caption):
    out = ['<table>', '<caption class="sr">' + h(caption) + '</caption>', '<thead><tr>']
    out.extend('<th scope="col">' + h(name) + '</th>' for name in headers)
    out.append('</tr></thead>')
    out.append('<tbody>')
    out.extend(rows)
    out.append('</tbody></table>')
    return out


def _text_of(value):
    return value if isinstance(value, str) else ''


# --- what the journal says ---------------------------------------------------------------

def _heal_records(events):
    """Every heal.applied event, oldest first, with whether a later canary confirmed it."""
    records, open_ = [], {}
    for event in events:
        name, key, xpath = event.get('event'), event.get('key'), event.get('xpath')
        if name == 'heal.applied':
            old = event.get('old')
            record = {'when': event.get('ts'), 'key': key, 'new': xpath,
                      'old': [str(o) for o in old] if isinstance(old, list) else [],
                      'provider': event.get('provider'), 'model': event.get('model'),
                      'latency_ms': event.get('latency_ms'), 'state': 'unconfirmed'}
            records.append(record)
            open_[(key, xpath)] = record
        elif name == 'heal.canary' and (key, xpath) in open_:
            open_[(key, xpath)]['state'] = 'confirmed' if event.get('success') is True else 'failed'
        elif name == 'heal.quarantine' and (key, xpath) in open_:
            open_[(key, xpath)]['state'] = 'failed'
    return records


def _quarantines(report, events):
    """Rows for the quarantined selectors: the ones the store holds, with when the journal
    says they were quarantined, and quarantine events whose selector is no longer there."""
    when = {}
    for event in events:
        if event.get('event') == 'heal.quarantine':
            when[(event.get('key'), event.get('xpath'))] = event.get('ts')
    rows, seen = [], set()
    for key in report['keys']:
        for c in key['candidates']:
            if c['status'] == 'quarantined':
                pair = (key['key'], c['selector'])
                seen.add(pair)
                rows.append({'key': key['key'], 'selector': c['selector'], 'source': c['source'],
                             'when': when.get(pair), 'in_store': True})
    for (key, xpath), ts in when.items():
        if (key, xpath) not in seen:
            rows.append({'key': key, 'selector': xpath, 'source': None, 'when': ts,
                         'in_store': False})
    return rows


# --- the sections ------------------------------------------------------------------------

def _card(number, label, note=''):
    return ('<div class="card"><div class="num">' + h(number) + '</div><div class="label">'
            + h(label) + '</div>' + ('<div class="note">' + h(note) + '</div>' if note else '')
            + '</div>')


def _summary(report, records, quarantines, flow_counts):
    keys = report['keys']
    total = sum(len(k['candidates']) for k in keys)
    active = sum(1 for k in keys for c in k['candidates'] if c['status'] == 'active')
    j = report['journal']
    have = j['exists']
    count = j['counts']
    confirmed = sum(1 for r in records if r['state'] == 'confirmed')
    cards = [
        _card(len(keys), 'keys'),
        _card(total, 'selectors', str(active) + ' active'),
        _card(len([q for q in quarantines if q['in_store']]), 'quarantined now',
              'selectors the store will not try'),
        _card(count['requested'] if have else '-', 'heals asked for',
              'by fingerprint or model'),
        _card(count['applied'] if have else '-', 'heals applied'),
        _card(confirmed if have else '-', 'confirmed', 'by a passing test or check'),
        _card(count['rejected'] if have else '-', 'proposals rejected', 'by the local checks'),
        _card(flow_counts['flow.interrupt'] if have else '-', 'interruptions handled'),
        _card(flow_counts['flow.skip'] if have else '-', 'steps skipped'),
        _card(flow_counts['flow.page_guard'] if have else '-', 'page guard stops',
              'heals declined on another page'),
    ]
    if have and flow_counts['flow.dismiss']:
        cards.append(_card(flow_counts['flow.dismiss'], 'popups dismissed', 'with dismiss=True'))
    return ['<section aria-labelledby="summary"><h2 id="summary">Summary</h2>',
            '<div class="cards">'] + cards + ['</div></section>']


def _candidate(c):
    status = c['status']
    kind = {'active': 'ok', 'quarantined': 'bad'}.get(status, 'muted')
    source = c['source']
    skind = {'heal': 'info', 'code': 'plain', 'seed': 'plain'}.get(source, 'plain')
    frame = ('<div class="sub">inside frame ' + _code(c['frame']) + '</div>'
             if c.get('frame') else '')
    if c.get('css'):
        frame += '<div class="sub">as CSS ' + _code(c['css']) + '</div>'
    return ('<li><div class="line">' + _badge(source, skind) + ' ' + _badge(status, kind) + '</div>'
            + _code(c['selector']) + frame
            + '<div class="sub">last success ' + h(_when(c['last_success'])) + '</div></li>')


def _keys_section(report):
    keys = report['keys']
    out = ['<section aria-labelledby="keys"><h2 id="keys">Keys and selectors</h2>']
    if not keys:
        out.append('<p class="empty">The store has no keys yet. A key is added the first time '
                   'your code calls <code>heal.locator()</code> with it.</p></section>')
        return out
    per_key = report['journal']['per_key']
    rows = []
    for k in keys:
        marks = ''
        if k['destructive']:
            marks = '<div class="line">' + _badge('destructive, never auto-healed', 'bad') + '</div>'
        cands = ('<ul class="cands">' + ''.join(_candidate(c) for c in k['candidates']) + '</ul>'
                 if k['candidates'] else '<span class="muted">no selectors saved</span>')
        counts = per_key.get(k['key'])
        heals = ('<div>' + h(counts['requested']) + ' asked, ' + h(counts['applied']) + ' applied</div>'
                 '<div class="sub">' + h(counts['rejected']) + ' rejected, '
                 + h(counts['quarantined']) + ' quarantined</div>') if counts \
            else '<span class="muted">none</span>'
        rows.append('<tr>' + _cell('Key', _code(k['key']) + marks, 'keycell')
                    + _cell('Role', h(k['role'] or 'unknown'))
                    + _cell('Selectors', cands) + _cell('Heals', heals) + '</tr>')
    out.extend(_table(('Key', 'Role', 'Selectors', 'Heals'), rows, 'Keys and their selectors'))
    out.append('</section>')
    return out


def _state_badge(state):
    if state == 'confirmed':
        return _badge('confirmed', 'ok')
    if state == 'failed':
        return _badge('failed, quarantined', 'bad')
    return _badge('not confirmed yet', 'warn')


def _heals_section(report, records):
    out = ['<section aria-labelledby="heals"><h2 id="heals">Recent heals</h2>']
    if not report['journal']['exists']:
        out.append('<p class="empty">There is no <code>journal.jsonl</code> next to the store, so '
                   'there are no heals to list. mendwright.open() writes it when a heal runs.</p>'
                   '</section>')
        return out
    if not records:
        out.append('<p class="empty">No heal has been applied yet.</p></section>')
        return out
    shown = records[-LAST_HEALS:][::-1]
    out.append('<p class="lead">Newest first' + (', the last ' + str(LAST_HEALS) + ' of '
               + str(len(records)) if len(records) > LAST_HEALS else '')
               + '. A heal is confirmed when the test that made it passed (the pytest plugin '
               'reports that), or when an expect= check or confirm(True) said so.</p>')
    rows = []
    for r in shown:
        old = r['old']
        was = (_code(old[0]) + (' <span class="sub">(+' + str(len(old) - 1) + ' more)</span>'
                                if len(old) > 1 else '')) if old else '<span class="muted">(none)</span>'
        who = h(r['provider'] or 'unknown provider') + (
            '<div class="sub">' + h(r['model']) + '</div>' if r['model'] else '')
        change = ('<div class="chg"><span class="lbl">old</span> ' + was + '</div>'
                  '<div class="chg"><span class="lbl">new</span> ' + _code(r['new']) + '</div>')
        rows.append('<tr>' + _cell('When', h(_when(r['when'])), 'nowrap')
                    + _cell('Key', _code(r['key']), 'k')
                    + _cell('Selector change', change)
                    + _cell('State', _state_badge(r['state']))
                    + _cell('Provider and model', who)
                    + _cell('Latency', h(_latency(r['latency_ms'])), 'nowrap') + '</tr>')
    out.extend(_table(('When', 'Key', 'Selector change', 'State', 'Provider and model', 'Latency'),
                      rows, 'Recent applied heals'))
    out.append('</section>')
    return out


def _flow_detail(event):
    name = event.get('event')
    if name == 'flow.interrupt':
        before = event.get('before')
        how = ('preset ' + _code(event.get('preset')) if event.get('preset')
               else ('selector ' + _code(event.get('selector')) if event.get('selector') else ''))
        action = event.get('action')
        return ('Handled before ' + (_code(before) if before else 'a step') + ': '
                + h(action or 'action') + (' via ' + how if how else '') + '.')
    if name == 'flow.skip':
        return 'Skipped instead of healed: ' + h(event.get('reason') or 'no reason recorded') + '.'
    if name == 'flow.dismiss':
        return _dismiss_detail(event)
    last, here = event.get('last_page'), event.get('page')
    return ('Heal declined. The key was last found on ' + (_code(last) if last else 'another page')
            + ' and the page is now ' + (_code(here) if here else 'a different one') + '.')


def _step(step):
    """One step on a popup, as the detail of a flow.dismiss event names it."""
    if not isinstance(step, dict):
        return 'a step'
    if step.get('how') == 'escape':
        return 'Escape'
    return 'its button ' + _code(step['label']) if step.get('label') else 'a button'


def _dismissed(event):
    """Whether a flow.dismiss event is a dismissal, not a popup mendwright gave up on."""
    return event.get('outcome', 'dismissed') == 'dismissed'


def _dismiss_detail(event):
    before = event.get('before')
    where = ' in the way of ' + _code(before) if before else ' in the way'
    popup = _code(event.get('popup') or 'a popup')
    tried = event.get('tried') if isinstance(event.get('tried'), list) else []
    after = (', after ' + ', '.join(_step(t) for t in tried) + ' did not close it') if tried else ''
    if _dismissed(event):
        return ('Dismissed a popup' + where + ': ' + popup + ', with '
                + _step({'how': event.get('how'), 'label': event.get('label')}) + after + '.')
    if event.get('outcome') == 'lost':
        return ('Stopped on a popup' + where + ': ' + popup + '. Tried '
                + ', '.join(_step(t) for t in tried) + ', and then the element could not be '
                'looked at as before, so it was not counted as dismissed.')
    return ('Could not dismiss a popup' + where + ': ' + popup
            + ('. Tried ' + ', '.join(_step(t) for t in tried) + ', which did not close it.'
               if tried else '.'))


def _flow_section(report, events, flow_counts):
    out = ['<section aria-labelledby="flow"><h2 id="flow">Flow events</h2>']
    if not report['journal']['exists']:
        out.append('<p class="empty">There is no <code>journal.jsonl</code> next to the store, so '
                   'there are no flow events to list.</p></section>')
        return out
    flow = [e for e in events if e.get('event') in _FLOW_EVENTS]
    if not flow:
        out.append('<p class="empty">No interruptions, skipped steps or page guard stops are '
                   'recorded.</p></section>')
        return out
    out.append('<p class="lead">' + _plural(flow_counts['flow.interrupt'], 'interruption') + ' handled, '
               + _plural(flow_counts['flow.skip'], 'step') + ' skipped, '
               + _plural(flow_counts['flow.page_guard'], 'heal') + ' declined by the page guard'
               + (', ' + _plural(flow_counts['flow.dismiss'], 'popup') + ' dismissed'
                  if flow_counts['flow.dismiss'] else '') + '. Newest '
               'first' + (', the last ' + str(LAST_FLOW) + ' of ' + str(len(flow))
                          if len(flow) > LAST_FLOW else '') + '.</p>')
    kinds = {'flow.interrupt': ('interruption', 'info'), 'flow.skip': ('skipped step', 'muted'),
             'flow.page_guard': ('page guard', 'warn'), 'flow.dismiss': ('popup', 'info')}
    rows = []
    for e in flow[-LAST_FLOW:][::-1]:
        label, kind = kinds[e['event']]
        if e['event'] == 'flow.dismiss' and not _dismissed(e):
            label, kind = 'popup left', 'warn'
        key = e.get('key')
        rows.append('<tr>' + _cell('Event', _badge(label, kind))
                    + _cell('When', h(_when(e.get('ts'))), 'nowrap')
                    + _cell('Key', _code(key) if key else '<span class="muted">-</span>')
                    + _cell('What happened', _flow_detail(e)) + '</tr>')
    out.extend(_table(('Event', 'When', 'Key', 'What happened'), rows, 'Flow events'))
    out.append('</section>')
    return out


def _quarantine_section(quarantines):
    out = ['<section aria-labelledby="quarantines"><h2 id="quarantines">Quarantines</h2>']
    if not quarantines:
        out.append('<p class="empty">No selector is quarantined. A heal is quarantined when the '
                   'action that used it failed.</p></section>')
        return out
    out.append('<p class="lead">mendwright does not try a quarantined selector again, and '
               'refuses a proposal that selects the same element.</p>')
    rows = []
    for q in quarantines:
        where = (_badge('in the store', 'bad') if q['in_store']
                 else _badge('journal only', 'muted'))
        rows.append('<tr>' + _cell('Key', _code(q['key']), 'k')
                    + _cell('Selector', _code(q['selector']))
                    + _cell('Source', h(q['source'] or '-'))
                    + _cell('Quarantined', h(_when(q['when'])) if q['when'] else '<span class="muted">'
                            'not in the journal</span>', 'nowrap')
                    + _cell('State', where) + '</tr>')
    out.extend(_table(('Key', 'Selector', 'Source', 'Quarantined', 'State'), rows,
                      'Quarantined selectors'))
    out.append('</section>')
    return out


# --- the page ----------------------------------------------------------------------------

_STYLE = """
:root{--bg:#f4f5f7;--surface:#fff;--text:#1a1d21;--muted:#5a6370;--border:#d8dce2;--code:#eef0f3;
--ok:#0b6b38;--ok-bg:#dff3e7;--warn:#7a4f00;--warn-bg:#fcefd0;--bad:#a8231b;--bad-bg:#fbe3e0;
--info:#1d4f98;--info-bg:#e2ecfa;--plain:#3a424d;--plain-bg:#e8ebef;--muted-bg:#eceff2}
@media (prefers-color-scheme:dark){:root{--bg:#0e1115;--surface:#161a20;--text:#e7eaee;
--muted:#97a1ae;--border:#2a313a;--code:#1f252d;--ok:#6fd49a;--ok-bg:#133522;--warn:#f1c466;
--warn-bg:#3b2d0c;--bad:#ff958d;--bad-bg:#40181a;--info:#92bcf2;--info-bg:#16293f;--plain:#c3cad3;
--plain-bg:#262d36;--muted-bg:#242a32}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,-apple-system,
"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
.page{max-width:1120px;margin:0 auto;padding:16px}
header{margin:8px 0 20px}
h1{font-size:1.6rem;line-height:1.2;margin:0 0 6px}
h2{font-size:1.15rem;margin:0 0 10px}
section{margin:0 0 28px;min-width:0}
.meta{margin:0;color:var(--muted);font-size:.9rem}
.meta code{font-size:.85em}
.lead{margin:0 0 10px;color:var(--muted);font-size:.92rem}
.empty{margin:0;padding:12px 14px;background:var(--surface);border:1px solid var(--border);
border-radius:8px;color:var(--muted)}
code{font:.86em/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
background:var(--code);padding:1px 5px;border-radius:4px;overflow-wrap:anywhere;word-break:break-word}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.num{font-size:1.7rem;font-weight:650;line-height:1.1}
.label{font-size:.88rem}
.note{font-size:.78rem;color:var(--muted);margin-top:2px}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);
border-radius:10px;overflow:hidden}
th,td{text-align:left;vertical-align:top;padding:10px 12px;border-bottom:1px solid var(--border)}
tbody tr:last-child td{border-bottom:0}
th{font-size:.74rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
background:var(--muted-bg)}
td{min-width:0;overflow-wrap:break-word}
td.keycell,td.k{min-width:9.5rem}
.nowrap{white-space:nowrap}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.sub{font-size:.8rem;color:var(--muted)}
.muted{color:var(--muted)}
.line{margin:4px 0 2px}
.cands{list-style:none;margin:0;padding:0}
.cands li{padding:6px 0;border-bottom:1px dashed var(--border)}
.cands li:first-child{padding-top:0}
.cands li:last-child{border-bottom:0;padding-bottom:0}
.badge{display:inline-block;font-size:.74rem;font-weight:600;line-height:1.5;padding:0 8px;
border-radius:999px;margin:0 2px 4px 0;white-space:nowrap}
.badge.ok{color:var(--ok);background:var(--ok-bg)}
.badge.warn{color:var(--warn);background:var(--warn-bg)}
.badge.bad{color:var(--bad);background:var(--bad-bg)}
.badge.info{color:var(--info);background:var(--info-bg)}
.badge.plain{color:var(--plain);background:var(--plain-bg)}
.badge.muted{color:var(--muted);background:var(--muted-bg)}
.chg{margin:0 0 4px}
.lbl{display:inline-block;min-width:2.4em;margin-right:6px;font-size:.74rem;font-weight:600;
text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
footer{margin:8px 0 0;color:var(--muted);font-size:.82rem}
@media (max-width:760px){
.page{padding:16px}
h1{font-size:1.35rem}
thead{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
table,tbody,tr,td{display:block;width:100%}
table{background:transparent;border:0;border-radius:0}
tr{background:var(--surface);border:1px solid var(--border);border-radius:10px;margin:0 0 10px;
padding:4px 0}
td,tbody tr:last-child td{border:0;padding:6px 14px}
td::before{content:attr(data-label);display:block;font-size:.7rem;font-weight:600;
text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
td.keycell::before{display:none}
td.keycell,td.k{min-width:0}
.nowrap{white-space:normal}
}
"""


def render_html(report, events, *, version=None):
    """The page for ``report`` (see mendwright.cli.collect_report) and the journal's
    ``events`` (oldest first; an empty list when there is no journal). Returns ASCII text."""
    events = [e for e in events or () if isinstance(e, dict)]
    records = _heal_records(events)
    quarantines = _quarantines(report, events)
    flow_counts = {name: sum(1 for e in events if e.get('event') == name) for name in _FLOW_EVENTS}
    flow_counts['flow.dismiss'] = sum(1 for e in events
                                      if e.get('event') == 'flow.dismiss' and _dismissed(e))
    journal = report['journal']
    stamps = [e['ts'] for e in events if isinstance(e.get('ts'), str)]
    if journal['exists']:
        seen = (str(len(events)) + ' events, the latest at ' + _when(max(stamps)) if stamps
                else str(len(events)) + ' events')
        journal_note = 'Journal ' + _code(journal['path']) + ' (' + h(seen) + ')'
    else:
        journal_note = 'No <code>journal.jsonl</code> next to the store'

    body = ['<div class="page">', '<header>', '<h1>mendwright heal report</h1>',
            '<p class="meta">Store ' + _code(report['store']) + '</p>',
            '<p class="meta">' + journal_note + '</p></header>', '<main>']
    body.extend(_summary(report, records, quarantines, flow_counts))
    body.extend(_keys_section(report))
    body.extend(_heals_section(report, records))
    body.extend(_flow_section(report, events, flow_counts))
    body.extend(_quarantine_section(quarantines))
    body.append('</main>')
    body.append('<footer>Made by mendwright' + (' ' + h(version) if version else '')
                + ' from the store and journal above. This page has no scripts and loads nothing '
                'from the network. Selectors and reasons are shown as recorded and may quote your '
                'pages, so check who can open the file before you share it.</footer></div>')
    doc = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
           '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
           '<meta name="color-scheme" content="light dark">\n'
           '<title>mendwright heal report</title>\n<style>' + _STYLE + '</style>\n</head>\n'
           '<body>\n' + '\n'.join(body) + '\n</body>\n</html>\n')
    return doc.encode('ascii', 'xmlcharrefreplace').decode('ascii')
