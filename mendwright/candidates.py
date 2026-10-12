"""Candidate elements for the local selector model, and the XPath mendwright builds for
the one it picks.

A small local model is not asked to write an XPath from a whole page. mendwright reads
the captured page itself and gives the model a short task instead: the broken step (key,
role, old selectors, expected text, what is known about the element it used to match,
the page path now and where the element was last found) and a numbered list of at most
MAX_CANDIDATES elements of the step's role. The model answers {"pick": <n>}, or
{"pick": null, "why": "<reason>"} when no element on the page is the one the step means.
mendwright then builds a unique XPath for the picked element in code, from its id,
data-testid, name, aria-label or a label, text or context anchor (never a bare position),
and the result goes through the same gate as any other proposal.

The functions here are the only definition of that task. The training data in
training/ is built with them, and LocalModelProvider (providers/local.py) uses them at
run time, so the model sees at run time exactly what it was trained on.

- extract(bundle): the candidate list for a capture bundle (see capture.build_bundle).
- format_task(bundle, candidates): the (system, user) messages for the model.
- parse_answer(text): the model's reply as {'pick': int or None, 'why': str or None}.
- robust_xpaths(element, doc) / robust_xpath(element, doc): unique XPaths for an element.

Standard library and lxml only.
"""

import json
import re

import lxml.etree
import lxml.html

from . import fingerprint as _fp
from .fingerprint import generated_id, stable_classes
from .validation import _role_of

MAX_CANDIDATES = 40

SYSTEM_PROMPT = (
    'Pick the candidate that is the same control the broken step used, even if its id, '
    'classes, text, language or place changed. Reply with JSON only: {"pick": <number>}, '
    'or {"pick": null, "why": "removed"} when it is not on this page, "ambiguous" when '
    'several fit equally, "moved" when the step now belongs to another page. Never pick a '
    'control that is only similar: another action, another row or item, or a destructive '
    'one.')

# The reasons the model may give for a null pick, and how a person reads them.
WHY = {
    'removed': 'the element is not on this page',
    'ambiguous': 'several elements fit equally and nothing tells them apart',
    'moved': 'the step seems to have moved to another page',
}

# Attributes test tools use for stable ids, best first.
TEST_ATTRS = ('data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy', 'data-e2e',
              'data-automation-id')

_SPACE = re.compile(r'[ \t\r\n]+')   # what XPath's normalize-space() collapses
_WORD = re.compile(r'[a-z0-9]+')
_CAMEL = re.compile(r'([a-z0-9])([A-Z])')
_LITERAL = re.compile(r"'([^']*)'|\"([^\"]*)\"")
_REDACTED = ('[URL]', '[redacted')
# ids and classes that a framework generated: React useId (:r1:), Ember, Angular
# Material, CSS-module and styled-components hashes, uuids and long numbers.
_GENERATED = re.compile(
    r'^:r[0-9a-z]*:$|^ember\d+$|^mat-[a-z-]+-\d+$|^cdk-[a-z-]+-\d+$|^react-[a-z-]+-\d+|'
    r'^radix-|^headlessui-|^[0-9a-f]{8}-[0-9a-f]{4}-|^\d+$|\d{4,}|'
    r'^(?:css|sc|jsx|emotion|svelte|chakra|mui)-[a-z0-9]{4,}$|'
    r'(?:^|[_-])[a-zA-Z0-9]{5,}$(?<=[0-9][a-zA-Z0-9]{4})|^[a-z]{1,3}[0-9][a-z0-9]{3,}$')
_HASHED_CLASS = re.compile(
    r'^(?:css|sc|jsx|emotion|svelte|chakra|mui|tw)-[a-z0-9]{4,}$|_[a-z0-9]{5,}$|__[a-z0-9]{4,}$|'
    r'^[a-z]{1,3}[0-9][a-z0-9]{3,}$|^[A-Za-z]{5,8}$(?<=[A-Z][a-z][A-Z][a-z][A-Z])')
_HIDDEN_CLASSES = {'hidden', 'd-none', 'is-hidden', 'sr-only', 'visually-hidden', 'invisible',
                   'hide', 'u-hidden', 'display-none', 'offscreen'}
_LANDMARKS = (
    ('dialog', ('dialog',), ('dialog', 'alertdialog')),
    ('header', ('header',), ('banner',)),
    ('nav', ('nav',), ('navigation', 'menubar', 'tablist')),
    ('footer', ('footer',), ('contentinfo',)),
    ('aside', ('aside',), ('complementary',)),
    ('form', ('form',), ('form', 'search')),
    ('main', ('main',), ('main',)),
)
_HEADINGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'legend', 'caption')
_ITEM_TAGS = ('tr', 'li', 'article')
_ITEM_ROLES = ('row', 'listitem', 'article', 'option', 'gridcell')
_CONTAINER_TAGS = ('section', 'form', 'fieldset', 'dialog', 'nav', 'aside', 'article', 'li',
                   'tr', 'table', 'header', 'footer', 'main', 'div', 'ul', 'ol', 'details')
_CONTROL_TAGS = ('button', 'a', 'input', 'select', 'textarea', 'label', 'option')
_FIELD_ROLES = ('input', 'checkbox', 'radio', 'combobox', 'textarea')

TEXT_CLIP = 40
ATTR_CLIP = 40


# --- small helpers ---------------------------------------------------------------------------

def _clip(text, limit):
    text = _SPACE.sub(' ', text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit - 1].rstrip() + '…'


def text_of(el):
    """An element's text the way XPath's normalize-space() sees it."""
    try:
        return _SPACE.sub(' ', el.text_content() or '').strip()
    except Exception:
        return ''


def _usable(value, limit=120):
    return (isinstance(value, str) and value.strip() != '' and len(value) <= limit
            and not any(mark in value for mark in _REDACTED))


def generated(value):
    """True for an id or class token that looks generated by a framework or build tool
    (mendwright.fingerprint.generated_id, plus a few framework patterns)."""
    if not isinstance(value, str) or not value:
        return True
    return bool(_GENERATED.search(value)) or generated_id(value)


def xpath_literal(value):
    """``value`` as an XPath 1.0 string literal."""
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return 'concat(' + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ')'


def tokens(text):
    """Lower-case word tokens of ``text``; camelCase and snake/kebab case are split."""
    if not text:
        return []
    return _WORD.findall(_CAMEL.sub(r'\1 \2', str(text)).lower())


def parse_html(html):
    """The document mendwright reads candidates from (lxml, like StandInPage)."""
    text = html if isinstance(html, str) and html.strip() else '<html><body></body></html>'
    try:
        return lxml.html.document_fromstring(text)
    except (lxml.etree.ParserError, ValueError):
        return lxml.html.document_fromstring('<html><body></body></html>')


# --- what an element looks like ----------------------------------------------------------------

def _hidden(el):
    """'hidden' when el or an ancestor is hidden by an inline style, the hidden attribute or
    aria-hidden, 'hidden?' when a class name suggests it, else ''."""
    if el.tag == 'input' and (el.get('type') or '').lower() == 'hidden':
        return 'hidden'
    maybe = False
    node = el
    while node is not None and isinstance(node.tag, str):
        style = (node.get('style') or '').replace(' ', '').lower()
        if ('display:none' in style or 'visibility:hidden' in style
                or node.get('hidden') is not None
                or (node.get('aria-hidden') or '').lower() == 'true'):
            return 'hidden'
        classes = set((node.get('class') or '').lower().split())
        if classes & _HIDDEN_CLASSES or any(c.endswith(':hidden') for c in classes):
            maybe = True
        node = node.getparent()
    return 'hidden?' if maybe else ''


def _region(el):
    """The landmarks el is in, outermost first, at most two, each with its id when that
    is a name a person chose: 'main>form#login', 'dialog', 'header>nav'."""
    found = []
    for anc in el.iterancestors():
        if not isinstance(anc.tag, str):
            continue
        tag = anc.tag.lower()
        role = (anc.get('role') or '').lower()
        for name, tags, roles in _LANDMARKS:
            if tag in tags or role in roles or (name == 'dialog' and anc.get('aria-modal') == 'true'):
                if not found or found[-1][0] != name:
                    ident = anc.get('id')
                    if not (ident and _usable(ident, 30) and not generated(ident)
                            and re.match(r'^[A-Za-z][\w-]*$', ident)):
                        ident = ''
                    found.append((name, ident))
                break
    found.reverse()
    for name, ident in found:
        if name == 'dialog':
            return 'dialog' + ('#' + ident if ident else '')
    return '>'.join(name + ('#' + ident if ident else '') for name, ident in found[-2:])


def _by_ids(el, ids):
    root = el.getroottree().getroot()
    parts = []
    for ident in (ids or '').split():
        for hit in root.xpath('//*[@id=$i]', i=ident)[:1]:
            parts.append(text_of(hit))
    return ' '.join(p for p in parts if p)


def _field_label(el):
    """The text that names a form field: aria-labelledby, <label for>, a wrapping <label>,
    or a short label-like element just before it."""
    by = _by_ids(el, el.get('aria-labelledby'))
    if by:
        return by
    ident = el.get('id')
    root = el.getroottree().getroot()
    if ident:
        for label in root.xpath('//label[@for=$i]', i=ident)[:1]:
            text = text_of(label)
            if text:
                return text
    for anc in el.iterancestors('label'):
        text = text_of(anc)
        if text:
            return text
        break
    parent = el.getparent()
    if parent is not None:
        prev = el.getprevious()
        hops = 0
        while prev is not None and hops < 2:
            if isinstance(prev.tag, str) and prev.tag not in ('input', 'select', 'textarea', 'button'):
                text = text_of(prev)
                if 0 < len(text) <= 60:
                    return text
            prev = prev.getprevious()
            hops += 1
        for label in parent.xpath('./label[not(@for)]')[:1]:
            text = text_of(label)
            if text:
                return text
    return ''


_CONTROL_ROLES = ('button', 'link', 'checkbox', 'radio', 'switch', 'textbox', 'combobox',
                  'menuitem', 'tab', 'option', 'searchbox')


def _is_control(node):
    return (isinstance(node.tag, str)
            and (node.tag in _CONTROL_TAGS or (node.get('role') or '').lower() in _CONTROL_ROLES))


def _inside(node, el):
    """True when node is el or one of el's ancestors or descendants."""
    if node is el:
        return True
    return any(a is el for a in node.iterancestors()) or any(a is node for a in el.iterancestors())


def _decorative(node, item):
    """True when node is in a subtree marked aria-hidden="true" (an avatar's initials, an
    icon) below item."""
    for anc in [node] + list(node.iterancestors()):
        if anc is item:
            return False
        if (anc.get('aria-hidden') or '').lower() == 'true':
            return True
    return False


def _item_texts(item, el):
    """The texts that name the row, list item or card that holds el, best first: its
    headings, its link texts, then the other text outside controls, then image alt text."""
    out = []

    def add(text):
        text = _SPACE.sub(' ', text or '').strip()
        if text and text not in out:
            out.append(text)

    for h in item.iter(*_HEADINGS):
        if not _inside(h, el) and not _decorative(h, item):
            add(text_of(h))
    for node in item.iter('a'):
        if (node.get('role') or 'link').lower() != 'link':
            continue   # a link styled as a button is an action, not the item's name
        if not _inside(node, el) and not _decorative(node, item):
            text = text_of(node)
            if len(text) > 1:
                add(text)
    short = []
    for node in item.iter():
        if not isinstance(node.tag, str) or _inside(node, el) or _decorative(node, item):
            continue
        if _is_control(node) or node.tag in ('option', 'script', 'style'):
            continue
        if any(_is_control(a) for a in node.iterancestors()
               if a is not item and any(b is item for b in a.iterancestors())):
            continue
        text = _SPACE.sub(' ', node.text or '').strip()
        if len(text) >= 3:
            add(text)
        elif len(text) > 1:
            short.append(text)
    for img in item.iter('img'):
        if _usable(img.get('alt')):
            add(img.get('alt'))
    for text in short:   # initials and the like, only when nothing else names the item
        add(text)
    return out


def _item_label(item, el):
    """A short name for the row, list item or card that holds el: its first text (see
    _item_texts), and the second one too when the first is short, so two rows of the same
    product in different variants ('Canvas cap · Red / M') stay apart."""
    texts = _item_texts(item, el)
    if not texts:
        return ''
    if len(texts) > 1 and len(texts[0]) < 24:
        return texts[0] + ' \u00b7 ' + texts[1]
    return texts[0]


_HEADING_XPATH = './/h1|.//h2|.//h3|.//h4|.//h5|.//h6|.//legend|.//*[@role="heading"]'


def _preceding_heading(node):
    """The text of the nearest heading before node among its earlier siblings (a heading
    itself, or the only heading inside a small header block)."""
    for sib in node.itersiblings(preceding=True):
        if not isinstance(sib.tag, str):
            continue
        if sib.tag in _HEADINGS or (sib.get('role') or '') == 'heading':
            text = text_of(sib)
            if text:
                return text
            continue
        if sib.tag in ('ul', 'ol', 'table', 'tbody', 'form', 'nav'):
            continue
        heads = sib.xpath(_HEADING_XPATH)
        if len(heads) == 1:
            text = text_of(heads[0])
            if text:
                return text
    return ''


_SCOPE_TAGS = ('header', 'nav', 'footer', 'aside', 'dialog')
_SCOPE_ROLES = ('dialog', 'alertdialog', 'banner', 'navigation', 'contentinfo', 'complementary')
_NAMED = ('section', 'form', 'fieldset', 'aside', 'nav', 'dialog', 'header', 'footer')
_NAMED_ROLES = ('dialog', 'alertdialog', 'region', 'group', 'radiogroup', 'navigation',
                'tabpanel', 'form', 'search')


_FIELD_TAGS = ('input', 'select', 'textarea')


def _paired_field(el):
    """The label of the form field a button or link works on, when the field sits just
    before it (a code box and its Apply button), else ''."""
    if _role_of(el) not in ('button', 'link'):
        return ''
    levels = [el]
    parent = el.getparent()
    if parent is not None and len([k for k in parent if isinstance(k.tag, str)]) <= 2:
        levels.append(parent)
    for node in levels:
        for sib in list(node.itersiblings(preceding=True))[:1]:
            if not isinstance(sib.tag, str):
                continue
            fields = [sib] if sib.tag in _FIELD_TAGS else sib.xpath('.//input|.//select|.//textarea')
            fields = [f for f in fields if (f.get('type') or '').lower() not in (
                'hidden', 'submit', 'button', 'checkbox', 'radio', 'reset', 'image')]
            if len(fields) == 1:
                label = _field_label(fields[0]) or fields[0].get('placeholder') or fields[0].get('aria-label')
                if label and _usable(label, 80):
                    return label
            if fields:
                return ''
    return ''


def _context(el):
    """What el sits in: the row, list item or card it belongs to, else the form field it
    belongs to (the code box before an Apply button), else the nearest heading before it
    (looking outwards, but not past a header, nav, footer, aside or dialog), else the name
    of the landmark or group around it."""
    for depth, anc in enumerate(el.iterancestors()):
        if not isinstance(anc.tag, str) or anc.tag in ('body', 'html') or depth > 8:
            break
        if anc.tag in _ITEM_TAGS or (anc.get('role') or '').lower() in _ITEM_ROLES:
            label = _item_label(anc, el)
            if label:
                return 'item ' + json.dumps(_clip(label, 32), ensure_ascii=False)
            break
    field = _paired_field(el)
    if field:
        return 'for ' + json.dumps(_clip(field, 32), ensure_ascii=False)
    node = el
    for depth, anc in enumerate(el.iterancestors()):
        if not isinstance(anc.tag, str) or anc.tag in ('body', 'html') or depth > 14:
            break
        role = (anc.get('role') or '').lower()
        if anc.tag in _ITEM_TAGS or role in _ITEM_ROLES:
            label = _item_label(anc, el)
            if label:
                return 'item ' + json.dumps(_clip(label, 32), ensure_ascii=False)
        text = _preceding_heading(node)
        if text:
            return json.dumps(_clip(text, 32), ensure_ascii=False)
        if anc.tag in _NAMED or role in _NAMED_ROLES:
            name = anc.get('aria-label') or _by_ids(anc, anc.get('aria-labelledby'))
            if name:
                return json.dumps(_clip(name, 32), ensure_ascii=False)
        if anc.tag in _SCOPE_TAGS or role in _SCOPE_ROLES:
            return ''
        node = anc
    return ''


def _href(value):
    if not _usable(value, 400):
        return ''
    value = value.strip()
    if value.startswith(('javascript:', 'mailto:', 'tel:')):
        return value.split(':', 1)[0] + ':'
    return _clip(value.split('?', 1)[0], ATTR_CLIP)


def _hashy(segment):
    """A class name part that looks machine-made: letters and digits mixed, or a capital
    letter inside a lower-case run (uIoob, kXhNjP)."""
    if len(segment) < 4:
        return False
    if any(c.isdigit() for c in segment) and any(c.isalpha() for c in segment):
        return True
    return any(c.isupper() for c in segment[1:]) and any(c.islower() for c in segment)


def meaningful_class(token):
    """False for a generated class name (css-1x2y3, sc-bdVaJa, Header_link__a1B2c,
    kXhNjP), a state word or a utility class; True for names a person chose."""
    if len(token) < 3 or len(token) > 32 or ':' in token or _HASHED_CLASS.search(token):
        return False
    if re.match(r'^(?:css|sc|jsx|svelte|astro|emotion)-', token, re.I):
        return False
    if any(_hashy(part) for part in re.split(r'[-_]+', token)):
        return False
    return True


_UTILITY = re.compile(r'^(?:p|m|px|py|mx|my|pt|pb|pl|pr|mt|mb|ml|mr|w|h|gap|text|bg|flex|grid|'
                      r'items|justify|rounded|border|shadow|font|leading|tracking|col|row|ring|'
                      r'space|inset|z|top|left|right|bottom|min|max|sm|md|lg|xl|hover|focus)-')


def _class_ok(token):
    """A class name worth showing: one a person chose, not a utility class."""
    return meaningful_class(token) and not _UTILITY.match(token)


def _classes(el):
    """Up to two class names that mean something (no hashes, no utility soup)."""
    return [t for t in stable_classes(el.get('class')) if _class_ok(t)][:2]


def describe(el):
    """The features of one element, as a dict with short keys and no empty values."""
    tag = el.tag.lower()
    role = (el.get('role') or '').strip().lower()
    typ = (el.get('type') or '').strip().lower()
    out = {}
    if tag == 'input':
        out['tag'] = 'input[' + (typ or 'text') + ']'
    elif role and role not in ('presentation', 'none'):
        out['tag'] = tag + '[role=' + role + ']'
    else:
        out['tag'] = tag
    gate_role = _role_of(el)
    if gate_role not in _FIELD_ROLES or tag in ('button', 'a'):
        text = text_of(el)
        if text:
            out['text'] = _clip(text, TEXT_CLIP)
    if gate_role in _FIELD_ROLES:
        label = _field_label(el)
        if label:
            out['label'] = _clip(label, TEXT_CLIP)
        if tag == 'select':
            options = [text_of(o) for o in el.iter('option')][:3]
            options = [o for o in options if o]
            if options:
                out['options'] = _clip(', '.join(options), TEXT_CLIP)
    for name, key in (('id', 'id'), ('name', 'name')):
        value = el.get(name)
        if _usable(value):
            out[key] = _clip(value, ATTR_CLIP)
    for name in TEST_ATTRS:
        value = el.get(name)
        if _usable(value):
            out['testid'] = _clip(value, ATTR_CLIP)
            break
    for name, key in (('aria-label', 'aria'), ('placeholder', 'ph'), ('title', 'title')):
        value = el.get(name)
        if _usable(value):
            out[key] = _clip(value, ATTR_CLIP)
    if 'aria' not in out and el.get('aria-labelledby') and gate_role not in _FIELD_ROLES:
        by = _by_ids(el, el.get('aria-labelledby'))
        if by:
            out['aria'] = _clip(by, ATTR_CLIP)
    if 'text' not in out:
        for img in el.iter('img'):
            if _usable(img.get('alt')):
                out['alt'] = _clip(img.get('alt'), ATTR_CLIP)
                break
    if tag == 'a' or el.get('href'):
        href = _href(el.get('href'))
        if href:
            out['href'] = href
    if el.get('autocomplete') and el.get('autocomplete') not in ('off', 'on'):
        out['auto'] = _clip(el.get('autocomplete'), 24)
    classes = _classes(el)
    if classes:
        out['cls'] = ' '.join(classes)
    ctx = _context(el)
    if ctx:
        out['in'] = ctx
    region = _region(el)
    if region:
        out['region'] = region
    hidden = _hidden(el)
    if hidden:
        out['hidden'] = hidden
    if el.get('disabled') is not None or (el.get('aria-disabled') or '').lower() == 'true':
        out['disabled'] = 'disabled'
    if (el.get('aria-current') or '') not in ('', 'false'):
        out['current'] = 'current'
    return out


_ORDER = ('text', 'label', 'id', 'name', 'testid', 'aria', 'ph', 'title', 'alt', 'href',
          'auto', 'options', 'cls')


def feature_line(features):
    """One candidate as a compact line: tag "text" key=value ... | in "..." | region."""
    parts = [features.get('tag', '?')]
    for key in _ORDER:
        value = features.get(key)
        if not value:
            continue
        if key == 'text':
            parts.append(json.dumps(value, ensure_ascii=False))
        elif key == 'cls':
            parts.append('class=' + json.dumps(value, ensure_ascii=False))
        else:
            parts.append(key + '=' + json.dumps(value, ensure_ascii=False))
    line = ' '.join(parts)
    ctx = features.get('in')
    if ctx:
        line += (' | ' if ctx.startswith('for ') else ' | in ') + ctx
    if features.get('region'):
        line += ' | ' + features['region']
    flags = [features[k] for k in ('hidden', 'disabled', 'current') if features.get(k)]
    if flags:
        line += ' | ' + ' '.join(flags)
    return line


# --- candidates -------------------------------------------------------------------------------

class Candidate:
    """One numbered candidate: its element in the bundle's document, its features and the
    line the model sees."""

    __slots__ = ('number', 'element', 'features', 'line')

    def __init__(self, number, element, features, line):
        self.number = number
        self.element = element
        self.features = features
        self.line = line

    def __repr__(self):
        return '<Candidate ' + str(self.number) + ': ' + self.line + '>'


class Candidates(list):
    """The candidate list for a bundle. ``doc`` is the parsed page, ``role`` the step's
    role and ``total`` how many elements of that role the page has (the list holds at most
    MAX_CANDIDATES of them)."""

    def __init__(self, items=(), *, doc=None, role=None, total=0):
        super().__init__(items)
        self.doc = doc
        self.role = role
        self.total = total

    def number_of(self, element):
        """The candidate number of ``element``, or None when it is not in the list."""
        for cand in self:
            if cand.element is element:
                return cand.number
        return None


def _selector_words(selector):
    """Words in an old selector: its quoted values and its attribute and tag names."""
    words = []
    for a, b in _LITERAL.findall(selector or ''):
        words.extend(tokens(a or b))
    bare = _LITERAL.sub(' ', selector or '')
    for word in re.findall(r'[#.]([A-Za-z][\w-]*)', bare):
        words.extend(tokens(word))
    return words


_NOISE = {'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'normalize', 'space',
          'contains', 'text', 'xpath', 'css', 'class', 'concat', 'div', 'span', 'id', 'name',
          'type', 'data', 'testid', 'aria', 'label', 'btn', 'button', 'input', 'link'}


def step_words(bundle):
    """The words that describe the step: from its key, old selectors, expected text,
    intent and saved fingerprint. Used to rank candidates when there are too many."""
    words = list(tokens(bundle.get('key')))
    for sel in bundle.get('candidates') or ():
        words.extend(_selector_words(sel))
    for text in bundle.get('expectedText') or ():
        words.extend(tokens(text))
    words.extend(tokens(bundle.get('intent')))
    fp = bundle.get('fingerprint')
    if isinstance(fp, dict):
        for key, value in fp.items():
            if isinstance(value, str) and key not in ('tag', 'region', 'role'):
                words.extend(tokens(value))
    return {w for w in words if w not in _NOISE and len(w) > 1}


def _score(features, words, fp_region):
    have = set()
    for key, value in features.items():
        if key in ('tag', 'region', 'hidden', 'disabled', 'current'):
            continue
        have.update(tokens(value))
    score = 0.0
    for w in words & have:
        score += 1.0 + min(len(w), 10) / 10.0
    for w in words:   # a stem in common (sign/signin, email/emailaddress)
        if w in have or len(w) < 4:
            continue
        if any(len(h) >= 4 and (h.startswith(w[:4]) or w.startswith(h[:4])) for h in have):
            score += 0.4
    region = features.get('region') or ''
    if fp_region and region == fp_region:
        score += 1.0
    elif fp_region and region.split('>')[-1].split('#')[0] == fp_region.split('>')[-1].split('#')[0]:
        score += 0.5
    if features.get('hidden') == 'hidden':
        score -= 0.3
    return score


def _fingerprint_scores(fp, elements):
    """How alike each element is to the saved fingerprint (mendwright.fingerprint.score,
    0 to 1), or zeros without one. It keeps the right element among the closest when its
    words changed (a translation), since its tag, classes and ancestors often did not."""
    if not _fp.valid(fp):
        return [0.0] * len(elements)
    out = []
    labels = _fp.label_index(elements[0].getroottree().getroot()) if elements else {}
    for el in elements:
        try:
            new = _fp.of_element(el, labels)
            out.append(_fp.score(fp, new).value if new else 0.0)
        except Exception:
            out.append(0.0)
    return out


def extract(bundle, limit=MAX_CANDIDATES):
    """The candidates for a capture bundle: every element of bundle['html'] whose role (as
    the gate computes it) is the step's expected role. When there are more than ``limit``,
    the ones that share the most words with the step are kept. The list is in document
    order and numbered from 1."""
    doc = parse_html(bundle.get('html') or '')
    role = bundle.get('expectedRole')
    found = []
    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag in ('html', 'head', 'body', 'title', 'meta', 'link', 'script', 'style', 'option'):
            continue
        if role and _role_of(el) == role:
            found.append(el)
    described = [(el, describe(el)) for el in found]
    if len(described) > limit:
        words = step_words(bundle)
        fp = bundle.get('fingerprint') if isinstance(bundle.get('fingerprint'), dict) else {}
        region = fp.get('region') if isinstance(fp.get('region'), str) else None
        if region is None and isinstance(fp.get('ancestors'), list):
            region = _chain(fp['ancestors'])[1] or None
        similar = _fingerprint_scores(fp, [el for el, _ in described])
        ranked = sorted(range(len(described)),
                        key=lambda i: (-(_score(described[i][1], words, region) + 3.0 * similar[i]), i))
        keep = sorted(ranked[:limit])
        described = [described[i] for i in keep]
    items = [Candidate(n, el, feats, feature_line(feats))
             for n, (el, feats) in enumerate(described, start=1)]
    return Candidates(items, doc=doc, role=role, total=len(found))


# --- the task ----------------------------------------------------------------------------------

def _landmark_id(ident):
    if (isinstance(ident, str) and ident and _usable(ident, 30) and not generated(ident)
            and re.match(r'^[A-Za-z][\w-]*$', ident)):
        return ident
    return ''


def _chain(ancestors):
    """The ancestors of a fingerprint as 'li.cart-item < ul < main' (at most three), and
    the landmarks among them written like a candidate's region ('main>form#login')."""
    parts = []
    found = []
    for anc in ancestors[:4]:
        if not isinstance(anc, dict) or not isinstance(anc.get('tag'), str):
            continue
        tag = anc['tag']
        role = anc.get('role') if isinstance(anc.get('role'), str) else ''
        for name, tags, roles in _LANDMARKS:
            if tag in tags or role in roles:
                if not found or found[-1][0] != name:
                    found.append((name, _landmark_id(anc.get('id'))))
                break
        if len(parts) >= 3:
            continue
        part = tag
        ident = anc.get('id')
        if isinstance(ident, str) and _usable(ident, 40) and not generated(ident):
            part += '#' + ident
        classes = anc.get('classes')
        if isinstance(classes, list) and classes and isinstance(classes[0], str) \
                and _class_ok(classes[0]):
            part += '.' + classes[0]
        parts.append(part)
    found.reverse()
    region = ''
    for name, ident in found:
        if name == 'dialog':
            region = 'dialog' + ('#' + ident if ident else '')
            break
    else:
        region = '>'.join(name + ('#' + ident if ident else '') for name, ident in found[-2:])
    return ' < '.join(parts), region


def fingerprint_line(fp):
    """What is known about the element the step used to match, as one line in the shape of
    a candidate line. Reads the fingerprint the store keeps for a key
    (mendwright.fingerprint: tag, attrs, classes, text, label, ancestors); a flat dict with
    the same names works too. Unknown keys are ignored."""
    if not isinstance(fp, dict) or not fp:
        return ''
    attrs = fp.get('attrs') if isinstance(fp.get('attrs'), dict) else {}

    def pick(*names):
        for name in names:
            for source in (fp, attrs):
                value = source.get(name)
                if isinstance(value, str) and _usable(value, 400):
                    return value
        return ''

    feats = {}
    tag = fp.get('tag')
    if isinstance(tag, str) and tag:
        typ = pick('type')
        role = pick('role')
        if tag == 'input':
            feats['tag'] = 'input[' + (typ or 'text') + ']'
        elif role:
            feats['tag'] = tag + '[role=' + role + ']'
        else:
            feats['tag'] = tag
    mapping = (('text', ('text',), TEXT_CLIP), ('label', ('label',), TEXT_CLIP),
               ('id', ('id',), ATTR_CLIP), ('name', ('name',), ATTR_CLIP),
               ('testid', ('testid',) + TEST_ATTRS, ATTR_CLIP),
               ('aria', ('aria', 'aria-label'), ATTR_CLIP),
               ('ph', ('ph', 'placeholder'), ATTR_CLIP), ('title', ('title',), ATTR_CLIP),
               ('alt', ('alt',), ATTR_CLIP), ('auto', ('autocomplete',), 24))
    for key, names, clip in mapping:
        value = pick(*names)
        if value and not (key == 'auto' and value in ('on', 'off')):
            feats[key] = _clip(value, clip)
    href = pick('href')
    if href:
        feats['href'] = _href(href)
    classes = fp.get('classes')
    if isinstance(classes, list):
        good = [c for c in classes if isinstance(c, str) and _class_ok(c)][:2]
        if good:
            feats['cls'] = ' '.join(good)
    ctx = pick('in', 'context', 'heading')
    if ctx:
        feats['in'] = ctx if ctx.startswith(('"', 'item ')) else json.dumps(_clip(ctx, 32), ensure_ascii=False)
    chain, region = _chain(fp.get('ancestors') if isinstance(fp.get('ancestors'), list) else [])
    region = pick('region') or region
    if region:
        feats['region'] = region
    if len(feats) <= 1 and 'tag' in feats and not chain:
        return ''
    feats.setdefault('tag', '?')
    line = feature_line(feats)
    if chain:
        line += ' | within ' + chain
    return line


def _page_line(bundle):
    page, last = bundle.get('page'), bundle.get('lastPage')
    if page and last:
        if page == last:
            return 'page: ' + page + ' (where it was last found)'
        return 'page: ' + page + ' (it was last found on ' + last + ')'
    if page:
        return 'page: ' + page
    if last:
        return 'page: unknown (it was last found on ' + last + ')'
    return ''


def _plural(role, n):
    word = role or 'element'
    if n == 1:
        return word
    if word.endswith(('x', 's')):
        return word + 'es'
    return word + 's'


def format_task(bundle, candidates):
    """The (system, user) messages for one heal: the step and the numbered candidates."""
    role = bundle.get('expectedRole') or 'unknown'
    lines = ['Step', 'key: ' + str(bundle.get('key') or '')]
    lines.append('role: ' + role)
    for sel in list(bundle.get('candidates') or ())[:3]:
        lines.append('old selector: ' + _clip(str(sel), 160))
    texts = [t for t in (bundle.get('expectedText') or ()) if isinstance(t, str) and t.strip()]
    if texts:
        lines.append('expected text: ' + ', '.join(json.dumps(_clip(t, 60), ensure_ascii=False)
                                                   for t in texts[:3]))
    if isinstance(bundle.get('intent'), str) and bundle['intent'].strip():
        lines.append('intent: ' + _clip(bundle['intent'], 120))
    known = fingerprint_line(bundle.get('fingerprint'))
    if known:
        lines.append('it was: ' + known)
    page = _page_line(bundle)
    if page:
        lines.append(page)
    lines.append('')
    total = getattr(candidates, 'total', len(candidates))
    head = 'Candidates (' + str(len(candidates)) + ' ' + _plural(role, len(candidates))
    if total > len(candidates):
        head += ', the ' + str(len(candidates)) + ' closest of ' + str(total)
    lines.append(head + ')')
    if not candidates:
        lines.append('(none)')
    for cand in candidates:
        lines.append(str(cand.number) + '. ' + cand.line)
    return SYSTEM_PROMPT, '\n'.join(lines)


def answer_text(pick=None, why=None):
    """The reply the model is trained to give."""
    if pick is not None:
        return json.dumps({'pick': int(pick)})
    return json.dumps({'pick': None, 'why': why or 'removed'})


def parse_answer(text):
    """The model's reply as {'pick': int or None, 'why': str or None}, or None when the
    reply is not a JSON object with a "pick" that is a positive integer or null."""
    if not isinstance(text, str):
        return None
    match = re.search(r'\{[\s\S]*?\}', text)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict) or 'pick' not in obj:
        return None
    pick = obj.get('pick')
    why = obj.get('why')
    why = why.strip() if isinstance(why, str) and why.strip() else None
    if pick is None:
        return {'pick': None, 'why': why or 'removed'}
    if isinstance(pick, bool):
        return None
    if isinstance(pick, str) and pick.strip().isdigit():
        pick = int(pick.strip())
    if not isinstance(pick, int) or pick < 1:
        return None
    return {'pick': pick, 'why': None}


# --- XPath for the picked element --------------------------------------------------------------

def _hits(doc, xpath):
    try:
        found = doc.xpath(xpath)
    except (lxml.etree.XPathError, TypeError, ValueError):
        return None
    if not isinstance(found, list):
        return None
    return [h for h in found if isinstance(getattr(h, 'tag', None), str)]


def _unique(doc, xpath, el):
    hits = _hits(doc, xpath)
    return hits is not None and len(hits) == 1 and hits[0] is el


def _text_pred(el):
    """normalize-space()='text' for el, or contains() when el holds an <svg> (whose
    content the captured page does not have)."""
    text = text_of(el)
    if not (0 < len(text) <= 80) or not _usable(text, 80):
        return None
    if el.find('.//svg') is not None:
        return 'contains(normalize-space(), ' + xpath_literal(text) + ')'
    return 'normalize-space()=' + xpath_literal(text)


def _own_predicates(el):
    """Predicates on el itself, best first, with how much each can be trusted."""
    preds = []
    ident = el.get('id')
    if _usable(ident) and not generated(ident):
        preds.append('@id=' + xpath_literal(ident))
    for name in TEST_ATTRS:
        value = el.get(name)
        if _usable(value) and not generated(value):
            preds.append('@' + name + '=' + xpath_literal(value))
            break
    for name in ('name', 'aria-label', 'placeholder', 'title', 'formcontrolname', 'for'):
        value = el.get(name)
        if _usable(value) and not (name == 'name' and generated(value)):
            preds.append('@' + name + '=' + xpath_literal(value))
    text = _text_pred(el)
    if text and el.tag not in ('input', 'select', 'textarea'):
        preds.append(text)
    href = el.get('href')
    if _usable(href) and not href.startswith('javascript') and href != '#':
        preds.append('@href=' + xpath_literal(href))
    if el.get('autocomplete') and _usable(el.get('autocomplete')):
        preds.append('@autocomplete=' + xpath_literal(el.get('autocomplete')))
    if _usable(ident) and generated(ident):
        preds.append('@id=' + xpath_literal(ident))
    return preds


def _label_anchors(el):
    """XPaths that find a form field through its label text."""
    out = []
    tag = el.tag
    ident = el.get('id')
    root = el.getroottree().getroot()
    if ident and _usable(ident):
        for label in root.xpath('//label[@for=$i]', i=ident)[:1]:
            text = text_of(label)
            if _usable(text, 80) and text:
                out.append('//' + tag + '[@id=//label[normalize-space()=' + xpath_literal(text)
                           + ']/@for]')
    for label in el.iterancestors('label'):
        text = text_of(label)
        if _usable(text, 80) and text:
            out.append('//label[normalize-space()=' + xpath_literal(text) + ']//' + tag)
        break
    return out


def _anchor_predicates(anc, el):
    """Predicates that name an ancestor of el: its id, test id, aria-label, or a heading or
    a cell text inside it."""
    preds = []
    for name in ('id',) + TEST_ATTRS + ('aria-label', 'name'):
        value = anc.get(name)
        if _usable(value) and not (name == 'id' and generated(value)):
            preds.append('@' + name + '=' + xpath_literal(value))
    if anc.tag in _CONTAINER_TAGS or (anc.get('role') or '') in _ITEM_ROLES + ('dialog', 'region', 'group'):
        probes = _HEADINGS + ('label', 'strong', 'b', 'td', 'th', 'span', 'p')
        seen = 0
        for node in anc.iter(*probes):
            if node is el or el in node.iterancestors() or node in el.iterancestors():
                continue
            text = text_of(node)
            if 0 < len(text) <= 60 and _usable(text, 60):
                literal = '[normalize-space()=' + xpath_literal(text) + ']'
                if node.getparent() is anc:
                    # a direct child: the card or section itself, not a grid that holds it
                    preds.append(node.tag + literal)
                preds.append('.//' + node.tag + literal)
                seen += 1
                if seen >= 2:
                    break
    return preds


def robust_xpaths(el, doc=None, limit=4):
    """Up to ``limit`` different XPaths that each match ``el`` and nothing else in
    ``doc``, most robust first: own stable attributes, label anchors, two attributes
    together, then an ancestor anchor (a container's id or a heading, row or card text)
    combined with the element's own attributes. When el has a hidden copy (a mobile menu,
    a sticky bar) that nothing else tells apart, the XPath also says el is not hidden.
    Never a bare position."""
    if doc is None:
        doc = el.getroottree().getroot()
    tag = el.tag
    out = []
    near = []   # XPaths that match el and others: tried again with VISIBLE at the end

    def add(xpath):
        if xpath in out:
            return len(out) >= limit
        hits = _hits(doc, xpath)
        if hits and len(hits) == 1 and hits[0] is el:
            out.append(xpath)
        elif hits and len(near) < 40 and any(h is el for h in hits):
            near.append(xpath)
        return len(out) >= limit

    own = _own_predicates(el)
    typed = []
    if tag == 'input' and el.get('type'):
        typed.append('@type=' + xpath_literal(el.get('type')))
    role = el.get('role')
    if role and _usable(role):
        typed.append('@role=' + xpath_literal(role))
    for pred in own:
        if add('//' + tag + '[' + pred + ']'):
            return out
    for xpath in _label_anchors(el):
        if add(xpath):
            return out
    pairs = own[:5] + typed
    for i, first in enumerate(pairs):
        for second in pairs[i + 1:]:
            if add('//' + tag + '[' + first + ' and ' + second + ']'):
                return out
    if len(out) >= 2:
        return out
    tails = [None] + own[:4] + typed[:1]
    depth = 0
    for anc in el.iterancestors():
        if not isinstance(anc.tag, str) or anc.tag in ('html', 'body'):
            break
        depth += 1
        if depth > 8:
            break
        for apred in _anchor_predicates(anc, el):
            for pred in tails:
                xpath = ('//' + anc.tag + '[' + apred + ']//' + tag
                         + ('[' + pred + ']' if pred else ''))
                if add(xpath):
                    return out
        if anc.tag in ('nav', 'header', 'footer', 'main', 'aside', 'form', 'dialog'):
            for pred in own[:4] + typed[:1]:
                if add('//' + anc.tag + '//' + tag + '[' + pred + ']'):
                    return out
    if not out:
        for xpath in list(near):
            if add(xpath + '[' + VISIBLE + ']'):
                return out
    return out


# True for an element that no inline style, hidden attribute or aria-hidden hides.
VISIBLE = ("not(ancestor-or-self::*[@hidden or @aria-hidden='true' or "
           "contains(translate(@style, ' ', ''), 'display:none') or "
           "contains(translate(@style, ' ', ''), 'visibility:hidden')])")


def robust_xpath(el, doc=None):
    """The most robust unique XPath for ``el`` in ``doc`` (see robust_xpaths), or None
    when no XPath without a bare position is unique."""
    found = robust_xpaths(el, doc, limit=1)
    return found[0] if found else None
