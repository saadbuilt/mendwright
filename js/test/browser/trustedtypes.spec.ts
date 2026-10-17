// Heals on a page whose Content-Security-Policy enforces Trusted Types, where DOMParser in
// the page refuses a plain string: mendwright parses the captured page through a policy of
// its own, and when the page does not allow that policy, it sends the model nothing.

import { LocalModelProvider, open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, SIGNUP_V1, SIGNUP_V2, journal, serve, server, tempStore } from './helpers';

const REQUIRE = { 'content-security-policy': "require-trusted-types-for 'script'" };
const NOT_NAMED = { 'content-security-policy': "require-trusted-types-for 'script'; trusted-types angular" };
const NAMED = { 'content-security-policy': "require-trusted-types-for 'script'; trusted-types mendwright" };

// The redesign, with the state a script keeps inline, as many apps do.
const STATE = '<script>window.__STATE__ = {"session": "s3cr3t-session-abc", "email": "ada@example.com"}</script>';
const SIGNUP_V2_STATE = SIGNUP_V2.replace('<title>', STATE + '<title>');

test('the fingerprint tier heals there', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V1, '/signup/v2': SIGNUP_V2 }, '/signup', REQUIRE);
  const store = tempStore('tt-fingerprint');
  await open(page, { storePath: store, timeoutMs: 300 }).locator('email', '#email').fill('x');
  await page.goto('https://shop.test/signup/v2');
  const heal = open(page, { storePath: store, timeoutMs: 300, healAcrossPages: true });
  await heal.locator('email', '#email').fill('ada@example.com');
  await expect(page.locator('#user-email')).toHaveValue('ada@example.com');
  expect(heal.heals.map((h) => [h.new, h.source])).toEqual([["//input[@id='user-email']", 'fingerprint']]);
});

test('a model gets the page cleaned there, without its scripts and styles', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V2_STATE }, '/signup', REQUIRE);
  const model = new FakeModel(["//input[@id='user-email']"]);
  const heal = open(page, { provider: model, storePath: tempStore('tt-model'), timeoutMs: 300 });
  await heal.locator('email', '#email', { role: 'input' }).fill('ada@example.com');
  expect(heal.heals.map((h) => h.new)).toEqual(["//input[@id='user-email']"]);
  expect(model.bundles).toHaveLength(1);
  expect(model.bundles[0].html).toContain('user-email');
  expect(model.bundles[0].html).not.toMatch(/<script|<style|s3cr3t|ada@example\.com/);
});

test('the local selector model reads its candidates there', async ({ page }) => {
  const ollama = await server((body) => {
    const line = String(body.messages[1].content).split('\n').find((l) => l.includes('"Create account"')) ?? '';
    return { message: { content: '{"pick": ' + Number(line.split('.')[0]) + '}' } };
  });
  try {
    await serve(page, { '/signup': SIGNUP_V2_STATE }, '/signup', REQUIRE);
    const heal = open(page, { provider: new LocalModelProvider({ baseUrl: ollama.url }), storePath: tempStore('tt-local'), timeoutMs: 300 });
    await heal.locator('create', '#create', { role: 'button' }).click();
    expect(heal.heals.map((h) => h.new)).toEqual(["//button[@id='create-account']"]);
  } finally {
    await ollama.close();
  }
});

test('a trusted-types directive that names mendwright allows one policy per document, for every healer', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V2, '/next': SIGNUP_V2 }, '/signup', NAMED);
  const store = tempStore('tt-named');
  const first = open(page, { provider: new FakeModel(["//input[@id='user-email']"]), storePath: store, timeoutMs: 300 });
  await first.locator('email', '#email', { role: 'input' }).fill('x');
  const second = open(page, { provider: new FakeModel(["//button[@id='create-account']"]), storePath: store, timeoutMs: 300 });
  await second.locator('create', '#create', { role: 'button' }).click();
  await page.goto('https://shop.test/next');
  const third = open(page, { provider: new FakeModel(["//input[@name='email']"]), storePath: store, timeoutMs: 300 });
  await third.locator('login', '#login', { role: 'input' }).fill('x');
  expect([...first.heals, ...second.heals, ...third.heals].map((h) => h.key)).toEqual(['email', 'create', 'login']);
});

test('a page that does not allow the policy sends the model nothing, and the heal says why', async ({ page }) => {
  await serve(page, { '/signup': SIGNUP_V2_STATE }, '/signup', NOT_NAMED);
  const store = tempStore('tt-refused');
  const model = new FakeModel(["//input[@id='user-email']"]);
  const heal = open(page, { provider: model, storePath: store, timeoutMs: 300 });
  await expect(heal.locator('email', '#email', { role: 'input' }).fill('x'))
    .rejects.toThrow(/sent the model nothing for 'email': the page enforces Trusted Types, and its trusted-types directive does not allow the policy 'mendwright'/);
  expect(model.bundles).toEqual([]);
  expect(journal(store).filter((e) => e.event === 'heal.declined').map((e) => e.code)).toEqual(['other']);
});

test('in Chromium, bypassCSP lets mendwright heal on such a page', async ({ browser, browserName }) => {
  test.skip(browserName !== 'chromium', 'bypassCSP turns Trusted Types off in Chromium only');
  const context = await browser.newContext({ bypassCSP: true });
  try {
    const page = await context.newPage();
    await serve(page, { '/signup': SIGNUP_V2_STATE }, '/signup', NOT_NAMED);
    const model = new FakeModel(["//input[@id='user-email']"]);
    const heal = open(page, { provider: model, storePath: tempStore('tt-bypass'), timeoutMs: 300 });
    await heal.locator('email', '#email', { role: 'input' }).fill('x');
    expect(model.bundles[0].html).not.toMatch(/<script|s3cr3t/);
  } finally {
    await context.close();
  }
});
