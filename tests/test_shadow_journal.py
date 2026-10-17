"""What the journal and mendwright notify say about keys inside open shadow roots: a heal or a
follow refused for one is a heal.declined with its own code, every line has the run id, and
the run's summary words it as mendwright does, never with its hosts or selectors.

On the fakes of tests/conftest.py (ShadowPage and ShadowDriver), as in tests/test_shadow_heal.py.
"""

import json

import pytest

import mendwright
from mendwright import AttentionRequired, runsummary
from mendwright.coordinator import DECLINE_CODES
from mendwright.journal import Journal

from test_shadow_heal import CHAIN, HOST, LOOKALIKE, SAVE_V1, SAVE_V2, V1, page, shadow

RUN = 'ci-42'


def _open(target, tmp_path, **kwargs):
    kwargs.setdefault('provider', None)
    kwargs.setdefault('journal', Journal(tmp_path / 'journal.jsonl', run=RUN))
    return mendwright.open(target, store_path=str(tmp_path / 'o.json'), timeout_ms=0, **kwargs)


def _events(tmp_path):
    path = tmp_path / 'journal.jsonl'
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] \
        if path.exists() else []


def _declines(tmp_path):
    return [(e['key'], e['code'], e['reason']) for e in _events(tmp_path)
            if e['event'] == 'heal.declined']


def test_shadow_is_a_decline_code_with_its_own_phrase():
    assert 'shadow' in DECLINE_CODES
    entry = {'key': 'save', 'code': 'shadow', 'about': 'selector'}
    assert runsummary.phrase(entry) == 'is inside a shadow root, where mendwright could not repair it'
    assert runsummary.phrase(entry) != runsummary.PHRASES['other'][1]


@pytest.mark.parametrize('v2', [
    page(LOOKALIKE),
    page(LOOKALIKE + shadow(SAVE_V2) + shadow(SAVE_V2)),
    page(LOOKALIKE + shadow(SAVE_V2, mode='closed')),
], ids=['host-gone', 'two-hosts', 'closed-root'])
def test_a_shadow_root_that_cannot_be_entered_is_declined_as_shadow(make_shadow_page, tmp_path,
                                                                    stub_provider, v2):
    page_ = make_shadow_page(V1)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(v2)
    heal = _open(page_, tmp_path, provider=stub_provider("//button[@data-testid='save']"))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=0)
    [(key, code, reason)] = _declines(tmp_path)
    assert (key, code) == ('save', 'shadow')
    assert reason == heal.coordinator.explain('save') and reason in str(info.value)
    assert all(e.get('run') == RUN for e in _events(tmp_path))


def test_a_host_nothing_tells_apart_is_declined_as_shadow(make_shadow_page, tmp_path):
    two = page(LOOKALIKE + shadow(SAVE_V1, host='<div>') + '<div></div>')
    page_ = make_shadow_page(two)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(page(LOOKALIKE + shadow(SAVE_V2, host='<div>') + '<div></div>'))
    with pytest.raises(AttentionRequired, match='whose host nothing tells apart'):
        _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    assert [code for _, code, _ in _declines(tmp_path)] == ['shadow']


@pytest.mark.parametrize('mode', ['dry_run', 'disabled'])
def test_a_shadow_root_that_cannot_be_entered_is_declined_as_shadow_in_every_mode(
        make_shadow_page, tmp_path, monkeypatch, stub_provider, mode):
    # It would be refused with healing on as well, so it needs a human, as a key marked
    # destructive does; only a follow, which these modes stop, is declined with their code.
    page_ = make_shadow_page(V1)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(page(LOOKALIKE))         # the host is gone
    kwargs = {'dry_run': True} if mode == 'dry_run' else {}
    if mode == 'disabled':
        monkeypatch.setenv('MENDWRIGHT_DISABLE', '1')
    heal = _open(page_, tmp_path, provider=stub_provider("//button[@data-testid='save']"),
                 **kwargs)
    with pytest.raises(AttentionRequired, match='cannot enter that shadow root now'):
        heal.locator('save', '#save', role='button', poll_ms=0)
    assert [code for _, code, _ in _declines(tmp_path)] == ['shadow']
    summary = runsummary.summarize(_events(tmp_path))
    assert [(e['key'], e['code']) for e in summary['needs_human']] == [('save', 'shadow')]
    assert summary['not_repaired_on_purpose'] == {'dry_run': 0, 'disabled': 0}


@pytest.mark.parametrize('how, code', [
    ({'html': page(shadow(SAVE_V1) + shadow(SAVE_V1, host='<other-app>'))}, 'shadow'),
    ({'html': page(shadow(SAVE_V1 + SAVE_V1))}, 'shadow'),
    ({'open': {'destructive_keys': {'save'}}}, 'shadow'),
    ({'open': {'guard': lambda: False}}, 'shadow'),
    ({'open': {'dry_run': True}}, 'dry_run'),
    ({'env': 'MENDWRIGHT_DISABLE'}, 'disabled'),
], ids=['two-roots', 'two-elements', 'destructive', 'guard', 'dry-run', 'disabled'])
def test_a_key_not_followed_into_a_shadow_root_is_declined_with_a_code(
        make_shadow_driver, tmp_path, monkeypatch, how, code):
    if 'env' in how:
        monkeypatch.setenv(how['env'], '1')
    heal = _open(make_shadow_driver(how.get('html', V1)), tmp_path, **how.get('open', {}))
    with pytest.raises(AttentionRequired) as info:
        heal.locator('save', '#save', role='button', poll_ms=0)
    [(key, got, reason)] = _declines(tmp_path)
    assert (key, got) == ('save', code)
    assert reason == heal.coordinator.explain('save') and reason in str(info.value)


def test_the_summary_of_a_refused_shadow_heal_names_the_key_and_no_host(make_shadow_page,
                                                                         tmp_path):
    page_ = make_shadow_page(V1)
    _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    page_.set_html(page(LOOKALIKE))
    with pytest.raises(AttentionRequired):
        _open(page_, tmp_path).locator('save', '#save', role='button', poll_ms=0)
    events, _ = runsummary.select(_events(tmp_path), run=RUN)
    summary = runsummary.summarize(events)
    assert summary['needs_human'] == [{'key': 'save', 'code': 'shadow', 'about': 'selector'}]
    text = runsummary.message(summary, slack=True)
    assert ('1 needs a human: `save` is inside a shadow root, where mendwright could not '
            'repair it.') in text
    assert HOST not in text and CHAIN not in text and 'my-app' not in text


def test_a_follow_into_a_shadow_root_is_counted_as_one_and_carries_the_run(make_shadow_driver,
                                                                          tmp_path):
    driver = make_shadow_driver(V1)
    heal = _open(driver, tmp_path)
    heal.locator('save', '#save', role='button', poll_ms=0).click()
    assert heal.heals[-1].source == 'shadow'
    [applied] = [e for e in _events(tmp_path) if e['event'] == 'heal.applied']
    assert applied['run'] == RUN and applied['source'] == 'shadow' and applied['shadow'] == [HOST]
    events, _ = runsummary.select(_events(tmp_path), run=RUN)
    repaired = runsummary.summarize(events)['repaired']
    assert (repaired['total'], repaired['shadow'], repaired['model']) == (1, 1, 0)
    text = runsummary.message(runsummary.summarize(events))
    assert '1 found inside a shadow root' in text and HOST not in text
