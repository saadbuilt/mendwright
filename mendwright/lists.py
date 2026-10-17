"""Lists: what the items of a list looked like, for heal.locator_all(), and where to look for
a list whose selector broke.

Each time a list lookup (Healer.locator_all()) matches two or more elements, mendwright
records a list fingerprint in one round trip (LIST_JS): the fingerprints of up to three of
the items (the first, the middle and the last, see mendwright.fingerprint), the tags inside
each one and of its children (its shape), how many items there were, their nearest common
container (its tag, id, test id, aria-label, classes and role) and how many levels below it
they sit. A list that matched fewer than two elements is not recorded: nothing repeats in
it. A lookup with nonempty=True records it with LIST_PLACE_JS instead, which also reads the
list's label (the aria-labelledby or aria-label around it, or else the nearest heading
before it) and where it sits: short hashes of its container and the elements around it, and
of the page's other elements and labels. Once a key's list fingerprint holds those, every
later recording of it reads them too.

The list fingerprint says that the list had items, so a lookup of it that matches nothing
waits and raises instead of giving [] at once. With nonempty=True the lookup then looks for
the list on the page with find(), and mendwright never uses what it finds: it is saved as a
proposed list heal for a person to check (see Healer.locator_all and mendwright lists),
because an emptied list, whose items can show in another list on the page, such as a cart's
in Saved for later, can't be told from a list whose selector broke.

find() lists the repeated groups of the captured page (groups()): two or more elements of
the items' role that sit the same number of levels (one to three) below one container, with
the same tags on the way down, and, for each class name two of them share, those that have
it. Each group is scored against the list fingerprint, from 0 to 1:

- how much each member looks like one of the recorded items (fingerprint.score), on
  average (a half of the score)
- how alike the members' shapes are to the recorded ones (a quarter), left out when the
  members and the recorded items have nothing inside them, which says nothing
- how alike the group's container is to the recorded one (0.15)
- how close the number of members is to the recorded count (0.10)

and it finds a group only when all of these hold: the best group scores at least
THRESHOLD, no group that shares less than half of its members with it comes within MARGIN,
every member it looked at has a shape at least SHAPE_MIN alike, two of the recorded items
(both, for a list recorded with two) are among its members, it is inside the recorded
container and the recorded items' ancestors with an id when those are still on the page, it
is not another list, and an XPath without positions matches its members and nothing else.

A recorded item is among the members when one of them shares an id, test id, name, link,
label or text with it (fingerprint.score's strong) that not every recorded item has, and
does not share one with every recorded item: what all the items have, such as a test id
each card has, says what kind of list it is, not which items it holds, and where a group
sits says nothing about that either. A group of placeholders is neither the answer nor a
rival: one whose members have no text where the recorded items had text, such as the
skeleton cards of a list that is still loading, or that is marked aria-busy="true".

A group that is another list (_elsewhere, _Places) is left out before anything is scored,
so it is no rival: one that, going out from it past markup that was not there, sits first in
an element that was on the page elsewhere, outside the list, when the list was recorded, or
one under another label than the list's when that label was on the page then or the list's
own label still is. A group found is refused as well when its XPath picks it by a word for a
state the recorded list did not have (state_word(): completed, done, saved and a few more).

What find() gives then goes through the list gate (client.list_gate): two or more elements
of the role on the captured page, none inside another, none of a rejected or quarantined
selector of the key, and, on the live page, two or more elements of the role with at least
one visible. proposal_evidence() and proposal_id() hold what a person checks before
accepting it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import lxml.html

from . import fingerprint as _fp
from .candidates import TEST_ATTRS, _class_ok, text_of
from .css import _IDENT, _anchor_parts, _class
from .fit import has_position
from .validation import Result, _is_brittle, _role_of

# Version of the stored shape. A list fingerprint with another version is ignored.
VERSION = 1

THRESHOLD = 0.65
MARGIN = 0.10
SHAPE_MIN = 0.5

SAMPLES = 3          # items recorded per list
NEEDED = 2           # recorded items that must be among a group's members for find() to take it
MAX_DEPTH = 3        # levels between a group's container and its members
SHAPE_TAGS = 40      # tags kept in a shape
SHAPE_LIMIT = 500    # descendants counted for a shape
SCORED = 25          # members of a group compared with the recorded items
IDENTIFIED = 200     # members of the best group looked through for the recorded items,
                     # spread over it, the first and the last included
RANKED = 30          # groups scored in full, of those that score best in a quick look
ITEM_TEXT = 100      # how much of an item's text a group's summary shows
ITEMS = 200          # elements ITEMS_JS reads for the live gate

CONTAINER_ATTRS = ('id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa')
_CONTAINER_TESTS = ('data-testid', 'data-test', 'data-qa')
_UNRENDERED = ('template', 'noscript', 'script', 'style')

# Where the list was: when the list fingerprint is recorded, the elements of the page outside
# the list's items are read too, at most PLACE_SCAN of them, and kept as short hashes (at most
# PLACE_KEPT), with those of the list's container and the elements around it apart. Text-level
# and empty elements, which never hold a list, are left out (_LEAF), and so is what a browser
# does not render as elements or what holds no HTML (_PRUNED).
PLACE_SCAN = 10000
PLACE_KEPT = 1000
PLACE_LABELS = 200
_LEAF = frozenset((
    'a', 'abbr', 'area', 'audio', 'b', 'base', 'bdi', 'bdo', 'br', 'button', 'canvas', 'cite',
    'code', 'col', 'data', 'dfn', 'em', 'embed', 'hr', 'i', 'iframe', 'img', 'input', 'kbd',
    'label', 'link', 'map', 'mark', 'meta', 'meter', 'object', 'optgroup', 'option', 'output',
    'param', 'picture', 'progress', 'q', 's', 'samp', 'select', 'small', 'source', 'span',
    'strong', 'sub', 'sup', 'textarea', 'time', 'track', 'u', 'var', 'video', 'wbr'))
_PRUNED = ('template', 'noscript', 'script', 'style', 'svg', 'math')
_LEAF_JS = '[' + ', '.join("'" + t + "'" for t in sorted(_LEAF)) + ']'

# One round trip: Playwright's locator.evaluate_all() passes the matches as an array, and
# Selenium runs it with find_elements()' result. null unless two or more elements matched.
LIST_JS = r"""(els) => {
  if (!els || els.length < 2) return null;
  const one = """ + _fp.FINGERPRINT_JS + r""";
  const shape = (el) => {
    const out = {};
    for (const c of el.children) {
      const t = '>' + c.tagName.toLowerCase();
      out[t] = (out[t] || 0) + 1;
    }
    let n = 0;
    for (const d of el.querySelectorAll('*')) {
      const t = d.tagName.toLowerCase();
      out[t] = (out[t] || 0) + 1;
      if (++n >= 500) break;
    }
    return out;
  };
  const last = els.length - 1;
  const picks = [...new Set([0, Math.floor(last / 2), last])];
  const items = picks.map((i) => ({fp: one([els[i]]), shape: shape(els[i])}));
  let box = els[0].parentElement;
  while (box && !els.every((e) => box.contains(e))) box = box.parentElement;
  let depth = 0;
  for (let n = els[0]; n && n !== box; n = n.parentElement) depth++;
  let container = null;
  if (box && !/^(body|html)$/i.test(box.tagName)) {
    const attrs = {};
    for (const a of ['id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa']) {
      const v = box.getAttribute(a);
      if (v !== null) attrs[a] = v;
    }
    container = {tag: box.tagName.toLowerCase(), attrs: attrs};
  }
  return {count: els.length, depth: depth, items: items, container: container};
}"""

# LIST_JS, plus the list's label and where it sits (see raw_of_elements(place=True)), for
# a lookup with nonempty=True and for every later recording of a key whose list
# fingerprint holds them. Also one round trip, and null for fewer than two elements.
LIST_PLACE_JS = r"""(els) => {
  if (!els || els.length < 2) return null;
  const one = """ + _fp.FINGERPRINT_JS + r""";
  const shape = (el) => {
    const out = {};
    for (const c of el.children) {
      const t = '>' + c.tagName.toLowerCase();
      out[t] = (out[t] || 0) + 1;
    }
    let n = 0;
    for (const d of el.querySelectorAll('*')) {
      const t = d.tagName.toLowerCase();
      out[t] = (out[t] || 0) + 1;
      if (++n >= 500) break;
    }
    return out;
  };
  const last = els.length - 1;
  const picks = [...new Set([0, Math.floor(last / 2), last])];
  const items = picks.map((i) => ({fp: one([els[i]]), shape: shape(els[i])}));
  let box = els[0].parentElement;
  while (box && !els.every((e) => box.contains(e))) box = box.parentElement;
  let depth = 0;
  for (let n = els[0]; n && n !== box; n = n.parentElement) depth++;
  let container = null;
  if (box && !/^(body|html)$/i.test(box.tagName)) {
    const attrs = {};
    for (const a of ['id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa']) {
      const v = box.getAttribute(a);
      if (v !== null) attrs[a] = v;
    }
    container = {tag: box.tagName.toLowerCase(), attrs: attrs};
  }
  const name = (e) => e.tagName.toLowerCase();
  const unread = /^(template|noscript|script|style)$/;
  const heading = (e) => !unread.test(name(e)) && (/^h[1-6]$/.test(name(e))
    || (e.getAttribute('role') || '').trim().toLowerCase() === 'heading');
  const headingIn = (e) => {
    if (unread.test(name(e))) return null;
    if (heading(e)) return e;
    for (let c = e.lastElementChild; c; c = c.previousElementSibling) if (heading(c)) return c;
    return null;
  };
  const named = (e) => {
    const ids = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const text = ids.map((i) => e.ownerDocument.getElementById(i)).filter(Boolean)
      .map((t) => t.textContent).join(' ');
    if (text.trim()) return text;
    const own = e.getAttribute('aria-label');
    return own && own.trim() ? own : null;
  };
  let label = '';
  if (box) {
    let node = els[0];
    while (node.parentElement && node.parentElement !== box) node = node.parentElement;
    let holder = box;
    search: while (holder) {
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        const h = headingIn(s);
        if (h) { label = h.textContent; break search; }
      }
      if (/^(body|html)$/i.test(holder.tagName)) break;
      const own = named(holder);
      if (own) { label = own; break; }
      node = holder;
      holder = holder.parentElement;
    }
  }
  const test = (e) => {
    for (const a of ['data-testid', 'data-test', 'data-qa']) {
      const v = e.getAttribute(a);
      if (v !== null) return v;
    }
    return '';
  };
  const raw = (e) => {
    const p = e.parentElement;
    return [name(e), e.getAttribute('id') || '', test(e), e.getAttribute('class') || '',
            p ? name(p) : '', p ? p.getAttribute('id') || '' : '', p ? test(p) : '',
            p ? p.getAttribute('class') || '' : ''];
  };
  const chain = [], mine = new Set();
  for (let n = box; n && !/^(body|html)$/i.test(n.tagName); n = n.parentElement) {
    chain.push(raw(n));
    mine.add(n);
  }
  for (const e of els) for (let n = e.parentElement; n && n !== box; n = n.parentElement) mine.add(n);
  const prune = /^(template|noscript|script|style|svg|math)$/;
  const leaf = new Set(""" + _LEAF_JS + r""");
  const skip = new Set(els), seen = new Set(), others = [], said = new Set();
  const body = els[0].ownerDocument.body;
  const stack = body ? Array.from(body.children).reverse() : [];
  let read = 0, cut = false;
  while (stack.length) {
    const e = stack.pop();
    if (skip.has(e) || prune.test(name(e))) continue;
    if (heading(e)) said.add(e.textContent);
    if (!leaf.has(name(e))) {
      const own = named(e);
      if (own) said.add(own);
    }
    if (!leaf.has(name(e)) && !mine.has(e)) {
      if (++read > """ + str(PLACE_SCAN) + r""") { cut = true; break; }
      const r = raw(e), k = r.join('\u0001');
      if (!seen.has(k)) { seen.add(k); others.push(r); }
    }
    for (let i = e.children.length - 1; i >= 0; i--) stack.push(e.children[i]);
  }
  const labels = Array.from(said).slice(0, """ + str(PLACE_LABELS) + r""");
  return {count: els.length, depth: depth, items: items, container: container, label: label,
          place: {chain: chain, others: others, labels: labels, cut: cut}};
}"""

# For the live gate: what each match is, and whether it is visible as Playwright counts it (a
# box with a size, and not visibility: hidden). At most 200.
ITEMS_JS = r"""(els) => els.slice(0, 200).map((e) => ({
  tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '',
  type: e.getAttribute('type') || '',
  visible: e.getClientRects().length > 0
    && (e.ownerDocument.defaultView || window).getComputedStyle(e).visibility !== 'hidden'}))"""


# --- the raw read, from lxml --------------------------------------------------------------

def _elements(node):
    """node's descendant elements in document order, without what a browser does not render
    as elements (the inside of <template>, <noscript>, <script>, <style>)."""
    stack = list(reversed([c for c in node if isinstance(c.tag, str)]))
    while stack:
        el = stack.pop()
        yield el
        if el.tag.lower() not in _UNRENDERED:
            stack.extend(reversed([c for c in el if isinstance(c.tag, str)]))


def shape_of(el):
    """What LIST_JS's shape() returns for an lxml element: {'>tag': how many} of its
    children, and {tag: how many} of its first SHAPE_LIMIT descendants."""
    out, n = {}, 0
    for child in el:
        if isinstance(child.tag, str):
            tag = '>' + child.tag.lower()
            out[tag] = out.get(tag, 0) + 1
    for node in _elements(el):
        tag = node.tag.lower()
        out[tag] = out.get(tag, 0) + 1
        n += 1
        if n >= SHAPE_LIMIT:
            break
    return out


def _common_container(els):
    """The nearest element that holds all of els, or None."""
    others = [set(e.iterancestors()) for e in els[1:]]
    for anc in els[0].iterancestors():
        if all(anc in s for s in others):
            return anc
    return None


def raw_of_elements(els, labels=None, *, place=False):
    """What LIST_JS returns for lxml elements ``els`` (in document order), computed with
    lxml, or None for fewer than two. With place=True, what LIST_PLACE_JS returns: the
    list's label and where it sits as well."""
    els = list(els)
    if len(els) < 2:
        return None
    last = len(els) - 1
    picks = sorted({0, last // 2, last})
    items = [{'fp': _fp.raw_of_element(els[i], labels), 'shape': shape_of(els[i])}
             for i in picks]
    box = _common_container(els)
    depth, node = 0, els[0]
    while node is not None and node is not box:
        depth += 1
        node = node.getparent()
    container = None
    if box is not None and box.tag.lower() not in ('body', 'html'):
        container = {'tag': box.tag.lower(),
                     'attrs': {a: box.get(a) for a in CONTAINER_ATTRS if box.get(a) is not None}}
    out = {'count': len(els), 'depth': depth, 'items': items, 'container': container}
    if place:
        out['label'] = _label_text(els[0], box) if box is not None else ''
        out['place'] = _place_raw(els, box)
    return out


def _hidden(el):
    """True when el, or an element around it, is hidden: the hidden attribute, or a style
    with display: none or visibility: hidden."""
    while el is not None and isinstance(el.tag, str):
        style = (el.get('style') or '').replace(' ', '').lower()
        if (el.get('hidden') is not None or 'display:none' in style
                or 'visibility:hidden' in style):
            return True
        el = el.getparent()
    return False


def items_of_elements(els):
    """What ITEMS_JS returns for lxml elements, computed with lxml: {'tag', 'role', 'type',
    'visible'} for each of the first ITEMS of them, role and type as written, and visible
    False for an element hidden by itself or by one around it."""
    return [{'tag': el.tag.lower(), 'role': el.get('role') or '', 'type': el.get('type') or '',
             'visible': not _hidden(el)} for el in list(els)[:ITEMS]]



def _children(el):
    return [c for c in el if isinstance(c.tag, str)]


def _heading(el):
    tag = el.tag.lower()
    return tag not in _UNRENDERED and (
        (len(tag) == 2 and tag[0] == 'h' and tag[1] in '123456')
        or (el.get('role') or '').strip().lower() == 'heading')


def _heading_in(el):
    """el when it is a heading (h1 to h6, or role="heading"), else the last of its children
    that is one, else None."""
    if el.tag.lower() in _UNRENDERED:
        return None
    if _heading(el):
        return el
    return next((c for c in reversed(_children(el)) if _heading(c)), None)


def _named(el):
    """The text of el's aria-labelledby, or its aria-label, or None."""
    ids = (el.get('aria-labelledby') or '').split()
    if ids:
        root = el.getroottree().getroot()
        found = [root.xpath('//*[@id=$i]', i=i) for i in ids]
        text = ' '.join(f[0].text_content() for f in found if f)
        if text.strip():
            return text
    own = el.get('aria-label')
    return own if own and own.strip() else None


def _label_text(first, box):
    """What LIST_JS reads as the label of a list whose first element is first and whose
    container is box: going out from the items, the nearest heading before them (one that
    is, or is a child of, an element before them, inside box or around it), or the
    aria-labelledby or aria-label of box or an element around it, whichever comes first.
    '' when there is none."""
    node = first
    while node.getparent() is not None and node.getparent() is not box:
        node = node.getparent()
    holder = box
    while holder is not None:
        for sibling in node.itersiblings(preceding=True):
            if isinstance(sibling.tag, str):
                found = _heading_in(sibling)
                if found is not None:
                    return found.text_content()
        if holder.tag.lower() in ('body', 'html'):
            break
        own = _named(holder)
        if own:
            return own
        node, holder = holder, holder.getparent()
    return ''


def _test_raw(el):
    return next((el.get(a) for a in _CONTAINER_TESTS if el.get(a) is not None), '')


def _node_raw(el):
    """[tag, id, test id, class] of el and of its parent, as LIST_JS's raw() reads them."""
    parent = el.getparent()
    own = [el.tag.lower(), el.get('id') or '', _test_raw(el), el.get('class') or '']
    if parent is None or not isinstance(parent.tag, str):
        return own + ['', '', '', '']
    return own + [parent.tag.lower(), parent.get('id') or '', _test_raw(parent),
                  parent.get('class') or '']


def _place_raw(els, box):
    """What LIST_JS reads of where the list is: {'chain': its container and the elements
    around it, 'others': every other element of the page outside the items and what holds
    them inside the container (at most PLACE_SCAN, each once), each as _node_raw() gives it,
    'labels': the text of the headings and the aria-labelledby or aria-label of the other
    elements of the page outside the items (at most PLACE_LABELS, each once), 'cut': whether
    there were more elements}."""
    chain, mine = [], set()
    node = box
    while node is not None and isinstance(node.tag, str) and node.tag.lower() not in ('body', 'html'):
        chain.append(_node_raw(node))
        mine.add(node)
        node = node.getparent()
    for el in els:             # what sits between an item and the container is the list's too
        node = el.getparent()
        while node is not None and node is not box:
            mine.add(node)
            node = node.getparent()
    root = els[0].getroottree().getroot()
    body = root.find('body') if root.tag.lower() == 'html' else None
    skip, seen, others, said = set(els), set(), [], {}
    stack = list(reversed(_children(body if body is not None else root)))
    read, cut = 0, False
    while stack:
        el = stack.pop()
        tag = el.tag.lower()
        if el in skip or tag in _PRUNED:
            continue
        if _heading(el):
            said.setdefault(el.text_content(), None)
        if tag not in _LEAF:
            own = _named(el)
            if own:
                said.setdefault(own, None)
        if tag not in _LEAF and el not in mine:
            read += 1
            if read > PLACE_SCAN:
                cut = True
                break
            raw = _node_raw(el)
            key = '\x01'.join(raw)
            if key not in seen:
                seen.add(key)
                others.append(raw)
        stack.extend(reversed(_children(el)))
    return {'chain': chain, 'others': others, 'labels': list(said)[:PLACE_LABELS], 'cut': cut}


# --- normalizing ----------------------------------------------------------------------------

def _shape(raw):
    """A shape as stored: {tag: count} for at most SHAPE_TAGS tags, the most common first."""
    if not isinstance(raw, dict):
        return {}
    pairs = [(t.strip().lower(), int(n)) for t, n in raw.items()
             if isinstance(t, str) and t.strip() and isinstance(n, (int, float))
             and not isinstance(n, bool) and n > 0]
    pairs.sort(key=lambda p: (-p[1], p[0]))
    return dict(pairs[:SHAPE_TAGS])


def _node(raw):
    """A container as stored: its tag, and its id, test id, aria-label, stable classes and
    role when it has them. None for anything else."""
    if not isinstance(raw, dict) or not isinstance(raw.get('tag'), str) or not raw['tag'].strip():
        return None
    attrs = raw.get('attrs') if isinstance(raw.get('attrs'), dict) else {}
    node = {'tag': raw['tag'].strip().lower()}
    for name, field_name in (('id', 'id'), ('aria-label', 'aria')):
        value = _fp._norm(attrs.get(name))[:_fp.VALUE_LIMIT]
        if value:
            node[field_name] = value
    for name in ('data-testid', 'data-test', 'data-qa'):
        value = _fp._norm(attrs.get(name))[:_fp.VALUE_LIMIT]
        if value:
            node['test'] = value
            break
    classes = _fp.stable_classes(attrs.get('class'))
    if classes:
        node['classes'] = classes
    role = _fp._norm(attrs.get('role')).lower()
    if role:
        node['role'] = role
    return node


def from_raw(raw):
    """A stored list fingerprint from LIST_JS's result (or raw_of_elements()'), or None when
    ``raw`` is not that shape or counts fewer than two items."""
    if not isinstance(raw, dict):
        return None
    count = raw.get('count')
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        return None
    items = []
    for item in (raw.get('items') or [])[:SAMPLES]:
        if not isinstance(item, dict):
            continue
        fp = _fp.from_raw(item.get('fp'))
        if fp is not None:
            fp['shape'] = _shape(item.get('shape'))
            items.append(fp)
    if not items:
        return None
    depth = raw.get('depth')
    out = {'v': VERSION, 'count': count,
           'depth': depth if isinstance(depth, int) and not isinstance(depth, bool)
           and 0 < depth <= 50 else 1, 'items': items}
    container = _node(raw.get('container'))
    if container:
        out['container'] = container
    if isinstance(raw.get('label'), str):
        out['label'] = _fp._text(raw['label'])
    place = _place(raw.get('place'))
    if place is not None:
        out['place'] = place
    return out


def _sig(raw):
    """A short hash of an element as _node_raw() reads it: its tag, id (unless a framework
    generated it), test id and stable classes, and its parent's. Two elements with the same
    hash look the same to a list fingerprint, where they sit included."""
    parts = []
    for tag, ident, test, classes in (raw[:4], raw[4:8]):
        ident = _fp._norm(ident)[:_fp.VALUE_LIMIT]
        parts.append(tag.strip().lower() + '#' + ('' if _fp.generated_id(ident) else ident)
                     + '[' + _fp._norm(test)[:_fp.VALUE_LIMIT] + ']'
                     + '.'.join(sorted(_fp.stable_classes(classes))))
    return _hash('<'.join(parts))


def _hash(text):
    return hashlib.blake2s(text.encode('utf-8'), digest_size=4).hexdigest()


def _label_key(text):
    """A label as two are compared: lower case, without digits, as a count in a heading such
    as "Cart (3)" changes with the list."""
    return ' '.join(''.join(c for c in text.lower() if not c.isdigit()).split())


def _raws(value):
    if not isinstance(value, list):
        return None
    out = []
    for raw in value:
        if not (isinstance(raw, list) and len(raw) == 8 and all(isinstance(v, str) for v in raw)):
            return None
        out.append(raw)
    return out


def _place(raw):
    """Where a list was, as stored: {'chain': the hashes (_sig) of its container and of the
    elements around it, the container first, 'others': those of the other elements of the page
    then, sorted, at most PLACE_KEPT, 'labels': the hashes of the labels of the page then
    (_label_key), sorted, 'cut': whether some elements were left out}. None for anything
    else."""
    if not isinstance(raw, dict):
        return None
    chain, others = _raws(raw.get('chain')), _raws(raw.get('others'))
    if chain is None or others is None:
        return None
    labels = raw.get('labels') if isinstance(raw.get('labels'), list) else []
    said = {_hash(_label_key(_fp._text(t))) for t in labels[:PLACE_LABELS]
            if isinstance(t, str) and _label_key(_fp._text(t))}
    kept = list(dict.fromkeys(_sig(r) for r in others))
    return {'chain': [_sig(r) for r in chain], 'others': sorted(kept[:PLACE_KEPT]),
            'labels': sorted(said), 'cut': raw.get('cut') is True or len(kept) > PLACE_KEPT}



def of_elements(els, labels=None, *, place=False):
    """The list fingerprint of lxml elements, with the list's label and place when
    place=True."""
    return from_raw(raw_of_elements(els, labels, place=place))


def valid(lfp):
    """True when ``lfp`` is a stored list fingerprint this version can score."""
    return (isinstance(lfp, dict) and lfp.get('v') == VERSION
            and isinstance(lfp.get('count'), int) and lfp['count'] >= 2
            and isinstance(lfp.get('items'), list) and bool(lfp['items'])
            and all(_fp.valid(item) for item in lfp['items']))


def placed(lfp):
    """True when the list fingerprint lfp holds where the list was and its label (recorded
    with LIST_PLACE_JS), which find() needs to tell another list from it."""
    if not isinstance(lfp, dict):
        return False
    place = lfp.get('place')
    return (isinstance(lfp.get('label'), str) and isinstance(place, dict)
            and isinstance(place.get('chain'), list) and isinstance(place.get('others'), list))


_placed = placed


def item_role(lfp):
    """The gate's role for the items of a list fingerprint, from its first item."""
    from .drivers.base import role_for
    item = lfp['items'][0]
    attrs = item.get('attrs') or {}
    return role_for(item['tag'], attrs.get('role'), attrs.get('type'))


# --- the groups of a page --------------------------------------------------------------------

@dataclass
class Group:
    """Elements that repeat: members (in document order) sit len(steps) levels below
    container, with the tags ``steps`` on the way down. score and the rest are find()'s:
    shape is None when no member's shape said anything."""
    members: list
    container: Any
    steps: tuple
    score: float = 0.0
    shape: float | None = 0.0
    xpath: str | None = None
    css: str | None = None
    built: bool = False


def groups(root, role):
    """The repeated groups of a parsed page whose members have the gate's role ``role``
    (validation._role_of), each member set once, the nearest container first."""
    pool = [el for el in _elements(root) if _role_of(el) == role]
    seen, out = set(), []
    for level in range(1, MAX_DEPTH + 1):
        buckets = {}
        for el in pool:
            node, steps = el, []
            for _ in range(level):
                steps.insert(0, node.tag.lower())
                node = node.getparent()
                if node is None or not isinstance(node.tag, str):
                    break
            else:
                if node.tag.lower() != 'html':
                    buckets.setdefault((node, tuple(steps)), []).append(el)
        for (container, steps), members in buckets.items():
            if len(members) < 2:
                continue
            for subset in _subsets(members):
                ident = frozenset(subset)
                if len(subset) >= 2 and ident not in seen:
                    seen.add(ident)
                    out.append(Group(subset, container, steps))
    return out


def _tokens(el):
    return [t for t in _fp.stable_classes(el.get('class')) if _class_ok(t) and _IDENT.fullmatch(t)]


def _subsets(members):
    """members, then for each class name two or more of them have, those that have it."""
    yield members
    counted = {}
    for el in members:
        for token in _tokens(el):
            counted.setdefault(token, []).append(el)
    for subset in counted.values():
        if 2 <= len(subset) < len(members):
            yield subset


# --- an XPath for a group ----------------------------------------------------------------------

def _common_tokens(members):
    first = _tokens(members[0])
    rest = [set(_tokens(m)) for m in members[1:]]
    return [t for t in first if all(t in r for r in rest)]


def group_xpath(group, root):
    """(xpath, css) that match the group's members and nothing else in root, or (None,
    None). The XPath has no position. Tried in order: the members' tag and a class name they
    share (//article[...'product-tile'...]), then the container's id, test id, name,
    aria-label or class and the tags down to the members, with and without that class name,
    then fingerprint.xpath_for() of the container. css is the same selector in CSS
    (article.product-tile, div#grid > article), None for the last form."""
    members = group.members
    tag = group.steps[-1]
    if not _IDENT.fullmatch(tag):
        return None, None
    tokens = _common_tokens(members)
    forms = []
    for token in tokens:
        css, pred = _class(token)
        forms.append(('//' + tag + '[' + pred + ']', tag + css))
    container, mid = group.container, group.steps[:-1]
    ctag = container.tag.lower() if isinstance(container.tag, str) else ''
    if _IDENT.fullmatch(ctag) and ctag not in ('html', 'body'):
        x_path = ''.join('/' + s for s in mid)
        c_path = ''.join(' > ' + s for s in mid)
        for a_css, a_pred in _anchor_parts(container):
            base_x, base_c = '//' + ctag + '[' + a_pred + ']' + x_path, ctag + a_css + c_path
            for token in tokens:
                css, pred = _class(token)
                forms.append((base_x + '/' + tag + '[' + pred + ']', base_c + ' > ' + tag + css))
            forms.append((base_x + '/' + tag, base_c + ' > ' + tag))
        anchor = _fp.xpath_for(container)
        if anchor:
            for token in tokens:
                forms.append((anchor + x_path + '/' + tag + '[' + _class(token)[1] + ']', None))
            forms.append((anchor + x_path + '/' + tag, None))
    want = set(members)
    for xpath, css in forms:
        try:
            hits = [h for h in root.xpath(xpath) if isinstance(getattr(h, 'tag', None), str)]
        except Exception:
            continue
        if len(hits) == len(want) and set(hits) == want:
            return xpath, css
    return None, None


def _built(group, root):
    if not group.built:
        group.xpath, group.css = group_xpath(group, root)
        group.built = True
    return group.xpath


# --- scoring ---------------------------------------------------------------------------------

def shape_similarity(old, new):
    """How alike two shapes are, from 0 to 1: the tags they share, weighted by how many of
    each they have (a weighted Jaccard). None for two empty shapes: two elements with
    nothing inside them, such as two <span>s of text, say nothing about each other."""
    tags = set(old) | set(new)
    if not tags:
        return None
    return (sum(min(old.get(t, 0), new.get(t, 0)) for t in tags)
            / sum(max(old.get(t, 0), new.get(t, 0)) for t in tags))


def _node_of(el):
    return _node({'tag': el.tag, 'attrs': {a: el.get(a) for a in CONTAINER_ATTRS
                                           if el.get(a) is not None}})


def _container_similarity(old, el):
    """How alike a recorded container and a group's container element are, from 0 to 1, or
    None when no container was recorded."""
    if not old:
        return None
    new = _node_of(el) or {}
    got, weights = (1.0 if old['tag'] == new.get('tag') else 0.0), 1.0
    for name, weight in (('id', 2.0), ('test', 2.0), ('aria', 1.0), ('role', 0.5)):
        if old.get(name):
            weights += weight
            got += weight if old[name] == new.get(name) else 0.0
    if old.get('classes'):
        weights += 1.0
        got += _fp._jaccard(old['classes'], new.get('classes'))
    return got / weights


def _sample(members, limit):
    """At most limit members, spread over the group."""
    if len(members) <= limit:
        return list(members)
    step = (len(members) - 1) / (limit - 1)
    return [members[round(i * step)] for i in range(limit)]


# The identity signals of a fingerprint (see fingerprint.score), as attributes and as fields.
IDENTITY_ATTRS = ('id', 'name', 'autocomplete', 'aria-label', 'href') + _fp.TEST_ATTRS
IDENTITY_FIELDS = ('text', 'label')


def _distinct(items):
    """The recorded items as _Scorer.identified() compares them: each without the identity
    values every one of them has (a test id, link, name or text all the items share).
    Unchanged for a single item."""
    if len(items) < 2:
        return items
    out = [dict(item, attrs=dict(item.get('attrs') or {})) for item in items]
    for name in IDENTITY_ATTRS:
        values = {(item.get('attrs') or {}).get(name) for item in items}
        if len(values) == 1 and None not in values:
            for item in out:
                item['attrs'].pop(name, None)
    for name in IDENTITY_FIELDS:
        values = {item.get(name) for item in items}
        if len(values) == 1 and None not in values:
            for item in out:
                item.pop(name, None)
    return out


class _Scorer:
    """Scores the groups of one page against one list fingerprint, with each element's
    fingerprint and shape read once."""

    def __init__(self, lfp, root):
        self.lfp = lfp
        self.root = root
        self.items = lfp['items']
        self.shapes = [item.get('shape') or {} for item in self.items]
        self.distinct = _distinct(self.items)
        self.labels = _fp.label_index(root)
        self._fps = {}
        self._shapes = {}
        self._scores = {}

    def fp(self, el):
        if el not in self._fps:
            self._fps[el] = _fp.of_element(el, self.labels)
        return self._fps[el]

    def scores(self, el):
        """fingerprint.score() of el against each recorded item, or None when el has no
        fingerprint."""
        if el not in self._scores:
            new = self.fp(el)
            self._scores[el] = (None if new is None
                                else [_fp.score(old, new) for old in self.items])
        return self._scores[el]

    def shape(self, el):
        if el not in self._shapes:
            self._shapes[el] = shape_of(el)
        return self._shapes[el]

    def _count(self, n):
        recorded = self.lfp['count']
        return min(n, recorded) / max(n, recorded)

    def _total(self, item, shape, container, count):
        parts = [(0.5, item), (0.10, count)]
        if shape is not None:
            parts.append((0.25, shape))
        if container is not None:
            parts.append((0.15, container))
        return sum(w * s for w, s in parts) / sum(w for w, _ in parts)

    def shaped(self, el):
        """How alike el's shape is to the most alike recorded item's, from 0 to 1, or None
        when el and a recorded item both have nothing inside them, which says nothing."""
        values = [shape_similarity(s, self.shape(el)) for s in self.shapes]
        if None in values:
            return None
        return max(values)

    def quick(self, group):
        """A score from the first member alone, without text: to pick the groups worth
        scoring in full."""
        first = group.members[0]
        tag = 1.0 if first.tag.lower() == self.items[0]['tag'] else 0.0
        container = _container_similarity(self.lfp.get('container'), group.container)
        return self._total(tag, self.shaped(first), container, self._count(len(group.members)))

    def full(self, group):
        """Score the group (its score and shape, the least alike shape of the members it
        looked at, None when no shape said anything)."""
        sample = _sample(group.members, SCORED)
        scores, shapes = [], []
        for el in sample:
            results = self.scores(el)
            if results is None:
                scores.append(0.0)
                continue
            scores.append(max(r.value for r in results))
            shape = self.shaped(el)
            if shape is not None:
                shapes.append(shape)
        container = _container_similarity(self.lfp.get('container'), group.container)
        mean_shape = sum(shapes) / len(shapes) if shapes else None
        group.score = self._total(sum(scores) / len(scores), mean_shape, container,
                                  self._count(len(group.members)))
        group.shape = min(shapes) if shapes else None
        return group

    def identified(self, group):
        """(found, shared): how many of the recorded items are among the group's members
        (IDENTIFIED of them, spread over the group, as the recorded items are the first, the
        middle and the last of the list), and whether a member shares an identity signal with every
        recorded item. A recorded item is among them when a member shares an id, test id,
        name, link, label or text with it (fingerprint.score's strong) that not every
        recorded item has, and does not share one with every recorded item: what all the
        items have, such as a test id each card has, or a link path that only a query told
        apart, says what kind of list it is, not which items it holds. With one recorded
        item, any such member counts."""
        found, shared = set(), False
        for el in _sample(group.members, IDENTIFIED):
            new = self.fp(el)
            if new is None:
                continue
            if len(self.items) > 1 and all(r.strong for r in self.scores(el)):
                shared = True
            hits = [i for i, old in enumerate(self.distinct) if _fp.score(old, new).strong]
            if hits and (len(hits) < len(self.items) or len(self.items) == 1):
                found.update(hits)
                if len(found) == len(self.items):
                    break
        return len(found), shared


def _holders(lfp, root):
    """The elements still on the page that held the recorded list: the container with the
    recorded id or test id, and the recorded items' ancestors with an id
    (fingerprint._containers), each found once on the page."""
    found = dict(_fp._containers(lfp['items'][0], root))
    old = lfp.get('container') or {}
    for name, attrs in (('id', ('id',)), ('test', ('data-testid', 'data-test', 'data-qa'))):
        value = old.get(name)
        if not value or (name == 'id' and _fp.generated_id(value)):
            continue
        hits = [el for el in _elements(root) for a in attrs
                if _fp._norm(el.get(a))[:_fp.VALUE_LIMIT] == value]
        if len(hits) == 1:
            found[name + ':' + value] = hits[0]
    return found


def _inside(group, holders):
    """True when the group's container is each holder or inside it."""
    chain = {group.container} | set(group.container.iterancestors())
    return all(holder in chain for holder in holders.values())


# --- another list ------------------------------------------------------------------------------
#
# An emptied list, such as a cart whose items all moved to Saved for later, must not be healed
# onto the other list that holds its items now. Two things recorded with the list fingerprint
# tell a group that is another list from the list after a redesign: where the list was on the
# page (its place), and its label. Each is a plain test, with no closeness scores:
#
# - place: going out from each member's parent, the first element that was on the page when
#   the list was recorded (by _sig) must be the list's container or one around it. One that
#   was elsewhere then, outside the list, holds another list. A redesign's new markup was not
#   there, so the test goes on past it. One like both, such as one of two section.panel side
#   by side, holds the list only under the list's own label.
# - label: a group under another label (_label_text) than the list's is another list when
#   that label was on the page when the list was recorded, or the list's own label still is.
#   A heading renamed with the list says nothing. Digits are left out (_label_key).

def _shown_label(text):
    return repr(text) if text else 'no heading or label'


class _Places:
    """The place and label tests of one page against one list fingerprint, each element's
    hash and each group's label read once."""

    def __init__(self, lfp, root):
        self.root = root
        self.label = lfp.get('label') if isinstance(lfp.get('label'), str) else None
        place = lfp.get('place')
        ok = (isinstance(place, dict) and isinstance(place.get('chain'), list)
              and isinstance(place.get('others'), list))
        self.chain = set(place['chain']) if ok else None
        self.others = set(place['others']) if ok else set()
        self.said = set(place.get('labels') or ()) if ok else set()
        self.cut = ok and place.get('cut') is True
        self._sigs = {}
        self._labels = {}
        self._now = None

    def label_of(self, group):
        key = (group.container, group.members[0])
        if key not in self._labels:
            self._labels[key] = _fp._text(_label_text(group.members[0], group.container))
        return self._labels[key]

    def labelled(self, group):
        """True when the group has the list's label and it is not empty."""
        return bool(self.label) and _label_key(self.label_of(group)) == _label_key(self.label)

    def _sig_of(self, el):
        if el not in self._sigs:
            self._sigs[el] = _sig(_node_raw(el))
        return self._sigs[el]

    def _said_now(self):
        """The labels of the page now (as _label_key gives them): the text of its headings, and
        the aria-labelledby or aria-label of its other elements."""
        if self._now is None:
            self._now = set()
            for el in _elements(self.root):
                if el.tag.lower() in _PRUNED:
                    continue
                if _heading(el):
                    self._now.add(_label_key(_fp._text(el.text_content())))
                if el.tag.lower() not in _LEAF:
                    own = _named(el)
                    if own:
                        self._now.add(_label_key(_fp._text(own)))
        return self._now

    def by_label(self, group):
        """Why the group is another list by its label, or '': it is not the list's label, and
        it was the label of another part of the page when the list was recorded, or the list's
        own label is still on the page. A label renamed with the list, the old one gone and
        the new one not there before, says nothing."""
        if self.label is None or self.chain is None:
            return ''
        theirs = self.label_of(group)
        mine = _label_key(self.label)
        if _label_key(theirs) == mine:
            return ''
        if theirs and _hash(_label_key(theirs)) in self.said:
            return ('is under ' + repr(theirs) + ', which labelled another part of the page when '
                    'the list was recorded, and the list was under ' + _shown_label(self.label))
        if mine and mine in self._said_now():
            return ('is under ' + _shown_label(theirs) + ', and the list was under '
                    + repr(self.label) + ', which is still on the page')
        return ''

    def _up(self, start):
        """Going out from start, the first element that was on the page when the list was
        recorded: ('mine', el) for the list's container or one around it, ('other', el) for
        one elsewhere, ('both', el) for one like each, or ('top', None) for none; and whether
        markup that was not there came first."""
        new = False
        node = start
        while node is not None and isinstance(node.tag, str) and node.tag.lower() not in ('body', 'html'):
            if node.tag.lower() not in _LEAF:
                sig = self._sig_of(node)
                mine, other = sig in self.chain, sig in self.others
                if mine or other:
                    return ('both' if mine and other else 'mine' if mine else 'other'), node, new
                new = True
            node = node.getparent()
        return 'top', None, new

    def by_place(self, group):
        """Why the group is another list by where it is, or '': from each member's parent,
        so a group that takes elements from two places is never the list."""
        if self.chain is None:
            return ''
        starts = list(dict.fromkeys(m.getparent() for m in group.members))
        for start in starts:
            kind, node, new = self._up(start)
            if kind == 'other':
                return ('is inside ' + _summary(node, 0) + ', which was on the page, outside '
                        'the list, when the list was recorded')
            if kind == 'both' and not self.labelled(group):
                return ('is inside ' + _summary(node, 0) + ', and when the list was recorded it '
                        'was in an element like that one, which was not the only one, with no '
                        'heading or label to tell them apart')
            if self.cut and new and not self.labelled(group):
                return ('is in markup that was not on the page when the list was recorded, and '
                        'the page was too large then to tell where the list was')
        return ''


def _elsewhere(group, holders, places):
    """Why the group is not the list but another list, or '' when it may be the list: outside
    a recorded container that is still on the page, under another label, or in another place
    (see _Places)."""
    if holders and not _inside(group, holders):
        held = next(iter(holders.values()))
        return ('is outside ' + _summary(held, 0) + ', which held the list and is still on the '
                'page')
    return places.by_label(group) or places.by_place(group)


def _busy(el):
    return (el.get('aria-busy') or '').strip().lower() == 'true'


def _placeholder(group, texts):
    """True when the group looks like the placeholders of a list that is still loading: no
    member has text while the recorded items had (texts), as skeleton cards have none, or
    aria-busy="true" is on a member, the container or above it."""
    if texts and not any(text_of(el) for el in group.members):
        return True
    return any(_busy(el) for el in group.members + [group.container]
               + list(group.container.iterancestors()))


def _overlap(a, b):
    shared = len(set(a.members) & set(b.members))
    return shared / min(len(a.members), len(b.members))


@dataclass
class ListMatch:
    """find()'s answer. xpath (and css, its CSS form when it has one) is set only when it
    found the list; reason says why not otherwise, and code and fragment say it once more
    for the refusal mendwright raises (L2, L3 and L10 to L19, see
    HealCoordinator.propose_list), with every control character made printable. count is
    how many elements the best group has, held how many of the recorded items are among
    them, of the recorded ones. best and runner are the best group and its nearest rival
    (Groups, or None), label the best group's label (_Places.label_of), container its
    container element and alike where that container is among the elements of the page like
    it, [i, n] (see _alike)."""
    xpath: str | None
    css: str | None
    count: int
    score: float
    runner_up: float
    reason: str
    # Every group on the page is another list (see _elsewhere), or the list fingerprint
    # can't tell one (OLD_FINGERPRINT).
    emptied: bool = False
    code: str = ''
    fragment: str = ''
    best: Any = None
    runner: Any = None
    held: int = 0
    recorded: int = 0
    label: str = ''
    container: Any = None
    alike: list = None


# What the refusal of a list whose groups are all other lists says: both things it can be.
EMPTY_OR_BROKEN = ('either the list is empty at this point, or its selector broke and its '
                   'items are not on the page')
# A list fingerprint saved before where the list was and its label were recorded can't tell
# another list from the list, so nothing is looked for from it.
OLD_FINGERPRINT = ('the stored list fingerprint was saved by an earlier build, which did not '
                   'record where the list was or its label, so another list could not be told '
                   'from it; it is recorded anew by the next run in which the list matches')


def _fmt(value):
    return format(value, '.2f')


def _refused(code, reason, fragment=None, **fields):
    """A ListMatch that found nothing, with its refusal's code and fragment."""
    return ListMatch(None, None, 0, 0.0, 0.0, reason, code=code,
                     fragment=_printable(reason if fragment is None else fragment), **fields)


def _lone(scorer, root, role):
    """A note for a list that was not found, when exactly one element of the role looks
    like a recorded item (a strong match, at fingerprint.THRESHOLD or more): one element is
    never taken for a list. '' otherwise."""
    alike = []
    for el in _elements(root):
        if _role_of(el) != role:
            continue
        new = scorer.fp(el)
        if new is None:
            continue
        if any(r.strong and r.value >= _fp.THRESHOLD for r in (_fp.score(old, new)
                                                             for old in scorer.items)):
            alike.append(el)
            if len(alike) > 1:
                return ''
    if not alike:
        return ''
    text = text_of(alike[0])
    shown = ' (' + repr(text[:40] + ('...' if len(text) > 40 else '')) + ')' if text else ''
    return ('. The only element like the recorded items is one <' + alike[0].tag.lower() + '>'
            + shown + '; mendwright never takes one element for a list')


def _alike(el, root):
    """[i, n]: el is the i-th of the n elements of the page with its tag and the same stable
    classes, in page order. [1, 1] when it is the only one, or for no element."""
    if el is None or not isinstance(el.tag, str):
        return [1, 1]
    tag, classes = el.tag.lower(), set(_fp.stable_classes(el.get('class')))
    same = [e for e in _elements(root) if e.tag.lower() == tag
            and set(_fp.stable_classes(e.get('class'))) == classes]
    if el not in same:
        return [1, 1]
    return [same.index(el) + 1, len(same)]


def find(lfp, html, role, *, threshold=THRESHOLD, margin=MARGIN,
         visible: Callable[[str], Any] | None = None):
    """Look for the list ``lfp`` (a list fingerprint) describes in ``html``, among the
    repeated groups of elements whose role is ``role``. Returns a ListMatch.

    visible(xpath) may check a group on the live page: it returns False when none of its
    elements is visible there, so a hidden copy of a list (a menu for small screens, say) is
    neither the answer nor a rival. None keeps the group."""
    if not valid(lfp):
        return _refused('L2', 'the stored list fingerprint is not one this version reads')
    if not placed(lfp):
        return _refused('L3', OLD_FINGERPRINT, emptied=True)
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return _refused('L10', 'the captured page could not be parsed')
    scorer = _Scorer(lfp, root)
    holders = _holders(lfp, root)
    places = _Places(lfp, root)
    recorded = len(scorer.items)
    found, others = [], []
    for group in groups(root, role):
        why = _elsewhere(group, holders, places)
        if why:
            others.append((group, why))     # another list: neither the answer nor a rival
        else:
            found.append(group)
    texts = any(item.get('text') for item in lfp['items'])
    kept = [g for g in found if not _placeholder(g, texts)]
    if found and not kept:
        return _refused('L12', 'the only groups of ' + str(role) + ' elements look like '
                        'placeholders (no text where the recorded items had text, or marked '
                        'aria-busy="true"), so the list may still be loading', recorded=recorded)
    found = kept
    nearest = max(others, key=lambda pair: scorer.quick(pair[0])) if others else None
    if not found and nearest:
        group, why = nearest
        return _refused('L11', 'each group of ' + str(role) + ' elements on the page is another '
                        'list. The one most like the list (' + str(len(group.members))
                        + ' elements) ' + why + '. So ' + EMPTY_OR_BROKEN,
                        'each group of ' + str(role) + ' elements on the page is another list: '
                        'the one most like the list (' + str(len(group.members)) + ' elements) '
                        + why, emptied=True, recorded=recorded)
    # What a refusal adds when groups were left out as other lists: the one most like the list.
    left = ('; left out as another list: a group of ' + str(len(nearest[0].members))
            + ' elements that ' + nearest[1]) if nearest else ''
    if not found:
        return _refused('L13', 'the page has no group of 2 or more ' + str(role) + ' elements '
                        'that repeat the same structure' + _lone(scorer, root, role),
                        recorded=recorded)
    found.sort(key=scorer.quick, reverse=True)
    ranked = sorted((scorer.full(g) for g in found[:RANKED]), key=lambda g: g.score,
                    reverse=True)
    shown = []
    for group in ranked:
        if visible is not None and len(shown) < 3 and _built(group, root) is not None:
            try:
                if visible(group.xpath) is False:
                    continue    # hidden on the live page: neither the answer nor a rival
            except Exception:
                pass
        shown.append(group)
    if not shown:
        return _refused('L14', 'no group of ' + str(role) + ' elements is visible on the live '
                        'page', recorded=recorded)
    best = shown[0]
    rivals = [g for g in shown[1:] if _overlap(g, best) < 0.5]
    rival = rivals[0] if rivals else None
    runner = rival.score if rival else 0.0
    label = places.label_of(best)
    n = str(len(best.members))

    def where(group):
        text = places.label_of(group)
        return ' under ' + repr(text) if text else ''

    def refuse(code, reason, fragment, held=0):
        return ListMatch(None, None, len(best.members), best.score, runner,
                         reason + left + _lone(scorer, root, role), code=code,
                         fragment=_printable(fragment + left), best=best, runner=rival,
                         held=held, recorded=recorded, label=label, container=best.container)

    if best.score < threshold:
        return refuse('L15', 'the best group (' + n + ' elements) scores ' + _fmt(best.score)
                      + ', below the ' + _fmt(threshold) + ' needed',
                      'the best group (' + n + ' elements' + where(best) + ') scores '
                      + _fmt(best.score) + ', below the ' + _fmt(threshold) + ' needed')
    if best.score - runner < margin:
        return refuse('L16', 'two groups score alike (' + _fmt(best.score) + ' and '
                      + _fmt(runner) + '), so the list fingerprint does not tell them apart',
                      'two groups score alike (' + _fmt(best.score) + ' for ' + n + ' elements'
                      + where(best) + ', ' + _fmt(runner) + ' for ' + str(len(rival.members))
                      + ' elements' + where(rival) + '), so the list fingerprint does not tell '
                      'them apart')
    if best.shape is not None and best.shape < SHAPE_MIN:
        shape = ('the elements of the best group are not all shaped like the recorded items '
                 '(the least alike is ' + _fmt(best.shape) + ', and ' + _fmt(SHAPE_MIN)
                 + ' is needed)')
        return refuse('L17', shape, shape)
    need = min(NEEDED, recorded)
    held, shared = scorer.identified(best)
    if held < need:
        head = 'the best group (score ' + _fmt(best.score) + ') '
        if held:
            return refuse('L18a', head + 'has only ' + str(held) + ' of the ' + str(recorded)
                          + ' recorded items among its elements (an element that shares an id, '
                          'test id, name, link, label or text with one), and ' + str(need)
                          + ' are needed, so it may be another list in the same place',
                          'the best group (' + n + ' elements' + where(best) + ') holds only '
                          + str(held) + ' of the ' + str(recorded) + ' items it recorded, and '
                          + str(need) + ' are needed, so it may be another list in the same '
                          'place', held)
        if shared:
            return refuse('L18b', head + 'shares an id, test id, name, link, label or text only '
                          'with every recorded item at once, which says what kind of list it '
                          'is, not which items it holds',
                          'the best group shares an id, test id, name, link, label or text only '
                          'with every recorded item at once, which says what kind of list it '
                          'is, not which items it holds')
        return refuse('L18c', head + 'shares no id, test id, name, link, label or text with the '
                      'items that were recorded',
                      'the best group shares no id, test id, name, link, label or text with the '
                      "items it recorded, so a list whose items change from run to run can't "
                      'be found')
    if _built(best, root) is None:
        none = 'no selector without positions matches the best group and nothing else'
        return refuse('L19', none, none, held)
    return ListMatch(best.xpath, best.css, len(best.members), best.score, runner, '',
                     best=best, runner=rival, held=held, recorded=recorded, label=label,
                     container=best.container, alike=_alike(best.container, root))


# --- what the list gate uses -----------------------------------------------------------------

POSITIONAL = 'it uses a position ([2], last() or position()), and a list heal never does'


def validate(xpath, html, role):
    """The static check of a list heal: a Result that is ok when xpath, without positions,
    matches two or more elements of role in html and none of them is inside another."""
    if _is_brittle(xpath):
        return Result(False, 'brittle absolute-index path', 0)
    if has_position(xpath):
        return Result(False, POSITIONAL, 0)
    try:
        doc = lxml.html.fromstring(html)
        matches = [m for m in doc.xpath(xpath) if isinstance(getattr(m, 'tag', None), str)]
    except Exception as exc:
        return Result(False, 'xpath error: ' + type(exc).__name__, 0)
    count = len(matches)
    if count == 0:
        return Result(False, 'no match', 0)
    if count == 1:
        return Result(False, 'it matches 1 element, and a list heal needs 2 or more', 1)
    for el in matches:
        got = _role_of(el)
        if got != role:
            return Result(False, 'role mismatch: got ' + got + ' want ' + str(role), count)
    found = set(matches)
    if any(anc in found for el in matches for anc in el.iterancestors()):
        return Result(False, 'its elements are inside one another', count)
    return Result(True, 'ok', count)


def _summary(el, text_limit=ITEM_TEXT):
    """An element as a refusal names it: its tag, id, class, role and test id, and its text,
    cut short."""
    attrs = ''.join(' ' + a + '="' + el.get(a).replace('"', "'") + '"'
                    for a in ('id', 'class', 'role') + TEST_ATTRS[:1]
                    if el.get(a) is not None and len(el.get(a)) <= 120)
    text = text_of(el) if text_limit else ''
    if len(text) > text_limit:
        text = text[:text_limit - 3] + '...'
    return '<' + el.tag.lower() + attrs + '>' + (text + '</' + el.tag.lower() + '>' if text else '')


# --- what a person checks: state words, the evidence and its id ----------------------------------
#
# A group find() takes is refused when its XPath picks it by a word that says a state the
# recorded list didn't have: the items of an emptied list often show in another list whose
# class or name says what happened to them (completed tasks, items saved for later). This
# says what the words mean and is no proof: a site whose class names say nothing gets no
# help from it. A list that was itself about a state (a "Completed" list, items with class
# done) has the word in what was recorded, so its redesigns are not refused for it.

STATE_WORDS = frozenset({
    'completed', 'complete', 'done', 'finished', 'archived', 'read', 'seen', 'saved',
    'removed', 'deleted', 'cancelled', 'canceled', 'expired', 'inactive', 'closed',
    'resolved', 'past', 'empty', 'unavailable', 'soldout'})

_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_WORD = re.compile('[a-z]+')
# Every C0 and C1 control character, DEL included.
_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')


def _words(text):
    """The runs of the letters a to z in text, lower-cased ('saved-item': saved, item)."""
    return _WORD.findall(text.lower()) if isinstance(text, str) else []


def state_word(xpath, lfp):
    """(token, word) when a string literal of xpath holds a word of STATE_WORDS that the
    recorded list did not have, else None. token is the first such literal, stripped
    ('saved-item'), and word the word ('saved'). What the list had: the words of the
    recorded items' classes, of the recorded container's tag, id, test id, aria-label and
    classes, and of the list's label."""
    had = set()
    for item in lfp.get('items') or ():
        for name in item.get('classes') or ():
            had.update(_words(name))
    container = lfp.get('container') if isinstance(lfp.get('container'), dict) else {}
    for name in ('tag', 'id', 'test', 'aria'):
        had.update(_words(container.get(name)))
    for name in container.get('classes') or ():
        had.update(_words(name))
    had.update(_words(lfp.get('label')))
    for literal in _LITERAL.finditer(xpath or ''):
        text = literal.group(1) if literal.group(1) is not None else literal.group(2)
        for word in _words(text):
            if word in STATE_WORDS and word not in had:
                return text.strip(), word
    return None


def _printable(text):
    """text with every control character (C0, C1 and DEL) as '?', so no escape sequence
    from a page reaches a terminal. '' for anything but a str."""
    return _CONTROL.sub('?', text) if isinstance(text, str) else ''


def _shown(text):
    """Page text as a proposal keeps it: fingerprint._text (whitespace collapsed, runs of 12
    to 19 digits as #, at most 80 characters), then _printable."""
    return _printable(_fp._text(text))


def shown_container(node):
    """A stored container node (see _node) as '<tag id="..." class="a b">', leaving out what
    it lacks; '' for no container."""
    if not isinstance(node, dict) or not isinstance(node.get('tag'), str):
        return ''
    out = '<' + _printable(node['tag'])
    if node.get('id'):
        out += ' id="' + _printable(node['id']).replace('"', "'") + '"'
    if node.get('classes'):
        out += ' class="' + _printable(' '.join(node['classes'])).replace('"', "'") + '"'
    return out + '>'


def _container_node(el):
    """The container element of a group as a stored node, None for none or for <body> and
    <html>, which LIST_JS does not record either."""
    if el is None or not isinstance(el.tag, str) or el.tag.lower() in ('body', 'html'):
        return None
    return _node_of(el)


def _container_differs(old, new):
    """True when two container nodes are not the same: their tags differ, or both have an id
    and the ids differ, or either has stable classes and they share none."""
    old, new = old or {}, new or {}
    if old.get('tag') != new.get('tag'):
        return True
    if old.get('id') and new.get('id') and old['id'] != new['id']:
        return True
    mine, theirs = set(old.get('classes') or ()), set(new.get('classes') or ())
    return bool(mine or theirs) and not (mine & theirs)


def proposal_evidence(key, lfp, match, xpath, css, broken, page, at):
    """What a proposed list heal saves for a person to check (the proposal of its row in
    overrides.json): where the list was when lfp was recorded and what it held ('was'),
    where the group match found is and what it holds ('now'), what is not the same
    ('differs': 'heading', 'container'), and the id (proposal_id). broken are the key's
    selectors that matched nothing, page the page's path and at the time. Every string from
    the page goes through fingerprint._text and _printable."""
    old = lfp.get('container') if isinstance(lfp.get('container'), dict) else None
    new = _container_node(match.container)
    members = match.best.members if match.best is not None else []
    was = {'selectors': [str(s) for s in broken], 'count': lfp['count'],
           'label': _shown(lfp.get('label')), 'container': shown_container(old),
           'items': [_shown(item['text']) for item in lfp['items'] if item.get('text')][:2]}
    now = {'label': _shown(match.label), 'container': shown_container(new),
           'alike': list(match.alike or [1, 1]),
           'items': [_shown(text_of(el)) for el in members[:2]]}
    differs = []
    if _label_key(was['label']) != _label_key(now['label']):
        differs.append('heading')
    if _container_differs(old, new):
        differs.append('container')
    evidence = {'id': '', 'at': at, 'by': 'fingerprint', 'page': page, 'count': match.count,
                'found': match.held, 'recorded': match.recorded,
                'score': round(match.score, 2), 'next': round(match.runner_up, 2),
                'was': was, 'now': now, 'differs': differs}
    evidence['id'] = proposal_id(key, xpath, css, evidence)
    return evidence


def proposal_id(key, xpath, css, evidence):
    """The id of a proposal: the first 8 hex digits of the SHA-256 of everything mendwright
    lists shows of it but the time and the scores, so it changes whenever that does."""
    text = json.dumps([key, xpath, css or '', evidence.get('page'), evidence.get('count'),
                       evidence.get('found'), evidence.get('recorded'), evidence.get('was'),
                       evidence.get('now'), evidence.get('differs')],
                      sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        return str(n) + 'th'
    return str(n) + {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')


def place_text(count, side, alike=False):
    """Where a list or a group sat, as mendwright lists and mendwright fix print it: '3
    elements under 'Shopping cart', in <section class="cart">', with ', the 2nd of 2 alike'
    when alike and its container was one of several alike. side is the 'was' or 'now' of a
    proposal (see proposal_evidence)."""
    side = side if isinstance(side, dict) else {}
    text = (str(count) + ' element' + ('' if count == 1 else 's')
            if isinstance(count, int) and not isinstance(count, bool) else 'its elements')
    if side.get('label'):
        text += ' under ' + repr(_printable(side['label']))
    if side.get('container'):
        text += (', in ' if side.get('label') else ' in ') + _printable(side['container'])
    shown = side.get('alike')
    if (alike and isinstance(shown, list) and len(shown) == 2
            and all(isinstance(n, int) for n in shown) and shown[1] > 1):
        text += ', the ' + _ordinal(shown[0]) + ' of ' + str(shown[1]) + ' alike'
    return text


def differs_lines(evidence):
    """What is not the same between where the list was and where the group found is, one
    sentence for each of evidence['differs'], as mendwright lists and mendwright fix print
    them."""
    was = evidence.get('was') if isinstance(evidence.get('was'), dict) else {}
    now = evidence.get('now') if isinstance(evidence.get('now'), dict) else {}
    differs = evidence.get('differs') if isinstance(evidence.get('differs'), list) else []

    def label(side):
        return repr(_printable(side['label'])) if side.get('label') else 'no heading'

    def box(side):
        return _printable(side['container']) if side.get('container') else '<body>'
    out = []
    if 'heading' in differs:
        out.append('The heading is not the same: ' + label(was) + ' then, ' + label(now) + ' now.')
    if 'container' in differs:
        out.append('The container is not the same: ' + box(was) + ' then, ' + box(now) + ' now.')
    return out


__all__ = ['LIST_JS', 'LIST_PLACE_JS', 'ITEMS_JS', 'STATE_WORDS', 'Group', 'ListMatch', 'find',
           'from_raw', 'group_xpath', 'groups', 'item_role', 'items_of_elements', 'of_elements',
           'placed', 'proposal_evidence', 'proposal_id', 'raw_of_elements', 'shown_container',
           'state_word', 'valid', 'validate']
