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
//      hides its native input). For the key of a heal.page.getByRole() call (see getby.ts),
//      Playwright itself must also give the element the call's ARIA role, and its level when
//      the call has one (driver.roleMatch): the gate's role is mostly the tag, and a
//      <button role="tab"> that lost its role is still a button to it. A driver that cannot
//      tell refuses the heal.
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
import { TrustedTypesRefused, type ItemRow, type PlaywrightDriver } from './driver';
import { ROLE_UNREADABLE, parse, roleMismatch, type GetByCall } from './getby';
import type { Proposal } from './prompt';
import type { HealDriver } from './scope';
import { CSS, XPATH, classify, frameSpelling, hasPosition, isBrittle, normalize, stripPrefix } from './selectors';
import { formsRead, hostsOf, shadowText, splitText, type Hosts } from './shadow';
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

/** driver.roleMatch() when the driver has it, else null: whether the browser gives the one
 * element selector matches the ARIA role role (and level). null too when it throws or answers
 * anything but true or false, so the live check refuses the heal (the Python package's
 * driver_role_match). */
export async function driverRoleMatch(driver: HealDriver, selector: string, role: string, level: number | null,
                                      frame: string | null = null): Promise<boolean | null> {
  if (typeof driver.roleMatch !== 'function') return null;
  try {
    const out = await driver.roleMatch(selector, role, level, frame);
    return typeof out === 'boolean' ? out : null;
  } catch {
    return null;
  }
}

/** '' when xpath passes the live check, else the reason it does not. frame, when given, is
 * the selector of an iframe of the page the driver reads: xpath is checked inside it. call is
 * the GetByCall of the key when the key is a getBy* call (see getby.ts): for getByRole(),
 * Playwright must give the element the call's role, and its level when it has one. */
export async function liveReason(driver: HealDriver, xpath: string, meta: Pick<KeyMeta, 'expectedRole'>,
                                 frame: string | null = null, call: GetByCall | null = null): Promise<string> {
  try {
    if ((await driver.count(xpath, frame)) !== 1) return NOT_LIVE;
    const live = await driver.describe(xpath, frame);
    if (!live || typeof live !== 'object') return UNREADABLE;
    const want = meta.expectedRole;
    if (want && want !== 'unknown' && live.role !== want) {
      return 'live element role mismatch: got ' + String(live.role) + ' want ' + String(want);
    }
    if (call !== null && call.method === 'get_by_role') {
      const ok = await driverRoleMatch(driver, xpath, call.value as string, call.level, frame);
      if (ok === null) return ROLE_UNREADABLE;
      if (!ok) return roleMismatch(call);
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
    const live = await liveReason(driver, css, meta, null, parse(key));
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
    const live = await liveReason(driver, xpath, meta, null, parse(key));
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

// --- the list gate (the Python package's client.list_gate and gate.list_live_reason) ------------
//
// A list found for heal.locatorAll() with nonempty: true (see lists.ts) is never used: it is
// proposed, for a person to check. These are the checks it passes before it is proposed.

/** listLiveReason()'s reason when the live page could not be read (fail closed). */
export const LIST_UNREADABLE = 'the live page could not be read';
/** listGate()'s reasons for a list a quarantine holds. An accepted list heal is quarantined
 * when the selector it replaced matches again (SelectorStore.retireListHeals). */
export const REJECTED_LIST = 'it is a list heal you rejected, or one mendwright quarantined: it quarantines a list heal '
  + 'you accepted once the selector it replaced matches again, even on one element';
export const REJECTED_ELEMENTS = 'it takes elements of a list heal you rejected, or of a quarantined selector of this key';
/** The static check's reason for an XPath with a position. */
export const LIST_POSITIONAL = 'it uses a position ([2], last() or position()), and a list heal never does';

function tooFew(count: number): string {
  return 'it matches ' + count + (count === 1 ? ' element' : ' elements') + ' on the live page, and a list heal needs 2 '
    + 'or more';
}

/** '' when the list xpath passes the live check, else the reason it does not: the first 200
 * elements it matches, read in one round trip, are two or more, each of the key's role, and at
 * least one is visible. Any error fails it (fail closed). */
export async function listLiveReason(driver: PlaywrightDriver, xpath: string, meta: Pick<KeyMeta, 'expectedRole'>):
    Promise<string> {
  const want = meta.expectedRole;
  let items: ItemRow[];
  try {
    items = await driver.listItems(xpath);
  } catch {
    return LIST_UNREADABLE;
  }
  if (!Array.isArray(items)) return LIST_UNREADABLE;
  if (items.length < 2) return tooFew(items.length);
  for (const item of items) {
    if (want && want !== 'unknown' && item.role !== want) {
      return 'live element role mismatch: got ' + String(item.role) + ' want ' + String(want);
    }
  }
  if (!items.some((item) => item.visible)) return 'none of its ' + items.length + ' elements is visible on the live page';
  return '';
}

export interface ListCheck {
  ok: boolean;
  reason: string;
  count: number;
  /** A quarantined selector of the key reaches one of the elements. */
  taken: boolean;
}

/** The static check of a list heal (the Python package's lists.validate): ok when xpath, with
 * no position and not an index-only path, matches two or more elements of role in the captured
 * page html and none of them is inside another; taken when one of the quarantined XPaths
 * reaches one of them. */
export async function validateList(driver: HealDriver, xpath: string, html: string, role: string,
                                   quarantined: string[] = []): Promise<ListCheck> {
  if (isBrittle(xpath)) return { ok: false, reason: 'brittle absolute-index path', count: 0, taken: false };
  if (hasPosition(xpath)) return { ok: false, reason: LIST_POSITIONAL, count: 0, taken: false };
  try {
    const result = await driver.lib<ListCheck>('listValidate', { xpath, html, role, quarantined });
    if (!result || typeof result.ok !== 'boolean') return { ok: false, reason: UNPARSED, count: 0, taken: false };
    return result;
  } catch (error) {
    const reason = error instanceof TrustedTypesRefused ? UNPARSED + ': ' + error.message : UNPARSED;
    return { ok: false, reason, count: 0, taken: false };
  }
}

/** The first of the key's selectors from code (on the page, not in an iframe or a shadow root)
 * that matches one element or more on the live page now, as a list that was still loading does
 * once it came while mendwright looked for it, else ''. Errors are skipped. */
async function codeBack(store: SelectorStore, key: string, driver: PlaywrightDriver): Promise<string> {
  let own: string[];
  try {
    own = store.candidates(key).filter((c) => (c.source === 'code' || c.source === 'seed') && !c.frameXPath
      && !hostsOf(c).length).map((c) => c.xpath);
  } catch {
    return '';
  }
  for (const selector of own) {
    try {
      if (await driver.count(selector)) return selector;
    } catch {
      continue;
    }
  }
  return '';
}

/** True when one of the key's selectors from code is CSS. */
function codeUsesCss(store: SelectorStore, key: string): boolean {
  try {
    return store.candidates(key).some((c) => c.source === 'code' && typeof c.xpath === 'string'
      && classify(c.xpath) === CSS);
  } catch {
    return false;
  }
}

/** The checks of a list found for heal.locatorAll() with nonempty: true, which mendwright
 * proposes and never uses: [kind, reason, count, css]. kind is
 *
 * - 'came' when a selector of the key's code matches on the live page now (reason is that
 *   selector): the list came while mendwright looked for it. Checked first, so a list that came
 *   is never refused;
 * - 'accepted' when the key already has this XPath as a list heal a person accepted;
 * - 'refused' with the reason: not an XPath; a list heal of the key that was rejected or
 *   quarantined; validateList(); an element a quarantined selector of the key reaches; or the
 *   live check (listLiveReason);
 * - 'ok', with count the elements it matches in the captured page, and css the CSS form css
 *   when one of the key's selectors in code is CSS and css matches as many elements on the live
 *   page as the XPath, else null.
 *
 * A superseded or proposed list heal with the same XPath passes: it may be proposed again. */
export async function listGate(store: SelectorStore, key: string, xpath: string, html: string, meta: KeyMeta,
                               driver: PlaywrightDriver, css: string | null = null):
    Promise<['came' | 'accepted' | 'refused' | 'ok', string, number, string | null]> {
  const came = await codeBack(store, key, driver);
  if (came) return ['came', came, 0, null];
  if (typeof xpath !== 'string' || !xpath.trim()) return ['refused', 'no candidates in proposal', 0, null];
  const [body, stored] = xpathForms(xpath);
  if (classify(stored) !== XPATH) return ['refused', 'not an XPath expression', 0, null];
  const status = store.status(key, stored);
  if (status === 'quarantined') return ['refused', REJECTED_LIST, 0, null];
  if (status === 'accepted') return ['accepted', '', 0, null];
  const result = await validateList(driver, body, html, meta.expectedRole, quarantinedBodies(store, key));
  if (!result.ok) return ['refused', result.reason, result.count, null];
  if (result.taken) return ['refused', REJECTED_ELEMENTS, result.count, null];
  const live = await listLiveReason(driver, stored, meta);
  if (live) return ['refused', live, result.count, null];
  let twin: string | null = null;
  if (css && codeUsesCss(store, key)) {
    try {
      if ((await driver.count(css)) === (await driver.count(stored))) twin = css;
    } catch {
      twin = null;
    }
  }
  return ['ok', 'ok', result.count, twin];
}
