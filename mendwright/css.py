"""A CSS selector for a healed element, for code that uses CSS.

Heals are XPath, because the gate checks them with lxml. When a key's selector in your
code is CSS, mendwright also looks for a CSS selector that matches the healed element and
nothing else, and saves it with the heal (the candidate's ``css``). ``mendwright fix``
writes that one into your code, so a CSS selector stays CSS, also where only CSS runs,
such as Selenium's By.CSS_SELECTOR or querySelector().

The CSS is built from forms that each have an exact XPath twin: a tag name, ``#id``,
``.class``, ``[attr="value"]`` and the descendant combinator. A candidate counts only if
its XPath twin matches the healed element and nothing else on the captured page, and then,
on the live page, the CSS itself matches one element whose outer HTML is the healed
element's. CSS can't match text, so an element that only its text tells apart gets no CSS,
and its heal is written back as XPath.
"""

import itertools
import re

from .candidates import TEST_ATTRS, _class_ok, _usable, generated, xpath_literal
from .fingerprint import stable_classes

_IDENT = re.compile(r'^-?[A-Za-z_][A-Za-z0-9_-]*$')
# Attributes worth naming, after the test ids and the id, best first.
_ATTRS = ('name', 'aria-label', 'placeholder', 'title', 'alt', 'for', 'formcontrolname',
          'autocomplete', 'href', 'type', 'role')
_ANCHOR_ATTRS = ('id',) + TEST_ATTRS + ('name', 'aria-label')
_FOREIGN = ('svg', 'math')
MAX_ANCESTORS = 6


def css_string(value):
    """``value`` as a double-quoted CSS string."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ch in '\n\r\f':
            out.append('\\' + format(ord(ch), 'x') + ' ')
        else:
            out.append(ch)
    return '"' + ''.join(out) + '"'


def _attr(name, value):
    """(css, xpath predicate) for an exact attribute value."""
    if name == 'id' and _IDENT.fullmatch(value):
        return '#' + value, '@id=' + xpath_literal(value)
    return '[' + name + '=' + css_string(value) + ']', '@' + name + '=' + xpath_literal(value)


def _class(token):
    return ('.' + token,
            "contains(concat(' ', normalize-space(@class), ' '), " + xpath_literal(' ' + token + ' ') + ')')


def _parts(el):
    """The (css, xpath predicate) pairs that describe el itself, best first."""
    parts = []
    for name in TEST_ATTRS:
        value = el.get(name)
        if _usable(value) and not generated(value):
            parts.append(_attr(name, value))
            break
    ident = el.get('id')
    if _usable(ident) and not generated(ident):
        parts.append(_attr('id', ident))
    for name in _ATTRS:
        value = el.get(name)
        if not _usable(value) or (name == 'name' and generated(value)):
            continue
        if name == 'href' and (value.startswith('javascript') or '?' in value or '#' in value):
            continue   # a query or fragment is often a session or tracking value
        parts.append(_attr(name, value))
    for token in stable_classes(el.get('class'))[:3]:
        if _class_ok(token) and _IDENT.fullmatch(token):
            parts.append(_class(token))
    return parts


def _anchor_parts(anc):
    parts = []
    for name in _ANCHOR_ATTRS:
        value = anc.get(name)
        if _usable(value) and not (name in ('id', 'name') and generated(value)):
            parts.append(_attr(name, value))
    for token in stable_classes(anc.get('class'))[:2]:
        if _class_ok(token) and _IDENT.fullmatch(token):
            parts.append(_class(token))
    return parts


def _compound(tag, parts):
    return tag + ''.join(css for css, _ in parts), tag + ''.join('[' + x + ']' for _, x in parts)


def _matches_only(doc, xpath, el):
    try:
        hits = [h for h in doc.xpath(xpath) if isinstance(getattr(h, 'tag', None), str)]
    except Exception:
        return False
    return len(hits) == 1 and hits[0] is el


def css_selectors(el, doc=None, limit=3):
    """Up to ``limit`` CSS selectors that match ``el`` and nothing else in ``doc`` (an lxml
    HTML document), most robust first: the element's own attributes, two of them together,
    then a container with an id, test id, name, aria-label or class name around it. [] when none of
    these forms tells el apart, or when el is inside an <svg> or <math>."""
    if doc is None:
        doc = el.getroottree().getroot()
    tag = el.tag if isinstance(el.tag, str) else ''
    if not _IDENT.fullmatch(tag) or any(a.tag in _FOREIGN for a in el.iterancestors()):
        return []
    tag = tag.lower()
    out = []

    def add(css, xpath):
        if css not in out and _matches_only(doc, '//' + xpath, el):
            out.append(css)
        return len(out) >= limit

    parts = _parts(el)
    # An id or a test id is written alone (#save, [data-testid="save"]), as people do;
    # everything else with its tag.
    bare = [(css, '*[' + x + ']') for css, x in parts[:2]
            if css.startswith(('#',) + tuple('[' + a + '=' for a in ('id',) + TEST_ATTRS))]
    targets = bare + [_compound(tag, [p]) for p in parts]
    targets += [_compound(tag, list(pair)) for pair in itertools.combinations(parts[:6], 2)]
    for css, xpath in targets:
        if add(css, xpath):
            return out
    if out:
        return out
    inner = [(tag, tag)] + targets
    for depth, anc in enumerate(el.iterancestors()):
        if depth >= MAX_ANCESTORS or not isinstance(anc.tag, str) or not _IDENT.fullmatch(anc.tag):
            break
        for part in _anchor_parts(anc):
            a_css, a_xpath = _compound(anc.tag.lower(), [part])
            for css, xpath in inner:
                if add(a_css + ' ' + css, a_xpath + '//' + xpath):
                    return out
        if out:
            return out
    return out
