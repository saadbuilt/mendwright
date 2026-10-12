"""Synthetic redesign scenarios for training mendwright's local selector model.

A scenario is a page before a redesign (v1), the page after it (v2), a step (a key and the
selector that matched on v1) and the truth: the element in v2 the step means, or none.
The truth element in v2 carries a marker attribute (TRUTH_ATTR) that build_dataset.py
removes from the captured bundle before anything else reads it.

Positive scenarios stack one to three redesign mutations: id renamed, removed or
generated; class churn; a new wrapper; the control moved to another region; fields
reordered; the label reworded or translated; a tag change that keeps the role; attribute
churn (name, test id, aria-label); near-duplicates where the row or card decides; a hidden
copy of the control; destructive decoys next to it; a URL-only route rename.

Negative scenarios (the right answer is no element): the control removed; removed with a
destructive or merely similar control in its place; duplicates that nothing tells apart;
the step moved to another page (a different path, often with a look-alike control); an
interstitial page.

    python training/gen_scenarios.py --show 5      # print a few scenarios

Nothing here reads benchmarks/cases.
"""

import argparse
import json
import random
import re
import sys
from pathlib import Path

import lxml.etree
import lxml.html

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lexicon import CONCEPTS, DESTRUCTIVE, LANGS, NEIGHBOURS  # noqa: E402
from pages import (RELATED, TEST_FAMILIES, TRAIN_FAMILIES, El, Gen, Site, build,  # noqa: E402
                   document, find_unit, parent_map, srng, stable)

TRUTH_ATTR = 'data-mw-truth'
TARGET_ATTR = 'data-mw-target'

POSITIVE_MUTATIONS = {
    'id': 10, 'classes': 8, 'wrap': 7, 'move': 6, 'order': 5, 'reword': 9, 'translate': 6,
    'tag': 6, 'attrs': 8, 'decoys': 7, 'hidden_dup': 5, 'ids_global': 4, 'label_mode': 3,
    'reorder_items': 4, 'overlay': 3,
}
NEGATIVE_KINDS = {'removed': 24, 'near_danger': 19, 'ambiguous': 11, 'moved': 50, 'interstitial': 12}

KIND_WEIGHT = {'button': 34, 'link': 18, 'input': 24, 'checkbox': 9, 'switch': 4, 'radio': 4,
               'select': 7, 'textarea': 3}

ROUTE_RENAMES = {
    'login': ('signin', 'sign-in', 'auth/login', 'account/login'),
    'signup': ('register', 'join', 'get-started', 'account/create'),
    'cart': ('basket', 'bag', 'shop/cart'),
    'checkout': ('checkout/information', 'secure-checkout', 'order/checkout'),
    'products': ('shop', 'catalog', 'collections/all'),
    'settings/profile': ('account/profile', 'settings/account', 'me/profile'),
    'settings/notifications': ('account/notifications', 'preferences/notifications'),
    'settings/security': ('account/security', 'settings/password'),
    'team/members': ('settings/members', 'people', 'org/members'),
    'dashboard': ('home', 'overview', 'app/dashboard'),
    'projects': ('workspace/projects', 'app/projects'),
    '': ('home', 'welcome'),
    'search': ('find', 'results'),
    'forgot-password': ('password/reset', 'account/recover'),
    'login/verify': ('auth/verify', 'login/2fa'),
}


def _kind_of(concept):
    kind = CONCEPTS[concept].kind
    return 'input' if kind.startswith('input:') else kind


# --- tree surgery -------------------------------------------------------------------------------

def _root(kids):
    return El('body', None, *[k for k in kids if k is not None])


def _hides(el):
    style = (el.attrs.get('style') or '').replace(' ', '').lower()
    cls = ' ' + (el.attrs.get('class') or '') + ' '
    return ('hidden' in el.attrs or 'display:none' in style or 'visibility:hidden' in style
            or el.attrs.get('aria-hidden') == 'true' or ' d-none ' in cls or 'mega-menu' in cls
            or el.tag in ('dialog',) or el.attrs.get('role') in ('dialog', 'alertdialog'))


def _containers(root, avoid):
    """Elements a control can be moved into: landmarks and sections not inside avoid, and
    not inside anything hidden (a mobile menu) or a dialog."""
    parents = parent_map(root)
    inside = set()
    for el in avoid.walk():
        inside.add(id(el))
    for el in root.walk():
        if _hides(el):
            for sub in el.walk():
                inside.add(id(sub))
    out = []
    for el in root.walk():
        if id(el) in inside or el is root:
            continue
        if el.tag in ('header', 'nav', 'aside', 'main', 'footer', 'section', 'form', 'fieldset', 'ul', 'div'):
            if el.tag == 'div' and len([k for k in el.kids if isinstance(k, El)]) < 2:
                continue
            # not an ancestor of avoid
            node = avoid
            is_anc = False
            while id(node) in parents:
                node = parents[id(node)]
                if node is el:
                    is_anc = True
                    break
            if not is_anc and el.uid is None and el.tag not in ('label',):
                out.append(el)
    return out


def detach(root, node):
    parents = parent_map(root)
    parent = parents.get(id(node))
    if parent is None:
        return None, -1
    idx = next(i for i, k in enumerate(parent.kids) if k is node)
    parent.kids.pop(idx)
    return parent, idx


def surgery_move(root, uid, rng):
    unit = find_unit(root, uid)
    if unit is None:
        return False
    options = _containers(root, unit)
    parents = parent_map(root)
    old_parent = parents.get(id(unit))
    options = [c for c in options if c is not old_parent]
    if not options:
        return False
    dest = rng.choice(options)
    detach(root, unit)
    if dest.tag in ('ul', 'ol'):
        unit = El('li', None, unit)
    dest.kids.insert(rng.randrange(len(dest.kids) + 1), unit)
    return True


def surgery_wrap(root, uid, rng, style_cls):
    unit = find_unit(root, uid)
    if unit is None:
        return False
    parent, idx = detach(root, unit)
    if parent is None:
        return False
    node = unit
    for _ in range(rng.randrange(1, 4)):
        tag = rng.choice(('div', 'div', 'span', 'div', 'ui-field', 'x-slot', 'app-control', 'section'))
        attrs = {'class': style_cls(rng.choice(('wrapper', 'slot', 'control', 'group', 'cell')))}
        if rng.random() < 0.2:
            attrs['data-slot'] = rng.choice(('control', 'root', 'trigger'))
        node = El(tag, attrs, node)
    parent.kids.insert(idx, node)
    return True


def surgery_order(root, uid, rng):
    unit = find_unit(root, uid)
    if unit is None:
        return False
    parents = parent_map(root)
    parent = parents.get(id(unit))
    for _ in range(3):
        if parent is None:
            return False
        els = [k for k in parent.kids if isinstance(k, El)]
        if len(els) >= 3:
            break
        parent = parents.get(id(parent))
    else:
        return False
    els = [k for k in parent.kids if isinstance(k, El)]
    before = list(els)
    for _ in range(5):
        rng.shuffle(els)
        if els != before:
            break
    it = iter(els)
    parent.kids = [next(it) if isinstance(k, El) else k for k in parent.kids]
    return True


def surgery_reorder_items(root, uid, rng):
    """Shuffle the rows, cards or list items around the item that holds uid."""
    parents = parent_map(root)
    node = find_unit(root, uid)
    while node is not None and node.tag not in ('tr', 'li', 'article') and not (node.attrs.get('class') or '').count('card'):
        node = parents.get(id(node))
    if node is None:
        return False
    parent = parents.get(id(node))
    if parent is None:
        return False
    els = [k for k in parent.kids if isinstance(k, El)]
    if len(els) < 2:
        return False
    before = list(els)
    for _ in range(5):
        rng.shuffle(els)
        if els != before:
            break
    it = iter(els)
    parent.kids = [next(it) if isinstance(k, El) else k for k in parent.kids]
    return True


def _decoy_concepts(concept, kind):
    pool = [c for c in NEIGHBOURS.get(concept, ()) if _kind_of(c) == kind]
    if kind == 'button':
        pool += [c for c in DESTRUCTIVE if _kind_of(c) == 'button' and c != concept]
    return [c for c in dict.fromkeys(pool) if c != concept]


def make_decoy(g2, concept, uid, area, variant='secondary'):
    kind = _kind_of(concept)
    if kind == 'button':
        return g2.button(uid, concept, area=area, variant=variant)
    if kind == 'link':
        return g2.link(uid, concept, '/' + CONCEPTS[concept].slugs[0], area=area)
    if kind in ('checkbox', 'radio', 'switch'):
        return g2.check(uid, concept, area=area)
    return g2.field(uid, concept, area=area)


def surgery_decoys(root, uid, concept, g2, rng, replace=False):
    """Put one or two look-alike or destructive controls next to uid (or in its place)."""
    kind = _kind_of(concept)
    options = _decoy_concepts(concept, kind)
    if not options:
        return False
    unit = find_unit(root, uid)
    if unit is None:
        return False
    parent, idx = detach(root, unit)
    if parent is None:
        return False
    picks = rng.sample(options, min(len(options), rng.choice((1, 1, 2))))
    decoys = [make_decoy(g2, c, 'decoy.' + c + '.' + str(i), 'decoy') for i, c in enumerate(picks)]
    decoys = [d for d in decoys if d is not None]
    if not decoys:
        parent.kids.insert(idx, unit)
        return False
    if replace:
        parent.kids[idx:idx] = decoys
    else:
        group = [unit] + decoys
        rng.shuffle(group)
        parent.kids[idx:idx] = group
    return True


def _strip_uids(node):
    for el in node.walk():
        el.uid = None
        el.unit = None
        for attr in (TRUTH_ATTR, TARGET_ATTR):
            el.attrs.pop(attr, None)
        if 'id' in el.attrs and el.attrs['id']:
            el.attrs['id'] = el.attrs['id'] + '-mobile' if srng(el.attrs['id']).random() < 0.5 else None
            if el.attrs['id'] is None:
                del el.attrs['id']
        if 'for' in el.attrs:
            el.attrs['for'] = el.attrs['for'] + '-mobile'


def surgery_hidden_dup(root, uid, rng):
    unit = find_unit(root, uid)
    if unit is None:
        return False
    copy = unit.clone()
    _strip_uids(copy)
    hide = rng.choice(({'hidden': True}, {'style': 'display:none'}, {'style': 'display: none;'},
                       {'class': 'mobile-only d-none', 'aria-hidden': 'true'},
                       {'aria-hidden': 'true', 'style': 'visibility:hidden'}))
    box = El('div', dict(hide, **{'class': (hide.get('class') or '') + ' sticky-bar'}), copy)
    parents = parent_map(root)
    parent = parents.get(id(unit))
    where = rng.random()
    if where < 0.4 and parent is not None:
        idx = next(i for i, k in enumerate(parent.kids) if k is unit)
        parent.kids.insert(idx + rng.choice((0, 1)), box)
    elif where < 0.7:
        root.kids.insert(0, box)
    else:
        root.kids.append(box)
    return True


def surgery_ambiguous(root, uid, g2, concept, area, rng):
    """Replace uid with two or three identical controls of its concept that nothing tells
    apart."""
    unit = find_unit(root, uid)
    if unit is None:
        return False
    parent, idx = detach(root, unit)
    if parent is None:
        return False
    n = rng.choice((2, 2, 3))
    text = g2.word(uid, concept)
    copies = []
    for i in range(n):
        dup_uid = 'dup.' + str(i)
        g2.over[dup_uid] = {'id': 'none', 'testid': 'none', 'name': 'none'}
        kind = _kind_of(concept)
        if kind == 'button':
            el = g2.button(dup_uid, concept, area=area, text=text, variant='secondary')
        elif kind == 'link':
            el = g2.link(dup_uid, concept, '#', area=area, text=text)
        elif kind in ('checkbox', 'radio', 'switch'):
            el = g2.check(dup_uid, concept, area=area, text=text)
        else:
            el = g2.field(dup_uid, concept, area=area)
        copies.append(el)
    holder = rng.random()
    if holder < 0.5:
        parent.kids[idx:idx] = copies
    else:
        parent.kids[idx:idx] = [El('div', {'class': g2.cls('block')}, c) for c in copies]
    return True


def surgery_inject(root, concept, g2, rng, area='main'):
    """Put a control of concept somewhere on the page (a look-alike on another page)."""
    el = make_decoy(g2, concept, 'inject.' + concept, area, variant='primary')
    if el is None:
        return False
    options = [c for c in root.walk() if c.tag in ('form', 'section', 'main', 'div') and c.uid is None
               and len([k for k in c.kids if isinstance(k, El)]) >= 1]
    if not options:
        return False
    dest = rng.choice(options)
    dest.kids.insert(rng.randrange(len(dest.kids) + 1), el)
    return True


# --- selectors -----------------------------------------------------------------------------------

_XPATH_SPACE = re.compile(r'[ \t\r\n]+')


def _text(el):
    return _XPATH_SPACE.sub(' ', el.text_content() or '').strip()


def _lit(value):
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return None


def _label_text(el):
    root = el.getroottree().getroot()
    ident = el.get('id')
    if ident:
        for lab in root.xpath('//label[@for=$i]', i=ident):
            return _text(lab), 'for'
    for anc in el.iterancestors('label'):
        return _text(anc), 'wrap'
    return None, None


def selector_options(el):
    """(style, selector) pairs a test author might have written for el."""
    out = []
    tag = el.tag
    root = el.getroottree().getroot()

    def add(style, sel):
        if sel and '\n' not in sel:
            out.append((style, sel))

    ident = el.get('id')
    if ident and _lit(ident):
        if re.match(r'^[A-Za-z_][\w-]*$', ident):
            add('id', '#' + ident)
        add('id', '//' + tag + '[@id=' + _lit(ident) + ']')
        add('id', '//*[@id=' + _lit(ident) + ']')
    for attr in ('data-testid', 'data-test', 'data-qa', 'data-cy', 'data-test-id'):
        value = el.get(attr)
        if value and _lit(value):
            add('testid', '//*[@' + attr + '=' + _lit(value) + ']')
            add('testid', '//' + tag + '[@' + attr + '=' + _lit(value) + ']')
    name = el.get('name')
    if name and _lit(name):
        add('name', '//' + tag + '[@name=' + _lit(name) + ']')
        if el.get('type'):
            add('name', '//' + tag + '[@type=' + _lit(el.get('type')) + ' and @name=' + _lit(name) + ']')
    for attr, style in (('aria-label', 'aria'), ('placeholder', 'placeholder'), ('title', 'aria')):
        value = el.get(attr)
        if value and _lit(value):
            add(style, '//' + tag + '[@' + attr + '=' + _lit(value) + ']')
    text = _text(el)
    if text and len(text) <= 60 and _lit(text) and tag not in ('input', 'select', 'textarea'):
        add('text', '//' + tag + '[normalize-space()=' + _lit(text) + ']')
        add('text', '//' + tag + '[normalize-space(.)=' + _lit(text) + ']')
        if el.text and el.text.strip() == text:
            add('text', '//' + tag + '[text()=' + _lit(text) + ']')
            add('text', '//*[text()=' + _lit(text) + ']')
        word = max(text.split(), key=len)
        if len(word) >= 4 and _lit(word):
            add('text', '//' + tag + '[contains(text(), ' + _lit(word) + ')]')
            add('text', '//' + tag + '[contains(., ' + _lit(word) + ')]')
    if el.get('value') and _lit(el.get('value')):
        add('text', '//input[@value=' + _lit(el.get('value')) + ']')
    cls = el.get('class')
    if cls and _lit(cls):
        add('class', '//' + tag + '[@class=' + _lit(cls) + ']')
        tokens = cls.split()
        for tok in tokens[:3]:
            add('class', '//' + tag + "[contains(@class, '" + tok + "')]")
        if re.match(r'^[A-Za-z_][\w-]*$', tokens[0]):
            add('class', "//" + tag + "[contains(concat(' ', normalize-space(@class), ' '), ' " + tokens[0] + " ')]")
    href = el.get('href')
    if href and href != '#' and _lit(href):
        add('href', '//a[@href=' + _lit(href) + ']')
    label, how = _label_text(el)
    if label and _lit(label) and len(label) <= 60:
        if how == 'for':
            add('label', '//label[normalize-space()=' + _lit(label) + ']/following-sibling::' + tag + '[1]')
            add('label', '//' + tag + '[@id=//label[normalize-space()=' + _lit(label) + ']/@for]')
            add('label', '//label[text()=' + _lit(label) + ']/..//' + tag)
        else:
            add('label', '//label[contains(., ' + _lit(label) + ')]//' + tag)
    typ = el.get('type')
    if tag == 'input' and typ in ('password', 'email', 'search', 'tel', 'checkbox', 'radio', 'number'):
        add('type', '//input[@type=' + _lit(typ) + ']')
    # context: an item (row, card) or a section heading, plus something of the element.
    own = []
    if cls and _lit(cls.split()[0]):
        own.append("contains(@class, '" + cls.split()[0] + "')")
    if el.get('aria-label') and _lit(el.get('aria-label')):
        own.append('@aria-label=' + _lit(el.get('aria-label')))
    if text and len(text) <= 40 and _lit(text) and tag not in ('input', 'select', 'textarea'):
        own.append('normalize-space()=' + _lit(text))
    if typ:
        own.append('@type=' + _lit(typ))
    for anc in el.iterancestors():
        if anc.tag in ('body', 'html'):
            break
        if anc.tag in ('tr', 'li', 'article') or 'card' in (anc.get('class') or ''):
            cells = [n for n in anc.iter('td', 'h3', 'span', 'a', 'h2') if n is not el and _text(n) and el not in n.iterancestors()]
            for cell in cells[:2]:
                t = _text(cell)
                if 0 < len(t) <= 40 and _lit(t):
                    for pred in own[:2] + [None]:
                        add('context', '//' + anc.tag + '[.//' + cell.tag + '[normalize-space()=' + _lit(t) + ']]//'
                            + tag + ('[' + pred + ']' if pred else ''))
                    break
            break
        if anc.tag in ('form', 'section', 'fieldset', 'nav', 'header', 'footer', 'aside', 'dialog'):
            if anc.get('id') and _lit(anc.get('id')):
                for pred in own[:2] + [None]:
                    add('context', '//' + anc.tag + '[@id=' + _lit(anc.get('id')) + ']//' + tag + ('[' + pred + ']' if pred else ''))
            heads = [n for n in anc.iter('h1', 'h2', 'h3', 'legend') if _text(n)]
            if heads and _lit(_text(heads[0])) and len(_text(heads[0])) <= 50:
                for pred in own[:2]:
                    add('context', '//' + anc.tag + '[.//' + heads[0].tag + '[normalize-space()=' + _lit(_text(heads[0])) + ']]//' + tag + '[' + pred + ']')
            if anc.tag in ('header', 'nav', 'footer') and own:
                add('context', '//' + anc.tag + '//' + tag + '[' + own[0] + ']')
    path = el.getroottree().getpath(el)
    add('position', path)
    parts = path.split('/')
    if len(parts) > 4:
        add('position', '//' + '/'.join(parts[-3:]))
    same = root.xpath('//' + tag)
    if el in same:
        add('position', '(//' + tag + ')[' + str(same.index(el) + 1) + ']')
    return out


STYLE_WEIGHTS = {'id': 26, 'testid': 11, 'name': 10, 'text': 15, 'class': 9, 'label': 5,
                 'context': 10, 'position': 6, 'aria': 3, 'placeholder': 2, 'href': 3, 'type': 2}


def _count(doc, selector):
    xp = selector
    if selector.startswith('#'):
        xp = "//*[@id='" + selector[1:] + "']"
    try:
        hits = doc.xpath(xp)
    except lxml.etree.XPathError:
        return -1, []
    hits = [x for x in hits if isinstance(getattr(x, 'tag', None), str)]
    return len(hits), hits


def _hidden(el):
    if el.tag == 'input' and (el.get('type') or '').lower() == 'hidden':
        return True
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or 'visibility:hidden' in style or el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def _broken(v2_doc, selector):
    n2, hits2 = _count(v2_doc, selector)
    return n2 >= 0 and not (n2 == 1 and not _hidden(hits2[0]))


def choose_selectors(rng, v1_marked, v2_doc, positive):
    """Old selectors for the step: they match the target in v1 (exactly once) and do not
    resolve to one visible element in v2. [] when there is none."""
    doc1 = lxml.html.document_fromstring(v1_marked)
    targets = doc1.xpath('//*[@' + TARGET_ATTR + ']')
    if len(targets) != 1:
        return [], None
    el = targets[0]
    del el.attrib[TARGET_ATTR]
    options = selector_options(el)
    good = []
    for style, sel in options:
        n1, hits1 = _count(doc1, sel)
        if n1 != 1 or hits1[0] is not el:
            continue
        n2, hits2 = _count(v2_doc, sel)
        if n2 < 0:
            continue
        if n2 == 1 and not _hidden(hits2[0]):
            continue
        good.append((style, sel))
    if not good:
        return [], el
    weights = [STYLE_WEIGHTS.get(style, 1) for style, _ in good]
    first = rng.choices(good, weights=weights)[0]
    chosen = [first[1]]
    if rng.random() < 0.15:
        others = [s for st, s in good if st != first[0]]
        if others:
            chosen.append(rng.choice(others))
    return chosen, el


# --- keys ---------------------------------------------------------------------------------------

def _words(text):
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower()) if w]


def make_key(rng, uid, concept, area, item, name_item=True):
    """A key a test author might use. For a control in a row or card the key names the
    item (name_item), since nothing else in the step would say which row is meant; an
    ambiguous negative leaves it out."""
    style = rng.random()
    base = rng.choice((concept.split('_'), CONCEPTS[concept].slugs[0].split('-'),
                       CONCEPTS[concept].slugs[-1].split('-')))
    parts = []
    if area and rng.random() < 0.5:
        parts += _words(area)[:1]
    parts += base
    if item and name_item:
        parts += _words(item)[:3]
    if rng.random() < 0.15:
        parts += [rng.choice(('btn', 'button', 'field', 'input', 'link', 'cb'))]
    if style < 0.08 and not item:
        return rng.choice(('step', 'el', 'field', 'btn', 'control', 'target')) + str(rng.randrange(1, 30))
    if style < 0.40:
        return '_'.join(parts)
    if style < 0.62:
        return (parts[0] + '.' + '_'.join(parts[1:])) if len(parts) > 1 else parts[0]
    if style < 0.82:
        return parts[0] + ''.join(p.capitalize() for p in parts[1:])
    return '-'.join(parts)


# --- scenarios ----------------------------------------------------------------------------------

def _eligible(controls, family):
    out = []
    for c in controls:
        uid = c['uid']
        if uid.startswith(('mob.', 'decoy.', 'dup.', 'inject.', 'dlg.x', 'dlg.r')):
            continue
        out.append(c)
    return out


def _pick_target(rng, controls):
    """A kind first (KIND_WEIGHT), then a control of that kind, so pages full of links do
    not make every step a link."""
    by_kind = {}
    for c in controls:
        by_kind.setdefault(_kind_of(c['concept']), []).append(c)
    kinds = sorted(by_kind)
    kind = rng.choices(kinds, weights=[KIND_WEIGHT.get(k, 5) for k in kinds])[0]
    pool = by_kind[kind]
    # chrome links are many; prefer the page's own controls two times out of three
    own = [c for c in pool if not c['uid'].startswith(CHROME)]
    if own and rng.random() < 0.67:
        pool = own
    return rng.choice(pool)


def _other_lang(rng, lang, concept):
    options = [lang_ for lang_ in LANGS if lang_ != lang and CONCEPTS[concept].words.get(lang_)]
    if lang != 'en':
        options.append('en')
    return rng.choice(options) if options else None


def _url(site, path, rng):
    url = site.host + '/' + path if path else site.host + '/'
    if rng.random() < 0.15:
        url += '?ref=' + rng.choice(('nav', 'email', 'promo'))
    return url


def _rename_route(rng, path):
    if path in ROUTE_RENAMES:
        return rng.choice(ROUTE_RENAMES[path])
    if path.startswith('blog/'):
        return 'articles/' + path[5:]
    if path.startswith('onboarding/'):
        return 'welcome/' + path.split('/', 1)[1]
    return 'v2/' + path if path else 'home'


class Scenario(dict):
    pass


# Controls in the site chrome: they are on every page of the site, so a step on one of them
# never moves to another page.
CHROME = ('hdr.', 'ftr.', 'side.', 'top.', 'mob.', 'mega.', 'ck.', 'nl.')


def make(seed, split="train", twin_rate=0.2):
    """The scenarios for one seed: one, or a positive and its negative twin. [] when this
    seed gives nothing usable."""
    rng = random.Random(stable(seed, split, 'scenario'))
    vocab = 'test' if split == 'test' else 'train'
    site = Site(rng, vocab)
    if split == 'test' and rng.random() < 0.6:
        family = rng.choice(TEST_FAMILIES)
    else:
        family = rng.choice(TRAIN_FAMILIES)
    content = rng.randrange(1 << 30)
    overlays1 = []
    if rng.random() < 0.12:
        overlays1.append('cookie')
    if rng.random() < 0.06:
        overlays1.append('newsletter')
    g1, kids1, path1 = build(site, family, content, split=vocab, overlays=overlays1)
    eligible = _eligible(g1.controls, family)
    if not eligible:
        return []
    target = _pick_target(rng, eligible)
    uid, concept, area, item = target['uid'], target['concept'], target['area'], target['item']
    positive = rng.random() < 0.80
    with_urls = rng.random() < 0.7
    out = []
    if positive:
        sc = _positive(rng, seed, split, site, family, content, vocab, overlays1, uid, concept, area, item, path1, with_urls)
        if sc:
            out.append(sc)
            if rng.random() < twin_rate:
                twin = _negative(random.Random(stable(seed, 'twin')), seed, split, site, family, content, vocab,
                                 overlays1, uid, concept, area, item, path1, sc['url_mode'] != 'none',
                                 kind=rng.choice(('removed', 'near_danger')), base=sc)
                if twin:
                    twin['twin_of'] = sc['id']
                    out.append(twin)
            sc.pop('_style2', None)
            sc.pop('_lang2', None)
    else:
        kind = rng.choices(list(NEGATIVE_KINDS), weights=list(NEGATIVE_KINDS.values()))[0]
        if kind in ('moved', 'interstitial') and uid.startswith(CHROME):
            kind = 'removed'
        if kind == 'moved' and not with_urls:
            with_urls = True
        sc = _negative(rng, seed, split, site, family, content, vocab, overlays1, uid, concept, area, item, path1,
                       with_urls, kind=kind)
        if sc:
            out.append(sc)
    if out and out[0]['kind'] not in ('ambiguous', 'moved') and rng.random() < 0.06:
        # A few steps are a key met for the first time, described only by intent=.
        out[0] = first_use(out[0], rng)
    return out


def _render_pair(site, family, content, vocab, overlays1, uid, style2, lang2, over2, overlays2, ops, rng, concept, area,
                 build_kw=None, family2=None):
    """v1 marked (target), v1 clean, v2 with truth marker. ops edits the v2 tree. family2
    builds v2 as another page of the site (for a control in the site chrome)."""
    g1m, kids1m, _ = build(site, family, content, split=vocab, overlays=overlays1, marker_uid=uid, marker_attr=TARGET_ATTR)
    v1_marked = document(g1m, kids1m)
    g1, kids1, _ = build(site, family, content, split=vocab, overlays=overlays1)
    v1 = document(g1, kids1)
    g2, kids2, path2 = build(site, family2 or family, content, style=style2, lang=lang2, over=over2, split=vocab,
                             overlays=overlays2, marker_uid=uid, marker_attr=TRUTH_ATTR, **(build_kw or {}))
    root = _root(kids2)
    applied = []
    for op in ops:
        if op(root, g2):
            applied.append(op.__name__ if hasattr(op, '__name__') else 'op')
    v2 = document(g2, root.kids)
    return v1_marked, v1, v2, g2, path2


def _positive(rng, seed, split, site, family, content, vocab, overlays1, uid, concept, area, item, path1, with_urls):
    kind = _kind_of(concept)
    muts = set()
    n = rng.choices((1, 2, 3), weights=(45, 38, 17))[0]
    pool = dict(POSITIVE_MUTATIONS)
    if item is None:
        pool.pop('reorder_items', None)
    else:
        pool.pop('move', None)   # into another row or card, it would belong to that item
    if len(CONCEPTS[concept].variants(site.lang)) < 2:
        pool.pop('reword', None)
    while len(muts) < n:
        muts.add(rng.choices(list(pool), weights=list(pool.values()))[0])
    style2 = site.style.copy()
    lang2 = site.lang
    over2 = {}
    o = over2.setdefault(uid, {})
    overlays2 = list(overlays1)
    ops = []
    if 'id' in muts:
        o['id'] = rng.choice(('rename', 'none', 'generated', 'rename'))
    if 'classes' in muts:
        style2.hash_seed = rng.randrange(1 << 30)
        if rng.random() < 0.35:
            style2.cls = rng.choice(('bem', 'utility', 'module', 'styled', 'emotion', 'bootstrap', 'plain'))
    if 'ids_global' in muts:
        style2.id_seed = rng.randrange(1 << 30)
        style2.ids = rng.choice(('generated', 'mixed', 'sparse'))
    if 'reword' in muts:
        n_var = len(CONCEPTS[concept].variants(site.lang))
        o['word'] = rng.randrange(1, n_var)
        if rng.random() < 0.3:
            o['aria'] = 'change'
    if 'translate' in muts:
        other = _other_lang(rng, site.lang, concept)
        if other:
            lang2 = other
    if 'tag' in muts:
        if kind == 'button':
            o['form'] = rng.choice(('div', 'a', 'span', 'input', 'button'))
        elif kind == 'link':
            o['form'] = rng.choice(('span', 'button'))
        elif kind == 'select':
            o['form'] = rng.choice(('div', 'input'))
        elif kind in ('checkbox', 'switch', 'radio'):
            o['form'] = 'div' if kind != 'switch' else rng.choice(('div', 'switch-input', 'input'))
        elif kind == 'input' and rng.random() < 0.3:
            o['form'] = 'div'
        else:
            o['id'] = o.get('id') or 'rename'
    if 'attrs' in muts:
        choice = rng.random()
        if choice < 0.3:
            o['testid'] = rng.choice(('rename', 'none', 'force'))
        elif choice < 0.5:
            style2.testid = rng.choice(('data-testid', 'data-test', 'data-qa', 'data-cy', None))
            o['testid'] = 'force' if style2.testid else None
        elif choice < 0.75:
            o['name'] = rng.choice(('rename', 'none'))
        else:
            o['aria'] = rng.choice(('add', 'change'))
            o['title'] = rng.random() < 0.3
    if 'label_mode' in muts and kind in ('input', 'select', 'textarea', 'checkbox', 'radio', 'switch'):
        o['labels'] = rng.choice(('for', 'wrap', 'aria', 'placeholder', 'labelledby', 'sibling'))
    if 'overlay' in muts:
        new = [o_ for o_ in ('cookie', 'newsletter') if o_ not in overlays2]
        if new:
            overlays2.append(rng.choice(new))
    if 'hidden_dup' in muts and family not in ('landing',):
        o['_unused'] = 1
    r_ops = random.Random(stable(seed, 'ops'))

    def op_move(root, g2):
        return surgery_move(root, uid, r_ops)

    def op_wrap(root, g2):
        return surgery_wrap(root, uid, r_ops, g2.cls)

    def op_order(root, g2):
        return surgery_order(root, uid, r_ops)

    def op_reorder(root, g2):
        return surgery_reorder_items(root, uid, r_ops)

    def op_decoys(root, g2):
        return surgery_decoys(root, uid, concept, g2, r_ops)

    def op_hidden(root, g2):
        return surgery_hidden_dup(root, uid, r_ops)

    for name, op in (('move', op_move), ('wrap', op_wrap), ('order', op_order), ('reorder_items', op_reorder),
                     ('decoys', op_decoys), ('hidden_dup', op_hidden)):
        if name in muts:
            ops.append(op)
    family2 = None
    if with_urls and uid.startswith(CHROME) and rng.random() < 0.4:
        # The site chrome is on every page: the step meets its control on another page of
        # the flow, under another path. Still the same control.
        for other in rng.sample(list(RELATED.get(family, ())), len(RELATED.get(family, ()))):
            if (split != 'test' and other in TEST_FAMILIES) or other == family:
                continue
            v1m, v1, v2, g2, path2 = _render_pair(site, family, content, vocab, overlays1, uid, style2, lang2,
                                                  over2, overlays2, ops, rng, concept, area, family2=other)
            if TRUTH_ATTR in v2 and path2 != path1:
                family2 = other
                muts.add('other_page')
                break
    if family2 is None:
        v1m, v1, v2, g2, path2 = _render_pair(site, family, content, vocab, overlays1, uid, style2, lang2, over2,
                                              overlays2, ops, rng, concept, area)
    if TRUTH_ATTR not in v2:
        return None
    v2_doc = lxml.html.document_fromstring(v2)
    selectors, el1 = choose_selectors(rng, v1m, v2_doc, True)
    if not selectors:
        # Nothing the author could have written broke: also change the id, and try again.
        if o.get('id') is None and o.get('form') is None:
            o['id'] = 'rename'
            muts.add('id')
            v1m, v1, v2, g2, path2 = _render_pair(site, family, content, vocab, overlays1, uid, style2, lang2, over2,
                                                  overlays2, ops, rng, concept, area, family2=family2)
            v2_doc = lxml.html.document_fromstring(v2)
            selectors, el1 = choose_selectors(rng, v1m, v2_doc, True)
        if not selectors:
            return None
    route = None
    url_mode = 'none'
    v1_url = v2_url = None
    if with_urls and family2 is not None:
        v1_url = _url(site, path1, rng)
        v2_url = _url(site, path2, rng)
        url_mode = 'other_page'
    elif with_urls:
        v1_url = _url(site, path1, rng)
        if rng.random() < 0.14:
            route = _rename_route(rng, path1)
            v2_url = _url(site, route, rng)
            url_mode = 'renamed'
            muts.add('route')
        else:
            v2_url = _url(site, path2, rng) if rng.random() < 0.5 else v1_url
            url_mode = 'same'
    sc = _scenario(rng, seed, split, site, family, uid, concept, area, item, el1, selectors, v1, v2,
                   v1_url, v2_url, url_mode, truth=True, why=None, kind='positive', muts=sorted(muts),
                   lang2=lang2)
    sc['_style2'] = style2
    sc['_lang2'] = lang2
    return sc


def _negative(rng, seed, split, site, family, content, vocab, overlays1, uid, concept, area, item, path1, with_urls,
              kind, base=None):
    style2 = site.style.copy()
    lang2 = site.lang
    over2 = {uid: {}}
    overlays2 = list(overlays1)
    ops = []
    muts = {kind}
    if base is not None:
        # The twin of a positive: the same redesign, but the control is gone.
        style2 = base['_style2'].copy()
        lang2 = base['_lang2']
        muts.add('twin')
    elif rng.random() < 0.5:
        # a light redesign on top, so the negative is not just "the page minus one control"
        style2.hash_seed = rng.randrange(1 << 30)
        muts.add('classes')
    if base is None and rng.random() < 0.15:
        other = _other_lang(rng, site.lang, concept)
        if other:
            lang2 = other
            muts.add('translate')
    r_ops = random.Random(stable(seed, 'negops', kind))
    target_family = family
    build_kw = {}
    why = 'removed'
    if kind == 'removed':
        over2[uid]['removed'] = True
    elif kind == 'near_danger':
        def op(root, g2):
            return surgery_decoys(root, uid, concept, g2, r_ops, replace=True)
        ops.append(op)
    elif kind == 'ambiguous':
        why = 'ambiguous'

        def op(root, g2):
            return surgery_ambiguous(root, uid, g2, concept, area, r_ops)
        ops.append(op)
    elif kind == 'moved':
        why = 'moved'
        target_family = rng.choice(RELATED.get(family, TRAIN_FAMILIES))
        if split != 'test' and target_family in TEST_FAMILIES:
            target_family = rng.choice(TRAIN_FAMILIES)
        if target_family == family and family == 'settings':
            build_kw = {}
        if rng.random() < 0.6:
            def op(root, g2):
                return surgery_inject(root, concept, g2, r_ops)
            ops.append(op)
    elif kind == 'interstitial':
        target_family = 'interstitial'
    # Build v1 (marked and clean) from the original family, v2 from the target family.
    from pages import build as _build, fam_interstitial
    g1m, kids1m, _ = _build(site, family, content, split=vocab, overlays=overlays1, marker_uid=uid, marker_attr=TARGET_ATTR)
    v1m = document(g1m, kids1m)
    g1, kids1, _ = _build(site, family, content, split=vocab, overlays=overlays1)
    v1 = document(g1, kids1)
    if target_family == 'interstitial':
        g2 = Gen(site, 'interstitial', content, style=style2, lang=lang2, split=vocab)
        kids2, path2 = fam_interstitial(g2)
    elif kind == 'moved':
        content2 = content if target_family != family else content + 1
        g2, kids2, path2 = _build(site, target_family, content2, style=style2, lang=lang2, over={}, split=vocab,
                                  overlays=overlays2, **build_kw)
        if path2 == path1:
            return None
    else:
        g2, kids2, path2 = _build(site, family, content, style=style2, lang=lang2, over=over2, split=vocab,
                                  overlays=overlays2)
    root = _root(kids2)
    for op in ops:
        if not op(root, g2):
            if kind in ('near_danger', 'ambiguous'):
                return None
    # The target must be gone from v2: a page the step "moved" to that still has the same
    # control (site chrome on every page) is not a negative.
    for el in root.walk():
        if el.uid == uid and kind in ('near_danger', 'ambiguous', 'moved', 'interstitial'):
            return None
        el.attrs.pop(TRUTH_ATTR, None)
    if kind in ('removed', 'near_danger') and item is None:
        # Another control with the same meaning on the page (a second sign-in link, the
        # hero and the header call to action) could fairly be picked: not a clean negative.
        skip = ('decoy.', 'dup.', 'inject.', 'mob.', 'mega.')
        if any(c['concept'] == concept and c['uid'] != uid and not c['uid'].startswith(skip)
               and c.get('item') is None for c in g2.controls):
            return None
    v2 = document(g2, root.kids)
    v2_doc = lxml.html.document_fromstring(v2)
    selectors, el1 = choose_selectors(rng, v1m, v2_doc, False)
    if base is not None and selectors:
        # keep the positive's selectors when they are broken here too: it is the same step
        if all(_broken(v2_doc, s) for s in base['selectors']):
            selectors = list(base['selectors'])
    if not selectors:
        return None
    url_mode = 'none'
    v1_url = v2_url = None
    if base is not None and base['url_mode'] != 'none':
        v1_url = base['v1_url']
        v2_url = base['v2_url']
        url_mode = base['url_mode']
    elif with_urls or kind == 'moved':
        v1_url = _url(site, path1, rng)
        if kind in ('moved',) or (kind == 'interstitial' and rng.random() < 0.6):
            v2_url = _url(site, path2, rng)
            url_mode = 'moved'
            why = 'moved'
        else:
            v2_url = v1_url
            url_mode = 'same'
    if kind == 'interstitial' and url_mode != 'moved':
        why = 'removed'
    if url_mode == 'moved':
        from mendwright.flow import page_path
        # both pages must have a path, or the step cannot be seen to move
        if page_path(v1_url) is None or page_path(v1_url) == page_path(v2_url):
            return None
    return _scenario(rng, seed, split, site, family, uid, concept, area, item, el1, selectors, v1, v2,
                     v1_url, v2_url, url_mode, truth=False, why=why, kind=kind, muts=sorted(muts), lang2=lang2,
                     base=base)


AREA_WORDS = {
    'login': 'login form', 'signup': 'sign-up form', 'checkout': 'checkout form',
    'shipping': 'shipping address form', 'payment': 'payment section', 'header': 'header',
    'nav': 'main navigation', 'footer': 'footer', 'sidebar': 'sidebar', 'cookie': 'cookie banner',
    'newsletter': 'newsletter form', 'popup': 'newsletter popup', 'dialog': 'dialog',
    'row': 'members table', 'grid': 'product grid', 'cart': 'cart', 'filters': 'filters',
    'profile': 'profile settings', 'notifications': 'notification settings',
    'security': 'security settings', 'danger': 'danger zone', 'table': 'members table',
    'dashboard': 'dashboard', 'hero': 'hero section', 'features': 'features section',
    'onboarding': 'onboarding form', 'reset': 'password reset form', 'mfa': 'verification form',
    'results': 'search results', 'search': 'search form', 'share': 'share buttons',
    'comment': 'comment form', 'tabs': 'settings tabs', 'stats': 'stats cards',
    'activity': 'recent activity list', 'pagination': 'pagination', 'invite': 'invite form',
    'plan': 'plan picker', 'byline': 'article byline', 'related': 'related articles',
    'pager': 'article navigation', 'tags': 'tag list', 'banner': 'upgrade banner',
}
KIND_WORDS = {'button': 'button', 'link': 'link', 'input': 'field', 'checkbox': 'checkbox',
              'switch': 'toggle', 'radio': 'option', 'select': 'dropdown', 'textarea': 'text box'}


def make_intent(rng, concept, area, item):
    """What a test author might write in intent=: the control's English words, its kind,
    and where it is or which item it belongs to."""
    words = rng.choice(CONCEPTS[concept].variants('en'))
    kind = KIND_WORDS.get(_kind_of(concept), 'control')
    style = rng.random()
    if style < 0.35:
        text = 'the ' + words + ' ' + kind
    elif style < 0.6:
        text = words + ' ' + kind
    elif style < 0.8:
        text = 'the ' + kind + ' that says ' + words
    else:
        text = 'the ' + kind + ' to ' + words[0].lower() + words[1:]
    if item:
        text += rng.choice((' for ', ' in the row for ', ' on the card for ')) + item
    elif area in AREA_WORDS and rng.random() < 0.6:
        text += ' in the ' + AREA_WORDS[area]
    return text


def _scenario(rng, seed, split, site, family, uid, concept, area, item, el1, selectors, v1, v2, v1_url, v2_url,
              url_mode, truth, why, kind, muts, lang2, base=None):
    from mendwright.validation import _role_of
    role = _role_of(el1)
    key = base['key'] if base else make_key(rng, uid, concept, area, item, name_item=kind != 'ambiguous')
    text = None
    if rng.random() < 0.2 and role in ('button', 'link'):
        t = _text(el1)
        if t and len(t) <= 60:
            text = t
    intent = base.get('intent') if base else None
    if base is None and rng.random() < 0.15:
        intent = make_intent(rng, concept, area, item)
    sid = split + '-' + str(seed) + ('-' + kind if base else '')
    return Scenario(id=sid, seed=seed, split=split, family=family, kind=kind, mutations=muts,
                    key=key, selectors=selectors, role=role, pass_role=rng.random() < 0.4, text=text,
                    intent=intent, first_use=False,
                    concept=concept, uid=uid, item=item, lang1=site.lang, lang2=lang2,
                    v1=v1, v2=v2, v1_url=v1_url, v2_url=v2_url, url_mode=url_mode,
                    positive=truth, why=why)


def first_use(sc, rng):
    """The scenario as a key with no selector yet: intent= and role= only, on the v2 page.
    The model finds the element from the words alone (or says it is not there)."""
    out = Scenario(sc)
    out['id'] = sc['id'] + '-intent'
    out['first_use'] = True
    out['selectors'] = []
    out['text'] = None
    out['pass_role'] = True
    out['intent'] = make_intent(rng, sc['concept'], None if sc['item'] else None, sc['item']) \
        if rng.random() < 0.5 else (sc.get('intent') or make_intent(rng, sc['concept'], None, sc['item']))
    out['mutations'] = list(sc['mutations']) + ['first_use']
    if not sc['positive'] and sc['why'] == 'moved':
        out['why'] = 'removed'
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--show', type=int, default=3)
    parser.add_argument('--split', default='train')
    parser.add_argument('--seed', type=int, default=0)
    parser.add_argument('--html', action='store_true', help='print the pages too')
    args = parser.parse_args(argv)
    shown = 0
    seed = args.seed
    while shown < args.show:
        for sc in make(seed, args.split):
            info = {k: v for k, v in sc.items() if k not in ('v1', 'v2')}
            print(json.dumps(info, ensure_ascii=False))
            if args.html:
                print('--- v1\n' + sc['v1'] + '\n--- v2\n' + sc['v2'])
            shown += 1
        seed += 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
