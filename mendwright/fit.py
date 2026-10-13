"""Fit a heal bundle into the model's prompt.

The prompt (mendwright.prompt.build_prompt) keeps at most 60,000 characters of the page's
HTML and of the element inventory and cuts off the rest. On a long page that cut dropped
whatever came last, which could be the element the step needs. fit_bundle() runs before
a provider gets the bundle and makes both parts fit, so the cut does not happen:

1. Text and attribute values over 200 characters are shortened, and style, srcset and
   sizes attributes are dropped.
2. If the HTML is still too long, subtrees that look unrelated to the step are left out,
   largest first, until it fits. Each run of them becomes a comment that says how many
   elements were left out. What counts as related narrows in steps, and only as far as
   needed. It starts with every element that shares a word with the step (its key, old
   selectors, expected text, intent and saved fingerprint), every element of the step's
   role, labels and headings, and all other controls, then drops the other controls, then
   the labels and headings, then the role. A related element keeps its ancestors and
   everything inside it.
3. Only if the page is still too long after that is the end cut off, as before.

When elements were left out, a comment at the top of the HTML says so. The heal that
follows rejects XPaths with a position in them ([2], last(), position()), because positions
in the fitted page no longer match the live one (client.apply(..., positional=False)).

The inventory keeps its rows whole. Long strings in it are shortened, and if it is still
too long, rows that share no word with the step and are not of its role are dropped from
the end first.

A provider that reads the page itself instead of a prompt (LocalModelProvider, which sets
wants_full_html = True) gets the bundle unchanged. Only what the model reads changes: the
gate still checks every proposal against the full captured HTML.
"""

import json
import re

import lxml.html
from lxml import etree

from . import fingerprint as _fingerprint
from .candidates import step_words, tokens
from .prompt import _MAX
from .validation import _role_of

BUDGET = _MAX
TEXT_LIMIT = 200
TEXT_KEEP = 160
ATTR_LIMIT = 200
ATTR_KEEP = 120
_DROP_ATTRS = frozenset({'style', 'srcset', 'sizes'})
_LABELS = frozenset({'label', 'legend', 'caption', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'})
_CONTROLS = frozenset({'a', 'button', 'input', 'select', 'textarea', 'option', 'summary'})

# How related an element is to the step. Each pass keeps the elements at or above a rank.
_HIT, _ROLE, _LABEL, _CONTROL, _OTHER = 4, 3, 2, 1, 0
_PASSES = (_CONTROL, _LABEL, _ROLE, _HIT)

NOTE = ('<!-- mendwright: this page is longer than the prompt allows, so parts that look '
        'unrelated to the step were left out, each marked by a comment like this one. '
        'Positions and counts here do not match the live page: do not use positional '
        'predicates such as [2], last() or position(). -->')
_MARKER = re.compile(r'^ mendwright: (\d+) elements? left out here $')
_MARKER_SIZE = len('<!-- mendwright: 100 elements left out here -->')
# A predicate that is only a number ([2], [ 2 ], [(2)], [2.0], [1+1]) or only number(...)
# is a position, and so is any use of position() or last(). A comparison such as
# [number(@value)>0] or [@maxlength=5] is not.
_POSITIONAL = re.compile(r'\[[\s\d.+\-*()]*\d[\s\d.+\-*()]*\]|\[\s*number\s*\([^\]]*\)\s*\]'
                         r'|\bposition\s*\(|\blast\s*\(')
# What capture.build_bundle() puts in place of a URL or a long number. They are not words
# of the page or of the step.
_PLACEHOLDER = re.compile(r'\[URL\]|\[redacted(?: number)?\]')
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _words(text):
    return tokens(_PLACEHOLDER.sub(' ', text)) if text else []


def has_position(xpath):
    """True when an XPath depends on an element's position: a predicate that is a number,
    such as [2], or uses position() or last(). Quoted strings are ignored."""
    return bool(_POSITIONAL.search(_QUOTED.sub("''", xpath or '')))


def _clip(text, limit=TEXT_LIMIT, keep=TEXT_KEEP):
    if isinstance(text, str) and len(text) > limit:
        return text[:keep] + '...'
    return text


def _shorten(root):
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        el.text = _clip(el.text)
        el.tail = _clip(el.tail)
        for name in list(el.attrib):
            if name.lower() in _DROP_ATTRS:
                del el.attrib[name]
            else:
                value = el.attrib[name]
                if len(value) > ATTR_LIMIT:
                    el.attrib[name] = value[:ATTR_KEEP] + '...'


def _rank(el, words, role):
    """How related one element is to the step (see _HIT and the others). Its own text is
    every text node directly in it, including what follows a child, such as the label
    after an icon or a checkbox."""
    own = _words(' '.join(el.xpath('text()')))
    for value in el.attrib.values():
        own.extend(_words(value))
    if words and any(word in words for word in own):
        return _HIT
    if role and _role_of(el) == role:
        return _ROLE
    tag = el.tag.lower()
    if tag in _LABELS:
        return _LABEL
    if tag in _CONTROLS or el.get('role'):
        return _CONTROL
    return _OTHER


def _removable(root, keep):
    """The largest subtrees that hold no kept element: children of the root, or of an
    ancestor of a kept element, that are neither kept nor ancestors of one."""
    above = set()
    for el in keep:
        node = el
        while node is not None and node not in above:
            above.add(node)
            node = node.getparent()
    out, todo = [], [root]
    while todo:
        for child in todo.pop():
            if not isinstance(child.tag, str) or child in keep:
                continue
            if child in above:
                todo.append(child)
            else:
                out.append(child)
    return out


def _html(el, tail=False):
    return lxml.html.tostring(el, encoding='unicode', with_tail=tail)


def _marker_count(node):
    """The number of elements a marker comment stands for, or None for any other node."""
    if node is None or node.tag is not etree.Comment:
        return None
    match = _MARKER.match(node.text or '')
    return int(match.group(1)) if match else None


def _marker(n):
    return etree.Comment(' mendwright: ' + str(n) + ' element' + ('' if n == 1 else 's')
                         + ' left out here ')


def _leave_out(elements):
    """Replace each element by a marker comment, merging it with a marker just before or
    after it (only whitespace between them), and counting the elements behind any marker
    of an earlier pass inside it. Returns how many elements were left out now."""
    total = 0
    for el in elements:
        parent = el.getparent()
        if parent is None:
            continue
        own = 0
        shown = 1
        for node in el.iterdescendants():
            if isinstance(node.tag, str):
                own += 1
            else:
                shown += _marker_count(node) or 0
        total += 1 + own
        n = shown + own
        tail = el.tail
        prev, after = el.getprevious(), el.getnext()
        before = _marker_count(prev) if prev is not None and not (prev.tail or '').strip() else None
        behind = _marker_count(after) if after is not None and not (tail or '').strip() else None
        if before is not None:
            n += before + (behind or 0)
            prev.text = _marker(n).text
            if behind is not None:
                prev.tail = after.tail
                parent.remove(after)
            else:
                prev.tail = tail
            parent.remove(el)
        elif behind is not None:
            after.text = _marker(n + behind).text
            parent.remove(el)
        else:
            marker = _marker(n)
            parent.replace(el, marker)
            marker.tail = tail
    return total


def fit_html(html, words=(), role=None, budget=BUDGET):
    """(html, left_out): ``html`` made to fit in ``budget`` characters (see the module
    docstring), and how many elements were left out (0 when only text was shortened or
    nothing changed). HTML that already fits is returned as it is."""
    if not isinstance(html, str) or len(html) <= budget:
        return html, 0
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return html[:budget], 0
    if not isinstance(root.tag, str):
        return html[:budget], 0
    _shorten(root)
    out = _html(root)
    if len(out) <= budget:
        return out, 0
    words = set(words or ())
    ranks = {el: _rank(el, words, role) for el in root.iter() if isinstance(el.tag, str)}
    left_out = 0
    for floor in _PASSES:
        keep = {el for el, rank in ranks.items() if rank >= floor}
        sized = sorted(((len(_html(el)), el) for el in _removable(root, keep)),
                       key=lambda pair: pair[0], reverse=True)
        removable = {el for _size, el in sized}
        total = len(out) + len(NOTE)
        chosen, small = [], []
        for size, el in sized:
            if total <= budget:
                break
            if size <= _MARKER_SIZE:
                # Alone, its marker would be as long as it is. Next to another subtree that
                # goes, or to a marker, it shares that marker, so it is kept for last.
                prev, after = el.getprevious(), el.getnext()
                if (prev in removable or after in removable or _marker_count(prev) is not None
                        or _marker_count(after) is not None):
                    small.append((size, el))
                continue
            chosen.append(el)
            total -= size - _MARKER_SIZE
        for size, el in small:
            if total <= budget:
                break
            chosen.append(el)
            total -= size
        if chosen:   # in document order, so neighbours share one marker
            picked = set(chosen)
            chosen = [el for el in root.iter() if el in picked]
        left_out += _leave_out(chosen)
        out = _html(root)
        if len(out) + len(NOTE) <= budget:
            return NOTE + out, left_out
    return (NOTE + out)[:budget], max(left_out, 1)


def _clip_row(row):
    if isinstance(row, dict):
        return {k: _clip(v, ATTR_LIMIT, ATTR_KEEP) for k, v in row.items()}
    return row


def fit_inventory(rows, words=(), role=None, budget=BUDGET):
    """The inventory with every row whole, fitted to ``budget`` characters of JSON: long
    strings shortened, then unrelated rows dropped from the end, then related ones."""
    if not isinstance(rows, list):
        return rows
    try:
        size = len(json.dumps(rows))
    except (TypeError, ValueError):
        return rows   # not JSON: the provider says so when it builds the prompt
    if size <= budget:
        return rows
    rows = [_clip_row(row) for row in rows]
    words = set(words or ())

    def related(row):
        if not isinstance(row, dict):
            return False
        if role and row.get('role') == role:
            return True
        return any(w in words for v in row.values() if isinstance(v, str) for w in _words(v))

    for drop_related in (False, True):
        for i in range(len(rows) - 1, -1, -1):
            if len(json.dumps(rows)) <= budget:
                return rows
            if drop_related or not related(rows[i]):
                del rows[i]
    return rows


def fit_bundle(bundle, fingerprint=None, budget=BUDGET):
    """(bundle, left_out): the bundle with its HTML and inventory fitted to the prompt,
    and how many elements of the HTML were left out. ``fingerprint`` is the key's saved
    fingerprint; its words help decide what is related to the step even when it is not
    sent. A bundle that already fits is returned as the same object."""
    hints = dict(bundle)
    if _fingerprint.valid(fingerprint):
        hints['fingerprint'] = fingerprint
    words = step_words(hints)
    role = bundle.get('expectedRole')
    role = role if isinstance(role, str) and role != 'unknown' else None
    html, left_out = fit_html(bundle.get('html'), words, role, budget)
    inventory = fit_inventory(bundle.get('inventory'), words, role, budget)
    if html is bundle.get('html') and inventory is bundle.get('inventory'):
        return bundle, 0
    return dict(bundle, html=html, inventory=inventory), left_out
