// A heal saved for a checkbox, reused on a page variant where its test id sits on a "Delete my
// account" button: the reuse check reads the element in the browser and does not use it there
// (see resolver.Reuse and gate.reuseReason). The store holds the rows the Python package's tests
// save, as both packages share overrides.json.

import * as fs from 'node:fs';

import { open } from 'mendwright';
import { expect, test } from 'mendwright/test';

import { journal, serve, tempStore } from './helpers';

const DELETE = `<!doctype html><html><body><main><h1>Checkout</h1><form id="checkout" onsubmit="return false">
  <label for="email">Email</label><input id="email" name="email" type="email">
  <button type="button" data-testid="tos-optin" onclick="window.deleted = true">Delete my account</button>
  <button id="pay" type="submit">Pay now</button></form></main></body></html>`;

const CHECKBOX = DELETE.replace(
  '<button type="button" data-testid="tos-optin" onclick="window.deleted = true">Delete my account</button>',
  '<label><input type="checkbox" data-testid="tos-optin"> I accept the terms</label>');

function writeStore(store: string): void {
  const row = (xpath: string, source: string, at: string) => ({
    xpath, source, addedAt: '2026-10-01T00:00:00+00:00', lastSucceededAt: at, status: 'active' });
  fs.writeFileSync(store, JSON.stringify({ schema: 2, selectors: { accept_terms: {
    expectedRole: 'checkbox', lastPage: '/checkout', pages: ['/checkout'],
    candidates: [row('#accept-terms', 'code', '2026-10-01T00:00:00+00:00'),
      row("//*[@data-testid='tos-optin']", 'heal', '2026-10-02T00:00:00+00:00')] } } }));
}

test('a saved heal on another control is not used: the Delete my account button is never clicked', async ({ page }) => {
  await serve(page, { '/checkout': DELETE });
  const store = tempStore('reuse');
  writeStore(store);
  const heal = open(page, { storePath: store, timeoutMs: 300 });
  await expect(heal.locator('accept_terms', '#accept-terms').click()).rejects.toThrow(
    /failed the live check here, so it was not used: live element role mismatch: got button want checkbox/);
  expect(await page.evaluate(() => (window as unknown as { deleted?: boolean }).deleted)).toBeUndefined();
  const refused = journal(store).filter((e) => e.event === 'heal.refused');
  expect(refused.map((e) => [e.xpath, e.page])).toEqual(
    [["//*[@data-testid='tos-optin']", '/checkout']]);
});

test('the same heal is used on the page it fits', async ({ page }) => {
  await serve(page, { '/checkout': CHECKBOX });
  const store = tempStore('reuse');
  writeStore(store);
  await open(page, { storePath: store, timeoutMs: 300 }).locator('accept_terms', '#accept-terms').check();
  expect(await page.locator('[data-testid="tos-optin"]').isChecked()).toBe(true);
  expect(journal(store).filter((e) => e.event === 'heal.refused')).toEqual([]);
});
