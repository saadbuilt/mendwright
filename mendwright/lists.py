"""Lists: heal a selector that matched the items of a list, such as the cards of a product
grid, onto the new items after a redesign.

Each time a list lookup (Healer.locator_all()) matches two or more elements, mendwright
records a list fingerprint in one round trip (LIST_JS): the fingerprints of up to three of
the items (the first, the middle and the last, see mendwright.fingerprint), the tags inside
each one and of its children (its shape), how many items there were, their nearest common
container (its tag, id, test id, aria-label, classes and role) and how many levels below it
they sit. A list that matched fewer than two elements is not recorded: nothing repeats in
it.

When every selector of a recorded list matches nothing, groups() lists the repeated groups
of the captured page: two or more elements of the items' role that sit the same number of
levels (one to three) below one container, with the same tags on the way down, and, for
each class name two of them share, those that have it. find() scores each group against
the list fingerprint, from 0 to 1:

- how much each member looks like one of the recorded items (fingerprint.score), on
  average (a half of the score)
- how alike the members' shapes are to the recorded ones (a quarter), left out when the
  members and the recorded items have nothing inside them, which says nothing
- how alike the group's container is to the recorded one (0.15)
- how close the number of members is to the recorded count (0.10)

and heals only when all of these hold: the best group scores at least THRESHOLD, no group
that shares less than half of its members with it comes within MARGIN, two of the
recorded items (both, for a list recorded with two) are among its members, every member it
looked at has a shape at least SHAPE_MIN alike, it is inside the recorded container and the
recorded items' ancestors with an id when those are still on the page, and an XPath
without positions matches its members and nothing else.

A recorded item is among the members when one of them shares an id, test id, name, link,
label or text with it (fingerprint.score's strong) that not every recorded item has, and
does not share one with every recorded item: what all the items have, such as a test id
each card has, says what kind of list it is, not which items it holds, and where a group
sits says nothing about that either. So an emptied list is not healed onto another list in
its place, such as the hints of a form whose errors went away, or the recommendations a
search with no results shows where its results were.
A group of placeholders is neither the answer nor a rival, and is not shown to the model:
one whose members have no text where the recorded items had text, such as the skeleton
cards of a list that is still loading, or that is marked aria-busy="true".

The XPath then goes through the list gate (client.apply_list): two or more elements of the
role on the captured page, none inside another, not quarantined for the key, and, on the
live page, two or more elements of the role with at least one visible. One element is never
taken for a list.

When find() is not confident, the coordinator asks the model, if there is one, with the
groups listed (prompt.build_list_prompt); the model may only pick one of them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
NEEDED = 2           # recorded items that must be among a group's members to heal onto it
MAX_DEPTH = 3        # levels between a group's container and its members
SHAPE_TAGS = 40      # tags kept in a shape
SHAPE_LIMIT = 500    # descendants counted for a shape
SCORED = 25          # members of a group compared with the recorded items
IDENTIFIED = 200     # members of the best group looked through for the recorded items,
                     # spread over it, the first and the last included
RANKED = 30          # groups scored in full, of those that score best in a quick look
PROMPT_GROUPS = 12   # groups listed for the model
ITEM_TEXT = 100      # how much of an item's text a group's description shows

CONTAINER_ATTRS = ('id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa')
_UNRENDERED = ('template', 'noscript', 'script', 'style')

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


def raw_of_elements(els, labels=None):
    """What LIST_JS returns for lxml elements ``els`` (in document order), computed with
    lxml, or None for fewer than two."""
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
    return {'count': len(els), 'depth': depth, 'items': items, 'container': container}


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
    return out


def of_elements(els, labels=None):
    """The list fingerprint of lxml elements."""
    return from_raw(raw_of_elements(els, labels))


def valid(lfp):
    """True when ``lfp`` is a stored list fingerprint this version can score."""
    return (isinstance(lfp, dict) and lfp.get('v') == VERSION
            and isinstance(lfp.get('count'), int) and lfp['count'] >= 2
            and isinstance(lfp.get('items'), list) and bool(lfp['items'])
            and all(_fp.valid(item) for item in lfp['items']))


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
    """find()'s answer. xpath (and css, its CSS form when it has one) is set only when it is
    confident; reason says why not otherwise. count is how many elements xpath matches.
    groups are the page's groups scored best first, for the model's prompt."""
    xpath: str | None
    css: str | None
    count: int
    score: float
    runner_up: float
    reason: str
    groups: list = field(default_factory=list)


def _fmt(value):
    return format(value, '.2f')


def _lone(scorer, root, role):
    """A note for a list that did not heal, when exactly one element of the role looks like
    a recorded item (a strong match, at fingerprint.THRESHOLD or more): a list is never
    healed onto it. '' otherwise."""
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
            + shown + '; mendwright never heals a list onto one element')


def find(lfp, html, role, *, threshold=THRESHOLD, margin=MARGIN,
         visible: Callable[[str], Any] | None = None):
    """Look for the list ``lfp`` (a list fingerprint) describes in ``html``, among the
    repeated groups of elements whose role is ``role``. Returns a ListMatch.

    visible(xpath) may check a group on the live page: it returns False when none of its
    elements is visible there, so a hidden copy of a list (a menu for small screens, say) is
    neither the answer nor a rival."""
    if not valid(lfp):
        return ListMatch(None, None, 0, 0.0, 0.0,
                         'the stored list fingerprint is not one this version reads')
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return ListMatch(None, None, 0, 0.0, 0.0, 'the captured page could not be parsed')
    scorer = _Scorer(lfp, root)
    found = groups(root, role)
    holders = _holders(lfp, root)
    if holders:
        found = [g for g in found if _inside(g, holders)]
    texts = any(item.get('text') for item in lfp['items'])
    kept = [g for g in found if not _placeholder(g, texts)]
    if found and not kept:
        return ListMatch(None, None, 0, 0.0, 0.0,
                         'the only groups of ' + str(role) + ' elements look like placeholders '
                         '(no text where the recorded items had text, or marked '
                         'aria-busy="true"), so the list may still be loading')
    found = kept
    if not found:
        return ListMatch(None, None, 0, 0.0, 0.0,
                         'the page has no group of 2 or more ' + str(role) + ' elements that '
                         'repeat the same structure' + _lone(scorer, root, role))
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
        return ListMatch(None, None, 0, 0.0, 0.0, 'no group of ' + str(role) + ' elements is '
                         'visible on the live page')
    best = shown[0]
    rivals = [g for g in shown[1:] if _overlap(g, best) < 0.5]
    runner = rivals[0].score if rivals else 0.0
    prompt = [g for g in shown if _built(g, root) is not None][:PROMPT_GROUPS]

    def refuse(reason):
        return ListMatch(None, None, len(best.members), best.score, runner,
                         reason + _lone(scorer, root, role), prompt)

    if best.score < threshold:
        return refuse('the best group (' + str(len(best.members)) + ' elements) scores '
                      + _fmt(best.score) + ', below the ' + _fmt(threshold) + ' needed')
    if best.score - runner < margin:
        return refuse('two groups score alike (' + _fmt(best.score) + ' and ' + _fmt(runner)
                      + '), so the list fingerprint does not tell them apart')
    if best.shape is not None and best.shape < SHAPE_MIN:
        return refuse('the elements of the best group are not all shaped like the recorded '
                      'items (the least alike is ' + _fmt(best.shape) + ', and '
                      + _fmt(SHAPE_MIN) + ' is needed)')
    need = min(NEEDED, len(scorer.items))
    held, shared = scorer.identified(best)
    if held < need:
        head = 'the best group (score ' + _fmt(best.score) + ') '
        if held:
            return refuse(head + 'has only ' + str(held) + ' of the ' + str(len(scorer.items))
                          + ' recorded items among its elements (an element that shares an id, '
                          'test id, name, link, label or text with one), and ' + str(need)
                          + ' are needed, so it may be another list in the same place')
        if shared:
            return refuse(head + 'shares an id, test id, name, link, label or text only with '
                          'every recorded item at once, which says what kind of list it is, '
                          'not which items it holds')
        return refuse(head + 'shares no id, test id, name, link, label or text with the items '
                      'that were recorded')
    if _built(best, root) is None:
        return refuse('no selector without positions matches the best group and nothing else')
    return ListMatch(best.xpath, best.css, len(best.members), best.score, runner, '', prompt)


# --- what the gate and the prompt use --------------------------------------------------------

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
        return Result(False, 'it matches 1 element, and a list heals only onto 2 or more', 1)
    for el in matches:
        got = _role_of(el)
        if got != role:
            return Result(False, 'role mismatch: got ' + got + ' want ' + str(role), count)
    found = set(matches)
    if any(anc in found for el in matches for anc in el.iterancestors()):
        return Result(False, 'its elements are inside one another', count)
    return Result(True, 'ok', count)


def _summary(el, text_limit=ITEM_TEXT):
    """An element as a group's description shows it: its tag, id, class, role and test id,
    and its text, cut short."""
    attrs = ''.join(' ' + a + '="' + el.get(a).replace('"', "'") + '"'
                    for a in ('id', 'class', 'role') + TEST_ATTRS[:1]
                    if el.get(a) is not None and len(el.get(a)) <= 120)
    text = text_of(el) if text_limit else ''
    if len(text) > text_limit:
        text = text[:text_limit - 3] + '...'
    return '<' + el.tag.lower() + attrs + '>' + (text + '</' + el.tag.lower() + '>' if text else '')


def describe(group):
    """What the prompt shows of a group: {'xpath', 'count', 'container', 'items'}, with the
    container's opening tag and the first two members."""
    return {'xpath': group.xpath, 'count': len(group.members),
            'container': _summary(group.container, 0),
            'items': [_summary(el) for el in group.members[:2]]}


__all__ = ['LIST_JS', 'ITEMS_JS', 'Group', 'ListMatch', 'find', 'groups', 'group_xpath',
           'from_raw', 'of_elements', 'raw_of_elements', 'valid', 'validate', 'describe']
