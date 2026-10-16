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

A call key is healed only from its fingerprint, never by a model (see
HealCoordinator._heal_get_by), and a heal of a get_by_role() call must have the call's
ARIA role, by Playwright's own matcher, on the live page (gate.live_reason). A lookup that
misses raises GetByNotFound, which is both AttentionRequired and Playwright's
TimeoutError (not_found_error).
"""

from __future__ import annotations

import ast
import functools
import inspect
import json
import re
from dataclasses import dataclass

from .exceptions import AttentionRequired

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

# Why a call key is not followed into an iframe (see Healer._get_by_frames).
NOT_FOLLOWED = 'mendwright does not follow a get_by_* call into an iframe'

# The live gate's reason when a driver cannot say whether the element has the ARIA role of
# a get_by_role() call (see gate.live_reason).
ROLE_UNREADABLE = "could not check the element's ARIA role on the live page"

# The sentence a decline of a call key ends with (see HealCoordinator._heal_get_by).
NO_MODEL = ('mendwright asks no model to heal a get_by_* call: the call names its element by '
            'role and words, and those are what changed, so a model could only guess. Change '
            'the call by hand.')


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

    A key matches one spelling of a call: name=re.compile("pay", re.I) or exact=True makes
    another key. Raises ValueError for a method mendwright does not heal and for a call
    that is never a key, and TypeError for arguments the method does not take."""
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
