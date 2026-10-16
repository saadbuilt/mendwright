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


# --- lastUrl: the address saved for `mendwright check` -------------------------------------

NOTE = "<form><textarea id='note'></textarea></form>"


def _writes(healer):
    writes = []
    real = healer.store._write

    def write(payload):
        writes.append(payload)
        real(payload)
    healer.store._write = write
    return writes


def _note(healer):
    return healer.locator('note', '#note', role='textarea', timeout_ms=0, poll_ms=0)


def test_the_address_is_saved_with_the_page_in_one_write(tmp_path, make_page):
    page = make_page(NOTE, url='about:blank')
    healer = sh_open(page, store_path=str(tmp_path / 'o.json'))
    _note(healer)                               # no address on about:blank
    assert healer.store.last_url('note') is None
    writes = _writes(healer)
    page.url = 'https://u:p@Shop.Example/Orders/10482/?t=1#x'
    _note(healer)
    assert len(writes) == 1
    import json
    saved = json.loads((tmp_path / 'o.json').read_text(encoding='utf-8'))['selectors']['note']
    assert saved['lastUrl'] == 'https://shop.example/Orders/*/'
    assert saved['lastPage'] == '/orders/*' and saved['pages'] == ['/orders/*']
    # Another record of the same page: nothing new to save.
    page.url = 'https://shop.example/Orders/10483/'
    _note(healer)
    assert len(writes) == 1
    # Another page: one write for lastPage, pages and lastUrl together.
    page.url = 'https://shop.example/Help'
    _note(healer)
    assert len(writes) == 2
    saved = json.loads(writes[-1])['selectors']['note']
    assert saved['lastUrl'] == 'https://shop.example/Help'
    assert saved['pages'] == ['/help', '/orders/*']


def test_a_new_port_changes_the_address_in_memory_only(tmp_path, make_page):
    page = make_page(NOTE, url='http://127.0.0.1:8000/signup')
    healer = sh_open(page, store_path=str(tmp_path / 'o.json'))
    _note(healer)
    writes = _writes(healer)
    page.url = 'http://127.0.0.1:8123/signup'
    _note(healer)
    assert writes == []
    assert healer.store.last_url('note') == 'http://127.0.0.1:8123/signup'


def test_the_site_root_saves_no_address_over_a_page(tmp_path, make_page):
    # A header link found on /signup, then on the home page: lastPage stays /signup, so
    # lastUrl stays the address of /signup, and `mendwright check` loads it there.
    page = make_page(NOTE, url='https://shop.example/signup')
    healer = sh_open(page, store_path=str(tmp_path / 'o.json'))
    _note(healer)
    page.url = 'https://shop.example/'
    _note(healer)
    assert healer.store.meta('note')['lastPage'] == '/signup'
    assert healer.store.last_url('note') == 'https://shop.example/signup'
    # A key found only on the root gets the root's address.
    page = make_page(NOTE, url='https://shop.example/')
    healer = sh_open(page, store_path=str(tmp_path / 'r.json'))
    _note(healer)
    assert healer.store.last_url('note') == 'https://shop.example/'
    assert healer.store.meta('note')['lastPage'] is None


def test_a_healer_on_a_frame_saves_no_address(tmp_path, make_page):
    frame = make_page(NOTE, url='https://shop.example/embed/consent')
    del frame.frames                            # a Playwright Frame has no frames list
    healer = sh_open(frame, store_path=str(tmp_path / 'o.json'))
    _note(healer)
    assert healer.store.last_url('note') is None
    assert healer.store.meta('note')['lastPage'] == '/embed/consent'


def _selenium(url, framed):
    from mendwright.drivers.selenium import SeleniumDriver

    class Element:
        def is_displayed(self):
            return True

        def get_attribute(self, name):
            return '<textarea id="note"></textarea>'

    class WebDriver:
        def __init__(self):
            self.current_url = url
            self.switch_to = None

        def find_elements(self, by, value):
            return [Element()] if value == "//textarea[@id='note']" else []

        def execute_script(self, script, *args):
            return None

        page_source = '<form><textarea id="note"></textarea></form>'

    class Driver(SeleniumDriver):
        calls = 0

        def inside_frame(self):
            Driver.calls += 1
            return framed

    return Driver(WebDriver()), Driver


def test_selenium_saves_the_address_on_the_top_document_only(tmp_path):
    driver, cls = _selenium('https://shop.example/notes', framed=False)
    healer = sh_open(driver, store_path=str(tmp_path / 'o.json'))
    for _ in range(10):
        healer.locator('note', "//textarea[@id='note']", role='textarea', timeout_ms=0, poll_ms=0)
    assert healer.store.last_url('note') == 'https://shop.example/notes'
    assert cls.calls == 1                       # one round trip: the address was new once
    driver, cls = _selenium('https://shop.example/notes', framed=True)
    healer = sh_open(driver, store_path=str(tmp_path / 'f.json'))
    healer.locator('note', "//textarea[@id='note']", role='textarea', timeout_ms=0, poll_ms=0)
    assert healer.store.last_url('note') is None
    assert healer.store.meta('note')['lastPage'] == '/notes'


def test_a_store_without_set_last_url_still_works(tmp_path, make_page):
    from mendwright.store import SelectorStore, load_store

    class OldStore(SelectorStore):
        set_last_url = None
        last_url = None

    base = load_store(None, str(tmp_path / 'o.json'))
    store = OldStore(base._data, str(tmp_path / 'o.json'))
    healer = sh_open(make_page(NOTE, url='https://shop.example/notes'), store=store)
    _note(healer)
    assert store.meta('note')['lastPage'] == '/notes'
    assert 'lastUrl' not in (tmp_path / 'o.json').read_text(encoding='utf-8')
