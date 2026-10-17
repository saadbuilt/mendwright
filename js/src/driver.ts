// The Playwright driver: the only code here that talks to the browser. It wraps a
// Playwright Page or Frame and answers the selector queries the resolver, the gate and the
// fingerprint tier make, each against an optional iframe selector (a candidate saved inside
// an iframe has one). A selector the browser cannot parse raises InvalidSelector at once.
//
// The page-side work runs mendwrightLib() (browser/lib.ts), sent as its source text.

import type { Frame, JSHandle, Locator, Page } from '@playwright/test';

import { mendwrightLib } from './browser/lib';
import { InvalidSelector } from './errors';
import { fromRaw, type Fingerprint, type RawElement } from './fingerprint';
import { pySlice } from './py';
import { roleFor } from './roles';
import { playwrightSelector } from './selectors';

export type PageLike = Page | Frame;

const LIB = '(' + mendwrightLib.toString() + ')()';
// The helpers of mendwrightLib() the driver calls by name. Only these names are ever put
// into a function's source below, never text from a page, a store or a caller.
const LIB_NAMES = new Set(['collect', 'describe', 'recordRaw', 'inventory', 'validate', 'cleanHtml',
  'candidates', 'indexOfMatch', 'robustXpaths']);
export type LibName = 'collect' | 'describe' | 'recordRaw' | 'inventory' | 'validate' | 'cleanHtml'
  | 'candidates' | 'indexOfMatch' | 'robustXpaths';

function checked(name: string): string {
  if (!LIB_NAMES.has(name)) throw new TypeError('mendwright: no page helper named ' + JSON.stringify(name));
  return name;
}

const pageFunctions = new Map<string, (arg: unknown) => unknown>();
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

export class PlaywrightDriver {
  readonly page: PageLike;

  constructor(page: PageLike) {
    this.page = page;
  }

  /** The Locator for a stored selector, inside the iframe frameXPath when it has one. */
  locator(selector: string, frameXPath?: string | null): Locator {
    const target = playwrightSelector(selector);
    if (frameXPath) return this.page.frameLocator(playwrightSelector(frameXPath)).locator(target);
    return this.page.locator(target);
  }

  private async probe<T>(selector: string, frameXPath: string | null | undefined,
                         op: (loc: Locator) => Promise<T>, fallback: T): Promise<T> {
    const loc = this.locator(selector, frameXPath);
    try {
      return await op(loc);
    } catch (error) {
      if (browserRejected(error)) throw invalid(error, selector);
      return fallback;
    }
  }

  count(selector: string, frameXPath?: string | null): Promise<number> {
    return this.probe(selector, frameXPath, (loc) => loc.count(), 0);
  }

  isVisible(selector: string, frameXPath?: string | null): Promise<boolean> {
    return this.probe(selector, frameXPath, (loc) => loc.isVisible(), false);
  }

  async scrollIntoView(selector: string, frameXPath?: string | null): Promise<void> {
    await this.probe(selector, frameXPath, (loc) => loc.scrollIntoViewIfNeeded({ timeout: 2000 }), undefined);
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

  /** Run one helper of mendwrightLib() in the page. */
  lib<T>(name: LibName, arg?: unknown): Promise<T> {
    return this.page.evaluate(pageFunction(name) as (arg: unknown) => T, arg);
  }

  /** describe() of the one element selector matches: its tag, gate role, text and outer
   * HTML without value attributes, or null when it matches none or several. */
  describe(selector: string, frameXPath?: string | null): Promise<Description | null> {
    return this.probe(selector, frameXPath,
      (loc) => loc.evaluateAll(elementsFunction('describe') as (els: Element[]) => Description | null), null);
  }

  /** The fingerprint of the one element selector matches, or null. One round trip, which
   * reads no input value. */
  fingerprint(selector: string, frameXPath?: string | null): Promise<Fingerprint | null> {
    return this.probe(selector, frameXPath,
      async (loc) => fromRaw(await loc.evaluateAll(elementsFunction('recordRaw') as (els: Element[]) => unknown)), null);
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
