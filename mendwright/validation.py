"""Validate a proposed selector against a captured HTML snapshot.

The static half of the checks a heal must pass: a candidate is accepted only if it
matches exactly one element of the expected role (and, for destructive keys, has one of
the expected action texts), and is not a pure absolute-index path. client.apply() runs it
before a heal is saved, followed by the live check in gate.py.
"""

import re
from dataclasses import dataclass

import lxml.html


@dataclass
class Result:
    ok: bool
    reason: str
    count: int


_PLAIN_STEP = re.compile(r'^[A-Za-z][\w:-]*(\[\d+\])?$')


def _is_brittle(xpath):
    """True for pure absolute-index paths like /html[1]/body[1]/div[3]/input[1]."""
    segs = [s for s in re.split(r'/+', xpath.strip()) if s]
    if not segs:
        return False
    return all(_PLAIN_STEP.match(s) for s in segs) and any('[' in s for s in segs)


# role="..." values the gate takes as they are.
GATE_ROLE_ATTRS = ('checkbox', 'button', 'link', 'combobox', 'radio')
# ARIA widget roles on a non-native element (a <div role="switch">), mapped to the gate role
# of the native control they stand for. Native controls keep their own role:
# <button role="switch"> stays a button and <textarea role="textbox"> a textarea.
ARIA_ROLE_ATTRS = {'textbox': 'input', 'searchbox': 'input', 'spinbutton': 'input',
                   'slider': 'input', 'switch': 'checkbox', 'listbox': 'combobox'}


def _role_of(el):
    role = (el.get('role') or '').strip().lower()
    if role in GATE_ROLE_ATTRS:
        return role
    tag = el.tag.lower() if isinstance(el.tag, str) else ''
    typ = (el.get('type') or '').strip().lower()
    if tag == 'input':
        if typ == 'checkbox':
            return 'checkbox'
        if typ == 'radio':
            return 'radio'
        if typ in ('submit', 'button'):
            return 'button'
        return 'input'
    if tag == 'button':
        return 'button'
    if tag == 'a':
        return 'link'
    if tag == 'select':
        return 'combobox'
    if tag != 'textarea' and role in ARIA_ROLE_ATTRS:
        return ARIA_ROLE_ATTRS[role]
    return tag or 'other'


def _norm(text):
    return re.sub(r'\s+', ' ', (text or '')).strip()


def validate(xpath, html, *, expected_role, expected_text=None, destructive=False):
    """Check xpath against the captured html. Returns a Result whose reason says why a
    candidate was refused. destructive=True also requires the element's text to be one of
    expected_text."""
    if _is_brittle(xpath):
        return Result(False, 'brittle absolute-index path', 0)
    try:
        doc = lxml.html.fromstring(html)
        matches = doc.xpath(xpath)
    except Exception as exc:
        return Result(False, 'xpath error: ' + type(exc).__name__, 0)
    matches = [m for m in matches if hasattr(m, 'tag')]
    count = len(matches)
    if count == 0:
        return Result(False, 'no match', 0)
    if count > 1:
        return Result(False, 'not unique (' + str(count) + ')', count)
    el = matches[0]
    role = _role_of(el)
    if role != expected_role:
        return Result(False, 'role mismatch: got ' + role + ' want ' + expected_role, 1)
    if destructive:
        if not expected_text:
            return Result(False, 'destructive key without expected action text', 1)
        text = _norm(el.text_content())
        if text not in {_norm(t) for t in expected_text}:
            return Result(False, 'action text mismatch: ' + text, 1)
    return Result(True, 'ok', 1)
