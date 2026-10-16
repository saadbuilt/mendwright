"""heal.drift() makes the decision a run makes, in the run's words.

Each case builds one store, copies it, runs heal.locator() on one copy and heal.drift() on
the other, on the same page. A refusal in the run (the AttentionRequired) must be the
refusal drift reports, a heal the run applies must be the one drift reports, and a run
that asks the provider must be drift's needs-model. Offline: conftest's FakePage and
StubProvider.
"""

import inspect
import shutil

import pytest

import mendwright
from mendwright import AttentionRequired
from mendwright.coordinator import HealCoordinator
from mendwright.drift import _SCORES, _MemoryJournal, _PreviewCoordinator
from mendwright.facade import _NullProvider
from mendwright.fingerprint import find, of_element
from mendwright.store import load_store

URL = 'https://shop.example/signup'
V1 = ("<form><label><input type='checkbox' id='terms' name='terms'> I accept the terms</label>"
      "<label><input type='checkbox' id='news' class='opt'> Send me the newsletter</label>"
      "<button type='button' id='pay'>Pay now</button></form>")
V2 = ("<form><label><input type='checkbox' name='terms' data-testid='legal'> I accept the "
      "terms</label>"
      "<label><input type='checkbox' class='opt'> Send me the newsletter</label>"
      "<label><input type='checkbox' class='opt'> Send me the newsletter</label>"
      "<button type='button' id='pay-now'>Pay now</button></form>")


def _fingerprint(selector, html=V1):
    import lxml.html
    doc = lxml.html.fromstring(html)
    return of_element(doc.xpath("//*[@id='" + selector.lstrip('#') + "']")[0])


def _store(tmp_path, key, selector, *, role='checkbox', destructive=False, page='/signup',
           fingerprint=True, quarantine=None):
    store = load_store(None, str(tmp_path / 'a' / 'o.json'))
    store.set_code_candidates(key, [selector])
    if role:
        store.set_meta(key, role=role)
    if destructive:
        store.set_meta(key, destructive=True)
    if fingerprint:
        store.set_meta(key, fingerprint=_fingerprint(selector))
    store.set_last_page(key, page)
    if quarantine:
        store.add_candidate(key, quarantine, source='heal')
        store.quarantine(key, quarantine)
    shutil.copytree(tmp_path / 'a', tmp_path / 'b')


def _run(make_page, tmp_path, side, provider, key, url=URL, **open_kw):
    page = make_page(V2, url=url, fingerprints=True)
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / side / 'o.json'),
                           **open_kw)
    if side == 'a':
        try:
            heal.locator(key, timeout_ms=0, poll_ms=0)
        except AttentionRequired as exc:
            return heal, str(exc).split('\n  -> ', 1)[1]
        return heal, None
    [k] = heal.drift([key], timeout_ms=0).keys
    return heal, k


CASES = {
    'destructive': dict(key='pay', selector='#pay', role='button', destructive=True),
    'page guard': dict(key='terms', selector='#terms', page='/checkout'),
    'no provider, role unknown': dict(key='terms', selector='#terms', role=None,
                                      provider=_NullProvider),
    'role unknown': dict(key='terms', selector='#terms', role=None),
    'guard': dict(key='terms', selector='#terms', open_kw={'guard': lambda: False}),
    'fingerprint, not confident, no provider': dict(key='news', selector='#news',
                                                    provider=_NullProvider),
    'fingerprint fails the gate': dict(key='terms', selector='#terms', provider=_NullProvider,
                                       quarantine=True),
    'disabled': dict(key='terms', selector='#terms', env={'MENDWRIGHT_DISABLE': '1'}),
}


@pytest.mark.parametrize('case', list(CASES))
def test_a_refusal_is_the_run_s_refusal(case, make_page, stub_provider, tmp_path, monkeypatch):
    spec = dict(CASES[case])
    for name, value in spec.pop('env', {}).items():
        monkeypatch.setenv(name, value)
    make_provider = spec.pop('provider', None)
    open_kw = spec.pop('open_kw', {})
    if spec.pop('quarantine', False):
        match = find(_fingerprint('#terms'), make_page(V2).content(), 'checkbox')
        assert match.xpath
        spec['quarantine'] = match.xpath
    _store(tmp_path, **spec)
    providers = {}

    def provider():
        made = make_provider() if make_provider else stub_provider(None)
        providers.setdefault('made', []).append(made)
        return made
    _heal, said = _run(make_page, tmp_path, 'a', provider(), spec['key'], **open_kw)
    _heal, k = _run(make_page, tmp_path, 'b', provider(), spec['key'], **open_kw)
    assert said, case
    assert k.verdict == 'refused', (case, k.reason)
    assert said in k.reason, (case, said, k.reason)
    assert all(getattr(p, 'bundles', []) == [] for p in providers['made'])


def test_a_heal_from_memory_is_the_run_s_heal(make_page, stub_provider, tmp_path):
    _store(tmp_path, 'terms', '#terms')
    run, said = _run(make_page, tmp_path, 'a', stub_provider(None), 'terms')
    assert said is None and run.heals[0].source == 'fingerprint'
    _heal, k = _run(make_page, tmp_path, 'b', stub_provider(None), 'terms')
    assert (k.verdict, k.detail) == ('would-heal', 'fingerprint')
    assert k.heal['selector'] == run.heals[0].new


def test_needs_model_is_a_run_that_asks_the_provider(make_page, stub_provider, tmp_path):
    _store(tmp_path, 'terms', '#terms', fingerprint=False)
    asked = stub_provider(None)
    _run(make_page, tmp_path, 'a', asked, 'terms')
    assert len(asked.bundles) == 1
    checked = stub_provider(None)
    _heal, k = _run(make_page, tmp_path, 'b', checked, 'terms')
    assert (k.verdict, k.provider) == ('needs-model', 'StubProvider')
    assert "'terms' has no fingerprint yet" in k.reason
    assert checked.bundles == [] and checked.outcomes == []


def test_the_preview_overrides_only_the_fingerprint_tier():
    overridden = sorted(name for name, value in vars(_PreviewCoordinator).items()
                        if not name.startswith('__') and hasattr(HealCoordinator, name))
    assert overridden == ['_fingerprint_tier']
    assert set(inspect.signature(_PreviewCoordinator._fingerprint_tier).parameters) == set(
        inspect.signature(HealCoordinator._fingerprint_tier).parameters)


def test_the_fingerprint_reasoning_drift_reads_is_the_coordinator_s(make_page, tmp_path):
    store = load_store(None, str(tmp_path / 'o.json'), read_only=True)
    store.set_code_candidates('terms', ['#terms'])
    store.set_meta('terms', role='checkbox', fingerprint=_fingerprint('#terms'))
    page = make_page(V2)
    journal = _MemoryJournal()
    coordinator = HealCoordinator(store, provider=_NullProvider(), html_provider=page.content,
                                  inventory_provider=lambda: [], guard=lambda: True,
                                  journal=journal, fingerprint=True)
    assert coordinator.try_heal('terms', store.meta('terms'), store.candidates('terms'), {})
    [proposed] = [e for e in journal.events if e['event'] == 'heal.proposed']
    assert _SCORES.fullmatch(proposed['reasoning'])


# --- the key of a heal.page.get_by_*() call, with heal_get_by=True ---------------------------

CALL = 'get_by_role("button", name="Pay now")'
PAY = "<form id='checkout'><button type='button' id='pay' class='primary'>Pay now</button></form>"
PAY_RENAMED = PAY.replace('>Pay now<', '>Pay<')          # the name changed, the id stayed
PAY_GONE = "<form id='checkout'><button type='button' class='go'>Go</button></form>"
# Two buttons named Pay now, and the one the saved heal finds.
PAY_TWINS = PAY_RENAMED.replace('</form>', "<button type='button'>Pay now</button>"
                                "<button type='button'>Pay now</button></form>")


def _call_store(make_page, stub_provider, tmp_path, *pages):
    """What a run with heal_get_by=True saves when it clicks the call on each of pages,
    copied to a and b."""
    page = make_page(pages[0], url=URL, fingerprints=True)
    heal = mendwright.open(page, provider=stub_provider(None),
                           store_path=str(tmp_path / 'a' / 'o.json'), heal_get_by=True,
                           timeout_ms=0)
    for html in pages:
        page.set_html(html)
        heal.page.get_by_role('button', name='Pay now').click()
    shutil.copytree(tmp_path / 'a', tmp_path / 'b')


def _call_run(make_page, tmp_path, side, html, provider):
    """On a, the run's click on the call (heal, the exception it raised or None); on b,
    heal.drift()'s verdict for the call's key (heal, KeyDrift)."""
    page = make_page(html, url=URL, fingerprints=True)
    heal = mendwright.open(page, provider=provider, store_path=str(tmp_path / side / 'o.json'),
                           heal_get_by=True, timeout_ms=0)
    if side == 'a':
        try:
            heal.page.get_by_role('button', name='Pay now').click()
        except Exception as exc:
            return heal, exc
        return heal, None
    [k] = heal.drift([CALL], timeout_ms=0).keys
    return heal, k


def test_a_call_heal_from_memory_is_the_run_s_heal(make_page, stub_provider, tmp_path):
    _call_store(make_page, stub_provider, tmp_path, PAY)
    run, raised = _call_run(make_page, tmp_path, 'a', PAY_RENAMED, stub_provider(None))
    assert raised is None and run.heals[0].source == 'fingerprint'
    _heal, k = _call_run(make_page, tmp_path, 'b', PAY_RENAMED, stub_provider(None))
    assert (k.verdict, k.detail) == ('would-heal', 'fingerprint')
    assert k.heal['selector'] == run.heals[0].new


def test_a_call_the_fingerprint_cannot_heal_is_the_run_s_refusal(make_page, stub_provider,
                                                                 tmp_path):
    _call_store(make_page, stub_provider, tmp_path, PAY)
    asked, checked = stub_provider("//button[@class='go']"), stub_provider("//button[@class='go']")
    _run_heal, raised = _call_run(make_page, tmp_path, 'a', PAY_GONE, asked)
    assert isinstance(raised, AttentionRequired)
    said = str(raised).split('\n  -> ', 1)[1]
    _heal, k = _call_run(make_page, tmp_path, 'b', PAY_GONE, checked)
    assert k.verdict == 'refused' and said in k.reason      # never needs-model: no model asked
    assert asked.bundles == [] and checked.bundles == []


def test_a_call_a_run_hands_to_playwright_is_refused_and_its_heal_unused(make_page, stub_provider,
                                                                        tmp_path):
    _call_store(make_page, stub_provider, tmp_path, PAY, PAY_RENAMED)       # a heal is saved
    run, raised = _call_run(make_page, tmp_path, 'a', PAY_TWINS, stub_provider(None))
    assert 'strict mode violation' in str(raised)        # Playwright's own error, no heal used
    assert run.heals == [] and run._page.actions == []
    _heal, k = _call_run(make_page, tmp_path, 'b', PAY_TWINS, stub_provider(None))
    assert (k.verdict, k.used) == ('refused', CALL)
    assert [p.result for p in k.probes] == ['matches 2 elements', 'not tried']
    assert 'a strict mode violation' in k.reason
