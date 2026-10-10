"""build_bundle cleans the HTML the model sees, and redacts the inventory like the HTML.

Offline and deterministic: plain strings in, plain strings out. The gate still validates
against the full captured HTML; the last test runs the coordinator to confirm that the
model gets the cleaned page while apply() checks the proposal against the raw one.
"""

import lxml.html
import pytest

from mendwright import HealCoordinator, SelectorStore, build_bundle
from mendwright import capture
from mendwright import client as client_mod
from mendwright.capture import clean_html
from mendwright.prompt import build_prompt

META = {'expectedRole': 'button', 'expectedText': [], 'destructive': False}
TARGET = '<button id="buy-now" type="button">Buy now</button>'


def _noisy_page(noise_chars=150_000):
    """A page whose head and body are mostly inline script and CSS, with the target last."""
    style = '<style>' + '.c{color:red}' * (noise_chars // 26) + '</style>'
    script = '<script>' + 'var x = 1;' * (noise_chars // 20) + '</script>'
    svg = ('<svg aria-label="cart" viewBox="0 0 24 24">'
           + '<path d="M0 0L24 24Z"/>' * 200 + '</svg>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"><title>Shop</title>'
            '<link rel="stylesheet" href="/a.css">' + style + script + '</head><body>'
            + script + style + '<!-- ' + 'c' * 5000 + ' -->' + svg
            + '<noscript><p>enable JS</p></noscript><template><p>tpl</p></template>'
            + '<main>' + TARGET + '</main></body></html>')


def test_target_after_150k_chars_of_noise_reaches_the_prompt():
    page = _noisy_page()
    assert page.index(TARGET) > 150_000
    bundle = build_bundle('buy', META, [{'xpath': '//old'}], [], page, {})
    assert TARGET in bundle['html']
    # Truncation to 60,000 chars happens in prompt.py, so check what the model gets.
    assert TARGET in build_prompt(bundle)['user']
    assert len(bundle['html']) < 1_000


def test_noise_is_removed_and_structure_kept():
    out = build_bundle('buy', META, [], [], _noisy_page(2_000), {})['html']
    for gone in ('<script', '<style', '<noscript', '<template', '<!--', '<meta', '<link',
                 'var x', 'color:red', '<path', 'enable JS', 'tpl'):
        assert gone not in out, gone
    assert '<title>Shop</title>' in out
    assert '<svg aria-label="cart" viewbox="0 0 24 24"></svg>' in out.lower()
    assert '<main>' + TARGET + '</main>' in out


def test_text_after_a_removed_element_is_kept():
    html = '<label><script>track()</script>Accept the terms <input type="checkbox"></label>'
    assert clean_html(html) == '<label>Accept the terms <input type="checkbox"></label>'


def test_whitespace_runs_collapse_but_non_breaking_spaces_stay():
    html = '<div>\n\n   Pay now  \t <b> x </b>\n</div>'
    assert clean_html(html) == '<div> Pay now <b> x </b> </div>'


def test_attribute_values_are_not_touched():
    html = '<a class="btn  primary" href="/x">Go</a>'
    assert clean_html(html) == html


def test_body_elements_that_libxml2_puts_in_head_survive():
    # libxml2 parses '<script>..</script><button>' by putting the button in <head>.
    assert '<button>Go</button>' in clean_html('<script>x()</script><button>Go</button>')


def test_cleaned_html_parses_the_same_way_for_the_target():
    page = _noisy_page(2_000)
    xpath = "//button[@id='buy-now']"
    assert len(lxml.html.fromstring(clean_html(page)).xpath(xpath)) == 1
    assert len(lxml.html.fromstring(page).xpath(xpath)) == 1


@pytest.mark.parametrize('value', [None, '', '   ', 42])
def test_blank_or_non_string_html_is_returned_as_is(value):
    assert clean_html(value) == value


def test_parse_failure_falls_back_to_the_raw_string(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError('cannot parse')
    monkeypatch.setattr(capture.lxml.html, 'fromstring', boom)
    raw = '<div><script>x()</script>hi</div>'
    assert clean_html(raw) == raw
    # build_bundle still redacts the raw string.
    bundle = build_bundle('k', META, [], [], raw + '<input value="Tim">', {})
    assert bundle['html'] == raw + '<input>'


def test_xml_declaration_does_not_block_cleaning():
    out = clean_html('<?xml version="1.0" encoding="UTF-8"?>'
                     '<html><body><script>x</script><p>ok</p></body></html>')
    assert out == '<html><body><p>ok</p></body></html>'


# --- redaction still holds after cleaning ------------------------------------------------

ROW = {'secrets': {'password': 'Hunter2secret', 'address': 'Main Street 1\nBerlin'},
       'profile': {'first_name': 'Jürgen', 'company': 'Smith & Sons'}}


def test_secrets_are_redacted_whatever_form_they_take_in_the_page():
    html = ('<main><p>Jürgen</p><p>J&uuml;rgen</p><p>J&#252;rgen</p>'
            '<p>Main Street 1\nBerlin</p><p>Smith &amp; Sons</p>'
            '<input value="Hunter2secret"><span>Hunter2secret</span>'
            '<a href="https://evil.example/u?id=1">x</a></main>')
    out = build_bundle('k', META, [], [], html, {}, row=ROW)['html']
    for leaked in ('Jürgen', 'J&uuml;rgen', 'J&#252;rgen', 'Main Street 1', 'Berlin',
                   'Smith', 'Hunter2secret', 'https://evil.example'):
        assert leaked not in out, leaked
    assert out.count('[redacted]') >= 6
    # lxml 5.0 percent-encodes the marker inside href; newer versions keep it as is.
    assert '[URL]' in out or '%5BURL%5D' in out


def test_inventory_strings_get_the_same_redaction_as_the_html():
    inventory = [
        {'tag': 'input', 'name': 'email', 'value': 'tim@example.com',
         'html': '<input name="email" value="tim@example.com">', 'label': 'Hunter2secret'},
        {'tag': 'a', 'text': 'see https://x.example/p?q=1', 'attrs': {'Value': 'Jürgen'}},
        ('tuple', 'Smith &amp; Sons', 4111111111111111),
        {'text': 'card 4111111111111111', 'count': 3, 'visible': True},
    ]
    out = build_bundle('k', META, [], inventory, '', {}, row=ROW)['inventory']
    flat = repr(out)
    for leaked in ('tim@example.com', 'Hunter2secret', 'https://x.example', 'Jürgen',
                   'Smith', "'card 4111111111111111'"):
        assert leaked not in flat, leaked
    assert 'value' not in out[0] and 'Value' not in out[1]['attrs']  # filled values dropped
    assert out[0]['html'] == '<input name="email">'
    assert out[1]['text'] == 'see [URL]'
    assert out[2] == ['tuple', '[redacted]', 4111111111111111]  # non-strings pass through
    assert out[3] == {'text': 'card [redacted number]', 'count': 3, 'visible': True}


# --- the gate still sees the full page -----------------------------------------------------

def test_model_sees_cleaned_html_while_gate_checks_the_raw_page(tmp_path, monkeypatch):
    page = _noisy_page(2_000)
    seen = {}

    class Provider:
        def propose(self, bundle):
            seen['html'] = bundle['html']
            return {'xpath': "//button[@id='buy-now']", 'fallbacks': [], 'reasoning': 'id'}

        def report_outcome(self, key, xpath, success):
            pass

    gate_inputs = []
    real_validate = client_mod.validate

    def spy_validate(xpath, html, **kwargs):
        gate_inputs.append(html)
        return real_validate(xpath, html, **kwargs)

    monkeypatch.setattr(client_mod, 'validate', spy_validate)
    data = {'schema': 2, 'selectors': {'buy': {
        'expectedRole': 'button', 'expectedText': [], 'destructive': False,
        'candidates': [{'xpath': '//old', 'source': 'seed', 'status': 'active'}]}}}
    store = SelectorStore(data, str(tmp_path / 'o.json'))
    coord = HealCoordinator(store, provider=Provider(), html_provider=lambda: page,
                            inventory_provider=lambda: [], guard=lambda: True)
    # The target is a "Buy now" button, which the gate refuses by default; this test is
    # about what the gate reads, so the key opts out of that check.
    meta = dict(store.meta('buy'), refuseDestructiveTarget=False)
    applied = coord.try_heal('buy', meta, [], {})
    assert applied == "//button[@id='buy-now']"
    assert '<script' not in seen['html']
    assert gate_inputs == [page]  # the gate got the raw page, scripts and all


# --- the selectors and expected text get the same redaction -------------------------------

def test_row_values_in_selectors_and_expected_text_are_redacted():
    row = {'secrets': {'token': 's3cr3t-tok'}, 'profile': {'email': 'jane@example.com'}}
    xpath = "//a[@href='https://bank.example/u/4111111111111111?token=s3cr3t-tok']"
    meta = {'expectedRole': 'link', 'expectedText': ['jane@example.com 4111111111111111'],
            'destructive': False}
    b = build_bundle('user', meta, [{'xpath': "//td[text()='jane@example.com']"},
                                    {'xpath': xpath}, "//span[@id='s3cr3t-tok']"],
                     [], '<main></main>', {}, row=row)
    prompt = str(build_prompt(b))
    for leaked in ('jane@example.com', 's3cr3t-tok', '4111111111111111', 'https://bank.example'):
        assert leaked not in prompt
    assert b['candidates'] == ["//td[text()='[redacted]']", "//a[@href='[URL]']",
                               "//span[@id='[redacted]']"]
    assert b['expectedText'] == ['[redacted] [redacted number]']


def test_a_single_expected_text_string_is_kept_whole():
    b = build_bundle('k', {'expectedRole': 'button', 'expectedText': 'Save'}, [], [], '', {})
    assert b['expectedText'] == ['Save']
