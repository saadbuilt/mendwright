"""mendwright._md: text from a store, a journal or your source files, put into Markdown so
that it can only show as text: in a code span or a fenced block, with the characters that
would make it read differently written out as \\uXXXX."""

import re

from mendwright import _md, cli


def _span_body(span):
    """The text a CommonMark code span shows."""
    m = re.fullmatch(r'(`+)(.*)\1', span, re.S)
    assert m, span
    body = m.group(2)
    if len(body) >= 2 and body[0] == ' ' and body[-1] == ' ' and body.strip(' '):
        body = body[1:-1]
    return body


def test_visible_writes_out_controls_separators_bidi_and_lone_surrogates():
    text = 'a\x1bb\tc\x7fd\x85e f g‮h⁦i؜j‎k\ud800l\nm'
    assert _md.visible(text) == ('a\\u001Bb\\u0009c\\u007Fd\\u0085e\\u2028f\\u2029g\\u202Eh'
                                 '\\u2066i\\u061Cj\\u200Ek\\uD800l\nm')
    assert _md.visible('plain é 😀 text') == 'plain é 😀 text'


def test_code_picks_a_delimiter_longer_than_any_run_inside():
    for text in ('a`b', 'a``b', 'a```b', '`x', 'x`', '``', 'a ` b'):
        span = _md.code(text)
        run = max(len(r) for r in re.findall('`+', text))
        assert span.startswith('`' * (run + 1)) and not span.startswith('`' * (run + 2)), span
        assert _span_body(span) == text, span


def test_code_pads_a_leading_or_trailing_space_or_backtick():
    assert _md.code(' x') == '`  x `'
    assert _span_body(_md.code(' x')) == ' x'
    assert _md.code('`x') == '`` `x ``'
    assert _md.code('x') == '`x`'


def test_code_collapses_whitespace_and_escapes_pipes_in_a_table():
    assert _md.code('a\n  b\tc') == '`a b\\u0009c`'
    assert _md.code('//a | //b', table=True) == '`//a \\| //b`'
    assert _md.code('//a | //b') == '`//a | //b`'


def test_code_cuts_long_text_and_says_so():
    span = _md.code('x' * 600)
    assert span == '`' + 'x' * 500 + '…` (cut, 600 characters)'
    assert _md.code('x' * 600, cut=None) == '`' + 'x' * 600 + '`'


def test_empty_text_is_named_outside_a_span():
    assert _md.code('') == '(empty)'


def test_fence_is_longer_than_any_run_and_at_least_four():
    assert _md.fence('a\nb') == '````text\na\nb\n````'
    assert _md.fence('x ````` y').startswith('``````text\n')
    assert _md.fence('x ````` y').endswith('\n``````')
    assert _md.fence('a‮b') == '````text\na\\u202Eb\n````'


def test_report_markdown_puts_two_backticks_in_a_row_in_a_span_that_holds_them():
    assert cli._md_code('a``b') == '```a``b```'
    assert cli._md_code('`a`') == '`` `a` ``'
    assert cli._md_code('//a | //b') == '`//a \\| //b`'
    assert cli._md_cell('a |\n b') == 'a \\| b'
