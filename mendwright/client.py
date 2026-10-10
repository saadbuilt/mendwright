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

from .gate import live_reason
from .selectors import XPATH, classify, normalize
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


def _quarantined_paths(store, selector_key, html):
    """(doc, paths): the captured page parsed the way validate() parses it, and the tree
    paths of the elements the key's quarantined selectors match in it. (None, set()) when
    the page cannot be parsed; stores without quarantined() give no paths."""
    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return None, set()
    listed = getattr(store, 'quarantined', None)
    tree, paths = doc.getroottree(), set()
    try:
        stored = list(listed(selector_key)) if callable(listed) else []
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


def refuses_destructive_target(store, selector_key, meta):
    """True when a heal of selector_key must not land on an element whose label looks
    destructive (see mendwright.destructive). On by default. Off when meta says
    refuseDestructiveTarget=False, or when the store holds an explicit destructive=False
    for the key (heal.locator(..., destructive=False) or store.set_meta(...))."""
    flag = (meta or {}).get('refuseDestructiveTarget')
    if isinstance(flag, bool):
        return flag
    choice = getattr(store, 'destructive_choice', None)
    if callable(choice):
        try:
            if choice(selector_key) is False:
                return False
        except Exception:
            pass
    return True


def apply(store, selector_key, proposal, html, meta, driver=None):
    """Validate the proposal and apply the first candidate that passes the full gate to the
    override store. Returns (applied_xpath, reason): the xpath + 'ok' on success, or
    (None, <last reason>) so the caller can feed a real reason into a reprompt.

    The gate is: static validate() against the captured HTML AND, when a live `driver` is
    supplied, the live check against the current page (exactly-one + visible). A candidate
    that validates statically but is not uniquely present live is NOT applied.

    A button or link (or input button) whose label contains a destructive word is refused,
    in the HTML and on the live page, unless the key was opted out (see
    refuses_destructive_target). So a key that is not destructive itself is never healed
    onto a "Pay now" or "Delete account" control.

    A candidate the store already holds as quarantined or superseded is rejected with
    reason 'previously quarantined' / 'previously superseded'. Adding it again would not
    reactivate it, so the retry would fail and cost another model call. A quarantine also
    blocks the element itself: a proposal that selects the element of a quarantined
    selector under another spelling ("(//input[@id='a'])[1]" for "//input[@id='a']") is
    rejected as well, until that row is removed from the store.
    """
    refuse = refuses_destructive_target(store, selector_key, meta)
    live_meta = dict(meta, refuseDestructiveTarget=refuse)
    doc, bad = _quarantined_paths(store, selector_key, html)
    candidates = [proposal.get('xpath')] + list(proposal.get('fallbacks') or [])
    reason = 'no candidates in proposal'
    for proposed in candidates:
        if not proposed:
            continue
        if not isinstance(proposed, str):
            reason = 'proposal is not a string'
            continue
        body, xpath = _xpath_forms(proposed)
        retired = _RETIRED.get(_stored_status(store, selector_key, xpath))
        if retired:
            reason = retired
            continue
        result = validate(body, html, expected_role=meta.get('expectedRole'),
                          expected_text=meta.get('expectedText'),
                          destructive=bool(meta.get('destructive')),
                          refuse_destructive_target=refuse)
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
        live = live_reason(driver, xpath, live_meta)
        if live:
            reason = live
            continue
        # proposal['frameXPath'] is unvalidated model output and is never stored. Frame
        # context comes from the gate, not from a proposal.
        store.add_candidate(selector_key, xpath, source='heal')
        return xpath, reason
    return None, reason


def report_outcome(gateway, selector_key, xpath, success, *, download_key, http=None,
                   timeout=15, auth_header=DEFAULT_AUTH_HEADER):
    http = http or _default_http
    try:
        http.post(gateway.rstrip('/') + '/heal/outcome',
                  json={'key': selector_key, 'xpath': xpath, 'success': bool(success)},
                  headers={auth_header: download_key}, timeout=timeout)
    except Exception:
        pass
