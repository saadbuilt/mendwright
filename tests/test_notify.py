"""mendwright notify: what it sends, where, and what it never prints. The webhook is a fake
behind notify.send()'s injectable http, or a local HTTP server on 127.0.0.1 for the urllib
path. No real webhook, no network beyond the loopback, no real clock or sleep."""

import http.server
import json
import threading

import pytest

import mendwright
from mendwright import cli, notify, runsummary

URL = 'https://hooks.slack.com/services/T000/B000/SeCrEtPaThToKeN'
PATH = '/services/T000/B000/SeCrEtPaThToKeN'
NOW = '2026-10-16T05:03:00+00:00'
RUN = 'gh-11893412207.1'

# Everything a journal can hold that must never reach a payload.
HEALED = "//input[@name='legal-consent' and contains(.,'Ada Lovelace')]"
OLD = '#accept-terms-SELECTOR'
REASON = 'the model says the page shows PAGE-TEXT-Ada'
POPUP = '<div role="dialog"> ("Get 10% off, POPUP-TEXT")'
LABEL = 'No thanks BUTTON-LABEL'
PAGE = '/account/PAGE-PATH-ada'
INTERRUPTION_SELECTOR = '#cookie-INTERRUPTION-SELECTOR'
NODE = 'tests/test_checkout.py::test_NODE_ID[ada@example.com]'
PROVIDER = 'AnthropicPROVIDER'
MODEL = 'claude-MODEL-name'
SECRETS = (HEALED, OLD, REASON, 'PAGE-TEXT', 'POPUP-TEXT', 'BUTTON-LABEL', 'PAGE-PATH',
           'INTERRUPTION-SELECTOR', 'NODE_ID', 'ada@example.com', 'PROVIDER', 'MODEL',
           'reasoning', 'overrides.json', 'journal.jsonl')
KEYS = ('accept_terms', 'search', 'pay', 'gift', 'cookies')


def _events():
    def e(name, key=None, **fields):
        out = {'ts': '2026-10-16T05:01:00+00:00', 'event': name, 'run': RUN}
        if key is not None:
            out['key'] = key
        out.update(fields)
        return out
    return [
        e('heal.requested', 'accept_terms'),
        e('heal.proposed', 'accept_terms', xpath=HEALED, reasoning=REASON, provider=PROVIDER,
          model=MODEL),
        e('heal.applied', 'accept_terms', xpath=HEALED, old=[OLD], provider=PROVIDER,
          model=MODEL, latency_ms=812),
        e('heal.canary', 'accept_terms', xpath=HEALED, success=True, by='test', test=NODE),
        e('heal.applied', 'search', xpath="//input[@name='q']", old=['#q'],
          provider='fingerprint', source='fingerprint'),
        e('heal.rejected', 'pay', xpath=HEALED, reason=REASON),
        e('heal.declined', 'pay', code='destructive', reason=REASON),
        e('flow.interrupt', 'cookies', before='accept_terms', selector=INTERRUPTION_SELECTOR),
        e('flow.dismiss', '<popup>', before='search', popup=POPUP, outcome='dismissed',
          how='close', label=LABEL, tried=[{'how': 'escape'}]),
        e('flow.page_guard', 'search', last_page=PAGE, page=PAGE),
        e('flow.step', 'gift', outcome='missing', page=PAGE),
        e('flow.stop', 'cookies', code='interruption_loop', before='accept_terms'),
    ]


def _write(tmp_path, events, folder='shop'):
    store = tmp_path / folder / '.mendwright' / 'overrides.json'
    store.parent.mkdir(parents=True)
    store.write_text('{"schema": 2, "selectors": {}}', encoding='utf-8')
    (store.parent / 'journal.jsonl').write_text(
        ''.join(json.dumps(ev) + '\n' for ev in events) + 'not json\n', encoding='utf-8')
    return store


class Hook:
    """The webhook: answers from queue ((status, body) or an exception), then 200."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.calls = []

    def post(self, url, body, headers, timeout):
        self.calls.append({'url': url, 'body': body, 'headers': headers, 'timeout': timeout})
        answer = self.queue.pop(0) if self.queue else (200, b'ok')
        if isinstance(answer, BaseException):
            raise answer
        return answer


def run_notify(argv, hook=None, sleeps=None):
    args = cli.build_parser().parse_args(['notify'] + argv)
    sleeps = [] if sleeps is None else sleeps
    return cli.cmd_notify(args, http=hook or Hook(), sleep=sleeps.append, clock=lambda: NOW)


@pytest.fixture
def store(tmp_path):
    return _write(tmp_path, _events())


def _body(hook, n=0):
    return json.loads(hook.calls[n]['body'].decode('utf-8'))


# --- what is sent ---------------------------------------------------------------------------

@pytest.mark.parametrize('fmt', ['slack', 'json'])
@pytest.mark.parametrize('detail', ['keys', 'counts'])
def test_a_payload_holds_no_page_text_selector_reason_or_name(store, monkeypatch, capsys, fmt,
                                                              detail):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    assert run_notify(['--store', str(store), '--run', 'gh-11893412207', '--format', fmt,
                       '--detail', detail, '--tests', 'failure'], hook) == 0
    [call] = hook.calls
    sent = call['body'].decode('utf-8')
    for secret in SECRETS:
        assert secret not in sent, secret
    for key in KEYS:
        assert (key in sent) is (detail == 'keys'), key
    assert 'mendwright' in sent and 'the site changed' in sent
    out = capsys.readouterr()
    assert out.out == ('mendwright notify: sent the summary of run ' + RUN + ' to '
                       'https://hooks.slack.com (HTTP 200).\n')


def test_the_slack_payload_is_the_message(store, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN, '--title', 'saadbuilt/shop',
                '--tests', 'success', '--pr-url', 'https://github.com/saadbuilt/shop/pull/12',
                '--run-url', 'https://github.com/saadbuilt/shop/actions/runs/11893412207'], hook)
    assert _body(hook) == {'text': (
        'mendwright, saadbuilt/shop: the site changed.\n'
        '2 selectors repaired: 1 from a fingerprint, 1 by the model. 1 confirmed, 1 not '
        'confirmed: `accept_terms`, `search`.\n'
        'Handled: 1 popup, 1 other interruption.\n'
        '3 need a human: `pay` is marked destructive, so mendwright did not repair it; step '
        '`gift` never showed, so the site may have dropped it; interruption `cookies` came back '
        'each time it was handled.\n'
        'The tests passed.\n'
        'Pull request: https://github.com/saadbuilt/shop/pull/12\n'
        'Run: https://github.com/saadbuilt/shop/actions/runs/11893412207')}


def test_the_json_payload_has_its_schema(store, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN, '--format', 'json', '--title',
                'saadbuilt/shop', '--tests', 'success'], hook)
    data = _body(hook)
    text = data.pop('text')
    assert text.startswith('mendwright, saadbuilt/shop: the site changed.\n')
    assert data == {
        'schema': 1, 'mendwright': mendwright.__version__, 'title': 'saadbuilt/shop', 'run': RUN,
        'run_url': None, 'pr_url': None, 'tests': 'success', 'changed': True,
        'repaired': {'total': 2, 'fingerprint': 1, 'model': 1, 'frame': 0, 'shadow': 0, 'confirmed': 1,
                     'not_confirmed': 1, 'quarantined': 0, 'keys': ['accept_terms', 'search'],
                     'more_keys': 0},
        'handled': {'cookie_banners': 0, 'popups': 1, 'interruptions': 1},
        'needs_human': [
            {'key': 'pay', 'code': 'destructive', 'about': 'selector',
             'text': 'is marked destructive, so mendwright did not repair it'},
            {'key': 'gift', 'code': 'step_missing', 'about': 'step',
             'text': 'never showed, so the site may have dropped it'},
            {'key': 'cookies', 'code': 'interruption_loop', 'about': 'interruption',
             'text': 'came back each time it was handled'}],
        'more_needs_human': 0, 'not_repaired_on_purpose': {'dry_run': 0, 'disabled': 0},
        'sent_at': NOW}
    assert '`' not in json.dumps(data['repaired']['keys'])
    # JSON receivers get the text as it is, without Slack's entities.
    assert '&amp;' not in text


def test_with_detail_counts_the_json_names_no_key(store, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN, '--format', 'json', '--detail', 'counts'],
               hook)
    data = _body(hook)
    assert data['repaired']['keys'] == []
    assert all('key' not in entry for entry in data['needs_human'])
    for key in KEYS:
        assert key not in json.dumps(data), key


def test_the_json_payload_is_cut_to_16_kib(tmp_path, monkeypatch):
    events = []
    for n in range(400):
        key = 'k' * 70 + str(n)
        events.append({'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.applied', 'run': RUN,
                       'key': key, 'xpath': '//x' + str(n)})
        events.append({'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.declined', 'run': RUN,
                       'key': 'n' * 70 + str(n), 'code': 'gate'})
    store = _write(tmp_path, events)
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN, '--format', 'json'], hook)
    body = hook.calls[0]['body']
    assert len(body) <= 16 * 1024
    data = json.loads(body)
    assert data['repaired']['total'] == 400
    assert len(data['repaired']['keys']) + data['repaired']['more_keys'] == 400
    assert len(data['needs_human']) + data['more_needs_human'] == 400
    assert data['repaired']['more_keys'] > 0 and data['more_needs_human'] > 0
    assert len(data['text']) <= 3000


def test_slack_text_escapes_mentions_and_links(tmp_path, monkeypatch):
    store = _write(tmp_path, [{'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.declined',
                               'run': RUN, 'key': '<!channel> <@U123> <https://e.example|x>',
                               'code': 'gate'}])
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN], hook)
    text = _body(hook)['text']
    assert '<' not in text and '>' not in text
    assert '`&lt;!channel&gt; &lt;@U123&gt; &lt;https://e.example|x&gt;`' in text


# --- when it sends -------------------------------------------------------------------------

def _journal_of(tmp_path, kind):
    def e(name, **fields):
        return dict({'ts': '2026-10-16T05:01:00+00:00', 'event': name, 'run': RUN}, **fields)
    events = {'nothing': [],
              'popups': [e('flow.dismiss', key='<popup>', before='go', outcome='dismissed')],
              'repaired': [e('heal.applied', key='k', xpath='//k')],
              'human': [e('heal.declined', key='pay', code='destructive')]}[kind]
    return _write(tmp_path, events or [e('heal.requested', key='k')], folder=kind)


@pytest.mark.parametrize('when, kind, tests, sent', [
    ('changes', 'nothing', None, False), ('changes', 'popups', None, False),
    ('changes', 'repaired', None, True), ('changes', 'human', None, True),
    ('changes', 'nothing', 'failure', False),
    ('attention', 'repaired', 'success', False), ('attention', 'repaired', None, False),
    ('attention', 'human', 'success', True), ('attention', 'nothing', 'failure', True),
    ('attention', 'nothing', 'cancelled', True),
    ('always', 'nothing', None, True), ('always', 'popups', 'success', True),
])
def test_when(tmp_path, monkeypatch, capsys, when, kind, tests, sent):
    store = _journal_of(tmp_path, kind)
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    argv = ['--store', str(store), '--run', RUN, '--when', when, '--fail-on-error']
    assert run_notify(argv + (['--tests', tests] if tests else []), hook) == 0
    assert bool(hook.calls) is sent
    out = capsys.readouterr().out
    if not sent:
        assert out.startswith('mendwright notify: nothing to send for run ' + RUN + ': ')
        assert out.endswith(' (--when ' + when + ').\n')
    if when == 'always' and kind == 'nothing':
        assert _body(hook)['text'] == 'mendwright, nothing: nothing changed.'


def test_the_when_messages(tmp_path, monkeypatch, capsys):
    store = _journal_of(tmp_path, 'repaired')
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    run_notify(['--store', str(store), '--run', RUN, '--when', 'attention', '--tests', 'success'])
    assert capsys.readouterr().out == (
        'mendwright notify: nothing to send for run ' + RUN + ': nothing needs a human, and the '
        'tests passed (--when attention).\n')
    store = _journal_of(tmp_path, 'popups')
    run_notify(['--store', str(store), '--run', RUN])
    assert capsys.readouterr().out == (
        'mendwright notify: nothing to send for run ' + RUN + ': nothing was repaired and '
        'nothing needs a human (--when changes).\n')


# --- which run --------------------------------------------------------------------------------

def test_by_default_the_run_is_the_one_the_environment_gives(tmp_path, monkeypatch):
    events = [{'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.applied', 'run': run,
               'key': key, 'xpath': '//' + key} for run, key in
              (('gh-5.1', 'first'), ('gh-5.2', 'second'), ('gh-6.1', 'other'),
               ('nightly-9', 'nightly'))]
    store = _write(tmp_path, events)
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_RUN_ID', '5')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')       # this job; the tests' attempt was 2
    hook = Hook()
    run_notify(['--store', str(store)], hook)
    assert '`second`' in _body(hook)['text'] and '`first`' not in _body(hook)['text']
    monkeypatch.setenv('MENDWRIGHT_RUN_ID', 'nightly-9')
    run_notify(['--store', str(store)], hook)
    assert '`nightly`' in _body(hook, 1)['text']
    run_notify(['--store', str(store), '--all'], hook)
    assert '4 selectors repaired' in _body(hook, 2)['text']


def test_no_run_is_refused_and_says_why(store, monkeypatch, capsys):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    assert run_notify(['--store', str(store)], hook) == 0
    assert run_notify(['--store', str(store), '--fail-on-error'], hook) == 1
    assert hook.calls == []
    err = capsys.readouterr().err
    assert err == ('mendwright notify: no run to summarize. Pass --run, --since or --all, or set '
                   'MENDWRIGHT_RUN_ID where the tests and this command run.\n') * 2
    monkeypatch.setenv('MENDWRIGHT_RUN_ID', 'not valid!')
    run_notify(['--store', str(store)], hook)
    assert ("set MENDWRIGHT_RUN_ID where the tests and this command run. MENDWRIGHT_RUN_ID must "
            "be 1 to 100 letters") in capsys.readouterr().err
    assert hook.calls == []


def test_a_journal_without_run_ids_is_said_not_taken_for_an_empty_run(tmp_path, monkeypatch,
                                                                       capsys):
    store = _write(tmp_path, [{'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.applied',
                               'key': 'k', 'xpath': '//k'}])
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    assert run_notify(['--store', str(store), '--run', 'gh-5', '--when', 'always',
                       '--fail-on-error'], hook) == 1
    assert hook.calls == []
    assert capsys.readouterr().err.startswith('mendwright notify: no event in ')
    # A run whose id has no events, in a journal that has run ids, is a run that recorded
    # nothing.
    store = _write(tmp_path, [{'ts': '2026-10-16T05:01:00+00:00', 'event': 'heal.applied',
                               'run': 'gh-4.1', 'key': 'k', 'xpath': '//k'}], folder='other')
    assert run_notify(['--store', str(store), '--run', 'gh-5', '--when', 'always'], hook) == 0
    assert _body(hook)['text'] == 'mendwright, other: nothing changed.'


def test_no_journal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    missing = tmp_path / 'none' / 'overrides.json'
    assert run_notify(['--store', str(missing), '--run', RUN]) == 0
    assert run_notify(['--store', str(missing), '--run', RUN, '--fail-on-error']) == 1
    assert capsys.readouterr().err == (
        'mendwright notify: no journal at ' + str(tmp_path / 'none' / 'journal.jsonl') + ', so '
        'there is nothing to summarize. Run your tests with mendwright first, or pass --store.\n'
    ) * 2


# --- the URL ----------------------------------------------------------------------------------

def test_dry_run_prints_the_payload_and_reads_no_variable(store, monkeypatch, capsys):
    import os
    read = set()

    class Watched(dict):
        def get(self, key, default=None):
            read.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            read.add(key)
            return super().__getitem__(key)

    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    monkeypatch.setattr(os, 'environ', Watched(os.environ))
    hook = Hook()
    assert run_notify(['--store', str(store), '--run', RUN, '--format', 'json', '--dry-run'],
                      hook) == 0
    assert hook.calls == [] and 'MENDWRIGHT_WEBHOOK_URL' not in read
    out = capsys.readouterr().out
    assert json.loads(out)['run'] == RUN and 'SeCrEt' not in out


@pytest.mark.parametrize('url, ok', [
    ('https://hooks.slack.com/services/x', True), ('https://[2001:db8::1]/x', True),
    ('http://localhost:8080/hook', True), ('http://127.0.0.1/hook', True),
    ('http://[::1]:9000/hook', True),
    ('http://hooks.slack.com/services/x', False), ('http://localhost.evil.example/x', False),
    ('ftp://example.com/x', False), ('https:///no-host', False), ('hooks.slack.com/x', False),
    ('https://h.example/a b', False), ('https://h.example/x\n', True),   # stripped first
    ('https://h.example:99999/x', False), ('javascript:alert(1)', False),
])
def test_the_url_must_be_https_or_a_local_http(tmp_path, monkeypatch, capsys, url, ok):
    store = _journal_of(tmp_path, 'repaired')
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', url)
    hook = Hook()
    code = run_notify(['--store', str(store), '--run', RUN, '--fail-on-error'], hook)
    assert (code, bool(hook.calls)) == ((0, True) if ok else (1, False))
    out = capsys.readouterr()
    if not ok:
        why = notify.url_problem(url.strip())
        assert out.err == ('mendwright notify: the webhook URL in MENDWRIGHT_WEBHOOK_URL must '
                           'start with https:// (http:// only for localhost)'
                           + ('' if why == notify.NOT_HTTPS else ' (' + why + ')')
                           + ', so nothing was sent.\n')
        assert 'h.example' not in out.err and 'hooks' not in out.err


def test_why_a_url_is_refused():
    assert [notify.url_problem(u) for u in (
        'http://hooks.slack.com/x', 'hooks.slack.com/x', 'ftp://h/x', 'https:///x',
        'https://h.example/a b', 'https://h.example:0/x', 'https://h.example:99999/x', '')] == [
        notify.NOT_HTTPS, notify.NOT_HTTPS, notify.NOT_HTTPS, 'it has no host',
        'it holds a space or a control character', 'its port is not a number from 1 to 65535',
        'it is not a URL', 'it is empty']


def test_an_unset_url_sends_nothing(store, capsys):
    assert run_notify(['--store', str(store), '--run', RUN]) == 0
    assert run_notify(['--store', str(store), '--run', RUN, '--fail-on-error']) == 1
    assert capsys.readouterr().err == ('mendwright notify: MENDWRIGHT_WEBHOOK_URL is not set, so '
                                       'nothing was sent.\n') * 2


@pytest.mark.parametrize('answers, err, tries, slept', [
    ([(404, b'no_service ' + URL.encode() + b' \x1b[31m')],
     'answered HTTP 404 (no_service *** [31m). Nothing was sent. Check the URL in '
     'MENDWRIGHT_WEBHOOK_URL.', 1, []),
    ([(400, b'invalid_payload')], 'answered HTTP 400 (invalid_payload). Nothing was sent. Check '
     'the URL in MENDWRIGHT_WEBHOOK_URL.', 1, []),
    ([(500, PATH.encode()), (500, b'still down')],
     'answered HTTP 500 (still down) (tried 2 times). Nothing was sent.', 2, [2.0]),
    ([(503, b''), (503, b'')], 'answered HTTP 503 (tried 2 times). Nothing was sent.', 2, [2.0]),
    ([(302, b'')], 'answered HTTP 302, a redirect, which mendwright does not follow. Nothing was '
     'sent. Check the URL in MENDWRIGHT_WEBHOOK_URL.', 1, []),
    ([TimeoutError('timed out'), TimeoutError('timed out')],
     None, 2, [2.0]),
    ([ConnectionError('its host name could not be resolved')] * 2, None, 2, [2.0]),
])
def test_a_failed_webhook_says_why_and_never_prints_the_url(store, monkeypatch, capsys, answers,
                                                            err, tries, slept):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook, sleeps = Hook(*answers), []
    assert run_notify(['--store', str(store), '--run', RUN, '--timeout', '7'], hook, sleeps) == 0
    assert len(hook.calls) == tries and sleeps == slept
    out = capsys.readouterr()
    assert out.out == ''
    if err:
        assert out.err == 'mendwright notify: the webhook at https://hooks.slack.com ' + err + '\n'
    elif isinstance(answers[0], TimeoutError):
        assert out.err == ('mendwright notify: could not reach the webhook at '
                           'https://hooks.slack.com (timed out after 7 s, tried 2 times). '
                           'Nothing was sent.\n')
    else:
        assert out.err == ('mendwright notify: could not reach the webhook at '
                           'https://hooks.slack.com (its host name could not be resolved, '
                           'tried 2 times). Nothing was sent.\n')
    assert 'SeCrEt' not in out.out + out.err and 'T000' not in out.out + out.err
    assert run_notify(['--store', str(store), '--run', RUN, '--fail-on-error'],
                      Hook(*answers), []) == 1


def test_429_is_tried_once_more_and_a_retry_that_works_is_sent(store, monkeypatch, capsys):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook, sleeps = Hook((429, b'rate limited')), []
    assert run_notify(['--store', str(store), '--run', RUN, '--fail-on-error'], hook, sleeps) == 0
    assert len(hook.calls) == 2 and sleeps == [2.0]
    assert capsys.readouterr().out.startswith('mendwright notify: sent the summary of run ')


def test_the_request_has_its_headers_and_timeout(store, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL + '\n')     # a secret pasted with a newline
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN, '--timeout', '12'], hook)
    [call] = hook.calls
    assert call['url'] == URL
    assert call['headers'] == {'User-Agent': 'mendwright/' + mendwright.__version__,
                               'Content-Type': 'application/json'}
    assert call['timeout'] == 12.0


@pytest.mark.parametrize('value', ['0', '31', 'ten', '-1'])
def test_bad_arguments_are_a_usage_error(store, value, capsys):
    assert cli.main(['notify', '--store', str(store), '--timeout', value]) == 2
    assert cli.main(['notify', '--run', 'has space']) == 2
    assert cli.main(['notify', '--since', '2026-10-16T05:00:00']) == 2     # no time zone
    assert cli.main(['notify', '--run', RUN, '--all']) == 2
    assert cli.main(['notify', '--pr-url', 'http://github.com/x/pull/1']) == 2
    assert cli.main(['notify', '--tests', 'ok']) == 2
    assert cli.main(['notify', '--format', 'teams']) == 2


def test_on_github_actions_each_error_is_also_a_warning(store, monkeypatch, capsys):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook((404, b'100% gone\nnext line'))
    run_notify(['--store', str(store), '--run', RUN], hook)
    out = capsys.readouterr()
    assert out.out == '' and out.err.startswith('mendwright notify: the webhook at ')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    run_notify(['--store', str(store), '--run', RUN], Hook((404, b'100% gone\nnext line')))
    out = capsys.readouterr()
    line = out.err.rstrip('\n')
    assert out.out == ('::warning title=mendwright notify::' + line.replace('%', '%25') + '\n')
    assert '100%25 gone next line' in out.out


def test_the_default_title_and_run_url_on_github_actions(store, monkeypatch):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', URL)
    hook = Hook()
    run_notify(['--store', str(store), '--run', RUN], hook)
    assert _body(hook)['text'].startswith('mendwright, shop: ')     # the store's project folder
    for name, value in (('GITHUB_ACTIONS', 'true'), ('GITHUB_REPOSITORY', 'saadbuilt/shop'),
                        ('GITHUB_SERVER_URL', 'https://github.com'), ('GITHUB_RUN_ID', '42')):
        monkeypatch.setenv(name, value)
    run_notify(['--store', str(store), '--run', RUN], hook)
    text = _body(hook, 1)['text']
    assert text.startswith('mendwright, saadbuilt/shop: ')
    assert text.endswith('\nRun: https://github.com/saadbuilt/shop/actions/runs/42')


# --- send() itself, and urllib against a local server ----------------------------------------------

def test_send_never_raises_for_a_broken_http():
    class Broken:
        def post(self, *args):
            raise RuntimeError('boom')
    result = notify.send(URL, b'{}', http=Broken(), sleep=lambda s: None)
    assert (result.ok, result.error, result.attempts) == (False, 'connection', 1)


def test_mask_takes_the_url_its_path_and_query_out():
    url = 'https://user:pw@hooks.example/p/a/t/h?token=abc'
    text = notify.mask('x ' + url + ' /p/a/t/h token=abc user:pw', url)
    assert text == 'x *** *** *** ***'
    assert notify.mask('a / b', 'https://h.example/') == 'a / b'   # a path of one character stays


class _Server(http.server.BaseHTTPRequestHandler):
    answers = []
    seen = []

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.seen.append({'path': self.path, 'headers': dict(self.headers),
                          'body': self.rfile.read(length)})
        status, headers = self.answers.pop(0) if self.answers else (200, {})
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    _Server.answers, _Server.seen = [], []
    httpd = http.server.HTTPServer(('127.0.0.1', 0), _Server)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield 'http://127.0.0.1:' + str(httpd.server_address[1])
    httpd.shutdown()
    httpd.server_close()


def test_urllib_posts_to_a_local_server_and_follows_no_redirect(server, store, monkeypatch,
                                                               capsys):
    monkeypatch.setenv('MENDWRIGHT_WEBHOOK_URL', server + '/hook/SeCrEt')
    assert cli.main(['notify', '--store', str(store), '--run', RUN, '--timeout', '5']) == 0
    [seen] = _Server.seen
    assert seen['path'] == '/hook/SeCrEt'
    assert seen['headers']['User-Agent'] == 'mendwright/' + mendwright.__version__
    assert seen['headers']['Content-Type'] == 'application/json'
    assert json.loads(seen['body'])['text'].startswith('mendwright, shop: the site changed.')
    assert capsys.readouterr().out == ('mendwright notify: sent the summary of run ' + RUN
                                       + ' to ' + server + ' (HTTP 200).\n')
    _Server.answers = [(302, {'Location': server + '/elsewhere'})]
    assert cli.main(['notify', '--store', str(store), '--run', RUN, '--fail-on-error']) == 1
    assert [s['path'] for s in _Server.seen] == ['/hook/SeCrEt', '/hook/SeCrEt']   # not followed
    err = capsys.readouterr().err
    assert 'answered HTTP 302, a redirect' in err and 'SeCrEt' not in err


def test_urllib_says_a_refused_connection_without_the_url():
    import socket
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    result = notify.send('http://127.0.0.1:' + str(port) + '/x/SeCrEt', b'{}', timeout=1,
                         sleep=lambda s: None)
    # Refused at once on Linux and macOS; Windows tries again until the timeout.
    assert (result.ok, result.attempts) == (False, 2)
    assert result.error in ('connection', 'timeout')
    assert 'SeCrEt' not in result.detail


def test_a_summary_holds_nothing_from_the_events_but_keys_and_codes():
    summary = runsummary.summarize(_events())
    flat = json.dumps(summary)
    for secret in SECRETS:
        assert secret not in flat, secret


def test_no_option_takes_the_webhook_url():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == 'command')
    options = {o for a in sub.choices['notify']._actions for o in a.option_strings}
    assert {o for o in options if 'url' in o} == {'--run-url', '--pr-url'}
    assert not any('webhook' in o or 'hook' in o for o in options)


def test_urllib_verifies_tls_with_the_default_context(monkeypatch):
    import ssl
    import urllib.request
    seen = []

    def build_opener(*handlers):
        seen.extend(handlers)
        raise RuntimeError('stop here')
    monkeypatch.setattr(urllib.request, 'build_opener', build_opener)
    with pytest.raises(RuntimeError):
        notify.UrllibHttp().post(URL, b'{}', {}, 5)
    [https] = [h for h in seen if isinstance(h, urllib.request.HTTPSHandler)]
    assert https._context.verify_mode == ssl.CERT_REQUIRED and https._context.check_hostname
    assert notify._NoRedirect in seen
