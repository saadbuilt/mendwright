"""Selector kinds: XPath, CSS, or a Playwright engine selector.

mendwright stores selectors as plain strings. ``classify`` decides how a driver should
run one, using Playwright's conventions:

- ``xpath=...``, or anything starting with ``/``, ``(``, ``./`` or ``..`` is XPath.
- ``css=...`` is CSS.
- ``text=``, ``role=``, ``id=``, ``data-testid=``, ``internal:...`` and any other
  ``name=`` prefix is a Playwright selector engine. So is a chain such as
  ``//form >> text=Save``. These only run on Playwright.
- Anything else (``#save``, ``form button.primary``) is CSS.

``check_syntax`` catches what can be caught without a browser: a non-string (for example
a Playwright Locator), an empty selector, or XPath that lxml cannot compile. CSS and engine
selectors are checked by the browser when they run; the drivers turn that error into the
same ``InvalidSelector``.

Heals always produce XPath, because the safety gate evaluates proposals with lxml. A heal
for a key whose code uses CSS also gets a CSS form for mendwright fix (mendwright.css).
"""

import functools
import re

import lxml.etree

XPATH = 'xpath'
CSS = 'css'
ENGINE = 'engine'

_XPATH_STARTS = ('/', '(', './', '..')
# A Playwright engine prefix: 'text=', 'role=', 'data-testid=', 'nth=', '_react=', ...
# A CSS selector can never start with "name=" (attribute tests live in brackets), so any
# such prefix is an engine, including ones registered with playwright's selectors.register.
_ENGINE_PREFIX = re.compile(r'^(?:internal:|_?[A-Za-z][\w-]*=)')

_TYPE_HINT = ("pass a selector string such as '#save' or \"role=button[name='Save']\"; "
              "Locator objects can't be stored")


class InvalidSelector(ValueError):
    """A selector that can never match because it is malformed, or because the current
    driver cannot run its kind (a Playwright engine selector on Selenium).

    Raised straight away instead of being polled until the timeout. It is a ValueError,
    not an AttentionRequired, so the heal loop never spends a model call on a typo.

    Attributes: ``selector`` (the string as given), ``kind`` (xpath, css or engine, when
    known), ``reason`` (what is wrong) and ``hint`` (what to do about it).
    """

    def __init__(self, message, *, selector=None, kind=None, reason=None, hint=None):
        super().__init__(message)
        self.selector = selector
        self.kind = kind
        self.reason = reason or message
        self.hint = hint or ''

    @classmethod
    def build(cls, selector, kind, reason, hint):
        return cls('mendwright: ' + reason + ' ' + hint, selector=selector, kind=kind,
                   reason=reason, hint=hint)

    def for_key(self, key):
        """The same error, with the selector key named in the message."""
        message = ('mendwright: the selector for ' + repr(key) + ' is not usable: '
                   + self.reason + ' ' + self.hint).rstrip()
        return InvalidSelector(message, selector=self.selector, kind=self.kind,
                               reason=self.reason, hint=self.hint)


def _require_str(selector):
    if not isinstance(selector, str):
        raise TypeError('mendwright: got ' + type(selector).__name__ + ' where a selector '
                        'was expected; ' + _TYPE_HINT + '.')


def _is_chain(text):
    """True if text contains a Playwright ' >> ' chain outside quotes and brackets."""
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == '\\':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch in '[(':
            depth += 1
        elif ch in '])':
            depth = max(0, depth - 1)
        elif depth == 0 and text.startswith(' >> ', i):
            return True
        i += 1
    return False


def classify(selector):
    """Return 'xpath', 'css' or 'engine' for a selector string (see the module docstring).

    Raises TypeError for a non-string such as a Playwright Locator."""
    _require_str(selector)
    text = selector.strip()
    if _is_chain(text):
        return ENGINE
    if text.startswith('xpath='):
        return XPATH
    if text.startswith(_XPATH_STARTS):
        return XPATH
    if text.startswith('css='):
        return CSS
    if _ENGINE_PREFIX.match(text):
        return ENGINE
    return CSS


def strip_prefix(selector):
    """The selector body without a leading 'xpath=' or 'css=' (and without surrounding
    whitespace). Engine selectors and chains come back unchanged apart from the trim."""
    kind = classify(selector)
    text = selector.strip()
    if kind == XPATH and text.startswith('xpath='):
        return text[len('xpath='):].strip()
    if kind == CSS and text.startswith('css='):
        return text[len('css='):].strip()
    return text


def normalize(selector):
    """A canonical spelling for storing a selector: trimmed, and without a redundant
    'xpath=' or 'css=' prefix. The prefix stays when dropping it would change the kind,
    so 'xpath=html/body' and 'css=/x' keep theirs."""
    kind = classify(selector)
    body = strip_prefix(selector)
    if kind == ENGINE:
        return body
    if body and classify(body) == kind:
        return body
    return ('xpath=' if kind == XPATH else 'css=') + body


@functools.lru_cache(maxsize=1024)
def _compile_xpath(expr):
    lxml.etree.XPath(expr)  # raises lxml.etree.XPathSyntaxError on bad syntax
    return True


def check_syntax(selector):
    """Check what can be checked without a browser and return the selector's kind.

    - Non-string (for example a Playwright Locator): TypeError.
    - Empty selector, or an XPath that lxml cannot compile: InvalidSelector.
    - CSS and engine selectors pass here; the browser checks them when they run.
    """
    _require_str(selector)
    if not selector.strip():
        raise InvalidSelector.build(
            selector, None, 'the selector is empty.',
            "Pass an XPath such as \"//button[@id='save']\" or a CSS selector such as '#save'; "
            "mendwright.selectors.check_syntax() checks one without a browser.")
    kind = classify(selector)
    body = strip_prefix(selector)
    if not body or (kind == ENGINE and _ENGINE_PREFIX.fullmatch(body)):
        raise InvalidSelector.build(
            selector, kind, repr(selector) + ' has a prefix but nothing after it.',
            "Add the expression after the prefix, for example \"css=#save\" or \"text=Save\", "
            "and check it with mendwright.selectors.check_syntax().")
    if kind == XPATH:
        try:
            _compile_xpath(body)
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            raise InvalidSelector.build(
                selector, kind, repr(body) + ' is not valid XPath (lxml says: ' + detail + ').',
                'Check its brackets and quotes; you can test a fix with '
                'mendwright.selectors.check_syntax().') from exc
    return kind
