// A stand-in for a Playwright Page, backed by a jsdom document: what the unit tests drive
// mendwright with, so they need no browser. It answers the calls mendwright makes (locator(),
// count(), isVisible(), evaluate(), evaluateAll(), evaluateHandle(), content(), url()) and runs
// the browser-side code (browser/lib.ts) inside the jsdom window, as a page would.
//
// Selectors: 'xpath=...' and 'css=...' (what the driver passes). CSS pierces open shadow
// roots, as Playwright's does, and locator().locator() looks inside the elements before it;
// XPath does not reach into a shadow root. A page's HTML can declare shadow roots with
// <template shadowrootmode="open|closed">, which become attached shadow roots, so
// content() holds none of what they contain, as in a browser. Visibility: an element is
// hidden when it or an ancestor has the hidden attribute, an inline display:none or
// visibility:hidden, or data-fake-hidden. A page made with { blocker } has an element that
// covers the others: a click on any element outside it fails with Playwright's
// "intercepts pointer events" while it is on the page.

import { JSDOM } from 'jsdom';

export interface FakePageOptions {
  url?: string;
  blocker?: string;
}

export class FakeLocator {
  constructor(readonly page: FakePage, readonly selector: string, readonly frame: string | null = null,
              readonly parent: FakeLocator | null = null) {}

  elements(): Element[] {
    if (!this.parent) return this.page.query(this.selector);
    const out: Element[] = [];
    for (const scope of this.parent.elements()) {
      for (const el of this.page.query(this.selector, scope)) if (!out.includes(el)) out.push(el);
    }
    return out;
  }

  /** A locator for what selector matches inside the elements this one matches. */
  locator(selector: string): FakeLocator {
    return new FakeLocator(this.page, selector, this.frame, this);
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

  async scrollIntoViewIfNeeded(): Promise<void> {}

  async evaluateAll(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    return this.page.run(fn, [this.elements(), arg]);
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
    return el;
  }

  async click(): Promise<void> {
    const el = this.act('click');
    const remove = el.getAttribute('data-click-removes');
    if (remove) for (const target of Array.from(this.page.document.querySelectorAll(remove))) target.remove();
    this.page.clicked.push(el);
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
}

class FakeHandle {
  constructor(private readonly page: FakePage, private readonly value: unknown) {}

  async evaluate(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<unknown> {
    return this.page.run(fn, [this.value, arg]);
  }

  async dispose(): Promise<void> {}
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

  constructor(html: string, options: FakePageOptions = {}) {
    this.currentUrl = options.url ?? 'https://shop.test/signup';
    this.blocker = options.blocker ?? null;
    this.dom = new JSDOM(html, { url: this.currentUrl, runScripts: 'outside-only' });
    declareShadowRoots(this.dom.window.document);
  }

  get document(): Document {
    return this.dom.window.document;
  }

  /** Load another page (a redesign, or the next step of a flow). */
  goto(html: string, url?: string): void {
    if (url) this.currentUrl = url;
    this.dom = new JSDOM(html, { url: this.currentUrl, runScripts: 'outside-only' });
    declareShadowRoots(this.dom.window.document);
  }

  /** What selector matches in the document, or inside scope (an element, not itself). */
  query(selector: string, scope: Element | null = null): Element[] {
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

  /** Run a function the way a page runs one sent by Playwright: inside the window, from its
   * source text. */
  run(fn: (...args: unknown[]) => unknown, args: unknown[]): unknown {
    const inWindow = this.dom.window.eval('(' + fn.toString() + ')') as (...a: unknown[]) => unknown;
    return inWindow(...args);
  }

  locator(selector: string): FakeLocator {
    return new FakeLocator(this, selector);
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
    const result = this.run(fn, [arg]);
    return result === undefined ? undefined : JSON.parse(JSON.stringify(result));
  }

  async evaluateHandle(fn: (...args: unknown[]) => unknown, arg?: unknown): Promise<FakeHandle> {
    return new FakeHandle(this, this.run(fn, [arg]));
  }
}
