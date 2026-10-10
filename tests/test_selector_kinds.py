"""mendwright.selectors: classify / strip_prefix / normalize / check_syntax."""

import pytest

import mendwright
from mendwright.selectors import (
    InvalidSelector,
    check_syntax,
    classify,
    normalize,
    strip_prefix,
)


@pytest.mark.parametrize('selector, kind', [
    ("//input[@id='a']", 'xpath'),
    ("xpath=//input[@id='a']", 'xpath'),
    ('xpath=html/body', 'xpath'),
    ('(//button)[2]', 'xpath'),
    ('./span', 'xpath'),
    ('..', 'xpath'),
    ('/html/body', 'xpath'),
    ('  //a  ', 'xpath'),
    ('#save', 'css'),
    ('form button.primary', 'css'),
    ('.card > a', 'css'),
    ("input[name='q']", 'css'),
    ('css=#save', 'css'),
    ('css=/odd', 'css'),
    ('text=Save', 'engine'),
    ("role=button[name='Save']", 'engine'),
    ('id=save', 'engine'),
    ('data-testid=submit', 'engine'),
    ('internal:role=button', 'engine'),
    ('nth=0', 'engine'),
    ('label=Email', 'engine'),
    ('placeholder=Search', 'engine'),
    ('alt=Logo', 'engine'),
    ('title=Help', 'engine'),
    ('_react=Button', 'engine'),
    ('//form >> text=Save', 'engine'),
    ('css=div >> nth=1', 'engine'),
])
def test_classify(selector, kind):
    assert classify(selector) == kind


def test_chain_marker_inside_quotes_or_brackets_is_not_a_chain():
    assert classify("//td[text()=' >> ']") == 'xpath'
    assert classify('a[title=" >> "]') == 'css'


def test_strip_prefix_and_normalize():
    assert strip_prefix("xpath=//a[@id='x']") == "//a[@id='x']"
    assert strip_prefix('css=#save') == '#save'
    assert strip_prefix('  #save ') == '#save'
    assert strip_prefix('text=Save') == 'text=Save'
    assert normalize("xpath=//a") == '//a'
    assert normalize('css=#save') == '#save'
    # Dropping the prefix would change the kind, so it stays.
    assert normalize('xpath=html/body') == 'xpath=html/body'
    assert normalize('css=/odd') == 'css=/odd'
    assert normalize(' text=Save ') == 'text=Save'


def test_check_syntax_returns_the_kind():
    assert check_syntax("//button[@id='go']") == 'xpath'
    assert check_syntax('#go') == 'css'
    assert check_syntax("role=button[name='Go']") == 'engine'


def test_check_syntax_rejects_bad_xpath_with_a_clear_message():
    with pytest.raises(InvalidSelector) as info:
        check_syntax('//div[')
    err = info.value
    assert isinstance(err, ValueError)
    assert err.selector == '//div[' and err.kind == 'xpath'
    assert 'not valid XPath' in str(err)
    assert 'mendwright.selectors.check_syntax' in str(err)


@pytest.mark.parametrize('selector', ['', '   ', 'xpath=', 'css=', 'text='])
def test_check_syntax_rejects_empty_selectors(selector):
    with pytest.raises(InvalidSelector):
        check_syntax(selector)


def test_non_string_selector_is_a_type_error():
    class Locator:
        pass
    for fn in (classify, check_syntax, normalize, strip_prefix):
        with pytest.raises(TypeError) as info:
            fn(Locator())
        assert ("pass a selector string such as '#save' or \"role=button[name='Save']\"; "
                "Locator objects can't be stored") in str(info.value)
        assert 'Locator' in str(info.value)


def test_invalid_selector_is_not_attention_required():
    # The heal loop only catches AttentionRequired; a typo must never reach the provider.
    assert not issubclass(InvalidSelector, mendwright.AttentionRequired)
    assert mendwright.InvalidSelector is InvalidSelector


def test_for_key_names_the_key():
    with pytest.raises(InvalidSelector) as info:
        check_syntax('//div[')
    keyed = info.value.for_key('terms')
    assert "the selector for 'terms' is not usable" in str(keyed)
    assert keyed.selector == '//div[' and keyed.kind == 'xpath'
