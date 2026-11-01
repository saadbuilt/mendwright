// The gate every proposed selector must pass before it is saved or acted on, whether the
// fingerprint tier or a model proposed it (Python's client.apply, validation.validate and
// gate.live_reason):
//
//   1. It is an XPath, and not an index-only path such as /html[1]/body[1]/div[3]/input[1].
//   2. The store does not hold it as quarantined or superseded for this key.
//   3. On the captured page: it matches exactly one element, that element has the key's
//      role, and no quarantined selector of the key reaches that element.
//   4. On the live page: it matches exactly one element, the element is read again and has
//      the key's role, and it is visible (a hidden checkbox is allowed: a styled checkbox
//      hides its native input).
//
// The capture is taken before a model call, which takes seconds, so the page can change in
// between; the live check is what makes the element that is saved and acted on the one
// that was checked. Any error on the way refuses the proposal (fail closed).

import { TrustedTypesRefused, type PlaywrightDriver } from './driver';
import type { Proposal } from './prompt';
import { XPATH, classify, hasPosition, isBrittle, normalize } from './selectors';
import type { KeyMeta, SelectorStore } from './store';

export const NOT_LIVE = 'passed static validation but not uniquely present on the live page';
export const UNREADABLE = 'could not read the live element, so its role could not be checked';
export const POSITIONAL = 'it uses a position ([2], last() or position()), but parts of the page were left '
  + 'out of the snapshot to fit the prompt, so positions there do not match the live page. Anchor on '
  + 'attributes or text instead';
export const UNPARSED = 'the captured page could not be read';
const SAME_ELEMENT = 'previously quarantined (the same element as a quarantined heal; choose a different element)';
const RETIRED: Record<string, string> = {
  quarantined: 'previously quarantined',
  superseded: 'previously superseded',
};

/** '' when xpath passes the live check, else the reason it does not. */
export async function liveReason(driver: PlaywrightDriver, xpath: string, meta: Pick<KeyMeta, 'expectedRole'>): Promise<string> {
  try {
    if ((await driver.count(xpath)) !== 1) return NOT_LIVE;
    const live = await driver.describe(xpath);
    if (!live || typeof live !== 'object') return UNREADABLE;
    const want = meta.expectedRole;
    if (want && want !== 'unknown' && live.role !== want) {
      return 'live element role mismatch: got ' + String(live.role) + ' want ' + String(want);
    }
    if (want === 'checkbox') return '';
    if (!(await driver.isVisible(xpath))) return NOT_LIVE;
    return '';
  } catch {
    return NOT_LIVE;
  }
}

interface StaticResult {
  ok: boolean;
  reason: string;
  count: number;
}

/** The static half of the gate, on the captured page html (parsed in the browser, where no
 * script of it runs). A page that cannot be parsed refuses the proposal, with why when its
 * Trusted Types policy is the reason. */
export async function validateStatic(driver: PlaywrightDriver, xpath: string, html: string, role: string,
                                     quarantined: string[] = []): Promise<StaticResult> {
  if (isBrittle(xpath)) return { ok: false, reason: 'brittle absolute-index path', count: 0 };
  try {
    const result = await driver.lib<StaticResult>('validate', { xpath, html, role, quarantined });
    if (!result || typeof result.ok !== 'boolean') return { ok: false, reason: UNPARSED, count: 0 };
    return result;
  } catch (error) {
    const reason = error instanceof TrustedTypesRefused ? UNPARSED + ': ' + error.message : UNPARSED;
    return { ok: false, reason, count: 0 };
  }
}

/** (body, stored): the expression the browser evaluates, and the string to store so it
 * runs as XPath: an 'xpath=' prefix is kept only where the text would read as CSS. */
export function xpathForms(proposed: string): [string, string] {
  const text = proposed.trim();
  const body = text.startsWith('xpath=') ? text.slice('xpath='.length).trim() : text;
  return [body, normalize('xpath=' + body)];
}

/** XPath bodies of the key's quarantined selectors on the page. */
function quarantinedBodies(store: SelectorStore, key: string): string[] {
  const out: string[] = [];
  for (const selector of store.quarantined(key, '')) {
    try {
      if (classify(selector) === XPATH) out.push(xpathForms(selector)[0]);
    } catch {
      // not a selector this check can run
    }
  }
  return out;
}

/** Check the proposal and save the first of its XPaths that passes the whole gate as a
 * heal of key. Returns [saved xpath, 'ok'], or [null, the last reason] for a re-prompt.
 * positional false refuses an XPath with a position in it (the model read a page that was
 * cut to fit the prompt). */
export async function apply(store: SelectorStore, key: string, proposal: Partial<Proposal>, html: string,
                            meta: KeyMeta, driver: PlaywrightDriver, positional = true): Promise<[string | null, string]> {
  const proposed = [proposal.xpath, ...(Array.isArray(proposal.fallbacks) ? proposal.fallbacks : [])];
  const bad = quarantinedBodies(store, key);
  let reason = 'no candidates in proposal';
  for (const candidate of proposed) {
    if (!candidate) continue;
    if (typeof candidate !== 'string') {
      reason = 'proposal is not a string';
      continue;
    }
    const [body, xpath] = xpathForms(candidate);
    if (!positional && hasPosition(body)) {
      reason = POSITIONAL;
      continue;
    }
    const retired = RETIRED[store.status(key, xpath) ?? ''];
    if (retired) {
      reason = retired;
      continue;
    }
    const result = await validateStatic(driver, body, html, meta.expectedRole, bad);
    reason = result.reason === 'quarantined' ? SAME_ELEMENT : result.reason;
    if (!result.ok) continue;
    if (classify(xpath) !== XPATH) {
      reason = 'not an XPath expression';
      continue;
    }
    const live = await liveReason(driver, xpath, meta);
    if (live) {
      reason = live;
      continue;
    }
    // frameXPath '' puts a candidate with the same text, saved inside an iframe before, on
    // the page, where this heal was checked.
    store.addCandidate(key, xpath, { source: 'heal', frameXPath: '' });
    return [xpath, result.reason];
  }
  return [null, reason];
}
