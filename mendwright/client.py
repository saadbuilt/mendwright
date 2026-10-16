"""Client side of the heal loop: POST the capture bundle to the gateway, validate
the returned selector locally, apply it to the writable override layer, and report
the canary outcome. Validation (``validate``) is the gate before applying.

Tool-agnostic: the gateway auth header name defaults to ``X-Heal-Key``
(``DEFAULT_AUTH_HEADER``) but can be overridden per tool via ``auth_header``.
"""

import json as _json
import urllib.error
import urllib.request

import lxml.html

from . import getby
from .css import css_selectors
from .fit import has_position
from .gate import list_live_reason, live_reason
from .selectors import CSS, XPATH, classify, frame_spelling, normalize
from .validation import validate

DEFAULT_AUTH_HEADER = 'X-Heal-Key'  # generic; a tool may pass its own via auth_header=
# A real User-Agent is REQUIRED: Cloudflare's edge rejects the urllib default
# ("Python-urllib/x.y") with HTTP 403 error 1010 before the request reaches the
# worker, so an absent/default UA silently breaks every heal.
USER_AGENT = 'mendwright-client/0.2'


class _UrllibHttp:
    def post(self, url, *, json=None, headers=None, timeout=30):
        body = _json.dumps(json or {}).encode('utf-8')
        req = urllib.request.Request(
            url, data=body, method='POST',
            headers={'content-type': 'application/json', 'User-Agent': USER_AGENT,
                     **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _Resp(resp.getcode(), resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:  # non-2xx still carries a body/status
            return _Resp(exc.code, exc.read().decode('utf-8', 'replace'))


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self._text = text

    def json(self):
        return _json.loads(self._text)


_default_http = _UrllibHttp()


def request_heal(bundle, *, gateway, download_key, http=None, timeout=30,
                 auth_header=DEFAULT_AUTH_HEADER, on_error=None):
    """POST the capture bundle to ``<gateway>/heal`` and return the gateway's proposal
    dict, or None when there is no usable proposal.

    ``on_error`` is optional. When given, it is called with one short, readable reason
    each time None is returned, for example "the gateway returned HTTP 429 (rate limited)."
    The reason never contains ``download_key``. GatewayProvider uses this to fill its
    ``last_error``; callers that leave it out get exactly the old behaviour.
    """
    http = http or _default_http
    url = gateway.rstrip('/') + '/heal'

    def fail(describe):
        if on_error is not None:
            try:
                # Imported here, not at module level: providers imports this module.
                from .providers.gateway import gateway_errors
                on_error(describe(gateway_errors(url, download_key)))
            except Exception:
                pass  # reporting a failure must never turn into a new one
        return None

    try:
        resp = http.post(url, json=bundle, headers={auth_header: download_key}, timeout=timeout)
    except Exception as exc:
        error = exc  # keep a reference: the name exc is cleared when this block ends
        return fail(lambda errors: errors.exception(error))
    if getattr(resp, 'status_code', 0) != 200:
        return fail(lambda errors: errors.response(resp))
    try:
        data = resp.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        return fail(lambda errors: errors.bad_body())
    if data.get('status') == 'needs_human':
        return fail(lambda errors: errors.needs_human(data.get('reason')))
    if not data.get('xpath'):
        return fail(lambda errors: errors.no_xpath())
    return data


# Statuses a candidate can have in the store besides 'active', with the reason apply()
# gives when a proposal repeats one of them. The re-prompt passes the reason to the model.
_RETIRED = {
    'quarantined': 'previously quarantined',
    'superseded': 'previously superseded',
}


# apply()'s reason for a proposal that selects the element of a quarantined heal under a
# different spelling. The re-prompt shows it to the model, so it says what to do.
_SAME_ELEMENT = ('previously quarantined (the same element as a quarantined heal; '
                 'choose a different element)')

# apply()'s reason for a positional XPath when the model read a fitted page (mendwright.fit).
POSITIONAL = ('it uses a position ([2], last() or position()), but parts of the page were '
              'left out of the snapshot to fit the prompt, so positions there do not match the '
              'live page. Anchor on attributes or text instead')


def _quarantined_paths(store, selector_key, html, frame_xpath=''):
    """(doc, paths): the captured page parsed the way validate() parses it, and the tree
    paths of the elements the key's quarantined selectors match in it. Only quarantines of
    the same place count: the page ('') or the iframe frame_xpath, whose document ``html``
    then is. A selector quarantined inside an iframe says nothing about an element that
    looks the same on the page. (None, set()) when the page cannot be parsed; stores
    without quarantined() give no paths, and a store whose quarantined() takes no
    frame_xpath gives all of them."""
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return None, set()
    listed = getattr(store, 'quarantined', None)
    tree, paths = doc.getroottree(), set()
    stored = []
    if callable(listed):
        try:
            stored = list(listed(selector_key, frame_xpath=frame_xpath))
        except TypeError:
            try:
                stored = list(listed(selector_key))
            except Exception:
                stored = []
        except Exception:
            stored = []
    for selector in stored:
        try:
            body = _xpath_forms(selector)[0]   # drop any 'xpath=' prefix
            paths.update(tree.getpath(n) for n in doc.xpath(body) if hasattr(n, 'tag'))
        except Exception:
            continue   # a CSS or engine selector, or one lxml cannot run
    return doc, paths


def _stored_status(store, selector_key, xpath):
    """The store's status for this candidate ('active', 'quarantined', ...), or None when
    the store has no such candidate or cannot say (stores without a status() method)."""
    status = getattr(store, 'status', None)
    if not callable(status):
        return None
    try:
        return status(selector_key, xpath)
    except Exception:
        return None


def _xpath_forms(proposed):
    """(body, stored) for a proposed xpath: the expression lxml evaluates, and the string
    to store so the drivers also run it as XPath. 'descendant::x' or "id('a')" would be
    read as CSS without the prefix, so those keep 'xpath='."""
    text = proposed.strip()
    body = text[len('xpath='):].strip() if text.startswith('xpath=') else text
    return body, normalize('xpath=' + body)


def _code_uses_css(store, selector_key):
    """True when one of the key's selectors from code is CSS."""
    try:
        return any(c.get('source') == 'code' and isinstance(c.get('xpath'), str)
                   and classify(c['xpath']) == CSS for c in store.candidates(selector_key))
    except Exception:
        return False


def _same_live(driver, css, xpath):
    """True when css matches one element on the live page and it is the one xpath
    matches (same outer HTML). Without a driver the captured page's check stands."""
    if driver is None:
        return True
    try:
        if driver.count(css) != 1:
            return False
        describe = getattr(driver, 'describe', None)
        if not callable(describe):
            return True
        a, b = describe(css), describe(xpath)
        return (isinstance(a, dict) and isinstance(b, dict) and bool(a.get('outer_html'))
                and a.get('outer_html') == b.get('outer_html'))
    except Exception:
        return False


def css_twin(store, selector_key, doc, body, xpath, driver=None):
    """The CSS form to save with a heal: None when the key's code doesn't use CSS (nothing
    to save), '' when no CSS selector could be confirmed, else a CSS selector that matches
    the healed element and nothing else on the captured page (see mendwright.css) and on
    the live page. Without a live driver nothing is confirmed, because browsers match some
    values (type, and ids and classes in quirks mode) without regard to case."""
    if not _code_uses_css(store, selector_key):
        return None
    if doc is None or driver is None:
        return ''
    try:
        hits = [h for h in doc.xpath(body) if isinstance(getattr(h, 'tag', None), str)]
    except Exception:
        return ''
    if len(hits) != 1:
        return ''
    for css in css_selectors(hits[0], doc):
        if _same_live(driver, css, xpath):
            return css
    return ''


def add_heal(store, selector_key, xpath, *, css=None, frame_xpath=None):
    """store.add_candidate(key, xpath, source='heal'), with css= and frame_xpath= when they
    are given. A store whose add_candidate is older still gets the heal: without css= (new
    in 0.6), and without frame_xpath= when it takes neither and the heal is on the page.
    Returns False, and saves nothing, when it would have to keep a heal that belongs inside
    an iframe and cannot."""
    extra = {}
    if css is not None:
        extra['css'] = css
    if frame_xpath is not None:
        extra['frame_xpath'] = frame_xpath
    tries = [extra]
    if 'css' in extra:
        tries.append({k: v for k, v in extra.items() if k != 'css'})
    if not frame_xpath:
        tries.append({})
    for kwargs in tries:
        try:
            store.add_candidate(selector_key, xpath, source='heal', **kwargs)
            return True
        except TypeError as exc:
            if not kwargs or 'keyword' not in str(exc):
                raise
    return False


def apply(store, selector_key, proposal, html, meta, driver=None, positional=True,
          frame_xpath=None):
    """Validate the proposal and apply the first candidate that passes the full gate to the
    override store. Returns (applied_xpath, reason): the xpath + 'ok' on success, or
    (None, <last reason>) so the caller can feed a real reason into a reprompt.

    The gate is: static validate() against the captured HTML AND, when a live `driver` is
    supplied, the live check against the current page (exactly-one + visible). A candidate
    that validates statically but is not uniquely present live is NOT applied. For the key
    of a get_by_role() call (see mendwright.getby), the live check also asks Playwright
    whether the element has the call's ARIA role (see gate.live_reason).

    A candidate the store already holds as quarantined or superseded is rejected with
    reason 'previously quarantined' / 'previously superseded'. Adding it again would not
    reactivate it, so the retry would fail and cost another model call. A quarantine also
    blocks the element itself: a proposal that selects the element of a quarantined
    selector under another spelling ("(//input[@id='a'])[1]" for "//input[@id='a']") is
    rejected as well, until that row is removed from the store.

    When one of the key's selectors in code is CSS, a CSS selector that matches the same
    element is saved with the heal as its ``css`` (see css_twin), for mendwright fix. A
    candidate with the same text saved inside another iframe, or with an older CSS form,
    is updated to this heal's.

    frame_xpath is the selector of the iframe the heal was made in, when the coordinator
    healed inside one (``html`` and ``driver`` are then that iframe's); it is saved with the
    candidate so the resolver looks for it there. Such a heal is saved and returned with
    its 'xpath=' prefix written out (see selectors.frame_spelling), so it never shares a
    row with the same text on the page, and only quarantines made in that iframe block it.

    positional=False rejects any candidate with a position in it ([2], last(),
    position()). The coordinator passes it when the model read a page with parts left out
    (see mendwright.fit), where a position counts different elements than the live page.
    """
    doc, bad = _quarantined_paths(store, selector_key, html, frame_xpath or '')
    candidates = [proposal.get('xpath')] + list(proposal.get('fallbacks') or [])
    reason = 'no candidates in proposal'
    for proposed in candidates:
        if not proposed:
            continue
        if not isinstance(proposed, str):
            reason = 'proposal is not a string'
            continue
        body, xpath = _xpath_forms(proposed)
        if frame_xpath:
            xpath = frame_spelling(xpath)
        if not positional and has_position(body):
            reason = POSITIONAL
            continue
        retired = _RETIRED.get(_stored_status(store, selector_key, xpath))
        if retired:
            reason = retired
            continue
        result = validate(body, html, expected_role=meta.get('expectedRole'),
                          expected_text=meta.get('expectedText'),
                          destructive=bool(meta.get('destructive')))
        reason = result.reason
        if not result.ok:
            continue
        if bad:
            hit = [n for n in doc.xpath(body) if hasattr(n, 'tag')]
            if hit and doc.getroottree().getpath(hit[0]) in bad:
                reason = _SAME_ELEMENT
                continue
        if classify(xpath) != XPATH:  # defensive: never store a model's engine selector
            reason = 'not an XPath expression'
            continue
        live = live_reason(driver, xpath, meta, call=getby.parse(selector_key))
        if live:
            reason = live
            continue
        # proposal['frameXPath'] is unvalidated model output and is never stored. Frame
        # context comes from the gate, not from a proposal.
        # frame_xpath '' puts a candidate with the same text, saved inside an iframe before,
        # on the page, where this heal was checked.
        css = css_twin(store, selector_key, doc, body, xpath, driver)
        if not add_heal(store, selector_key, xpath, css=css, frame_xpath=frame_xpath or ''):
            return None, 'this store cannot save a selector inside an iframe'
        return xpath, reason
    return None, reason


# list_gate()'s reasons for a list that a quarantine holds. An accepted list heal is
# quarantined when the selector it replaced matches again (SelectorStore.retire_list_heals).
_REJECTED_LIST = ('it is a list heal you rejected, or one mendwright quarantined: it quarantines '
                  'a list heal you accepted once the selector it replaced matches again, even on '
                  'one element')
_REJECTED_ELEMENTS = ('it takes elements of a list heal you rejected, or of a quarantined '
                      'selector of this key')


def _code_back(store, selector_key, driver):
    """For a list found for heal.locator_all(..., nonempty=True): the first of the key's
    selectors from code or a seed file (on the page, not in an iframe) that matches one
    element or more on the live page now, as a list that was still loading does once it came
    while mendwright looked for it, else ''. Driver errors are skipped."""
    if driver is None:
        return ''
    try:
        own = [c['xpath'] for c in store.candidates(selector_key)
               if c.get('source') in ('code', 'seed') and not c.get('frameXPath')]
    except Exception:
        return ''
    for selector in own:
        try:
            if driver.count(selector):
                return selector
        except Exception:
            continue
    return ''


def list_gate(store, selector_key, xpath, html, meta, driver, css=None):
    """The checks of a list found for heal.locator_all(..., nonempty=True) (see
    mendwright.lists), which mendwright proposes and never saves as a selector it uses:
    (kind, reason, count, css_twin). kind is

    - 'came' when a selector of the key's code or seed file matches on the live page now
      (reason is that selector): the list came while mendwright looked for it. Checked
      first, so a list that came is never refused;
    - 'accepted' when the key already has this XPath as a list heal a person accepted;
    - 'refused' with the reason: not an XPath; a list heal of the key that was rejected or
      quarantined; lists.validate() (no position or brittle path, two or more elements of
      the key's role in the captured page, none inside another); an element a quarantined
      selector of the key matches on the page; or the live check (gate.list_live_reason);
    - 'ok', with count the elements it matches in the captured page, and css_twin the CSS
      form css when one of the key's selectors in code is CSS and css matches as many
      elements on the live page as the XPath, else None.

    A superseded or proposed list heal with the same XPath passes: it may be proposed
    again."""
    from .lists import validate as validate_list
    came = _code_back(store, selector_key, driver)
    if came:
        return 'came', came, 0, None
    if not isinstance(xpath, str) or not xpath.strip():
        return 'refused', 'no candidates in proposal', 0, None
    body, stored = _xpath_forms(xpath)
    if classify(stored) != XPATH:
        return 'refused', 'not an XPath expression', 0, None
    status = _stored_status(store, selector_key, stored)
    if status == 'quarantined':
        return 'refused', _REJECTED_LIST, 0, None
    if status == 'accepted':
        return 'accepted', '', 0, None
    result = validate_list(body, html, meta.get('expectedRole'))
    if not result.ok:
        return 'refused', result.reason, result.count, None
    doc, bad = _quarantined_paths(store, selector_key, html, '')
    if bad:
        tree = doc.getroottree()
        if any(tree.getpath(n) in bad for n in doc.xpath(body) if hasattr(n, 'tag')):
            return 'refused', _REJECTED_ELEMENTS, result.count, None
    live = list_live_reason(driver, stored, meta)
    if live:
        return 'refused', live, result.count, None
    twin = None
    if css and _code_uses_css(store, selector_key):
        try:
            if driver.count(css) == driver.count(stored):
                twin = css
        except Exception:
            twin = None
    return 'ok', 'ok', result.count, twin


def report_outcome(gateway, selector_key, xpath, success, *, download_key, http=None,
                   timeout=15, auth_header=DEFAULT_AUTH_HEADER):
    http = http or _default_http
    try:
        http.post(gateway.rstrip('/') + '/heal/outcome',
                  json={'key': selector_key, 'xpath': xpath, 'success': bool(success)},
                  headers={auth_header: download_key}, timeout=timeout)
    except Exception:
        pass
