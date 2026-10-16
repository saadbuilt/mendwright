"""Lists: what the items of a list looked like, for heal.locator_all().

Each time a list lookup (Healer.locator_all()) matches two or more elements, mendwright
records a list fingerprint in one round trip (LIST_JS): the fingerprints of up to three of
the items (the first, the middle and the last, see mendwright.fingerprint), the tags inside
each one and of its children (its shape), how many items there were, their nearest common
container (its tag, id, test id, aria-label, classes and role) and how many levels below it
they sit. A list that matched fewer than two elements is not recorded: nothing repeats in
it.

The list fingerprint says that the list had items, so a lookup of it that matches nothing
waits and raises instead of giving [] at once. It is never used to heal the list: an
emptied list, whose items can show in another list on the page, such as a cart's in Saved
for later, can't be told from a list whose selector broke (see Healer.locator_all).
"""

from __future__ import annotations

from . import fingerprint as _fp

# Version of the stored shape. A list fingerprint with another version is ignored.
VERSION = 1

SAMPLES = 3          # items recorded per list
SHAPE_TAGS = 40      # tags kept in a shape
SHAPE_LIMIT = 500    # descendants counted for a shape

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


__all__ = ['LIST_JS', 'from_raw', 'item_role', 'of_elements', 'raw_of_elements', 'valid']
