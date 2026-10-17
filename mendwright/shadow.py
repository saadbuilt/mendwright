"""Open shadow roots: where an element is, and how a selector runs inside one.

XPath reaches into no shadow root on any driver, and neither page.content() nor Selenium's
page_source holds what a shadow root contains. So mendwright handles an element inside an
open shadow root the way it handles one inside an iframe: by its place.

- Where it is. Each time a key's selector matches, the fingerprint read
  (fingerprint.FINGERPRINT_JS) also gives the shadow hosts around the element, outermost
  first. host_selectors() turns each one into a CSS selector that matches it and nothing
  else where it sits (its document, or the shadow root around it): a test id, id or name
  with its tag, such as my-app[id="root"], or the tag alone for a custom element that is the
  only one of its tag there. A host that none of these tell apart is None. The facade saves
  the list as the key's shadowHosts, and drops it when the element is on the page itself.
- How a selector runs there. A selector inside a shadow root is CSS. It is saved with its
  hosts as the candidate's shadowHosts, and as its text the Playwright selector chain of the
  hosts and the CSS (shadow_text): 'my-app[id="root"] >> #save'. mendwright itself runs it
  through run_target(): the CSS (the chain's last part), with a ShadowHost step per host at
  the end of the frame chain. A built-in driver enters the shadow root strictly
  (drivers.base.ENTER_JS): each host selector must match exactly one element in the document
  or shadow root before it, and that element must have an open shadow root. The CSS then
  matches only what querySelectorAll() finds in that shadow root: not the host's light DOM,
  not a nested shadow root, not another host with the same tag. Playwright's own CSS
  pierces every open shadow root, so its drivers check where each match is
  (drivers.base.PLACE_JS) before they use one. The text is a chain so that a mendwright that
  knows nothing of shadowHosts (0.9.0 and before) runs it as Playwright does, inside the
  host, and refuses it on Selenium, instead of running the bare CSS on the whole page.
- A heal of a key last found inside a shadow root runs there, through a ShadowDriver: the
  fingerprint tier and the model get that shadow root's HTML and nothing else, and the XPath
  they find is checked in that HTML and turned into a CSS selector (mendwright.css) before
  the live check runs it inside the shadow root (client.apply).

Closed shadow roots are out of reach: the browser gives a script no handle on one, so its
elements are never found, recorded or healed.
"""

import re

from .candidates import generated
from .css import css_string
from .fingerprint import HOST_ATTRS
from .selectors import CSS, InvalidSelector, check_syntax, strip_prefix

# The candidate field, and the key's field, that hold shadow hosts.
HOSTS = 'shadowHosts'

# What take_hosts() gives for a fingerprint that says nothing about shadow roots (one from a
# driver that does not read where an element is).
MISSING = object()


class ShadowHost(str):
    """A step of a frame chain that enters the open shadow root of the one element this CSS
    selector matches in the document, or the shadow root, before it (see the module docs).
    A plain str in a frame chain is an iframe's selector."""

    __slots__ = ()

    def __repr__(self):
        return 'ShadowHost(' + str.__repr__(self) + ')'


# --- hosts as saved ---------------------------------------------------------------------------

def saved_hosts(value):
    """A saved shadowHosts value as a tuple: () for none (missing, None, '' or []), else one
    entry per host, its CSS selector or None for a host nothing tells apart. Anything
    malformed counts as a host nothing tells apart, so it never reads as the page."""
    if value is None or value == '' or (isinstance(value, (list, tuple)) and not value):
        return ()
    if isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list):
        return (None,)
    return tuple(item if isinstance(item, str) and item.strip() else None for item in value)


def hosts_of(candidate):
    """The shadow hosts a candidate runs inside (saved_hosts of its shadowHosts)."""
    return saved_hosts(candidate.get(HOSTS)) if isinstance(candidate, dict) else ()


# A shadow host's selector as host_selector() builds it: its tag with one of HOST_ATTRS and a
# value in css_string's quoting, or a custom element's tag alone.
_BUILT_HOST = re.compile(
    r'[a-z][a-z0-9_-]*\[(?:' + '|'.join(re.escape(name) for name in HOST_ATTRS) + ')='
    r'"(?:[^"\\\r\n\f]|\\["\\]|\\[0-9a-f]{1,6} )*"\]'
    r'|[a-z][a-z0-9_]*-[a-z0-9_-]*')


def built_host(selector):
    """Whether selector is a shadow host's selector of the form host_selector() builds."""
    return isinstance(selector, str) and _BUILT_HOST.fullmatch(selector) is not None


def chain_hosts(candidate):
    """The shadow hosts that a heal saved inside a shadow root names in its text, for one
    that has lost its shadowHosts: a 0.9.0 process drops every shadowHosts when it writes the
    store, a candidate's as well as the key's. () unless the candidate is a heal (source
    'heal') with no frameXPath and no shadowHosts field, whose text is the chain (shadow_text)
    of hosts of the form host_selector() builds and a CSS selector. mendwright saves no other
    heal with ' >> ' in it: a heal on the page or in an iframe is an XPath, or the key's own
    selector, and a heal your code passes again is a 'code' candidate."""
    if not isinstance(candidate, dict) or candidate.get('source') != 'heal':
        return ()
    if candidate.get('frameXPath') or HOSTS in candidate:
        return ()
    text = candidate.get('xpath')
    parts = split_text(text) if isinstance(text, str) else []
    if len(parts) < 2 or not all(built_host(part) for part in parts[:-1]):
        return ()
    try:
        inner_css(parts[-1])
    except (TypeError, ValueError):     # InvalidSelector is a ValueError
        return ()
    return tuple(parts[:-1])


def chain_of(candidate):
    """The frame chain a candidate runs in: its iframe's selector (frameXPath), then a
    ShadowHost step per shadow host. A host nothing tells apart gives an empty step, which
    no driver runs (split_chain raises)."""
    frame = candidate.get('frameXPath') if isinstance(candidate, dict) else None
    steps = (frame,) if frame else ()
    return steps + tuple(ShadowHost(h or '') for h in hosts_of(candidate))


def split_chain(chain):
    """(frames, hosts) of a frame chain: the iframe selectors, then the CSS of each
    ShadowHost step, without a css= prefix. Raises InvalidSelector for a host that is not
    CSS (or is empty), and for an iframe after a shadow host, which no driver reaches."""
    frames, hosts = [], []
    for step in chain or ():
        if isinstance(step, ShadowHost):
            hosts.append(host_css(step))
        elif hosts:
            raise InvalidSelector.build(
                None, None, 'an iframe inside a shadow root is not supported.',
                'Open mendwright on that frame instead.')
        else:
            frames.append(step)
    return tuple(frames), tuple(hosts)


def host_css(selector):
    """The CSS of a shadow host's selector, without a css= prefix. InvalidSelector for one
    that is empty or not CSS: a host is found with querySelectorAll()."""
    try:
        kind = check_syntax(selector)
    except TypeError:
        raise InvalidSelector.build(None, None, 'a shadow host is not saved as a selector.',
                                    'Check the shadowHosts of the key in overrides.json.') from None
    if kind != CSS:
        raise InvalidSelector.build(
            selector, kind, repr(selector) + ' is not a CSS selector, and a shadow host is '
            'looked up with CSS.', 'Check the shadowHosts of the key in overrides.json.')
    return strip_prefix(selector)


def inner_css(selector):
    """The CSS of a selector that runs inside a shadow root, without a css= prefix.
    InvalidSelector for any other kind: XPath does not reach into a shadow root on any
    driver, and neither does a Playwright engine selector run that way."""
    kind = check_syntax(selector, calls=True)
    if kind != CSS:
        raise InvalidSelector.build(
            selector, kind, repr(selector) + ' is not CSS, and only a CSS selector runs inside '
            'a shadow root.', 'Use a CSS selector, such as \'#save\' or \'button[name="save"]\'.')
    return strip_prefix(selector)


def as_document(inner):
    """A shadow root's HTML (its innerHTML) as a document, as the gate, the fingerprint tier
    and the model read it: '<html><head></head><body>' + inner + '</body></html>'. The host
    is left out: a selector that names it would not run inside its shadow root."""
    return '<html><head></head><body>' + inner + '</body></html>'


def shadow_text(hosts, css):
    """The text a selector inside the shadow root that hosts lead to is saved with: the
    Playwright selector chain of the hosts and the CSS, 'my-app[id="root"] >> #save' (see the
    module docs). Playwright's CSS pierces open shadow roots, so each part finds the next one
    inside the element before it; mendwright fix writes it into Playwright code that way. It
    is looser than mendwright's own lookup (run_target), and Selenium cannot run it."""
    return ' >> '.join([strip_prefix(h) for h in hosts] + [strip_prefix(css)])


def split_text(text):
    """The parts of a Playwright selector chain, split at each ' >> ' outside quotes and
    brackets, each one trimmed."""
    parts, quote, depth, start, i = [], None, 0, 0, 0
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
            parts.append(text[start:i].strip())
            start = i = i + len(' >> ')
            continue
        i += 1
    parts.append(text[start:].strip())
    return parts


def run_target(candidate):
    """(selector, frame_chain) a candidate runs with: its text and its iframe for one on the
    page or in an iframe; for one inside a shadow root, the CSS its text ends in, and its
    iframe, if any, then a ShadowHost step per host (chain_of). Raises InvalidSelector for a
    candidate inside a shadow root whose text is not the chain of its hosts and a CSS
    selector (shadow_text), or whose hosts are not all saved as CSS selectors."""
    hosts = hosts_of(candidate)
    selector = candidate.get('xpath')
    if not hosts:
        return selector, chain_of(candidate)
    if not isinstance(selector, str):
        raise InvalidSelector.build(None, None, 'a selector inside a shadow root is not saved '
                                    'as text.', 'Check the key in overrides.json.')
    split_chain(chain_of(candidate))
    parts = split_text(selector)
    if len(parts) != len(hosts) + 1 or parts[:-1] != [strip_prefix(h) for h in hosts]:
        raise InvalidSelector.build(
            selector, None, repr(selector) + ' was saved inside a shadow root, and it is not '
            'the chain of its shadow hosts and a CSS selector (' + shadow_text(hosts, 'CSS')
            + ').', 'Check the key in overrides.json.')
    css = inner_css(parts[-1])
    return css, chain_of(candidate)


# --- where an element is ------------------------------------------------------------------------

_TAG = re.compile(r'^[a-z][a-z0-9_-]*$')
_VALUE_LIMIT = 200


def host_selector(facts):
    """A CSS selector for one shadow host, from what HOST_FACTS_JS read of it, that matches
    it and nothing else where it sits: its tag with a test id, an id or a name that no other
    element of its tag there has, best first, else its tag alone when it is a custom element
    (its tag has a '-') that is the only one of its tag there. None when nothing tells it
    apart. An id, name or test id that looks generated is never used: the next build of the
    page gives it another one."""
    if not isinstance(facts, dict):
        return None
    tag = facts.get('tag')
    if not isinstance(tag, str) or not _TAG.match(tag):
        return None
    attrs = facts.get('attrs') if isinstance(facts.get('attrs'), dict) else {}
    counts = facts.get('counts') if isinstance(facts.get('counts'), dict) else {}
    for name in HOST_ATTRS:
        value = attrs.get(name)
        if not (isinstance(value, str) and value.strip() and len(value) <= _VALUE_LIMIT
                and '\n' not in value) or generated(value):
            continue
        if counts.get(name) == 1:
            return tag + '[' + name + '=' + css_string(value) + ']'
    if '-' in tag and counts.get('tag') == 1:
        return tag
    return None


def host_selectors(raw):
    """host_selector() for each host of a hosts list HOST_FACTS_JS read, outermost first."""
    return [host_selector(item) for item in raw] if isinstance(raw, list) else []


def with_hosts(fp, raw):
    """A fingerprint (fingerprint.from_raw(raw)) with the hosts of the read added under
    shadowHosts: a list, [] for an element on the page. A raw read without hosts gives fp as
    it is. The facade takes them out again before it saves the fingerprint (take_hosts)."""
    if fp is None or not isinstance(raw, dict) or not isinstance(raw.get('hosts'), list):
        return fp
    return dict(fp, **{HOSTS: host_selectors(raw['hosts'])})


def take_hosts(fp):
    """(fp, hosts): the fingerprint without shadowHosts, and the hosts it had (a list, []
    for the page), or MISSING when it had none (a driver that does not read where an
    element is)."""
    if not isinstance(fp, dict) or HOSTS not in fp:
        return fp, MISSING
    hosts = fp[HOSTS]
    rest = {k: v for k, v in fp.items() if k != HOSTS}
    return rest, list(saved_hosts(hosts)) if isinstance(hosts, list) else [None]


def roots_of(raw):
    """drivers.base.SHADOW_ROOTS_JS's result as (roots, cut): roots is a list of {'hosts':
    the host selectors (host_selectors), 'count': how many elements a CSS selector matches
    there or None, 'html': the root's HTML or None}; cut says the read left some out. ([],
    True) for anything malformed, which mendwright then does not act on."""
    if not isinstance(raw, dict) or not isinstance(raw.get('roots'), list):
        return [], True
    roots = []
    for item in raw['roots']:
        if not isinstance(item, dict) or not isinstance(item.get('hosts'), list):
            return [], True
        count = item.get('count')
        html = item.get('html')
        roots.append({'hosts': host_selectors(item['hosts']),
                      'count': count if isinstance(count, int) and not isinstance(count, bool)
                      else None,
                      'html': html if isinstance(html, str) else None})
    return roots, raw.get('cut') is True


# --- words ------------------------------------------------------------------------------------

def place_text(hosts):
    """'the shadow root of inner-box inside my-app[id="root"]': the shadow root the hosts
    lead to, in words. A host nothing tells apart reads as 'an element nothing tells apart'."""
    names = [h if h else 'an element nothing tells apart' for h in hosts]
    if not names:
        return 'the page'
    text = 'the shadow root of ' + names[-1]
    if len(names) > 1:
        text += ' inside ' + ' inside '.join(reversed(names[:-1]))
    return text


# --- a driver for the inside of one shadow root -------------------------------------------------

_OPTIONAL = ('describe', 'fingerprint', 'visible_match', 'role_match', 'act_on')


class ShadowDriver:
    """A driver for the inside of the open shadow root that hosts lead to, over a base driver
    that reaches into shadow roots (drivers.base.reaches_shadow): each call adds the hosts to
    the frame chain as ShadowHost steps, so a selector runs only inside that shadow root, and
    outer_html() and inventory() give what that shadow root holds. HealCoordinator.scoped()
    heals through one; it has no shadow_html() of its own, so no lookup goes further in.
    The base driver's optional methods it lacks are None here too."""

    def __init__(self, base, hosts):
        self._base = base
        self.shadow_hosts = tuple(hosts)
        self._steps = tuple(ShadowHost(h) for h in self.shadow_hosts)
        for name in _OPTIONAL:
            if not callable(getattr(base, name, None)):
                setattr(self, name, None)

    def _in(self, frame_chain):
        if frame_chain:
            raise InvalidSelector.build(None, None, 'an iframe inside a shadow root is not '
                                        'supported.', 'Open mendwright on that frame instead.')
        return self._steps

    def count(self, selector, frame_chain=()):
        return self._base.count(selector, self._in(frame_chain))

    def is_visible(self, selector, frame_chain=()):
        return self._base.is_visible(selector, self._in(frame_chain))

    def scroll_into_view(self, selector, frame_chain=()):
        return self._base.scroll_into_view(selector, self._in(frame_chain))

    def handle(self, selector, frame_chain=()):
        return self._base.handle(selector, self._in(frame_chain))

    def act(self, selector, verb, *args, frame_chain=(), **kwargs):
        return self._base.act(selector, verb, *args, frame_chain=self._in(frame_chain), **kwargs)

    def act_on(self, native, verb, *args, **kwargs):
        return self._base.act_on(native, verb, *args, **kwargs)

    def url(self):
        return self._base.url()

    def describe(self, selector, frame_chain=()):
        return self._base.describe(selector, self._in(frame_chain))

    def fingerprint(self, selector, frame_chain=(), **options):
        return self._base.fingerprint(selector, self._in(frame_chain), **options)

    def visible_match(self, selector, frame_chain=()):
        return self._base.visible_match(selector, self._in(frame_chain))

    def role_match(self, selector, role, level=None, frame_chain=()):
        return self._base.role_match(selector, role, level, self._in(frame_chain))

    def inner_html(self):
        """What the shadow root holds (its innerHTML), or None when the hosts lead to no
        open shadow root now. Errors read as None."""
        try:
            html = self._base.shadow_html(self.shadow_hosts)
        except Exception:
            return None
        return html if isinstance(html, str) else None

    def outer_html(self, scope=None):
        """The shadow root's HTML as a document, '<html><head></head><body>' + its innerHTML +
        '</body></html>', for the gate and the model; '' when the hosts lead to none. The
        host itself is left out: a selector that names it would not run inside its shadow
        root."""
        if scope:
            return ''
        html = self.inner_html()
        return '' if html is None else as_document(html)

    def inventory(self, limit=200):
        method = getattr(self._base, 'shadow_inventory', None)
        if not callable(method):
            return []
        try:
            out = method(self.shadow_hosts, limit)
        except Exception:
            return []
        return out if isinstance(out, list) else []


def driver_shadowed(driver, hosts):
    """A ShadowDriver for the shadow root that hosts lead to, or None when the driver does
    not reach into shadow roots or a host is not saved as a selector."""
    from .drivers.base import reaches_shadow
    if not hosts or any(not h for h in hosts) or not reaches_shadow(driver):
        return None
    return ShadowDriver(driver, hosts)
