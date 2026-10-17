// The Playwright driver: the only code here that talks to the browser. It wraps a
// Playwright Page or Frame and answers the selector queries the resolver, the gate and the
// fingerprint tier make, each against an optional iframe selector (a candidate saved inside
// an iframe has one) or the shadow hosts of a CSS selector saved inside an open shadow root
// (see shadow.ts). A selector the browser cannot parse raises InvalidSelector at once.
//
// The page-side work runs mendwrightLib() (browser/lib.ts), sent as its source text.

import type { Frame, JSHandle, Locator, Page } from '@playwright/test';

import { mendwrightLib } from './browser/lib';
import { InvalidSelector } from './errors';
import { fromRaw, type Fingerprint, type RawElement } from './fingerprint';
import { pySlice } from './py';
import { roleFor } from './roles';
import { playwrightSelector } from './selectors';
import { savedHosts, type Hosts } from './shadow';

export type PageLike = Page | Frame;

const LIB = '(' + mendwrightLib.toString() + ')()';
// The helpers of mendwrightLib() the driver calls by name. Only these names are ever put
// into a function's source below, never text from a page, a store or a caller.
const LIB_NAMES = new Set(['collect', 'describe', 'recordRaw', 'inventory', 'validate', 'cleanHtml',
  'candidates', 'indexOfMatch', 'robustXpaths', 'placed']);
export type LibName = 'collect' | 'describe' | 'recordRaw' | 'inventory' | 'validate' | 'cleanHtml'
  | 'candidates' | 'indexOfMatch' | 'robustXpaths' | 'placed';

// The helpers that parse an HTML string (the captured page, or its cleaned copy) and read
// nothing of the live page. A page that enforces Trusted Types refuses a plain string to
// DOMParser, so these get mendwright's own policy there (see PlaywrightDriver.lib).
const PARSERS = new Set<string>(['validate', 'cleanHtml', 'candidates', 'indexOfMatch', 'robustXpaths']);
const TRUSTED_TYPES = 'mendwright:trusted-types';

/** What lib() throws when the page enforces Trusted Types and its trusted-types directive
 * does not allow mendwright's policy, so no HTML string can be parsed in it. */
export class TrustedTypesRefused extends Error {
  constructor() {
    super("the page enforces Trusted Types, and its trusted-types directive does not allow the policy 'mendwright'");
    this.name = 'TrustedTypesRefused';
  }
}

// mendwright's policy: it passes the string through as it is. The page produced that HTML
// itself, and DOMParser runs none of its scripts. The policy is made once per document and
// shared by every healer of the page, and only Playwright's handle holds it, so the page's
// own scripts cannot reach it.
const MAKE_POLICY = () => (window as any).trustedTypes.createPolicy('mendwright', { createHTML: (text: string) => text });
// page -> its policy, being made or made. Two heals at once share one.
const policies = new WeakMap<object, Promise<JSHandle>>();

function checked(name: string): string {
  if (!LIB_NAMES.has(name)) throw new TypeError('mendwright: no page helper named ' + JSON.stringify(name));
  return name;
}

const pageFunctions = new Map<string, (arg: unknown) => unknown>();
const parserFunctions = new Map<string, (call: { arg: unknown; policy: unknown }) => unknown>();
const elementFunctions = new Map<string, (els: Element[], arg: unknown) => unknown>();
const handleFunctions = new Map<string, (h: unknown, arg: unknown) => unknown>();

// The functions are built from source text in Node, where no page policy applies, and
// Playwright sends each one to the page as its source, as it does any function.
function pageFunction(name: LibName): (arg: unknown) => unknown {
  let fn = pageFunctions.get(name);
  if (!fn) {
    fn = new Function('arg', 'return ' + LIB + '.' + checked(name) + '(arg);') as (arg: unknown) => unknown;
    pageFunctions.set(name, fn);
  }
  return fn;
}

// A parsing helper, run with the document's policy or none: call is { arg, policy }.
function parserFunction(name: LibName): (call: { arg: unknown; policy: unknown }) => unknown {
  let fn = parserFunctions.get(name);
  if (!fn) {
    fn = new Function('call', 'return (' + mendwrightLib.toString() + ')(call.policy).' + checked(name) + '(call.arg);') as
      (call: { arg: unknown; policy: unknown }) => unknown;
    parserFunctions.set(name, fn);
  }
  return fn;
}

function elementsFunction(name: LibName): (els: Element[], arg: unknown) => unknown {
  let fn = elementFunctions.get(name);
  if (!fn) {
    fn = new Function('els', 'arg', 'return ' + LIB + '.' + checked(name) + '(els, arg);') as
      (els: Element[], arg: unknown) => unknown;
    elementFunctions.set(name, fn);
  }
  return fn;
}

// For a JSHandle of collect()'s result: one of its elements, by index, still in the page.
function handleFunction(name: 'xpathFor' | 'outside' | 'raw'): (h: unknown, arg: unknown) => unknown {
  let fn = handleFunctions.get(name);
  if (!fn) {
    const call = name === 'xpathFor' ? 'lib.xpathFor(el)'
      : name === 'outside' ? 'lib.outside(el, arg.ids)' : 'lib.rawOf(el)';
    fn = new Function('h', 'arg', 'var el = h.els[typeof arg === "number" ? arg : arg.index];'
      + ' if (!el || !el.isConnected) return null; var lib = ' + LIB + '; return ' + call + ';') as
      (h: unknown, arg: unknown) => unknown;
    handleFunctions.set(name, fn);
  }
  return fn;
}

// How Playwright words a selector it cannot parse (the Python drivers read the same text).
const BROWSER_SELECTOR_ERROR = /while parsing|Unknown engine|not a valid (?:XPath|selector)|not a legal expression|did not match the expected pattern/i;

/** True for a Playwright error that says the browser cannot parse a selector. */
export function browserRejected(error: unknown): boolean {
  const first = String((error as Error)?.message ?? error).trim().split('\n')[0] ?? '';
  return BROWSER_SELECTOR_ERROR.test(first);
}

function invalid(error: unknown, selector: string): InvalidSelector {
  const first = String((error as Error)?.message ?? error).trim().split('\n')[0] ?? '';
  const detail = /^\w+\.\w+: /.test(first) ? first.slice(first.indexOf(': ') + 2) : first;
  return InvalidSelector.build(selector, 'the browser rejected a selector (' + detail.replace(/\.$/, '') + ').',
    "Fix the selector, or use an XPath such as \"//button[@id='save']\".");
}

interface Placed {
  n: number;
  inside: number;
}

/** count() from placed(): how many elements the Locator matches when one of them is in the
 * shadow root, else 0. Two matches with one outside count 2, so the selector is not unique;
 * one match outside counts 0. */
function placedCount(placed: unknown): number {
  const p = placed as Placed | null;
  if (!p || typeof p.n !== 'number' || typeof p.inside !== 'number') return 0;
  return p.inside > 0 ? p.n : 0;
}

/** True when placed() says the Locator matches something, and all of it is in the shadow
 * root: only then does a read go on. */
function allPlaced(placed: unknown): boolean {
  const p = placed as Placed | null;
  return !!p && typeof p.n === 'number' && p.n > 0 && p.inside === p.n;
}

/** What a fingerprint read gives: the fingerprint, and the shadow hosts around the element
 * ([] for one on the page), or undefined when the read said nothing about them. */
export interface FingerprintRead {
  fp: Fingerprint | null;
  hosts: Hosts | undefined;
}

export interface Description {
  tag: string;
  role: string;
  text: string;
  outer_html: string;
}

export interface InventoryRow {
  tag: string;
  role: string;
  id: string;
  name: string;
  text: string;
  aria_label: string;
}

/** collect()'s result kept in the page: the elements of one role, and their raw reads. */
export interface Snapshot {
  raws: RawElement[];
  xpathFor(index: number): Promise<string | null>;
  outside(index: number, ids: string[]): Promise<string | null>;
  dispose(): Promise<void>;
}

/** True while handle's document is still the page's. */
async function alive(handle: JSHandle): Promise<boolean> {
  try {
    await handle.evaluate(() => true);
    return true;
  } catch {
    return false;
  }
}

export class PlaywrightDriver {
  readonly page: PageLike;

  constructor(page: PageLike) {
    this.page = page;
  }

  /** The Locator for a stored selector, inside the iframe frameXPath when it has one. For a
   * CSS selector inside an open shadow root, hosts are the CSS of its shadow hosts, outermost
   * first, and the Locator is their chain: Playwright's CSS pierces open shadow roots, so
   * every read through it checks first that each match is in that shadow root (placed). */
  locator(selector: string, frameXPath?: string | null, hosts?: string[] | null): Locator {
    const target = playwrightSelector(selector);
    if (hosts && hosts.length) {
      let loc = this.page.locator('css=' + hosts[0]);
      for (const host of hosts.slice(1)) loc = loc.locator('css=' + host);
      return loc.locator(target);
    }
    if (frameXPath) return this.page.frameLocator(playwrightSelector(frameXPath)).locator(target);
    return this.page.locator(target);
  }

  /** What placed() says of the Locator of a selector inside a shadow root. */
  private place(loc: Locator, hosts: string[]): Promise<unknown> {
    return loc.evaluateAll(elementsFunction('placed') as (els: Element[], arg: unknown) => unknown, hosts);
  }

  /** Whether the Locator of a selector inside the shadow root that hosts lead to matches one
   * element now, and that one inside the shadow root. Playwright resolves a Locator again for
   * each action and read, and its CSS pierces, so HealLocator checks this right before each
   * one. One round trip that does not wait; false on any error. */
  async placedOne(loc: Locator, hosts: string[]): Promise<boolean> {
    try {
      const placed = await this.place(loc, hosts);
      return allPlaced(placed) && (placed as Placed).n === 1;
    } catch {
      return false;
    }
  }

  private async probe<T>(selector: string, frameXPath: string | null | undefined,
                         op: (loc: Locator) => Promise<T>, fallback: T, hosts?: string[] | null): Promise<T> {
    const loc = this.locator(selector, frameXPath, hosts);
    try {
      // Inside a shadow root, a match anywhere else makes the read find nothing.
      if (hosts && hosts.length && !allPlaced(await this.place(loc, hosts))) return fallback;
      return await op(loc);
    } catch (error) {
      if (browserRejected(error)) throw invalid(error, selector);
      return fallback;
    }
  }

  async count(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<number> {
    if (hosts && hosts.length) {
      const loc = this.locator(selector, frameXPath, hosts);
      try {
        return placedCount(await this.place(loc, hosts));
      } catch (error) {
        if (browserRejected(error)) throw invalid(error, selector);
        return 0;
      }
    }
    return this.probe(selector, frameXPath, (loc) => loc.count(), 0);
  }

  isVisible(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<boolean> {
    return this.probe(selector, frameXPath, (loc) => loc.isVisible(), false, hosts);
  }

  async scrollIntoView(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<void> {
    await this.probe(selector, frameXPath, (loc) => loc.scrollIntoViewIfNeeded({ timeout: 2000 }), undefined, hosts);
  }

  url(): string {
    try {
      return this.page.url() || '';
    } catch {
      return '';
    }
  }

  /** True when mendwright was opened on a Page, whose url() is the top-level address. */
  topLevel(): boolean {
    return typeof (this.page as Page).mainFrame === 'function';
  }

  async content(): Promise<string> {
    try {
      return await this.page.content();
    } catch {
      return '';
    }
  }

  /** Run one helper of mendwrightLib() in the page. A helper that parses an HTML string
   * gets mendwright's Trusted Types policy on a page that requires one; when the page does
   * not allow that policy, it throws TrustedTypesRefused. */
  async lib<T>(name: LibName, arg?: unknown): Promise<T> {
    if (!PARSERS.has(name)) return this.page.evaluate(pageFunction(name) as (arg: unknown) => T, arg);
    const run = parserFunction(name) as (call: { arg: unknown; policy: unknown }) => T;
    const cached = policies.get(this.page);
    const known = cached ? await cached.catch(() => null) : null;
    if (cached && known) {
      try {
        return await this.page.evaluate(run, { arg, policy: known });
      } catch (error) {
        if (await alive(known)) throw error;
        // The page went to another document since, and the policy went with the old one.
        if (policies.get(this.page) === cached) policies.delete(this.page);
      }
    }
    try {
      return await this.page.evaluate(run, { arg, policy: null });
    } catch (error) {
      if (!String((error as Error)?.message ?? error).includes(TRUSTED_TYPES)) throw error;
    }
    let policy: JSHandle;
    try {
      policy = await this.makePolicy();
    } catch {
      throw new TrustedTypesRefused();
    }
    return this.page.evaluate(run, { arg, policy });
  }

  /** The page's policy: the one being made, or a new one. One the page refused is not kept,
   * so the next document is asked again. */
  private makePolicy(): Promise<JSHandle> {
    const page = this.page;
    let made = policies.get(page);
    if (!made) {
      const making = page.evaluateHandle(MAKE_POLICY);
      made = making;
      policies.set(page, making);
      making.catch(() => {
        if (policies.get(page) === making) policies.delete(page);
      });
    }
    return made;
  }

  /** describe() of the one element selector matches: its tag, gate role, text and outer
   * HTML without value attributes, or null when it matches none or several. */
  describe(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<Description | null> {
    return this.probe(selector, frameXPath,
      (loc) => loc.evaluateAll(elementsFunction('describe') as (els: Element[]) => Description | null), null, hosts);
  }

  /** The fingerprint of the one element selector matches, or null. One round trip, which
   * reads no input value. */
  async fingerprint(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<Fingerprint | null> {
    return (await this.fingerprintRead(selector, frameXPath, hosts)).fp;
  }

  /** fingerprint(), and where the element is: the selectors of the shadow hosts around it,
   * outermost first, as the Python package saves them as the key's shadowHosts (null for a
   * host nothing tells apart), [] for an element on the page itself. Same round trip. */
  fingerprintRead(selector: string, frameXPath?: string | null, hosts?: string[] | null): Promise<FingerprintRead> {
    return this.probe(selector, frameXPath, async (loc) => {
      const raw = await loc.evaluateAll(elementsFunction('recordRaw') as (els: Element[]) => unknown);
      const fp = fromRaw(raw);
      const read = raw && typeof raw === 'object' ? (raw as { hosts?: unknown }).hosts : undefined;
      return { fp, hosts: fp && Array.isArray(read) ? savedHosts(read) : undefined };
    }, { fp: null, hosts: undefined } as FingerprintRead, hosts);
  }

  /** Up to limit interactive elements of the page, as the model's inventory. Never input
   * values. [] on any error. */
  async inventory(limit = 200): Promise<InventoryRow[]> {
    let raw: unknown;
    try {
      raw = await this.lib('inventory', limit);
    } catch {
      return [];
    }
    if (!Array.isArray(raw)) return [];
    const norm = (s: unknown) => String(s ?? '').replace(/\s+/g, ' ').trim();
    return raw.slice(0, limit).filter((r) => r && typeof r === 'object').map((r: Record<string, unknown>) => ({
      tag: String(r.tag ?? '').toLowerCase(),
      role: roleFor(String(r.tag ?? '').toLowerCase(), r.role as string, r.type as string),
      id: pySlice(String(r.id ?? ''), 200),
      name: pySlice(String(r.name ?? ''), 200),
      text: pySlice(norm(r.text), 80),
      aria_label: pySlice(norm(r.aria_label), 80),
    }));
  }

  /** The elements of the page that have role (a hidden one only for checkbox), with their
   * raw reads, kept in the page for xpathFor() and outside(). */
  async snapshot(role: string): Promise<Snapshot> {
    const handle: JSHandle = await this.page.evaluateHandle(pageFunction('collect') as (arg: unknown) => unknown, role);
    const raws = (await handle.evaluate((h: any) => h.raws)) as RawElement[];
    return {
      raws,
      xpathFor: async (index) => (await handle.evaluate(handleFunction('xpathFor') as any, index)) as string | null,
      outside: async (index, ids) => (await handle.evaluate(handleFunction('outside') as any, { index, ids })) as string | null,
      dispose: async () => {
        try {
          await handle.dispose();
        } catch {
          // the page navigated or closed
        }
      },
    };
  }
}
