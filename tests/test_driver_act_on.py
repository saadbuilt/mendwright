"""act_on(native, verb, ...): Playwright verbs pass straight through; on Selenium they are
mapped to WebElement calls, and bad calls fail before anything reaches the browser."""

import pytest

from mendwright.drivers import selenium as sel
from mendwright.drivers.playwright_sync import PlaywrightSyncDriver
from mendwright.drivers.selenium import FramedElement, SeleniumDriver

ENTER, TAB, SHIFT, CONTROL, NULL = '', '', '', '', ''


class El:
    """A WebElement stand-in that logs every call."""

    def __init__(self, selected=False, toggles=True, aria=None):
        self.log = []
        self._selected = selected
        self._toggles = toggles
        self._aria = aria

    def clear(self):
        self.log.append(('clear',))

    def send_keys(self, *keys):
        self.log.append(('send_keys',) + keys)

    def click(self):
        self.log.append(('click',))
        if self._toggles:
            self._selected = not self._selected

    def is_selected(self):
        return self._selected

    def get_attribute(self, name):
        if name == 'aria-checked':
            return self._aria
        return 'attr:' + name


class Drv:
    def __init__(self, found=None):
        self.found = found or {}
        self.switch_to = self
        self.log = []

    def find_elements(self, by, value):
        self.log.append(('find', value))
        return list(self.found.get(value, []))

    def frame(self, el):
        self.log.append(('frame',))

    def default_content(self):
        self.log.append(('top',))


def _act(verb, *args, el=None, **kwargs):
    el = el or El()
    SeleniumDriver(Drv()).act_on(el, verb, *args, **kwargs)
    return el.log


# --- Selenium mapping -------------------------------------------------------------------

def test_fill_clears_then_types():
    assert _act('fill', 'Ada') == [('clear',), ('send_keys', 'Ada')]
    assert _act('fill', value='Ada') == [('clear',), ('send_keys', 'Ada')]
    assert _act('fill', '') == [('clear',)]


def test_playwright_only_options_are_ignored_but_unknown_ones_are_not():
    assert _act('fill', 'x', timeout=1000, force=True) == [('clear',), ('send_keys', 'x')]
    el = El()
    with pytest.raises(TypeError) as info:
        SeleniumDriver(Drv()).act_on(el, 'fill', 'x', trial=True)
    assert 'trial=' in str(info.value)
    assert el.log == []


def test_check_and_uncheck_click_only_when_the_state_differs():
    assert _act('check', el=El(selected=False)) == [('click',)]
    assert _act('check', el=El(selected=True)) == []
    assert _act('uncheck', el=El(selected=True)) == [('click',)]
    assert _act('uncheck', el=El(selected=False)) == []
    assert _act('set_checked', True, el=El(selected=False)) == [('click',)]
    assert _act('set_checked', False, el=El(selected=False)) == []


def test_check_reads_aria_checked_for_custom_switches():
    assert _act('check', el=El(selected=False, aria='true')) == []


def test_check_raises_when_the_click_did_not_change_the_state():
    el = El(selected=False, toggles=False)
    with pytest.raises(RuntimeError) as info:
        SeleniumDriver(Drv()).act_on(el, 'check')
    assert 'did not change its checked state' in str(info.value)


class NoSuchElementException(Exception):
    pass


class FakeSelect:
    def __init__(self, el, multiple=False, values=('red', 'blue')):
        self.el = el
        self.is_multiple = multiple
        self.values = values
        self.log = []

    def deselect_all(self):
        self.log.append(('deselect_all',))

    def select_by_value(self, v):
        if v not in self.values:
            raise NoSuchElementException('Cannot locate option with value: ' + v)
        self.log.append(('value', v))

    def select_by_visible_text(self, t):
        self.log.append(('text', t))

    def select_by_index(self, i):
        self.log.append(('index', i))


@pytest.fixture
def fake_select(monkeypatch):
    made = []

    def make(el, **kw):
        s = FakeSelect(el, **kw)
        made.append(s)
        return s
    monkeypatch.setattr(sel, '_make_select', make)
    return made


def test_select_option_by_value_then_visible_text(fake_select):
    _act('select_option', 'blue')
    _act('select_option', 'Green')            # no such value -> visible text
    _act('select_option', label='Red')
    _act('select_option', index=1)
    _act('select_option', ['red', 'blue'])
    assert [s.log for s in fake_select] == [
        [('value', 'blue')], [('text', 'Green')], [('text', 'Red')], [('index', 1)],
        [('value', 'red'), ('value', 'blue')]]


def test_select_option_on_a_multi_select_replaces_the_selection(monkeypatch):
    made = []
    monkeypatch.setattr(sel, '_make_select', lambda el: made.append(FakeSelect(el, multiple=True)) or made[-1])
    _act('select_option', 'red')
    assert made[0].log == [('deselect_all',), ('value', 'red')]


def test_select_option_needs_something_to_select(fake_select):
    with pytest.raises(TypeError):
        _act('select_option')
    assert fake_select == []


def test_type_and_press():
    assert _act('type', 'abc') == [('send_keys', 'abc')]
    assert _act('press_sequentially', 'abc') == [('send_keys', 'abc')]
    assert _act('press', 'Enter') == [('send_keys', ENTER)]
    assert _act('press', 'a') == [('send_keys', 'a')]
    assert _act('press', 'Shift+Tab') == [('send_keys', SHIFT, TAB, NULL)]
    assert _act('press', 'Control+A') == [('send_keys', CONTROL, 'a', NULL)]
    assert _act('press', 'Control++') == [('send_keys', CONTROL, '+', NULL)]


def test_press_with_an_unknown_key_fails_before_the_browser():
    el = El()
    with pytest.raises(ValueError) as info:
        SeleniumDriver(Drv()).act_on(el, 'press', 'Bogus')
    assert "'Bogus'" in str(info.value) and "'Enter'" in str(info.value)
    assert el.log == []


def test_key_table_matches_selenium_keys():
    keys = pytest.importorskip('selenium.webdriver.common.keys').Keys
    for attr, code in sel._KEY_CODES.items():
        assert getattr(keys, attr) == code, attr


def test_hover_and_dblclick_use_action_chains(monkeypatch):
    calls = []

    class Chains:
        def __init__(self, drv):
            calls.append(('chains', drv))

        def move_to_element(self, el):
            calls.append(('move', el))
            return self

        def double_click(self, el):
            calls.append(('double', el))
            return self

        def perform(self):
            calls.append(('perform',))
    monkeypatch.setattr(sel, '_action_chains', Chains)
    drv, el = Drv(), El()
    d = SeleniumDriver(drv)
    d.act_on(el, 'hover')
    d.act_on(el, 'dblclick')
    assert calls == [('chains', drv), ('move', el), ('perform',),
                     ('chains', drv), ('double', el), ('perform',)]


def test_other_verbs_pass_through():
    el = El()
    d = SeleniumDriver(Drv())
    d.act_on(el, 'send_keys', 'x', 'y')
    d.act_on(el, 'click')
    assert el.log == [('send_keys', 'x', 'y'), ('click',)]
    assert d.act_on(el, 'get_attribute', 'href') == 'attr:href'


def test_unknown_verb_is_an_attribute_error_that_lists_the_mapped_verbs():
    el = El()
    with pytest.raises(AttributeError) as info:
        SeleniumDriver(Drv()).act_on(el, 'inner_text')
    msg = str(info.value)
    assert "'inner_text'" in msg and 'fill' in msg and 'select_option' in msg
    assert el.log == []


def test_act_uses_the_same_mapping():
    el = El()
    drv = Drv(found={'//in': [el]})
    SeleniumDriver(drv).act('//in', 'fill', 'Ada')
    assert el.log == [('clear',), ('send_keys', 'Ada')]


# --- Selenium mapping inside a frame ----------------------------------------------------

def _framed():
    target = El(selected=False)

    class FrameDrv(Drv):
        def __init__(self):
            super().__init__()
            self.inside = False

        def find_elements(self, by, value):
            self.log.append(('find', value))
            if value == '//iframe':
                return [El()]
            return [target] if (self.inside and value == '//cb') else []

        def frame(self, el):
            self.inside = True
            self.log.append(('frame',))

        def default_content(self):
            self.inside = False
            self.log.append(('top',))

    drv = FrameDrv()
    d = SeleniumDriver(drv)
    return drv, d, target, d.handle('//cb', frame_chain=('//iframe',))


def test_act_on_a_framed_element_runs_inside_the_frame():
    drv, d, target, framed = _framed()
    assert isinstance(framed, FramedElement)
    drv.log.clear()
    d.act_on(framed, 'check')
    assert target.log == [('click',)]
    assert drv.log[0] == ('find', '//iframe') and drv.log[-1] == ('top',)
    assert drv.inside is False


def test_unknown_verb_on_a_framed_element_fails_before_switching():
    drv, d, target, framed = _framed()
    drv.log.clear()
    with pytest.raises(AttributeError):
        d.act_on(framed, 'inner_text')
    assert drv.log == []


# --- Playwright -------------------------------------------------------------------------

class Loc:
    def __init__(self):
        self.calls = []

    def click(self, *args, **kwargs):
        self.calls.append(('click', args, kwargs))

    def select_option(self, *args, **kwargs):
        self.calls.append(('select_option', args, kwargs))
        return ['blue']


class Page:
    url = 'https://example.test/'

    def __init__(self, loc):
        self.loc = loc

    def locator(self, sel):
        return self.loc

    def frame_locator(self, sel):
        return self

    def content(self):
        return ''


def test_playwright_act_on_passes_args_and_kwargs_through():
    loc = Loc()
    d = PlaywrightSyncDriver(Page(loc))
    d.act_on(loc, 'click', timeout=2000, force=True)
    assert d.act_on(loc, 'select_option', 'blue', timeout=10) == ['blue']
    assert loc.calls == [('click', (), {'timeout': 2000, 'force': True}),
                         ('select_option', ('blue',), {'timeout': 10})]


def test_playwright_act_routes_through_act_on():
    loc = Loc()
    PlaywrightSyncDriver(Page(loc)).act('#go', 'click', timeout=5)
    assert loc.calls == [('click', (), {'timeout': 5})]


def test_playwright_unknown_verb_is_an_attribute_error():
    with pytest.raises(AttributeError):
        PlaywrightSyncDriver(Page(Loc())).act_on(Loc(), 'send_keys', 'x')
