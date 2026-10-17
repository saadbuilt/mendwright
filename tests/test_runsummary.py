"""mendwright.runsummary: which journal events are one run, what they add up to, and the
lines `mendwright notify` sends. Pure functions over lists of events: no file, no clock."""

from datetime import datetime, timezone

import pytest

from mendwright import runsummary
from mendwright.runsummary import ON_PURPOSE, PHRASES, message, select, summarize

T = '2026-10-16T05:0{}:00+00:00'
T1 = T.format(1)


def ev(name, key=None, run='gh-7.1', ts=T1, **fields):
    event = {'ts': ts, 'event': name, 'run': run}
    if key is not None:
        event['key'] = key
    event.update(fields)
    return event


def applied(key, xpath, run='gh-7.1', **fields):
    return ev('heal.applied', key, run, xpath=xpath, **fields)


def canary(key, xpath, success=True, run='gh-7.1'):
    return ev('heal.canary', key, run, xpath=xpath, success=success)


# --- select -------------------------------------------------------------------------------------

def test_a_run_is_its_exact_id():
    events = [ev('heal.requested', 'a', run='nightly-1'), ev('heal.requested', 'b', run='nightly-2'),
              ev('heal.requested', 'c', run=None)]
    picked, label = select(events, run='nightly-2')
    assert [e['key'] for e in picked] == ['b'] and label == 'run nightly-2'


def test_gh_without_an_attempt_is_its_highest_attempt():
    events = [ev('heal.requested', 'one', run='gh-7.1'), ev('heal.requested', 'two', run='gh-7.2'),
              ev('heal.requested', 'ten', run='gh-7.10'), ev('heal.requested', 'other', run='gh-70.3'),
              ev('heal.requested', 'bare', run='gh-7')]
    picked, label = select(events, run='gh-7')
    assert [e['key'] for e in picked] == ['ten'] and label == 'run gh-7.10'
    # An exact attempt is that attempt; a bare gh-<digits> with no attempt is itself.
    assert [e['key'] for e in select(events, run='gh-7.2')[0]] == ['two']
    assert [e['key'] for e in select([events[-1]], run='gh-7')[0]] == ['bare']


def test_an_id_with_no_events_is_a_run_that_recorded_nothing():
    picked, label = select([ev('heal.requested', 'a', run='gh-1.1')], run='gh-2')
    assert picked == [] and label == 'run gh-2'
    assert summarize(picked)['changed'] is False


def test_there_is_no_fallback_to_the_newest_run():
    with pytest.raises(ValueError):
        select([ev('heal.requested', 'a')])


@pytest.mark.parametrize('since', ['2026-10-16T05:02:00Z', '2026-10-16T05:02:00+00:00',
                                   '2026-10-16T07:02:00+02:00',
                                   datetime(2026, 10, 16, 5, 2, tzinfo=timezone.utc)])
def test_since_takes_the_events_at_or_after_a_time_with_a_zone(since):
    events = [ev('heal.requested', 'early', ts=T.format(1)), ev('heal.requested', 'on', ts=T.format(2)),
              ev('heal.requested', 'late', ts=T.format(3)), ev('heal.requested', 'bad', ts='yesterday'),
              ev('heal.requested', 'none', ts=None)]
    picked, label = select(events, since=since)
    assert [e['key'] for e in picked] == ['on', 'late']
    assert label == 'the events since 2026-10-16T05:02:00+00:00'


def test_a_time_without_a_zone_is_refused():
    for text in ('2026-10-16T05:02:00', 'yesterday', '', None):
        with pytest.raises(ValueError):
            runsummary.parse_time(text)


def test_all_is_every_event_and_select_reads_an_iterator_once():
    events = [ev('heal.requested', 'a', run=None), ev('heal.requested', 'b')]
    assert select(iter(events), all=True) == (events, 'the whole journal')
    assert [e['key'] for e in select(iter(events), run='gh-7')[0]] == ['b']


# --- summarize ------------------------------------------------------------------------------------

def test_repairs_by_tier_and_state():
    events = [
        applied('accept_terms', '//a', provider='fingerprint', source='fingerprint'),
        canary('accept_terms', '//a'),
        applied('email', '//e', provider='AnthropicProvider', model='m'),
        applied('search', '//s', provider='AnthropicProvider', source='intent'),
        canary('search', '//s', success=False), ev('heal.quarantine', 'search', xpath='//s'),
        applied('terms', 'css=#t', provider='frame', source='frame'),
        applied('save', 'my-app[id="root"] >> #save', provider='shadow', source='shadow',
                shadow=['my-app[id="root"]']),
        applied('accept_terms', '//a', provider='fingerprint', source='fingerprint'),  # again
    ]
    r = summarize(events)['repaired']
    assert r == {'total': 5, 'fingerprint': 1, 'model': 2, 'frame': 1, 'shadow': 1,
                 'confirmed': 1, 'not_confirmed': 3, 'quarantined': 1,
                 'keys': ['accept_terms', 'email', 'save', 'search', 'terms']}


def test_a_selector_found_inside_a_shadow_root_is_no_model_heal():
    # The follow of a key's own selector into a shadow root asks no model (source 'shadow').
    s = summarize([applied('save', 'my-app[id="root"] >> #save', provider='shadow',
                           source='shadow', shadow=['my-app[id="root"]'])])
    assert (s['repaired']['model'], s['repaired']['shadow']) == (0, 1)
    text = message(s)
    assert '1 selector repaired: 1 found inside a shadow root. 1 not confirmed: `save`.' in text
    assert 'by the model' not in text and 'my-app' not in text


def test_a_quarantine_wins_over_a_canary_success():
    events = [applied('k', '//x'), canary('k', '//x'), canary('k', '//x', success=False)]
    r = summarize(events)['repaired']
    assert (r['confirmed'], r['quarantined']) == (0, 1)


def test_a_canary_for_a_heal_of_another_run_counts_nothing():
    assert summarize([canary('k', '//x')])['repaired']['total'] == 0


def test_handled_cookie_banners_popups_and_interruptions():
    events = [ev('flow.interrupt', 'cookie_banner', preset='reject', before='email'),
              ev('flow.interrupt', 'cookie_banner', before='email', selector='#x'),
              ev('flow.interrupt', 'promo', before='email', selector='#close'),
              ev('flow.interrupt', 'relogin', before='search', outcome='failed', error='E'),
              ev('flow.dismiss', '<popup>', before='go', outcome='dismissed'),
              ev('flow.dismiss', '<popup>', before='go', outcome='failed')]
    s = summarize(events)
    assert s['handled'] == {'cookie_banners': 1, 'popups': 1, 'interruptions': 2}
    assert s['needs_human'] == [
        {'key': 'relogin', 'code': 'run_failed', 'about': 'interruption'},
        {'key': 'go', 'code': 'popup_failed', 'about': 'popup'}]


def test_handled_popups_alone_change_nothing():
    s = summarize([ev('flow.dismiss', '<popup>', before='go', outcome='dismissed')])
    assert s['changed'] is False and s['handled']['popups'] == 1


def test_every_source_of_needs_a_human():
    events = [ev('heal.declined', 'pay', code='destructive', reason='page text'),
              ev('heal.quarantine', 'email', xpath='//e'),
              ev('flow.stop', 'cookies', code='interruption_loop', before='email'),
              ev('flow.stop', '<popup>', code='popup_loop', before='go'),
              ev('flow.stop', '<next step>', code='step_cap', ran=40),
              ev('flow.stop', 'payment', code='step_again', page='/checkout/b'),
              ev('flow.dismiss', '<popup>', before='create', outcome='lost'),
              ev('flow.interrupt', 'session', before='search', outcome='failed'),
              ev('flow.step', 'gift', outcome='missing'),
              ev('heal.held', 'cart', xpath='//li', list=True),
              ev('heal.declined', 'x', code='a-code-from-the-future')]
    assert [(e['key'], e['code'], e['about']) for e in summarize(events)['needs_human']] == [
        ('pay', 'destructive', 'selector'), ('email', 'quarantined', 'selector'),
        ('cookies', 'interruption_loop', 'interruption'), ('go', 'popup_loop', 'popup'),
        (None, 'step_cap', 'run_steps'), ('payment', 'step_again', 'step'),
        ('create', 'popup_failed', 'popup'), ('session', 'run_failed', 'interruption'),
        ('gift', 'step_missing', 'step'), ('cart', 'list_proposed', 'selector'),
        ('x', 'other', 'selector')]


def test_one_entry_per_key_in_order_of_first_appearance_the_latest_code_winning():
    events = [ev('heal.declined', 'a', code='gate'), ev('heal.declined', 'b', code='guard'),
              ev('heal.declined', 'a', code='not_here')]
    assert [(e['key'], e['code']) for e in summarize(events)['needs_human']] == [
        ('a', 'not_here'), ('b', 'guard')]


def test_an_entry_is_dropped_when_a_later_heal_of_its_key_holds():
    events = [ev('heal.declined', 'a', code='page_guard'), applied('a', '//a'),
              ev('heal.declined', 'b', code='gate'), applied('b', '//b'),
              canary('b', '//b', success=False), ev('heal.quarantine', 'b', xpath='//b'),
              applied('c', '//c'), ev('heal.declined', 'c', code='gate')]
    # a healed after; b's later heal was quarantined; c's heal came before its decline.
    assert [(e['key'], e['code']) for e in summarize(events)['needs_human']] == [
        ('b', 'quarantined'), ('c', 'gate')]


def test_dry_run_and_disabled_are_counted_never_a_thing_for_a_human():
    events = [ev('heal.declined', 'a', code='dry_run'), ev('heal.declined', 'a', code='dry_run'),
              ev('heal.declined', 'b', code='dry_run'), ev('heal.declined', 'c', code='disabled')]
    s = summarize(events)
    assert s['needs_human'] == [] and s['changed'] is False
    assert s['not_repaired_on_purpose'] == {'dry_run': 2, 'disabled': 1}


def test_a_heal_quarantined_because_its_key_is_destructive_says_so():
    events = [ev('heal.canary', 'pay', xpath='//p', success=False, by='destructive'),
              ev('heal.quarantine', 'pay', xpath='//p'),
              applied('email', '//e'), ev('heal.canary', 'email', xpath='//e', success=False,
                                          by='expect'),
              ev('heal.quarantine', 'email', xpath='//e')]
    assert [(e['key'], e['code']) for e in summarize(events)['needs_human']] == [
        ('pay', 'destructive'), ('email', 'quarantined')]


def test_a_list_heal_quarantined_is_not_a_thing_for_a_human():
    s = summarize([ev('heal.quarantine', 'cart', xpath='//li', list=True)])
    assert s['needs_human'] == []


def test_a_code_that_is_not_a_string_is_other_and_never_raises():
    # A line another tool or a broken write left: a list or a dict is no key of PHRASES.
    events = [ev('heal.declined', 'a', code=['x']), ev('heal.declined', 'b', code={'y': 1}),
              ev('heal.declined', 'c', code=7), ev('heal.declined', 'd', code=['dry_run']),
              ev('flow.stop', 'e', code=['interruption_loop']), ev('flow.stop', 'f', code={})]
    s = summarize(events)
    assert [(e['key'], e['code']) for e in s['needs_human']] == [
        ('a', 'other'), ('b', 'other'), ('c', 'other'), ('d', 'other')]
    assert message(s).startswith('mendwright: the site changed.\n4 need a human: ')


# --- the message --------------------------------------------------------------------------------

EXAMPLE = [
    applied('accept_terms', '//a', provider='fingerprint', source='fingerprint'),
    canary('accept_terms', '//a'),
    applied('email', '//e', provider='fingerprint', source='fingerprint'),
    applied('search', '//s', provider='AnthropicProvider', model='claude-x'),
    canary('search', '//s'),
    ev('flow.interrupt', 'cookie_banner', preset='reject', before='email'),
    ev('flow.dismiss', '<popup>', before='go', outcome='dismissed'),
    ev('heal.declined', 'pay', code='destructive', reason="'pay' is marked ..."),
]


def test_the_message_reads_as_the_guide_shows_it():
    text = message(summarize(EXAMPLE), title='saadbuilt/shop', tests='success',
                   pr_url='https://github.com/saadbuilt/shop/pull/12',
                   run_url='https://github.com/saadbuilt/shop/actions/runs/11893412207')
    assert text == (
        'mendwright, saadbuilt/shop: the site changed.\n'
        '3 selectors repaired: 2 from fingerprints, 1 by the model. 2 confirmed, 1 not '
        'confirmed: `accept_terms`, `email`, `search`.\n'
        'Handled: 1 cookie banner, 1 popup.\n'
        '1 needs a human: `pay` is marked destructive, so mendwright did not repair it.\n'
        'The tests passed.\n'
        'Pull request: https://github.com/saadbuilt/shop/pull/12\n'
        'Run: https://github.com/saadbuilt/shop/actions/runs/11893412207')


def test_with_detail_counts_the_message_names_no_key():
    text = message(summarize(EXAMPLE + [ev('flow.step', 'gift', outcome='missing'),
                                        ev('flow.stop', '<popup>', code='popup_loop',
                                           before='checkout')]),
                   detail='counts', title='shop')
    for key in ('accept_terms', 'email', 'search', 'pay', 'gift', 'checkout'):
        assert key not in text
    assert ('3 need a human: a selector is marked destructive, so mendwright did not repair '
            'it; a step never showed, so the site may have dropped it; a popup came back each '
            'time it was dismissed.') in text
    assert '2 confirmed, 1 not confirmed.\n' in text


def test_subjects_and_plurals():
    s = summarize([applied('k', '//k', provider='fingerprint'),
                   ev('flow.stop', '<next step>', code='no_step', page='/x'),
                   ev('flow.stop', 'payment', code='step_moved', page='/b'),
                   ev('flow.stop', 'cookies', code='interruption_loop', before='email'),
                   ev('flow.dismiss', '<popup>', before='go', outcome='failed')])
    text = message(s)
    assert text.startswith('mendwright: the site changed.\n1 selector repaired: 1 from a '
                           'fingerprint. 1 not confirmed: `k`.\n')
    assert ('4 need a human: run_steps() stopped: no registered step showed; step `payment` '
            'showed on another page than before; interruption `cookies` came back each time '
            'it was handled; a popup in front of `go` could not be dismissed.') in text


def test_nothing_needs_a_human_and_the_other_lines():
    s = summarize([applied('k', '//k'), ev('heal.declined', 'd', code='dry_run'),
                   ev('heal.declined', 'e', code='disabled'),
                   ev('flow.interrupt', 'promo', before='go', selector='#x')])
    text = message(s, tests='failure')
    assert '\nHandled: 1 interruption.\n' in text
    assert '\nNothing needs a human.\n' in text
    assert '\nNot repaired on purpose: 1 in dry run, 1 with MENDWRIGHT_DISABLE=1.\n' in text
    assert text.endswith('\nThe tests failed. The journal records no heal or flow problem, so '
                         'the cause is something else.')
    assert message(s, tests='cancelled').endswith('\nThe tests were cancelled.')
    assert message(summarize([ev('heal.declined', 'p', code='guard')]),
                   tests='failure').endswith('\nThe tests failed.')


def test_nothing_changed():
    assert message(summarize([]), title='shop') == 'mendwright, shop: nothing changed.'


def test_every_code_has_a_phrase_and_a_subject():
    for code, (about, text) in PHRASES.items():
        assert about in ('selector', 'interruption', 'popup', 'step', 'run_steps'), code
        assert text and not text.endswith('.'), code


def test_every_decline_code_has_a_phrase_of_its_own_or_is_on_purpose():
    # A code without one would read as the bare 'was not repaired' of 'other'.
    from mendwright.coordinator import DECLINE_CODES
    for code in DECLINE_CODES:
        assert code in ON_PURPOSE or PHRASES[code][0] == 'selector', code


def test_keys_are_capped_cut_and_shown_safely():
    events = [applied('key' + str(n), '//x' + str(n)) for n in range(12)]
    text = message(summarize(events))
    # Sorted as text, so key10 and key11 come before key2; key8 and key9 are the 2 more.
    assert '`key6`, `key7`, and 2 more.' in text and '`key8`' not in text
    long = 'k' * 200
    weird = 'a`b\n‮c\x1b'
    text = message(summarize([applied(long, '//l'), applied(weird, '//w')]))
    assert '`' + 'k' * 79 + '…`' in text
    assert "`a'b \\u202Ec\\u001B`" in text
    assert '‮' not in text and '\x1b' not in text


def test_slack_escapes_what_would_notify_or_link():
    s = summarize([ev('heal.declined', '<!channel> & <@U123>', code='gate')])
    text = message(s, title='a&b', slack=True)
    assert '<' not in text and '>' not in text
    assert '`&lt;!channel&gt; &amp; &lt;@U123&gt;`' in text
    assert text.startswith('mendwright, a&amp;b: the site changed.')
    plain = message(s, title='a&b')
    assert '`<!channel> & <@U123>`' in plain


def test_the_whole_text_fits_3000_characters_by_naming_fewer_keys():
    events = [ev('heal.declined', 'needs-' + 'x' * 70 + str(n), code='gate') for n in range(40)]
    events += [applied('fixed-' + 'y' * 70 + str(n), '//f' + str(n)) for n in range(40)]
    text = message(summarize(events), run_url='https://e.example/run')
    assert len(text) <= 3000
    assert '40 need a human' in text and text.endswith('Run: https://e.example/run')
    short = message(summarize(events), limit=500)
    assert len(short) <= 500
    assert '40 need a human.' in short          # no entry named, but the count stays
