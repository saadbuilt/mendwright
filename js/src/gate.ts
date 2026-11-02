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
//
// A heal inside an iframe (see scope.ts) runs all of this in the iframe's document, through
// a driver on its Frame: the captured page is the iframe's, only the quarantines made in that
// iframe count, and the XPath is saved with the iframe's selector as its frameXPath and its
// 'xpath=' prefix written out, so it never shares a store row with the same XPath on the page.
//
// A heal inside an open shadow root (see scope.ts) checks the XPath in the HTML of that shadow
// root, where only the quarantines made in that shadow root count. No XPath runs inside a
// shadow root, so the CSS forms of its element (css.ts) are tried in turn instead of the
// XPath: one not quarantined or superseded, and the live check inside the shadow root,
// through the ShadowDriver. The first that passes is saved as the chain of the hosts and the
// CSS, with the hosts as its shadowHosts. An element no CSS form tells apart is refused.

import { cssXpath } from './css';
import { TrustedTypesRefused } from './driver';
import type { Proposal } from './prompt';
import type { HealDriver } from './scope';
import { CSS, XPATH, classify, frameSpelling, hasPosition, isBrittle, normalize, stripPrefix } from './selectors';
import { formsRead, shadowText, splitText, type Hosts } from './shadow';
import type { KeyMeta, SelectorStore } from './store';

export const NOT_LIVE = 'passed static validation but not uniquely present on the live page';
export const UNREADABLE = 'could not read the live element, so its role could not be checked';
export const POSITIONAL = 'it uses a position ([2], last() or position()), but parts of the page were left '
  + 'out of the snapshot to fit the prompt, so positions there do not match the live page. Anchor on '
  + 'attributes or text instead';
export const UNPARSED = 'the captured page could not be read';
/** apply()'s reason for an element inside a shadow root that no CSS selector tells apart. */
export const NO_CSS = 'no CSS selector tells its element apart inside the shadow root (mendwright builds one from a '
  + 'test id, an id, other attributes or class names, and CSS cannot match text), and only CSS runs inside a shadow root';
const SAME_ELEMENT = 'previously quarantined (the same element as a quarantined heal; choose a different element)';
const RETIRED: Record<string, string> = {
  quarantined: 'previously quarantined',
  superseded: 'previously superseded',
};

/** '' when xpath passes the live check, else the reason it does not. frame, when given, is
 * the selector of an iframe of the page the driver reads: xpath is checked inside it. */
export async function liveReason(driver: HealDriver, xpath: string, meta: Pick<KeyMeta, 'expectedRole'>,
                                 frame: string | null = null): Promise<string> {
  try {
    if ((await driver.count(xpath, frame)) !== 1) return NOT_LIVE;
    const live = await driver.describe(xpath, frame);
    if (!live || typeof live !== 'object') return UNREADABLE;
    const want = meta.expectedRole;
    if (want && want !== 'unknown' && live.role !== want) {
      return 'live element role mismatch: got ' + String(live.role) + ' want ' + String(want);
    }
    if (want === 'checkbox') return '';
    if (!(await driver.isVisible(xpath, frame))) return NOT_LIVE;
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
export async function validateStatic(driver: HealDriver, xpath: string, html: string, role: string,
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

/** XPath bodies of the key's quarantined selectors in one place: frame '' for the page, or
 * the selector of an iframe for that iframe, and hosts the shadow hosts of a shadow root, whose
 * HTML the check then reads. A selector quarantined inside an iframe or a shadow root says
 * nothing about an element that looks the same on the page, and the other way round. Inside a
 * shadow root, where a heal is saved as the chain of its hosts and a CSS selector
 * (shadow.shadowText), that CSS, of the forms mendwright builds, is matched through its XPath
 * twin (css.cssXpath), so the element stays blocked under any CSS form (the Python package's
 * client._quarantined_paths). */
export function quarantinedBodies(store: SelectorStore, key: string, frame = '', hosts: Hosts = []): string[] {
  const out: string[] = [];
  for (const selector of store.quarantined(key, frame, hosts.length ? hosts : null)) {
    try {
      if (hosts.length) {
        const parts = splitText(selector);
        const css = parts.length === hosts.length + 1 ? parts[parts.length - 1] : selector;
        const body = classify(css) === CSS ? cssXpath(stripPrefix(css)) : null;
        if (body !== null) out.push(body);
      } else if (classify(selector) === XPATH) {
        out.push(xpathForms(selector)[0]);
      }
    } catch {
      // not a selector this check can run
    }
  }
  return out;
}

/** apply() for a proposal checked in the HTML of a shadow root (body, which matches one
 * element of html), the Python package's client._shadow_heal: [saved, 'ok'], or [null, the
 * last reason]. The element's CSS forms are tried in turn: not retired, and the live check
 * inside the shadow root (driver is a ShadowDriver). The first that passes is saved as the
 * chain of the hosts and the CSS, with the hosts as its shadowHosts. */
async function shadowHeal(store: SelectorStore, key: string, html: string, body: string, meta: KeyMeta,
                          driver: HealDriver, hosts: string[]): Promise<[string | null, string]> {
  let read: unknown = null;
  try {
    read = await driver.lib('cssForms', { html, xpath: body });
  } catch {
    // no form can be built
  }
  const found = formsRead(read);
  const forms = found && found.count === 1 ? found.forms : [];
  let reason = NO_CSS;
  for (const css of forms) {
    const saved = shadowText(hosts, css);
    const retired = RETIRED[store.status(key, saved) ?? ''];
    if (retired) {
      reason = retired;
      continue;
    }
    const live = await liveReason(driver, css, meta);
    if (live) {
      reason = live;
      continue;
    }
    store.addCandidate(key, saved, { source: 'heal', frameXPath: '', shadowHosts: hosts });
    return [saved, 'ok'];
  }
  return [null, reason];
}

/** Check the proposal and save the first of its XPaths that passes the whole gate as a
 * heal of key. Returns [saved selector, 'ok'], or [null, the last reason] for a re-prompt.
 * positional false refuses an XPath with a position in it (the model read a page that was
 * cut to fit the prompt). frame is the selector of the iframe the heal runs in, when it runs
 * in one (html and driver are then the iframe's): the XPath is saved with it as its
 * frameXPath and its 'xpath=' prefix written out, and only the quarantines made in that
 * iframe block it. hosts are the shadow hosts of the open shadow root the heal runs in, when
 * it runs in one (html is then that shadow root's HTML and driver a ShadowDriver): the XPath
 * is checked in that HTML, and a CSS form of its element is saved instead (see shadowHeal),
 * as the chain of the hosts and the CSS; only the quarantines made in that shadow root block
 * it, and an element no CSS form tells apart is refused (NO_CSS). */
export async function apply(store: SelectorStore, key: string, proposal: Partial<Proposal>, html: string,
                            meta: KeyMeta, driver: HealDriver, positional = true,
                            frame: string | null = null, hosts: string[] | null = null): Promise<[string | null, string]> {
  const proposed = [proposal.xpath, ...(Array.isArray(proposal.fallbacks) ? proposal.fallbacks : [])];
  const inside = hosts !== null && hosts.length > 0;
  const bad = quarantinedBodies(store, key, frame ?? '', inside ? (hosts as string[]) : []);
  let reason = 'no candidates in proposal';
  for (const candidate of proposed) {
    if (!candidate) continue;
    if (typeof candidate !== 'string') {
      reason = 'proposal is not a string';
      continue;
    }
    const [body, stored] = xpathForms(candidate);
    const xpath = frame ? frameSpelling(stored) : stored;
    if (!positional && hasPosition(body)) {
      reason = POSITIONAL;
      continue;
    }
    // Inside a shadow root the XPath is never saved, so only its CSS forms can be retired.
    const retired = inside ? undefined : RETIRED[(xpath === null ? null : store.status(key, xpath)) ?? ''];
    if (retired) {
      reason = retired;
      continue;
    }
    const result = await validateStatic(driver, body, html, meta.expectedRole, bad);
    reason = result.reason === 'quarantined' ? SAME_ELEMENT : result.reason;
    if (!result.ok) continue;
    if (xpath === null || classify(xpath) !== XPATH) {
      reason = 'not an XPath expression';
      continue;
    }
    if (inside) {
      const [saved, why] = await shadowHeal(store, key, html, body, meta, driver, hosts as string[]);
      reason = why;
      if (saved !== null) return [saved, why];
      continue;
    }
    const live = await liveReason(driver, xpath, meta);
    if (live) {
      reason = live;
      continue;
    }
    // frameXPath '' puts a candidate with the same text, saved inside an iframe before, on
    // the page, where this heal was checked; an iframe's selector moves one to that iframe.
    // shadowHosts [] takes one out of a shadow root.
    store.addCandidate(key, xpath, { source: 'heal', frameXPath: frame ?? '', shadowHosts: [] });
    return [xpath, result.reason];
  }
  return [null, reason];
}
