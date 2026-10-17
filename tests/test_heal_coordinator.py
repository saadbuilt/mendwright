import unittest

import lxml.html
import pytest

from mendwright import HealCoordinator, resolve_with_heal
from mendwright import AttentionRequired, SelectorStore, fingerprint, getby
from mendwright.coordinator import DISABLED_REASON, GUARD_REASON

TERMS_HTML = ("<html><body><main><dml-checkbox><label><input type='checkbox'>"
              "<span>accept the <a href='https://shop.example.com/en/terms-and-conditions'>terms of use</a></span>"
              "</label></dml-checkbox></main></body></html>")
TERMS_XPATH = "//main//label[.//a[contains(@href,'terms-and-conditions')]]//input[@type='checkbox']"
META = {'expectedRole': 'checkbox', 'expectedText': [], 'destructive': False}


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if '/heal/outcome' in url:
            return FakeResp({})
        return self.responses.pop(0)


class FakeStore:
    def __init__(self):
        self.added = []
        self.quarantined = []

    def candidates(self, key):
        return [{'xpath': '//broken'}]

    def add_candidate(self, key, xpath, *, source='heal', **kw):
        self.added.append((key, xpath, source))

    def quarantine(self, key, xpath):
        self.quarantined.append((key, xpath))


def coord(store, responses, guard=True):
    return HealCoordinator(store, 'https://gw', 'k', http=FakeHttp(responses),
                           html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                           guard=lambda: guard)


class CoordinatorTests(unittest.TestCase):
    def test_try_heal_applies_valid_proposal(self):
        store = FakeStore()
        c = coord(store, [FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        self.assertTrue(c.try_heal('terms', META, store.candidates('terms'), {}, None))
        self.assertEqual(store.added, [('terms', TERMS_XPATH, 'heal')])

    def test_guard_blocks_heal(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: False)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))
        self.assertEqual(http.calls, [])  # no heal attempted while a submission is pending

    def test_reprompts_once_then_fails(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'xpath': "//main//input[@id='nope']"}),
                         FakeResp({'xpath': "//main//input[@id='still-nope']"})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))
        self.assertEqual(len([u for u, _ in http.calls if u.endswith('/heal')]), 2)

    def test_reprompt_second_succeeds(self):
        store = FakeStore()
        http = FakeHttp([FakeResp({'xpath': "//main//input[@id='nope']"}),
                         FakeResp({'xpath': TERMS_XPATH})])
        c = HealCoordinator(store, 'https://gw', 'k', http=http,
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True)
        self.assertTrue(c.try_heal('terms', META, [], {}, None))

    def test_confirm_failure_quarantines(self):
        store = FakeStore()
        c = coord(store, [])
        c.confirm('terms', TERMS_XPATH, success=False)
        self.assertEqual(store.quarantined, [('terms', TERMS_XPATH)])

    def test_confirm_success_does_not_quarantine(self):
        store = FakeStore()
        c = coord(store, [])
        c.confirm('terms', TERMS_XPATH, success=True)
        self.assertEqual(store.quarantined, [])

    def test_dry_run_validates_but_does_not_apply(self):
        store = FakeStore()
        logs = []
        c = HealCoordinator(store, 'https://gw', 'k',
                            http=FakeHttp([FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})]),
                            html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                            guard=lambda: True, dry_run=True, log=logs.append)
        self.assertFalse(c.try_heal('terms', META, [], {}, None))  # never applies/retries in dry-run
        self.assertEqual(store.added, [])
        self.assertTrue(any('valid=True' in m for m in logs))


class ResolveWithHealTests(unittest.TestCase):
    def test_auto_retries_same_step_after_heal(self):
        store = FakeStore()
        c = coord(store, [FakeResp({'status': 'ok', 'xpath': TERMS_XPATH})])
        calls = {'n': 0}

        def resolve_fn():
            calls['n'] += 1
            if not store.added:          # healed candidate not yet present
                raise AttentionRequired('No matching element for: terms')
            return 'LOCATOR'

        result = resolve_with_heal(resolve_fn, c, 'terms', META, {}, None)
        self.assertEqual(result, 'LOCATOR')
        self.assertEqual(calls['n'], 2)  # first raised, second (post-heal) succeeded

    def test_reraises_when_no_coordinator(self):
        def resolve_fn():
            raise AttentionRequired('x')

        with self.assertRaises(AttentionRequired):
            resolve_with_heal(resolve_fn, None, 'terms', META, {}, None)


if __name__ == '__main__':
    unittest.main()


# --- the key of a heal.page.get_by_*() call: the fingerprint tier only ------------------------

CALL = 'get_by_role("button", name="Sign in")'
OLD_PAGE = "<html><body><main><button id='login' class='primary'>Sign in</button></main></body></html>"
NEW_PAGE = "<html><body><main><button id='login' class='primary'>Log in</button></main></body></html>"
TWINS = ("<html><body><main><button class='primary'>Log in</button>"
         "<button class='primary'>Log in</button></main></body></html>")


class Model:
    """A provider that would propose the right element, to show it is never asked."""

    def __init__(self):
        self.proposals = 0

    def propose(self, bundle):
        self.proposals += 1
        return {'xpath': "//button[@id='login']", 'fallbacks': [], 'reasoning': 'stub'}

    def report_outcome(self, key, xpath, success):
        pass


class Limits:
    def __init__(self):
        self.asked = 0

    def allow(self, key, provider=None, cost=0.0):
        self.asked += 1
        return True


class Events:
    def __init__(self):
        self.seen = []

    def __getattr__(self, name):
        return lambda key, **fields: self.seen.append((name, key, fields))


class LivePage:
    """The live page: one visible button, read as a button, whose ARIA role is answer."""

    def __init__(self, answer=True):
        self.answer = answer

    def count(self, selector, frame_chain=()):
        return 1

    def is_visible(self, selector, frame_chain=()):
        return True

    def describe(self, selector, frame_chain=()):
        return {'tag': 'button', 'role': 'button', 'text': 'Log in',
                'outer_html': "<button id='login'>Log in</button>"}

    def role_match(self, selector, role, level=None, frame_chain=()):
        return self.answer


def _call_store(tmp_path, *, fp=True, role='button'):
    entry = {'expectedRole': role, 'candidates': [
        {'xpath': CALL, 'source': 'code', 'status': 'active',
         'lastSucceededAt': '2026-10-20T09:00:00+00:00'}]}
    if fp:
        el = lxml.html.fromstring(OLD_PAGE).xpath('//button')[0]
        entry['fingerprint'] = fingerprint.of_element(el)
    return SelectorStore({'schema': 2, 'selectors': {CALL: entry}}, str(tmp_path / 'o.json'))


def _call_coord(store, html=NEW_PAGE, *, live=None, guard=True, tier=True, dry_run=False):
    return HealCoordinator(store, provider=Model(), html_provider=lambda: html,
                           inventory_provider=lambda: [], guard=lambda: guard,
                           driver=live or LivePage(), fingerprint=tier, guardrails=Limits(),
                           journal=Events(), dry_run=dry_run)


def _heal(c, store):
    return c.try_heal(CALL, store.meta(CALL), store.candidates(CALL), {})


def test_a_call_key_heals_from_its_fingerprint_and_never_asks_a_model(tmp_path):
    store = _call_store(tmp_path)
    c = _call_coord(store)
    assert _heal(c, store) == "//button[@id='login']"
    assert c.provider.proposals == 0 and c.guardrails.asked == 0
    assert [e[0] for e in c.journal.seen] == ['requested', 'proposed', 'applied']
    assert c.journal.seen[-1][2]['source'] == 'fingerprint'


@pytest.mark.parametrize('setup, reason', [
    (dict(disabled=True, tier=False), DISABLED_REASON),
    (dict(tier=False), getby.tier_off_message(CALL)),
    (dict(fp=False), getby.no_fingerprint_message(CALL)),
    (dict(role='unknown'), getby.role_unknown_message(CALL)),
    (dict(guard=False, fp=False, role='unknown'), getby.no_fingerprint_message(CALL)),
    (dict(guard=False), GUARD_REASON),
])
def test_a_call_key_is_declined_in_order_with_the_call_sentence(tmp_path, setup, reason):
    store = _call_store(tmp_path, fp=setup.get('fp', True), role=setup.get('role', 'button'))
    c = _call_coord(store, guard=setup.get('guard', True), tier=setup.get('tier', True))
    c.disabled = setup.get('disabled', False)
    assert not _heal(c, store)
    assert c.explain(CALL) == reason
    assert c.journal.seen == [] and c.provider.proposals == 0 and c.guardrails.asked == 0


def test_the_messages_name_the_call():
    assert getby.tier_off_message(CALL) == (
        'the fingerprint tier is off (fingerprint=False or MENDWRIGHT_FINGERPRINT=0), and '
        "mendwright heals a get_by_* call only from its fingerprint, so 'get_by_role(\"button\", "
        "name=\"Sign in\")' was not healed. Turn the fingerprint tier on to heal get_by_* calls.")
    assert getby.role_unknown_message('get_by_text("Free shipping")') == (
        "mendwright does not know what kind of element 'get_by_text(\"Free shipping\")' is: it "
        'could not read the element when the call last matched, so it cannot check a repaired '
        'one. Run the step again while the call matches.')


def test_an_unsure_fingerprint_match_is_not_handed_to_a_model(tmp_path):
    store = _call_store(tmp_path)
    c = _call_coord(store, html=TWINS)
    assert not _heal(c, store)
    reason = c.explain(CALL)
    assert reason.startswith("the fingerprint match for '" + CALL + "' was not confident: ")
    assert reason.endswith('. ' + getby.NO_MODEL)
    assert c.provider.proposals == 0 and c.guardrails.asked == 0
    assert [e[0] for e in c.journal.seen] == ['requested']


def test_a_pick_without_the_calls_aria_role_is_rejected(tmp_path):
    store = _call_store(tmp_path)
    c = _call_coord(store, live=LivePage(answer=False))
    assert not _heal(c, store)
    assert c.explain(CALL) == (
        "the fingerprint match for '" + CALL + "', //button[@id='login'], failed mendwright's "
        "checks (live element role mismatch: Playwright does not give it the ARIA role 'button'). "
        + getby.NO_MODEL)
    rejected = [e for e in c.journal.seen if e[0] == 'rejected']
    assert rejected[0][2]['reason'] == (
        "live element role mismatch: Playwright does not give it the ARIA role 'button'")
    assert store.candidates(CALL)[0]['xpath'] == CALL and len(store.candidates(CALL)) == 1
    assert c.provider.proposals == 0


def test_a_dry_run_checks_the_call_key_and_applies_nothing(tmp_path):
    store = _call_store(tmp_path)
    c = _call_coord(store, dry_run=True)
    assert _heal(c, store) is False
    assert 'dry-run mode is on' in c.explain(CALL)
    assert [e[0] for e in c.journal.seen] == ['requested', 'proposed']
    assert len(store.candidates(CALL)) == 1 and c.provider.proposals == 0


def test_a_destructive_call_key_keeps_the_destructive_refusal(tmp_path):
    store = _call_store(tmp_path)
    store.set_meta(CALL, destructive=True)
    c = _call_coord(store)
    assert not _heal(c, store)
    assert 'is marked as a destructive control' in c.explain(CALL)
    assert c.journal.seen == []
