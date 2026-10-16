"""Build the redacted heal bundle that goes to the model provider or gateway.

Redaction is best-effort. Before upload, quoted value="..." attributes are removed, row
secrets (the secrets, proxy and profile strings) are replaced with [redacted], absolute
URLs become [URL] and runs of 12 to 19 digits become [redacted number], in the HTML, the
broken selectors, the expected text, the intent and every string of the DOM inventory. Visible text
and other attribute values (aria-label, title, placeholder, data-*) are sent as they are;
SECURITY.md lists the gaps.

The key's saved fingerprint (mendwright.fingerprint) goes into the bundle only for a provider
that asks for it with ``wants_fingerprint = True``, which LocalModelProvider does: a model on
your own machine. It is redacted the same way.

The HTML that goes into the bundle is also cleaned first. Scripts, styles, <noscript>,
<template>, comments, the inside of <svg> and everything in <head> except <title> are
removed, and runs of whitespace are collapsed. Real pages are often mostly inline
script and CSS, and the prompt has room for 60,000 characters, so without this a button
near the end of a large page would not reach the model. A page that is still too long is
fitted to the prompt later, by leaving out what looks unrelated to the step
(mendwright.fit). Cleaning changes only what the model sees: the gate still validates
every proposal against the full captured HTML.
"""

import html as _html
import re

import lxml.html
from lxml import etree

from . import fingerprint as _fingerprint

_VALUE_ATTR = re.compile(r"""\svalue=("[^"]*"|'[^']*')""")
_URL = re.compile(r'https?://[^\s"\'<>]+')
_LONGNUM = re.compile(r'(?<!\d)\d{12,19}(?!\d)')

# Only ASCII whitespace: a non-breaking space is part of a label's text, and collapsing it
# would show the model text that an xpath could not match on the live page.
_WHITESPACE = re.compile(r'[ \t\n\r\f\v]+')
_XML_DECL = re.compile(r'^\s*<\?xml[^>]*\?>')
_DROP_TAGS = ('script', 'style', 'noscript', 'template')
_HEAD_ONLY = ('meta', 'link', 'base')


def _scrub(text):
    text = _URL.sub('[URL]', text)
    return _LONGNUM.sub('[redacted number]', text)


def _secret_values(row):
    if not row:
        return []
    values = list((row.get('secrets') or {}).values()) + list((row.get('proxy') or {}).values())

    def collect(node):
        if isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, str) and node:
            values.append(node)

    collect(row.get('profile', {}))
    return sorted({v for v in values if isinstance(v, str) and v}, key=len, reverse=True)


def _secret_forms(values):
    """Each secret as written in plain text and as HTML-escaped (``&`` -> ``&amp;``,
    quotes -> entities), longest first, so a secret serialized into markup still matches."""
    forms = set()
    for value in values:
        forms.add(value)
        forms.add(_html.escape(value, quote=False))
        forms.add(_html.escape(value, quote=True))
    return sorted(forms, key=len, reverse=True)


def _redact_text(text, forms):
    """The one redaction used for the HTML snapshot and for every inventory string:
    drop value="..." attributes, replace secrets, then scrub URLs and long numbers."""
    out = _VALUE_ATTR.sub('', text)  # drop filled input/option values
    for value in forms:
        if len(value) < 4:
            out = re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', '[redacted]', out)
        else:
            out = out.replace(value, '[redacted]')
    return _scrub(out)  # URLs + long numbers


def redact(text, row=None):
    if not isinstance(text, str):
        return text
    return _redact_text(text, _secret_forms(_secret_values(row)))


def _redact_tree(node, forms):
    """Redact every string in an inventory structure. A ``value`` key is dropped, the way
    the value="..." attribute is dropped from the HTML: it holds what was typed or chosen."""
    if isinstance(node, dict):
        return {k: _redact_tree(v, forms) for k, v in node.items()
                if not (isinstance(k, str) and k.lower() == 'value')}
    if isinstance(node, (list, tuple)):
        return [_redact_tree(v, forms) for v in node]
    if isinstance(node, str):
        return _redact_text(node, forms)
    return node


def clean_html(text):
    """Return ``text`` without the parts that never help a model pick a selector.

    Removes <script>, <style>, <noscript>, <template>, comments and processing
    instructions, empties <svg> (the tag and its attributes stay), keeps only <title> inside
    <head>, and collapses runs of whitespace in text. Text that follows a removed element
    is kept. Returns ``text`` unchanged if it is not a string, is blank, or cannot be parsed.
    """
    if not isinstance(text, str) or not text.strip():
        return text
    try:
        root = lxml.html.fromstring(_XML_DECL.sub('', text, count=1))
        if not isinstance(root.tag, str):
            return text
        if root.tag.lower() in _DROP_TAGS:
            return ''
        etree.strip_elements(root, *_DROP_TAGS, etree.Comment, etree.ProcessingInstruction,
                             with_tail=False)
        for svg in list(root.iter('svg')):
            for child in list(svg):
                svg.remove(child)
            svg.text = None
        # <head> keeps only <title>: script/style/noscript/template are already gone, and
        # meta/link/base go here. Anything else stays, because libxml2 parses a fragment
        # that starts with <script> by putting the following body elements into <head>.
        for head in list(root.iter('head')):
            for child in list(head):
                if isinstance(child.tag, str) and child.tag.lower() in _HEAD_ONLY:
                    head.remove(child)
        for el in root.iter():
            if el.text:
                el.text = _WHITESPACE.sub(' ', el.text)
            if el.tail:
                el.tail = _WHITESPACE.sub(' ', el.tail)
        return lxml.html.tostring(root, encoding='unicode')
    except Exception:
        return text


def build_bundle(key, meta, broken_candidates, inventory, html, context, row=None,
                 with_fingerprint=False):
    """The redacted bundle for one heal. with_fingerprint=True adds the key's saved
    fingerprint (meta['fingerprint']), redacted; otherwise it stays out."""
    forms = _secret_forms(_secret_values(row))
    # Redact before and after cleaning: collapsing whitespace could split a multi-line
    # secret, and re-serializing can turn an entity-encoded secret into plain text.
    page = html
    if isinstance(page, str):
        page = _redact_text(clean_html(_redact_text(page, forms)), forms)
    # The broken selectors and the expected text can be built from the same row values (a
    # data-driven test), so they get the same redaction as the page before the prompt.
    text = meta.get('expectedText') or []
    if isinstance(text, str):
        text = [text]
    xpaths = [c.get('xpath') if isinstance(c, dict) else c for c in (broken_candidates or [])]
    intent = meta.get('intent')
    fp = meta.get('fingerprint') if with_fingerprint else None
    bundle = {
        'key': key,
        'expectedRole': meta.get('expectedRole'),
        'expectedText': [_redact_text(t, forms) if isinstance(t, str) else t for t in text],
        # What the element is for, in the user's words (locator(intent=...)).
        'intent': _redact_text(intent, forms) if isinstance(intent, str) and intent else None,
        'destructive': bool(meta.get('destructive')),
        'candidates': [_redact_text(x, forms) for x in xpaths if isinstance(x, str) and x],
        'inventory': _redact_tree(inventory, forms),
        'html': page,
        'url': _scrub(redact(context.get('url') or '', row)) or None,
        # The page path now, and the one the key's element was last found on (see
        # mendwright.flow.page_path), so the model can tell a moved step from a renamed one.
        'page': _scrub(redact(context.get('page') or '', row)) or None,
        'lastPage': _scrub(redact(context.get('lastPage') or '', row)) or None,
        'stage': context.get('stage'),
        'version': context.get('version'),
    }
    if _fingerprint.valid(fp):
        # The element the key matched last time, redacted like the expected text.
        bundle['fingerprint'] = _redact_tree(fp, forms)
    return bundle
