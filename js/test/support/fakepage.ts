// A stand-in for a Playwright Page, backed by a jsdom document: what the unit tests drive
// mendwright with, so they need no browser. It answers the calls mendwright makes (locator(),
// count(), isVisible(), isHidden(), evaluate(), evaluateAll(), evaluateHandle(), content(),
// url()) and runs the browser-side code (browser/lib.ts, browser/popup.ts) inside the jsdom
// window, as a page would.
//
// Selectors: 'xpath=...' and 'css=...' (what the driver passes), and either followed by
// ' >> nth=<n>', the nth of its matches, as in Playwright. CSS pierces open shadow roots, as
// Playwright's does, and locator().locator() looks inside the elements before it; XPath does
// not reach into a shadow root. A page's HTML can declare shadow roots with
// <template shadowrootmode="open|closed">, which become attached shadow roots, so
// content() holds none of what they contain, as in a browser. Visibility: an element is
// hidden when it or an ancestor has the hidden attribute, an inline display:none or
// visibility:hidden, or data-fake-hidden. A page made with { blocker } has an element that
// covers the others: a click on any element outside it fails with Playwright's
// "intercepts pointer events" while it is on the page.
//
// A page made with { trustedTypes } enforces Trusted Types as a browser does under
// require-trusted-types-for 'script': DOMParser refuses a plain string. 'require' lets any
// policy be made, as when the CSP has no trusted-types directive; 'refuse' lets none be
// made, as when its trusted-types directive does not name mendwright. A handle belongs to
// the document it was made in, and goto() makes a new one.
//
// A page made with { layout: true } (or { layout: { width, height } }) has a layout, which
// jsdom has not, so the popup scripts of open({ dismiss: true }) run on it as in a browser:
//
// - data-rect="x y w h" gives an element its box in the page (an element without one has no
//   box, and is not rendered). The page scrolls (scrollY), except a fixed element and what is
//   in it. getBoundingClientRect() and getClientRects() answer from it, getClientRects() with
//   nothing for an element that display: none (its own or an ancestor's) hides.
// - document.elementFromPoint() and a shadow root's elementFromPoint() return the shown
//   element whose box holds the point, that takes pointer events (pointer-events is read from
//   the computed style, which inherits it), the highest data-z first (inherited, 0 without
//   one), then the last in document order, so the deepest; an element in a shadow root is
//   seen from outside as its host. innerWidth and innerHeight are the window's (1000 x 800
//   by default). scrollIntoView() scrolls the page so the element's middle is in the middle
//   of the window.
// - innerText is the text that is rendered: without what display: none hides, scripts and
//   styles, with a line break around a block.
// - A pointer action (click, dblclick, check, uncheck, hover, tap) on a Locator or an
//   ElementHandle scrolls its element into view when its middle is outside the window, then
//   fails with Playwright's "intercepts pointer events" when another element is on top at
//   its middle. fill() and press() focus their element and go through, as in Playwright.
// - page.keyboard.press(key) sends keydown and keyup to the focused element (document.body
//   when none). Escape removes each shown element with data-escape and hides each with
//   data-escape-hides.
// - A click on an element runs what its attributes say: data-click-removes="<css>" removes
//   what the CSS matches, data-click-hides hides it, data-click-reopens puts a fresh copy of
//   it back at once (a popup that comes back), data-click-uncovers lets pointer events
//   through it, data-click-removes-after="<ms>" with data-click-removes waits that long first,
//   as an animation does (data-click-removes-after-looks="<n>": until the page was looked at
//   n more times), and data-click-raises then throws the error Playwright throws for a
//   click whose navigation did not commit in time. page.clicked lists the elements clicked.
//
// Any page, with a layout or without: a click on an element runs data-click-shows="<css>",
// which takes the inline display: none off what the CSS matches, and data-click-goes="<url>",
// which loads the page page.site holds for that address (resolved against the current one),
// as a link does, data-click-goes-after="<ms>" that long after the click, as a slow site does.
// page.visits lists every address the page showed, in order.

import { JSDOM } from 'jsdom';

export interface FakePageOptions {
  url?: string;
  blocker?: string;
  trustedTypes?: 'require' | 'refuse';
  layout?: boolean | { width?: number; height?: number };
}

const POINTER = ['click', 'dblclick', 'check', 'uncheck', 'hover', 'tap'];
const BLOCKS = new Set(['address', 'article', 'aside', 'blockquote', 'dd', 'dialog', 'div', 'dl', 'dt', 'fieldset',
  'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav',
  'ol', 'p', 'pre', 'section', 'table', 'tr', 'ul']);

/** Make window enforce Trusted Types; the names of the policies made go to policies. */
function enforceTrustedTypes(window: any, mode: 'require' | 'refuse', policies: string[]): void {
  const trusted = new WeakSet<object>();
  const parse = window.DOMParser.prototype.parseFromString;
  window.DOMParser.prototype.parseFromString = function (input: unknown, type: string) {
    if (input && typeof input === 'object' && trusted.has(input)) return parse.call(this, String(input), type);
    throw new window.TypeError("Failed to execute 'parseFromString' on 'DOMParser': This document requires "
      + "'TrustedHTML' assignment.");
  };
  window.trustedTypes = {
    createPolicy(name: string, rules: { createHTML(text: string): string }) {
      if (mode === 'refuse') {
        throw new window.TypeError("Failed to execute 'createPolicy' on 'TrustedTypePolicyFactory': Policy \""
          + name + '\" disallowed.');
      }
      policies.push(name);
      return {
        createHTML(text: string) {
          const html = { text: rules.createHTML(text), toString() { return this.text; } };
          trusted.add(html);
          return html;
        },
      };
    },
  };
}

/** An element's parent, out of a shadow root to its host. */
function up(el: Element): Element | null {
  return el.parentElement || ((el.parentNode as ShadowRoot | null)?.host ?? null);
}

/** Give window the layout the page describes (see the header). */
function installLayout(page: FakePage, window: any, width: number, height: number): void {
  Object.defineProperty(window, 'innerWidth', { value: width, configurable: true });
  Object.defineProperty(window, 'innerHeight', { value: height, configurable: true });
  const style = (el: Element) => window.getComputedStyle(el);
  const box = (el: Element) => {
    const attr = el.getAttribute('data-rect');
    if (!attr) return null;
    const [x, y, w, h] = attr.trim().split(/\s+/).map(Number);
    return { x, y, w, h };
  };
  const fixed = (el: Element) => {
    for (let n: Element | null = el; n; n = up(n)) if (style(n).position === 'fixed') return true;
    return false;
  };
  const rendered = (el: Element) => {
    if (!el.isConnected || !box(el)) return false;
    for (let n: Element | null = el; n; n = up(n)) if (style(n).display === 'none') return false;
    return true;
  };
  const rect = (el: Element) => {
    const b = box(el);
    if (!b) return new window.DOMRect(0, 0, 0, 0);
    return new window.DOMRect(b.x, b.y - (fixed(el) ? 0 : page.scrollY), b.w, b.h);
  };
  window.Element.prototype.getBoundingClientRect = function () {
    return rendered(this) ? rect(this) : new window.DOMRect(0, 0, 0, 0);
  };
  window.Element.prototype.getClientRects = function () {
    return rendered(this) ? [rect(this)] : [];
  };
  window.Element.prototype.scrollIntoView = function () {
    const b = box(this);
    if (!b || fixed(this)) return;
    page.scrollY = Math.max(0, b.y + b.h / 2 - height / 2);
  };
  const zOf = (el: Element) => {
    for (let n: Element | null = el; n; n = up(n)) {
      const z = n.getAttribute('data-z');
      if (z !== null) return Number(z);
    }
    return 0;
  };
  // Every element of scope, in document order, a shadow root's in place of its host's children.
  const all = (scope: Document | ShadowRoot) => {
    const out: Element[] = [];
    const walk = (parent: ParentNode) => {
      for (const el of Array.from(parent.children)) {
        out.push(el);
        if (el.shadowRoot && el.shadowRoot.mode === 'open') walk(el.shadowRoot);
        walk(el);
      }
    };
    walk(scope);
    return out;
  };
  function fromPoint(this: Document | ShadowRoot, x: number, y: number) {
    let top: Element | null = null;
    let topZ = -Infinity;
    for (const el of all(this)) {
      if (!rendered(el)) continue;
      const s = style(el);
      if (s.visibility === 'hidden' || s.pointerEvents === 'none') continue;
      const r = rect(el);
      if (x < r.left || x >= r.right || y < r.top || y >= r.bottom) continue;
      const z = zOf(el);
      if (z >= topZ) {
        top = el;
        topZ = z;
      }
    }
    // Seen from this scope: an element in a shadow root inside it is its host there.
    while (top && top.getRootNode() !== this) top = (top.getRootNode() as ShadowRoot).host ?? null;
    return top;
  }
  window.Document.prototype.elementFromPoint = fromPoint;
  window.ShadowRoot.prototype.elementFromPoint = fromPoint;
  // innerText: the text that is rendered.
  const text = (node: Node): string => {
    if (node.nodeType === 3) return (node as Text).data;
    if (node.nodeType !== 1) return '';
    const el = node as Element;
    const tag = el.localName;
    if (['script', 'style', 'template', 'noscript'].includes(tag) || style(el).display === 'none') return '';
    if (tag === 'br') return '\n';
    const inner = Array.from(el.childNodes).map(text).join('');
    return BLOCKS.has(tag) ? '\n' + inner + '\n' : inner;
  };
  Object.defineProperty(window.HTMLElement.prototype, 'innerText', {
    configurable: true,
    get(this: HTMLElement) {
      return style(this).display === 'none' ? (this.textContent || '')
        : Array.from(this.childNodes).map(text).join('').replace(/\n+/g, '\n').trim();
    },
  });
}

export class FakeLocator {
  constructor(readonly page: FakePage, readonly selector: string, readonly frame: string | null = null,
              readonly parent: FakeLocator | null = null, readonly index: number | null = null) {}

  elements(): Element[] {
    let out: Element[];
    if (!this.parent) {
      out = this.page.query(this.selector);
    } else {
      out = [];
      for (const scope of this.parent.elements()) {
        for (const el of this.page.query(this.selector, scope)) if (!out.includes(el)) out.push(el);
      }
    }
    if (this.index === null) return out;
    const at = this.index < 0 ? out.length + this.index : this.index;
    return out[at] ? [out[at]] : [];
  }

  /** A locator for what selector matches inside the elements this one matches. */
  locator(selector: string): FakeLocator {
    return new FakeLocator(this.page, selector, this.frame, this);
  }

  getByTestId(id: string): FakeLocator {
    return this.locator('css=[data-testid="' + id + '"]');
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this.page, selector);
  }

  nth(index: number): FakeLocator {
    return new FakeLocator(this.page, this.selector, this.frame, this.parent, index);
  }

  first(): FakeLocator {
    return this.nth(0);
  }

  last(): FakeLocator {
    return this.nth(-1);
  }

  async all(): Promise<FakeLocator[]> {
    return this.elements().map((_, i) => this.nth(i));
  }

  async count(): Promise<number> {
    this.page.calls.push(['count', this.selector]);
    return this.elements().length;
  }

  private one(action: string): Element {
    const els = this.elements();
    if (els.length !== 1) {
      throw new Error('locator.' + action + ': Error: strict mode violation or no element: ' + this.selector
        + ' resolved to ' + els.length + ' elements');
    }
    return els[0];
  }

  async isVisible(): Promise<boolean> {
    const els = this.elements();
    return els.length === 1 && this.page.visible(els[0]);
  }

  async isHidden(): Promise<boolean> {
    return !(await this.isVisible());
  }

  async scrollIntoViewIfNeeded(): Promise<void> {}

  async evaluateAll(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    this.page.calls.push(['evaluateAll', this.selector]);
    this.page.looked();
    return this.page.run(fn, [this.elements(), unwrapHandles(arg)]);
  }

  /** Playwright's Locator.evaluateHandle(): fn on the one element the locator matches. */
  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    this.page.calls.push(['evaluateHandle', this.selector]);
    return new FakeHandle(this.page, this.page.run(fn, [this.one('evaluateHandle'), unwrapHandles(arg)]));
  }

  private act(action: string): Element {
    const el = this.one(action);
    this.page.actions.push([action, this.selector]);
    const failure = this.page.failNext.get(this.selector);
    if (failure) {
      this.page.failNext.delete(this.selector);
      throw new Error(failure);
    }
    const blocker = this.page.blocking();
    if (blocker && !blocker.contains(el)) {
      throw new Error('locator.' + action + ': Timeout 1000ms exceeded.\nCall log:\n  - waiting for ' + this.selector
        + '\n  - <div id="' + (blocker.id || 'blocker') + '"> intercepts pointer events\n  - retrying ' + action + ' action');
    }
    if (!this.page.visible(el) && action !== 'check' && action !== 'uncheck') {
      throw new Error('locator.' + action + ': Timeout 1000ms exceeded.\n  - element is not visible');
    }
    if (this.page.layout && POINTER.includes(action)) this.page.hitTest(el, 'locator.' + action);
    if (this.page.layout && !POINTER.includes(action)) (el as HTMLElement).focus?.();
    return el;
  }

  async click(): Promise<void> {
    this.page.clickOn(this.act('click'));
  }

  async dblclick(): Promise<void> {
    this.page.clickOn(this.act('dblclick'));
  }

  async fill(value: string): Promise<void> {
    if (typeof value !== 'string') throw new Error('locator.fill: value: expected string, got ' + typeof value);
    const el = this.act('fill') as HTMLInputElement;
    el.value = value;
    this.page.filled.set(el, value);
  }

  async check(): Promise<void> {
    const el = this.act('check') as HTMLInputElement;
    el.checked = true;
  }

  async uncheck(): Promise<void> {
    const el = this.act('uncheck') as HTMLInputElement;
    el.checked = false;
  }

  async press(): Promise<void> {
    this.act('press');
  }

  async hover(): Promise<void> {
    this.act('hover');
  }

  async setInputFiles(): Promise<void> {
    this.one('setInputFiles');
    this.page.actions.push(['setInputFiles', this.selector]);
  }

  async focus(): Promise<void> {
    const el = this.one('focus') as HTMLElement;
    this.page.actions.push(['focus', this.selector]);
    el.focus?.();
  }

  async textContent(): Promise<string | null> {
    return this.one('textContent').textContent;
  }

  async innerText(): Promise<string> {
    return (this.one('innerText').textContent || '').trim();
  }

  async getAttribute(name: string): Promise<string | null> {
    return this.one('getAttribute').getAttribute(name);
  }

  async inputValue(): Promise<string> {
    return (this.one('inputValue') as HTMLInputElement).value;
  }

  async isChecked(): Promise<boolean> {
    return Boolean((this.one('isChecked') as HTMLInputElement).checked);
  }
}

class FakeFrameLocator {
  constructor(readonly page: FakePage, readonly frame: string) {}

  locator(selector: string): FakeLocator {
    return new FakeLocator(this.page, selector, this.frame);
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this.page, selector);
  }

  nth(): FakeFrameLocator {
    return this;
  }
}

/** A JSHandle, or an ElementHandle when its value is an element. */
class FakeHandle {
  readonly document: number;
  disposed = false;

  constructor(private readonly page: FakePage, readonly value: unknown) {
    this.document = page.documents;
  }

  /** The value, while the page still shows the document it was made in. */
  live(): unknown {
    if (this.document !== this.page.documents) throw new Error('Execution context was destroyed, most likely because of a navigation');
    return this.value;
  }

  async evaluate(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    return this.page.run(fn, [this.live(), unwrapHandles(arg)]);
  }

  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    return new FakeHandle(this.page, this.page.run(fn, [this.live(), unwrapHandles(arg)]));
  }

  asElement(): FakeHandle | null {
    const value = this.value as Node | null;
    return value && typeof value === 'object' && value.nodeType === 1 ? this : null;
  }

  /** ElementHandle.click(): a pointer action on the element, as a Locator's is. */
  async click(options: { timeout?: number } = {}): Promise<void> {
    const el = this.live() as Element;
    this.page.handleClicks.push([el, options.timeout]);
    if (!el.isConnected || !this.page.visible(el)) {
      throw new Error('elementHandle.click: Timeout ' + (options.timeout ?? 0) + 'ms exceeded.\n  - element is not visible');
    }
    this.page.hitTest(el, 'elementHandle.click');
    this.page.clickOn(el);
  }

  async dispose(): Promise<void> {
    this.disposed = true;
  }
}

/** arg with each handle in it, or in an object or array at its top, replaced by its value,
 * as Playwright passes handles to the page. */
function unwrapHandles(arg: unknown): unknown {
  if (arg instanceof FakeHandle) return arg.live();
  if (Array.isArray(arg)) return arg.map((v) => (v instanceof FakeHandle ? v.live() : v));
  if (arg && typeof arg === 'object' && Object.getPrototypeOf(arg) === Object.prototype) {
    return Object.fromEntries(Object.entries(arg).map(([k, v]) => [k, v instanceof FakeHandle ? v.live() : v]));
  }
  return arg;
}

/** Every element under node, in document order, and inside each open shadow root on the way,
 * as Playwright's CSS sees them. */
function pierced(node: ParentNode): Element[] {
  const out: Element[] = [];
  const walk = (parent: ParentNode) => {
    for (const el of Array.from(parent.children)) {
      out.push(el);
      if (el.shadowRoot) walk(el.shadowRoot);
      walk(el);
    }
  };
  const own = (node as Element).shadowRoot;
  if (own) walk(own);
  walk(node);
  return out;
}

/** Attach the shadow roots a page declares with <template shadowrootmode>, at any depth. */
function declareShadowRoots(root: ParentNode): void {
  for (const template of Array.from(root.querySelectorAll('template[shadowrootmode]')) as HTMLTemplateElement[]) {
    const host = template.parentElement;
    if (!host) continue;
    const mode = template.getAttribute('shadowrootmode') === 'closed' ? 'closed' : 'open';
    const shadow = host.attachShadow({ mode });
    shadow.appendChild(template.content.cloneNode(true));
    template.remove();
    declareShadowRoots(shadow);
  }
}

/** How Playwright names an element in its call log: <div id="nl" class="nl">. */
function named(el: Element): string {
  let text = '<' + el.localName;
  for (const attr of ['id', 'class', 'role']) {
    const value = el.getAttribute(attr);
    if (value) text += ' ' + attr + '="' + value + '"';
  }
  return text + '>';
}

export class FakePage {
  dom: JSDOM;
  currentUrl: string;
  blocker: string | null;
  readonly calls: [string, string][] = [];
  readonly actions: [string, string][] = [];
  readonly clicked: Element[] = [];
  readonly filled = new Map<Element, string>();
  /** selector -> the error message the next action on it throws (a failed action). */
  readonly failNext = new Map<string, string>();
  readonly trustedTypes: 'require' | 'refuse' | null;
  /** The names of the Trusted Types policies the page's documents made. */
  readonly policies: string[] = [];
  /** How many documents the page has shown. */
  documents = 0;
  /** With a layout: the window's size, and how far the page is scrolled down. */
  readonly layout: { width: number; height: number } | null;
  scrollY = 0;
  /** The keys page.keyboard pressed, and the element each went to (its id, or its tag). */
  readonly keys: [string, string][] = [];
  /** ElementHandle clicks: the element, and the timeout passed. */
  readonly handleClicks: [Element, number | undefined][] = [];
  /** address -> HTML: the pages a click on an element with data-click-goes loads. */
  site: Record<string, string> = {};
  /** Every address the page showed, in order. */
  readonly visits: string[] = [];
  // What a click put off until the page was looked at n more times (data-click-removes-after-looks).
  private readonly pending: { left: number; run: () => void }[] = [];
  readonly keyboard = {
    press: async (key: string): Promise<void> => this.press(key),
  };

  constructor(html: string, options: FakePageOptions = {}) {
    this.currentUrl = options.url ?? 'https://shop.test/signup';
    this.blocker = options.blocker ?? null;
    this.trustedTypes = options.trustedTypes ?? null;
    const layout = options.layout;
    this.layout = layout ? { width: 1000, height: 800, ...(layout === true ? {} : layout) } : null;
    this.dom = this.load(html);
  }

  private load(html: string): JSDOM {
    const dom = new JSDOM(html, { url: this.currentUrl, runScripts: 'outside-only' });
    declareShadowRoots(dom.window.document);
    if (this.trustedTypes) enforceTrustedTypes(dom.window, this.trustedTypes, this.policies);
    if (this.layout) installLayout(this, dom.window, this.layout.width, this.layout.height);
    this.scrollY = 0;
    this.documents += 1;
    this.visits.push(this.currentUrl);
    return dom;
  }

  /** Load the page this.site holds for url, as a link does. */
  navigate(url: string): void {
    const html = this.site[url];
    if (html === undefined) throw new Error('page.goto: net::ERR_NAME_NOT_RESOLVED at ' + url);
    this.goto(html, url);
  }

  get document(): Document {
    return this.dom.window.document;
  }

  /** Load another page (a redesign, or the next step of a flow). */
  goto(html: string, url?: string): void {
    if (url) this.currentUrl = url;
    this.dom = this.load(html);
  }

  /** What selector matches in the document, or inside scope (an element, not itself). */
  query(selector: string, scope: Element | null = null): Element[] {
    const nth = /^([\s\S]*) >> nth=(-?\d+)$/.exec(selector);
    if (nth) {
      const all = this.query(nth[1], scope);
      const at = Number(nth[2]) < 0 ? all.length + Number(nth[2]) : Number(nth[2]);
      return all[at] ? [all[at]] : [];
    }
    const doc = this.document;
    if (selector.startsWith('xpath=')) {
      const expression = selector.slice('xpath='.length);
      let result: XPathResult;
      try {
        result = doc.evaluate(expression, scope ?? doc, null, 7, null);
      } catch (error) {
        throw new Error("locator.count: SyntaxError: Failed to execute 'evaluate' on 'Document': The string '"
          + expression + "' is not a valid XPath expression.");
      }
      const out: Element[] = [];
      for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        if (node && node.nodeType === 1) out.push(node as Element);
      }
      return out;
    }
    const css = selector.startsWith('css=') ? selector.slice(4) : selector;
    try {
      doc.querySelector(css);
    } catch {
      throw new Error('locator.count: Error: Unexpected token while parsing css selector "' + css + '"');
    }
    return pierced(scope ?? doc).filter((el) => el.matches(css));
  }

  visible(el: Element): boolean {
    if (el.localName === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
    for (let node: Element | null = el; node; node = node.parentElement) {
      if (node.hasAttribute('hidden') || node.hasAttribute('data-fake-hidden')) return false;
      const style = (node.getAttribute('style') || '').replace(/ /g, '').toLowerCase();
      if (style.includes('display:none') || style.includes('visibility:hidden')) return false;
    }
    return true;
  }

  blocking(): Element | null {
    if (!this.blocker) return null;
    const el = this.document.querySelector(this.blocker);
    return el && this.visible(el) ? el : null;
  }

  /** With a layout, before a pointer action on el: scroll it into view when its middle is
   * outside the window, then throw Playwright's error when another element is on top at its
   * middle. */
  hitTest(el: Element, call: string): void {
    const { width, height } = this.layout!;
    let r = el.getBoundingClientRect();
    let x = r.left + r.width / 2;
    let y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= width || y >= height) {
      el.scrollIntoView();
      r = el.getBoundingClientRect();
      x = r.left + r.width / 2;
      y = r.top + r.height / 2;
    }
    let hit = this.document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    const holds = (a: Element, b: Element) => {
      for (let n: Element | null = b; n; n = up(n)) if (n === a) return true;
      return false;
    };
    if (hit && !holds(el, hit) && !holds(hit, el)) {
      throw new Error(call + ': Timeout 1000ms exceeded.\nCall log:\n  - waiting for element to be visible, enabled and '
        + 'stable\n  - element is visible, enabled and stable\n  - scrolling into view if needed\n  - done scrolling\n  - '
        + named(hit) + ' intercepts pointer events\n  - retrying click action');
    }
  }

  /** What a click on el does, from its attributes (see the header). */
  clickOn(el: Element): void {
    this.clicked.push(el);
    const root = el.getRootNode() as Document | ShadowRoot;
    const scopes = root === this.document ? [root] : [this.document, root];
    const targets = (attr: string) => {
      const css = el.getAttribute(attr);
      if (!css) return [];
      return scopes.flatMap((scope) => Array.from(scope.querySelectorAll(css)));
    };
    const removes = () => {
      // On the page, and in the shadow root the element is in, as the element's own script would.
      for (const target of targets('data-click-removes')) target.remove();
    };
    const after = el.getAttribute('data-click-removes-after');
    const looks = el.getAttribute('data-click-removes-after-looks');
    if (after !== null) setTimeout(removes, Number(after)).unref();
    else if (looks !== null) this.pending.push({ left: Number(looks), run: removes });
    else removes();
    for (const target of targets('data-click-hides')) (target as HTMLElement).style.display = 'none';
    for (const target of targets('data-click-shows')) (target as HTMLElement).style.removeProperty('display');
    for (const target of targets('data-click-reopens')) target.replaceWith(target.cloneNode(true));
    for (const target of targets('data-click-uncovers')) (target as HTMLElement).style.pointerEvents = 'none';
    const goes = el.getAttribute('data-click-goes');
    if (goes !== null) {
      const url = new URL(goes, this.currentUrl).href;
      const later = el.getAttribute('data-click-goes-after');
      if (later !== null) setTimeout(() => this.navigate(url), Number(later)).unref();
      else this.navigate(url);
    }
    if (el.hasAttribute('data-click-raises')) {
      throw new Error('elementHandle.click: Timeout 1000ms exceeded.\nCall log:\n  - attempting click action\n  - '
        + 'performing click action\n  - click action done\n  - waiting for scheduled navigations to finish');
    }
  }

  /** One more look at the page (a Locator's evaluateAll): run what was put off until then. */
  looked(): void {
    for (const item of [...this.pending]) {
      item.left -= 1;
      if (item.left <= 0) {
        this.pending.splice(this.pending.indexOf(item), 1);
        item.run();
      }
    }
  }

  /** page.keyboard.press(key): keydown and keyup on the focused element. */
  private async press(key: string): Promise<void> {
    const doc = this.document;
    const window = this.dom.window;
    const target = (doc.activeElement as Element | null) ?? doc.body;
    this.keys.push([key, target.id || target.localName]);
    const down = new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
    const go = target.dispatchEvent(down);
    target.dispatchEvent(new window.KeyboardEvent('keyup', { key, bubbles: true }));
    if (key !== 'Escape' || !go) return;
    for (const el of pierced(doc)) {
      if (!el.isConnected || !el.getClientRects().length) continue;
      if (el.hasAttribute('data-escape')) el.remove();
      else if (el.hasAttribute('data-escape-hides')) (el as HTMLElement).style.display = 'none';
    }
  }

  /** Run a function the way a page runs one sent by Playwright: inside the window, from its
   * source text. */
  run(fn: (...args: unknown[]) => unknown, args: unknown[]): unknown {
    const inWindow = this.dom.window.eval('(' + fn.toString() + ')') as (...a: unknown[]) => unknown;
    return inWindow(...args);
  }

  locator(selector: string): FakeLocator {
    return new FakeLocator(this, selector);
  }

  getByTestId(id: string): FakeLocator {
    return this.locator('css=[data-testid="' + id + '"]');
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this, selector);
  }

  async content(): Promise<string> {
    return this.dom.serialize();
  }

  url(): string {
    return this.currentUrl;
  }

  mainFrame(): FakePage {
    return this;
  }

  // The page methods that take a selector, as Playwright's page has them.
  async isVisible(selector: string): Promise<boolean> {
    const els = this.query(selector.startsWith('/') ? 'xpath=' + selector : selector);
    return els.length > 0 && this.visible(els[0]);
  }

  async textContent(selector: string): Promise<string | null> {
    const els = this.query(selector.startsWith('/') ? 'xpath=' + selector : selector);
    this.calls.push(['textContent', selector]);
    return els.length ? els[0].textContent : null;
  }

  async evaluate(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    const result = this.run(fn, [unwrapHandles(arg)]);
    return result === undefined ? undefined : JSON.parse(JSON.stringify(result));
  }

  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    return new FakeHandle(this, this.run(fn, [unwrapHandles(arg)]));
  }
}
