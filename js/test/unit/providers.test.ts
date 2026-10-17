// The providers against a local HTTP server that answers as Anthropic, OpenAI and Ollama do.
// No test reaches a real model API.

import { strict as assert } from 'node:assert';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
import { after, before, beforeEach, describe, test } from 'node:test';

import { JSDOM } from 'jsdom';

import { mendwrightLib } from '../../src/browser/lib';
import { NOT_LIVE } from '../../src/gate';
import type { Bundle } from '../../src/prompt';
import { AnthropicProvider } from '../../src/providers/anthropic';
import type { ProviderTools } from '../../src/providers/base';
import { autoProvider } from '../../src/providers/detect';
import { scrub } from '../../src/providers/errors';
import { LocalModelProvider, ollamaHost } from '../../src/providers/local';
import { OpenAICompatibleProvider } from '../../src/providers/openai';
import { cleanEnv } from '../support/env';

interface Seen {
  path: string;
  headers: http.IncomingHttpHeaders;
  body: any;
}

let server: http.Server;
let base: string;
let seen: Seen[] = [];
let reply: (seen: Seen) => [number, unknown] = () => [500, {}];

before(async () => {
  server = http.createServer((req, res) => {
    let data = '';
    req.on('data', (chunk) => (data += chunk));
    req.on('end', () => {
      const entry = { path: req.url ?? '', headers: req.headers, body: data ? JSON.parse(data) : null };
      seen.push(entry);
      const [status, body] = reply(entry);
      res.writeHead(status, { 'content-type': 'application/json' });
      res.end(typeof body === 'string' ? body : JSON.stringify(body));
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = 'http://127.0.0.1:' + (server.address() as AddressInfo).port;
});

after(() => new Promise<void>((resolve) => server.close(() => resolve())));

beforeEach(() => {
  cleanEnv();
  seen = [];
});

const BUNDLE: Bundle = {
  key: 'email', expectedRole: 'input', expectedText: [], intent: null, destructive: false, candidates: ['#email'],
  inventory: [], html: '<html><body><form><label for="e">Email</label><input id="e" name="email"></form></body></html>',
  url: null, page: '/signup', lastPage: '/signup', stage: null, version: null,
};

describe('AnthropicProvider', () => {
  test('sends the shared prompt to /v1/messages and reads the proposal', async () => {
    reply = () => [200, { content: [{ type: 'text', text: '{"xpath": "//input[@name=\'email\']", "reasoning": "same name"}' }] }];
    const provider = new AnthropicProvider({ model: 'test-model', apiKey: 'sk-ant-test-0123456789', baseUrl: base });
    const proposal = await provider.propose(BUNDLE);
    assert.deepEqual(proposal, { xpath: "//input[@name='email']", fallbacks: [], reasoning: 'same name' });
    assert.equal(seen[0].path, '/v1/messages');
    assert.equal(seen[0].headers['x-api-key'], 'sk-ant-test-0123456789');
    assert.equal(seen[0].headers['anthropic-version'], '2023-06-01');
    assert.equal(seen[0].body.model, 'test-model');
    assert.match(seen[0].body.system, /You repair ONE broken web-automation xpath selector/);
    assert.match(seen[0].body.messages[0].content, /^key: email\nexpectedRole: input/);
  });

  test('a refusal gives a reason without the key', async () => {
    reply = (s) => [401, { type: 'error', error: { type: 'authentication_error', message: 'invalid x-api-key ' + s.headers['x-api-key'] } }];
    const provider = new AnthropicProvider({ model: 'm', apiKey: 'sk-ant-secret-0123456789', baseUrl: base });
    assert.equal(await provider.propose(BUNDLE), null);
    assert.match(String(provider.lastError), /^Anthropic returned HTTP 401 \(authentication_error\): invalid x-api-key/);
    assert.ok(!String(provider.lastError).includes('sk-ant-secret'));
    assert.match(String(provider.lastError), /Check ANTHROPIC_API_KEY/);
  });

  test('a reply that says no element fits gives the model\'s reason', async () => {
    reply = () => [200, { content: [{ type: 'text', text: '{"xpath": null, "reason": "The email field is gone."}' }] }];
    const provider = new AnthropicProvider({ model: 'm', apiKey: 'k', baseUrl: base });
    assert.equal(await provider.propose(BUNDLE), null);
    assert.match(String(provider.lastError), /the model says no element on this page fits: The email field is gone/);
  });

  test('a server that is not there', async () => {
    const closed = http.createServer();
    await new Promise<void>((resolve) => closed.listen(0, '127.0.0.1', resolve));
    const port = (closed.address() as AddressInfo).port;
    await new Promise<void>((resolve) => closed.close(() => resolve()));
    const provider = new AnthropicProvider({ model: 'm', apiKey: 'k', baseUrl: 'http://127.0.0.1:' + port });
    assert.equal(await provider.propose(BUNDLE), null);
    assert.equal(provider.lastError, 'could not connect to http://127.0.0.1:' + port + ' (connection refused). '
      + 'Check your network connection, and baseUrl if you use a proxy.');
  });

  test('has no default model', () => {
    assert.throws(() => new AnthropicProvider({ apiKey: 'k' }), /ANTHROPIC_MODEL/);
  });
});

describe('OpenAICompatibleProvider', () => {
  test('sends the prompt to /chat/completions with the key as a bearer token', async () => {
    reply = () => [200, { choices: [{ message: { content: '{"xpath": "//input[@id=\'e\']", "fallbacks": ["//input"]}' } }] }];
    const provider = new OpenAICompatibleProvider({ model: 'gpt-test', apiKey: 'sk-test-abcdefgh', baseUrl: base });
    const proposal = await provider.propose(BUNDLE);
    assert.equal(proposal?.xpath, "//input[@id='e']");
    assert.deepEqual(proposal?.fallbacks, ['//input']);
    assert.equal(seen[0].path, '/chat/completions');
    assert.equal(seen[0].headers.authorization, 'Bearer sk-test-abcdefgh');
    assert.deepEqual(seen[0].body.response_format, { type: 'json_object' });
    assert.equal(seen[0].body.messages[0].role, 'system');
  });

  test('a local Ollama never gets the OpenAI key', async () => {
    process.env.OPENAI_API_KEY = 'sk-cloud-key-0123456789';
    reply = () => [200, { choices: [{ message: { content: '{"xpath": "//input"}' } }] }];
    const provider = OpenAICompatibleProvider.forOllama('llama3', { baseUrl: base + '/v1' });
    await provider.propose(BUNDLE);
    assert.equal(seen[0].path, '/v1/chat/completions');
    assert.equal(seen[0].headers.authorization, undefined);
  });
});

function pageTools(): ProviderTools {
  const dom = new JSDOM('<html><body></body></html>', { runScripts: 'outside-only' });
  const lib = dom.window.eval('(' + mendwrightLib.toString() + ')()') as Record<string, (arg: unknown) => unknown>;
  return { lib: async (name, arg) => JSON.parse(JSON.stringify(lib[name](arg))) };
}

const LOCAL_BUNDLE: Bundle = {
  ...BUNDLE, expectedRole: 'button',
  html: '<html><body><form id="signup"><button id="back">Back</button><button id="go">Create account</button></form></body></html>',
  candidates: ['#create'], fingerprint: { v: 1, tag: 'button', attrs: { id: 'create' }, text: 'Create account' },
};

function ollamaReply(pick: number | null, logprob = Math.log(0.95)) {
  const content = pick === null ? '{"pick": null, "why": "removed"}' : '{"pick": ' + pick + '}';
  return [200, { message: { content }, logprobs: [{ token: '{"', logprob: 0 }, { token: 'pick', logprob: 0 },
    { token: '":', logprob: 0 }, { token: ' ' + String(pick), logprob }, { token: '}', logprob: 0 }] }] as [number, unknown];
}

describe('LocalModelProvider', () => {
  test('asks Ollama which candidate the step meant, and builds the XPath itself', async () => {
    reply = () => ollamaReply(2);
    const provider = new LocalModelProvider({ baseUrl: base });
    const proposal = await provider.propose(LOCAL_BUNDLE, pageTools());
    assert.equal(proposal?.xpath, "//button[@id='go']");
    assert.equal(seen[0].path, '/api/chat');
    assert.equal(seen[0].body.model, 'mendwright-selector');
    assert.equal(seen[0].body.format, 'json');
    assert.equal(seen[0].body.logprobs, true);
    assert.equal(seen[0].body.options.temperature, 0);
    const task = seen[0].body.messages[1].content as string;
    assert.match(task, /^Step\nkey: email\nrole: button\nold selector: #create\nit was: button "Create account" id="create"/);
    assert.match(task, /\n1\. button "Back" id="back"/);
    assert.match(task, /\n2\. button "Create account" id="go"/);
  });

  test('declines a pick it is not sure of', async () => {
    reply = () => ollamaReply(2, Math.log(0.5));
    const provider = new LocalModelProvider({ baseUrl: base });
    assert.equal(await provider.propose(LOCAL_BUNDLE, pageTools()), null);
    assert.match(String(provider.lastError), /was not sure which element the step meant .* confidence 0\.50, below 0\.80/);
  });

  test('says when the model finds no element that fits', async () => {
    reply = () => ollamaReply(null);
    const provider = new LocalModelProvider({ baseUrl: base });
    assert.equal(await provider.propose(LOCAL_BUNDLE, pageTools()), null);
    assert.match(String(provider.lastError), /no element on this page fits: the element is not on this page/);
  });

  test('does not guess a second time after the gate refused its pick for its role or a quarantine', async () => {
    const provider = new LocalModelProvider({ baseUrl: base });
    const proposal = await provider.propose({ ...LOCAL_BUNDLE, rejectedXpath: "//button[@id='go']",
      rejectReason: 'previously quarantined' }, pageTools());
    assert.equal(proposal, null);
    assert.equal(seen.length, 0);
    assert.match(String(provider.lastError), /does not guess a second time/);
  });

  test('asks again without the element that was not on the live page', async () => {
    reply = () => ollamaReply(1);
    const provider = new LocalModelProvider({ baseUrl: base });
    const proposal = await provider.propose({ ...LOCAL_BUNDLE, rejectedXpath: "//button[@id='go']", rejectReason: NOT_LIVE },
      pageTools());
    assert.equal(proposal?.xpath, "//button[@id='back']");
    assert.match(seen[0].body.messages[1].content, /Candidates \(1 button\)\n1\. button "Back"/);
  });

  test('reads OLLAMA_HOST as Ollama does', () => {
    assert.equal(ollamaHost('localhost:11434'), 'http://127.0.0.1:11434');
    assert.equal(ollamaHost('0.0.0.0'), 'http://127.0.0.1:11434');
    assert.equal(ollamaHost('http://gpu.lan:8080/v1'), 'http://gpu.lan:8080');
    assert.equal(ollamaHost(''), 'http://127.0.0.1:11434');
  });
});

describe('autoProvider', () => {
  test('reads the same variables in the same order as the Python package', () => {
    assert.throws(() => autoProvider(), /no model provider is configured/);
    process.env.OLLAMA_MODEL = 'llama3';
    assert.ok(autoProvider() instanceof OpenAICompatibleProvider);
    process.env.MENDWRIGHT_LOCAL_MODEL = 'mendwright-selector';
    assert.ok(autoProvider() instanceof LocalModelProvider);
    process.env.OPENAI_API_KEY = 'k';
    assert.throws(() => autoProvider(), /OPENAI_MODEL/);
    process.env.OPENAI_MODEL = 'gpt';
    assert.ok(autoProvider() instanceof OpenAICompatibleProvider);
    process.env.ANTHROPIC_API_KEY = 'k';
    process.env.ANTHROPIC_MODEL = 'claude';
    assert.ok(autoProvider() instanceof AnthropicProvider);
    process.env.MENDWRIGHT_PROVIDER = 'local';
    assert.ok(autoProvider() instanceof LocalModelProvider);
    process.env.MENDWRIGHT_PROVIDER = 'nope';
    assert.throws(() => autoProvider(), /not recognised/);
  });

  test('a provider this package does not have stops the search, so the page goes nowhere else', () => {
    process.env.GEMINI_API_KEY = 'k';
    process.env.OLLAMA_MODEL = 'llama3';
    assert.throws(() => autoProvider(), /no Gemini provider yet/);
  });

  test('Ollama from OLLAMA_MODEL is keyless and at OLLAMA_HOST', async () => {
    process.env.OLLAMA_MODEL = 'llama3';
    process.env.OLLAMA_HOST = base.replace('http://', '');
    process.env.OPENAI_API_KEY = '';
    reply = () => [200, { choices: [{ message: { content: '{"xpath": "//a"}' } }] }];
    await (autoProvider() as OpenAICompatibleProvider).propose(BUNDLE);
    assert.equal(seen[0].path, '/v1/chat/completions');
    assert.equal(seen[0].headers.authorization, undefined);
  });
});

test('a reason never shows a key or a token', () => {
  const text = scrub('bad key sk-ant-api03-abcdefghijk, Authorization: Bearer abcdefghijklmnopqrstu, url https://x.test/?key=s3cr3t');
  assert.ok(!/abcdefghijk|abcdefghijklmnopqrstu|s3cr3t/.test(text), text);
  assert.equal(scrub('my own secret-value here', ['secret-value']), 'my own [redacted] here');
});
