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
