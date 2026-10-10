"""SeleniumDriver: probes ignore the user's implicit wait, and frame candidates come back
as a FramedElement that works after the driver has switched back to the top document."""

import pytest

from mendwright.drivers.selenium import FramedElement, SeleniumDriver


class Timeouts:
    def __init__(self, drv):
        self._drv = drv

    @property
    def implicit_wait(self):
        self._drv.log.append('get')
        return self._drv.implicit


class Switch:
    def __init__(self, drv):
        self._drv = drv

    def frame(self, el):
        self._drv.log.append('frame')
        self._drv.stack.append(el.doc)

    def default_content(self):
        self._drv.log.append('top')
        del self._drv.stack[1:]


class StaleError(Exception):
    pass


class StrictElement:
    """Only usable while the driver's active document is the one it was found in, like a
    real WebElement found inside an iframe."""

    def __init__(self, drv, doc_name, doc=None):
        self._drv = drv
        self._home = doc_name
        self.doc = doc or {}
        self.clicks = 0
        self.keys = []

    def _check(self):
        if self._drv.active_name() != self._home:
            raise StaleError('no such element: element not found')

    def click(self):
        self._check()
        self.clicks += 1

    def send_keys(self, *keys):
        self._check()
        self.keys.append(keys)

    @property
    def text(self):
        self._check()
        return 'I accept'

    def is_displayed(self):
        return True


class Driver:
    def __init__(self, implicit=None):
        self.log = []
        self.implicit = implicit
        if implicit is not None:
            self.timeouts = Timeouts(self)
        self.switch_to = Switch(self)
        self.top = {'__name__': 'top'}
        self.stack = [self.top]

    def active_name(self):
        return self.stack[-1]['__name__']

    def implicitly_wait(self, seconds):
        self.log.append(('wait', seconds))
        self.implicit = seconds

    def find_elements(self, by, value):
        self.log.append(('find', value, self.implicit))
        return list(self.stack[-1].get(value, []))


def _with_frame():
    drv = Driver()
    inner = {'__name__': 'consent'}
    target = StrictElement(drv, 'consent')
    inner['//input'] = [target]
    frame = StrictElement(drv, 'top', doc=inner)
    drv.top['//iframe'] = [frame]
    return drv, target


# --- implicit wait ----------------------------------------------------------------------

def test_probes_run_with_implicit_wait_zero_and_restore_it():
    drv = Driver(implicit=10)
    d = SeleniumDriver(drv)
    assert d.count('//missing') == 0
    assert drv.log == ['get', ('wait', 0), ('find', '//missing', 0), ('wait', 10)]
    assert drv.implicit == 10


def test_implicit_wait_is_restored_when_the_probe_fails():
    drv = Driver(implicit=5)

    def boom(by, value):
        raise RuntimeError('session gone')
    drv.find_elements = boom
    assert SeleniumDriver(drv).is_visible('//x') is False
    assert drv.implicit == 5


def test_zero_or_unreadable_implicit_wait_is_left_alone():
    drv = Driver(implicit=0)
    SeleniumDriver(drv).count('//x')
    assert ('wait', 0) not in drv.log
    drv = Driver(implicit=None)                # no .timeouts at all (fake / Selenium 3)
    SeleniumDriver(drv).count('//x')
    assert not any(isinstance(e, tuple) and e[0] == 'wait' for e in drv.log)


def test_actions_keep_the_users_implicit_wait():
    drv = Driver(implicit=3)
    el = StrictElement(drv, 'top')
    drv.top['//b'] = [el]
    SeleniumDriver(drv).act('//b', 'click')
    assert ('find', '//b', 3) in drv.log
    assert ('wait', 0) not in drv.log


# --- frame-aware handle -----------------------------------------------------------------

def test_raw_element_from_a_frame_fails_at_the_top_document():
    # The bug this replaces: the old handle() returned the raw element after switching back.
    drv, target = _with_frame()
    with pytest.raises(StaleError):
        target.click()


def test_framed_handle_switches_in_for_each_call_and_restores():
    drv, target = _with_frame()
    d = SeleniumDriver(drv)
    framed = d.handle('//input', frame_chain=('//iframe',))
    assert isinstance(framed, FramedElement)
    assert drv.active_name() == 'top'

    framed.click()
    framed.send_keys('x', 'y')
    assert target.clicks == 1 and target.keys == [('x', 'y')]
    assert framed.text == 'I accept'          # property reads switch in too
    assert drv.active_name() == 'top'


def test_framed_handle_restores_when_the_action_raises():
    drv, target = _with_frame()
    framed = SeleniumDriver(drv).handle('//input', frame_chain=('//iframe',))

    def broken():
        raise RuntimeError('element not interactable')
    target.click = broken
    with pytest.raises(RuntimeError):
        framed.click()
    assert drv.active_name() == 'top'


def test_framed_handle_raises_when_the_element_is_gone():
    drv, target = _with_frame()
    framed = SeleniumDriver(drv).handle('//input', frame_chain=('//iframe',))
    drv.top['//iframe'][0].doc['//input'] = []
    with pytest.raises(Exception) as info:
        framed.click()
    assert 'no element matches' in str(info.value)
    assert drv.active_name() == 'top'


def test_in_frame_runs_a_function_against_the_live_element():
    drv, target = _with_frame()
    framed = SeleniumDriver(drv).handle('//input', frame_chain=('//iframe',))
    assert framed.in_frame(lambda el: el is target) is True
    assert 'FramedElement' in repr(framed) and '//iframe' in repr(framed)


def test_top_level_handle_is_still_the_raw_element():
    drv, target = _with_frame()
    el = StrictElement(drv, 'top')
    drv.top['//b'] = [el]
    assert SeleniumDriver(drv).handle('//b') is el
