"""Roles: what kind of element a selector key points at, in the gate's vocabulary.

The gate (validation._role_of) gives every element one role: 'checkbox', 'radio',
'button', 'link', 'combobox' or 'input' for the controls it knows, and the tag name for
anything else ('textarea', 'div', 'li', ...). A heal is accepted only when the proposed
element has the key's expected role, so the expected role must use the same words.

``role_choices`` turns what a user passes as role= into gate roles. It accepts the gate's
own words, ARIA role names that map onto them (textbox, switch, listbox, ...), HTML tag
names and custom element names, and raises ValueError for anything else.

``infer_role`` guesses the role from the selector text alone. mendwright uses it only
before the element has been seen, or for drivers that cannot describe an element.
"""

import re

import lxml.html.defs

from .selectors import CSS, XPATH, classify, strip_prefix
from .validation import ARIA_ROLE_ATTRS, GATE_ROLE_ATTRS, _role_of

# The roles validation._role_of returns for the controls it knows.
GATE_ROLES = ('button', 'checkbox', 'radio', 'link', 'input', 'combobox')
# The role="..." values _role_of takes from the attribute as they are. ARIA widget roles
# in ARIA_ROLE_ATTRS map to a gate role; any other role attribute falls back to the tag.
_ROLE_ATTRS = GATE_ROLE_ATTRS

# ARIA role names (and a few common words) mapped to the gate roles they can mean.
# When there is more than one, the live element decides; the first is used until then.
ALIASES = {
    'textbox': ('input', 'textarea'),
    'searchbox': ('input',),
    'spinbutton': ('input',),
    'slider': ('input',),
    'listbox': ('combobox',),
    'select': ('combobox',),
    'dropdown': ('combobox',),
    'switch': ('checkbox', 'button'),
    'a': ('link',),
    'image': ('img',),
    'img': ('img',),
    'listitem': ('li',),
    'list': ('ul', 'ol'),
    'heading': ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'),
    'paragraph': ('p',),
    'navigation': ('nav',),
    'row': ('tr',),
    'cell': ('td',),
    'columnheader': ('th',),
}

# HTML elements newer than lxml's tag list.
_EXTRA_TAGS = frozenset({'main', 'dialog', 'template', 'picture', 'search', 'slot', 'data',
                         'bdi', 'portal'})
_HTML_TAGS = frozenset(lxml.html.defs.tags) | _EXTRA_TAGS
_CUSTOM_ELEMENT = re.compile(r'^[a-z][a-z0-9._]*-[a-z0-9._-]*$')


def _unknown_role_error(role):
    return ValueError(
        'mendwright: unknown role ' + repr(role) + '. Use one of ' + ', '.join(GATE_ROLES)
        + ', an ARIA role such as textbox, switch or listbox, or an HTML tag name such as '
        "textarea or div (role='h1' for a heading). Other ARIA roles, such as tab or "
        "menuitem, are not supported: pass the element's tag name, or leave role out.")


def role_choices(role):
    """The gate roles a role= value can mean, as a tuple (first is the default).

    None gives None. Raises ValueError for a value that is not a gate role, a known
    ARIA alias, an HTML tag or a custom element name (one with a '-')."""
    if role is None:
        return None
    if not isinstance(role, str) or not role.strip():
        raise _unknown_role_error(role)
    name = role.strip().lower()
    if name in GATE_ROLES:
        return (name,)
    if name in ALIASES:
        return ALIASES[name]
    if name in _HTML_TAGS or _CUSTOM_ELEMENT.match(name):
        return (_tag_role(name, None, None),)
    raise _unknown_role_error(role)


def _tag_role(tag, role_attr, type_attr):
    """_role_of for an element known only by tag, role and type attributes."""
    try:
        el = lxml.html.Element(tag)
    except Exception:  # a tag lxml will not build (some custom elements)
        name = (role_attr or '').strip().lower()
        if name in _ROLE_ATTRS:
            return name
        return ARIA_ROLE_ATTRS.get(name, tag)
    if role_attr:
        el.set('role', role_attr)
    if type_attr:
        el.set('type', type_attr)
    return _role_of(el)


# --- guessing from the selector text --------------------------------------------------

_ATTR_EQ = re.compile(r"""@(role|type)\s*=\s*(['"])([^'"]*)\2""")
_CSS_ATTR = re.compile(r"""\[\s*(role|type)\s*=\s*(['"]?)([^'"\]]*)\2\s*\]""")
_NAME = re.compile(r'^[A-Za-z][\w.-]*')
_CSS_NAME = re.compile(r'^[A-Za-z][\w-]*')


def _top_level_split(text, sep):
    """Split text on sep outside quotes, brackets and parentheses."""
    parts, depth, quote, start, i = [], 0, None, 0, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = None
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        elif ch in '"\'':
            quote = ch
        elif ch in '[(':
            depth += 1
        elif ch in '])':
            depth = max(0, depth - 1)
        i += 1
    parts.append(text[start:])
    return parts


def _predicates(step):
    """The top-level [...] groups of one location step, without the brackets."""
    out, depth, quote, start = [], 0, None, None
    for i, ch in enumerate(step):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in '"\'':
            quote = ch
        elif ch == '[':
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0 and start is not None:
                out.append(step[start:i])
    return out


def _from_parts(tag, role_attr, type_attr):
    role_attr = (role_attr or '').strip().lower() or None
    type_attr = (type_attr or '').strip().lower() or None
    if tag in (None, '*', 'node()'):
        if role_attr in _ROLE_ATTRS:
            return role_attr
        if role_attr in ARIA_ROLE_ATTRS:
            return ARIA_ROLE_ATTRS[role_attr]
        if type_attr in ('checkbox', 'radio', 'submit', 'button'):
            return _tag_role('input', None, type_attr)   # only inputs carry these types
        return 'unknown'
    return _tag_role(tag.lower(), role_attr, type_attr)


def _infer_xpath(xpath):
    text = xpath.strip()
    if len(_top_level_split(text, '|')) > 1:
        return 'unknown'
    if text.startswith('('):
        inner = _top_level_split(text[1:], ')')
        if len(inner) >= 2 and all(not p.strip() or p.strip().startswith('[') for p in inner[1:]):
            return _infer_xpath(inner[0])
        return 'unknown'
    steps = [s for s in _top_level_split(text, '/') if s.strip()]
    if not steps:
        return 'unknown'
    step = steps[-1].strip()
    if '::' in step:
        axis, step = step.split('::', 1)
        if axis.strip() not in ('self', 'child', 'descendant', 'descendant-or-self'):
            return 'unknown'
    name = _NAME.match(step)
    tag = '*' if step.startswith('*') else (name.group(0) if name else None)
    if tag is None or tag in ('text', 'comment', 'processing-instruction'):
        return 'unknown'
    attrs = {}
    for predicate in _predicates(step):
        if '[' in predicate or '/' in predicate:
            continue  # a nested path talks about other elements
        for attr, _q, value in _ATTR_EQ.findall(predicate):
            attrs.setdefault(attr, value)
    return _from_parts(tag, attrs.get('role'), attrs.get('type'))


def _infer_css(css):
    compounds = re.split(r'\s*[>+~]\s*|\s+', css.strip())
    last = compounds[-1] if compounds else ''
    if not last or ',' in css:
        return 'unknown'
    name = _CSS_NAME.match(last)
    tag = name.group(0).lower() if name else '*'
    if tag != '*' and tag not in _HTML_TAGS and not _CUSTOM_ELEMENT.match(tag):
        return 'unknown'
    attrs = {}
    for attr, _q, value in _CSS_ATTR.findall(last):
        attrs.setdefault(attr, value)
    return _from_parts(tag, attrs.get('role'), attrs.get('type'))


def infer_role(selector):
    """Guess the gate role from the selector text, or 'unknown'.

    Looks at the last step of an XPath (or the last compound of a CSS selector): its tag
    and any @type='...' or @role='...' test on it. The answer matches what the gate
    would compute for such an element. Anything it cannot read gives 'unknown'."""
    if not isinstance(selector, str) or not selector.strip():
        return 'unknown'
    try:
        kind = classify(selector)
        body = strip_prefix(selector)
        if kind == XPATH:
            return _infer_xpath(body)
        if kind == CSS:
            return _infer_css(body)
    except Exception:
        return 'unknown'
    return 'unknown'
