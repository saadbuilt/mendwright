"""Zero-config facade: mendwright.open(page).locator(...) — empty-store inline seed, heal
through the facade, and the canary via expect=."""

import lxml.html

from mendwright import open as sh_open

HTML = '<form><input type="checkbox" id="tc"><button>Go</button></form>'


class FakeLoc:
    def __init__(self, n, els=()):
        self._n = n
        self._els = list(els)
        self.checked = False
        self.clicked = False
    def count(self):
        return self._n
    def evaluate_all(self, js):
        # describe(): what a browser returns for exactly one match, else null.
        if len(self._els) != 1:
            return None
        el = self._els[0]
        return {'tag': el.tag.lower(),
                'html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}
    def is_visible(self):
        return True
    def scroll_into_view_if_needed(self):
        pass
    def check(self):
        self.checked = True
    def click(self):
        self.clicked = True


class FakePage:
    """lxml-backed Playwright-ish page: locator count reflects the real DOM."""
    def __init__(self, html):
        self._doc = lxml.html.fromstring(html)
        self.url = 'https://example.test/signup'
    def locator(self, sel):
        xp = sel[len('xpath='):] if sel.startswith('xpath=') else sel
        try:
            els = [e for e in self._doc.xpath(xp) if hasattr(e, 'tag')]
        except Exception:
            els = []
        return FakeLoc(len(els), els)
    def frame_locator(self, sel):
        return self
    def content(self):
        return lxml.html.tostring(self._doc, encoding='unicode')


class DirectProvider:
    def __init__(self, xpath):
        self.xpath = xpath
        self.outcomes = []
    def propose(self, bundle):
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': 'facade-heal'}
    def report_outcome(self, key, xpath, success):
        self.outcomes.append((key, xpath, success))


def test_empty_store_quickstart_resolves_inline_seed(tmp_path):
    healer = sh_open(FakePage(HTML), store_path=str(tmp_path / 'o.json'))
    loc = healer.locator('terms', "//input[@id='tc']", role='checkbox', timeout_ms=0, poll_ms=0)
    assert loc.count() == 1          # first call on an empty store just works (inline seed)
    loc.check()                      # action forwards to the native locator


def test_facade_heals_a_broken_inline_selector(tmp_path):
    prov = DirectProvider("//input[@type='checkbox']")
    healer = sh_open(FakePage(HTML), provider=prov, store_path=str(tmp_path / 'o.json'))
    # The inline selector is already broken (id GONE); the facade heals + retries inline.
    loc = healer.locator('terms', "//input[@id='GONE']", role='checkbox', timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert "//input[@type='checkbox']" in [c['xpath'] for c in healer.store.candidates('terms')]


def test_facade_canary_routes_through_provider_after_heal(tmp_path):
    # A real heal sets pending; expect=True must route the confirmation through the provider
    # and clear the canary. (The old version used no provider, so no heal happened and the
    # assertion was vacuous.)
    prov = DirectProvider("//input[@type='checkbox']")
    healer = sh_open(FakePage(HTML), provider=prov, store_path=str(tmp_path / 'o.json'))
    loc = healer.locator('terms', "//input[@id='GONE']", role='checkbox', timeout_ms=0, poll_ms=0)
    loc.check(expect=lambda: True)
    assert prov.outcomes and prov.outcomes[-1] == ('terms', "//input[@type='checkbox']", True)
    assert 'terms' not in healer.coordinator.pending   # canary consumed


def test_facade_canary_quarantines_on_failed_expect(tmp_path):
    prov = DirectProvider("//input[@type='checkbox']")
    healer = sh_open(FakePage(HTML), provider=prov, store_path=str(tmp_path / 'o.json'))
    loc = healer.locator('terms', "//input[@id='GONE']", role='checkbox', timeout_ms=0, poll_ms=0)
    loc.check(expect=lambda: False)   # postcondition fails -> quarantine the healed candidate
    assert prov.outcomes[-1] == ('terms', "//input[@type='checkbox']", False)
    assert "//input[@type='checkbox']" not in [c['xpath'] for c in healer.store.candidates('terms')]


def test_facade_never_downgrades_a_destructive_key(tmp_path):
    # A later locator() that passes role/text must NOT silently clear an existing
    # destructive flag (which would make a pay/submit control auto-healable). Fail-closed.
    healer = sh_open(FakePage(HTML), store_path=str(tmp_path / 'o.json'))
    healer.store.set_meta('pay', role='button', destructive=True)
    healer.store.add_candidate('pay', '//button', source='code')
    assert healer.store.meta('pay')['destructive'] is True
    healer.locator('pay', role='button', timeout_ms=0, poll_ms=0)   # role given, destructive omitted
    assert healer.store.meta('pay')['destructive'] is True          # still protected
