"""Roles in the facade: read from the live element when role= is omitted, ARIA aliases,
unknown role names, and a clear stop when a heal is needed but the role is unknown."""

import logging

import lxml.html
import pytest

import mendwright
from mendwright import AttentionRequired, infer_role, load_store, migrate_schema1
from mendwright.roles import role_choices
from mendwright.validation import _role_of

PAGE = """<html><body><form>
<textarea id='bio'></textarea>
<select id='country'><option>NL</option></select>
<input type='radio' id='r1' name='r'>
<input type='checkbox' id='cb'>
<input type='text' id='name'>
<input type='submit' id='send' value='Send'>
<div role='button' id='fancy'>Fancy</div>
<button id='btn'>Save</button>
<span id='badge'>3</span>
<h1 id='title'>Hello</h1>
<ul><li id='item'>One</li></ul>
<a href='#' id='docs'>Docs</a>
</form></body></html>"""

# (selector as written in code, the element id, what the gate calls that element)
CASES = [
    ("//textarea[@id='bio']", 'bio', 'textarea'),
    ("//select[@id='country']", 'country', 'combobox'),
    ("//input[@id='r1']", 'r1', 'radio'),
    ("//input[@id='cb']", 'cb', 'checkbox'),
    ("//input[@id='name']", 'name', 'input'),
    ("//input[@id='send']", 'send', 'button'),
    ("//div[@id='fancy']", 'fancy', 'button'),
    ("//*[@id='btn']", 'btn', 'button'),
    ("//span[@id='badge']", 'badge', 'span'),
    ("//h1[@id='title']", 'title', 'h1'),
    ("//li[@id='item']", 'item', 'li'),
    ("//a[@id='docs']", 'docs', 'link'),
    ("#btn", 'btn', 'button'),
    ("css=#cb", 'cb', 'checkbox'),
]


def _moved(element_id):
    """The same page after a redesign renamed one element's id."""
    return PAGE.replace("id='" + element_id + "'", "id='" + element_id + "-v2'")


@pytest.mark.parametrize('selector,element_id,gate_role', CASES)
def test_role_comes_from_the_live_element_and_a_later_heal_passes(
        selector, element_id, gate_role, tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None), store_path=sp)
    healer.locator('k', selector, timeout_ms=0, poll_ms=0)
    assert healer.store.meta('k')['expectedRole'] == gate_role
    assert load_store(None, sp).meta('k')['expectedRole'] == gate_role

    # Next run: the id changed, and the model proposes the right element.
    fixed = "//*[@id='" + element_id + "-v2']"
    provider = stub_provider(fixed)
    healer = mendwright.open(make_page(_moved(element_id)), provider=provider, store_path=sp)
    loc = healer.locator('k', selector, timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert healer.coordinator.pending == {'k': fixed}


def test_the_role_is_read_once_and_saved(tmp_path, make_page, stub_provider):
    page = make_page(PAGE)
    healer = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    healer.locator('k', "//input[@id='cb']", timeout_ms=0, poll_ms=0)
    healer.locator('k', "//input[@id='cb']", timeout_ms=0, poll_ms=0)
    assert page.describes == 1


@pytest.mark.parametrize('alias,selector,expected', [
    ('textbox', "//input[@id='name']", 'input'),
    ('textbox', "//textarea[@id='bio']", 'textarea'),
    ('searchbox', "//input[@id='name']", 'input'),
    ('listbox', "//select[@id='country']", 'combobox'),
    ('select', "//select[@id='country']", 'combobox'),
    ('switch', "//input[@id='cb']", 'checkbox'),
    ('switch', "//div[@role='button']", 'button'),
    ('Button', "//button[@id='btn']", 'button'),
    ('a', "//a[@id='docs']", 'link'),
    ('listitem', "//li[@id='item']", 'li'),
])
def test_aria_aliases_map_to_the_gate_role(alias, selector, expected, tmp_path, make_page,
                                           stub_provider):
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    healer.locator('k', selector, role=alias, timeout_ms=0, poll_ms=0)
    assert healer.store.meta('k')['expectedRole'] == expected


ARIA_WIDGETS = """<html><body><form>
<div id='ed' role='textbox' contenteditable='true'></div>
<div id='sw' role='switch' aria-checked='false'>Dark mode</div>
<ul id='lb' role='listbox'><li role='option'>NL</li></ul>
</form></body></html>"""


@pytest.mark.parametrize('alias, element_id, gate_role', [
    ('textbox', 'ed', 'input'), ('switch', 'sw', 'checkbox'), ('listbox', 'lb', 'combobox'),
    (None, 'ed', 'input'), (None, 'sw', 'checkbox'), (None, 'lb', 'combobox'),
])
def test_aria_widgets_heal_with_their_alias_or_with_role_left_out(
        alias, element_id, gate_role, tmp_path, make_page, stub_provider):
    sp = str(tmp_path / 'o.json')
    selector = "//*[@id='" + element_id + "']"
    healer = mendwright.open(make_page(ARIA_WIDGETS), provider=stub_provider(None),
                             store_path=sp)
    healer.locator('k', selector, role=alias, timeout_ms=0, poll_ms=0)
    assert healer.store.meta('k')['expectedRole'] == gate_role

    # Next run: the id changed. One model call, and the heal passes the role check.
    fixed = "//*[@id='" + element_id + "-v2']"
    moved = ARIA_WIDGETS.replace("id='" + element_id + "'", "id='" + element_id + "-v2'")
    provider = stub_provider(fixed)
    healer = mendwright.open(make_page(moved), provider=provider, store_path=sp)
    healer.locator('k', selector, role=alias, timeout_ms=0, poll_ms=0)
    assert healer.coordinator.pending == {'k': fixed}
    assert len(provider.bundles) == 1


def test_a_native_control_keeps_its_role_under_an_aria_role():
    html = ("<div><button role='switch'>b</button><textarea role='textbox'></textarea>"
            "<input type='checkbox' role='switch'></div>")
    doc = lxml.html.fromstring(html)
    assert [_role_of(el) for el in doc] == ['button', 'textarea', 'checkbox']
    assert infer_role("//div[@role='switch']") == 'checkbox'
    assert infer_role("//*[@role='textbox']") == 'input'


def test_an_alias_the_element_does_not_match_logs_a_warning(tmp_path, make_page, stub_provider,
                                                            caplog):
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        healer.locator('k', "//span[@id='badge']", role='textbox', timeout_ms=0, poll_ms=0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "role= for 'k' means one of input/textarea" in warnings[0]
    assert "is a 'span'" in warnings[0]
    assert healer.store.meta('k')['expectedRole'] == 'input'


def test_an_unknown_role_is_a_value_error_naming_the_valid_ones(tmp_path, make_page,
                                                                stub_provider):
    page = make_page(PAGE)
    healer = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    with pytest.raises(ValueError) as info:
        healer.locator('k', "//button[@id='btn']", role='buton')
    message = str(info.value)
    assert "unknown role 'buton'" in message
    for valid in ('button', 'checkbox', 'radio', 'link', 'input', 'combobox', 'textbox'):
        assert valid in message
    assert page.queries == []                  # rejected before touching the page


@pytest.mark.parametrize('role', ['dml-checkbox', 'textarea', 'div', 'main', 'h3'])
def test_tag_names_and_custom_elements_are_accepted(role):
    assert role_choices(role) == (role,)


def test_a_heal_with_an_unknown_role_asks_for_role(tmp_path, make_page, stub_provider):
    provider = stub_provider("//button[@id='btn']")
    healer = mendwright.open(make_page(PAGE), provider=provider, store_path=str(tmp_path / 'o.json'))
    with pytest.raises(AttentionRequired) as info:
        healer.locator('k', "//*[@id='gone']", timeout_ms=0, poll_ms=0)
    assert 'role=' in str(info.value)
    assert 'expected role is unknown' in str(info.value)
    assert provider.bundles == []              # no model call that could only fail


def test_a_first_call_heal_uses_the_role_guessed_from_the_selector(tmp_path, make_page,
                                                                   stub_provider):
    provider = stub_provider("//select[@id='country']")
    healer = mendwright.open(make_page(PAGE), provider=provider, store_path=str(tmp_path / 'o.json'))
    loc = healer.locator('k', "//select[@id='gone']", timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert provider.bundles[0]['expectedRole'] == 'combobox'


def test_a_hidden_checkbox_resolves_on_the_first_call(tmp_path, make_page, stub_provider):
    page = make_page("<label><input type='checkbox' id='t' style='display:none'>Terms</label>")
    healer = mendwright.open(page, provider=stub_provider(None), store_path=str(tmp_path / 'o.json'))
    loc = healer.locator('t', "//input[@type='checkbox']", timeout_ms=0, poll_ms=0)
    assert loc.count() == 1
    assert healer.store.meta('t')['expectedRole'] == 'checkbox'


def test_a_user_role_that_disagrees_with_the_page_is_warned_about_once(tmp_path, make_page,
                                                                       stub_provider, caplog):
    healer = mendwright.open(make_page(PAGE), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    with caplog.at_level(logging.WARNING, logger='mendwright'):
        healer.locator('k', "//input[@id='cb']", role='input', timeout_ms=0, poll_ms=0)
        healer.locator('k', "//input[@id='cb']", role='input', timeout_ms=0, poll_ms=0)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Pass role='checkbox'" in warnings[0]
    assert healer.store.meta('k')['expectedRole'] == 'input'   # the user's role is kept


class NoDescribeDriver:
    """A custom driver with only the required methods."""

    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def handle(self, selector, frame_chain=()):
        return object()

    def url(self):
        return ''


def test_without_describe_the_guess_from_the_selector_is_saved(tmp_path, stub_provider):
    healer = mendwright.open(NoDescribeDriver(), provider=stub_provider(None),
                             store_path=str(tmp_path / 'o.json'))
    healer.locator('k', "//select[@id='c']", timeout_ms=0, poll_ms=0)
    assert load_store(None, str(tmp_path / 'o.json')).meta('k')[
        'expectedRole'] == 'combobox'


# --- infer_role agrees with the gate ------------------------------------------------

@pytest.mark.parametrize('selector', [
    "//textarea", "//select", "//input[@type='radio']", "//input[@type='checkbox']",
    "//input[@type='submit']", "//input[@type='text']", "//button",
    "//div[@role='button']", "//div[@role='combobox']", "//span", "//h1", "//li", "//a",
    "//main//label/input[@type='checkbox']", "(//button)[1]",
    "//label[contains(normalize-space(.),'accept')]//input[@type='checkbox']",
    "//*[@role='checkbox']", "//*[@type='checkbox']",
])
def test_infer_role_matches_the_gate(selector):
    html = ("<div><textarea></textarea><select></select><input type='radio'>"
            "<input type='checkbox'><input type='submit'><input type='text'><input>"
            "<button></button><div role='button'></div><div role='combobox'></div><span></span>"
            "<h1></h1><li></li><a></a><main><label>accept<input type='checkbox'></label></main>"
            "<i role='checkbox'></i></div>")
    doc = lxml.html.fromstring(html)
    element = doc.xpath(selector)[0]
    assert infer_role(selector) == _role_of(element)


@pytest.mark.parametrize('selector', ["//*[@id='x']", "//a | //b", "#x", "text=Save",
                                      "//label[.//input]/following-sibling::*"])
def test_infer_role_says_unknown_when_it_cannot_tell(selector):
    assert infer_role(selector) == 'unknown'


def test_schema1_migration_uses_the_gate_vocabulary():
    out = migrate_schema1({'selectors': [{'key': 'c', 'xpath': "//select[@id='c']"},
                                         {'key': 'b', 'xpath': "//textarea"}]})
    assert out['selectors']['c']['expectedRole'] == 'combobox'
    assert out['selectors']['b']['expectedRole'] == 'textarea'
