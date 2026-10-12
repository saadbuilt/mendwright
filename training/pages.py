"""A tiny DOM, site styles and the page families the generator builds.

A page is built by a family function from a Gen context: the site (brand, style,
language) and a content seed. Building the same family twice with the same content seed
gives the same page; a redesign changes the style, the language or per-control overrides
(see Gen.over), and gen_scenarios.py then edits the tree (moves, wraps, decoys).

Every actionable control gets a uid that names its meaning on the page ("login.submit",
"row3.edit"), which is how a control in the old page is found again in the new one.
"""

import hashlib
import html as _html
import random

from lexicon import (ARTICLE_TITLES, BRAND_NAMES, CATEGORIES, CONCEPTS, FIRST, LAST, PHRASES,
                     PRODUCTS, PROJECTS, ROLES_TEXT, STATUS_TEXT)

VOID = frozenset({'input', 'img', 'br', 'hr', 'meta', 'link', 'source', 'col'})
BLOCK = frozenset({'div', 'section', 'form', 'header', 'footer', 'nav', 'main', 'aside', 'ul',
                   'ol', 'li', 'table', 'thead', 'tbody', 'tr', 'fieldset', 'article', 'dialog',
                   'p', 'h1', 'h2', 'h3', 'h4', 'label'})


def stable(*parts):
    """A stable integer from the parts (Python's hash() is salted per process)."""
    digest = hashlib.blake2b('\x1f'.join(str(p) for p in parts).encode('utf-8'), digest_size=8)
    return int.from_bytes(digest.digest(), 'big')


def srng(*parts):
    return random.Random(stable(*parts))


def b36(n, width=0):
    chars = '0123456789abcdefghijklmnopqrstuvwxyz'
    out = ''
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if n == 0:
            break
    return out.rjust(width, '0')


class El:
    """One element. uid is set on actionable controls; unit names the control whose group
    (label + field + hint) this element is, so a move takes the whole group along."""

    __slots__ = ('tag', 'attrs', 'kids', 'uid', 'unit', 'concept', 'item')

    def __init__(self, tag, attrs=None, *kids, uid=None, unit=None, concept=None, item=None):
        self.tag = tag
        self.attrs = {k: v for k, v in (attrs or {}).items() if v is not None and v is not False}
        self.kids = [k for k in kids if k is not None and k != '']
        self.uid = uid
        self.unit = unit
        self.concept = concept
        self.item = item

    def add(self, *kids):
        self.kids.extend(k for k in kids if k is not None and k != '')
        return self

    def walk(self):
        yield self
        for kid in self.kids:
            if isinstance(kid, El):
                yield from kid.walk()

    def clone(self):
        out = El(self.tag, dict(self.attrs), uid=self.uid, unit=self.unit, concept=self.concept,
                 item=self.item)
        out.kids = [k.clone() if isinstance(k, El) else k for k in self.kids]
        return out


def h(tag, attrs=None, *kids, **kw):
    return El(tag, attrs, *kids, **kw)


def render(node):
    out = []
    _render(node, out)
    return ''.join(out)


def _render(node, out):
    if node is None:
        return
    if isinstance(node, str):
        out.append(_html.escape(node, quote=False))
        return
    parts = ['<', node.tag]
    for key, value in node.attrs.items():
        if value is True:
            parts.append(' ' + key)
        else:
            parts.append(' ' + key + '="' + _html.escape(str(value), quote=True) + '"')
    parts.append('>')
    out.append(''.join(parts))
    if node.tag in VOID:
        return
    for kid in node.kids:
        _render(kid, out)
    out.append('</' + node.tag + '>')
    if node.tag in BLOCK:
        out.append('\n')


def parent_map(root):
    out = {}
    for el in root.walk():
        for kid in el.kids:
            if isinstance(kid, El):
                out[id(kid)] = el
    return out


def find_uid(root, uid):
    for el in root.walk():
        if el.uid == uid:
            return el
    return None


def find_unit(root, uid):
    """The outermost element that is the group of control uid (or the control itself)."""
    best = None
    for el in root.walk():
        if el.unit == uid:
            return el
        if el.uid == uid and best is None:
            best = el
    return best


# --- styles ------------------------------------------------------------------------------------

CLASS_SCHEMES = ('bem', 'utility', 'module', 'styled', 'emotion', 'bootstrap', 'plain', 'none')
ID_MODES = ('semantic', 'semantic', 'mixed', 'generated', 'sparse')
TESTID_ATTRS = ('data-testid', 'data-testid', 'data-test', 'data-qa', 'data-cy', 'data-test-id', None, None)
LABEL_MODES = ('for', 'for', 'wrap', 'aria', 'placeholder', 'labelledby', 'sibling')
WRAPPERS = (None, None, None, 'mui', 'mat', 'ion', 'custom', 'chakra')
CASES = ('kebab', 'kebab', 'camel', 'snake')


class Style:
    """How a site writes its markup. A redesign changes some of these."""

    FIELDS = ('cls', 'hash_seed', 'ids', 'id_seed', 'case', 'testid', 'testid_rate', 'labels',
              'buttons', 'wrapper', 'names', 'icons', 'shell')

    def __init__(self, rng):
        self.cls = rng.choice(CLASS_SCHEMES)
        self.hash_seed = rng.randrange(1 << 30)
        self.ids = rng.choice(ID_MODES)
        self.id_seed = rng.randrange(1 << 30)
        self.case = rng.choice(CASES)
        self.testid = rng.choice(TESTID_ATTRS)
        self.testid_rate = rng.choice((0.3, 0.6, 0.9))
        self.labels = rng.choice(LABEL_MODES)
        self.buttons = rng.choice(('button', 'button', 'button', 'mixed', 'divs'))
        self.wrapper = rng.choice(WRAPPERS)
        self.names = rng.random() < 0.75
        self.icons = rng.random() < 0.5
        self.shell = rng.choice(('react', 'vue', 'angular', 'plain', 'next', 'svelte'))

    def copy(self):
        out = Style.__new__(Style)
        for name in self.FIELDS:
            setattr(out, name, getattr(self, name))
        return out


_UTILITY = {
    'button': 'inline-flex items-center justify-center rounded-md px-4 py-2 text-sm font-semibold',
    'primary': 'bg-indigo-600 text-white hover:bg-indigo-500',
    'secondary': 'bg-white text-gray-900 ring-1 ring-inset ring-gray-300',
    'danger': 'bg-red-600 text-white hover:bg-red-500',
    'icon': 'rounded-full p-1.5 text-gray-400 hover:text-gray-600',
    'input': 'block w-full rounded-md border-0 py-1.5 shadow-sm ring-1 ring-gray-300',
    'checkbox': 'h-4 w-4 rounded border-gray-300 text-indigo-600',
    'select': 'mt-2 block w-full rounded-md py-1.5 pl-3 pr-10',
    'label': 'block text-sm font-medium leading-6 text-gray-900',
    'field': 'sm:col-span-4',
    'link': 'text-sm font-semibold text-indigo-600 hover:text-indigo-500',
    'nav-link': 'rounded-md px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50',
    'card': 'group relative rounded-lg border border-gray-200 bg-white p-4',
    'row': 'border-b border-gray-200',
    'header': 'bg-white shadow-sm',
    'footer': 'bg-gray-900 text-gray-400',
    'form': 'space-y-6',
    'section': 'mx-auto max-w-7xl px-4 py-8',
    'dialog': 'fixed inset-0 z-50 flex items-center justify-center',
    'banner': 'fixed bottom-0 inset-x-0 z-40 bg-white p-4 shadow-lg',
    'title': 'text-2xl font-bold tracking-tight text-gray-900',
}
_BOOTSTRAP = {
    'button': 'btn', 'primary': 'btn-primary', 'secondary': 'btn-outline-secondary',
    'danger': 'btn-danger', 'icon': 'btn btn-sm btn-link', 'input': 'form-control',
    'checkbox': 'form-check-input', 'select': 'form-select', 'label': 'form-label',
    'field': 'mb-3', 'link': 'link-primary', 'nav-link': 'nav-link', 'card': 'card',
    'row': 'align-middle', 'header': 'navbar navbar-expand-lg', 'footer': 'footer mt-auto py-3',
    'form': 'needs-validation', 'section': 'container', 'dialog': 'modal d-block',
    'banner': 'fixed-bottom bg-light p-3', 'title': 'h3',
}


def _hash(seed, *parts, n=6, alpha=False):
    value = stable(seed, *parts)
    if alpha:
        letters = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
        out = ''
        for _ in range(n):
            value, r = divmod(value, len(letters))
            out += letters[r]
        return out
    return b36(value)[-n:]


def classes(style, comp, variant='', block='app'):
    """The class attribute for a component in a style, or None."""
    scheme = style.cls
    if scheme == 'none':
        return None
    seed = style.hash_seed
    if scheme == 'bem':
        name = block + '__' + comp
        return name + (' ' + name + '--' + variant if variant else '')
    if scheme == 'utility':
        base = _UTILITY.get(comp, '')
        extra = _UTILITY.get(variant, '') if variant else ''
        return (base + ' ' + extra).strip() or None
    if scheme == 'bootstrap':
        base = _BOOTSTRAP.get(comp, comp)
        extra = _BOOTSTRAP.get(variant, '') if variant else ''
        return (base + ' ' + extra).strip()
    if scheme == 'module':
        comp_name = ''.join(p.capitalize() for p in comp.split('-'))
        out = comp_name + '_root__' + _hash(seed, comp, n=5)
        if variant:
            out += ' ' + comp_name + '_' + variant + '__' + _hash(seed, comp, variant, n=5)
        return out
    if scheme == 'styled':
        return 'sc-' + _hash(seed, comp, n=6, alpha=True) + ' ' + _hash(seed, comp, variant, 'b', n=6, alpha=True)
    if scheme == 'emotion':
        return 'css-' + _hash(seed, comp, variant, n=7)
    # plain
    return comp + (' ' + variant if variant else '')


# --- the build context -----------------------------------------------------------------------------

class Site:
    """A website: brand, base URL, language and style. Kept across the two versions of a page
    unless the redesign changes them."""

    def __init__(self, rng, split):
        from lexicon import BRAND_SUFFIX, SYLLABLES
        syl = SYLLABLES[split]
        name = ''.join(rng.choice(syl) for _ in range(rng.choice((2, 2, 3))))
        name = name + rng.choice(BRAND_SUFFIX)
        self.brand = name.capitalize()
        self.host = 'https://' + name.lower() + '.example'
        self.style = Style(rng)
        self.lang = rng.choices(('en', 'de', 'fr', 'es', 'pt', 'it', 'nl'),
                                weights=(62, 7, 7, 7, 6, 5, 6))[0]
        self.split = split


class Gen:
    """Everything a family function needs to build one page."""

    def __init__(self, site, family, seed, *, style=None, lang=None, over=None, split='train',
                 marker_uid=None, marker_attr=None):
        self.site = site
        self.family = family
        self.seed = seed
        self.style = style or site.style
        self.lang = lang or site.lang
        self.over = over or {}
        self.split = split
        self.rng = srng(seed, family, 'content')    # content choices: same in v1 and v2
        self.controls = []
        self.marker_uid = marker_uid
        self.marker_attr = marker_attr
        self._ids = set()
        self._gen_counter = stable(seed, self.style.id_seed) % 900 + 100
        self.block = family
        # A dense page has a mega menu, a big footer and long lists, so a role can have
        # more candidates than the model is shown.
        self.dense = srng(seed, family, 'dense').random() < 0.25

    # words ---------------------------------------------------------------------------------------

    def word(self, uid, concept, lang=None):
        c = CONCEPTS[concept]
        variants = c.variants(lang or self.lang)
        base = stable(self.seed, uid, 'word') % len(variants)
        shift = self.over.get(uid, {}).get('word', 0)
        return variants[(base + shift) % len(variants)]

    def phrase(self, key, salt='', **fmt):
        variants = PHRASES[key].get(self.lang) or PHRASES[key]['en']
        text = variants[stable(self.seed, key, salt) % len(variants)]
        if '{brand}' in text:
            text = text.replace('{brand}', self.site.brand)
        for k, v in fmt.items():
            text = text.replace('{' + k + '}', v)
        return text

    def pick(self, seq, *salt):
        return seq[stable(self.seed, self.family, *salt) % len(seq)]

    # identity --------------------------------------------------------------------------------------

    def _case(self, words):
        case = self.style.case
        if case == 'camel':
            return words[0] + ''.join(w.capitalize() for w in words[1:])
        if case == 'snake':
            return '_'.join(words)
        return '-'.join(words)

    def _generated_id(self, kind, salt):
        self._gen_counter += 1 + stable(salt) % 3
        n = self._gen_counter
        form = stable(self.style.id_seed, 'genform') % 6
        if form == 0:
            return ':r' + b36(n % 1296) + ':'
        if form == 1:
            return (kind.split(':')[0] if kind else 'field') + '-' + str(1000 + n * 7 % 9000)
        if form == 2:
            return 'mat-input-' + str(n % 40)
        if form == 3:
            return 'ember' + str(n)
        if form == 4:
            return 'headlessui-' + (kind.split(':')[0] if kind else 'control') + '-' + str(n % 60)
        return 'f' + _hash(self.style.id_seed, salt, n=6)

    def _unique(self, ident):
        base = ident
        k = 2
        while ident in self._ids:
            ident = base + '-' + str(k)
            k += 1
        self._ids.add(ident)
        return ident

    def slug_words(self, uid, concept, area=None):
        c = CONCEPTS[concept]
        r = srng(self.seed, uid, 'slug', self.over.get(uid, {}).get('slug', 0))
        slug = r.choice(c.slugs).split('-')
        if area and r.random() < 0.45:
            slug = area.split('-') + slug
        return slug

    def ident(self, uid, concept, kind, area=None, force=None):
        """The id of a control, or None."""
        o = self.over.get(uid, {})
        mode = o.get('id') or force
        r = srng(self.seed, uid, 'id', self.style.id_seed)
        if mode is None:
            ids = self.style.ids
            roll = r.random()
            if ids == 'semantic':
                mode = 'semantic' if roll < 0.85 else 'none'
            elif ids == 'mixed':
                mode = 'semantic' if roll < 0.45 else ('generated' if roll < 0.7 else 'none')
            elif ids == 'generated':
                mode = 'generated' if roll < 0.8 else 'none'
            else:
                mode = 'semantic' if roll < 0.25 else 'none'
        if mode == 'none':
            return None
        if mode == 'generated':
            return self._unique(self._generated_id(kind, uid))
        words = self.slug_words(uid, concept, area)
        if mode == 'rename':
            alt = srng(self.seed, uid, 'rename').choice(CONCEPTS[concept].slugs).split('-')
            prefix = srng(self.seed, uid, 'renamepfx').choice(
                (['btn'], ['input'], ['field'], ['js'], ['ctl'], [], ['new'], ['v2'], ['main']))
            words = prefix + alt if alt != words or prefix else alt + ['v2']
            if words == self.slug_words(uid, concept, area):
                words = words + ['2']
        return self._unique(self._case(words))

    def testid(self, uid, concept, area=None):
        o = self.over.get(uid, {})
        attr = o.get('testid_attr', self.style.testid)
        if o.get('testid') == 'none' or attr is None:
            return None, None
        r = srng(self.seed, uid, 'testid')
        if o.get('testid') != 'force' and r.random() > self.style.testid_rate:
            return None, None
        words = self.slug_words(uid, concept, area)
        if o.get('testid') == 'rename':
            words = srng(self.seed, uid, 'testid-rename').choice(CONCEPTS[concept].slugs).split('-') + ['btn' if 'button' in CONCEPTS[concept].kind else 'el']
        return attr, '-'.join(words) if r.random() < 0.7 else '_'.join(words)

    def name_attr(self, uid, concept, area=None):
        o = self.over.get(uid, {})
        if o.get('name') == 'none' or not self.style.names:
            return None
        words = self.slug_words(uid, concept, None)
        if o.get('name') == 'rename':
            words = srng(self.seed, uid, 'name-rename').choice(CONCEPTS[concept].slugs).split('-') + ['field']
        r = srng(self.seed, uid, 'nameform')
        form = r.random()
        if form < 0.5:
            return '_'.join(words)
        if form < 0.75:
            return words[0] + ''.join(w.capitalize() for w in words[1:])
        if form < 0.9 and area:
            return area.split('-')[0] + '[' + '_'.join(words) + ']'
        return '-'.join(words)

    def cls(self, comp, variant=''):
        return classes(self.style, comp, variant, self.block)

    # controls --------------------------------------------------------------------------------------

    def _register(self, el, uid, concept, kind, area, item):
        el.uid = uid
        el.concept = concept
        el.item = item
        if self.marker_uid is not None and uid == self.marker_uid:
            el.attrs[self.marker_attr] = '1'
        self.controls.append({'uid': uid, 'concept': concept, 'kind': kind, 'area': area,
                              'item': item})
        return el

    def removed(self, uid):
        return bool(self.over.get(uid, {}).get('removed'))

    def button(self, uid, concept, *, area=None, variant='primary', item=None, text=None,
               aria=None, icon=None, submit=False, form=None):
        """A button-role control. form picks the markup: button, input, a, div, span."""
        if self.removed(uid):
            return None
        o = self.over.get(uid, {})
        label = text if text is not None else self.word(uid, concept)
        if item and text is None and o.get('itemtext'):
            label = label + ' ' + item
        r = srng(self.seed, uid, 'form', self.style.buttons)
        if form is None:
            form = o.get('form')
        if form is None:
            if self.style.buttons == 'button':
                form = 'button'
            elif self.style.buttons == 'divs':
                form = r.choice(('button', 'div', 'div', 'a'))
            else:
                form = r.choice(('button', 'button', 'input', 'a', 'div'))
            if form == 'input' and not submit:
                form = 'button'
        if icon is None:
            icon = o.get('icon')
        if icon is None:
            icon = False
        attrs = {'class': self.cls('icon' if icon else 'button', '' if icon else variant)}
        ident = self.ident(uid, concept, 'button', area)
        attrs['id'] = ident
        tattr, tval = self.testid(uid, concept, area)
        if tattr:
            attrs[tattr] = tval
        if aria is None and o.get('aria') == 'add':
            aria = label + ((' ' + item) if item else '')
        if icon:
            aria = aria or (label + ((' ' + item) if item else ''))
        if o.get('aria') == 'change' and aria:
            aria = aria + ' ' + srng(uid, 'aria').choice(('now', 'item', 'this'))
        if aria:
            attrs['aria-label'] = aria
        if o.get('title'):
            attrs['title'] = label
        icon_svg = h('svg', {'width': '16', 'height': '16', 'viewBox': '0 0 24 24', 'aria-hidden': 'true'},
                     h('path', {'d': 'M4 12h16M12 4v16'}))
        if form == 'input':
            attrs.update({'type': 'submit' if submit else 'button', 'value': label})
            el = h('input', attrs)
        elif form == 'a':
            attrs.update({'href': '#', 'role': 'button'})
            el = h('a', attrs, icon_svg if icon else label)
        elif form in ('div', 'span'):
            attrs.update({'role': 'button', 'tabindex': '0'})
            el = h(form, attrs, icon_svg if icon else label)
        else:
            attrs['type'] = 'submit' if submit else 'button'
            if icon:
                el = h('button', attrs, icon_svg)
            elif self.style.icons and r.random() < 0.25:
                el = h('button', attrs, icon_svg, h('span', None, label))
            else:
                el = h('button', attrs, label)
        el = self._register(el, uid, concept, 'button', area, item)
        return self._wrap_custom(el, uid, 'button')

    def link(self, uid, concept, href, *, area=None, item=None, text=None, variant='', aria=None,
             form=None):
        if self.removed(uid):
            return None
        o = self.over.get(uid, {})
        label = text if text is not None else self.word(uid, concept)
        attrs = {'class': self.cls('nav-link' if area in ('nav', 'header', 'sidebar') else 'link', variant)}
        attrs['id'] = self.ident(uid, concept, 'link', area) if srng(self.seed, uid, 'lid').random() < 0.35 else None
        if o.get('id') and attrs['id'] is None:
            attrs['id'] = self.ident(uid, concept, 'link', area)
        tattr, tval = self.testid(uid, concept, area)
        if tattr:
            attrs[tattr] = tval
        if o.get('href'):
            href = o['href']
        if aria or o.get('aria') == 'add':
            attrs['aria-label'] = aria or label
        form = form or o.get('form') or 'a'
        if form == 'span':
            attrs.update({'role': 'link', 'tabindex': '0', 'data-href': href})
            el = h('span', attrs, label)
        elif form == 'button':
            attrs.update({'role': 'link', 'type': 'button'})
            el = h('button', attrs, label)
        else:
            attrs['href'] = href
            if srng(self.seed, uid, 'lspan').random() < 0.2:
                el = h('a', attrs, h('span', None, label))
            else:
                el = h('a', attrs, label)
        return self._register(el, uid, concept, 'link', area, item)

    def _label_mode(self, uid):
        o = self.over.get(uid, {})
        if o.get('labels'):
            return o['labels']
        mode = self.style.labels
        r = srng(self.seed, uid, 'lm', mode)
        if r.random() < 0.15:
            mode = r.choice(LABEL_MODES)
        return mode

    def field(self, uid, concept, *, area=None, item=None, required=False, hint=None, value=None,
              options=None):
        """A labelled form control for input/select/textarea concepts, inside its group."""
        if self.removed(uid):
            return None
        c = CONCEPTS[concept]
        kind = c.kind
        o = self.over.get(uid, {})
        label = self.word(uid, concept)
        ident = self.ident(uid, concept, kind, area)
        mode = self._label_mode(uid)
        if mode == 'for' and not ident:
            mode = 'wrap'
        attrs = {}
        if kind.startswith('input:'):
            typ = kind.split(':', 1)[1]
            tag = 'input'
            attrs['type'] = typ
            attrs['class'] = self.cls('input')
        elif kind == 'textarea':
            tag = 'textarea'
            attrs['class'] = self.cls('input', 'multiline')
            attrs['rows'] = '4'
        else:
            tag = 'select'
            attrs['class'] = self.cls('select')
        form = o.get('form')
        if form == 'div' and tag == 'input':
            tag = 'div'
            attrs = {'class': self.cls('input'), 'role': 'textbox', 'contenteditable': 'true',
                     'tabindex': '0'}
        elif form == 'div' and tag == 'select':
            tag = 'div'
            attrs = {'class': self.cls('select'), 'role': 'combobox', 'aria-expanded': 'false',
                     'tabindex': '0', 'aria-haspopup': 'listbox'}
        elif form == 'input' and tag == 'select':
            tag = 'input'
            attrs = {'class': self.cls('select'), 'role': 'combobox', 'type': 'text',
                     'aria-autocomplete': 'list'}
        attrs['id'] = ident
        attrs['name'] = self.name_attr(uid, concept, area) if tag in ('input', 'select', 'textarea') else None
        tattr, tval = self.testid(uid, concept, area)
        if tattr:
            attrs[tattr] = tval
        if required:
            attrs['required'] = True
        if concept in ('email', 'password', 'first_name', 'last_name', 'phone', 'address', 'city',
                       'postcode', 'username', 'full_name') and tag == 'input':
            auto = {'email': 'email', 'password': 'current-password', 'first_name': 'given-name',
                    'last_name': 'family-name', 'phone': 'tel', 'address': 'address-line1',
                    'city': 'address-level2', 'postcode': 'postal-code', 'username': 'username',
                    'full_name': 'name'}[concept]
            if srng(self.seed, uid, 'auto').random() < 0.5:
                attrs['autocomplete'] = auto
        if value is not None and tag == 'input':
            attrs['value'] = value
        label_el = None
        if mode == 'aria':
            attrs['aria-label'] = label
        elif mode == 'placeholder' and tag in ('input', 'textarea'):
            attrs['placeholder'] = label
        elif mode == 'labelledby':
            lid = self._unique('lbl-' + (ident or _hash(self.seed, uid, n=5)))
            attrs['aria-labelledby'] = lid
            label_el = h('span', {'id': lid, 'class': self.cls('label')}, label)
        elif mode == 'for':
            label_el = h('label', {'for': ident, 'class': self.cls('label')}, label)
        elif mode == 'sibling':
            label_el = h('span', {'class': self.cls('label')}, label)
        if o.get('placeholder') and tag in ('input', 'textarea'):
            attrs['placeholder'] = o['placeholder']
        elif tag == 'input' and 'placeholder' not in attrs and srng(self.seed, uid, 'ph').random() < 0.3:
            attrs['placeholder'] = {'email': 'you@example.com', 'phone': '+1 555 0100',
                                    'search': label + '…', 'coupon_code': 'SAVE10'}.get(concept, label)
        if tag == 'select' or attrs.get('role') == 'combobox':
            opts = options or ['-', 'One', 'Two', 'Three']
            control = h(tag, attrs, *[h('option', None, o_) for o_ in opts]) if tag == 'select' else h(tag, attrs)
        elif tag == 'textarea':
            control = h('textarea', attrs)
        else:
            control = h(tag, attrs)
        self._register(control, uid, concept, kind, area, item)
        hint_el = h('p', {'class': self.cls('hint')}, hint) if hint else None
        wrapper = self.style.wrapper
        if mode == 'wrap':
            inner = h('label', {'class': self.cls('label')}, h('span', None, label), control)
            group = h('div', {'class': self.cls('field')}, inner, hint_el, unit=uid)
        elif wrapper == 'mui':
            group = h('div', {'class': 'MuiFormControl-root MuiTextField-root'},
                      label_el if label_el is not None and label_el.tag == 'label' else label_el,
                      h('div', {'class': 'MuiInputBase-root MuiOutlinedInput-root'}, control),
                      hint_el, unit=uid)
        elif wrapper == 'mat':
            group = h('mat-form-field', {'class': 'mat-mdc-form-field', 'appearance': 'outline'},
                      label_el, h('div', {'class': 'mat-mdc-form-field-infix'}, control), hint_el,
                      unit=uid)
        elif wrapper == 'ion':
            group = h('ion-item', None, label_el, h('ion-input', None, control), unit=uid)
        elif wrapper == 'chakra':
            group = h('div', {'class': 'chakra-form-control', 'role': 'group'}, label_el, control,
                      hint_el, unit=uid)
        elif wrapper == 'custom':
            group = h('app-form-field', None, label_el, control, hint_el, unit=uid)
        else:
            group = h('div', {'class': self.cls('field')}, label_el, control, hint_el, unit=uid)
        return group

    def check(self, uid, concept, *, area=None, item=None, text=None, checked=False, radio_name=None):
        """A checkbox, switch or radio with its label."""
        if self.removed(uid):
            return None
        c = CONCEPTS[concept]
        kind = c.kind
        o = self.over.get(uid, {})
        label = text if text is not None else self.word(uid, concept)
        ident = self.ident(uid, concept, kind, area)
        mode = self._label_mode(uid)
        if mode in ('aria', 'placeholder'):
            mode = 'for' if ident else 'wrap'
        if mode == 'for' and not ident:
            mode = 'wrap'
        form = o.get('form')
        if kind == 'radio':
            attrs = {'type': 'radio', 'class': self.cls('checkbox', 'radio'), 'id': ident,
                     'name': radio_name or self.name_attr(uid, concept, area) or 'choice'}
            tag = 'input'
            if form == 'div':
                tag = 'div'
                attrs = {'role': 'radio', 'aria-checked': 'true' if checked else 'false',
                         'tabindex': '0', 'class': self.cls('checkbox', 'radio'), 'id': ident}
        else:
            attrs = {'type': 'checkbox', 'class': self.cls('checkbox'), 'id': ident,
                     'name': self.name_attr(uid, concept, area)}
            tag = 'input'
            if kind == 'switch' and form is None:
                form = srng(self.seed, uid, 'switch').choice(('input', 'input', 'switch-input', 'div'))
            if form == 'switch-input':
                attrs['role'] = 'switch'
            elif form == 'div':
                tag = 'div'
                role = 'switch' if kind == 'switch' else 'checkbox'
                attrs = {'role': role, 'aria-checked': 'true' if checked else 'false',
                         'tabindex': '0', 'class': self.cls('checkbox', 'switch'), 'id': ident}
        if checked and tag == 'input':
            attrs['checked'] = True
        tattr, tval = self.testid(uid, concept, area)
        if tattr:
            attrs[tattr] = tval
        label_el = None
        if mode == 'labelledby' or tag == 'div':
            lid = self._unique('lbl-' + (ident or _hash(self.seed, uid, n=5)))
            attrs['aria-labelledby'] = lid
            label_el = h('span', {'id': lid, 'class': self.cls('label')}, label)
        control = self._register(h(tag, attrs), uid, concept, kind, area, item)
        if mode == 'wrap' and tag == 'input':
            group = h('div', {'class': self.cls('field', 'check')},
                      h('label', {'class': self.cls('label', 'check')}, control, ' ', label), unit=uid)
        elif mode == 'for' and tag == 'input':
            group = h('div', {'class': self.cls('field', 'check')}, control,
                      h('label', {'for': ident, 'class': self.cls('label', 'check')}, label), unit=uid)
        elif label_el is not None:
            group = h('div', {'class': self.cls('field', 'check')}, control, label_el, unit=uid)
        else:
            group = h('div', {'class': self.cls('field', 'check')}, control,
                      h('span', {'class': self.cls('label')}, label), unit=uid)
        return group

    def _wrap_custom(self, el, uid, kind):
        o = self.over.get(uid, {})
        if self.style.wrapper == 'custom' and srng(self.seed, uid, 'cw').random() < 0.3:
            return h(self.pick(('app-button', 'ui-button', 'x-button', 'my-button'), 'cw'), None, el, unit=uid)
        if self.style.wrapper == 'ion' and srng(self.seed, uid, 'cw').random() < 0.3:
            return h('ion-buttons', None, el, unit=uid)
        if o.get('slot_wrap'):
            return h('span', {'class': self.cls('slot')}, el, unit=uid)
        return el

    # text ------------------------------------------------------------------------------------------

    def heading(self, level, text, cls='title'):
        return h('h' + str(level), {'class': self.cls(cls)}, text)

    def para(self, text):
        return h('p', {'class': self.cls('text')}, text)


# --- entities ----------------------------------------------------------------------------------

def products(g, n):
    pool = list(PRODUCTS[g.split])
    g.rng.shuffle(pool)
    return pool[:n]


def people(g, n):
    firsts = list(FIRST[g.split])
    lasts = list(LAST[g.split])
    g.rng.shuffle(firsts)
    g.rng.shuffle(lasts)
    return [firsts[i % len(firsts)] + ' ' + lasts[i % len(lasts)] for i in range(n)]


def projects(g, n):
    pool = list(PROJECTS[g.split])
    g.rng.shuffle(pool)
    return pool[:n]


def slugify(text):
    out = ''.join(ch.lower() if ch.isalnum() else '-' for ch in text)
    while '--' in out:
        out = out.replace('--', '-')
    return out.strip('-')


def price(g):
    return '$' + str(g.rng.randrange(9, 240)) + '.' + g.rng.choice(('00', '50', '99', '95'))


# --- chrome ------------------------------------------------------------------------------------

def site_header(g, *, shop=False, app=False, minimal=False):
    g.block = 'site-header'
    logo = g.link('hdr.home', 'home', '/', area='header', text=g.site.brand,
                  aria=g.site.brand + ' home' if g.rng.random() < 0.4 else None)
    if minimal:
        return h('header', {'class': g.cls('header')}, logo)
    links = []
    concepts = ['products', 'pricing', 'features', 'docs', 'blog', 'customers', 'integrations', 'about']
    if shop:
        concepts = ['products', 'about', 'blog', 'help', 'shipping_info', 'returns']
    picked = [c for c in concepts if g.rng.random() < 0.65][:6] or concepts[:3]
    for concept in picked:
        links.append(h('li', {'class': g.cls('nav-item')},
                       g.link('hdr.' + concept, concept, '/' + CONCEPTS[concept].slugs[0], area='nav')))
    if g.dense:
        cats = list(CATEGORIES)
        g.rng.shuffle(cats)
        for group in range(g.rng.randrange(2, 4)):
            sub = [h('li', None, g.link('mega.' + str(group) + '.' + slugify(c), 'products',
                                        '/c/' + slugify(c) + ('-' + str(group) if group else ''),
                                        area='nav', text=c + ('' if group == 0 else ' ' + ('Sale', 'New', 'Kids')[group - 1]),
                                        item=c))
                   for c in cats[:g.rng.randrange(6, 12)]]
            links.append(h('li', {'class': g.cls('nav-item', 'mega')},
                           h('button', {'type': 'button', 'aria-expanded': 'false', 'class': g.cls('nav-link')},
                             ('Shop', 'Collections', 'Outlet')[group]),
                           h('ul', {'class': g.cls('mega-menu'), 'hidden': True if g.rng.random() < 0.3 else None}, *sub)))
    nav = h('nav', {'class': g.cls('nav'), 'aria-label': 'Main'},
            h('ul', {'class': g.cls('nav-list')}, *links))
    tools = []
    if shop or g.rng.random() < 0.3:
        tools.append(h('div', {'role': 'search', 'class': g.cls('search')},
                       g.field('hdr.search', 'search', area='header')))
    if shop:
        tools.append(g.link('hdr.cart', 'view_cart', '/cart', area='header'))
        tools.append(g.link('hdr.account', 'account_link', '/account', area='header'))
    elif not app:
        tools.append(g.link('hdr.sign_in', 'sign_in_link', '/login', area='header'))
        tools.append(g.button('hdr.get_started', 'get_started', area='header'))
    header = h('header', {'class': g.cls('header')},
               h('div', {'class': g.cls('container')}, logo, nav, h('div', {'class': g.cls('tools')}, *tools)))
    if g.rng.random() < 0.35 or g.over.get('_mobile_nav'):
        # A mobile drawer that repeats the main links, hidden on desktop.
        copies = [h('li', None, g.link('mob.' + c, c, '/' + CONCEPTS[c].slugs[0], area='nav'))
                  for c in picked[:4]]
        hide = g.pick(({'hidden': True}, {'style': 'display:none'}, {'class': 'mobile-menu d-none'},
                       {'aria-hidden': 'true', 'style': 'display: none'}), 'mob')
        header.add(h('div', dict(hide, id=None), h('ul', None, *copies)))
    return header


def app_shell(g, main_kids, title):
    """Sidebar + top bar app layout."""
    g.block = 'sidebar'
    side_links = []
    for concept, path in (('home', '/dashboard'), ('products', '/projects'), ('customers', '/customers'),
                          ('integrations', '/integrations'), ('help', '/help')):
        if g.rng.random() < 0.8:
            side_links.append(g.link('side.' + concept, concept, path, area='sidebar'))
    side_links.append(g.link('side.settings', 'account_link', '/settings', area='sidebar',
                             text=g.phrase('h_settings')))
    sidebar = h('nav', {'class': g.cls('sidebar'), 'aria-label': 'Sidebar'},
                h('div', {'class': g.cls('workspace')}, h('span', None, g.site.brand)), *side_links)
    g.block = 'topbar'
    top = h('header', {'class': g.cls('header', 'app')},
            h('div', {'role': 'search'}, g.field('top.search', 'search', area='header')) if g.rng.random() < 0.6 else None,
            g.button('top.notifications', 'notifications', area='header', icon=True, variant=''),
            g.link('top.help', 'help', '/help', area='header') if g.rng.random() < 0.5 else None,
            g.button('top.account', 'account_menu', area='header', icon=g.rng.random() < 0.5, variant=''))
    main = h('main', {'class': g.cls('main')}, g.heading(1, title), *main_kids)
    return [h('div', {'class': g.cls('shell')}, top, h('div', {'class': g.cls('body')}, sidebar, main))]


def site_footer(g):
    g.block = 'site-footer'
    cols = []
    seen = set()
    groups = (('f_company', ('about', 'careers', 'press', 'contact', 'customers')),
              ('f_resources', ('blog', 'docs', 'help', 'status', 'changelog')),
              ('f_legal', ('privacy', 'terms', 'cookie_policy', 'security')))
    if g.dense:
        groups = groups + (('f_support', ('faq', 'shipping_info', 'returns', 'track_order', 'orders',
                                          'contact', 'help')),
                           ('f_resources', ('integrations', 'docs', 'features', 'pricing', 'customers',
                                            'blog', 'changelog', 'status')))
    for key, concepts in groups:
        if g.rng.random() < 0.15:
            continue
        items = [h('li', None, g.link('ftr.' + c + ('' if key not in seen else '.' + str(len(seen))), c,
                                      '/' + CONCEPTS[c].slugs[0] + ('' if key not in seen else '/all'), area='footer'))
                 for c in concepts if g.rng.random() < (0.9 if g.dense else 0.75)]
        seen.add(key)
        cols.append(h('div', {'class': g.cls('footer-col')}, h('h4', None, g.phrase(key)),
                      h('ul', None, *items)))
    return h('footer', {'class': g.cls('footer')}, h('div', {'class': g.cls('container')}, *cols),
             h('p', None, '© 2026 ' + g.site.brand))


# --- families ----------------------------------------------------------------------------------

def fam_login(g):
    g.block = 'login-form'
    uid_pref = 'login.'
    rows = []
    if g.rng.random() < 0.4:
        rows.append(h('div', {'class': g.cls('social')},
                      g.button(uid_pref + 'google', 'google_sign_in', area='login', variant='secondary'),
                      g.button(uid_pref + 'apple', 'apple_sign_in', area='login', variant='secondary')
                      if g.rng.random() < 0.5 else None))
    ident_concept = 'username' if g.rng.random() < 0.2 else 'email'
    rows.append(g.field(uid_pref + 'email', ident_concept, area='login', required=True))
    pw = g.field(uid_pref + 'password', 'password', area='login', required=True)
    if g.rng.random() < 0.4 and pw is not None:
        pw.add(g.button(uid_pref + 'show', 'show_password', area='login', icon=True, variant=''))
    rows.append(pw)
    rows.append(h('div', {'class': g.cls('row', 'between')},
                  g.check(uid_pref + 'remember', 'remember_me', area='login') if g.rng.random() < 0.7 else None,
                  g.link(uid_pref + 'forgot', 'forgot_password', '/forgot-password', area='login')))
    rows.append(g.button(uid_pref + 'submit', 'sign_in', area='login', submit=True))
    form = h('form', {'class': g.cls('form'), 'id': g.pick(('login-form', 'signin', None, 'loginForm'), 'fid'),
                      'action': '/session', 'method': 'post'}, *rows)
    card = h('section', {'class': g.cls('card', 'auth')}, g.heading(1, g.phrase('h_login')), form,
             h('p', None, g.link(uid_pref + 'signup', 'sign_up_link', '/signup', area='login')))
    return [site_header(g, minimal=g.rng.random() < 0.5), h('main', {'class': g.cls('main')}, card),
            site_footer(g) if g.rng.random() < 0.5 else None], 'login'


def fam_signup(g):
    g.block = 'signup-form'
    p = 'signup.'
    rows = []
    if g.rng.random() < 0.5:
        rows.append(h('div', {'class': g.cls('row', 'split')},
                      g.field(p + 'first', 'first_name', area='signup', required=True),
                      g.field(p + 'last', 'last_name', area='signup', required=True)))
    else:
        rows.append(g.field(p + 'name', 'full_name', area='signup', required=True))
    rows.append(g.field(p + 'email', 'email', area='signup', required=True))
    if g.rng.random() < 0.4:
        rows.append(g.field(p + 'company', 'company', area='signup'))
    if g.rng.random() < 0.3:
        rows.append(g.field(p + 'phone', 'phone', area='signup'))
    rows.append(g.field(p + 'password', 'password', area='signup', required=True,
                        hint=g.pick(('At least 8 characters.', 'Use 8 or more characters with a number.', None), 'hint')))
    if g.rng.random() < 0.5:
        rows.append(g.field(p + 'confirm', 'confirm_password', area='signup', required=True))
    rows.append(g.check(p + 'terms', 'accept_terms', area='signup'))
    if g.rng.random() < 0.6:
        rows.append(g.check(p + 'news', 'newsletter_optin', area='signup'))
    rows.append(g.button(p + 'submit', 'sign_up' if g.rng.random() < 0.7 else 'continue', area='signup', submit=True))
    form = h('form', {'class': g.cls('form'), 'id': g.pick(('signup-form', 'register', None), 'fid'),
                      'novalidate': True}, *rows)
    aside = h('aside', {'class': g.cls('aside')}, h('p', None, g.link(p + 'login', 'sign_in_link', '/login', area='signup')),
              g.button(p + 'google', 'google_sign_in', area='signup', variant='secondary') if g.rng.random() < 0.5 else None)
    return [site_header(g), h('main', {'class': g.cls('main')},
                               h('section', {'class': g.cls('card')}, g.heading(1, g.phrase('h_signup')), form, aside)),
            site_footer(g)], 'signup'


def _order_summary(g, items):
    rows = []
    for name in items:
        rows.append(h('li', {'class': g.cls('summary-item')}, h('span', None, name), h('span', None, price(g))))
    return h('aside', {'class': g.cls('summary'), 'aria-label': g.phrase('h_summary')},
             h('h2', None, g.phrase('h_summary')), h('ul', None, *rows),
             h('div', {'class': g.cls('coupon')},
               g.field('co.coupon', 'coupon_code', area='checkout'),
               g.button('co.apply', 'apply_coupon', area='checkout', variant='secondary')))


def fam_checkout(g):
    g.block = 'checkout'
    p = 'co.'
    contact = h('fieldset', {'class': g.cls('section')}, h('legend', None, g.phrase('h_contact_info')),
                g.field(p + 'email', 'email', area='checkout', required=True),
                g.check(p + 'news', 'newsletter_optin', area='checkout') if g.rng.random() < 0.5 else None)
    ship = h('fieldset', {'class': g.cls('section')}, h('legend', None, g.phrase('h_shipping')),
             g.field(p + 'country', 'country', area='shipping', options=['United States', 'Canada', 'Germany', 'France', 'Spain']),
             h('div', {'class': g.cls('row', 'split')},
               g.field(p + 'first', 'first_name', area='shipping', required=True),
               g.field(p + 'last', 'last_name', area='shipping', required=True)),
             g.field(p + 'address', 'address', area='shipping', required=True),
             g.field(p + 'address2', 'address2', area='shipping') if g.rng.random() < 0.6 else None,
             h('div', {'class': g.cls('row', 'split')},
               g.field(p + 'city', 'city', area='shipping', required=True),
               g.field(p + 'state', 'state', area='shipping', options=['-', 'California', 'Texas', 'New York']) if g.rng.random() < 0.5 else None,
               g.field(p + 'zip', 'postcode', area='shipping', required=True)),
             g.field(p + 'phone', 'phone', area='shipping') if g.rng.random() < 0.5 else None)
    method = h('fieldset', {'class': g.cls('section'), 'role': 'radiogroup'}, h('legend', None, g.phrase('h_shipping_method')),
               g.check(p + 'std', 'ship_standard', area='shipping', radio_name='shipping_method', checked=True),
               g.check(p + 'exp', 'ship_express', area='shipping', radio_name='shipping_method'))
    pay = h('fieldset', {'class': g.cls('section')}, h('legend', None, g.phrase('h_payment')),
            g.check(p + 'card', 'pay_card', area='payment', radio_name='payment', checked=True),
            g.check(p + 'paypal', 'pay_paypal', area='payment', radio_name='payment'),
            g.check(p + 'billing', 'same_as_billing', area='payment', checked=True))
    actions = h('div', {'class': g.cls('actions')},
                g.link(p + 'back', 'continue_shopping', '/cart', area='checkout') if g.rng.random() < 0.7 else None,
                g.button(p + 'place', 'place_order' if g.rng.random() < 0.6 else 'continue', area='checkout', submit=True))
    form = h('form', {'class': g.cls('form'), 'id': g.pick(('checkout-form', 'checkout', None), 'fid')},
             contact, ship, method, pay, actions)
    return [site_header(g, shop=True, minimal=g.rng.random() < 0.5),
            h('main', {'class': g.cls('main')}, g.heading(1, g.phrase('h_checkout')),
              h('div', {'class': g.cls('grid')}, form, _order_summary(g, products(g, g.rng.randrange(1, 4))))),
            ], 'checkout'


def fam_cart(g):
    g.block = 'cart'
    items = products(g, g.rng.randrange(2, 6))
    rows = []
    for i, name in enumerate(items):
        k = 'cart.' + slugify(name)
        g.block = 'cart-item'
        rows.append(h('li', {'class': g.cls('row', 'item'), 'data-sku': None if g.rng.random() < 0.5 else 'SKU-' + str(1000 + i)},
                      h('img', {'src': '/img/' + slugify(name) + '.jpg', 'alt': name}),
                      h('div', {'class': g.cls('item-info')},
                        g.link(k + '.title', 'products', '/products/' + slugify(name), area='cart', item=name, text=name),
                        h('span', {'class': g.cls('price')}, price(g))),
                      g.field(k + '.qty', 'quantity', area='cart', item=name, value='1'),
                      g.button(k + '.remove', 'remove_item', area='cart', item=name, variant='', icon=g.rng.random() < 0.4),
                      g.button(k + '.later', 'save_for_later', area='cart', item=name, variant='') if g.rng.random() < 0.4 else None))
    g.block = 'cart'
    summary = h('aside', {'class': g.cls('summary')}, h('h2', None, g.phrase('h_summary')),
                g.field('cart.coupon', 'coupon_code', area='cart') if g.rng.random() < 0.6 else None,
                g.button('cart.apply', 'apply_coupon', area='cart', variant='secondary') if g.rng.random() < 0.6 else None,
                g.button('cart.checkout', 'checkout', area='cart'),
                g.link('cart.continue', 'continue_shopping', '/products', area='cart'))
    tools = h('div', {'class': g.cls('toolbar')},
              g.button('cart.update', 'update_cart', area='cart', variant='secondary') if g.rng.random() < 0.5 else None,
              g.button('cart.empty', 'empty_cart', area='cart', variant='') if g.rng.random() < 0.4 else None)
    return [site_header(g, shop=True), h('main', {'class': g.cls('main')}, g.heading(1, g.phrase('h_cart')),
                                         h('ul', {'class': g.cls('list')}, *rows), tools, summary),
            site_footer(g)], 'cart'


def fam_grid(g):
    g.block = 'filters'
    brands = list(BRAND_NAMES)
    g.rng.shuffle(brands)
    filters = [g.check('flt.' + slugify(b), 'in_stock', area='filters', text=b, item=b) for b in brands[:g.rng.randrange(3, 6)]]
    side = h('aside', {'class': g.cls('filters'), 'aria-label': g.phrase('h_filters')},
             h('h2', None, g.phrase('h_filters')), h('fieldset', None, h('legend', None, g.phrase('h_brand')), *filters),
             g.check('flt.stock', 'in_stock', area='filters'),
             g.button('flt.apply', 'apply_filters', area='filters'),
             g.button('flt.clear', 'clear_filters', area='filters', variant='secondary'))
    cards = []
    for name in products(g, g.rng.randrange(12, 25) if g.dense else g.rng.randrange(4, 9)):
        k = 'card.' + slugify(name)
        g.block = 'product-card'
        cards.append(h('li' if g.rng.random() < 0.5 else 'article',
                       {'class': g.cls('card', 'product'), 'data-product': slugify(name) if g.rng.random() < 0.5 else None},
                       h('img', {'src': '/img/' + slugify(name) + '.jpg', 'alt': name}),
                       h('h3', {'class': g.cls('card-title')},
                         g.link(k + '.title', 'products', '/products/' + slugify(name), area='grid', item=name, text=name)),
                       h('span', {'class': g.cls('price')}, price(g)),
                       g.button(k + '.add', 'add_to_cart', area='grid', item=name),
                       g.button(k + '.wish', 'add_to_wishlist', area='grid', item=name, icon=True, variant='') if g.rng.random() < 0.5 else None))
    g.block = 'catalog'
    top = h('div', {'class': g.cls('toolbar')}, g.field('grid.sort', 'sort_by', area='grid',
                                                         options=['Featured', 'Price: low to high', 'Price: high to low', 'Newest']))
    pager = h('nav', {'class': g.cls('pagination'), 'aria-label': 'Pagination'},
              g.link('grid.prev', 'prev_page', '?page=1', area='pagination'),
              g.link('grid.next', 'next_page', '?page=3', area='pagination'))
    return [site_header(g, shop=True),
            h('main', {'class': g.cls('main')}, g.heading(1, g.phrase('h_products')),
              h('div', {'class': g.cls('layout')}, side,
                h('section', None, top, h('ul' if g.rng.random() < 0.5 else 'div', {'class': g.cls('grid')}, *cards), pager))),
            site_footer(g)], 'products'


def fam_settings(g, tab=None):
    g.block = 'settings'
    tab = tab or g.pick(('profile', 'profile', 'notifications', 'security'), 'tab')
    tabs = h('nav', {'class': g.cls('tabs'), 'role': 'tablist' if g.rng.random() < 0.4 else None},
             *[g.link('tab.' + t, {'profile': 'account_link', 'notifications': 'help', 'security': 'security'}[t],
                      '/settings/' + t, area='tabs', text=g.phrase('h_' + t))
               for t in ('profile', 'notifications', 'security')])
    sections = []
    p = 'set.' + tab + '.'
    if tab == 'profile':
        sections.append(h('section', {'class': g.cls('section')}, h('h2', None, g.phrase('h_profile')),
                          h('div', {'class': g.cls('avatar')},
                            g.button(p + 'upload', 'upload_avatar', area='profile', variant='secondary'),
                            g.button(p + 'remove_photo', 'remove_avatar', area='profile', variant='') if g.rng.random() < 0.5 else None),
                          g.field(p + 'display', 'display_name', area='profile'),
                          g.field(p + 'email', 'email', area='profile'),
                          g.field(p + 'bio', 'bio', area='profile') if g.rng.random() < 0.7 else None,
                          g.field(p + 'lang', 'language', area='profile', options=['English', 'Deutsch', 'Français', 'Español']),
                          g.field(p + 'tz', 'timezone', area='profile', options=['UTC', 'Europe/Berlin', 'America/New_York']) if g.rng.random() < 0.6 else None))
    elif tab == 'notifications':
        sections.append(h('section', {'class': g.cls('section')}, h('h2', None, g.phrase('h_notifications')),
                          g.check(p + 'email', 'email_notifications', area='notifications', checked=True),
                          g.check(p + 'push', 'push_notifications', area='notifications'),
                          g.check(p + 'digest', 'weekly_digest', area='notifications'),
                          g.check(p + 'dark', 'dark_mode', area='notifications') if g.rng.random() < 0.4 else None))
    else:
        sections.append(h('section', {'class': g.cls('section')}, h('h2', None, g.phrase('h_security')),
                          g.button(p + 'pw', 'change_password', area='security', variant='secondary'),
                          g.check(p + '2fa', 'two_factor', area='security'),
                          g.button(p + 'signout', 'sign_out', area='security', variant='') if g.rng.random() < 0.5 else None))
    actions = h('div', {'class': g.cls('actions')},
                g.button(p + 'cancel', 'cancel', area=tab, variant='secondary') if g.rng.random() < 0.6 else None,
                g.button(p + 'save', 'save_changes', area=tab, submit=True))
    danger = None
    if g.rng.random() < 0.5:
        danger = h('section', {'class': g.cls('section', 'danger')}, h('h2', None, g.phrase('h_danger')),
                   g.button(p + 'export', 'export_data', area='danger', variant='secondary') if g.rng.random() < 0.5 else None,
                   g.button(p + 'delete', 'delete_account', area='danger', variant='danger'))
    form = h('form', {'class': g.cls('form'), 'id': g.pick((tab + '-form', 'settings-form', None), 'fid')},
             *sections, actions)
    return app_shell(g, [tabs, form, danger], g.phrase('h_settings')), 'settings/' + tab


def fam_table(g):
    g.block = 'members'
    names = people(g, g.rng.randrange(12, 26) if g.dense else g.rng.randrange(3, 8))
    head_cb = g.check('tbl.all', 'select_all', area='table') if g.rng.random() < 0.5 else None
    rows = []
    for i, name in enumerate(names):
        k = 'row.' + slugify(name)
        g.block = 'member-row'
        email = slugify(name).replace('-', '.') + '@' + g.site.brand.lower() + '.example'
        rows.append(h('tr', {'class': g.cls('row'), 'data-id': None if g.rng.random() < 0.6 else str(4000 + i * 13)},
                      h('td', None, g.check(k + '.select', 'select_all', area='row', item=name, text='') if head_cb is not None else None),
                      h('td', None, h('div', {'class': g.cls('person')}, h('span', {'class': g.cls('name')}, name),
                                      h('span', {'class': g.cls('email')}, email))),
                      h('td', None, g.pick(ROLES_TEXT, 'role', i)),
                      h('td', None, h('span', {'class': g.cls('badge')}, g.pick(STATUS_TEXT, 'st', i))),
                      h('td', {'class': g.cls('actions')},
                        g.button(k + '.edit', 'edit', area='row', item=name, icon=g.style.icons, variant=''),
                        g.button(k + '.view', 'view', area='row', item=name, icon=g.style.icons, variant='') if g.rng.random() < 0.3 else None,
                        g.button(k + '.delete', 'delete', area='row', item=name, icon=g.style.icons, variant=''))))
    g.block = 'members'
    toolbar = h('div', {'class': g.cls('toolbar')},
                g.field('tbl.search', 'search_table', area='table'),
                g.field('tbl.status', 'filter_status', area='table', options=['All', 'Active', 'Invited']) if g.rng.random() < 0.5 else None,
                g.button('tbl.export', 'export_csv', area='table', variant='secondary') if g.rng.random() < 0.5 else None,
                g.button('tbl.invite', 'invite', area='table'))
    table = h('table', {'class': g.cls('table')},
              h('thead', None, h('tr', None, h('th', None, head_cb), h('th', None, 'Name'), h('th', None, 'Role'),
                                 h('th', None, 'Status'), h('th', None, h('span', {'class': 'sr-only'}, 'Actions')))),
              h('tbody', None, *rows))
    pager = h('div', {'class': g.cls('pagination')},
              g.field('tbl.size', 'rows_per_page', area='table', options=['10', '25', '50']) if g.rng.random() < 0.5 else None,
              g.button('tbl.prev', 'prev_page', area='table', variant='', text=None),
              g.button('tbl.next', 'next_page', area='table', variant='', text=None))
    return app_shell(g, [toolbar, table, pager], g.phrase('h_members')), 'team/members'


def fam_dashboard(g):
    g.block = 'dashboard'
    cards = []
    for i, key in enumerate(('Revenue', 'Active users', 'Conversion', 'Churn')[:g.rng.randrange(2, 5)]):
        k = 'stat.' + slugify(key)
        cards.append(h('div', {'class': g.cls('card', 'stat')}, h('h3', None, key),
                       h('p', {'class': g.cls('metric')}, str(g.rng.randrange(10, 999)) + ('%' if i > 1 else '')),
                       g.link(k + '.view', 'view_report', '/reports/' + slugify(key), area='stats', item=key)))
    projs = projects(g, g.rng.randrange(8, 14) if g.dense else g.rng.randrange(2, 6))
    items = []
    for name in projs:
        k = 'proj.' + slugify(name)
        items.append(h('li', {'class': g.cls('row', 'activity')}, h('span', {'class': g.cls('title')}, name),
                       h('span', None, g.pick(('2h ago', 'yesterday', '3 days ago'), name)),
                       g.link(k + '.open', 'view', '/projects/' + str(stable(name) % 9000 + 1000), area='activity', item=name)))
    actions = h('div', {'class': g.cls('actions')},
                g.button('dash.new', 'new_project', area='dashboard'),
                g.button('dash.report', 'create_report', area='dashboard', variant='secondary') if g.rng.random() < 0.6 else None,
                g.button('dash.invite', 'invite', area='dashboard', variant='secondary') if g.rng.random() < 0.5 else None,
                g.button('dash.refresh', 'refresh', area='dashboard', icon=True, variant='') if g.rng.random() < 0.5 else None,
                g.field('dash.range', 'date_range', area='dashboard', options=['Last 7 days', 'Last 30 days', 'This year']) if g.rng.random() < 0.6 else None)
    activity = h('section', {'class': g.cls('section')}, h('h2', None, g.phrase('h_activity')),
                 h('ul', None, *items), g.link('dash.all', 'view_all', '/activity', area='activity'))
    upgrade = h('div', {'class': g.cls('banner')}, g.button('dash.upgrade', 'upgrade', area='banner')) if g.rng.random() < 0.4 else None
    return app_shell(g, [actions, h('div', {'class': g.cls('grid')}, *cards), activity, upgrade], g.phrase('h_dashboard')), 'dashboard'


def fam_dialog(g):
    """A project list with a confirmation dialog open on top of it."""
    base, _ = fam_table(g) if g.rng.random() < 0.5 else fam_dashboard(g)
    g.block = 'dialog'
    kind = g.pick(('delete', 'archive', 'unsaved', 'rename'), 'dlg')
    proj = projects(g, 1)[0]
    if kind == 'delete':
        body = [h('h2', {'id': 'dlg-title'}, g.phrase('h_delete_dialog')), g.para(proj),
                g.field('dlg.name', 'confirm_name', area='dialog'),
                h('div', {'class': g.cls('actions')}, g.button('dlg.cancel', 'cancel', area='dialog', variant='secondary'),
                  g.button('dlg.confirm', 'delete_project', area='dialog', variant='danger'))]
    elif kind == 'archive':
        body = [h('h2', {'id': 'dlg-title'}, proj), g.para(g.word('dlg.x', 'archive_project')),
                h('div', {'class': g.cls('actions')}, g.button('dlg.cancel', 'cancel', area='dialog', variant='secondary'),
                  g.button('dlg.archive', 'archive_project', area='dialog'),
                  g.button('dlg.delete', 'delete_project', area='dialog', variant='danger') if g.rng.random() < 0.5 else None)]
    elif kind == 'unsaved':
        body = [h('h2', {'id': 'dlg-title'}, g.phrase('h_unsaved')),
                h('div', {'class': g.cls('actions')}, g.button('dlg.keep', 'keep_editing', area='dialog', variant='secondary'),
                  g.button('dlg.draft', 'save_draft', area='dialog'),
                  g.button('dlg.discard', 'discard', area='dialog', variant='danger'))]
    else:
        body = [h('h2', {'id': 'dlg-title'}, g.word('dlg.r', 'rename')), g.field('dlg.name', 'workspace_name', area='dialog'),
                h('div', {'class': g.cls('actions')}, g.button('dlg.cancel', 'cancel', area='dialog', variant='secondary'),
                  g.button('dlg.rename', 'rename', area='dialog'))]
    close = g.button('dlg.close', 'close', area='dialog', icon=True, variant='')
    tag = g.pick(('dialog', 'div', 'div'), 'dtag')
    attrs = {'class': g.cls('dialog'), 'aria-labelledby': 'dlg-title'}
    if tag == 'dialog':
        attrs['open'] = True
    else:
        attrs.update({'role': g.pick(('dialog', 'alertdialog'), 'drole'), 'aria-modal': 'true'})
    dialog = h(tag, attrs, close, *body)
    return base + [h('div', {'class': g.cls('backdrop')}, dialog)], 'projects'


CMPS = ('onetrust', 'cookiebot', 'didomi', 'usercentrics', 'quantcast', 'osano', 'cookieyes', 'complianz', 'generic')


def cookie_banner(g, cmp=None):
    cmp = cmp or g.pick(CMPS, 'cmp')
    p = 'ck.'
    title = h('h2', None, g.phrase('h_cookie'))
    text = g.para(g.phrase('p_cookie'))
    if cmp == 'onetrust':
        return h('div', {'id': 'onetrust-consent-sdk'},
                 h('div', {'id': 'onetrust-banner-sdk', 'class': 'otFlat', 'role': 'region', 'aria-label': 'Cookie banner'},
                   title, text,
                   h('div', {'id': 'onetrust-button-group'},
                     g.button(p + 'manage', 'manage_cookies', area='cookie', form='button', variant=''),
                     g.button(p + 'reject', 'reject_all', area='cookie', form='button', variant=''),
                     g.button(p + 'accept', 'accept_all', area='cookie', form='button', variant=''))))
    if cmp == 'cookiebot':
        return h('div', {'id': 'CybotCookiebotDialog', 'role': 'dialog', 'aria-modal': 'true'}, title, text,
                 h('div', {'id': 'CybotCookiebotDialogBodyButtons'},
                   g.button(p + 'reject', 'reject_all', area='cookie', form='button', variant=''),
                   g.button(p + 'manage', 'manage_cookies', area='cookie', form='button', variant=''),
                   g.button(p + 'accept', 'accept_all', area='cookie', form='button', variant='')))
    if cmp == 'didomi':
        return h('div', {'id': 'didomi-host'}, h('div', {'id': 'didomi-notice', 'class': 'didomi-popup-notice'},
                 text, g.button(p + 'manage', 'manage_cookies', area='cookie', form='button', variant=''),
                 g.button(p + 'reject', 'reject_all', area='cookie', form='button', variant=''),
                 g.button(p + 'accept', 'accept_all', area='cookie', form='button', variant='')))
    if cmp == 'usercentrics':
        return h('div', {'id': 'usercentrics-root', 'data-testid': 'uc-banner'}, title, text,
                 h('div', {'data-testid': 'uc-buttons-container'},
                   g.button(p + 'manage', 'manage_cookies', area='cookie', form='button', variant=''),
                   g.button(p + 'reject', 'reject_all', area='cookie', form='button', variant=''),
                   g.button(p + 'accept', 'accept_all', area='cookie', form='button', variant='')))
    if cmp == 'quantcast':
        return h('div', {'id': 'qc-cmp2-container'}, h('div', {'class': 'qc-cmp2-summary-section'}, title, text),
                 h('div', {'class': 'qc-cmp2-summary-buttons'},
                   g.button(p + 'manage', 'manage_cookies', area='cookie', form='button', variant=''),
                   g.button(p + 'reject', 'reject_all', area='cookie', form='button', variant=''),
                   g.button(p + 'accept', 'accept_all', area='cookie', form='button', variant='')))
    attrs = {'class': g.cls('banner', 'cookie'), 'role': g.pick(('dialog', 'region', None), 'ckrole'),
             'aria-label': 'Cookie consent' if g.rng.random() < 0.5 else None,
             'id': {'osano': 'osano-cm-window', 'cookieyes': 'cky-consent', 'complianz': 'cmplz-cookiebanner-container'}.get(cmp)}
    prefs = None
    if g.rng.random() < 0.4:
        prefs = h('div', {'class': g.cls('prefs')}, g.check(p + 'analytics', 'analytics_cookies', area='cookie'),
                  g.check(p + 'marketing', 'marketing_cookies', area='cookie'),
                  g.button(p + 'save', 'save_preferences', area='cookie', variant='secondary'))
    return h('div', attrs, title, text, prefs,
             h('div', {'class': g.cls('actions')},
               g.button(p + 'accept', 'accept_all', area='cookie'),
               g.button(p + 'reject', 'reject_all', area='cookie', variant='secondary'),
               g.link(p + 'policy', 'cookie_policy', '/cookies', area='cookie')))


def newsletter_popup(g):
    p = 'nl.'
    g.block = 'newsletter'
    return h('div', {'class': g.cls('dialog', 'newsletter'), 'role': 'dialog', 'aria-modal': 'true',
                     'aria-label': g.phrase('h_newsletter')},
             g.button(p + 'close', 'close', area='popup', icon=True, variant=''),
             h('h2', None, g.phrase('h_newsletter')),
             h('form', {'class': g.cls('form', 'inline')}, g.field(p + 'email', 'email', area='newsletter'),
               g.button(p + 'subscribe', 'subscribe', area='newsletter', submit=True)),
             g.button(p + 'no', 'no_thanks', area='popup', variant='') if g.rng.random() < 0.7 else None)


def fam_landing(g):
    g.block = 'hero'
    hero = h('section', {'class': g.cls('section', 'hero')}, g.heading(1, g.phrase('h_hero')),
             h('div', {'class': g.cls('actions')}, g.button('hero.start', 'get_started', area='hero'),
               g.button('hero.demo', 'book_demo', area='hero', variant='secondary') if g.rng.random() < 0.6 else None))
    feats = []
    for name in ('Automations', 'Reporting', 'Integrations', 'Security', 'Workflows')[:g.rng.randrange(2, 5)]:
        k = 'feat.' + slugify(name)
        g.block = 'feature'
        feats.append(h('div', {'class': g.cls('card', 'feature')}, h('h3', None, name),
                       g.para('Do more with ' + name.lower() + '.'),
                       g.link(k + '.more', 'learn_more', '/features/' + slugify(name), area='features', item=name)))
    g.block = 'features'
    sect = h('section', {'class': g.cls('section')}, h('h2', None, g.phrase('h_features')), h('div', {'class': g.cls('grid')}, *feats))
    news = h('section', {'class': g.cls('section', 'newsletter')},
             h('form', None, g.field('lnd.email', 'email', area='newsletter'),
               g.button('lnd.subscribe', 'subscribe', area='newsletter', submit=True))) if g.rng.random() < 0.6 else None
    return [site_header(g), h('main', {'class': g.cls('main')}, hero, sect, news), site_footer(g)], ''


def fam_search(g):
    query = g.pick(('invoice', 'shipping', 'password reset', 'api key', 'refund'), 'q')
    g.block = 'search'
    form = h('form', {'role': 'search', 'class': g.cls('form', 'search')},
             g.field('srch.q', 'search', area='search', value=query),
             g.button('srch.go', 'search_button', area='search', submit=True))
    results = []
    titles = projects(g, 3) + products(g, 3)
    g.rng.shuffle(titles)
    for name in titles[:g.rng.randrange(3, 7)]:
        k = 'res.' + slugify(name)
        results.append(h('li', {'class': g.cls('result')},
                         h('h3', None, g.link(k + '.title', 'view', '/r/' + slugify(name), area='results', item=name, text=name)),
                         g.para('… ' + query + ' …'),
                         g.button(k + '.save', 'save_result', area='results', item=name, variant='', icon=g.rng.random() < 0.5)))
    filters = h('aside', {'class': g.cls('filters')}, h('h2', None, g.phrase('h_filters')),
                *[g.check('sf.' + slugify(c), 'in_stock', area='filters', text=c, item=c) for c in CATEGORIES[:g.rng.randrange(2, 5)]],
                g.button('sf.clear', 'clear_filters', area='filters', variant=''))
    more = g.button('srch.more', 'load_more', area='results', variant='secondary')
    return [site_header(g, shop=g.rng.random() < 0.5),
            h('main', {'class': g.cls('main')}, g.heading(1, g.phrase('h_search', query=query)), form,
              h('div', {'class': g.cls('layout')}, filters, h('section', None, h('ol', None, *results), more))),
            site_footer(g)], 'search'


def fam_wizard(g, step=None):
    step = step or g.pick(('account', 'team', 'invite', 'plan'), 'step')
    g.block = 'onboarding'
    names = ('account', 'team', 'invite', 'plan')
    stepper = h('ol', {'class': g.cls('stepper')},
                *[h('li', {'class': g.cls('step', 'active' if n == step else ''),
                           'aria-current': 'step' if n == step else None},
                    g.phrase('h_wizard_' + n)) for n in names])
    p = 'wiz.' + step + '.'
    if step == 'account':
        fields = [g.field(p + 'name', 'full_name', area='onboarding'), g.field(p + 'email', 'email', area='onboarding'),
                  g.field(p + 'password', 'password', area='onboarding')]
    elif step == 'team':
        fields = [g.field(p + 'workspace', 'workspace_name', area='onboarding'),
                  g.field(p + 'size', 'team_size', area='onboarding', options=['1-10', '11-50', '51-200', '200+']),
                  g.field(p + 'role', 'job_role', area='onboarding', options=['Engineering', 'Design', 'Marketing', 'Other']),
                  g.check(p + 'work', 'use_work', area='onboarding', radio_name='use'),
                  g.check(p + 'personal', 'use_personal', area='onboarding', radio_name='use')]
    elif step == 'invite':
        fields = [g.field(p + 'email1', 'email', area='invite'), g.field(p + 'email2', 'email', area='invite', item='second'),
                  g.button(p + 'add', 'invite', area='invite', variant='secondary')]
    else:
        fields = [g.check(p + 'free', 'use_personal', area='plan', radio_name='plan', text='Free'),
                  g.check(p + 'pro', 'use_work', area='plan', radio_name='plan', text='Pro')]
    actions = h('div', {'class': g.cls('actions')},
                g.button(p + 'back', 'back', area='onboarding', variant='secondary') if step != 'account' else None,
                g.button(p + 'skip', 'skip', area='onboarding', variant='') if step in ('invite', 'plan') else None,
                g.button(p + 'next', 'finish' if step == 'plan' else 'continue', area='onboarding', submit=True))
    form = h('form', {'class': g.cls('form'), 'id': step + '-step' if g.rng.random() < 0.5 else None}, *fields, actions)
    return [site_header(g, minimal=True), h('main', {'class': g.cls('main')}, stepper,
                                            h('section', None, g.heading(1, g.phrase('h_wizard_' + step)), form))], 'onboarding/' + step


def fam_article(g):
    title = g.pick(ARTICLE_TITLES[g.split], 'title')
    author = people(g, 1)[0]
    g.block = 'article'
    share = h('div', {'class': g.cls('share')}, g.button('art.copy', 'copy_link', area='share', icon=g.rng.random() < 0.5, variant=''),
              g.button('art.li', 'share_linkedin', area='share', icon=True, variant=''),
              g.button('art.x', 'share_x', area='share', icon=True, variant=''))
    head = h('header', {'class': g.cls('article-header')}, g.heading(1, title),
             h('p', None, g.link('art.author', 'about', '/authors/' + slugify(author), area='byline', text=author),
               ' · 6 min read'), share,
             g.button('art.follow', 'follow', area='byline', variant='secondary') if g.rng.random() < 0.5 else None)
    body = h('div', {'class': g.cls('prose')}, *[g.para('Paragraph ' + str(i) + ' of the article.') for i in range(3)])
    tags = h('ul', {'class': g.cls('tags')}, *[h('li', None, g.link('tag.' + slugify(t), 'blog', '/tags/' + slugify(t), area='tags', text=t, item=t))
                                             for t in ('engineering', 'product', 'culture')[:g.rng.randrange(1, 4)]])
    related = []
    for t in ARTICLE_TITLES[g.split][:3]:
        if t == title:
            continue
        k = 'rel.' + slugify(t)
        related.append(h('article', {'class': g.cls('card')}, h('h3', None, t),
                         g.link(k + '.more', 'learn_more', '/blog/' + slugify(t), area='related', item=t)))
    comments = h('section', {'class': g.cls('section', 'comments')}, h('h2', None, g.phrase('h_comments')),
                 h('form', None, g.field('cmt.name', 'full_name', area='comment') if g.rng.random() < 0.5 else None,
                   g.field('cmt.body', 'comment', area='comment'),
                   g.button('cmt.post', 'post_comment', area='comment', submit=True)))
    nav = h('nav', {'class': g.cls('pager')}, g.link('art.prev', 'prev_article', '/blog/prev', area='pager'),
            g.link('art.next', 'next_article', '/blog/next', area='pager'))
    return [site_header(g), h('main', {'class': g.cls('main')},
                              h('article', None, head, body, tags), nav,
                              h('section', None, h('h2', None, g.phrase('h_related')), *related), comments),
            site_footer(g)], 'blog/' + slugify(title)


def fam_reset(g):
    g.block = 'reset'
    form = h('form', {'class': g.cls('form')}, g.field('rst.email', 'email', area='reset'),
             g.button('rst.send', 'send_reset_link', area='reset', submit=True),
             g.link('rst.back', 'sign_in_link', '/login', area='reset'))
    return [site_header(g, minimal=True), h('main', None, h('section', {'class': g.cls('card')},
                                                             g.heading(1, g.phrase('h_reset')), form))], 'forgot-password'


def fam_twofactor(g):
    g.block = 'mfa'
    form = h('form', {'class': g.cls('form')}, g.field('mfa.code', 'verify_code', area='mfa'),
             g.button('mfa.verify', 'verify', area='mfa', submit=True),
             g.button('mfa.resend', 'resend_code', area='mfa', variant=''),
             g.check('mfa.remember', 'remember_me', area='mfa') if g.rng.random() < 0.5 else None)
    return [site_header(g, minimal=True), h('main', None, h('section', {'class': g.cls('card')},
                                                             g.heading(1, g.phrase('h_twofactor')), form))], 'login/verify'


def fam_interstitial(g, kind=None):
    kind = kind or g.pick(('session', 'verify', 'denied', 'age', 'idle'), 'inter')
    g.block = 'interstitial'
    if kind == 'session':
        body = [g.heading(1, g.phrase('h_session')), g.button('int.again', 'sign_in_again', area='interstitial'),
                g.link('int.help', 'help', '/help', area='interstitial')]
        path = 'session-expired'
    elif kind == 'verify':
        body = [g.heading(1, g.phrase('h_verify')), g.button('int.resend', 'resend_email', area='interstitial'),
                g.link('int.change', 'change_email', '/signup', area='interstitial')]
        path = 'verify-email'
    elif kind == 'denied':
        body = [g.heading(1, g.phrase('h_denied')), g.button('int.request', 'request_access', area='interstitial'),
                g.link('int.switch', 'switch_account', '/login', area='interstitial')]
        path = 'access-denied'
    elif kind == 'age':
        body = [g.heading(1, g.phrase('h_age')), g.button('int.yes', 'age_yes', area='interstitial'),
                g.button('int.no', 'age_no', area='interstitial', variant='secondary')]
        path = 'age-check'
    else:
        body = [g.heading(1, g.phrase('h_idle')), g.button('int.stay', 'stay_signed_in', area='interstitial'),
                g.button('int.out', 'sign_out', area='interstitial', variant='secondary')]
        path = 'session'
    return [site_header(g, minimal=True), h('main', None, h('section', {'class': g.cls('card'), 'role': 'dialog' if g.rng.random() < 0.4 else None}, *body))], path


FAMILIES = {
    'login': fam_login, 'signup': fam_signup, 'checkout': fam_checkout, 'cart': fam_cart,
    'grid': fam_grid, 'settings': fam_settings, 'table': fam_table, 'dashboard': fam_dashboard,
    'dialog': fam_dialog, 'landing': fam_landing, 'search': fam_search, 'wizard': fam_wizard,
    'article': fam_article, 'reset': fam_reset, 'twofactor': fam_twofactor,
}
# Families held out of training: the test split uses them (with held-out names) to measure
# how the model does on page layouts it never saw.
TEST_FAMILIES = ('search', 'wizard', 'article')
TRAIN_FAMILIES = ('login', 'signup', 'checkout', 'cart', 'grid', 'settings', 'table', 'dashboard',
                  'dialog', 'landing', 'reset', 'twofactor')
# For a step that moved to another page: pages a flow goes to from each family.
RELATED = {
    'login': ('twofactor', 'reset', 'signup', 'dashboard'),
    'signup': ('wizard', 'login', 'twofactor'),
    'checkout': ('cart', 'grid'),
    'cart': ('checkout', 'grid'),
    'grid': ('cart', 'checkout', 'search'),
    'settings': ('settings', 'dashboard', 'table'),
    'table': ('dashboard', 'settings'),
    'dashboard': ('table', 'settings'),
    'dialog': ('table', 'dashboard'),
    'landing': ('signup', 'login'),
    'search': ('grid', 'cart'),
    'wizard': ('wizard', 'signup'),
    'article': ('landing', 'search'),
    'reset': ('login', 'twofactor'),
    'twofactor': ('login', 'reset', 'dashboard'),
}


def build(site, family, seed, *, style=None, lang=None, over=None, split='train', marker_uid=None,
          marker_attr=None, overlays=(), **kw):
    """Build one page. Returns (Gen, body El list, path)."""
    g = Gen(site, family, seed, style=style, lang=lang, over=over, split=split,
            marker_uid=marker_uid, marker_attr=marker_attr)
    fn = FAMILIES[family]
    kids, path = fn(g, **kw) if kw else fn(g)
    for overlay in overlays:
        if overlay == 'cookie':
            kids.append(cookie_banner(g))
        elif overlay == 'newsletter':
            kids.append(newsletter_popup(g))
    return g, kids, path


def document(g, kids, title=None):
    """The whole HTML document for a body."""
    shell = g.style.shell
    root_attrs = {'react': {'id': 'root'}, 'next': {'id': '__next'}, 'vue': {'id': 'app', 'data-v-app': True},
                  'angular': {}, 'svelte': {'id': 'svelte'}, 'plain': {}}[shell]
    body_kids = [k for k in kids if k is not None]
    if shell == 'angular':
        wrapper = h('app-root', {'ng-version': '17.3.0'}, *body_kids)
    elif root_attrs:
        wrapper = h('div', root_attrs, *body_kids)
    else:
        wrapper = None
    head = ('<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + _html.escape(title or g.site.brand) + '</title>'
            '<link rel="stylesheet" href="/assets/app.' + _hash(g.style.hash_seed, 'css', n=8) + '.css">'
            '<script>window.__CONFIG__={"env":"prod"};</script></head>')
    body = render(wrapper) if wrapper is not None else ''.join(render(k) for k in body_kids)
    scripts = '<script src="/assets/main.' + _hash(g.style.hash_seed, 'js', n=8) + '.js" defer></script>'
    return ('<!doctype html>\n<html lang="' + g.lang + '">' + head + '\n<body>\n' + body + scripts
            + '\n</body></html>\n')
