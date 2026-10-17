// Pages and helpers for the real-browser tests. Each page is served by page.route() at an
// address under https://shop.test, so nothing leaves the machine.

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import type { Page } from '@playwright/test';
import type { Bundle, HealProvider, Proposal } from 'mendwright';

export const SIGNUP_V1 = `<!doctype html><html><head><title>Sign up</title></head><body><main>
  <form id="signup" onsubmit="return false">
    <label for="email">Email</label> <input id="email" name="email" type="email">
    <label><input type="checkbox" id="terms"> I accept the terms</label>
    <button id="create" type="submit">Create account</button>
  </form></main></body></html>`;

// The same page after a redesign: every id changed, and the checkbox is styled (its native
// input is hidden behind the label).
export const SIGNUP_V2 = `<!doctype html><html><head><title>Sign up</title>
  <style>.box input { position: absolute; opacity: 0; width: 1px; height: 1px; }</style></head><body><main>
  <form id="signup" onsubmit="return false">
    <label for="user-email">Email</label> <input id="user-email" name="email" type="email">
    <label class="box"><input type="checkbox" id="accept-terms" data-testid="terms"> I accept the terms</label>
    <button id="create-account" type="submit">Create account</button>
  </form></main></body></html>`;

export const ONETRUST = `<div id="onetrust-banner-sdk" style="position: fixed; inset: 0; background: #fff; z-index: 10">
  We use cookies to improve your experience.
  <button id="onetrust-accept-btn-handler" onclick="this.parentElement.remove(); window.consent = 'accept'">Accept all</button>
  <button id="onetrust-reject-all-handler" onclick="this.parentElement.remove(); window.consent = 'reject'">Reject all</button>
</div>`;

/** Serve html at https://shop.test + pathname on page, and go there. */
export async function serve(page: Page, routes: Record<string, string>, start?: string): Promise<void> {
  await page.route('https://shop.test/**', (route) => {
    const url = new URL(route.request().url());
    const body = routes[url.pathname];
    if (body === undefined) return route.fulfill({ status: 404, body: 'not found' });
    return route.fulfill({ contentType: 'text/html', body });
  });
  await page.goto('https://shop.test' + (start ?? Object.keys(routes)[0]));
}

export function tempStore(name: string): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'mw-browser-' + name + '-')), 'overrides.json');
}

export function journal(store: string): Record<string, unknown>[] {
  const file = path.join(path.dirname(store), 'journal.jsonl');
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf-8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

export function readStore(store: string): any {
  return JSON.parse(fs.readFileSync(store, 'utf-8'));
}

/** A model that answers with the given proposals, in order, and keeps what it was sent. */
export class FakeModel implements HealProvider {
  readonly model = 'fake';
  readonly bundles: Bundle[] = [];
  lastError: string | null = null;

  constructor(private readonly replies: string[]) {}

  async propose(bundle: Bundle): Promise<Proposal | null> {
    this.bundles.push(bundle);
    const xpath = this.replies.shift();
    return xpath ? { xpath, fallbacks: [], reasoning: 'fake' } : null;
  }
}
