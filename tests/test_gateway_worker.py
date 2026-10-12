"""The reference gateway (examples/heal_gateway.mjs) against the Python core.

These tests run the worker module under node, so they are skipped when node is not on
PATH. They stay offline: the worker's global fetch is replaced with a stub that records
the request and returns a canned model reply, and no KV binding is passed, so no clock or
random value is read.

Checked: buildPrompt() produces exactly what mendwright.prompt.build_prompt() produces,
parseProposal() matches parse_proposal(), the model id comes only from MODEL (or the older
ANTHROPIC_MODEL / OPENAI_MODEL), and a missing model is a clear HTTP 500.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from mendwright.prompt import build_prompt, not_here_reason, parse_proposal

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKER = ROOT / 'examples' / 'heal_gateway.mjs'
NODE = shutil.which('node')

needs_node = pytest.mark.skipif(NODE is None, reason='node is not installed')

RUNNER = r"""
import worker, { buildPrompt, notHereReason, parseProposal } from '__WORKER__';

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const input = JSON.parse(Buffer.concat(chunks).toString('utf8'));

const out = {
  prompts: input.bundles.map(buildPrompt),
  proposals: input.texts.map(parseProposal),
  notHere: input.texts.map(notHereReason),
  responses: [],
};
for (const c of input.requests) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body) });
    return new Response(JSON.stringify(c.reply), { status: 200 });
  };
  const request = new Request('https://gw.example' + c.path, {
    method: 'POST',
    headers: { 'X-Heal-Key': c.key, 'content-type': 'application/json' },
    body: JSON.stringify(c.body),
  });
  const res = await worker.fetch(request, c.env);
  out.responses.push({ status: res.status, body: await res.json(), calls });
}
process.stdout.write(JSON.stringify(out));
"""


def _run_worker(tmp_path, *, bundles=(), texts=(), requests=()):
    runner = tmp_path / 'runner.mjs'
    runner.write_text(RUNNER.replace('__WORKER__', WORKER.as_uri()), encoding='utf-8')
    payload = json.dumps({'bundles': list(bundles), 'texts': list(texts),
                          'requests': list(requests)})
    proc = subprocess.run([NODE, str(runner)], input=payload, capture_output=True,
                          encoding='utf-8', timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


BUNDLES = [
    {'key': 'terms', 'expectedRole': 'checkbox', 'expectedText': [],
     'candidates': ['//a', '//b'], 'inventory': [], 'html': '<form></form>'},
    {'key': 'pay', 'expectedRole': 'button', 'destructive': True,
     'expectedText': ['Pay', 'Zahlungspflichtig bestellen', 'Bestätigen'],
     'candidates': ["//button[@id='pay']"], 'html': '<button>Pay</button>'},
    {'key': 'buy', 'expectedRole': 'button', 'expectedText': ['Kaufen'],
     'inventory': [{'tag': 'button', 'text': 'Kaufen ü "now" \\ \t \x7f ☃ \U0001F600',
                    'n': 3, 'ok': True, 'none': None, 'nested': {'a': [1, 'b']}}],
     'html': '<p>über \U0001F600</p>'},
    {'key': 'k', 'expectedRole': 'link', 'rejectReason': 'not unique (2)'},
    {'key': 'k', 'expectedRole': 'link', 'rejectedXpath': '//a', 'rejectReason': 'no match',
     'candidates': []},
    {'key': 'long', 'expectedRole': 'input', 'html': '\U0001F600' * 10 + 'a' * 70000,
     'inventory': [{'text': 'x' * 70000}]},
    {'key': 'bare', 'expectedRole': 'button'},
    {'key': 'falsy', 'expectedRole': 'button', 'inventory': {}, 'expectedText': None,
     'destructive': 0, 'html': '', 'rejectReason': ''},
    {'key': 7, 'expectedRole': 'button', 'destructive': 'yes', 'expectedText': ['A']},
    {'key': 'continue', 'expectedRole': 'button', 'page': '/signup/details',
     'lastPage': '/signup', 'html': '<button>Help</button>'},
    {'key': 'continue', 'expectedRole': 'button', 'page': '', 'lastPage': None},
    {'key': 'continue', 'expectedRole': 'button', 'page': '/caf\u00e9/*'},
    {'key': 'terms', 'expectedRole': 'checkbox', 'candidates': [],
     'intent': 'the terms checkbox in the signup form, \u00fc "quoted" \U0001F600'},
    {'key': 'terms', 'expectedRole': 'checkbox', 'intent': ''},
    {'key': 'terms', 'expectedRole': 'checkbox', 'intent': None, 'expectedText': ['I agree']},
    {'key': 'terms', 'expectedRole': 'checkbox', 'intent': 7},
]

TEXTS = [
    'here {"xpath":"//a","fallbacks":["//b",1,null,true,"e","f"],"reasoning":"r"} thanks',
    '{"xpath": ""}',
    'no json at all',
    '{"xpath": "//a"} and then {"x": 1}',
    '[{"xpath": "//a"}]',
    '{"xpath": 5, "reasoning": 0}',
    '{"xpath": "//a", "fallbacks": "not a list"}',
    '{"xpath": true, "fallbacks": [null]}',
    '{"xpath": "//ü"}',
    '',
    None,
    '{"xpath": null, "reason": "The Continue button moved to the next page."}',
    'text {"reason": "  gone \u00fc  "} text',
    '{"xpath": "", "reason": "gone"}',
    '{"xpath": null, "reason": ""}',
    '{"xpath": null, "reason": 3}',
    '{"xpath": null, "reasoning": "only reasoning"}',
]


def test_worker_has_no_hardcoded_model_ids():
    source = WORKER.read_text(encoding='utf-8')
    toml = (ROOT / 'examples' / 'wrangler.toml').read_text(encoding='utf-8')
    for text in (source, toml):
        assert not re.search(r'claude-[a-z0-9]|gpt-[0-9a-z]|gemini-[0-9]', text)


def test_wrangler_toml_binds_kv_at_the_top_level():
    tomllib = pytest.importorskip('tomllib')  # Python 3.11+
    data = tomllib.loads((ROOT / 'examples' / 'wrangler.toml').read_text(encoding='utf-8'))
    assert data['kv_namespaces'][0]['binding'] == 'HEAL_KV'
    assert data['vars'] == {'PROVIDER': 'anthropic'}
    assert '# MODEL = ' in (ROOT / 'examples' / 'wrangler.toml').read_text(encoding='utf-8')


@needs_node
def test_worker_syntax_is_valid():
    proc = subprocess.run([NODE, '--check', str(WORKER)], capture_output=True,
                          encoding='utf-8', timeout=60)
    assert proc.returncode == 0, proc.stderr


@needs_node
def test_build_prompt_parity(tmp_path):
    out = _run_worker(tmp_path, bundles=BUNDLES)
    for bundle, js in zip(BUNDLES, out['prompts'], strict=True):
        assert js == build_prompt(bundle), bundle['key']


@needs_node
def test_parse_proposal_parity(tmp_path):
    out = _run_worker(tmp_path, texts=TEXTS)
    for text, js in zip(TEXTS, out['proposals'], strict=True):
        assert js == parse_proposal(text), text


@needs_node
def test_not_here_reason_parity(tmp_path):
    out = _run_worker(tmp_path, texts=TEXTS)
    for text, js in zip(TEXTS, out['notHere'], strict=True):
        assert js == not_here_reason(text), text
    assert any(out['notHere'])


@needs_node
def test_a_no_element_reply_comes_back_as_needs_human_with_the_reason(tmp_path):
    reply = {'content': [{'type': 'text',
                          'text': '{"xpath": null, "reason": "Continue is on the next page."}'}]}
    out = _run_worker(tmp_path, requests=[_request(dict(ENV, MODEL='m'), reply=reply)])
    assert out['responses'][0]['body'] == {
        'status': 'needs_human',
        'reason': 'the model says no element on this page fits: Continue is on the next page.'}


ENV = {'HEAL_KEY': 'client-key', 'ANTHROPIC_API_KEY': 'test-key'}
BODY = {'key': 'terms', 'expectedRole': 'checkbox', 'html': '<form></form>'}
ANTHROPIC_REPLY = {'content': [{'type': 'text', 'text': '{"xpath": "//input", "reasoning": "r"}'}]}
OPENAI_REPLY = {'choices': [{'message': {'content': '{"xpath": "//input"}'}}]}


def _request(env, reply=ANTHROPIC_REPLY, path='/heal', body=BODY):
    return {'env': env, 'reply': reply, 'path': path, 'body': body, 'key': 'client-key'}


@needs_node
def test_missing_model_is_a_clear_500_and_no_model_call(tmp_path):
    out = _run_worker(tmp_path, requests=[_request(dict(ENV))])
    res = out['responses'][0]
    assert res['status'] == 500
    assert res['body']['status'] == 'error'
    assert 'MODEL is not set' in res['body']['reason']
    assert 'wrangler.toml' in res['body']['reason']
    assert res['calls'] == []


@needs_node
def test_outcome_endpoint_works_without_a_model(tmp_path):
    out = _run_worker(tmp_path, requests=[_request(
        dict(ENV), path='/heal/outcome', body={'key': 'terms', 'xpath': '//a', 'success': True})])
    assert out['responses'][0] == {'status': 200, 'body': {'status': 'ok'}, 'calls': []}


@needs_node
def test_model_comes_from_the_environment(tmp_path):
    out = _run_worker(tmp_path, requests=[
        _request(dict(ENV, MODEL='model-from-env')),
        _request(dict(ENV, ANTHROPIC_MODEL='older-anthropic-var')),
        _request(dict(ENV, MODEL='wins', ANTHROPIC_MODEL='loses')),
        _request({'HEAL_KEY': 'client-key', 'OPENAI_API_KEY': 'k', 'PROVIDER': 'openai',
                  'MODEL': 'openai-model'}, reply=OPENAI_REPLY),
    ])
    models = [r['calls'][0]['body']['model'] for r in out['responses']]
    assert models == ['model-from-env', 'older-anthropic-var', 'wins', 'openai-model']
    first = out['responses'][0]
    assert first['status'] == 200
    assert first['body'] == {'status': 'ok', 'xpath': '//input', 'fallbacks': [],
                             'reasoning': 'r'}
    # The request the worker sends carries the shared prompt.
    expected = build_prompt(BODY)
    assert first['calls'][0]['body']['system'] == expected['system']
    assert first['calls'][0]['body']['messages'] == [{'role': 'user',
                                                      'content': expected['user']}]


@needs_node
def test_wrong_client_key_is_rejected_before_the_model_check(tmp_path):
    request = _request(dict(ENV))
    request['key'] = 'wrong'
    out = _run_worker(tmp_path, requests=[request])
    assert out['responses'][0]['status'] == 401
