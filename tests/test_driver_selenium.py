"""SeleniumDriver: duck-typed detection, delegation, and STATEFUL frame handling.

Fully offline + deterministic: a hand-rolled fake WebDriver stands in for a real browser
(find_elements / switch_to / current_url / page_source / execute_script). No real network,
no API key, no real browser, no Date.now/random. The fake models the stateful frame context
(switch_to.frame pushes a frame's document; default_content pops back to the top) and records
every switch so the tests can assert default_content() is always restored.
"""

import pytest

from mendwright.drivers.selenium import FramedElement, SeleniumDriver, detect, register


class FakeElement:
    """A WebElement stand-in. When used as a frame, `frame_doc` is the xpath->[elements]
    map that becomes the active document after switch_to.frame(this)."""

    def __init__(self, visible=True, frame_doc=None, outer='<el></el>'):
        self._visible = visible
        self.frame_doc = frame_doc or {}
        self._outer = outer
        self.clicked = False
        self.sent = None

    def is_displayed(self):
        return self._visible

    def click(self):
        self.clicked = True

    def send_keys(self, *args):
        self.sent = args

    def get_attribute(self, name):
        return self._outer if name == 'outerHTML' else None


class FakeSwitchTo:
    def __init__(self, driver):
        self._driver = driver

    def frame(self, element):
        self._driver.frame_calls.append(element)
        self._driver._stack.append(element.frame_doc)  # enter the frame's document

    def default_content(self):
        self._driver.default_content_calls += 1
        del self._driver._stack[1:]  # back to the top-level document


class FakeWebDriver:
    """Selenium-like: find_elements(by, xpath) resolves against the ACTIVE document (which
    changes as frames are entered/left), plus switch_to / current_url / page_source."""

    def __init__(self, root, url='https://example.test/x', raise_on=None):
        self._stack = [root]                 # stack of documents; [-1] is active
        self.switch_to = FakeSwitchTo(self)
        self.current_url = url
        self._raise_on = raise_on            # an xpath whose lookup raises (op-fails-in-frame)
        self.frame_calls = []
        self.default_content_calls = 0
        self.by_used = None
        self.scripts = []

    def find_elements(self, by, xpath):
        self.by_used = by
        if xpath == self._raise_on:
            raise RuntimeError('boom locating ' + xpath)
        return list(self._stack[-1].get(xpath, []))

    def execute_script(self, script, *args):
        self.scripts.append((script, args))

    @property
    def page_source(self):
        return '<html>root</html>'


# --- detection (duck-typed, never module name) ---------------------------------------

def test_detect_identifies_driver_not_element_or_page():
    assert detect(FakeWebDriver({})) is True

    class FakeWebElement:                     # has find_elements but NO switch_to
        def find_elements(self, by, xpath):
            return []

    assert detect(FakeWebElement()) is False

    class PwPage:                             # Playwright surface: neither method
        def locator(self, sel):
            ...

        def frame_locator(self, sel):
            ...

    assert detect(PwPage()) is False


# --- top-level delegation -------------------------------------------------------------

def test_count_visible_handle_top_level():
    el = FakeElement(visible=True)
    drv = FakeWebDriver({'//x': [el]})
    d = SeleniumDriver(drv)

    assert d.count('//x') == 1
    assert d.count('//missing') == 0
    assert d.is_visible('//x') is True
    assert d.is_visible('//missing') is False
    assert d.handle('//x') is el             # native WebElement returned for the caller to act on

    assert drv.by_used == 'xpath'            # By.XPATH, lazily resolved (or its literal fallback)
    assert drv.default_content_calls == 0    # no frame_chain -> never touched the frame context


def test_act_invokes_verb_on_native_element():
    el = FakeElement()
    d = SeleniumDriver(FakeWebDriver({'//btn': [el]}))
    d.act('//btn', 'click')
    assert el.clicked is True


def test_act_passes_args_to_verb():
    el = FakeElement()
    d = SeleniumDriver(FakeWebDriver({'//in': [el]}))
    d.act('//in', 'send_keys', 'hello')
    assert el.sent == ('hello',)


def test_scroll_into_view_runs_script_without_raising():
    el = FakeElement()
    drv = FakeWebDriver({'//x': [el]})
    SeleniumDriver(drv).scroll_into_view('//x')
    assert len(drv.scripts) == 1 and drv.scripts[0][1] == (el,)
    SeleniumDriver(drv).scroll_into_view('//missing')  # no element -> no script, no raise
    assert len(drv.scripts) == 1


def test_url_and_outer_html():
    drv = FakeWebDriver({'//x': [FakeElement(outer='<x>1</x>')]})
    d = SeleniumDriver(drv)
    assert d.url() == 'https://example.test/x'
    assert d.outer_html() == '<html>root</html>'
    assert d.outer_html('//x') == '<x>1</x>'
    assert d.outer_html('//missing') == ''
    assert d.inventory() == []


# --- stateful frame handling ----------------------------------------------------------

def _framed(raise_on=None):
    """A driver whose root holds an //iframe whose document holds //btn (a visible el)."""
    target = FakeElement(visible=True)
    frame_el = FakeElement(frame_doc={'//btn': [target]})
    drv = FakeWebDriver({'//iframe': [frame_el]}, raise_on=raise_on)
    return drv, frame_el, target


def test_frame_chain_switches_in_and_restores():
    drv, frame_el, target = _framed()
    d = SeleniumDriver(drv)

    assert d.count('//btn', frame_chain=('//iframe',)) == 1
    assert d.is_visible('//btn', frame_chain=('//iframe',)) is True
    # This used to assert `is target`: the raw element, returned after switching back to
    # the top document, where a real WebElement from the frame no longer works. handle()
    # now returns a FramedElement that switches in for each use.
    framed = d.handle('//btn', frame_chain=('//iframe',))
    assert isinstance(framed, FramedElement)
    assert framed.selector == '//btn' and framed.frame_chain == ('//iframe',)

    # Each op entered the frame and then restored the top document.
    assert drv.frame_calls == [frame_el, frame_el, frame_el]
    assert drv.default_content_calls == 3

    d.act('//btn', 'click', frame_chain=('//iframe',))
    assert target.clicked is True
    assert drv.default_content_calls == 4    # act restores too


def test_default_content_restored_when_op_raises_inside_frame():
    # The op (locating //boom) raises AFTER we've switched into //iframe; the finally must
    # still put us back at the top document, and the exception must propagate.
    drv, frame_el, _ = _framed(raise_on='//boom')
    d = SeleniumDriver(drv)

    with pytest.raises(RuntimeError):
        d.act('//boom', 'click', frame_chain=('//iframe',))

    assert drv.frame_calls == [frame_el]     # we did enter the frame
    assert drv.default_content_calls == 1    # ...and were restored despite the raise


def test_read_methods_swallow_frame_errors_to_safe_defaults():
    # A missing frame makes the read methods fall back to their safe default (not raise),
    # while still never leaving the driver stranded in a frame.
    drv = FakeWebDriver({})                  # no //iframe at all
    d = SeleniumDriver(drv)
    assert d.count('//btn', frame_chain=('//iframe',)) == 0
    assert d.is_visible('//btn', frame_chain=('//iframe',)) is False
    assert drv.default_content_calls == 0    # never entered a frame -> nothing to restore


# --- registry wiring ------------------------------------------------------------------

def test_register_then_wrap_builds_selenium_driver():
    from mendwright import wrap

    register()  # what mendwright/drivers/__init__.py must do (orchestrator wires the import)
    drv = FakeWebDriver({'//x': [FakeElement()]})
    d = wrap(drv)
    assert isinstance(d, SeleniumDriver)
    assert d.count('//x') == 1


def test_click_and_clear_ignore_playwright_only_options():
    from mendwright.drivers.selenium import selenium_verb

    class El:
        def __init__(self):
            self.calls = []

        def click(self):
            self.calls.append('click')

        def clear(self):
            self.calls.append('clear')

        def send_keys(self, *keys):
            self.calls.append(('send_keys', keys))

    el = El()
    selenium_verb('click', (), {'timeout': 2000, 'force': True})(None, el)
    selenium_verb('clear', (), {'timeout': 1})(None, el)
    selenium_verb('send_keys', ('x',), {'timeout': 1})(None, el)
    assert el.calls == ['click', 'clear', ('send_keys', ('x',))]
