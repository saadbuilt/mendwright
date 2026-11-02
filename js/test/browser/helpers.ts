// Pages and helpers for the real-browser tests. Each page is served by page.route() at an
// address under https://shop.test, so nothing leaves the machine.

import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as http from 'node:http';
import type { AddressInfo } from 'node:net';
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

/** Serve html at https://shop.test + pathname on page, with headers, and go there. */
export async function serve(page: Page, routes: Record<string, string>, start?: string,
                            headers: Record<string, string> = {}): Promise<void> {
  await page.route('https://shop.test/**', (route) => {
    const url = new URL(route.request().url());
    const body = routes[url.pathname];
    if (body === undefined) return route.fulfill({ status: 404, body: 'not found' });
    return route.fulfill({ contentType: 'text/html', headers, body });
  });
  await page.goto('https://shop.test' + (start ?? Object.keys(routes)[0]));
}

/** A local HTTP server that answers each JSON POST with answer(body), as a model API does. */
export async function server(answer: (body: any) => unknown): Promise<{ url: string; bodies: any[]; close: () => Promise<void> }> {
  const bodies: any[] = [];
  const srv = http.createServer((req, res) => {
    let data = '';
    req.on('data', (c) => (data += c));
    req.on('end', () => {
      const body = JSON.parse(data);
      bodies.push(body);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(answer(body)));
    });
  });
  await new Promise<void>((resolve) => srv.listen(0, '127.0.0.1', resolve));
  return {
    url: 'http://127.0.0.1:' + (srv.address() as AddressInfo).port, bodies,
    close: () => new Promise<void>((resolve) => srv.close(() => resolve())),
  };
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

/** A model that answers with the given proposals, in order (an XPath, or a whole proposal with
 * its fallbacks), and keeps what it was sent and the outcomes reported to it. */
export class FakeModel implements HealProvider {
  readonly model = 'fake';
  readonly bundles: Bundle[] = [];
  readonly outcomes: [string, string, boolean][] = [];
  lastError: string | null = null;

  constructor(private readonly replies: (string | Proposal)[]) {}

  async propose(bundle: Bundle): Promise<Proposal | null> {
    this.bundles.push(bundle);
    const reply = this.replies.shift();
    if (!reply) return null;
    return typeof reply === 'string' ? { xpath: reply, fallbacks: [], reasoning: 'fake' } : reply;
  }

  reportOutcome(key: string, xpath: string, success: boolean): void {
    this.outcomes.push([key, xpath, success]);
  }
}

/** The repository's root, which holds the Python package. */
export const REPO = path.resolve(__dirname, '..', '..', '..');

let python: string | null | undefined;

/** The Python command that imports mendwright from this repository (MENDWRIGHT_PYTHON, else
 * python3 or python), or null. */
export function pythonCommand(): string | null {
  if (python !== undefined) return python;
  python = null;
  const env = { ...process.env, PYTHONPATH: REPO };
  for (const command of process.env.MENDWRIGHT_PYTHON ? [process.env.MENDWRIGHT_PYTHON] : ['python3', 'python']) {
    const result = spawnSync(command, ['-c', 'import lxml, mendwright'], { cwd: REPO, env, encoding: 'utf-8' });
    if (result.status === 0) {
      python = command;
      break;
    }
  }
  return python;
}

/** python -m mendwright args, the Python package's command line: [exit status, stdout, stderr]. */
export function mendwrightCli(args: string[]): [number, string, string] {
  const command = pythonCommand();
  if (!command) throw new Error('no Python that imports mendwright from ' + REPO);
  const result = spawnSync(command, ['-m', 'mendwright', ...args], {
    cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  return [result.status ?? -1, result.stdout, result.stderr];
}
