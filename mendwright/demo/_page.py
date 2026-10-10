"""A stand-in for a browser page, so the demo runs with no browser installed.

StandInPage has the shape of a sync Playwright Page (locator, frame_locator, content,
evaluate, set_content, url) over an lxml DOM. mendwright.open() detects it as a
Playwright page, so the real driver, resolver, gate and store run unchanged. A selector
matches only when it really matches the HTML, and check() really changes the DOM.

It understands XPath and '#id' CSS selectors, which is all the demo uses. Anything
else is rejected the way a browser rejects a selector it cannot parse.
"""

import re

import lxml.etree
import lxml.html

from ..drivers.base import DESCRIBE_JS, INVENTORY_JS

_CSS_ID = re.compile(r'^#([A-Za-z_][\w-]*)$')
_EMPTY = '<!doctype html><html><head></head><body></body></html>'


def _hidden(el):
    """True when el or an ancestor is hidden the ways a static page can say so."""
    if el.tag == 'input' and (el.get('type') or '').lower() == 'hidden':
        return True
    while el is not None:
        style = (el.get('style') or '').replace(' ', '').lower()
        if 'display:none' in style or 'visibility:hidden' in style:
            return True
        if el.get('hidden') is not None:
            return True
        el = el.getparent()
    return False


def _norm(text):
    return ' '.join((text or '').split())


class StandInLocator:
    """What StandInPage.locator() returns: a lazy query, like a Playwright Locator."""

    def __init__(self, page, selector):
        self._page = page
        self._selector = selector

    def _elements(self):
        return self._page.query(self._selector)

    def _one(self, action):
        els = self._elements()
        if len(els) != 1:
            # Playwright's strict mode refuses to act on zero or several matches.
            raise RuntimeError('Locator.' + action + ': ' + self._selector + ' matched '
                               + str(len(els)) + ' elements on the stand-in page')
        return els[0]

    def count(self):
        return len(self._elements())

    def is_visible(self, **kwargs):
        els = self._elements()
        return len(els) == 1 and not _hidden(els[0])

    def scroll_into_view_if_needed(self, **kwargs):
        pass

    def evaluate_all(self, expression, arg=None):
        if expression != DESCRIBE_JS:
            raise NotImplementedError('the stand-in page only runs mendwright\'s describe script')
        els = self._elements()
        if len(els) != 1:
            return None
        el = els[0]
        return {'tag': el.tag.lower(),
                'html': lxml.html.tostring(el, encoding='unicode', with_tail=False)}

    def get_attribute(self, name, **kwargs):
        els = self._elements()
        return els[0].get(name) if els else None

    def inner_text(self, **kwargs):
        return _norm(self._one('inner_text').text_content())

    def is_checked(self, **kwargs):
        return self._one('is_checked').get('checked') is not None

    def check(self, **kwargs):
        self._one('check').set('checked', '')

    def uncheck(self, **kwargs):
        el = self._one('uncheck')
        if el.get('checked') is not None:
            del el.attrib['checked']

    def click(self, **kwargs):
        el = self._one('click')
        if el.tag == 'input' and (el.get('type') or '').lower() in ('checkbox', 'radio'):
            if el.get('checked') is None:
                el.set('checked', '')
            elif (el.get('type') or '').lower() == 'checkbox':
                del el.attrib['checked']

    def __repr__(self):
        return '<StandInLocator ' + repr(self._selector) + '>'


class StandInPage:
    """A sync-Playwright-shaped page over an lxml DOM. set_content() loads new HTML, the
    way page.set_content() does on a real page."""

    url = 'about:blank'

    def __init__(self, html=None):
        self.set_content(html or _EMPTY)

    def set_content(self, html, **kwargs):
        self._doc = lxml.html.document_fromstring(html if html.strip() else _EMPTY)

    def query(self, selector):
        """The elements a driver-level selector ('xpath=...' or 'css=#id') matches."""
        if selector.startswith('xpath='):
            try:
                found = self._doc.xpath(selector[len('xpath='):])
            except lxml.etree.XPathError:
                return []
            return [e for e in found if isinstance(getattr(e, 'tag', None), str)]
        body = selector[len('css='):].strip() if selector.startswith('css=') else selector
        m = _CSS_ID.match(body)
        if m:
            return self._doc.xpath('//*[@id=$i]', i=m.group(1))
        # Worded like Playwright, so mendwright reports it as an invalid selector.
        raise ValueError('Locator.count: ' + repr(body) + ' is not a valid selector on the '
                         'stand-in page, which runs XPath and #id CSS only')

    def locator(self, selector):
        return StandInLocator(self, selector)

    def frame_locator(self, selector):
        return self  # the demo pages have no frames

    def content(self):
        return '<!DOCTYPE html>\n' + lxml.html.tostring(self._doc, encoding='unicode')

    def evaluate(self, expression, arg=None):
        if expression != INVENTORY_JS:
            raise NotImplementedError('the stand-in page only runs mendwright\'s inventory script')
        return self._inventory(200 if arg is None else int(arg))

    def _inventory(self, limit):
        """What mendwright's inventory script returns in a browser, computed with lxml."""
        out = []
        for el in self._doc.iter():
            if len(out) >= limit:
                break
            tag = el.tag.lower() if isinstance(el.tag, str) else ''
            if tag not in ('button', 'a', 'input', 'select', 'textarea') and el.get('role') is None:
                continue
            typ = (el.get('type') or '').lower()
            if tag == 'input' and typ == 'hidden':
                continue
            field = tag in ('input', 'textarea', 'select')
            out.append({'tag': tag, 'role': el.get('role') or '', 'type': typ,
                        'id': el.get('id') or '', 'name': el.get('name') or '',
                        'text': '' if field else _norm(el.text_content())[:80],
                        'aria_label': el.get('aria-label') or ''})
        return out

    def __repr__(self):
        return '<StandInPage>'
