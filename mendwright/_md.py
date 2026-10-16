"""Markdown for text mendwright did not write: keys, selectors, paths and names from a store,
a journal or your source files, shown in `mendwright report --format md` and `mendwright fix
--format md` (the body of the GitHub Action's pull request).

Every such value goes in a code span or a fenced block, so `@name` mentions, `#123`
references, links, images and HTML in it do nothing. visible() first writes the characters
that would make the text read differently from what it says as ``\\uXXXX``: control
characters, line and paragraph separators, the bidirectional controls, and lone surrogates.
"""

import re

# The bidirectional controls: they reorder the text around them when it is shown.
_BIDI = frozenset([0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
                   0x2066, 0x2067, 0x2068, 0x2069])
_SPACE = re.compile(r'\s+')
_TICKS = re.compile(r'`+')


def _shown_as_code(ch):
    n = ord(ch)
    if ch == '\n':
        return False
    return (n < 0x20 or 0x7F <= n <= 0x9F or n in (0x2028, 0x2029) or n in _BIDI
            or 0xD800 <= n <= 0xDFFF)


def visible(text):
    """text with control characters (other than a newline), U+2028, U+2029, the
    bidirectional controls and lone surrogates written as the text \\uXXXX."""
    text = str(text)
    return ''.join('\\u%04X' % ord(ch) if _shown_as_code(ch) else ch for ch in text)


def _longest_run(text):
    return max((len(m) for m in _TICKS.findall(text)), default=0)


def code(text, *, table=False, cut=500):
    """text as one Markdown code span: visible(), whitespace runs collapsed to one space, cut
    to ``cut`` characters (with an ellipsis inside the span and the length after it). The
    span's backticks are one more than the longest run of backticks inside it, so no text
    ends it early. In a table cell, | is written \\| so it does not split the row. Empty
    text gives (empty), outside a span."""
    text = _SPACE.sub(' ', visible(text))
    if not text:
        return '(empty)'
    after = ''
    if cut is not None and len(text) > cut:
        after = ' (cut, ' + str(len(text)) + ' characters)'
        text = text[:cut] + '…'
    if table:
        text = text.replace('|', '\\|')
    ticks = '`' * (_longest_run(text) + 1)
    if text[0] in '` ' or text[-1] in '` ':
        text = ' ' + text + ' '
    return ticks + text + ticks + after


def fence(text):
    """text as a fenced code block with the info string text: visible() with newlines kept,
    in a fence of backticks one longer than the longest run in it, and at least four."""
    text = visible(text)
    ticks = '`' * max(4, _longest_run(text) + 1)
    return ticks + 'text\n' + text + ('' if text.endswith('\n') else '\n') + ticks


def cell(text):
    """text for a table cell outside a code span: whitespace collapsed, | escaped. Only for
    text mendwright wrote itself, such as a status or a date."""
    return ' '.join(str(text).split()).replace('|', '\\|')
