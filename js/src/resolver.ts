// Resolve a key to one element (Python's mendwright/resolver.py): try its active candidates
// in store order, the code's selectors first, and take the first that matches exactly one
// element, visible unless the key's role is checkbox (a styled checkbox hides its native
// input). Poll until timeoutMs, then raise AttentionRequired; only then does the caller try
// a heal. A candidate the browser rejects is dropped from the poll, and when none can run,
// InvalidSelector is raised at once instead of waiting, so no model call is spent on a typo.
//
// A candidate the Python package saved inside an open shadow root (its shadowHosts) runs as
// the Python package runs it: its CSS, through its hosts, and it counts only what matches in
// that shadow root (see shadow.ts). One that cannot run that way, such as one behind a host
// nothing tells apart, is not runnable, like a selector the browser rejects.

import type { Locator } from '@playwright/test';

import type { PlaywrightDriver } from './driver';
import { AttentionRequired, InvalidSelector } from './errors';
import { checkSyntax } from './selectors';
import { runTarget } from './shadow';
import type { Candidate, SelectorStore } from './store';

function requireVisible(store: SelectorStore, key: string): boolean {
  try {
    return store.metaOf(key).expectedRole !== 'checkbox';
  } catch {
    return true;
  }
}

function splitRunnable(key: string, candidates: Candidate[]): [Candidate[], Error[]] {
  const runnable: Candidate[] = [];
  const errors: Error[] = [];
  for (const candidate of candidates) {
    try {
      checkSyntax(candidate.xpath);
      if (candidate.frameXPath) checkSyntax(candidate.frameXPath);
      runTarget(candidate);
      runnable.push(candidate);
    } catch (error) {
      errors.push(error instanceof InvalidSelector ? error.forKey(key) : (error as Error));
    }
  }
  return [runnable, errors];
}

/** The Locator when candidate matches exactly one (visible) element now, else null.
 * InvalidSelector propagates; any other error counts as no match. */
async function matchHandle(driver: PlaywrightDriver, candidate: Candidate, visible: boolean): Promise<Locator | null> {
  const { selector, frame, hosts } = runTarget(candidate);
  try {
    if ((await driver.count(selector, frame, hosts)) === 1 && (!visible || (await driver.isVisible(selector, frame, hosts)))) {
      return driver.locator(selector, frame, hosts);
    }
  } catch (error) {
    if (error instanceof InvalidSelector) throw error;
    return null;
  }
  return null;
}

export function noMatchMessage(key: string, candidates: Candidate[], errors: Error[]): string {
  let message = 'No matching element for: ' + key + ' (tried ' + candidates.length + ' candidate(s)).';
  if (errors.length) {
    const first = (errors[0] as InvalidSelector).reason || errors[0].message;
    message += ' ' + errors.length + ' of them could not run: ' + first;
  }
  return message;
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export async function resolve(store: SelectorStore, driver: PlaywrightDriver, key: string,
                              options: { timeoutMs: number; pollMs?: number }): Promise<Locator> {
  const candidates = store.candidates(key);
  if (!candidates.length) throw new AttentionRequired('No selector candidates for: ' + key);
  const [runnable, errors] = splitRunnable(key, candidates);
  if (!runnable.length) throw errors[0];
  const visible = requireVisible(store, key);
  const pollMs = options.pollMs ?? 200;
  const deadline = Date.now() + options.timeoutMs;
  for (;;) {
    for (const candidate of [...runnable]) {
      let handle: Locator | null;
      try {
        handle = await matchHandle(driver, candidate, visible);
      } catch (error) {
        runnable.splice(runnable.indexOf(candidate), 1);
        errors.push((error as InvalidSelector).forKey(key));
        if (!runnable.length) throw errors[0];
        continue;
      }
      if (handle !== null) {
        const { selector, frame, hosts } = runTarget(candidate);
        await driver.scrollIntoView(selector, frame, hosts);
        store.recordSuccess(key, candidate.xpath);
        return handle;
      }
    }
    if (Date.now() >= deadline) throw new AttentionRequired(noMatchMessage(key, candidates, errors));
    await sleep(pollMs);
  }
}

/** True when one of key's candidates matches exactly one (visible) element now. No waiting,
 * no healing, nothing recorded. */
export async function present(store: SelectorStore, driver: PlaywrightDriver, key: string): Promise<boolean> {
  const visible = requireVisible(store, key);
  const candidates = store.candidates(key);
  const [runnable, errors] = splitRunnable(key, candidates);
  for (const candidate of runnable) {
    try {
      if ((await matchHandle(driver, candidate, visible)) !== null) return true;
    } catch (error) {
      errors.push((error as InvalidSelector).forKey(key));
    }
  }
  if (candidates.length && errors.length === candidates.length) throw errors[0];
  return false;
}

/** For heal.page's isVisible(): whether the first of key's candidates that matches one
 * element shows it visible now. No wait, no heal, nothing recorded. */
export async function peekVisible(store: SelectorStore, driver: PlaywrightDriver, key: string): Promise<boolean> {
  const visible = requireVisible(store, key);
  const [runnable] = splitRunnable(key, store.candidates(key));
  for (const candidate of runnable) {
    let handle: Locator | null = null;
    try {
      handle = await matchHandle(driver, candidate, visible);
    } catch {
      continue;
    }
    if (handle !== null) {
      try {
        return await handle.isVisible();
      } catch {
        return false;
      }
    }
  }
  return false;
}
