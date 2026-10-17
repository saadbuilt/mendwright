"""client.apply() for a heal inside an open shadow root (see mendwright.shadow): the XPath is
checked in that shadow root's HTML, turned into a CSS selector for the same element, and that
CSS passes the live check inside the shadow root before it is saved as the chain of the hosts
and the CSS. Only quarantines made there block it."""

import pytest

from mendwright import shadow
from mendwright.client import NO_CSS, _quarantined_paths, add_heal, apply
from mendwright.store import load_store

HOST = 'my-app[id="root"]'


# --- client.apply inside a shadow root ------------------------------------------------------------

SHADOW_HTML = shadow.as_document(
    '<div class="panel"><button id="save-2" data-testid="save" type="button">Save</button>'
    '<button type="button">Cancel</button></div>')


class LiveShadow:
    """A stand-in for a ShadowDriver: count(), is_visible() and describe() of a CSS selector
    inside the shadow root, from fixed answers."""

    shadow_hosts = (HOST,)

    def __init__(self, count=1, visible=True, role='button'):
        self._count, self._visible, self._role = count, visible, role
        self.asked = []

    def count(self, selector, frame_chain=()):
        self.asked.append(selector)
        return self._count

    def is_visible(self, selector, frame_chain=()):
        return self._visible

    def describe(self, selector, frame_chain=()):
        return {'tag': 'button', 'role': self._role, 'text': 'Save', 'outer_html': '<button>'}


META = {'expectedRole': 'button', 'expectedText': [], 'destructive': False}


def _apply(store, xpath, driver, **kwargs):
    return apply(store, 'k', {'xpath': xpath}, SHADOW_HTML, META, driver=driver,
                 shadow_hosts=[HOST], **kwargs)


def test_a_proposal_inside_a_shadow_root_is_saved_as_css_with_its_hosts(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    live = LiveShadow()
    saved, reason = _apply(store, "//button[@id='save-2']", live)
    assert (saved, reason) == (HOST + ' >> [data-testid="save"]', 'ok')
    assert live.asked == ['[data-testid="save"]']        # only CSS runs inside
    row = [c for c in store.candidates('k') if c['xpath'] == saved][0]
    assert row['shadowHosts'] == [HOST] and row['source'] == 'heal'


def test_a_proposal_inside_a_shadow_root_with_no_css_form_is_refused(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    assert _apply(store, "//button[normalize-space()='Cancel']", LiveShadow()) == (None, NO_CSS)
    assert store.candidates('k') == []


@pytest.mark.parametrize('live, why', [
    (LiveShadow(count=2), 'not uniquely present'),
    (LiveShadow(visible=False), 'not uniquely present'),
    (LiveShadow(role='link'), 'live element role mismatch'),
    (None, 'no live page to check it inside the shadow root'),
])
def test_the_live_check_runs_inside_the_shadow_root(tmp_path, live, why):
    store = load_store(None, str(tmp_path / 'o.json'))
    saved, reason = _apply(store, "//button[@id='save-2']", live)
    assert saved is None and why in reason and store.candidates('k') == []


def test_a_quarantined_heal_blocks_its_element_in_its_shadow_root_only(tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'))
    store.add_candidate('k', HOST + ' >> [data-testid="save"]', source='heal', shadow_hosts=[HOST])
    store.quarantine('k', HOST + ' >> [data-testid="save"]')
    _doc, bad = _quarantined_paths(store, 'k', SHADOW_HTML, '', (HOST,))
    assert len(bad) == 1
    saved, reason = _apply(store, "//button[@id='save-2']", LiveShadow())
    assert saved is None and 'previously quarantined' in reason
    # On the page, the same-looking element is not blocked by it.
    assert _quarantined_paths(store, 'k', SHADOW_HTML, '')[1] == set()


def test_a_store_that_cannot_keep_shadow_hosts_saves_nothing_inside_a_shadow_root(tmp_path):
    class OldStore:
        def __init__(self):
            self.added = []

        def add_candidate(self, key, xpath, *, source='heal', frame_xpath=None, css=None):
            self.added.append(xpath)

        def candidates(self, key):
            return []

    old = OldStore()
    assert add_heal(old, 'k', HOST + ' >> #a', shadow_hosts=[HOST]) is False and old.added == []
    assert add_heal(old, 'k', '//a', shadow_hosts=[]) is True and old.added == ['//a']
    saved, reason = apply(old, 'k', {'xpath': "//button[@id='save-2']"}, SHADOW_HTML, META,
                          driver=LiveShadow(), shadow_hosts=[HOST])
    assert saved is None and 'cannot save a selector inside a shadow root' in reason
