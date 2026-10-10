"""Functional success test for the self-healing loop.

A ``FakePage`` evaluates xpaths against a real lxml DOM, so ``resolve`` behaves like
Playwright: the seeded (stale) selector genuinely fails to match, and recovery only
counts if a *healed* selector genuinely matches the same DOM. This proves the whole
loop end-to-end: resolve-fails -> capture -> heal -> validate -> apply -> retry -> match.

The offline case uses a stub gateway (deterministic). The live case (opt-in via
MENDWRIGHT_GATEWAY + MENDWRIGHT_KEY) exercises the real deployed worker + Sonnet.
"""

import os

import lxml.html
import pytest

from mendwright import (
    AttentionRequired,
    HealCoordinator,
    SelectorStore,
    resolve,
    resolve_with_heal,
)

# A registration page where the terms checkbox lost its old id ('terms-de') and now
# has id 'tc' — exactly the kind of drift the healer must recover from.
HTML = (
    '<form>'
    '<label><input type="checkbox" id="tc"> I accept the terms and conditions</label>'
    '<button type="submit">Register</button>'
    '</form>'
)
STALE = "//input[@id='terms-de']"
HEALED = "//input[@type='checkbox']"


def seed():
    return {'schema': 2, 'selectors': {'terms': {
        'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False,
        'maxCandidates': 5, 'stage': 'registration', 'action': 'check',
        'candidates': [{'xpath': STALE, 'source': 'seed', 'addedAt': None,
                        'lastSucceededAt': None, 'status': 'active'}]}}}


class _FakeLoc:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    def is_visible(self):
        return True

    def scroll_into_view_if_needed(self):
        pass


class FakePage:
    """Minimal Playwright-locator surface backed by a real lxml DOM."""

    def __init__(self, html):
        self._doc = lxml.html.fromstring(html)

    def locator(self, sel):
        xp = sel[len('xpath='):] if sel.startswith('xpath=') else sel
        try:
            els = [e for e in self._doc.xpath(xp) if hasattr(e, 'tag')]
        except Exception:
            els = []
        return _FakeLoc(len(els))

    def frame_locator(self, sel):
        return self


class _StubResp:
    def __init__(self, payload):
        self.status_code = 200
        self._p = payload

    def json(self):
        return self._p


class StubGateway:
    """Stands in for the /heal worker: returns a fixed, valid proposal."""

    def __init__(self, proposal):
        self.proposal = proposal
        self.posts = []

    def post(self, url, *, json=None, headers=None, timeout=30):
        self.posts.append((url, json, headers))
        if url.endswith('/heal'):
            return _StubResp({'status': 'ok', **self.proposal})
        return _StubResp({'status': 'ok'})


def _resolver(store, page):
    # timeout_ms=0 => resolve() checks once then raises/returns immediately (no sleeping).
    return lambda: resolve(store, page, 'terms', timeout_ms=0)


def test_stale_selector_fails_before_healing(tmp_path):
    store = SelectorStore(seed(), str(tmp_path / 'o.json'))
    page = FakePage(HTML)
    with pytest.raises(AttentionRequired):
        resolve(store, page, 'terms', timeout_ms=0)


def test_self_heal_recovers_broken_selector_offline(tmp_path):
    store = SelectorStore(seed(), str(tmp_path / 'o.json'))
    page = FakePage(HTML)
    gw = StubGateway({'xpath': HEALED, 'fallbacks': []})
    coord = HealCoordinator(
        store, 'https://gw.example', 'download-key', http=gw,
        html_provider=lambda: HTML, inventory_provider=lambda: [], guard=lambda: True)

    loc = resolve_with_heal(_resolver(store, page), coord, 'terms',
                            store.meta('terms'),
                            {'url': 'https://x', 'stage': 'registration', 'version': '1'})

    # The healed selector genuinely matches the live DOM.
    assert loc is not None and loc.count() == 1
    # It was persisted to the store as a heal-sourced candidate.
    healed = [c['xpath'] for c in store.candidates('terms') if c.get('source') == 'heal']
    assert healed == [HEALED]
    # A canary is pending for the action outcome.
    assert coord.pending == {'terms': HEALED}
    # The gateway was actually called at /heal.
    assert any(u.endswith('/heal') for u, _, _ in gw.posts)


def test_heal_refused_for_destructive_key(tmp_path):
    data = seed()
    data['selectors']['submit'] = {
        'expectedRole': 'button', 'expectedText': ['Register', 'Registrieren'],
        'destructive': True, 'maxCandidates': 5, 'candidates': [
            {'xpath': "//button[@id='old-submit']", 'source': 'seed', 'status': 'active'}]}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    page = FakePage(HTML)
    gw = StubGateway({'xpath': HEALED, 'fallbacks': []})
    coord = HealCoordinator(
        store, 'https://gw.example', 'k', http=gw,
        html_provider=lambda: HTML, inventory_provider=lambda: [], guard=lambda: True)

    # A destructive control must never be auto-healed — it stays AttentionRequired.
    with pytest.raises(AttentionRequired):
        resolve_with_heal(lambda: resolve(store, page, 'submit', timeout_ms=0),
                          coord, 'submit', store.meta('submit'),
                          {'url': 'https://x'})
    assert not any(u.endswith('/heal') for u, _, _ in gw.posts)


def test_heal_blocked_while_guard_closed(tmp_path):
    store = SelectorStore(seed(), str(tmp_path / 'o.json'))
    page = FakePage(HTML)
    gw = StubGateway({'xpath': HEALED, 'fallbacks': []})
    coord = HealCoordinator(
        store, 'https://gw.example', 'k', http=gw,
        html_provider=lambda: HTML, inventory_provider=lambda: [], guard=lambda: False)
    with pytest.raises(AttentionRequired):
        resolve_with_heal(_resolver(store, page), coord, 'terms',
                          store.meta('terms'), {'url': 'https://x'})
    assert not gw.posts  # guard closed => no heal attempted


@pytest.mark.skipif(not os.environ.get('MENDWRIGHT_GATEWAY'),
                    reason='live worker not configured (set MENDWRIGHT_GATEWAY + MENDWRIGHT_KEY)')
def test_self_heal_recovers_against_live_worker(tmp_path):
    gateway = os.environ['MENDWRIGHT_GATEWAY']
    key = os.environ.get('MENDWRIGHT_KEY', '')
    auth_header = os.environ.get('MENDWRIGHT_AUTH_HEADER', 'X-Heal-Key')
    store = SelectorStore(seed(), str(tmp_path / 'o.json'))
    page = FakePage(HTML)
    coord = HealCoordinator(  # real urllib HTTP against the deployed worker
        store, gateway, key,
        html_provider=lambda: HTML,
        inventory_provider=lambda: [
            {'tag': 'input', 'type': 'checkbox', 'id': 'tc',
             'nearby': 'I accept the terms and conditions'}],
        guard=lambda: True, auth_header=auth_header)

    loc = resolve_with_heal(_resolver(store, page), coord, 'terms',
                            store.meta('terms'),
                            {'url': 'https://x', 'stage': 'registration', 'version': '1'})

    assert loc is not None and loc.count() == 1, 'healed selector should match the DOM'
    healed = [c['xpath'] for c in store.candidates('terms') if c.get('source') == 'heal']
    assert healed, 'the live worker proposed a selector that validated and was applied'
    print('LIVE heal applied:', healed[0])
