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
//
// A candidate may be a getBy* call written as its key (see getby.ts), which the driver runs as
// that call. The key of a heal.page.getBy*() call is looked up by resolveGetBy() instead, whose
// rule differs: the call itself first, and its saved heals only while the call matches nothing.
// A call that matches several elements, or one hidden element, is not broken, and goes to
// Playwright as written (THROUGH).

import type { Locator } from '@playwright/test';

import type { PlaywrightDriver } from './driver';
import { AttentionRequired, InvalidSelector } from './errors';
import { checkSyntax } from './selectors';
import { runTarget } from './shadow';
import type { Candidate, SelectorStore } from './store';

/** Whether a key's element must be visible to count: not for a checkbox, which a styled one
 * hides behind its label. */
export function requireVisible(store: SelectorStore, key: string): boolean {
  try {
    return store.metaOf(key).expectedRole !== 'checkbox';
  } catch {
    return true;
  }
}

/** [runnable, errors]: the candidates whose selector and frame selector pass checkSyntax and
 * that can run where they were saved, and the errors of the others (the Python package's
 * resolver._split_runnable). A getBy* call is a candidate like any selector: the driver runs
 * it. */
export function splitRunnable(key: string, candidates: Candidate[]): [Candidate[], Error[]] {
  const runnable: Candidate[] = [];
  const errors: Error[] = [];
  for (const candidate of candidates) {
    try {
      checkSyntax(candidate.xpath, { calls: true });
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

/** What resolveGetBy() gives when the call matches something other than one usable element
 * (several, one hidden one), or the browser rejects it: Playwright runs the call as written,
 * and no saved heal is used. */
export const THROUGH: unique symbol = Symbol('through');

/** [count, visible] of the call key itself on the page, visible null when it was not asked.
 * InvalidSelector propagates (the browser rejected the call). */
async function callState(driver: PlaywrightDriver, key: string, visible: boolean): Promise<[number, boolean | null]> {
  const count = await driver.count(key);
  return [count, count === 1 && visible ? await driver.isVisible(key) : null];
}

/** True when [count, visible] is one element mendwright can use: visible, unless the key's role
 * is checkbox. */
function callUsable(state: [number, boolean | null], visible: boolean): boolean {
  return state[0] === 1 && (!visible || Boolean(state[1]));
}

/** resolve() for the key of a heal.page.getBy*() call (see getby.ts; the Python package's
 * resolve_get_by). Each poll:
 *
 * 1. The call itself (the key): one visible element, or one of role checkbox, is scrolled into
 *    view, recorded and returned. One element or more otherwise, or a call the browser rejects:
 *    THROUGH, at once.
 * 2. Only when the call matches nothing: the key's other candidates (saved heals), in store
 *    order, the first that matches one (visible) element. One that cannot run is dropped with
 *    its error.
 * 3. At the deadline: AttentionRequired, as resolve() throws it.
 *
 * first is [count, visible] of the call, already read by the caller, used in the first poll
 * instead of asking the page again. */
export async function resolveGetBy(store: SelectorStore, driver: PlaywrightDriver, key: string,
                                   options: { timeoutMs: number; pollMs?: number; first?: [number, boolean | null] | null }):
    Promise<Locator | typeof THROUGH> {
  const visible = requireVisible(store, key);
  const candidates = store.candidates(key);
  const [runnable, errors] = splitRunnable(key, candidates.filter((c) => c.xpath !== key));
  const pollMs = options.pollMs ?? 200;
  const deadline = Date.now() + options.timeoutMs;
  let first = options.first ?? null;
  for (;;) {
    let state: [number, boolean | null];
    try {
      state = first ?? (await callState(driver, key, visible));
      if (state[0] === 1 && visible && state[1] === null) state = await callState(driver, key, visible);
    } catch (error) {
      if (error instanceof InvalidSelector) return THROUGH;
      throw error;
    }
    first = null;
    if (callUsable(state, visible)) {
      try {
        await driver.scrollIntoView(key);
      } catch {
        // as Playwright's own scroll before an action would
      }
      store.recordSuccess(key, key);
      return driver.locator(key);
    }
    if (state[0] >= 1) return THROUGH;
    for (const candidate of [...runnable]) {
      let handle: Locator | null;
      try {
        handle = await matchHandle(driver, candidate, visible);
      } catch (error) {
        runnable.splice(runnable.indexOf(candidate), 1);
        errors.push((error as InvalidSelector).forKey(key));
        continue;
      }
      if (handle !== null) {
        const { selector, frame, hosts } = runTarget(candidate);
        try {
          await driver.scrollIntoView(selector, frame, hosts);
        } catch {
          // as above
        }
        store.recordSuccess(key, candidate.xpath);
        return handle;
      }
    }
    if (Date.now() >= deadline) throw new AttentionRequired(noMatchMessage(key, candidates, errors));
    await sleep(pollMs);
  }
}

/** present() for the key of a heal.page.getBy*() call: one pass of resolveGetBy()'s rule, with
 * no wait, no scrolling and nothing recorded. True when the call matches one usable element, or
 * matches nothing and a saved selector of the key does; false when the call matches several
 * elements or one hidden one, the browser rejects it, or nothing matches (the Python package's
 * present_get_by). */
export async function presentGetBy(store: SelectorStore, driver: PlaywrightDriver, key: string): Promise<boolean> {
  const visible = requireVisible(store, key);
  let state: [number, boolean | null];
  try {
    state = await callState(driver, key, visible);
  } catch (error) {
    if (error instanceof InvalidSelector) return false;
    throw error;
  }
  if (state[0] >= 1) return callUsable(state, visible);
  const [runnable] = splitRunnable(key, store.candidates(key).filter((c) => c.xpath !== key));
  for (const candidate of runnable) {
    try {
      if ((await matchHandle(driver, candidate, visible)) !== null) return true;
    } catch {
      continue;
    }
  }
  return false;
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
