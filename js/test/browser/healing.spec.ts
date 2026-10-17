// Heals in a real browser: a model's proposal, the gate's live checks, the canary, the page
// guard, the Anthropic provider and the local selector model against local servers that
// answer as those APIs do.

import * as fs from 'node:fs';

import { AnthropicProvider, AttentionRequired, LocalModelProvider, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, SIGNUP_V1, SIGNUP_V2, journal, readStore, serve, server, tempStore } from './helpers';

test('a model heals a selector that never matched, and the gate checks it on the live page', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V2 });
  const model = new FakeModel(["//input[@id='user-email']"]);
  const heal = open(page, { provider: model, storePath: tempStore('model'), timeoutMs: 500 });
  await heal.locator('email', '#email', { role: 'textbox' }).fill('ada@example.com');
  await expect(page.locator('#user-email')).toHaveValue('ada@example.com');
  expect(heal.heals).toEqual([{ key: 'email', old: ['#email'], new: "//input[@id='user-email']", confirmed: null, source: 'model' }]);
  // The model read the page cleaned: no style, no script.
  expect(model.bundles[0].html).not.toContain('<style>');
  expect(model.bundles[0].html).toContain('user-email');
});

test('the gate refuses an element that is hidden, and one of another role, on the live page', async ({ page }) => {
  const html = SIGNUP_V2.replace('</form>', '<input id="old-email" name="email-old" style="display: none"></form>');
  await serve(page, { '/signup': html });
  const store = tempStore('gate');
  const model = new FakeModel(["//input[@id='old-email']", "//button[@id='create-account']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  const error = await heal.locator('email', '#email', { role: 'input' }).fill('x').catch((e) => e);
  expect(error).toBeInstanceOf(AttentionRequired);
  expect(model.bundles[1].rejectReason).toBe('passed static validation but not uniquely present on the live page');
  expect(String(error.message)).toContain('role mismatch: got button want input');
  expect(readStore(store).selectors.email.candidates.map((c: any) => c.xpath)).toEqual(['#email']);
});

test('a heal whose action fails in the browser is quarantined and refused from then on', async ({ page }) => {
  const html = SIGNUP_V2.replace('<button id="create-account"', '<button id="create-account" disabled');
  await serve(page, { '/signup': html });
  const store = tempStore('quarantine');
  const heal = open(page, { provider: new FakeModel(["//button[@id='create-account']"]), storePath: store, timeoutMs: 300 });
  await expect(heal.locator('create', '#create', { role: 'button' }).click({ timeout: 500 })).rejects.toThrow(/Timeout/);
  expect(readStore(store).selectors.create.candidates.find((c: any) => c.source === 'heal').status).toBe('quarantined');
  expect(journal(store).some((e) => e.event === 'heal.quarantine' && e.key === 'create')).toBe(true);
  const again = open(page, { provider: new FakeModel(["//button[@id='create-account']", "//button[normalize-space()='Create account']"]),
                             storePath: store, timeoutMs: 300 });
  await expect(again.locator('create', '#create', { role: 'button' }).click({ timeout: 500 }))
    .rejects.toThrow(/previously quarantined \(the same element as a quarantined heal/);
});

test('a key is not healed on a page where it was never found', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V1, '/checkout/review': SIGNUP_V2 });
  const store = tempStore('guard');
  const model = new FakeModel(["//button[@id='create-account']"]);
  await open(page, { provider: model, storePath: store, timeoutMs: 300 }).locator('create', '#create').click();
  await page.goto('https://shop.test/checkout/review');
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  await expect(heal.locator('create', '#create').click()).rejects.toThrow(/'create' was last found on \/signup, and this page is \/checkout\/review/);
  expect(model.bundles).toEqual([]);
});

test('a key marked destructive is never healed, and the model is not asked', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V2 });
  const model = new FakeModel(["//button[@id='create-account']"]);
  const heal = open(page, { provider: model, storePath: tempStore('destructive'), timeoutMs: 300 });
  await expect(heal.locator('create', '#create', { role: 'button', destructive: true }).click())
    .rejects.toThrow(/marked as a destructive control/);
  expect(model.bundles).toEqual([]);
});

test('AnthropicProvider heals through the Messages API (a local stand-in)', async ({ page }) => {
  const api = await server(() => ({ content: [{ type: 'text', text: '{"xpath": "//input[@name=\'email\']", "reasoning": "same name"}' }] }));
  try {
    await serve(page, { '/signup': SIGNUP_V2 });
    const provider = new AnthropicProvider({ model: 'test-model', apiKey: 'sk-ant-local-test', baseUrl: api.url });
    const heal = open(page, { provider, storePath: tempStore('anthropic'), timeoutMs: 300 });
    await heal.locator('email', '#email', { role: 'input' }).fill('ada@example.com', { expect: async () => true });
    expect(heal.heals[0].confirmed).toBe(true);
    expect(api.bodies[0].messages[0].content).toContain('key: email\nexpectedRole: input');
  } finally {
    await api.close();
  }
});

test('the local selector model picks a candidate, and mendwright builds its XPath', async ({ page }) => {
  const ollama = await server((body) => {
    const task = String(body.messages[1].content);
    const line = task.split('\n').find((l) => l.includes('"Create account"')) ?? '';
    return { message: { content: '{"pick": ' + Number(line.split('.')[0]) + '}' } };
  });
  try {
    await serve(page, { '/signup': SIGNUP_V2.replace('<button id="create-account"', '<button type="button">Back</button><button id="create-account"') });
    const heal = open(page, { provider: new LocalModelProvider({ baseUrl: ollama.url }), storePath: tempStore('local'), timeoutMs: 300 });
    await heal.locator('create', '#create', { role: 'button' }).click();
    expect(heal.heals[0].new).toBe("//button[@id='create-account']");
    const task = String(ollama.bodies[0].messages[1].content);
    expect(task).toContain('Candidates (2 buttons)');
    expect(task.split('\n')).toContain('1. button "Back" | main>form#signup');
  } finally {
    await ollama.close();
  }
});

test('the browser rejects a selector it cannot parse at once, with no heal', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V1 });
  const model = new FakeModel(['//input']);
  const heal = open(page, { provider: model, storePath: tempStore('invalid'), timeoutMs: 5000 });
  const started = Date.now();
  await expect(heal.locator('email', '//input[', { role: 'input' }).fill('x')).rejects.toThrow(/browser rejected a selector/);
  expect(Date.now() - started).toBeLessThan(3000);
  expect(model.bundles).toEqual([]);
});

test('a heal the Python package saved inside an iframe is looked up there', async ({ page }) => {
  const store = tempStore('frame');
  fs.writeFileSync(store, JSON.stringify({ schema: 2, selectors: { card: { expectedRole: 'input', candidates: [
    { xpath: '#card', source: 'code', addedAt: null, lastSucceededAt: null, status: 'active' },
    { xpath: "xpath=//input[@id='card-number']", source: 'heal', addedAt: null, lastSucceededAt: null, status: 'active',
      frameXPath: "iframe[name='pay']" }] } } }));
  await serve(page, {
    '/checkout': '<html><body><h1>Pay</h1><iframe name="pay" src="/frame"></iframe></body></html>',
    '/frame': '<html><body><input id="card-number"></body></html>',
  }, '/checkout');
  const heal = open(page, { storePath: store, timeoutMs: 2000 });
  await heal.locator('card', '#card').fill('4242');
  await expect(page.frameLocator("iframe[name='pay']").locator('#card-number')).toHaveValue('4242');
  expect(heal.heals).toEqual([]);
});

test.describe('the mendwrightProvider option', () => {
  const model = new FakeModel(["//input[@id='user-email']"]);
  test.use({ mendwrightProvider: model, mendwrightStore: tempStore('provider'), mendwrightOptions: { timeoutMs: 300 } });

  test('is the model of heal and page', async ({ heal, page }) => {
    await serve(page, { '/signup': SIGNUP_V2 });
    await heal.locator('email', '#email', { role: 'input' }).fill('ada@example.com');
    expect(model.bundles.length).toBe(1);
    expect(heal.heals[0].source).toBe('model');
  });
});
