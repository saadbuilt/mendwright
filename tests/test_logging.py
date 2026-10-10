"""mendwright logs through logging.getLogger('mendwright') instead of print()."""

import logging

import lxml.html

import mendwright
from mendwright import HealCoordinator

TERMS_HTML = ("<html><body><main><label><input type='checkbox'>"
              "<span>accept the terms</span></label></main></body></html>")
GOOD = "//label[contains(.,'accept the terms')]//input[@type='checkbox']"
META = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}


class FakeStore:
    def __init__(self):
        self.added = []

    def candidates(self, key):
        return [{'xpath': "//input[@id='old']"}]

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath))

    def quarantine(self, key, xpath):
        pass


class StubProvider:
    def __init__(self, xpath):
        self.xpath = xpath

    def propose(self, bundle):
        return {'xpath': self.xpath, 'fallbacks': [], 'reasoning': ''}

    def report_outcome(self, key, xpath, success):
        pass


class FakeClock:
    """Returns the given readings in order, then keeps returning the last one."""

    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0]


def _coord(provider, **kw):
    return HealCoordinator(FakeStore(), provider=provider, html_provider=lambda: TERMS_HTML,
                           inventory_provider=lambda: [], guard=lambda: True, **kw)


def test_the_package_adds_no_handler_of_its_own():
    # No NullHandler: with no logging configured, Python's last-resort handler prints a
    # WARNING to stderr, so a heal is never silent.
    assert logging.getLogger('mendwright').handlers == []


def test_applied_heal_logs_one_warning_line(caplog):
    coord = _coord(StubProvider(GOOD), clock=FakeClock(10.0, 11.4))
    with caplog.at_level(logging.INFO, logger='mendwright'):
        applied = coord.try_heal('terms', META, [{'xpath': "//input[@id='old']"}], {})
    assert applied == GOOD
    lines = [r.getMessage() for r in caplog.records if r.name == 'mendwright']
    assert lines == ["mendwright: healed 'terms': //input[@id='old'] -> " + GOOD
                     + " (StubProvider, 1.4s)"]
    assert caplog.records[-1].levelno == logging.WARNING


_HEAL_WITHOUT_LOGGING_SETUP = '''
import sys
sys.path.insert(0, sys.argv[1])
from mendwright import HealCoordinator


class Store:
    def candidates(self, key):
        return []

    def add_candidate(self, key, xpath, **kw):
        pass


class Provider:
    def propose(self, bundle):
        return {'xpath': sys.argv[2], 'fallbacks': []}

    def report_outcome(self, *a):
        pass


html = sys.argv[3]
coord = HealCoordinator(Store(), provider=Provider(), html_provider=lambda: html,
                        inventory_provider=lambda: [], guard=lambda: True)
assert coord.try_heal('terms', {'expectedRole': 'checkbox'}, [{'xpath': '//old'}], {})
'''


def test_a_heal_prints_one_line_to_stderr_with_no_logging_configured():
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parents[1])
    done = subprocess.run([sys.executable, '-c', _HEAL_WITHOUT_LOGGING_SETUP, root, GOOD,
                           TERMS_HTML], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout == ''
    lines = done.stderr.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("mendwright: healed 'terms': //old -> " + GOOD + ' (Provider, ')


def test_several_old_selectors_are_summarised(caplog):
    coord = _coord(StubProvider(GOOD), clock=FakeClock(0.0, 2.0))
    with caplog.at_level(logging.INFO, logger='mendwright'):
        coord.try_heal('terms', META, [{'xpath': '//a'}, {'xpath': '//b'}, {'xpath': '//c'}], {})
    assert "healed 'terms': //a (+2 more) -> " in caplog.records[-1].getMessage()


def test_declined_heal_logs_a_warning(caplog):
    coord = _coord(StubProvider(GOOD))
    meta = {'expectedRole': 'button', 'expectedText': ['Pay'], 'destructive': True}
    with caplog.at_level(logging.INFO, logger='mendwright'):
        assert not coord.try_heal('pay', meta, [], {})
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("mendwright: did not heal 'pay': ")
    assert 'destructive control' in warnings[0].getMessage()


def test_default_coordinator_no_longer_prints(capsys):
    coord = _coord(StubProvider(GOOD))
    coord.try_heal('pay', {'expectedRole': 'button', 'destructive': True}, [], {})
    assert capsys.readouterr().out == ''


def test_log_callable_still_receives_the_lines(caplog):
    lines = []
    coord = _coord(StubProvider(GOOD), log=lines.append, clock=FakeClock(0.0, 0.5))
    with caplog.at_level(logging.INFO, logger='mendwright'):
        coord.try_heal('terms', META, [{'xpath': '//old'}], {})
    assert lines == ["healed 'terms': //old -> " + GOOD + " (StubProvider, 0.5s)"]
    assert not [r for r in caplog.records if r.name == 'mendwright']  # log= replaces the logger


def test_broken_log_callable_does_not_break_a_heal():
    def boom(_message):
        raise RuntimeError('sink down')
    coord = _coord(StubProvider(GOOD), log=boom)
    assert coord.try_heal('terms', META, [], {}) == GOOD


class _Page:
    def __init__(self, html):
        self._doc = lxml.html.fromstring(html)
        self.url = 'https://example.test/'

    def locator(self, sel):
        return self

    def frame_locator(self, sel):
        return self

    def content(self):
        return lxml.html.tostring(self._doc, encoding='unicode')


def test_open_warns_once_when_no_provider_is_available(monkeypatch, tmp_path, caplog):
    import mendwright.providers as providers

    def no_provider():
        raise ValueError('no provider configured (set ANTHROPIC_API_KEY)')

    monkeypatch.setattr(providers, 'auto_provider', no_provider)
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        mendwright.open(_Page(TERMS_HTML), store_path=str(tmp_path / 'o.json'))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert 'no model provider is available' in warnings[0]
    assert 'ANTHROPIC_API_KEY' in warnings[0]


def test_without_a_provider_each_miss_logs_a_short_line(monkeypatch, tmp_path, caplog,
                                                        make_page):
    # open() logs the full list of provider variables once; each miss points back to it,
    # and the AttentionRequired still carries the full reason.
    import mendwright.providers as providers

    def no_provider():
        raise ValueError('no provider configured (set ANTHROPIC_API_KEY)')

    monkeypatch.setattr(providers, 'auto_provider', no_provider)
    heal = mendwright.open(make_page(TERMS_HTML), store_path=str(tmp_path / 'o.json'))
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        try:
            heal.locator('terms', "//input[@id='old']", role='checkbox', timeout_ms=0, poll_ms=0)
        except mendwright.AttentionRequired as exc:
            message = str(exc)
    assert 'ANTHROPIC_API_KEY' in message
    lines = [r.getMessage() for r in caplog.records if 'did not heal' in r.getMessage()]
    assert lines == ["mendwright: did not heal 'terms': no model provider is configured (see "
                     'the mendwright.open() warning, or run mendwright doctor)']


def test_a_missing_role_is_named_together_with_a_missing_provider(monkeypatch, tmp_path,
                                                                  make_page):
    import mendwright.providers as providers

    def no_provider():
        raise ValueError('no provider configured (set ANTHROPIC_API_KEY)')

    monkeypatch.setattr(providers, 'auto_provider', no_provider)
    heal = mendwright.open(make_page("<form><input id='e2'></form>"),
                           store_path=str(tmp_path / 'o.json'))
    try:
        heal.locator('email', '#email', timeout_ms=0, poll_ms=0)
    except mendwright.AttentionRequired as exc:
        message = str(exc)
    assert 'ANTHROPIC_API_KEY' in message
    assert "Also, mendwright does not know what kind of element 'email' is" in message
    assert "role='textbox' for a text field" in message
