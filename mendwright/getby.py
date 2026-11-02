"""Playwright's get_by_* calls as keys, for mendwright.open(page, heal_get_by=True).

With heal_get_by on, heal.page.get_by_role("button", name="Sign in") and the six other
get_by_* calls on heal.page are looked up the way heal.page.locator(selector) is, under a
key written as the call itself:

    get_by_role("button", name="Sign in")

The key is the call in one canonical spelling (canonical): strings in double quotes with
JSON escapes, which are also Python string literals with the same value; a regular
expression as re.compile("...", re.IGNORECASE); the options get_by_role takes in the order
name, exact, level; exact=True only where it changes the match. parse() reads any spelling
of such a call back, without running anything in it: it walks the syntax tree and takes
only literals and re.compile() with flag names, which it calls itself. A string that parse()
reads as a call is reserved for heal.page.get_by_*: mendwright.selectors classifies it as
the kind 'get_by', which heal.locator() and the others refuse as a key or a selector.

Some calls are never keys (call_of): get_by_role() with a state filter (checked=,
disabled=, expanded=, pressed=, selected=) or include_hidden=True, an empty role or text,
arguments that are not text, a regular expression or a whole number, a regular expression
with flags other than re.IGNORECASE, re.MULTILINE and re.DOTALL, an argument mendwright
does not read, or a key longer than MAX_KEY characters. Those go to Playwright as written.

With heal_get_by=True a call key is healed only from its fingerprint, never by a model (see
HealCoordinator._heal_get_by). With heal_get_by='model' (MODEL) the model provider is asked
too, when the fingerprint does not heal the call or the call never matched: the call's own
role and words are the intent it gets (intent_of), and a call that never matched is looked
for only when it is a get_by_role() call whose role names a kind of element the gate knows
(gate_role). A heal of a get_by_role() call must have the call's ARIA role, by Playwright's
own matcher, on the live page (gate.live_reason), whoever proposed it. A lookup that misses
raises GetByNotFound, which is both AttentionRequired and Playwright's TimeoutError
(not_found_error).

A heal of a call key is saved with a twin (twin_calls, checked live by client.get_by_twin):
a get_by_role(), get_by_label(), get_by_test_id() or similar call that matched the healed
element, and nothing else, on the live page when the heal was made. mendwright fix shows it
next to each place that makes the call, and never writes it.
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import logging
import re
from dataclasses import dataclass

from .exceptions import AttentionRequired

logger = logging.getLogger('mendwright')

# The get_by_* methods mendwright heals, in the order the docs list them.
METHODS = ('get_by_role', 'get_by_text', 'get_by_label', 'get_by_placeholder',
           'get_by_alt_text', 'get_by_title', 'get_by_test_id')
# The ones that take a text and exact=.
_TEXT_METHODS = frozenset(METHODS[1:6])

# The longest key mendwright saves.
MAX_KEY = 1000

# get_by_role()'s state filters, in the order of Playwright's signature.
_STATES = ('checked', 'disabled', 'expanded', 'pressed', 'selected')

# The flags a regular expression in a key may have, in the order a key writes them, and
# the names parse() takes for each.
_FLAGS = (('IGNORECASE', re.IGNORECASE), ('MULTILINE', re.MULTILINE), ('DOTALL', re.DOTALL))
_FLAG_NAMES = {'IGNORECASE': re.IGNORECASE, 'I': re.IGNORECASE, 'MULTILINE': re.MULTILINE,
               'M': re.MULTILINE, 'DOTALL': re.DOTALL, 'S': re.DOTALL}
_KEPT_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL | re.UNICODE


# Playwright's signatures (1.53), which a call is bound to. A later Playwright can add
# arguments: call_of() says such a call is not a key, and it goes to Playwright as written.

def _get_by_role(role, *, checked=None, disabled=None, expanded=None, include_hidden=None,
                 level=None, name=None, pressed=None, selected=None, exact=None):
    pass


def _get_by_text(text, *, exact=None):
    pass


def _get_by_test_id(test_id):
    pass


_SIGNATURES = {method: inspect.signature(_get_by_text) for method in _TEXT_METHODS}
_SIGNATURES['get_by_role'] = inspect.signature(_get_by_role)
_SIGNATURES['get_by_test_id'] = inspect.signature(_get_by_test_id)

# What each method's first argument is, in the sentences of call_of().
_FIRST = {'get_by_role': 'role', 'get_by_test_id': 'test id'}

STATE_REASON = '{}= picks the element by its state, which a saved selector cannot keep'
HIDDEN_REASON = ('include_hidden=True looks for hidden elements, and mendwright heals only '
                 'onto a visible one')
EMPTY_REASON = 'its {} is empty, so it does not say which element is meant'
TYPE_REASON = 'its arguments are not text, a regular expression or a whole number'
SURROGATE_REASON = 'its text has characters mendwright cannot save'
FLAGS_REASON = ('its regular expression has flags mendwright cannot save: only re.IGNORECASE, '
                're.MULTILINE and re.DOTALL')
UNKNOWN_REASON = 'it passes {}=, which this version of mendwright does not read'
BIND_REASON = 'it passes arguments this version of mendwright does not read'
LONG_REASON = ('its arguments are longer than mendwright saves in a key ('
               + format(MAX_KEY, ',') + ' characters)')

# Why a call goes to Playwright as written (see Healer._get_by_locator), in its DEBUG line.
REJECTED = 'was rejected by the browser'
HIDDEN = 'matches 1 hidden element'
STARTED_TO_MATCH = 'started to match something other than one visible element'

# Why a call key is not followed into an iframe (see Healer._get_by_frames).
NOT_FOLLOWED = 'mendwright does not follow a get_by_* call into an iframe'

# The live gate's reason when a driver cannot say whether the element has the ARIA role of
# a get_by_role() call (see gate.live_reason).
ROLE_UNREADABLE = "could not check the element's ARIA role on the live page"

# heal_get_by='model': a model may heal a call too (see HealCoordinator.try_heal).
MODEL = 'model'

# The sentence a decline of a call key ends with when no model may heal it (see
# HealCoordinator._heal_get_by).
NO_MODEL = ("With heal_get_by=True, mendwright asks no model to heal a get_by_* call: the call "
            'names its element by role and words, and those are what changed. Change the call '
            "by hand, or pass heal_get_by='model' to mendwright.open() to let the model provider "
            'look for the element.')


@dataclass(frozen=True)
class GetByCall:
    """One get_by_* call, as a key stands for it. value is the role for get_by_role(), else
    the text or test id; name and level are get_by_role()'s; exact is True only where it
    changes the match. A str or a compiled regular expression where text goes."""
    method: str
    value: object
    name: object = None
    exact: bool = False
    level: int | None = None


def _is_pattern(value):
    return isinstance(value, re.Pattern) and isinstance(value.pattern, str)


def _is_text(value):
    return isinstance(value, str) or _is_pattern(value)


def _empty(value):
    if isinstance(value, str):
        return not value.strip()
    return value.pattern == ''


def _savable(value):
    """False for text with characters UTF-8 cannot hold (a lone surrogate)."""
    text = value.pattern if _is_pattern(value) else value
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def call_of(method, args, kwargs):
    """(GetByCall, '') for heal.page.<method>(*args, **kwargs), or (None, why) for a call
    that is never a key (see the module docstring). method is one of METHODS."""
    signature = _SIGNATURES[method]
    for name in kwargs:
        if name not in signature.parameters:
            return None, UNKNOWN_REASON.format(name)
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:
        return None, BIND_REASON
    given = bound.arguments
    if method == 'get_by_role':
        for state in _STATES:
            if given.get(state) is not None:
                return None, STATE_REASON.format(state)
        hidden = given.get('include_hidden')
        if hidden is True:
            return None, HIDDEN_REASON
        value, name = given['role'], given.get('name')
        level, exact = given.get('level'), given.get('exact')
        if (not isinstance(value, str) or (name is not None and not _is_text(name))
                or not (hidden is None or hidden is False)
                or (level is not None and (not isinstance(level, int) or isinstance(level, bool)
                                           or level < 0))):
            return None, TYPE_REASON
        texts = (('role', value),) + ((('name', name),) if name is not None else ())
    else:
        first = 'test_id' if method == 'get_by_test_id' else 'text'
        value, name, level, exact = given[first], None, None, given.get('exact')
        if not _is_text(value):
            return None, TYPE_REASON
        texts = ((_FIRST.get(method, 'text'), value),)
    if exact is not None and not isinstance(exact, bool):
        return None, TYPE_REASON
    for what, text in texts:
        if _empty(text):
            return None, EMPTY_REASON.format(what)
    for _what, text in texts:
        if not _savable(text):
            return None, SURROGATE_REASON
    for _what, text in texts:
        if _is_pattern(text) and text.flags & ~_KEPT_FLAGS:
            return None, FLAGS_REASON
    exact = bool(exact) and ((method in _TEXT_METHODS and isinstance(value, str))
                             or (method == 'get_by_role' and isinstance(name, str)))
    call = GetByCall(method, value, name, exact, level)
    if len(canonical(call)) > MAX_KEY:
        return None, LONG_REASON
    return call, ''


def _literal(value):
    """A string or a regular expression as a key writes it."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    flags = ' | '.join('re.' + name for name, flag in _FLAGS if value.flags & flag)
    return 're.compile(' + json.dumps(value.pattern, ensure_ascii=False) + (
        ', ' + flags if flags else '') + ')'


def canonical(call):
    """The key of a GetByCall: the call in its one canonical spelling, such as
    get_by_role("button", name="Sign in")."""
    parts = [_literal(call.value)]
    if call.method == 'get_by_role':
        if call.name is not None:
            parts.append('name=' + _literal(call.name))
        if call.exact:
            parts.append('exact=True')
        if call.level is not None:
            parts.append('level=' + str(int(call.level)))
    elif call.exact:
        parts.append('exact=True')
    return call.method + '(' + ', '.join(parts) + ')'


# --- reading a key -------------------------------------------------------------------------

_BAD = object()


def _flags_of(node):
    """The re flags an AST node names (re.I, re.IGNORECASE | re.DOTALL, ...), or _BAD."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _flags_of(node.left), _flags_of(node.right)
        return _BAD if _BAD in (left, right) else left | right
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 're' and node.attr in _FLAG_NAMES):
        return _FLAG_NAMES[node.attr]
    return _BAD


def _value_of(node):
    """The value of an argument node: a str, bool, int or None literal, or a re.compile()
    of a str literal with flag names, compiled here. _BAD for anything else, which is
    never run."""
    if isinstance(node, ast.Constant):
        value = node.value
        if value is None or isinstance(value, (str, bool, int)):
            return value
        return _BAD
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 're'
            and node.func.attr == 'compile' and not node.keywords
            and 1 <= len(node.args) <= 2 and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)):
        flags = _flags_of(node.args[1]) if len(node.args) == 2 else 0
        if flags is _BAD:
            return _BAD
        try:
            return re.compile(node.args[0].value, flags)
        except Exception:
            return _BAD
    return _BAD


def from_ast_call(method, node):
    """The GetByCall of an ast.Call node whose arguments are literals (see _value_of), for
    a call of method, else None. parse() reads keys with it, and mendwright fix finds the
    calls in source files with it."""
    if method not in METHODS or not isinstance(node, ast.Call):
        return None
    args, kwargs = [], {}
    for arg in node.args:
        value = _value_of(arg)          # an ast.Starred (*args) is _BAD too
        if value is _BAD:
            return None
        args.append(value)
    for keyword in node.keywords:
        if keyword.arg is None or keyword.arg in kwargs:   # **kwargs, or a repeated name
            return None
        value = _value_of(keyword.value)
        if value is _BAD:
            return None
        kwargs[keyword.arg] = value
    return call_of(method, tuple(args), kwargs)[0]


def parse(text):
    """The GetByCall a key (or any spelling of a get_by_* call) stands for, or None.

    Nothing in the text is run: it is read with ast.parse, and only literals and
    re.compile() with flag names are taken. None for anything that is not a str, is longer
    than MAX_KEY characters, does not start with get_by_, or is not such a call."""
    if not isinstance(text, str) or len(text) > MAX_KEY:
        return None
    text = text.strip()
    if not text.startswith('get_by_'):
        return None
    return _parse(text)


@functools.lru_cache(maxsize=1024)
def _parse(text):
    try:
        tree = ast.parse(text, mode='eval')
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    body = tree.body
    if not (isinstance(body, ast.Call) and isinstance(body.func, ast.Name)):
        return None
    return from_ast_call(body.func.id, body)


def is_key(key):
    """True when key is a get_by_* call (see parse): a key mendwright keeps for
    heal.page.get_by_*, which heal.locator() and the others refuse."""
    return parse(key) is not None


def canonical_key(key):
    """The key mendwright saves a get_by_* call under, for a key written as that call in
    any spelling parse() reads, such as get_by_role('button', name='Pay'); any other key
    as it is. destructive_keys=, expect_values=, seed files and heal.present() read their
    keys through it, so a mark written that way still applies to the call."""
    call = parse(key)
    return key if call is None else canonical(call)


# A get_by_* call written on an object, such as page.get_by_role(...): the call from get_by_.
_ON_OBJECT = re.compile(r'(?:[A-Za-z_]\w*\.)+(get_by_.*)', re.DOTALL)


def on_object(key):
    """The GetByCall of a key written as a get_by_* call on an object, such as
    page.get_by_role('button', name='Pay') from a log line, or None. Such a key is a key of
    its own, not the call's (canonical_key leaves it as it is), so destructive_keys=,
    expect_values= and seed files log a WARNING for one (warn_on_object)."""
    if not isinstance(key, str) or len(key) > MAX_KEY:
        return None
    match = _ON_OBJECT.fullmatch(key.strip())
    return None if match is None else parse(match.group(1))


def on_object_message(where, key, call):
    """The WARNING for a key of ``where`` written as call on an object (see on_object)."""
    return ('mendwright: ' + where + ' has the key ' + repr(key) + ', which no heal.page.'
            + call.method + "() call uses. That call's key is " + canonical(call)
            + ', as mendwright.get_by_key() writes it.')


def warn_on_object(where, key):
    """Log on_object_message() when key, of ``where`` (destructive_keys=, expect_values=, a
    seed file), is a get_by_* call written on an object."""
    call = on_object(key)
    if call is not None:
        logger.warning(on_object_message(where, key, call))


def build(ctx, call):
    """The Locator for call on ctx, a Page, Frame or FrameLocator: its public get_by_*
    method with the call's arguments."""
    method = getattr(ctx, call.method)
    if call.method == 'get_by_role':
        kwargs = {}
        if call.name is not None:
            kwargs['name'] = call.name
        if call.exact:
            kwargs['exact'] = True
        if call.level is not None:
            kwargs['level'] = call.level
        return method(call.value, **kwargs)
    if call.exact:
        return method(call.value, exact=True)
    return method(call.value)


def get_by_key(method: str, /, *args, **kwargs) -> str:
    """The key a heal.page.<method>(*args, **kwargs) call is saved under, for
    destructive_keys=, expect_values=, seed files, heal.present() and reading the store:

        get_by_key("get_by_role", "button", name="Pay")   # 'get_by_role("button", name="Pay")'

    A key matches one call: name=re.compile("pay", re.I) or exact=True makes another key.
    Raises ValueError for a method mendwright does not heal and for a call that is never a
    key, and TypeError for arguments the method does not take."""
    if method not in METHODS:
        raise ValueError('mendwright.get_by_key(): ' + repr(method) + ' is not a get_by_* method '
                         'mendwright heals. Use one of ' + ', '.join(METHODS[:-1]) + ' or '
                         + METHODS[-1] + '.')
    try:
        _SIGNATURES[method].bind(*args, **kwargs)
    except TypeError as exc:
        raise TypeError('mendwright.get_by_key(): ' + method + '() ' + str(exc) + '.') from None
    call, why = call_of(method, args, kwargs)
    if call is None:
        from .proxies import _call_text
        raise ValueError('mendwright.get_by_key(): page.' + _call_text(method, args, kwargs)
                         + ' is never healed, so it has no key: ' + why + '.')
    return canonical(call)


# --- messages ------------------------------------------------------------------------------

def not_healed_message(step, why):
    """The INFO line for a heal.page.get_by_*() call that is never a key (see call_of)."""
    return step + ' is not healed: ' + why + '. It goes to Playwright as written.'


def through_message(key, why):
    """The DEBUG line for a call that goes to Playwright as written."""
    return key + ' ' + why + '; Playwright runs it as written.'


def two_spellings_message(where, first, second):
    """The error for two keys of ``where`` (expect_values=, a seed file) that name one call."""
    return ('mendwright: ' + where + ' has two keys for the call ' + canonical_key(first) + ': '
            + repr(first) + ' and ' + repr(second) + '. Keep one of them.')


def reserved_key_message(key, entry):
    """The ValueError for a key of heal.locator() and the others written as a call."""
    return ('mendwright: ' + repr(key) + ' is how mendwright names a heal.page.'
            + parse(key).method + '() call, so ' + entry + ' cannot take it as a key. Pick '
            'another key.')


def role_mismatch(call):
    """The live gate's reason when Playwright does not give the element call's ARIA role."""
    text = 'live element role mismatch: Playwright does not give it the ARIA role ' + repr(call.value)
    if call.level is not None:
        text += ' at level ' + str(call.level)
    return text


def tier_off_message(key):
    return ('the fingerprint tier is off (fingerprint=False or MENDWRIGHT_FINGERPRINT=0), and '
            "mendwright heals a get_by_* call only from its fingerprint, so '" + key + "' was "
            'not healed. Turn the fingerprint tier on to heal get_by_* calls.')


def no_fingerprint_message(key):
    return ('mendwright heals a get_by_* call only from the fingerprint it records each time '
            "the call matches one element, and '" + key + "' has none in this store: it has "
            'not matched one element here while the fingerprint tier was on. A call that never '
            'matched is more often a mistake in the call than a redesign. Check its arguments '
            'against the page.')


def role_unknown_message(key):
    return ("mendwright does not know what kind of element '" + key + "' is: it could not "
            'read the element when the call last matched, so it cannot check a repaired one. '
            'Run the step again while the call matches.')


# --- heal_get_by='model' ---------------------------------------------------------------------

def _quoted(value):
    """A name or text as the intent quotes it: a str in double quotes, a regular expression
    as /pattern/ with i, m and s for its flags."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    flags = ''.join(letter for letter, flag in (('i', re.IGNORECASE), ('m', re.MULTILINE),
                                                 ('s', re.DOTALL)) if value.flags & flag)
    return '/' + value.pattern + '/' + flags


def _named(value, what):
    """' named "Sign in"', or ' whose name matches the regular expression /.../i'."""
    if value is None:
        return ''
    if isinstance(value, str):
        return ' ' + what + ' ' + _quoted(value)
    return ' whose ' + ('name' if what == 'named' else what.split()[-1]) + (
        ' matches the regular expression ' + _quoted(value))


def intent_of(call):
    """What the call says about its element, in words, for the model that heals it with
    heal_get_by='model' (the bundle's intent): its role and name, or its text, label,
    placeholder, alt text, title or test id, such as 'the button named "Sign in"'. The model
    also gets the call itself, as the key and the selector that broke."""
    method = call.method
    if method == 'get_by_role':
        text = 'the ' + str(call.value)
        if call.level is not None:
            text += ' at level ' + str(call.level)
        text += _named(call.name, 'named')
    elif method == 'get_by_test_id':
        text = 'the element with the test id ' + _quoted(call.value)
    else:
        what = {'get_by_text': ('the element with the text', 'text'),
                'get_by_label': ('the form field labelled', 'label'),
                'get_by_placeholder': ('the field with the placeholder', 'placeholder'),
                'get_by_alt_text': ('the image with the alt text', 'alt text'),
                'get_by_title': ('the element with the title', 'title')}[method]
        if isinstance(call.value, str):
            text = what[0] + ' ' + _quoted(call.value)
        else:
            text = ('the element whose ' + what[1] + ' matches the regular expression '
                    + _quoted(call.value))
    return text


def gate_role(call):
    """The gate role (see mendwright.roles) a heal of call must have when the store knows
    none for its key, because the call never matched: for a get_by_role() call, the first
    gate role its ARIA role can mean, as role= takes it (a heading of level= 2 is an h2),
    else None. None too for a role the gate has no word for, such as tab or menuitem, and
    for the other get_by_* methods, which say nothing of the element's kind."""
    if call is None or call.method != 'get_by_role' or not isinstance(call.value, str):
        return None
    from .roles import role_choices
    name = call.value.strip().lower()
    if name == 'heading' and call.level in (1, 2, 3, 4, 5, 6):
        return 'h' + str(call.level)
    try:
        choices = role_choices(name)
    except ValueError:
        return None
    return choices[0] if choices else None


def role_unknown_model_message(key, call):
    """Why a call key that never matched is not looked for by a model: nothing says what
    kind of element it is, so what the model picks could not be checked."""
    if call is not None and call.method == 'get_by_role':
        why = ("mendwright's checks have no word for the ARIA role " + repr(call.value)
               + ' (they know button, checkbox, radio, link, combobox, textbox and the roles of '
               'HTML tags)')
    else:
        why = 'only a get_by_role() call says what kind of element it finds'
    return ("mendwright does not know what kind of element '" + key + "' is: the call has not "
            'matched one element in this store, and ' + why + ', so what a model picks could '
            'not be checked and no model was asked. Run the step once while the call matches, '
            'so mendwright reads the element.')


# --- twins: a call that finds the healed element ---------------------------------------------

# The longest name, label or text a twin is made with: a longer one is no call to write.
MAX_TWIN_TEXT = 100
# The methods a twin is made with, after the call's own method.
_TWIN_ORDER = ('get_by_role', 'get_by_label', 'get_by_test_id')
_FIELD_TAGS = ('input', 'select', 'textarea')
_NOT_FIELDS = ('hidden', 'submit', 'button', 'reset', 'image')
_SPACE = re.compile(r'[ \t\n\r\f]+')


def _flat(text):
    return _SPACE.sub(' ', text or '').strip()


def _usable_text(text):
    return isinstance(text, str) and 0 < len(text) <= MAX_TWIN_TEXT


def _label_text(el):
    """The text of the <label> elements of a form field (for= its id, or around it),
    without the text of the field itself and of other controls in it, or ''."""
    if not isinstance(el.tag, str) or el.tag.lower() not in _FIELD_TAGS:
        return ''
    if el.tag.lower() == 'input' and (el.get('type') or '').lower() in _NOT_FIELDS:
        return ''
    labels = []
    ident = el.get('id')
    root = el.getroottree().getroot()
    if ident:
        try:
            labels.extend(root.xpath('//label[@for=$i]', i=ident))
        except Exception:
            pass
    labels.extend(a for a in el.iterancestors() if isinstance(a.tag, str)
                  and a.tag.lower() == 'label')
    return _flat(' '.join(_text_without(label, _SKIPPED) for label in labels))


# What a label's text leaves out: the text of the controls in it.
_SKIPPED = frozenset(('select', 'textarea', 'option', 'script', 'style', 'button'))


def _text_without(node, skip):
    """node's text, without the text inside its descendants whose tag is in skip."""
    out = [node.text or '']
    for child in node:
        if not (isinstance(child.tag, str) and child.tag.lower() in skip):
            out.append(_text_without(child, skip))
        out.append(child.tail or '')
    return ''.join(out)


def _call(method, *args, **kwargs):
    """call_of() for a twin, or None when it is never a key."""
    return call_of(method, args, kwargs)[0]


def twin_calls(el, call=None, aria=None):
    """The get_by_* calls that may find el, the element a heal of a key found, best first,
    to be checked on the live page (client.get_by_twin): each must match el and nothing
    else there before it is saved. call is the GetByCall of the key when the key is a call,
    whose method goes first; aria is {'role', 'name', 'level'} for el as Playwright reads it
    (driver.aria_name), or None.

    - get_by_role(role, name=..., exact=True), from aria, with level= when call has one;
    - get_by_label(label, exact=True), from the <label> of a form field;
    - get_by_test_id(value), from data-testid, Playwright's default test id attribute;
    - and, when call is one of them, get_by_text(), get_by_placeholder(),
      get_by_alt_text() or get_by_title() with el's text or attribute, exact=True.

    A name, label or text longer than MAX_TWIN_TEXT characters, or empty, gives none."""
    found = {}
    if isinstance(aria, dict) and isinstance(aria.get('role'), str) and aria['role']:
        name = aria.get('name')
        if _usable_text(name):
            level = None
            if (call is not None and call.method == 'get_by_role' and call.level is not None
                    and isinstance(aria.get('level'), int)):
                level = aria['level']
            kwargs = {'name': name, 'exact': True}
            if level is not None:
                kwargs['level'] = level
            found['get_by_role'] = _call('get_by_role', aria['role'], **kwargs)
    label = _label_text(el) or _flat(el.get('aria-label'))
    if _usable_text(label) and isinstance(el.tag, str) and el.tag.lower() in _FIELD_TAGS:
        found['get_by_label'] = _call('get_by_label', label, exact=True)
    test_id = el.get('data-testid')
    if _usable_text(test_id) and test_id.strip():
        found['get_by_test_id'] = _call('get_by_test_id', test_id)
    own = call.method if call is not None else None
    if own == 'get_by_text':
        text = _flat(el.text_content() if isinstance(el.tag, str) else '')
        if _usable_text(text):
            found[own] = _call(own, text, exact=True)
    attr = {'get_by_placeholder': 'placeholder', 'get_by_alt_text': 'alt',
            'get_by_title': 'title'}.get(own)
    if attr and _usable_text(_flat(el.get(attr))):
        found[own] = _call(own, _flat(el.get(attr)), exact=True)
    order = ((own,) if own else ()) + _TWIN_ORDER
    out = []
    for method in dict.fromkeys(order):
        twin = found.get(method)
        if twin is not None and (call is None or canonical(twin) != canonical(call)):
            out.append(twin)
    return out


def twin_text(value):
    """value, a twin read from the store or the journal, when it is a call in mendwright's
    canonical spelling (parse, canonical), else None: something else wrote it, and it is
    never shown."""
    if not isinstance(value, str) or not value:
        return None
    call = parse(value)
    if call is None or canonical(call) != value:
        return None
    return value


_ARIA_LINE = re.compile(r'(?P<role>[a-z]+)(?: (?P<name>"(?:[^"\\]|\\.)*"))?(?P<rest>.*)', re.DOTALL)
_ARIA_LEVEL = re.compile(r'\[level=(\d+)\]')


def parse_aria(snapshot):
    """{'role', 'name', 'level'} for the first line of Playwright's aria_snapshot() of one
    element, such as '- button "Log in"' or '- heading "Cart" [level=2]', or None. name is
    None when the line has none, or gives it as a regular expression."""
    if not isinstance(snapshot, str) or not snapshot.startswith('- '):
        return None
    line = snapshot.splitlines()[0][2:]
    if line.startswith("'"):            # a key YAML had to quote: 'button "a: b"':
        end, i = None, 1
        while i < len(line):
            if line[i] == "'":
                if line[i + 1:i + 2] == "'":
                    i += 2
                    continue
                end = i
                break
            i += 1
        if end is None:
            return None
        line = line[1:end].replace("''", "'")
    match = _ARIA_LINE.match(line)
    if match is None:
        return None
    name = None
    if match.group('name'):
        try:
            name = json.loads(match.group('name'))
        except ValueError:
            return None
    level = _ARIA_LEVEL.search(match.group('rest') or '')
    return {'role': match.group('role'), 'name': name,
            'level': int(level.group(1)) if level else None}


# --- the error a miss raises ---------------------------------------------------------------

@functools.lru_cache(maxsize=None)
def _not_found_class(timeout_error):
    """An error class that is both AttentionRequired and Playwright's TimeoutError, so code
    that catches either still does (see proxies._not_found_class for Selenium's)."""

    class GetByNotFound(AttentionRequired, timeout_error):
        def __init__(self, message):
            AttentionRequired.__init__(self, message)
            self._message = message
            self._name = 'TimeoutError'
            self._stack = None

        def __str__(self):
            return self._message

    GetByNotFound.__module__ = __name__
    return GetByNotFound


def _timeout_error():
    """Playwright's TimeoutError (the same class in the sync and async API), or None."""
    try:
        from playwright._impl._errors import TimeoutError as timeout_error
    except Exception:
        return None
    return timeout_error


def not_found_error(exc):
    """exc (an AttentionRequired) as a GetByNotFound, which is also Playwright's
    TimeoutError, or exc itself when Playwright cannot be imported."""
    timeout_error = _timeout_error()
    if timeout_error is None:
        return exc
    return _not_found_class(timeout_error)(str(exc))


def __getattr__(name):
    # GetByNotFound is made on first use, so importing mendwright imports no Playwright.
    if name == 'GetByNotFound':
        timeout_error = _timeout_error()
        if timeout_error is not None:
            return _not_found_class(timeout_error)
    raise AttributeError('module ' + repr(__name__) + ' has no attribute ' + repr(name))
