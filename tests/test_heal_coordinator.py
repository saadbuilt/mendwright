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


@pytest.mark.parametrize('setup, reason, code', [
    (dict(disabled=True, tier=False), DISABLED_REASON, 'disabled'),
    (dict(tier=False), getby.tier_off_message(CALL), 'get_by'),
    (dict(fp=False), getby.no_fingerprint_message(CALL), 'get_by'),
    (dict(role='unknown'), getby.role_unknown_message(CALL), 'role_unknown'),
    (dict(guard=False, fp=False, role='unknown'), getby.no_fingerprint_message(CALL), 'get_by'),
    (dict(guard=False), GUARD_REASON, 'guard'),
])
def test_a_call_key_is_declined_in_order_with_the_call_sentence(tmp_path, setup, reason, code):
    store = _call_store(tmp_path, fp=setup.get('fp', True), role=setup.get('role', 'button'))
    c = _call_coord(store, guard=setup.get('guard', True), tier=setup.get('tier', True))
    c.disabled = setup.get('disabled', False)
    assert not _heal(c, store)
    assert c.explain(CALL) == reason
    assert c.journal.seen == [('declined', CALL, {'code': code, 'reason': reason})]
    assert c.provider.proposals == 0 and c.guardrails.asked == 0


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
    assert [e[0] for e in c.journal.seen] == ['requested', 'declined']
    assert c.journal.seen[-1][2] == {'code': 'get_by', 'reason': reason}


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
    assert [e[0] for e in c.journal.seen] == ['requested', 'proposed', 'declined']
    assert c.journal.seen[-1][2] == {'code': 'dry_run', 'reason': c.explain(CALL)}
    assert len(store.candidates(CALL)) == 1 and c.provider.proposals == 0


def test_a_destructive_call_key_keeps_the_destructive_refusal(tmp_path):
    store = _call_store(tmp_path)
    store.set_meta(CALL, destructive=True)
    c = _call_coord(store)
    assert not _heal(c, store)
    assert 'is marked as a destructive control' in c.explain(CALL)
    assert c.journal.seen == [('declined', CALL, {'code': 'destructive',
                                                  'reason': c.explain(CALL)})]


# --- heal.declined: one event per heal that did not apply, with a code ----------------------

class Recorder:
    """A journal that keeps each event as a dict, with the named helpers of every journal."""

    def __init__(self):
        from mendwright.journal import _BaseJournal
        self.events = []
        self._base = _BaseJournal

    def event(self, name, /, **fields):
        self.events.append(dict(fields, event=name))

    def __getattr__(self, name):
        helper = getattr(self._base, name, None)
        if helper is None:
            raise AttributeError(name)
        return lambda *args, **fields: helper(self, *args, **fields)


class Proposer:
    """A provider that answers from ``answers`` in turn (then None), with last_error set to
    ``error`` when it answers None. ``unavailable`` stands in for the facade's no-provider
    stand-in."""

    def __init__(self, answers=(), error=None, unavailable=None):
        self.answers = list(answers)
        self.error = error
        self.asked = 0
        self.last_error = None
        if unavailable:
            self.unavailable_reason = unavailable

    def propose(self, bundle):
        self.asked += 1
        answer = self.answers.pop(0) if self.answers else None
        self.last_error = None if answer else self.error
        return answer

    def report_outcome(self, key, xpath, success):
        pass


class Closed:
    """Guardrails whose rate limit is used up."""

    def allow(self, key, provider=None, cost=0.0):
        return False


OTHER_PAGE = "<html><body><form><input type='checkbox' id='newsletter' name='news'></form></body></html>"


def _fp_of(html, xpath):
    return fingerprint.of_element(lxml.html.fromstring(html).xpath(xpath)[0])


def _declining(provider=None, *, guard=True, dry_run=False, tier=False, guardrails=None,
               disabled=False):
    c = HealCoordinator(FakeStore(), provider=provider or Proposer(),
                        html_provider=lambda: TERMS_HTML, inventory_provider=lambda: [],
                        guard=lambda: guard, dry_run=dry_run, journal=Recorder(),
                        fingerprint=tier, guardrails=guardrails, log=lambda line: None)
    c.disabled = disabled
    return c


def _declined(c):
    return [e for e in c.journal.events if e['event'] == 'heal.declined']


NO_PROVIDER = 'no model provider is configured (set ANTHROPIC_API_KEY).'


def _terms_fp():
    return _fp_of(TERMS_HTML, '//input')


DECLINES = [
    ('destructive', dict(), dict(meta=dict(META, destructive=True))),
    ('page_guard', dict(), dict(context={'page': '/checkout', 'lastPage': '/login'})),
    ('disabled', dict(provider=lambda: Proposer(unavailable=DISABLED_REASON), disabled=True),
     dict()),
    ('no_provider', dict(provider=lambda: Proposer(unavailable=NO_PROVIDER)), dict()),
    ('no_provider', dict(provider=lambda: Proposer(unavailable=NO_PROVIDER), tier=True),
     dict(meta=dict(META, expectedRole='unknown'))),
    ('no_provider', dict(provider=lambda: Proposer(unavailable=NO_PROVIDER), tier=True),
     dict(meta=dict(META, intent='the box that accepts the terms'), broken=[])),
    ('no_provider', dict(provider=lambda: Proposer(unavailable=NO_PROVIDER), tier=True),
     dict(meta=dict(META, fingerprint='other'))),
    ('role_unknown', dict(), dict(meta=dict(META, expectedRole='unknown'))),
    ('guard', dict(guard=False), dict()),
    ('rate_limit', dict(guardrails=Closed()), dict()),
    ('no_proposal', dict(provider=lambda: Proposer(error='Anthropic answered HTTP 500. Try '
                                                         'again later.')), dict()),
    ('no_proposal', dict(), dict()),
    ('gate', dict(provider=lambda: Proposer([{'xpath': "//main//input[@id='nope']"},
                                             {'xpath': "//main//input[@id='still-nope']"}])),
     dict()),
    ('dry_run', dict(provider=lambda: Proposer([{'xpath': TERMS_XPATH}]), dry_run=True), dict()),
    ('dry_run', dict(tier=True, dry_run=True), dict(meta=dict(META, fingerprint='terms'))),
]


@pytest.mark.parametrize('code, made, call', DECLINES,
                         ids=[code + '-' + str(i) for i, (code, _, _) in enumerate(DECLINES)])
def test_each_decline_writes_one_heal_declined_with_its_code(code, made, call):
    made = dict(made)
    if callable(made.get('provider')):
        made['provider'] = made['provider']()
    c = _declining(**made)
    meta = dict(call.get('meta', META))
    if meta.get('fingerprint') == 'other':
        meta['fingerprint'] = _fp_of(OTHER_PAGE, '//input')
    elif meta.get('fingerprint') == 'terms':
        meta['fingerprint'] = _terms_fp()
    broken = call.get('broken', [{'xpath': '#accept-terms'}])
    assert not c.try_heal('terms', meta, broken, call.get('context', {}), None)
    [event] = _declined(c)
    assert event == {'event': 'heal.declined', 'key': 'terms', 'code': code,
                     'reason': c.explain('terms')}
    assert event['reason']


def test_the_decline_codes_are_the_documented_ones():
    from mendwright.coordinator import DECLINE_CODES
    assert {code for code, _, _ in DECLINES} | {'not_here', 'list_empty', 'list_not_found',
                                                'get_by', 'frame', 'shadow', 'other'} == set(DECLINE_CODES)


def test_a_heal_that_applies_writes_no_decline():
    c = _declining(Proposer([{'xpath': TERMS_XPATH}]))
    assert c.try_heal('terms', META, [{'xpath': '#accept-terms'}], {}, None) == TERMS_XPATH
    assert _declined(c) == []
    assert [e['event'] for e in c.journal.events] == ['heal.requested', 'heal.proposed',
                                                      'heal.applied']


def test_a_list_and_a_caller_without_a_code_are_declined_too():
    c = _declining()
    reason = c.decline_list('cards', {'listFingerprint': {'count': 4}})
    c._decline('menu', 'some reason')
    assert [(e['key'], e['code'], e['reason']) for e in _declined(c)] == [
        ('cards', 'list_empty', reason), ('menu', 'other', 'some reason')]


def test_a_journal_without_the_declined_helper_still_gets_the_line():
    class Old:
        def __init__(self):
            self.lines = []

        def event(self, name, /, **fields):
            self.lines.append((name, fields))

    c = _declining()
    c.journal = Old()
    c.try_heal('terms', dict(META, destructive=True), [], {}, None)
    assert c.journal.lines == [('heal.declined', {'key': 'terms', 'code': 'destructive',
                                                  'reason': c.explain('terms')})]


def _not_here_after(provider):
    c = _declining(provider)
    assert not c.try_heal('terms', META, [{'xpath': '#accept-terms'}], {}, None)
    [event] = _declined(c)
    return event


def test_a_model_that_says_no_element_fits_is_not_here():
    from mendwright.providers._errors import Describer
    # The sentence the API providers build from a reply such as {"xpath": null, "reason": ...}.
    error = Describer('Anthropic').reply('{"xpath": null, "reason": "The terms box is not on '
                                         'this page."}')
    event = _not_here_after(Proposer(error=error))
    assert event['code'] == 'not_here' and 'no element on this page fits' in event['reason']


class _Ollama:
    """Ollama's /api/chat for LocalModelProvider: the model answers ``content``."""

    class _Resp:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    def __init__(self, content, logprobs=None):
        self.content = content
        self.logprobs = logprobs

    def post(self, url, *, json=None, headers=None, timeout=None):
        data = {'model': json['model'], 'done': True,
                'message': {'role': 'assistant', 'content': self.content}}
        if self.logprobs is not None:
            data['logprobs'] = self.logprobs
        return self._Resp(data)


def test_the_local_model_saying_no_element_fits_or_not_sure_is_not_here():
    import math

    from mendwright.providers import LocalModelProvider
    gone = _not_here_after(LocalModelProvider(http=_Ollama('{"pick": null, "why": "removed"}')))
    assert gone['code'] == 'not_here'
    assert 'the local model says no element on this page fits' in gone['reason']
    unsure = [{'token': '{"', 'logprob': 0.0}, {'token': 'pick', 'logprob': 0.0},
              {'token': '":', 'logprob': 0.0}, {'token': ' ', 'logprob': math.log(0.5)},
              {'token': '1', 'logprob': 0.0}, {'token': '}', 'logprob': 0.0}]
    event = _not_here_after(LocalModelProvider(http=_Ollama('{"pick": 1}', logprobs=unsure),
                                               min_confidence=0.8))
    assert event['code'] == 'not_here'
    assert 'the local model was not sure which element the step meant' in event['reason']
    # A reply it cannot read is not that: the model did not say the element is gone.
    event = _not_here_after(LocalModelProvider(http=_Ollama('no json here')))
    assert event['code'] == 'no_proposal'


def test_a_gateway_that_passes_the_models_answer_on_is_not_here():
    from mendwright.providers import GatewayProvider
    http = FakeHttp([FakeResp({'status': 'needs_human',
                               'reason': 'the model says no element on this page fits: it '
                                         'was removed.'})])
    event = _not_here_after(GatewayProvider('https://gw', 'k', http=http))
    assert event['code'] == 'not_here'
    http = FakeHttp([FakeResp({'status': 'needs_human', 'reason': 'rate limit'})])
    assert _not_here_after(GatewayProvider('https://gw', 'k', http=http))['code'] == 'no_proposal'


def test_a_list_looked_for_with_nonempty_and_not_found_is_list_not_found():
    c = _declining()
    outcome = c.propose_list('cards', {'expectedRole': 'listitem'}, [], {})
    assert outcome.kind == 'refused'
    outcome = c.propose_list('basket', {'expectedRole': 'listitem', 'destructive': True}, [], {})
    assert [(e['key'], e['code']) for e in _declined(c)] == [('cards', 'list_not_found'),
                                                              ('basket', 'destructive')]
    assert _declined(c)[1]['reason'] == outcome.reason == c.explain('basket')
