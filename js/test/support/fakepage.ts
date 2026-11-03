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
// A page made with { rects: true } and no layout gives each element that visible() says is
// visible one box in getClientRects(), and the others none, so a script that counts boxes, such
// as the live gate of a list (ITEMS_JS), sees what a browser would.
//
// Any page, with a layout or without: a click on an element runs data-click-shows="<css>",
// which takes the inline display: none off what the CSS matches, and data-click-goes="<url>",
// which loads the page page.site holds for that address (resolved against the current one),
// as a link does, data-click-goes-after="<ms>" that long after the click, as a slow site does.
// page.visits lists every address the page showed, in order.
//
// Iframes: an <iframe> (or <frame>) has a document of its own, jsdom's, written from its
// srcdoc, or for a src from the page page.site holds at that address, the first time it is
// looked at; nested ones too. Each is a FakeFrame, a stand-in for a Playwright Frame:
// locator(), frameLocator(), content(), evaluate(), evaluateHandle(), name() (its name
// attribute, else its id, as Playwright's), url() ('about:srcdoc' for a srcdoc), parentFrame(),
// childFrames(), page() and frameElement(), and no mainFrame(), as a Frame has none. The page
// answers frames(), childFrames() and parentFrame() (null) as its main frame. A function runs
// in the window of the frame it is evaluated in. page.frameLocator(selector) looks in the
// document of the iframe selector matches, each time it is used: none matches nothing, and of
// several, count() and evaluateAll() take the first and the other calls throw Playwright's
// strict mode error, as Playwright's do. locator.elementHandle() gives a handle whose
// contentFrame() is the frame of an iframe. content() of the page holds none of what an
// iframe's document holds, and an element inside an iframe is visible only while its iframe
// is.

import { JSDOM } from 'jsdom';

export interface FakePageOptions {
  url?: string;
  blocker?: string;
  trustedTypes?: 'require' | 'refuse';
  layout?: boolean | { width?: number; height?: number };
  /** Without a layout: getClientRects() gives a box to each element visible() says is
   * visible, and none to the others, so a script that counts boxes, such as the live gate of a
   * list, sees what a browser would. */
  rects?: boolean;
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

/** Give each element of window that page.visible() says is visible a box (see { rects }). */
function installRects(page: FakePage, window: any): void {
  window.Element.prototype.getClientRects = function () {
    return this.isConnected && page.visible(this) ? [new window.DOMRect(0, 0, 100, 20)] : [];
  };
}

/** A document a locator looks in, and runs functions in: the page's, or a frame's. */
interface FakeContext {
  readonly document: Document;
  run(fn: (...args: unknown[]) => unknown, args: unknown[]): unknown;
  query(selector: string, scope?: Element | null): Element[];
}

/** Where a locator looks: strict is false for count() and evaluateAll(), which Playwright
 * runs in the first of several iframes a frameLocator() matches, where the other calls throw
 * its strict mode error. */
type Where = (strict: boolean) => FakeContext | null;

export class FakeLocator {
  /** where: the document it looks in, read each time (null: the page's). */
  constructor(readonly page: FakePage, readonly selector: string, readonly where: Where | null = null,
              readonly parent: FakeLocator | null = null, readonly index: number | null = null) {}

  /** The document this locator looks in now: the page's, or a frame's, or null when the
   * iframe of its frameLocator() is not there. */
  context(strict = true): FakeContext | null {
    return this.where ? this.where(strict) : this.page;
  }

  elements(strict = true): Element[] {
    const ctx = this.context(strict);
    if (!ctx) return [];
    let out: Element[];
    if (!this.parent) {
      out = ctx.query(this.selector);
    } else {
      out = [];
      for (const scope of this.parent.elements(strict)) {
        for (const el of ctx.query(this.selector, scope)) if (!out.includes(el)) out.push(el);
      }
    }
    if (this.index === null) return out;
    const at = this.index < 0 ? out.length + this.index : this.index;
    return out[at] ? [out[at]] : [];
  }

  /** A locator for what selector matches inside the elements this one matches. */
  locator(selector: string): FakeLocator {
    return new FakeLocator(this.page, selector, this.where, this);
  }

  getByRole(role: string, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByRole', role, options));
  }

  getByText(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByText', text, options));
  }

  getByLabel(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByLabel', text, options));
  }

  getByPlaceholder(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByPlaceholder', text, options));
  }

  getByAltText(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByAltText', text, options));
  }

  getByTitle(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByTitle', text, options));
  }

  getByTestId(id: unknown): FakeLocator {
    return this.locator(getBySelector('getByTestId', id));
  }

  /** Locator.and(): what both match. */
  and(other: FakeLocator): FakeLocator {
    return new FakeCombined(this.page, this.selector + ' & ' + other.selector, this.where,
      (strict) => this.elements(strict).filter((el) => other.elements(strict).includes(el)));
  }

  /** Locator.or(): what either matches, in document order. */
  or(other: FakeLocator): FakeLocator {
    return new FakeCombined(this.page, this.selector + ' | ' + other.selector, this.where, (strict) => {
      const both = [...this.elements(strict), ...other.elements(strict).filter((el) => !this.elements(strict).includes(el))];
      return both.sort((a, b) => (a.compareDocumentPosition(b) & 4 ? -1 : 1));
    });
  }

  /** Locator.filter({ hasText }). */
  filter(options: { hasText?: string } = {}): FakeLocator {
    return new FakeCombined(this.page, this.selector + '.filter()', this.where, (strict) => this.elements(strict)
      .filter((el) => options.hasText === undefined || (el.textContent ?? '').includes(options.hasText)));
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this.page, selector, (strict) => this.context(strict));
  }

  nth(index: number): FakeLocator {
    return new FakeLocator(this.page, this.selector, this.where, this.parent, index);
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
    return this.elements(false).length;
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
    const els = this.elements(false);
    return (this.context(false) ?? this.page).run(fn, [els, unwrapHandles(arg)]);
  }

  /** Playwright's Locator.evaluateHandle(): fn on the one element the locator matches. */
  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    this.page.calls.push(['evaluateHandle', this.selector]);
    const el = this.one('evaluateHandle');
    const ctx = this.context() ?? this.page;
    return new FakeHandle(this.page, ctx.run(fn, [el, unwrapHandles(arg)]), ctx);
  }

  /** Playwright's Locator.elementHandle(): a handle on the one element the locator matches;
   * a strict mode error for several, and a timeout for none, at once. */
  async elementHandle(options: { timeout?: number } = {}): Promise<FakeHandle> {
    this.page.calls.push(['elementHandle', this.selector]);
    const els = this.elements();
    if (els.length > 1) {
      throw new Error('locator.elementHandle: Error: strict mode violation: ' + this.selector + ' resolved to '
        + els.length + ' elements');
    }
    if (!els.length) throw new Error('locator.elementHandle: Timeout ' + (options.timeout ?? 30000) + 'ms exceeded.');
    return new FakeHandle(this.page, els[0], this.context() ?? this.page);
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

/** A locator from and(), or() or filter(): what find gives. */
class FakeCombined extends FakeLocator {
  constructor(page: FakePage, name: string, where: Where | null, private readonly find: (strict: boolean) => Element[]) {
    super(page, name, where);
  }

  override elements(strict = true): Element[] {
    return this.find(strict);
  }

  override nth(index: number): FakeLocator {
    return new FakeCombined(this.page, this.selector + ' >> nth=' + index, this.where, (strict) => {
      const all = this.find(strict);
      const at = index < 0 ? all.length + index : index;
      return all[at] ? [all[at]] : [];
    });
  }
}

/** Something with locator() that gets the getBy* methods: a page, a frame or a frame locator. */
abstract class GetBys {
  abstract locator(selector: string): FakeLocator;

  getByRole(role: string, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByRole', role, options));
  }

  getByText(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByText', text, options));
  }

  getByLabel(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByLabel', text, options));
  }

  getByPlaceholder(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByPlaceholder', text, options));
  }

  getByAltText(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByAltText', text, options));
  }

  getByTitle(text: unknown, options: Record<string, unknown> = {}): FakeLocator {
    return this.locator(getBySelector('getByTitle', text, options));
  }

  getByTestId(id: unknown): FakeLocator {
    return this.locator(getBySelector('getByTestId', id));
  }
}

class FakeFrameLocator extends GetBys {
  /** outer: the document the iframe is looked for in, read each time it is used. */
  constructor(readonly page: FakePage, readonly frame: string, private readonly outer: Where) {
    super();
  }

  /** The frame of the iframe this.frame matches in the document outside it, now: null when it
   * matches none, or an element with no document. When it matches several, the first one's,
   * not strict, and else Playwright's strict mode error. */
  resolve(strict: boolean): FakeFrame | null {
    const ctx = this.outer(strict);
    if (!ctx) return null;
    const els = ctx.query(this.frame);
    if (els.length > 1 && strict) {
      throw new Error('frameLocator: Error: strict mode violation: ' + this.frame + ' resolved to ' + els.length
        + ' elements');
    }
    return els.length ? this.page.frameOf(els[0]) : null;
  }

  override locator(selector: string): FakeLocator {
    return new FakeLocator(this.page, selector, (strict) => this.resolve(strict));
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this.page, selector, (strict) => this.resolve(strict));
  }

  nth(): FakeFrameLocator {
    return this;
  }
}

/** A JSHandle, or an ElementHandle when its value is an element. ctx is the document whose
 * window its functions run in. */
class FakeHandle {
  readonly document: number;
  disposed = false;

  constructor(private readonly page: FakePage, readonly value: unknown, private readonly ctx: FakeContext = page) {
    this.document = page.documents;
  }

  /** The value, while the page still shows the document it was made in. */
  live(): unknown {
    if (this.document !== this.page.documents) throw new Error('Execution context was destroyed, most likely because of a navigation');
    return this.value;
  }

  async evaluate(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    return this.ctx.run(fn, [this.live(), unwrapHandles(arg)]);
  }

  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    return new FakeHandle(this.page, this.ctx.run(fn, [this.live(), unwrapHandles(arg)]), this.ctx);
  }

  asElement(): FakeHandle | null {
    const value = this.value as Node | null;
    return value && typeof value === 'object' && value.nodeType === 1 ? this : null;
  }

  /** ElementHandle.contentFrame(): the frame of an iframe element, else null. */
  async contentFrame(): Promise<FakeFrame | null> {
    const value = this.live() as Node | null;
    return value && typeof value === 'object' && value.nodeType === 1 ? this.page.frameOf(value as Element) : null;
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

// --- the getBy* calls, as Playwright's engines match them (in short) ---------------------------
//
// A getBy* locator's selector is 'getby=' and the call as JSON ({ m, v, o }, a RegExp as
// { re, flags }). What it matches, as Playwright does in short:
//
// - getByRole: the elements whose ARIA role is the role (the role attribute, else the tag's:
//   button, a link with href, h1 to h6 a heading, an input by its type, and so on), that are in
//   the accessibility tree (no aria-hidden="true", hidden attribute or inline display: none or
//   visibility: hidden on it or an ancestor; data-fake-hidden hides it from the eye only, as a
//   styled checkbox's input is), with level (aria-level, else the heading's number) and name:
//   aria-labelledby, aria-label, a field's label, an img's alt, else the text of a role that
//   takes its name from it, else title. level on a role that has none throws Playwright's
//   error. includeHidden: true counts what is hidden from the tree too.
// - getByText: the innermost elements whose text has the text; getByLabel: the fields whose
//   label (label for, a label around it, aria-label, aria-labelledby) has it; getByPlaceholder,
//   getByAltText, getByTitle: the elements whose attribute has it; getByTestId: data-testid.
//
// Text matches as Playwright's does: in any case and with runs of white space as one, a whole
// match with exact: true, a RegExp by search. Open shadow roots are looked into.

type FakeCall = { m: string; v: unknown; o: Record<string, unknown> };

function encodeValue(value: unknown): unknown {
  return value instanceof RegExp ? { re: value.source, flags: value.flags } : value;
}

function decodeValue(value: unknown): unknown {
  const re = value as { re?: unknown; flags?: unknown } | null;
  return re && typeof re === 'object' && typeof re.re === 'string' ? new RegExp(re.re, String(re.flags ?? '')) : value;
}

/** The selector of a fake getBy* locator. */
function getBySelector(method: string, value: unknown, options: Record<string, unknown> = {}): string {
  const o: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(options ?? {})) if (v !== undefined) o[k] = encodeValue(v);
  return 'getby=' + JSON.stringify({ m: method, v: encodeValue(value), o });
}

const NAME_FROM_CONTENT = new Set(['button', 'link', 'heading', 'checkbox', 'radio', 'tab', 'menuitem', 'option',
  'cell', 'columnheader', 'rowheader', 'switch', 'treeitem', 'listitem', 'tooltip']);
const LEVEL_ROLES = ['heading', 'listitem', 'row', 'treeitem'];

const squash = (text: string | null | undefined) => (text ?? '').replace(/\s+/g, ' ').trim();

/** The element's ARIA role, as Playwright gives it in short, or null. */
export function fakeRole(el: Element): string | null {
  const explicit = squash(el.getAttribute('role')).split(' ')[0];
  if (explicit) return explicit;
  const tag = el.localName;
  if (/^h[1-6]$/.test(tag)) return 'heading';
  if (tag === 'input') {
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
    if (type === 'checkbox' || type === 'radio') return type;
    if (type === 'hidden') return null;
    if (type === 'range') return 'slider';
    if (type === 'number') return 'spinbutton';
    return 'textbox';
  }
  const roles: Record<string, string> = {
    button: 'button', textarea: 'textbox', nav: 'navigation', main: 'main', ul: 'list', ol: 'list', li: 'listitem',
    dialog: 'dialog', option: 'option', table: 'table', tr: 'row', td: 'cell', th: 'columnheader', p: 'paragraph',
    form: 'form', img: 'img', section: 'region', article: 'article', header: 'banner', footer: 'contentinfo',
  };
  if (tag === 'a' || tag === 'area') return el.hasAttribute('href') ? 'link' : null;
  if (tag === 'select') return el.hasAttribute('multiple') ? 'listbox' : 'combobox';
  return roles[tag] ?? null;
}

/** Whether el is out of the accessibility tree (see the comment above). */
function outOfTree(el: Element): boolean {
  for (let n: Element | null = el; n; n = up(n)) {
    if (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('hidden')) return true;
    const style = (n.getAttribute('style') || '').replace(/ /g, '').toLowerCase();
    if (style.includes('display:none') || style.includes('visibility:hidden')) return true;
  }
  return false;
}

function byIds(el: Element, ids: string): string {
  const root = el.getRootNode() as Document | ShadowRoot;
  return ids.split(/\s+/).filter(Boolean).map((id) => squash(root.getElementById?.(id)?.textContent
    ?? (root as ShadowRoot).querySelector?.('#' + id)?.textContent)).join(' ');
}

/** The text of the labels of a field: label for its id, and a label around it. */
function labelText(el: Element): string {
  const parts: string[] = [];
  const id = el.getAttribute('id');
  const root = el.getRootNode() as Document | ShadowRoot;
  if (id) for (const label of Array.from(root.querySelectorAll('label'))) if (label.getAttribute('for') === id) parts.push(squash(label.textContent));
  for (let n = el.parentElement; n; n = n.parentElement) if (n.localName === 'label') parts.push(squash(n.textContent));
  return parts.filter(Boolean).join(' ');
}

const FIELDS = ['input', 'textarea', 'select'];

/** The element's accessible name, in short. */
export function fakeName(el: Element): string {
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby && byIds(el, labelledby)) return byIds(el, labelledby);
  const label = squash(el.getAttribute('aria-label'));
  if (label) return label;
  if (FIELDS.includes(el.localName)) {
    const text = labelText(el);
    if (text) return text;
  }
  if (el.localName === 'img' && el.getAttribute('alt')) return squash(el.getAttribute('alt'));
  const role = fakeRole(el);
  if (role && NAME_FROM_CONTENT.has(role) && squash(el.textContent)) return squash(el.textContent);
  return squash(el.getAttribute('title') || el.getAttribute('placeholder'));
}

function textMatches(wanted: unknown, text: string, exact: boolean): boolean {
  if (wanted instanceof RegExp) {
    wanted.lastIndex = 0;
    return wanted.test(text);
  }
  const want = squash(String(wanted));
  return exact ? squash(text) === want : squash(text).toLowerCase().includes(want.toLowerCase());
}

function levelOf(el: Element): number | null {
  const aria = Number(el.getAttribute('aria-level'));
  if (Number.isInteger(aria) && aria > 0) return aria;
  const heading = /^h([1-6])$/.exec(el.localName);
  return heading ? Number(heading[1]) : null;
}

/** The elements of doc (inside scope) that the call matches (see the comment above). */
function getByElements(doc: Document, call: FakeCall, scope: Element | null): Element[] {
  const all = pierced(scope ?? doc).filter((el) => !['html', 'head', 'script', 'style', 'template'].includes(el.localName));
  const value = decodeValue(call.v);
  const o = Object.fromEntries(Object.entries(call.o).map(([k, v]) => [k, decodeValue(v)]));
  const exact = o.exact === true;
  switch (call.m) {
    case 'getByRole': {
      if (o.level !== undefined && !LEVEL_ROLES.includes(String(value))) {
        throw new Error('locator.count: Error: "level" attribute is only supported for roles: "heading", "listitem", '
          + '"row", "treeitem"');
      }
      return all.filter((el) => fakeRole(el) === value && (o.includeHidden === true || !outOfTree(el))
        && (o.level === undefined || levelOf(el) === o.level)
        && (o.name === undefined || textMatches(o.name, fakeName(el), exact))
        && (o.checked === undefined || Boolean((el as HTMLInputElement).checked) === o.checked)
        && (o.disabled === undefined || el.hasAttribute('disabled') === o.disabled));
    }
    case 'getByText': {
      const found = all.filter((el) => textMatches(value, el.textContent ?? '', exact));
      return found.filter((el) => !found.some((other) => other !== el && el.contains(other)));
    }
    case 'getByLabel':
      return all.filter((el) => (FIELDS.includes(el.localName) || el.hasAttribute('aria-label')
        || el.hasAttribute('aria-labelledby')) && textMatches(value, FIELDS.includes(el.localName) && labelText(el)
        ? labelText(el) : fakeName(el), exact));
    case 'getByTestId':
      return all.filter((el) => el.hasAttribute('data-testid') && textMatches(value, el.getAttribute('data-testid') ?? '',
        true));
    default: {
      const attr = ({ getByPlaceholder: 'placeholder', getByAltText: 'alt', getByTitle: 'title' } as Record<string, string>)[call.m];
      return all.filter((el) => el.hasAttribute(attr) && textMatches(value, el.getAttribute(attr) ?? '', exact));
    }
  }
}

/** What selector matches in doc, or inside scope (an element of doc, not itself): 'xpath=...',
 * 'css=...', 'text=...' or 'getby=...' (a getBy* call, see above), or one of them followed by
 * ' >> nth=<n>'. text=Words matches, as Playwright's text engine does, the innermost elements
 * whose text has the words, in any case and with runs of white space as one; text="Words" those
 * whose whole text is them. */
function queryIn(doc: Document, selector: string, scope: Element | null = null): Element[] {
  const nth = /^([\s\S]*) >> nth=(-?\d+)$/.exec(selector);
  if (nth) {
    const all = queryIn(doc, nth[1], scope);
    const at = Number(nth[2]) < 0 ? all.length + Number(nth[2]) : Number(nth[2]);
    return all[at] ? [all[at]] : [];
  }
  if (selector.startsWith('getby=')) return getByElements(doc, JSON.parse(selector.slice('getby='.length)), scope);
  if (selector.startsWith('text=')) {
    const body = selector.slice('text='.length);
    const exact = /^(["']).*\1$/s.test(body);
    const words = (exact ? body.slice(1, -1) : body).replace(/\s+/g, ' ').trim();
    const has = (el: Element) => {
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      return exact ? text === words : text.toLowerCase().includes(words.toLowerCase());
    };
    const all = pierced(scope ?? doc).filter((el) => !['html', 'head', 'script', 'style'].includes(el.localName) && has(el));
    return all.filter((el) => !all.some((other) => other !== el && el.contains(other)));
  }
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

const FRAME_TAGS = ['iframe', 'frame'];

/** A stand-in for a Playwright Frame: the document of one iframe of the page (see the header). */
export class FakeFrame extends GetBys implements FakeContext {
  constructor(readonly owner: FakePage, readonly element: Element, readonly parent: FakePage | FakeFrame) {
    super();
  }

  get document(): Document {
    return (this.element as HTMLIFrameElement).contentDocument as Document;
  }

  /** Playwright's frame.name(): the iframe's name attribute, else its id. */
  name(): string {
    return this.element.getAttribute('name') || this.element.getAttribute('id') || '';
  }

  url(): string {
    if (this.element.hasAttribute('srcdoc')) return 'about:srcdoc';
    const src = this.element.getAttribute('src');
    if (!src) return 'about:blank';
    try {
      return new URL(src, this.owner.currentUrl).href;
    } catch {
      return src;
    }
  }

  parentFrame(): FakePage | FakeFrame {
    return this.parent;
  }

  childFrames(): FakeFrame[] {
    return this.owner.framesIn(this.document);
  }

  page(): FakePage {
    return this.owner;
  }

  isDetached(): boolean {
    return !this.element.isConnected;
  }

  /** A handle on the iframe element, in the document around it. */
  async frameElement(): Promise<FakeHandle> {
    return new FakeHandle(this.owner, this.element, this.parent);
  }

  query(selector: string, scope: Element | null = null): Element[] {
    return queryIn(this.document, selector, scope);
  }

  /** As FakePage.run(): mendwright's own page functions, from their source text, in the
   * iframe's window, as Playwright runs them in a frame. */
  run(fn: (...args: unknown[]) => unknown, args: unknown[]): unknown {
    const window = (this.element as HTMLIFrameElement).contentWindow as unknown as { eval(code: string): unknown };
    const inWindow = window.eval('(' + fn.toString() + ')') as (...a: unknown[]) => unknown;
    return inWindow(...args);
  }

  /** This frame while its iframe is on the page, else null (it was detached). */
  private here(): FakeFrame | null {
    return this.element.isConnected ? this : null;
  }

  override locator(selector: string): FakeLocator {
    return new FakeLocator(this.owner, selector, () => this.here());
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this.owner, selector, () => this.here());
  }

  async content(): Promise<string> {
    const doc = this.document;
    return (doc.doctype ? '<!DOCTYPE ' + doc.doctype.name + '>' : '') + (doc.documentElement?.outerHTML ?? '');
  }

  async evaluate(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    const result = this.run(fn, [unwrapHandles(arg)]);
    return result === undefined ? undefined : JSON.parse(JSON.stringify(result));
  }

  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    return new FakeHandle(this.owner, this.run(fn, [unwrapHandles(arg)]), this);
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

export class FakePage extends GetBys implements FakeContext {
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
  readonly rects: boolean;
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
  // An iframe element -> its frame, and the iframes whose documents were written.
  private readonly frameCache = new WeakMap<Element, FakeFrame>();
  private readonly written = new WeakSet<Element>();
  readonly keyboard = {
    press: async (key: string): Promise<void> => this.press(key),
  };

  constructor(html: string, options: FakePageOptions = {}) {
    super();
    this.currentUrl = options.url ?? 'https://shop.test/signup';
    this.blocker = options.blocker ?? null;
    this.trustedTypes = options.trustedTypes ?? null;
    const layout = options.layout;
    this.layout = layout ? { width: 1000, height: 800, ...(layout === true ? {} : layout) } : null;
    this.rects = Boolean(options.rects);
    this.dom = this.load(html);
  }

  private load(html: string): JSDOM {
    const dom = new JSDOM(html, { url: this.currentUrl, runScripts: 'outside-only' });
    declareShadowRoots(dom.window.document);
    if (this.trustedTypes) enforceTrustedTypes(dom.window, this.trustedTypes, this.policies);
    if (this.layout) installLayout(this, dom.window, this.layout.width, this.layout.height);
    else if (this.rects) installRects(this, dom.window);
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
    return queryIn(this.document, selector, scope);
  }

  visible(el: Element): boolean {
    if (el.localName === 'input' && (el.getAttribute('type') || '').toLowerCase() === 'hidden') return false;
    for (let node: Element | null = el; node; node = node.parentElement) {
      if (node.hasAttribute('hidden') || node.hasAttribute('data-fake-hidden')) return false;
      const style = (node.getAttribute('style') || '').replace(/ /g, '').toLowerCase();
      if (style.includes('display:none') || style.includes('visibility:hidden')) return false;
    }
    // Inside an iframe, only while the iframe is.
    const iframe = el.ownerDocument?.defaultView?.frameElement ?? null;
    return iframe ? this.visible(iframe) : true;
  }

  // --- frames (see the header) -------------------------------------------------------------

  /** The frame of an <iframe> or <frame> element of the page or of one of its frames, its
   * document written the first time; null for any other element, or one no longer in its
   * document. The same element always gives the same frame. */
  frameOf(el: Element): FakeFrame | null {
    if (!FRAME_TAGS.includes(el.localName) || !el.isConnected) return null;
    const known = this.frameCache.get(el);
    if (known) return known;
    if (!(el as HTMLIFrameElement).contentDocument) return null;
    let parent: FakePage | FakeFrame | null = null;
    if (el.ownerDocument === this.document) {
      parent = this;
    } else {
      const outer = el.ownerDocument.defaultView?.frameElement ?? null;
      parent = outer ? this.frameOf(outer) : null;
    }
    if (!parent) return null;
    this.write(el);
    const frame = new FakeFrame(this, el, parent);
    this.frameCache.set(el, frame);
    return frame;
  }

  /** The frames of the iframes in doc, in document order, inside its open shadow roots too. */
  framesIn(doc: Document): FakeFrame[] {
    return pierced(doc).map((el) => this.frameOf(el)).filter((frame): frame is FakeFrame => frame !== null);
  }

  /** Playwright's page.frames(): the page as its main frame, then every frame, at any depth. */
  frames(): (FakePage | FakeFrame)[] {
    const out: (FakePage | FakeFrame)[] = [this];
    const walk = (frames: FakeFrame[]) => {
      for (const frame of frames) {
        out.push(frame);
        walk(frame.childFrames());
      }
    };
    walk(this.childFrames());
    return out;
  }

  /** As the main frame: the frames of the page's own iframes. */
  childFrames(): FakeFrame[] {
    return this.framesIn(this.document);
  }

  /** As the main frame, which has none. */
  parentFrame(): null {
    return null;
  }

  name(): string {
    return '';
  }

  /** Write the document of iframe el from its srcdoc, or for a src from the page this.site
   * holds at that address, once. */
  private write(el: Element): void {
    if (this.written.has(el)) return;
    this.written.add(el);
    const doc = (el as HTMLIFrameElement).contentDocument;
    if (!doc) return;
    let html = el.getAttribute('srcdoc');
    if (html === null) {
      const src = el.getAttribute('src');
      try {
        html = src ? this.site[new URL(src, this.currentUrl).href] ?? null : null;
      } catch {
        html = null;
      }
    }
    if (html === null) return;
    doc.open();
    doc.write(html);
    doc.close();
    declareShadowRoots(doc);
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

  override locator(selector: string): FakeLocator {
    return new FakeLocator(this, selector);
  }

  frameLocator(selector: string): FakeFrameLocator {
    return new FakeFrameLocator(this, selector, () => this);
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
