// heal.interrupt(key, selector, { run }) in a real browser: a session-expired popup that needs
// a user name and a password, answered by a run function that gets heal.page (the Python
// package's tests/browser/test_browser_flow.py). The values typed are dummies.

import type { Page } from '@playwright/test';
import { open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { FakeModel, ONETRUST, journal, serve, tempStore } from './helpers';

// A session-expired popup over the page: it closes once the user name and a password are
// filled in and Sign in is clicked, and shows the form when the page hid it.
const RELOGIN_PAGE = `<!doctype html><html><head><title>Account</title></head><body>
<p>Signed in as <span id="session">nobody</span>. Saved: <span id="saved">no</span></p>
<form id="account" onsubmit="return false"><label for="email">Email</label> <input id="email" type="email">
  <button id="save" type="button" onclick="document.getElementById('saved').textContent = 'yes'">Save</button></form>
<div id="relogin" role="dialog" aria-label="Session expired"
  style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4)">
  <div style="position: absolute; left: 0; right: 0; top: 0; padding: 16px; background: #fff">
    <p>Your session expired. Sign in again.</p>
    <label for="user">User</label> <input id="user">
    <label for="password">Password</label> <input id="password" type="password">
    <button id="signin" type="button" onclick="signIn()">Sign in</button>
  </div>
</div>
<script>
function signIn() {
  if (document.getElementById('password').value === '') return;     // it stays open
  document.getElementById('session').textContent = document.getElementById('user').value;
  document.getElementById('relogin').remove();
  document.getElementById('account').style.display = 'block';
}
</script></body></html>`;

// The same page, with a OneTrust banner that opens over the popup as its user name is typed.
const LATE_BANNER = RELOGIN_PAGE.replace('</body>', `<template id="banner">${ONETRUST.replace('z-index: 10', 'z-index: 20')}</template>
<script>
document.getElementById('user').addEventListener('focus', () => {
  document.body.append(document.getElementById('banner').content.cloneNode(true));
}, { once: true });
</script></body>`);

/** The run function for the popup, written once: it signs in through the page it gets. */
function signIn(calls: unknown[], password = 'not-a-real-password') {
  return async function relogin(page: Page): Promise<void> {
    calls.push(page);
    await page.fill('#user', 'ada');
    await page.fill('#password', password);
    await page.click('#signin');
  };
}

/** The flow.interrupt events: key, before, action, function and outcome. */
function interrupts(store: string): unknown[][] {
  return journal(store).filter((e) => e.event === 'flow.interrupt')
    .map((e) => [e.key, e.before, e.action, e.function, e.outcome]);
}

test('a session-expired popup is signed in by its run function, and the step goes on', async ({ page }) => {
  await serve(page, { '/account': RELOGIN_PAGE });
  const store = tempStore('relogin');
  const lines: string[] = [];
  const heal = open(page, { storePath: store, timeoutMs: 1000, log: (_level, message) => lines.push(message) });
  const calls: unknown[] = [];
  heal.interrupt('relogin', '#relogin', { run: signIn(calls) });

  await heal.locator('email', '#email').fill('ada@example.com');
  await heal.locator('save', '#save').click();

  expect(calls.length).toBe(1);
  expect(calls[0]).toBe(heal.page);
  await expect(page.locator('#session')).toHaveText('ada');
  await expect(page.locator('#email')).toHaveValue('ada@example.com');
  await expect(page.locator('#saved')).toHaveText('yes');
  expect(heal.handledInterruptions.map((e) => [e.key, e.before])).toEqual([['relogin', 'email']]);
  expect(lines).toContain("handled interruption 'relogin' before 'email' with relogin()");
  expect(interrupts(store)).toEqual([['relogin', 'email', 'run', 'relogin', undefined]]);
});

test('what its run function throws stops the step, as it is', async ({ page }) => {
  await serve(page, { '/account': RELOGIN_PAGE });
  const store = tempStore('relogin-throws');
  const lines: string[] = [];
  const heal = open(page, { storePath: store, timeoutMs: 1000, log: (_level, message) => lines.push(message) });
  const calls: unknown[] = [];
  const error = new Error('no password for ada in the test vault');
  heal.interrupt('relogin', '#relogin', {
    run: async function relogin(p: Page) {
      calls.push(p);
      throw error;
    },
  });

  await expect(heal.locator('email', '#email').fill('ada@example.com')).rejects.toBe(error);

  expect(calls.length).toBe(1);
  await expect(page.locator('#email')).toHaveValue('');
  expect(heal.handledInterruptions).toEqual([]);
  expect(interrupts(store)).toEqual([['relogin', 'email', 'run', 'relogin', 'failed']]);
  expect(lines.filter((l) => l.startsWith("relogin(), the run function of interruption 'relogin', raised an error while "
    + "mendwright handled it before 'email' (Error: no password for ada in the test vault)."))).toHaveLength(1);
});

test('a cookie banner that opens over its form is handled by its own actions', async ({ page }) => {
  await serve(page, { '/account': LATE_BANNER });
  const heal = open(page, { cookies: 'reject', storePath: tempStore('relogin-cookies'), timeoutMs: 1000 });
  const calls: unknown[] = [];
  heal.interrupt('relogin', '#relogin', { run: signIn(calls) });

  await heal.locator('email', '#email').fill('ada@example.com');

  expect(calls.length).toBe(1);
  expect(await page.evaluate(() => (window as unknown as { consent?: string }).consent)).toBe('reject');
  await expect(page.locator('#session')).toHaveText('ada');
  await expect(page.locator('#email')).toHaveValue('ada@example.com');
  expect(heal.handledInterruptions.map((e) => [e.key, e.before])).toEqual([['cookie_banner', '#password'],
    ['relogin', 'email']]);
});

test('a session popup its run function cannot close stops the step after 3', async ({ page }) => {
  // The page hides its form until the user signs in, so the lookup misses.
  await serve(page, { '/account': RELOGIN_PAGE.replace('<form id="account"', '<form id="account" style="display: none"') });
  const model = new FakeModel(["//input[@id='email']"]);
  const heal = open(page, { provider: model, storePath: tempStore('relogin-loop'), timeoutMs: 500 });
  const calls: unknown[] = [];
  heal.interrupt('relogin', '#relogin', { run: signIn(calls, '') });   // no password: the popup stays

  await expect(heal.locator('email', '#email', { role: 'input' }).fill('x')).rejects.toThrow(
    "3 interruptions were handled for 'email' and 'relogin' is still on the page");

  expect(calls.length).toBe(3);
  await expect(page.locator('#session')).toHaveText('nobody');
  expect(model.bundles).toEqual([]);
});
