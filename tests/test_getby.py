"""mendwright.getby: the keys of heal.page.get_by_*() calls, how they are read back, and the
calls that are never keys. Offline: no browser, no model."""

import ast
import builtins
import random
import re

import pytest

import mendwright
from mendwright import AttentionRequired, InvalidSelector
from mendwright import getby
from mendwright.getby import GetByCall, call_of, canonical, get_by_key, is_key, parse
from mendwright.roles import infer_role
from mendwright.selectors import check_syntax, classify, frame_spelling, normalize


def _key(method, *args, **kwargs):
    call, why = call_of(method, args, kwargs)
    assert call is not None, why
    return canonical(call)


# The table of the spec, section 4.1: each call on heal.page, and its key.
KEYS = [
    (('get_by_role', 'button'), {'name': 'Sign in'}, 'get_by_role("button", name="Sign in")'),
    (('get_by_role',), {'role': 'button', 'name': 'Sign in', 'exact': False},
     'get_by_role("button", name="Sign in")'),
    (('get_by_role', 'button'), {'name': 'Sign in', 'exact': True},
     'get_by_role("button", name="Sign in", exact=True)'),
    (('get_by_role', 'heading'), {'name': 'Cart', 'level': 2},
     'get_by_role("heading", name="Cart", level=2)'),
    (('get_by_role', 'button'), {'name': re.compile(r'sign\s?in', re.I)},
     'get_by_role("button", name=re.compile("sign\\\\s?in", re.IGNORECASE))'),
    (('get_by_role', 'navigation'), {}, 'get_by_role("navigation")'),
    (('get_by_role', 'button'), {'exact': True}, 'get_by_role("button")'),
    (('get_by_text', 'Free shipping'), {}, 'get_by_text("Free shipping")'),
    (('get_by_text', 'Say "hi"'), {}, 'get_by_text("Say \\"hi\\"")'),
    (('get_by_label', 'Email'), {'exact': True}, 'get_by_label("Email", exact=True)'),
    (('get_by_placeholder', 'you@example.com'), {}, 'get_by_placeholder("you@example.com")'),
    (('get_by_alt_text', 'Logo'), {}, 'get_by_alt_text("Logo")'),
    (('get_by_title', 'Close'), {}, 'get_by_title("Close")'),
    (('get_by_test_id', 'login'), {}, 'get_by_test_id("login")'),
]


@pytest.mark.parametrize('args, kwargs, key', KEYS)
def test_the_key_of_each_method(args, kwargs, key):
    assert _key(*args, **kwargs) == key
    assert get_by_key(*args, **kwargs) == key


def test_the_key_spells_the_options_one_way():
    assert _key('get_by_role', 'button', exact=None) == 'get_by_role("button")'
    assert _key('get_by_text', 'Save', exact=False) == 'get_by_text("Save")'
    assert _key('get_by_text', text='Save', exact=None) == 'get_by_text("Save")'
    assert _key('get_by_test_id', test_id='go') == 'get_by_test_id("go")'
    # exact= changes nothing for a regular expression, so the key leaves it out.
    assert _key('get_by_text', re.compile('save'), exact=True) == 'get_by_text(re.compile("save"))'
    assert (_key('get_by_role', 'button', name=re.compile('go'), exact=True)
            == 'get_by_role("button", name=re.compile("go"))')
    # name, exact, level, whatever order they were passed in.
    assert (_key('get_by_role', 'heading', level=3, exact=True, name='Cart')
            == 'get_by_role("heading", name="Cart", exact=True, level=3)')
    assert _key('get_by_role', 'button', include_hidden=False) == 'get_by_role("button")'
    flags = re.compile('a', re.DOTALL | re.IGNORECASE | re.MULTILINE | re.UNICODE)
    assert (_key('get_by_text', flags)
            == 'get_by_text(re.compile("a", re.IGNORECASE | re.MULTILINE | re.DOTALL))')


def test_the_key_escapes_text_as_json_and_python_read_it():
    for text in ('back\\slash', 'line\nbreak', 'tab\there', 'bell\x07', 'Café ✓ 日本',
                 "it's", '"quoted"', 'line\u2028separator'):
        key = _key('get_by_text', text)
        assert key.startswith('get_by_text("') and key.endswith('")')
        assert ast.literal_eval(key[len('get_by_text('):-1]) == text   # a Python literal
        assert parse(key) == GetByCall('get_by_text', text)
    assert _key('get_by_text', 'a\nb') == 'get_by_text("a\\nb")'
    assert _key('get_by_text', 'Café') == 'get_by_text("Café")'      # written as it is
    # Case, spacing and the role's case are kept: two spellings are two keys.
    assert _key('get_by_text', 'Save ') != _key('get_by_text', 'Save')
    assert _key('get_by_role', 'Button') == 'get_by_role("Button")'


def test_canonical_text_round_trips():
    for _args, _kwargs, key in KEYS:
        assert canonical(parse(key)) == key
    rng = random.Random(9001)
    words = ['Save', 'Sign in', 'a"b', 'back\\', 'é', 'x\ny', '  pad ', '日本']
    for _ in range(200):
        method = rng.choice(getby.METHODS)
        text = rng.choice(words) + rng.choice(['', '!', ' 2'])
        if rng.random() < 0.3:
            text = re.compile(re.escape(text), rng.choice([0, re.I, re.I | re.S, re.M]))
        if method == 'get_by_role':
            kwargs = {'name': text} if rng.random() < 0.7 else {}
            if rng.random() < 0.5:
                kwargs['exact'] = rng.choice([True, False, None])
            if rng.random() < 0.3:
                kwargs['level'] = rng.randint(1, 6)
            call, why = call_of(method, ('button',), kwargs)
        elif method == 'get_by_test_id':
            call, why = call_of(method, (text,), {})
        else:
            call, why = call_of(method, (text,), {'exact': rng.choice([True, False, None])})
        assert call is not None, why
        key = canonical(call)
        assert parse(key) == call, key
        assert canonical(parse(key)) == key


def test_other_spellings_read_as_the_canonical_key():
    assert canonical(parse("get_by_role('button', exact=False)")) == 'get_by_role("button")'
    assert canonical(parse('get_by_role(role="button")')) == 'get_by_role("button")'
    assert (canonical(parse("get_by_text(re.compile('go', re.I | re.S))"))
            == 'get_by_text(re.compile("go", re.IGNORECASE | re.DOTALL))')
    assert canonical(parse("  get_by_text('Sa' 've')  ")) == 'get_by_text("Save")'
    assert normalize("get_by_label('Email', exact=None)") == 'get_by_label("Email")'
    assert is_key('get_by_test_id("go")') and not is_key('#go') and not is_key(None)


def test_canonical_key_reads_a_call_in_any_spelling_and_leaves_other_keys():
    assert getby.canonical_key("get_by_role('button', name='Pay')") == (
        'get_by_role("button", name="Pay")')
    assert getby.canonical_key('get_by_role("button", name="Pay")') == (
        'get_by_role("button", name="Pay")')
    for key in ('#pay', 'pay_button', "page.get_by_role('button', name='Pay')",
                "get_by_role('checkbox', checked=True)", None):
        assert getby.canonical_key(key) is key
    assert getby.two_spellings_message('expect_values=', 'get_by_test_id("t")',
                                       "get_by_test_id('t')") == (
        "mendwright: expect_values= has two keys for the call get_by_test_id(\"t\"): "
        "'get_by_test_id(\"t\")' and \"get_by_test_id('t')\". Keep one of them.")


def test_a_call_written_on_an_object_is_found_but_stays_a_key_of_its_own():
    pay = GetByCall('get_by_role', 'button', 'Pay')
    for key in ("page.get_by_role('button', name='Pay')",
                'heal.page.get_by_role("button", name="Pay")',
                " self._page.get_by_role('button', name='Pay')\n"):
        assert getby.on_object(key) == pay
        assert getby.canonical_key(key) is key
    for key in ('get_by_role("button", name="Pay")', '#pay', 'page.locator("#pay")',
                "page.get_by_role('checkbox', checked=True)", "page.get_by_role('button').click()",
                ".get_by_role('button')", 'page.get_by_role(' + 'x' * 1000 + ')', None):
        assert getby.on_object(key) is None
    assert getby.on_object_message('destructive_keys=', "page.get_by_role('button', name='Pay')",
                                   pay) == (
        "mendwright: destructive_keys= has the key \"page.get_by_role('button', name='Pay')\", "
        'which no heal.page.get_by_role() call uses. That call\'s key is '
        'get_by_role("button", name="Pay"), as mendwright.get_by_key() writes it.')


@pytest.mark.parametrize('method, args, kwargs, why', [
    ('get_by_role', ('checkbox',), {'name': 'Terms', 'checked': True},
     'checked= picks the element by its state, which a saved selector cannot keep'),
    ('get_by_role', ('button',), {'pressed': False, 'disabled': True},
     'disabled= picks the element by its state, which a saved selector cannot keep'),
    ('get_by_role', ('tab',), {'selected': True}, 'selected= picks the element by its state, '
                                                  'which a saved selector cannot keep'),
    ('get_by_role', ('button',), {'expanded': False}, 'expanded= picks the element by its '
                                                      'state, which a saved selector cannot keep'),
    ('get_by_role', ('button',), {'include_hidden': True},
     'include_hidden=True looks for hidden elements, and mendwright heals only onto a visible one'),
    ('get_by_role', ('  ',), {}, 'its role is empty, so it does not say which element is meant'),
    ('get_by_role', ('button',), {'name': ''},
     'its name is empty, so it does not say which element is meant'),
    ('get_by_text', ('\n',), {}, 'its text is empty, so it does not say which element is meant'),
    ('get_by_test_id', ('',), {}, 'its test id is empty, so it does not say which element is meant'),
    ('get_by_label', (re.compile(''),), {},
     'its text is empty, so it does not say which element is meant'),
    ('get_by_role', (1,), {}, 'its arguments are not text, a regular expression or a whole number'),
    ('get_by_role', (re.compile('button'),), {},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_text', (b'Save',), {}, 'its arguments are not text, a regular expression or a whole number'),
    ('get_by_text', (re.compile(b'Save'),), {},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_text', ('Save',), {'exact': 1},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_role', ('heading',), {'level': True},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_role', ('heading',), {'level': '2'},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_role', ('heading',), {'level': -1},
     'its arguments are not text, a regular expression or a whole number'),
    ('get_by_text', ('Sa\ud800ve',), {}, 'its text has characters mendwright cannot save'),
    ('get_by_text', (re.compile('save', re.X),), {},
     'its regular expression has flags mendwright cannot save: only re.IGNORECASE, '
     're.MULTILINE and re.DOTALL'),
    ('get_by_text', (re.compile('save', re.A),), {},
     'its regular expression has flags mendwright cannot save: only re.IGNORECASE, '
     're.MULTILINE and re.DOTALL'),
    ('get_by_role', ('button',), {'description': 'Go'},
     'it passes description=, which this version of mendwright does not read'),
    ('get_by_text', ('x' * 990,), {},
     'its arguments are longer than mendwright saves in a key (1,000 characters)'),
])
def test_calls_that_are_never_keys(method, args, kwargs, why):
    assert call_of(method, args, kwargs) == (None, why)


def test_parse_never_runs_code(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError('parse() imported something')

    texts = ["get_by_role(__import__('os').system('x'))", 'get_by_role(x)', 'get_by_role(*a)',
             'get_by_role(**k)', "get_by_role(f'x')", "get_by_role(b'x')",
             "get_by_role('a').first", "get_by_role('a', level=-1)",
             "get_by_text(re.compile('a', re.X))", "get_by_text(re.compile('a', flags=re.I))",
             "get_by_text(re.compile(x))", "get_by_text(os.compile('a'))",
             "get_by_role('a', name='b', name='c')", "get_by_role('a', 1.5)",
             'get_by_text("' + 'x' * 1000 + '")',
             'get_by_text(' + '(' * 300 + '"x"' + ')' * 300 + ')',
             'get_by_name("x")', 'get_by_role("a"); import os', 'get_by_role("a") or 1',
             'get_by_text(re.compile("("))']
    monkeypatch.setattr(builtins, '__import__', refuse)
    for text in texts:
        assert parse(text) is None, text


def test_classify_reads_calls_and_nothing_else():
    assert classify('get_by_role("button")') == 'get_by'
    for text in ('get_by_role', 'get_by_rolex(1)', '#get_by_role', 'get_by_role('):
        assert classify(text) == 'css', text
    with pytest.raises(InvalidSelector) as caught:
        check_syntax('get_by_role("button", name="Go")')
    assert caught.value.kind == 'get_by'
    assert str(caught.value.for_key('go')) == (
        "mendwright: the selector for 'go' is not usable: 'get_by_role(\"button\", name=\"Go\")' "
        'is a get_by_* call, not a selector. Call heal.page.get_by_role(...) on '
        'mendwright.open(page, heal_get_by=True), or pass an XPath, CSS or Playwright selector.')
    assert check_syntax('get_by_role("button")', calls=True) == 'get_by'
    assert frame_spelling('get_by_role("button")') is None
    assert infer_role('get_by_role("button")') == 'unknown'


def test_get_by_key():
    pay = mendwright.get_by_key('get_by_role', 'button', name='Pay')
    assert pay == 'get_by_role("button", name="Pay")'
    assert mendwright.get_by_key('get_by_test_id', 'total') == 'get_by_test_id("total")'
    assert 'get_by_key' in mendwright.__all__
    with pytest.raises(ValueError) as caught:
        get_by_key('get_by_name', 'Pay')
    assert str(caught.value) == (
        "mendwright.get_by_key(): 'get_by_name' is not a get_by_* method mendwright heals. Use "
        'one of get_by_role, get_by_text, get_by_label, get_by_placeholder, get_by_alt_text, '
        'get_by_title or get_by_test_id.')
    with pytest.raises(TypeError) as caught:
        get_by_key('get_by_role', 'button', nmae='Pay')
    assert str(caught.value) == ("mendwright.get_by_key(): get_by_role() got an unexpected "
                                 "keyword argument 'nmae'.")
    with pytest.raises(TypeError):
        get_by_key('get_by_text')
    with pytest.raises(ValueError) as caught:
        get_by_key('get_by_role', 'checkbox', checked=True)
    assert str(caught.value) == (
        "mendwright.get_by_key(): page.get_by_role('checkbox', checked=True) is never healed, so "
        'it has no key: checked= picks the element by its state, which a saved selector cannot '
        'keep.')


def test_build_calls_the_method_with_the_key_arguments():
    class Ctx:
        def __getattr__(self, method):
            return lambda *args, **kwargs: (method, args, kwargs)

    ctx = Ctx()
    assert getby.build(ctx, parse('get_by_role("heading", name="Cart", exact=True, level=2)')) == (
        'get_by_role', ('heading',), {'name': 'Cart', 'exact': True, 'level': 2})
    assert getby.build(ctx, parse('get_by_role("navigation")')) == ('get_by_role', ('navigation',), {})
    assert getby.build(ctx, parse('get_by_label("Email", exact=True)')) == (
        'get_by_label', ('Email',), {'exact': True})
    assert getby.build(ctx, parse('get_by_test_id("go")')) == ('get_by_test_id', ('go',), {})


def test_role_mismatch_names_the_role_and_level():
    assert getby.role_mismatch(parse('get_by_role("tab", name="Settings")')) == (
        "live element role mismatch: Playwright does not give it the ARIA role 'tab'")
    assert getby.role_mismatch(parse('get_by_role("heading", name="Cart", level=2)')) == (
        "live element role mismatch: Playwright does not give it the ARIA role 'heading' at level 2")


def test_not_found_error_is_both():
    sync_api = pytest.importorskip('playwright.sync_api')
    import playwright.async_api as async_api
    exc = getby.not_found_error(AttentionRequired('No matching element for: k (tried 1 candidate(s)).'))
    assert isinstance(exc, AttentionRequired)
    assert isinstance(exc, sync_api.TimeoutError) and isinstance(exc, async_api.TimeoutError)
    assert str(exc) == 'No matching element for: k (tried 1 candidate(s)).'
    assert exc.message == str(exc) and exc.name == 'TimeoutError'
    assert type(exc) is getby.GetByNotFound and type(exc).__module__ == 'mendwright.getby'
    with pytest.raises(sync_api.TimeoutError):
        raise exc


def test_not_found_error_without_playwright(monkeypatch):
    real = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith('playwright'):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', no_playwright)
    exc = AttentionRequired('missed')
    assert getby.not_found_error(exc) is exc
    with pytest.raises(AttributeError):
        getby.GetByNotFound  # noqa: B018
