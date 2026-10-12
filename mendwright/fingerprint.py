"""Fingerprint healing: a first tier that needs no model.

Each time a key's selector matches, mendwright records a fingerprint of the element: its
tag, the attributes that tend to survive a redesign (id, name, type, role, test ids,
aria-label, placeholder, title, alt, autocomplete, the path of href), its class tokens that
do not look generated, its visible text, the text of its label for a form field, and the
tag, id, classes and role of up to 4 ancestors. Input values are never read.

When the key's selectors stop matching, every element of the captured page that has the
key's role is scored against the fingerprint. A heal is proposed only when the best score
reaches THRESHOLD, the best element carries at least one strong identity signal (the same
id that is not a generated one, test id, name, link path or autocomplete value, or nearly
the same text, label or aria-label), and no other visible element comes within MARGIN of
it. The proposal is a unique, non-positional XPath, and it
goes through the same checks as a model's proposal (client.apply: validate() against the
captured HTML, then the live check).

The browser reads the element with FINGERPRINT_JS in one round trip; an lxml element goes
through raw_of_element(). Both give the same raw shape, and from_raw() is the one place
that normalizes it, so a fingerprint read in a browser and one computed from captured HTML
compare the same way.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

import lxml.html

from .validation import _role_of

# Version of the stored shape. A fingerprint with another version is ignored.
VERSION = 1

# A heal needs a score of at least THRESHOLD (0 to 1) and a strong identity signal, and the
# next best element must score at least MARGIN lower. Tuned on benchmarks/cases, where the
# tier makes no wrong heal with these values (see benchmarks/RESULTS.md).
THRESHOLD = 0.40
MARGIN = 0.12

ANCESTORS = 4
TEXT_LIMIT = 80
VALUE_LIMIT = 120

# Attributes read from the element. value is never read: it holds what a user typed.
ATTRS = ('id', 'name', 'type', 'role', 'class', 'data-testid', 'data-test', 'data-qa',
         'aria-label', 'placeholder', 'title', 'alt', 'href', 'autocomplete')
ANCESTOR_ATTRS = ('id', 'class', 'role')
TEST_ATTRS = ('data-testid', 'data-test', 'data-qa')
_LOWER = ('type', 'role', 'autocomplete')

# One round trip: Playwright's locator.evaluate_all() passes the matches as an array, and
# Selenium runs it as (FINGERPRINT_JS)([element]). null unless exactly one element matched.
FINGERPRINT_JS = r"""(els) => {
  if (!els || els.length !== 1) return null;
  const el = els[0];
  const read = (e, names) => {
    const out = {};
    for (const n of names) { const v = e.getAttribute(n); if (v !== null) out[n] = v; }
    return out;
  };
  const tag = el.tagName.toLowerCase();
  const role = (el.getAttribute('role') || '').toLowerCase();
  const field = tag === 'input' || tag === 'textarea' || tag === 'select'
    || el.isContentEditable || /^(textbox|searchbox|combobox|spinbutton|slider)$/.test(role);
  let label = null;
  if (field) {
    const wrap = el.parentElement ? el.parentElement.closest('label') : null;
    if (wrap) {
      label = wrap.textContent;
    } else {
      const id = el.getAttribute('id');
      if (id) {
        for (const l of el.ownerDocument.getElementsByTagName('label')) {
          if (l.getAttribute('for') === id) { label = l.textContent; break; }
        }
      }
    }
  }
  const ancestors = [];
  let p = el.parentElement;
  while (p && ancestors.length < 4) {
    const t = p.tagName.toLowerCase();
    if (t === 'body' || t === 'html') break;
    ancestors.push({tag: t, attrs: read(p, ['id', 'class', 'role'])});
    p = p.parentElement;
  }
  return {tag: tag, attrs: read(el, ['id', 'name', 'type', 'role', 'class', 'data-testid',
          'data-test', 'data-qa', 'aria-label', 'placeholder', 'title', 'alt', 'href',
          'autocomplete']),
          field: field, text: field ? null : el.textContent, label: label,
          ancestors: ancestors};
}"""

# XPath's normalize-space() collapses only these, so a non-breaking space stays text.
_SPACE = re.compile(r'[ \t\n\r\f]+')
_LONGNUM = re.compile(r'(?<!\d)\d{12,19}(?!\d)')
_FIELD_ROLES = ('textbox', 'searchbox', 'combobox', 'spinbutton', 'slider')


def _norm(text):
    if not isinstance(text, str):
        return ''
    return _SPACE.sub(' ', text).strip()


def _text(text):
    """Visible text as stored: whitespace collapsed, long digit runs masked, cut short."""
    return _LONGNUM.sub('#', _norm(text))[:TEXT_LIMIT]


# --- class tokens -------------------------------------------------------------------------

# Prefixes of class names that CSS-in-JS tools generate: emotion (css-), styled-components
# (sc-), styled-jsx (jsx-), Svelte, Astro.
_GENERATED_PREFIX = re.compile(r'^(?:css|sc|jsx|svelte|astro|emotion)-', re.IGNORECASE)
# CSS modules: Header_cartLink__uIoob, _root_1x2y3_1.
_MODULE_HASH = re.compile(r'(?:__|_)[A-Za-z0-9-]{5,}$')
# Classes that say what state an element is in, not what it is.
_STATE = frozenset({'active', 'is-active', 'selected', 'is-selected', 'focus', 'focused',
                    'hover', 'disabled', 'is-disabled', 'open', 'is-open', 'show', 'shown',
                    'collapsed', 'expanded', 'visible', 'hidden', 'checked', 'current'})


def _hashy(segment):
    """A segment that looks machine-made: letters and digits mixed, or a capital letter
    inside a lower-case run (uIoob, mcGkxu)."""
    if len(segment) < 4:
        return False
    has_digit = any(c.isdigit() for c in segment)
    has_alpha = any(c.isalpha() for c in segment)
    if has_digit and has_alpha:
        return True
    return any(c.isupper() for c in segment[1:]) and any(c.islower() for c in segment)


def stable_classes(value):
    """The class tokens of a class attribute that are likely to survive a rebuild, in
    order, without duplicates: generated names (css-1x2y3, sc-bdVaJa, Header_link__a1B2c),
    tokens with a machine-made part, Angular's ng- state classes and state words such as
    active are left out."""
    if not isinstance(value, str):
        return []
    out = []
    for token in value.split():
        low = token.lower()
        if low in _STATE or low.startswith('ng-') or _GENERATED_PREFIX.match(token):
            continue
        if '__' in token and _MODULE_HASH.search(token):
            continue
        if any(_hashy(part) for part in re.split(r'[-_]+', token)):
            continue
        if token not in out:
            out.append(token)
    return out[:8]


def generated_id(value):
    """True for an id that a framework generated (:r3:, mui-generated-8842, ember123,
    x1n2onr6): it has a colon, a run of 3 or more digits, or a part of 5 or more characters
    that mixes letters and digits. A camelCase id such as lastName is not generated."""
    if not isinstance(value, str) or not value:
        return True
    if ':' in value or re.search(r'\d{3,}', value):
        return True
    return any(len(part) >= 5 and any(c.isdigit() for c in part) and any(c.isalpha() for c in part)
               for part in re.split(r'[-_.]+', value))


def _href_path(value):
    """The path of a link target, without scheme, host, query or fragment, and without a
    trailing slash. '' for a fragment-only or script link."""
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if not text or text.startswith(('#', 'javascript:', 'mailto:', 'tel:', 'data:')):
        return ''
    try:
        path = urlsplit(text).path
    except ValueError:
        return ''
    if len(path) > 1:
        path = path.rstrip('/')
    return path[:VALUE_LIMIT]


# --- the raw read, from lxml --------------------------------------------------------------

def _is_field(el):
    tag = el.tag.lower() if isinstance(el.tag, str) else ''
    if tag in ('input', 'textarea', 'select'):
        return True
    if (el.get('role') or '').strip().lower() in _FIELD_ROLES:
        return True
    node = el
    while node is not None:
        editable = node.get('contenteditable')
        if editable is not None:
            return editable.strip().lower() in ('', 'true', 'plaintext-only')
        node = node.getparent()
    return False


def _read(el, names):
    return {n: el.get(n) for n in names if el.get(n) is not None}


def label_index(root):
    """{for: text} for the <label for=...> elements of a document, the first one for each
    id, as the browser's getElementsByTagName('label') loop finds it."""
    out = {}
    for label in root.iter('label'):
        target = label.get('for')
        if target is not None and target not in out:
            out[target] = label.text_content()
    return out


def raw_of_element(el, labels=None):
    """What FINGERPRINT_JS returns for ``el``, computed with lxml. ``labels`` is a
    label_index() of the document, or None to look the label up here."""
    tag = el.tag.lower() if isinstance(el.tag, str) else ''
    field = _is_field(el)
    label = None
    if field:
        wrap = next(el.iterancestors('label'), None)
        if wrap is not None:
            label = wrap.text_content()
        elif el.get('id'):
            if labels is None:
                labels = label_index(el.getroottree().getroot())
            label = labels.get(el.get('id'))
    ancestors = []
    for anc in el.iterancestors():
        if len(ancestors) >= ANCESTORS:
            break
        name = anc.tag.lower() if isinstance(anc.tag, str) else ''
        if name in ('body', 'html'):
            break
        ancestors.append({'tag': name, 'attrs': _read(anc, ANCESTOR_ATTRS)})
    return {'tag': tag, 'attrs': _read(el, ATTRS), 'field': field,
            'text': None if field else el.text_content(), 'label': label,
            'ancestors': ancestors}


# --- normalizing ----------------------------------------------------------------------------

def from_raw(raw):
    """A stored fingerprint from FINGERPRINT_JS's result (or raw_of_element()'s), or None
    when ``raw`` is not that shape. Every value is normalized here and nowhere else."""
    if not isinstance(raw, dict) or not isinstance(raw.get('tag'), str) or not raw['tag']:
        return None
    attrs = raw.get('attrs')
    if not isinstance(attrs, dict) or not isinstance(raw.get('ancestors', []), list):
        return None
    fp = {'v': VERSION, 'tag': raw['tag'].strip().lower()}
    own = {}
    for name in ATTRS:
        value = attrs.get(name)
        if name == 'class' or not isinstance(value, str):
            continue
        value = _href_path(value) if name == 'href' else _norm(value)[:VALUE_LIMIT]
        if name in _LOWER:
            value = value.lower()
        if value:
            own[name] = value
    if own:
        fp['attrs'] = own
    classes = stable_classes(attrs.get('class'))
    if classes:
        fp['classes'] = classes
    if not raw.get('field'):
        text = _text(raw.get('text'))
        if text:
            fp['text'] = text
    label = _text(raw.get('label'))
    if label:
        fp['label'] = label
    chain = []
    for item in (raw.get('ancestors') or [])[:ANCESTORS]:
        if not isinstance(item, dict) or not isinstance(item.get('tag'), str):
            return None
        a_attrs = item.get('attrs') if isinstance(item.get('attrs'), dict) else {}
        node = {'tag': item['tag'].strip().lower()}
        if isinstance(a_attrs.get('id'), str) and _norm(a_attrs['id']):
            node['id'] = _norm(a_attrs['id'])[:VALUE_LIMIT]
        a_classes = stable_classes(a_attrs.get('class'))
        if a_classes:
            node['classes'] = a_classes
        if isinstance(a_attrs.get('role'), str) and _norm(a_attrs['role']):
            node['role'] = _norm(a_attrs['role']).lower()
        chain.append(node)
    if chain:
        fp['ancestors'] = chain
    return fp


def of_element(el, labels=None):
    """The fingerprint of an lxml element."""
    return from_raw(raw_of_element(el, labels))


def valid(fp):
    """True when ``fp`` is a stored fingerprint this version can score."""
    return (isinstance(fp, dict) and fp.get('v') == VERSION and isinstance(fp.get('tag'), str)
            and bool(fp['tag']))


# --- scoring --------------------------------------------------------------------------------

WEIGHTS = {
    'tag': 1.0, 'id': 3.0, 'test': 3.0, 'name': 2.0, 'aria-label': 2.0, 'placeholder': 1.5,
    'title': 1.0, 'alt': 1.0, 'href': 2.0, 'autocomplete': 2.0, 'type': 0.5, 'role': 0.5,
    'classes': 1.0, 'text': 3.0, 'label': 3.0, 'ancestors': 2.0,
}
_RATIO_ATTRS = ('placeholder', 'title', 'alt')
_EXACT_ATTRS = ('type', 'role')
# Two texts count as the same text (a strong signal) at this ratio, or at LONG_SAME for a
# text of LONG_TEXT characters or more, where a few changed words leave most of it alike.
SAME_TEXT = 0.9
LONG_SAME = 0.8
LONG_TEXT = 16
# autocomplete values that say nothing about which field it is.
_VAGUE_AUTOCOMPLETE = ('on', 'off', 'nope', 'false', 'true', 'none')
_TOKEN = re.compile(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+')


def _ratio(a, b):
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def _same_text(text, ratio):
    return ratio >= SAME_TEXT or (ratio >= LONG_SAME and len(text) >= LONG_TEXT)


def _tokens(value):
    """The words of an id or name: user[last_name] and lastName both give {last, name}."""
    return {t.lower() for t in _TOKEN.findall(value or '')}


def _jaccard(a, b):
    a, b = set(a or ()), set(b or ())
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _ancestor_similarity(old, new):
    weights = (1.0 + (2.0 if old.get('id') else 0) + (1.0 if old.get('classes') else 0)
               + (0.5 if old.get('role') else 0))
    got = 1.0 if old['tag'] == new.get('tag') else 0.0
    if old.get('id'):
        got += 2.0 if old['id'] == new.get('id') else 0.0
    if old.get('classes'):
        got += _jaccard(old['classes'], new.get('classes'))
    if old.get('role'):
        got += 0.5 if old['role'] == new.get('role') else 0.0
    return got / weights


def _chain_similarity(old, new):
    if not old:
        return None
    if not new:
        return 0.0
    return sum(max(_ancestor_similarity(a, b) for b in new) for a in old) / len(old)


@dataclass
class Score:
    value: float
    strong: bool


def _identifier(old, new, generated):
    """(similarity, strong) of two ids or names: 1 and strong when equal; otherwise how
    many words they share, scaled down, and never strong. A generated id is never strong."""
    if old == new:
        return 1.0, not generated
    return 0.6 * _jaccard(_tokens(old), _tokens(new)) if new else 0.0, False


def score(old, new):
    """How alike two fingerprints are, from 0 to 1, as a weighted average over what the
    recorded fingerprint ``old`` has. strong is True when ``new`` shares at least one
    identity signal with it: the same id (not a generated one), test id, name, link path
    or autocomplete token, or nearly the same text, label or aria-label."""
    old_attrs, new_attrs = old.get('attrs') or {}, new.get('attrs') or {}
    parts = [(WEIGHTS['tag'], 1.0 if old['tag'] == new.get('tag') else 0.0)]
    strong = False
    if old_attrs.get('id'):
        generated = generated_id(old_attrs['id'])
        sim, hit = _identifier(old_attrs['id'], new_attrs.get('id'), generated)
        parts.append((1.0 if generated else WEIGHTS['id'], sim))
        strong = strong or hit
    tests = [old_attrs[n] for n in TEST_ATTRS if old_attrs.get(n)]
    if tests:
        theirs = {new_attrs.get(n) for n in TEST_ATTRS if new_attrs.get(n)}
        hits = sum(1 for t in tests if t in theirs)
        parts.append((WEIGHTS['test'], hits / len(tests)))
        strong = strong or hits > 0
    if old_attrs.get('name'):
        sim, hit = _identifier(old_attrs['name'], new_attrs.get('name'), False)
        parts.append((WEIGHTS['name'], sim))
        strong = strong or hit
    auto = old_attrs.get('autocomplete')
    if auto and auto not in _VAGUE_AUTOCOMPLETE:
        same = auto == new_attrs.get('autocomplete')
        parts.append((WEIGHTS['autocomplete'], 1.0 if same else 0.0))
        strong = strong or same
    for name in _EXACT_ATTRS:
        if old_attrs.get(name):
            parts.append((WEIGHTS[name], 1.0 if old_attrs[name] == new_attrs.get(name) else 0.0))
    for name in _RATIO_ATTRS:
        if old_attrs.get(name):
            parts.append((WEIGHTS[name], _ratio(old_attrs[name], new_attrs.get(name))))
    # A field's name on screen may move between a <label> and aria-label in a redesign.
    names = [new.get('label'), new_attrs.get('aria-label')]
    for weight, text in ((WEIGHTS['aria-label'], old_attrs.get('aria-label')),
                         (WEIGHTS['label'], old.get('label'))):
        if text:
            ratio = max(_ratio(text, other) for other in names)
            parts.append((weight, ratio))
            strong = strong or (_same_text(text, ratio) and len(text) >= 2)
    if old_attrs.get('href'):
        same = old_attrs['href'] == new_attrs.get('href')
        parts.append((WEIGHTS['href'], 1.0 if same else _ratio(old_attrs['href'],
                                                              new_attrs.get('href'))))
        strong = strong or (same and old_attrs['href'] != '/')
    if old.get('classes'):
        parts.append((WEIGHTS['classes'], _jaccard(old['classes'], new.get('classes'))))
    if old.get('text'):
        ratio = _ratio(old['text'], new.get('text'))
        parts.append((WEIGHTS['text'], ratio))
        strong = strong or (_same_text(old['text'], ratio) and len(old['text']) >= 2)
    chain = _chain_similarity(old.get('ancestors'), new.get('ancestors'))
    if chain is not None:
        parts.append((WEIGHTS['ancestors'], chain))
    total = sum(w for w, _ in parts)
    return Score(sum(w * s for w, s in parts) / total if total else 0.0, strong)


# --- an XPath for the winner ------------------------------------------------------------------

def xpath_literal(value):
    """``value`` as an XPath 1.0 string literal."""
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return 'concat(' + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ')'


_SAFE_TAG = re.compile(r'^[a-z][a-z0-9-]*$')
_PRED_ATTRS = ('data-testid', 'data-test', 'data-qa', 'data-cy', 'id', 'name', 'aria-label',
               'placeholder', 'title', 'alt', 'href', 'autocomplete', 'for', 'type', 'role')
_ANCHOR_ATTRS = ('id', 'data-testid', 'data-test', 'data-qa', 'aria-label')
_HEADINGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'legend', 'caption')


def _usable(value):
    return isinstance(value, str) and value.strip() != '' and len(value) <= VALUE_LIMIT \
        and '\n' not in value


def _own_predicates(el):
    preds, late = [], []
    for name in _PRED_ATTRS:
        value = el.get(name)
        if not _usable(value):
            continue
        pred = '@' + name + '=' + xpath_literal(value)
        if name == 'id' and generated_id(value):
            late.append(pred)       # unique now, but a rebuild will change it
        else:
            preds.append(pred)
    if not _is_field(el):
        text = _norm(el.text_content())
        if 0 < len(text) <= TEXT_LIMIT:
            preds.insert(min(len(preds), 4), 'normalize-space()=' + xpath_literal(text))
    for token in stable_classes(el.get('class'))[:3]:
        preds.append("contains(concat(' ', normalize-space(@class), ' '), "
                     + xpath_literal(' ' + token + ' ') + ')')
    return preds + late


def _anchor_predicates(anc):
    preds = []
    for name in _ANCHOR_ATTRS:
        value = anc.get(name)
        if _usable(value) and not (name == 'id' and generated_id(value)):
            preds.append('@' + name + '=' + xpath_literal(value))
    for probe in _HEADINGS:
        for hit in anc.iter(probe):
            text = _norm(hit.text_content())
            if 0 < len(text) <= 60:
                preds.append('.//' + probe + '[normalize-space()=' + xpath_literal(text) + ']')
            break
    return preds


def xpath_for(el):
    """A non-positional XPath that matches ``el`` and nothing else in its document, built
    from test ids, ids, names, labels, text and nearby anchors, or None when there is none.
    Never an index-only path."""
    tag = el.tag.lower() if isinstance(el.tag, str) else ''
    if not _SAFE_TAG.match(tag):
        return None
    root = el.getroottree().getroot()

    def unique(xp):
        try:
            hits = [h for h in root.xpath(xp) if isinstance(getattr(h, 'tag', None), str)]
        except Exception:
            return False
        return len(hits) == 1 and hits[0] is el

    own = _own_predicates(el)
    for pred in own:
        xp = '//' + tag + '[' + pred + ']'
        if unique(xp):
            return xp
    if _is_field(el):
        label = next(el.iterancestors('label'), None)
        if label is not None:
            text = _norm(label.text_content())
            if 0 < len(text) <= TEXT_LIMIT:
                xp = '//label[normalize-space()=' + xpath_literal(text) + ']//' + tag
                if unique(xp):
                    return xp
        elif _usable(el.get('id')):
            for lab in root.iter('label'):
                if lab.get('for') == el.get('id'):
                    text = _norm(lab.text_content())
                    if 0 < len(text) <= TEXT_LIMIT:
                        xp = ('//' + tag + '[@id=//label[normalize-space()='
                              + xpath_literal(text) + ']/@for]')
                        if unique(xp):
                            return xp
                    break
    for i, first in enumerate(own[:6]):
        for second in own[i + 1:6]:
            xp = '//' + tag + '[' + first + ' and ' + second + ']'
            if unique(xp):
                return xp
    for anc in el.iterancestors():
        name = anc.tag.lower() if isinstance(anc.tag, str) else ''
        if name in ('html', 'body') or not _SAFE_TAG.match(name):
            break
        for apred in _anchor_predicates(anc):
            for pred in [None] + own[:4]:
                xp = '//' + name + '[' + apred + ']//' + tag + ('[' + pred + ']' if pred else '')
                if unique(xp):
                    return xp
    return None


# --- finding the element ------------------------------------------------------------------------

def statically_hidden(el):
    """True when the captured HTML itself hides ``el``: the hidden attribute, an inline
    display:none or visibility:hidden on it or an ancestor, an input of type hidden, or a
    <template>."""
    if el.tag == 'input' and (el.get('type') or '').strip().lower() == 'hidden':
        return True
    node = el
    while node is not None:
        if node.tag == 'template' or node.get('hidden') is not None:
            return True
        style = (node.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or 'visibility:hidden' in style:
            return True
        node = node.getparent()
    return False


@dataclass
class Match:
    """The tier's answer. xpath is set only when it is confident; reason says why not
    otherwise. score and runner_up are the best and next best scores (0 when none)."""
    xpath: str | None
    score: float
    runner_up: float
    reason: str
    element: Any = None


def _fmt(value):
    return format(value, '.2f')


def find(fp, html, role, *, threshold=THRESHOLD, margin=MARGIN,
         visible: Callable[[str], Any] | None = None):
    """Look for the element ``fp`` describes in ``html``, among the elements whose role
    (validation._role_of) is ``role``. Returns a Match.

    visible(xpath) may check an element on the live page: it returns False when that
    element is not visible there, so a hidden copy of the element is not counted as a
    rival. Without it, only what the HTML itself hides is left out. A checkbox is never
    left out for being hidden (a styled checkbox hides its native input)."""
    if not valid(fp):
        return Match(None, 0.0, 0.0, 'the stored fingerprint is not one this version reads')
    try:
        root = lxml.html.fromstring(html)
    except Exception:
        return Match(None, 0.0, 0.0, 'the captured page could not be parsed')
    labels = label_index(root)
    scored = []
    for el in root.iter():
        if not isinstance(el.tag, str) or _role_of(el) != role:
            continue
        if role != 'checkbox' and statically_hidden(el):
            continue
        new = of_element(el, labels)
        if new is None:
            continue
        result = score(fp, new)
        scored.append((result.value, result.strong, el))
    if not scored:
        return Match(None, 0.0, 0.0, "no element on the page has the role '" + str(role) + "'")
    scored.sort(key=lambda item: item[0], reverse=True)
    viable, below = [], None
    for value, strong, el in scored:
        if viable and value < viable[0][0] - margin:
            below = value     # no rival can come closer than this
            break
        if not viable and value < threshold:
            below = value
            break
        xpath = xpath_for(el)
        if visible is not None and role != 'checkbox' and xpath is not None:
            try:
                if visible(xpath) is False:
                    continue  # hidden on the live page: neither the answer nor a rival
            except Exception:
                pass
        viable.append((value, strong, el, xpath))
        if len(viable) == 2:
            break
    if not viable:
        if below is None:
            return Match(None, 0.0, 0.0, 'no visible element scored ' + _fmt(threshold)
                         + ' or more')
        return Match(None, below, 0.0, 'the best score was ' + _fmt(below) + ', below the '
                     + _fmt(threshold) + ' needed')
    best, strong, el, xpath = viable[0]
    runner = viable[1][0] if len(viable) > 1 else (below or 0.0)
    if best - runner < margin:
        return Match(None, best, runner,
                     'two elements score alike (' + _fmt(best) + ' and ' + _fmt(runner)
                     + '), so the fingerprint does not tell them apart', el)
    if not strong:
        return Match(None, best, runner,
                     'the best match (score ' + _fmt(best) + ') shares no id, test id, name, '
                     'link, autocomplete value, label or text with the element that was '
                     'recorded', el)
    if xpath is None:
        return Match(None, best, runner,
                     'no selector that is unique on the page could be built for the best '
                     'match without using its position', el)
    return Match(xpath, best, runner, '', el)
