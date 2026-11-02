// The code mendwright runs inside the page. mendwrightLib() is sent to the browser as its
// source text (see dom.ts), so it must stay self-contained: no imports, no references to
// anything outside the function, and no syntax the TypeScript compiler would rewrite with
// a helper. It returns the helpers by name.
//
// It ports the parts of the Python package that read a document with lxml:
// validation._role_of, fingerprint.raw_of_element / FINGERPRINT_JS / xpath_for /
// statically_hidden, gate's static check (validation.validate), capture.clean_html, the
// driver's describe() and inventory(), the candidate list and XPaths of the local selector
// model (candidates.py), the CSS forms of a healed element (css.css_selectors), the
// scripts that read open shadow roots (drivers.base ENTER_JS, PLACE_JS, SHADOW_HTML_JS,
// SHADOW_INVENTORY_JS and SHADOW_ROOTS_JS), and the reads of a list (lists.LIST_JS,
// LIST_PLACE_JS and ITEMS_JS). Text rules follow the Python ones: lengths and cuts count
// code points, and str.strip() strips what Python's strips.
//
// policy, when given, is mendwright's own Trusted Types policy in this document (see the
// driver): the helpers that parse an HTML string pass the string through it, because a page
// whose Content-Security-Policy has require-trusted-types-for 'script' refuses a plain
// string there. Without it, such a page makes them throw 'mendwright:trusted-types'.

/* eslint-disable */
export function mendwrightLib(policy?: { createHTML(text: string): unknown } | null) {
  // --- Python text rules -----------------------------------------------------------------
  const PY_SPACE = '\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000';
  const PY_STRIP = new RegExp('^[' + PY_SPACE + ']+|[' + PY_SPACE + ']+$', 'g');
  const pyStrip = (s: string) => s.replace(PY_STRIP, '');
  const pyLen = (s: string) => Array.from(s).length;
  const pySlice = (s: string, n: number) => (s.length <= n ? s : Array.from(s).slice(0, n).join(''));
  const lower = (v: unknown) => (typeof v === 'string' ? v : '').toLowerCase();

  // fingerprint._norm: what XPath's normalize-space() collapses, then str.strip().
  const fpNorm = (text: unknown) => (typeof text === 'string' ? pyStrip(text.replace(/[ \t\n\r\f]+/g, ' ')) : '');
  // validation._norm: any whitespace.
  const wsNorm = (text: unknown) => (typeof text === 'string' ? text : '').replace(/\s+/g, ' ').trim();

  const tagOf = (el: Element) => (el.localName || el.tagName || '').toLowerCase();
  const isElement = (n: any): n is Element => !!n && n.nodeType === 1;

  // --- roles (validation._role_of) ------------------------------------------------------
  const GATE_ROLE_ATTRS = ['checkbox', 'button', 'link', 'combobox', 'radio'];
  const ARIA_ROLE_ATTRS: Record<string, string> = {
    textbox: 'input', searchbox: 'input', spinbutton: 'input', slider: 'input',
    switch: 'checkbox', listbox: 'combobox',
  };
  const roleOf = (el: Element) => {
    const role = pyStrip(el.getAttribute('role') || '').toLowerCase();
    if (GATE_ROLE_ATTRS.indexOf(role) >= 0) return role;
    const tag = tagOf(el);
    const type = pyStrip(el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input') {
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (type === 'submit' || type === 'button') return 'button';
      return 'input';
    }
    if (tag === 'button') return 'button';
    if (tag === 'a') return 'link';
    if (tag === 'select') return 'combobox';
    if (tag !== 'textarea' && Object.prototype.hasOwnProperty.call(ARIA_ROLE_ATTRS, role)) return ARIA_ROLE_ATTRS[role];
    return tag || 'other';
  };

  // --- walking the tree, as lxml does ----------------------------------------------------
  // root.iter(): the element itself, then its descendants, in document order.
  const iterAll = (root: Element): Element[] => [root].concat(Array.from(root.getElementsByTagName('*')));
  // el.iter(*tags): the same, keeping the elements of those tags.
  const iterTags = (root: Element, tags: string[]): Element[] => iterAll(root).filter((e) => tags.indexOf(tagOf(e)) >= 0);
  const ancestors = (el: Element): Element[] => {
    const out: Element[] = [];
    for (let p = el.parentElement; p; p = p.parentElement) out.push(p);
    return out;
  };
  const allElements = (doc: Document): Element[] => Array.from(doc.getElementsByTagName('*'));

  // --- XPath ----------------------------------------------------------------------------
  const xpathLiteral = (value: string) => {
    if (value.indexOf("'") < 0) return "'" + value + "'";
    if (value.indexOf('"') < 0) return '"' + value + '"';
    return 'concat(' + value.split("'").map((part) => "'" + part + "'").join(', "\'", ') + ')';
  };
  // The elements an XPath matches in doc, in document order, or null when it cannot run.
  const xpathElements = (doc: Document, xpath: string, context?: Node): Element[] | null => {
    try {
      const result = doc.evaluate(xpath, context || doc, null, 7 /* ORDERED_NODE_SNAPSHOT_TYPE */, null);
      const out: Element[] = [];
      for (let i = 0; i < result.snapshotLength; i++) {
        const node = result.snapshotItem(i);
        if (isElement(node)) out.push(node);
      }
      return out;
    } catch (e) {
      return null;
    }
  };
  const xpathError = (doc: Document, xpath: string) => {
    try {
      doc.evaluate(xpath, doc, null, 7, null);
      return '';
    } catch (e: any) {
      return (e && e.name) || 'Error';
    }
  };
  const parse = (html: string) => {
    const text = typeof html === 'string' && html.trim() ? html : '<html><body></body></html>';
    const parser = new DOMParser();
    if (policy) return parser.parseFromString(policy.createHTML(text) as string, 'text/html');
    try {
      return parser.parseFromString(text, 'text/html');
    } catch (e: any) {
      if (e && e.name === 'TypeError' && typeof (window as any).trustedTypes === 'object') {
        throw new Error('mendwright:trusted-types');
      }
      throw e;
    }
  };

  // --- fingerprints (fingerprint.py) ------------------------------------------------------
  const FIELD_ROLES = ['textbox', 'searchbox', 'combobox', 'spinbutton', 'slider'];
  const ATTRS = ['id', 'name', 'type', 'role', 'class', 'data-testid', 'data-test', 'data-qa',
    'aria-label', 'placeholder', 'title', 'alt', 'href', 'autocomplete'];
  const read = (e: Element, names: string[]) => {
    const out: Record<string, string> = {};
    for (const n of names) {
      const v = e.getAttribute(n);
      if (v !== null) out[n] = v;
    }
    return out;
  };
  // fingerprint._is_field, as lxml reads the captured page.
  const isField = (el: Element) => {
    const tag = tagOf(el);
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
    if (FIELD_ROLES.indexOf(pyStrip(el.getAttribute('role') || '').toLowerCase()) >= 0) return true;
    for (let node: Element | null = el; node; node = node.parentElement) {
      const editable = node.getAttribute('contenteditable');
      if (editable !== null) return ['', 'true', 'plaintext-only'].indexOf(pyStrip(editable).toLowerCase()) >= 0;
    }
    return false;
  };
  const labelIndex = (doc: Document) => {
    const out: Record<string, string> = {};
    for (const label of Array.from(doc.getElementsByTagName('label'))) {
      const target = label.getAttribute('for');
      if (target !== null && !Object.prototype.hasOwnProperty.call(out, target)) out[target] = label.textContent || '';
    }
    return out;
  };
  const closestLabel = (el: Element) => {
    for (const anc of ancestors(el)) if (tagOf(anc) === 'label') return anc;
    return null;
  };
  // fingerprint.raw_of_element.
  const rawOf = (el: Element, labels?: Record<string, string>) => {
    const tag = tagOf(el);
    const field = isField(el);
    let label: string | null = null;
    if (field) {
      const wrap = closestLabel(el);
      if (wrap) {
        label = wrap.textContent || '';
      } else if (el.getAttribute('id')) {
        const index = labels || labelIndex(el.ownerDocument);
        const id = el.getAttribute('id') as string;
        label = Object.prototype.hasOwnProperty.call(index, id) ? index[id] : null;
      }
    }
    const chain: { tag: string; attrs: Record<string, string> }[] = [];
    for (const anc of ancestors(el)) {
      if (chain.length >= 4) break;
      const name = tagOf(anc);
      if (name === 'body' || name === 'html') break;
      chain.push({ tag: name, attrs: read(anc, ['id', 'class', 'role']) });
    }
    return { tag: tag, attrs: read(el, ATTRS), field: field, text: field ? null : el.textContent || '', label: label, ancestors: chain };
  };
  // Open shadow roots (Python's mendwright/shadow.py and fingerprint.HOST_FACTS_JS). hostsOf(el)
  // gives a CSS selector for each shadow host around el, outermost first, that matches the
  // host and nothing else where it sits: its tag with a test id, an id or a name that no other
  // element of its tag there has, best first, else a custom element's tag alone when it is the
  // only one of its tag there, else null (shadow.host_selector). [] for an element on the page.
  const HOST_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy', 'data-e2e',
    'data-automation-id', 'id', 'name'];
  const HOST_TAG = /^[a-z][a-z0-9_-]*$/;
  const isRoot = (r: any) => !!r && r.nodeType === 11 && !!r.host && r.host.nodeType === 1;
  // css.css_string: a double-quoted CSS string.
  const cssString = (value: string) => {
    let out = '"';
    for (const ch of Array.from(value)) {
      if (ch === '"' || ch === '\\') out += '\\' + ch;
      else if (ch === '\n' || ch === '\r' || ch === '\f') out += '\\' + ch.charCodeAt(0).toString(16) + ' ';
      else out += ch;
    }
    return out + '"';
  };
  const hostSelector = (host: Element): string | null => {
    const scope = host.getRootNode() as any;
    const tag = host.localName;
    if (typeof tag !== 'string' || !HOST_TAG.test(tag)) return null;
    const same = Array.from(scope.querySelectorAll('*') as ArrayLike<Element>).filter((e) => e.localName === tag);
    for (const name of HOST_ATTRS) {
      const value = host.getAttribute(name);
      if (value === null || !pyStrip(value) || pyLen(value) > 200 || value.indexOf('\n') >= 0 || generated(value)) continue;
      if (same.filter((e) => e.getAttribute(name) === value).length === 1) return tag + '[' + name + '=' + cssString(value) + ']';
    }
    if (tag.indexOf('-') >= 0 && same.length === 1) return tag;
    return null;
  };
  const hostsOf = (el: Element) => {
    const out: (string | null)[] = [];
    for (let r: any = el.getRootNode(); isRoot(r); r = r.host.getRootNode()) out.unshift(hostSelector(r.host));
    return out;
  };
  // drivers.base.ENTER_JS: the shadow root the host selectors lead to from doc, each one
  // matching exactly one element in the document or shadow root before it, with an open
  // shadow root; null when they lead to none.
  const enter = (doc: any, hosts: string[]) => {
    let scope = doc;
    for (const sel of hosts) {
      let found: ArrayLike<Element>;
      try {
        found = scope.querySelectorAll(sel);
      } catch (e) {
        return null;
      }
      if (found.length !== 1 || !(found[0] as Element).shadowRoot) return null;
      scope = (found[0] as Element).shadowRoot;
    }
    return scope;
  };
  // drivers.base.PLACE_JS, for a Locator whose CSS pierces every open shadow root: how many
  // elements it matches (n), and how many of them are in the shadow root the hosts lead to
  // (inside). A selector inside a shadow root counts only when they agree.
  const placed = (els: Element[], hosts: string[]) => {
    if (!els || !els.length) return { n: 0, inside: 0 };
    const root = enter(els[0].ownerDocument, hosts);
    return { n: els.length, inside: root ? els.filter((e) => e.getRootNode() === root).length : 0 };
  };

  // FINGERPRINT_JS, the read the Python drivers make in the browser when a selector
  // matched: null unless exactly one element matched. hosts is where the element is (see
  // hostsOf), which fromRaw() leaves out of the fingerprint.
  const recordRaw = (els: Element[]) => {
    if (!els || els.length !== 1) return null;
    const el = els[0] as HTMLElement;
    const tag = el.tagName.toLowerCase();
    const role = (el.getAttribute('role') || '').toLowerCase();
    const field = tag === 'input' || tag === 'textarea' || tag === 'select'
      || el.isContentEditable || /^(textbox|searchbox|combobox|spinbutton|slider)$/.test(role);
    let label: string | null = null;
    if (field) {
      const wrap = el.parentElement ? el.parentElement.closest('label') : null;
      if (wrap) {
        label = wrap.textContent;
      } else {
        const id = el.getAttribute('id');
        if (id) {
          // In the element's own shadow root when it is inside one, as the Python package reads it.
          const root = el.getRootNode() as any;
          const labels = isRoot(root) ? Array.from(root.querySelectorAll('label') as ArrayLike<Element>)
            : Array.from(el.ownerDocument.getElementsByTagName('label'));
          for (const l of labels) {
            if (l.getAttribute('for') === id) { label = l.textContent; break; }
          }
        }
      }
    }
    const chain: { tag: string; attrs: Record<string, string> }[] = [];
    let p = el.parentElement;
    while (p && chain.length < 4) {
      const t = p.tagName.toLowerCase();
      if (t === 'body' || t === 'html') break;
      chain.push({ tag: t, attrs: read(p, ['id', 'class', 'role']) });
      p = p.parentElement;
    }
    return { tag: tag, attrs: read(el, ATTRS), field: field, text: field ? null : el.textContent, label: label, ancestors: chain,
             hosts: hostsOf(el) };
  };

  // fingerprint.statically_hidden: the captured HTML itself hides el.
  const staticallyHidden = (el: Element) => {
    if (tagOf(el) === 'input' && pyStrip(el.getAttribute('type') || '').toLowerCase() === 'hidden') return true;
    for (let node: Element | null = el; node; node = node.parentElement) {
      if (tagOf(node) === 'template' || node.getAttribute('hidden') !== null) return true;
      const style = (node.getAttribute('style') || '').replace(/ /g, '').toLowerCase();
      if (style.indexOf('display:none') >= 0 || style.indexOf('visibility:hidden') >= 0) return true;
    }
    return false;
  };

  const STATE = ['active', 'is-active', 'selected', 'is-selected', 'focus', 'focused', 'hover', 'disabled',
    'is-disabled', 'open', 'is-open', 'show', 'shown', 'collapsed', 'expanded', 'visible', 'hidden',
    'checked', 'current'];
  const hashy = (segment: string) => {
    const chars = Array.from(segment);
    if (chars.length < 4) return false;
    const digit = chars.some((c) => /\p{Nd}/u.test(c));
    const alpha = chars.some((c) => /\p{L}/u.test(c));
    if (digit && alpha) return true;
    return chars.slice(1).some((c) => /\p{Lu}/u.test(c)) && chars.some((c) => /\p{Ll}/u.test(c));
  };
  const stableClasses = (value: unknown): string[] => {
    if (typeof value !== 'string') return [];
    const out: string[] = [];
    for (const token of value.split(/\s+/).filter(Boolean)) {
      const low = token.toLowerCase();
      if (STATE.indexOf(low) >= 0 || low.indexOf('ng-') === 0 || /^(?:css|sc|jsx|svelte|astro|emotion)-/i.test(token)) continue;
      if (token.indexOf('__') >= 0 && /(?:__|_)[A-Za-z0-9-]{5,}$/.test(token)) continue;
      if (token.split(/[-_]+/).some(hashy)) continue;
      if (out.indexOf(token) < 0) out.push(token);
    }
    return out.slice(0, 8);
  };
  const generatedId = (value: unknown) => {
    if (typeof value !== 'string' || !value) return true;
    if (value.indexOf(':') >= 0 || /\p{Nd}{3,}/u.test(value)) return true;
    return value.split(/[-_.]+/).some((part) => {
      const chars = Array.from(part);
      return chars.length >= 5 && chars.some((c) => /\p{Nd}/u.test(c)) && chars.some((c) => /\p{L}/u.test(c));
    });
  };

  // fingerprint.xpath_for: a non-positional XPath that matches el and nothing else in its
  // document, or null.
  const SAFE_TAG = /^[a-z][a-z0-9-]*$/;
  const PRED_ATTRS = ['data-testid', 'data-test', 'data-qa', 'data-cy', 'id', 'name', 'aria-label',
    'placeholder', 'title', 'alt', 'href', 'autocomplete', 'for', 'type', 'role'];
  const ANCHOR_ATTRS = ['id', 'data-testid', 'data-test', 'data-qa', 'aria-label'];
  const HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'legend', 'caption'];
  const usable = (value: unknown): value is string => typeof value === 'string' && pyStrip(value) !== ''
    && pyLen(value) <= 120 && value.indexOf('\n') < 0;
  const ownPredicates = (el: Element) => {
    const preds: string[] = [];
    const late: string[] = [];
    for (const name of PRED_ATTRS) {
      const value = el.getAttribute(name);
      if (!usable(value)) continue;
      const pred = '@' + name + '=' + xpathLiteral(value);
      if (name === 'id' && generatedId(value)) late.push(pred);
      else preds.push(pred);
    }
    if (!isField(el)) {
      const text = fpNorm(el.textContent || '');
      const n = pyLen(text);
      if (n > 0 && n <= 80) preds.splice(Math.min(preds.length, 4), 0, 'normalize-space()=' + xpathLiteral(text));
    }
    for (const token of stableClasses(el.getAttribute('class')).slice(0, 3)) {
      preds.push("contains(concat(' ', normalize-space(@class), ' '), " + xpathLiteral(' ' + token + ' ') + ')');
    }
    return preds.concat(late);
  };
  const anchorPredicates = (anc: Element) => {
    const preds: string[] = [];
    for (const name of ANCHOR_ATTRS) {
      const value = anc.getAttribute(name);
      if (usable(value) && !(name === 'id' && generatedId(value))) preds.push('@' + name + '=' + xpathLiteral(value));
    }
    for (const probe of HEADINGS) {
      const hits = iterTags(anc, [probe]);
      if (hits.length) {
        const text = fpNorm(hits[0].textContent || '');
        const n = pyLen(text);
        if (n > 0 && n <= 60) preds.push('.//' + probe + '[normalize-space()=' + xpathLiteral(text) + ']');
      }
    }
    return preds;
  };
  const xpathFor = (el: Element): string | null => {
    const tag = tagOf(el);
    if (!SAFE_TAG.test(tag)) return null;
    const doc = el.ownerDocument;
    const unique = (xp: string) => {
      const hits = xpathElements(doc, xp);
      return !!hits && hits.length === 1 && hits[0] === el;
    };
    const own = ownPredicates(el);
    for (const pred of own) {
      const xp = '//' + tag + '[' + pred + ']';
      if (unique(xp)) return xp;
    }
    if (isField(el)) {
      const label = closestLabel(el);
      if (label) {
        const text = fpNorm(label.textContent || '');
        const n = pyLen(text);
        if (n > 0 && n <= 80) {
          const xp = '//label[normalize-space()=' + xpathLiteral(text) + ']//' + tag;
          if (unique(xp)) return xp;
        }
      } else if (usable(el.getAttribute('id'))) {
        for (const lab of Array.from(doc.getElementsByTagName('label'))) {
          if (lab.getAttribute('for') === el.getAttribute('id')) {
            const text = fpNorm(lab.textContent || '');
            const n = pyLen(text);
            if (n > 0 && n <= 80) {
              const xp = '//' + tag + '[@id=//label[normalize-space()=' + xpathLiteral(text) + ']/@for]';
              if (unique(xp)) return xp;
            }
            break;
          }
        }
      }
    }
    const firstSix = own.slice(0, 6);
    for (let i = 0; i < firstSix.length; i++) {
      for (const second of firstSix.slice(i + 1)) {
        const xp = '//' + tag + '[' + firstSix[i] + ' and ' + second + ']';
        if (unique(xp)) return xp;
      }
    }
    for (const anc of ancestors(el)) {
      const name = tagOf(anc);
      if (name === 'html' || name === 'body' || !SAFE_TAG.test(name)) break;
      for (const apred of anchorPredicates(anc)) {
        for (const pred of [null as string | null].concat(own.slice(0, 4))) {
          const xp = '//' + name + '[' + apred + ']//' + tag + (pred ? '[' + pred + ']' : '');
          if (unique(xp)) return xp;
        }
      }
    }
    return null;
  };

  // The elements of the live page that have role, for the fingerprint tier: what
  // fingerprint.find() scores, each with its raw read. A hidden one is left out, unless the
  // role is checkbox (a styled checkbox hides its native input).
  const collect = (role: string) => {
    const doc = document;
    const root = doc.documentElement;
    const labels = labelIndex(doc);
    const els: Element[] = [];
    const raws: unknown[] = [];
    if (root) {
      for (const el of iterAll(root)) {
        if (roleOf(el) !== role) continue;
        if (role !== 'checkbox' && staticallyHidden(el)) continue;
        els.push(el);
        raws.push(rawOf(el, labels));
      }
    }
    return { els: els, raws: raws };
  };
  // The id of a recorded container in ids that is on the page once and does not hold el, or ''.
  const outside = (el: Element, ids: string[]) => {
    const doc = el.ownerDocument;
    const found: Record<string, Element[]> = {};
    for (const node of iterAll(doc.documentElement)) {
      const raw = node.getAttribute('id');
      if (raw === null) continue;
      const value = pySlice(fpNorm(raw), 120);
      if (ids.indexOf(value) >= 0) (found[value] = found[value] || []).push(node);
    }
    const held = ancestors(el);
    for (const value of Object.keys(found).sort()) {
      if (found[value].length === 1 && held.indexOf(found[value][0]) < 0) return value;
    }
    return '';
  };

  // --- the static gate (validation.validate), on a captured page -------------------------
  // ok when xpath matches exactly one element of role in the page html, which no
  // quarantined selector of the key reaches.
  const validate = (arg: { xpath: string; html: string; role: string; quarantined?: string[] }) => {
    const doc = parse(arg.html);
    const error = xpathError(doc, arg.xpath);
    if (error) return { ok: false, reason: 'xpath error: ' + error, count: 0 };
    const hits = xpathElements(doc, arg.xpath) || [];
    if (!hits.length) return { ok: false, reason: 'no match', count: 0 };
    if (hits.length > 1) return { ok: false, reason: 'not unique (' + hits.length + ')', count: hits.length };
    const role = roleOf(hits[0]);
    if (role !== arg.role) return { ok: false, reason: 'role mismatch: got ' + role + ' want ' + arg.role, count: 1 };
    for (const selector of arg.quarantined || []) {
      const taken = xpathElements(doc, selector);
      if (taken && taken.indexOf(hits[0]) >= 0) return { ok: false, reason: 'quarantined', count: 1 };
    }
    return { ok: true, reason: 'ok', count: 1 };
  };

  // --- describe() and inventory() (drivers/base.py) --------------------------------------
  const describe = (els: Element[]) => {
    if (!els || els.length !== 1) return null;
    const el = els[0];
    const tag = tagOf(el);
    return {
      tag: tag, role: roleOf(el),
      text: tag === 'textarea' ? '' : pySlice(wsNorm(el.textContent || ''), 200),
      outer_html: pySlice((el.outerHTML || '').replace(/\svalue=("[^"]*"|'[^']*'|[^\s>]+)/gi, ''), 2000),
    };
  };
  // drivers.base._INVENTORY_SCAN: one pass over the interactive elements of scope, the
  // document or a shadow root.
  const inventoryIn = (limit: number, scope: any) => {
    const out: Record<string, string>[] = [];
    const nodes = scope.querySelectorAll('button, a, input, select, textarea, [role]');
    const fieldRole = /^(textbox|searchbox|combobox|spinbutton|slider)$/i;
    for (const e of Array.from(nodes) as HTMLElement[]) {
      if (out.length >= limit) break;
      const tag = e.tagName.toLowerCase();
      const type = (e.getAttribute('type') || '').toLowerCase();
      if (tag === 'input' && type === 'hidden') continue;
      const role = e.getAttribute('role') || '';
      const field = tag === 'input' || tag === 'textarea' || tag === 'select' || e.isContentEditable || fieldRole.test(role);
      out.push({ tag: tag, role: role, type: type, id: e.getAttribute('id') || '', name: e.getAttribute('name') || '',
        text: field ? '' : (e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
        aria_label: e.getAttribute('aria-label') || '' });
    }
    return out;
  };
  const inventory = (limit: number) => inventoryIn(limit, document);

  // --- open shadow roots (drivers.base) ---------------------------------------------------
  // SHADOW_HTML_JS: what the shadow root the host selectors lead to holds (its innerHTML: not
  // the host, not the light DOM slotted into it, not a nested shadow root's content), or null
  // when they lead to none.
  const shadowHtml = (hosts: string[]) => {
    const root = enter(document, hosts);
    return root ? root.innerHTML : null;
  };
  // SHADOW_INVENTORY_JS: inventoryIn()'s rows for that shadow root, or null.
  const shadowInventory = (arg: { hosts: string[]; limit: number }) => {
    const root = enter(document, arg.hosts);
    return root ? inventoryIn(arg.limit, root) : null;
  };
  // SHADOW_ROOTS_JS: every open shadow root of the document, at any depth, in document order,
  // with the selectors of its hosts, outermost first (see hostsOf). With arg.css, the roots css
  // matches something in, and how many elements (count). Without it, each root's innerHTML
  // (html), for an XPath that mendwright evaluates in it. At most arg.limit roots, and
  // arg.budget characters of HTML; cut says some were left out.
  const shadowRoots = (arg: { css: string | null; limit: number; budget: number }) => {
    const roots: { hosts: (string | null)[]; count?: number; html?: string }[] = [];
    let cut = false;
    let size = 0;
    const walk = (scope: any, chain: (string | null)[]) => {
      for (const el of Array.from(scope.querySelectorAll('*')) as Element[]) {
        if (cut) return;
        const root = el.shadowRoot;
        if (!root) continue;
        const here = chain.concat([hostSelector(el)]);
        if (arg.css !== null) {
          const n = root.querySelectorAll(arg.css).length;
          if (n) {
            if (roots.length >= arg.limit) { cut = true; return; }
            roots.push({ hosts: here, count: n });
          }
        } else {
          const html = root.innerHTML;
          size += html.length;
          if (roots.length >= arg.limit || size > arg.budget) { cut = true; return; }
          roots.push({ hosts: here, html: html });
        }
        walk(root, here);
      }
    };
    walk(document, []);
    return { roots: roots, cut: cut };
  };

  // --- capture.clean_html ---------------------------------------------------------------
  // The page without what never helps a model pick a selector: scripts, styles, noscript,
  // templates, comments and processing instructions, the inside of svg, and everything in
  // head but title; runs of ASCII whitespace in text collapsed.
  const cleanHtml = (html: string) => {
    if (typeof html !== 'string' || !html.trim()) return html;
    const doc = parse(html.replace(/^\s*<\?xml[^>]*\?>/, ''));
    const root = doc.documentElement;
    for (const el of Array.from(root.querySelectorAll('script, style, noscript, template'))) el.remove();
    const walker = doc.createTreeWalker(root, 128 | 64 /* comments, processing instructions */);
    const drop: Node[] = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) drop.push(n);
    for (const n of drop) n.parentNode && n.parentNode.removeChild(n);
    for (const svg of Array.from(root.querySelectorAll('svg'))) {
      while (svg.firstChild) svg.removeChild(svg.firstChild);
    }
    const head = doc.head;
    if (head) {
      for (const child of Array.from(head.children)) {
        if (['meta', 'link', 'base'].indexOf(tagOf(child)) >= 0) child.remove();
      }
    }
    const texts = doc.createTreeWalker(root, 4 /* text */);
    for (let n = texts.nextNode(); n; n = texts.nextNode()) {
      n.nodeValue = (n.nodeValue || '').replace(/[ \t\n\r\f\v]+/g, ' ');
    }
    return root.outerHTML;
  };

  // --- the local selector model's candidates (candidates.py) -------------------------------
  const TEST_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy', 'data-e2e', 'data-automation-id'];
  const C_SPACE = /[ \t\r\n]+/g;
  const REDACTED = ['[URL]', '[redacted'];
  const GENERATED = /^:r[0-9a-z]*:$|^ember\d+$|^mat-[a-z-]+-\d+$|^cdk-[a-z-]+-\d+$|^react-[a-z-]+-\d+|^radix-|^headlessui-|^[0-9a-f]{8}-[0-9a-f]{4}-|^\d+$|\d{4,}|^(?:css|sc|jsx|emotion|svelte|chakra|mui)-[a-z0-9]{4,}$|(?:^|[_-])[a-zA-Z0-9]{5,}$(?<=[0-9][a-zA-Z0-9]{4})|^[a-z]{1,3}[0-9][a-z0-9]{3,}$/;
  const HASHED_CLASS = /^(?:css|sc|jsx|emotion|svelte|chakra|mui|tw)-[a-z0-9]{4,}$|_[a-z0-9]{5,}$|__[a-z0-9]{4,}$|^[a-z]{1,3}[0-9][a-z0-9]{3,}$|^[A-Za-z]{5,8}$(?<=[A-Z][a-z][A-Z][a-z][A-Z])/;
  const HIDDEN_CLASSES = ['hidden', 'd-none', 'is-hidden', 'sr-only', 'visually-hidden', 'invisible', 'hide',
    'u-hidden', 'display-none', 'offscreen'];
  const LANDMARKS: [string, string[], string[]][] = [
    ['dialog', ['dialog'], ['dialog', 'alertdialog']],
    ['header', ['header'], ['banner']],
    ['nav', ['nav'], ['navigation', 'menubar', 'tablist']],
    ['footer', ['footer'], ['contentinfo']],
    ['aside', ['aside'], ['complementary']],
    ['form', ['form'], ['form', 'search']],
    ['main', ['main'], ['main']],
  ];
  const ITEM_TAGS = ['tr', 'li', 'article'];
  const ITEM_ROLES = ['row', 'listitem', 'article', 'option', 'gridcell'];
  const CONTAINER_TAGS = ['section', 'form', 'fieldset', 'dialog', 'nav', 'aside', 'article', 'li', 'tr',
    'table', 'header', 'footer', 'main', 'div', 'ul', 'ol', 'details'];
  const CONTROL_TAGS = ['button', 'a', 'input', 'select', 'textarea', 'label', 'option'];
  const C_FIELD_ROLES = ['input', 'checkbox', 'radio', 'combobox', 'textarea'];
  const CONTROL_ROLES = ['button', 'link', 'checkbox', 'radio', 'switch', 'textbox', 'combobox', 'menuitem',
    'tab', 'option', 'searchbox'];
  const SCOPE_TAGS = ['header', 'nav', 'footer', 'aside', 'dialog'];
  const SCOPE_ROLES = ['dialog', 'alertdialog', 'banner', 'navigation', 'contentinfo', 'complementary'];
  const NAMED = ['section', 'form', 'fieldset', 'aside', 'nav', 'dialog', 'header', 'footer'];
  const NAMED_ROLES = ['dialog', 'alertdialog', 'region', 'group', 'radiogroup', 'navigation', 'tabpanel', 'form', 'search'];
  const FIELD_TAGS = ['input', 'select', 'textarea'];
  const UTILITY = /^(?:p|m|px|py|mx|my|pt|pb|pl|pr|mt|mb|ml|mr|w|h|gap|text|bg|flex|grid|items|justify|rounded|border|shadow|font|leading|tracking|col|row|ring|space|inset|z|top|left|right|bottom|min|max|sm|md|lg|xl|hover|focus)-/;
  const TEXT_CLIP = 40;
  const ATTR_CLIP = 40;

  const clip = (text: unknown, limit: number) => {
    const t = pyStrip((typeof text === 'string' ? text : '').replace(C_SPACE, ' '));
    if (pyLen(t) <= limit) return t;
    return pySlice(t, limit - 1).replace(new RegExp('[' + PY_SPACE + ']+$'), '') + '\u2026';
  };
  const textOf = (el: Element) => pyStrip((el.textContent || '').replace(C_SPACE, ' '));
  const cUsable = (value: unknown, limit = 120): value is string => typeof value === 'string' && pyStrip(value) !== ''
    && pyLen(value) <= limit && !REDACTED.some((mark) => value.indexOf(mark) >= 0);
  const generated = (value: unknown) => typeof value !== 'string' || !value || GENERATED.test(value) || generatedId(value);
  // lxml's .text: the text before the first child element.
  const leadText = (el: Element) => {
    let out = '';
    for (const n of Array.from(el.childNodes)) {
      if (n.nodeType === 3 || n.nodeType === 4) out += (n as Text).data;
      else break;
    }
    return out;
  };
  const getRole = (el: Element) => lower(el.getAttribute('role') || '');

  const hidden = (el: Element) => {
    if (tagOf(el) === 'input' && lower(el.getAttribute('type') || '') === 'hidden') return 'hidden';
    let maybe = false;
    for (let node: Element | null = el; node; node = node.parentElement) {
      const style = (node.getAttribute('style') || '').replace(/ /g, '').toLowerCase();
      if (style.indexOf('display:none') >= 0 || style.indexOf('visibility:hidden') >= 0
          || node.getAttribute('hidden') !== null || lower(node.getAttribute('aria-hidden') || '') === 'true') return 'hidden';
      const classes = lower(node.getAttribute('class') || '').split(/\s+/).filter(Boolean);
      if (classes.some((c) => HIDDEN_CLASSES.indexOf(c) >= 0 || c.slice(-7) === ':hidden')) maybe = true;
    }
    return maybe ? 'hidden?' : '';
  };
  const landmarkId = (ident: unknown) => (typeof ident === 'string' && ident && cUsable(ident, 30) && !generated(ident)
    && /^[A-Za-z][\w-]*$/.test(ident) ? ident : '');
  const region = (el: Element) => {
    const found: [string, string][] = [];
    for (const anc of ancestors(el)) {
      const tag = tagOf(anc);
      const role = getRole(anc);
      for (const [name, tags, roles] of LANDMARKS) {
        if (tags.indexOf(tag) >= 0 || roles.indexOf(role) >= 0 || (name === 'dialog' && anc.getAttribute('aria-modal') === 'true')) {
          if (!found.length || found[found.length - 1][0] !== name) found.push([name, landmarkId(anc.getAttribute('id'))]);
          break;
        }
      }
    }
    found.reverse();
    for (const [name, ident] of found) {
      if (name === 'dialog') return 'dialog' + (ident ? '#' + ident : '');
    }
    return found.slice(-2).map(([name, ident]) => name + (ident ? '#' + ident : '')).join('>');
  };
  const byIds = (el: Element, ids: string | null) => {
    const doc = el.ownerDocument;
    const parts: string[] = [];
    for (const ident of (ids || '').split(/\s+/).filter(Boolean)) {
      const hit = doc.getElementById(ident);
      if (hit) parts.push(textOf(hit));
    }
    return parts.filter(Boolean).join(' ');
  };
  const fieldLabel = (el: Element) => {
    const by = byIds(el, el.getAttribute('aria-labelledby'));
    if (by) return by;
    const ident = el.getAttribute('id');
    const doc = el.ownerDocument;
    if (ident) {
      for (const label of Array.from(doc.getElementsByTagName('label'))) {
        if (label.getAttribute('for') === ident) {
          const text = textOf(label);
          if (text) return text;
          break;
        }
      }
    }
    const wrap = closestLabel(el);
    if (wrap) {
      const text = textOf(wrap);
      if (text) return text;
    }
    const parent = el.parentElement;
    if (parent) {
      let prev = el.previousElementSibling;
      let hops = 0;
      while (prev && hops < 2) {
        if (['input', 'select', 'textarea', 'button'].indexOf(tagOf(prev)) < 0) {
          const text = textOf(prev);
          const n = pyLen(text);
          if (n > 0 && n <= 60) return text;
        }
        prev = prev.previousElementSibling;
        hops += 1;
      }
      for (const label of Array.from(parent.children)) {
        if (tagOf(label) === 'label' && label.getAttribute('for') === null) {
          const text = textOf(label);
          if (text) return text;
          break;
        }
      }
    }
    return '';
  };
  const isControl = (node: Element) => CONTROL_TAGS.indexOf(tagOf(node)) >= 0 || CONTROL_ROLES.indexOf(getRole(node)) >= 0;
  const inside = (node: Element, el: Element) => node === el || el.contains(node) || node.contains(el);
  const decorative = (node: Element, item: Element) => {
    for (let anc: Element | null = node; anc; anc = anc.parentElement) {
      if (anc === item) return false;
      if (lower(anc.getAttribute('aria-hidden') || '') === 'true') return true;
    }
    return false;
  };
  const itemTexts = (item: Element, el: Element) => {
    const out: string[] = [];
    const add = (text: string) => {
      const t = pyStrip((text || '').replace(C_SPACE, ' '));
      if (t && out.indexOf(t) < 0) out.push(t);
    };
    for (const h of iterTags(item, HEADINGS)) {
      if (!inside(h, el) && !decorative(h, item)) add(textOf(h));
    }
    for (const node of iterTags(item, ['a'])) {
      if (lower(node.getAttribute('role') || 'link') !== 'link') continue;
      if (!inside(node, el) && !decorative(node, item)) {
        const text = textOf(node);
        if (pyLen(text) > 1) add(text);
      }
    }
    const short: string[] = [];
    for (const node of iterAll(item)) {
      if (inside(node, el) || decorative(node, item)) continue;
      if (isControl(node) || ['option', 'script', 'style'].indexOf(tagOf(node)) >= 0) continue;
      let inControl = false;
      for (let a = node.parentElement; a && a !== item; a = a.parentElement) {
        if (item.contains(a) && isControl(a)) { inControl = true; break; }
      }
      if (inControl) continue;
      const text = pyStrip(leadText(node).replace(C_SPACE, ' '));
      const n = pyLen(text);
      if (n >= 3) add(text);
      else if (n > 1) short.push(text);
    }
    for (const img of iterTags(item, ['img'])) {
      const alt = img.getAttribute('alt');
      if (cUsable(alt)) add(alt);
    }
    for (const text of short) add(text);
    return out;
  };
  const itemLabel = (item: Element, el: Element) => {
    const texts = itemTexts(item, el);
    if (!texts.length) return '';
    if (texts.length > 1 && pyLen(texts[0]) < 24) return texts[0] + ' \u00b7 ' + texts[1];
    return texts[0];
  };
  const HEADING_SELECTOR = 'h1, h2, h3, h4, h5, h6, legend, [role="heading"]';
  const precedingHeading = (node: Element) => {
    for (let sib = node.previousElementSibling; sib; sib = sib.previousElementSibling) {
      const tag = tagOf(sib);
      if (HEADINGS.indexOf(tag) >= 0 || (sib.getAttribute('role') || '') === 'heading') {
        const text = textOf(sib);
        if (text) return text;
        continue;
      }
      if (['ul', 'ol', 'table', 'tbody', 'form', 'nav'].indexOf(tag) >= 0) continue;
      const heads = sib.querySelectorAll(HEADING_SELECTOR);
      if (heads.length === 1) {
        const text = textOf(heads[0]);
        if (text) return text;
      }
    }
    return '';
  };
  const pairedField = (el: Element) => {
    const role = roleOf(el);
    if (role !== 'button' && role !== 'link') return '';
    const levels = [el];
    const parent = el.parentElement;
    if (parent && parent.children.length <= 2) levels.push(parent);
    for (const node of levels) {
      const sib = node.previousElementSibling;
      if (!sib) continue;
      let fields = FIELD_TAGS.indexOf(tagOf(sib)) >= 0 ? [sib] : Array.from(sib.querySelectorAll('input, select, textarea'));
      fields = fields.filter((f) => ['hidden', 'submit', 'button', 'checkbox', 'radio', 'reset', 'image']
        .indexOf(lower(f.getAttribute('type') || '')) < 0);
      if (fields.length === 1) {
        const label = fieldLabel(fields[0]) || fields[0].getAttribute('placeholder') || fields[0].getAttribute('aria-label');
        if (label && cUsable(label, 80)) return label;
      }
      if (fields.length) return '';
    }
    return '';
  };
  const context = (el: Element) => {
    const chain = ancestors(el);
    for (let depth = 0; depth < chain.length; depth++) {
      const anc = chain[depth];
      if (['body', 'html'].indexOf(tagOf(anc)) >= 0 || depth > 8) break;
      if (ITEM_TAGS.indexOf(tagOf(anc)) >= 0 || ITEM_ROLES.indexOf(getRole(anc)) >= 0) {
        const label = itemLabel(anc, el);
        if (label) return 'item ' + JSON.stringify(clip(label, 32));
        break;
      }
    }
    const field = pairedField(el);
    if (field) return 'for ' + JSON.stringify(clip(field, 32));
    let node = el;
    for (let depth = 0; depth < chain.length; depth++) {
      const anc = chain[depth];
      const tag = tagOf(anc);
      if (['body', 'html'].indexOf(tag) >= 0 || depth > 14) break;
      const role = getRole(anc);
      if (ITEM_TAGS.indexOf(tag) >= 0 || ITEM_ROLES.indexOf(role) >= 0) {
        const label = itemLabel(anc, el);
        if (label) return 'item ' + JSON.stringify(clip(label, 32));
      }
      const text = precedingHeading(node);
      if (text) return JSON.stringify(clip(text, 32));
      if (NAMED.indexOf(tag) >= 0 || NAMED_ROLES.indexOf(role) >= 0) {
        const name = anc.getAttribute('aria-label') || byIds(anc, anc.getAttribute('aria-labelledby'));
        if (name) return JSON.stringify(clip(name, 32));
      }
      if (SCOPE_TAGS.indexOf(tag) >= 0 || SCOPE_ROLES.indexOf(role) >= 0) return '';
      node = anc;
    }
    return '';
  };
  const href = (value: unknown) => {
    if (!cUsable(value, 400)) return '';
    const v = pyStrip(value);
    for (const p of ['javascript:', 'mailto:', 'tel:']) if (v.indexOf(p) === 0) return v.split(':')[0] + ':';
    return clip(v.split('?')[0], ATTR_CLIP);
  };
  const meaningfulClass = (token: string) => {
    const n = pyLen(token);
    if (n < 3 || n > 32 || token.indexOf(':') >= 0 || HASHED_CLASS.test(token)) return false;
    if (/^(?:css|sc|jsx|svelte|astro|emotion)-/i.test(token)) return false;
    if (token.split(/[-_]+/).some(hashy)) return false;
    return true;
  };
  const classOk = (token: string) => meaningfulClass(token) && !UTILITY.test(token);
  const featuresOf = (el: Element) => {
    const tag = tagOf(el);
    const role = pyStrip(el.getAttribute('role') || '').toLowerCase();
    const typ = pyStrip(el.getAttribute('type') || '').toLowerCase();
    const out: Record<string, string> = {};
    if (tag === 'input') out.tag = 'input[' + (typ || 'text') + ']';
    else if (role && role !== 'presentation' && role !== 'none') out.tag = tag + '[role=' + role + ']';
    else out.tag = tag;
    const gate = roleOf(el);
    const fieldRole = C_FIELD_ROLES.indexOf(gate) >= 0;
    if (!fieldRole || tag === 'button' || tag === 'a') {
      const text = textOf(el);
      if (text) out.text = clip(text, TEXT_CLIP);
    }
    if (fieldRole) {
      const label = fieldLabel(el);
      if (label) out.label = clip(label, TEXT_CLIP);
      if (tag === 'select') {
        const options = iterTags(el, ['option']).map(textOf).slice(0, 3).filter(Boolean);
        if (options.length) out.options = clip(options.join(', '), TEXT_CLIP);
      }
    }
    for (const name of ['id', 'name']) {
      const value = el.getAttribute(name);
      if (cUsable(value)) out[name] = clip(value, ATTR_CLIP);
    }
    for (const name of TEST_ATTRS) {
      const value = el.getAttribute(name);
      if (cUsable(value)) { out.testid = clip(value, ATTR_CLIP); break; }
    }
    for (const [name, key] of [['aria-label', 'aria'], ['placeholder', 'ph'], ['title', 'title']]) {
      const value = el.getAttribute(name);
      if (cUsable(value)) out[key] = clip(value, ATTR_CLIP);
    }
    if (!('aria' in out) && el.getAttribute('aria-labelledby') && !fieldRole) {
      const by = byIds(el, el.getAttribute('aria-labelledby'));
      if (by) out.aria = clip(by, ATTR_CLIP);
    }
    if (!('text' in out)) {
      for (const img of iterTags(el, ['img'])) {
        const alt = img.getAttribute('alt');
        if (cUsable(alt)) { out.alt = clip(alt, ATTR_CLIP); break; }
      }
    }
    if (tag === 'a' || el.getAttribute('href')) {
      const h = href(el.getAttribute('href'));
      if (h) out.href = h;
    }
    const auto = el.getAttribute('autocomplete');
    if (auto && auto !== 'off' && auto !== 'on') out.auto = clip(auto, 24);
    const classes = stableClasses(el.getAttribute('class')).filter(classOk).slice(0, 2);
    if (classes.length) out.cls = classes.join(' ');
    const ctx = context(el);
    if (ctx) out.in = ctx;
    const reg = region(el);
    if (reg) out.region = reg;
    const hid = hidden(el);
    if (hid) out.hidden = hid;
    if (el.getAttribute('disabled') !== null || lower(el.getAttribute('aria-disabled') || '') === 'true') out.disabled = 'disabled';
    if (['', 'false'].indexOf(el.getAttribute('aria-current') || '') < 0) out.current = 'current';
    return out;
  };
  const ORDER = ['text', 'label', 'id', 'name', 'testid', 'aria', 'ph', 'title', 'alt', 'href', 'auto', 'options', 'cls'];
  const featureLine = (features: Record<string, string>) => {
    const parts = [features.tag || '?'];
    for (const key of ORDER) {
      const value = features[key];
      if (!value) continue;
      if (key === 'text') parts.push(JSON.stringify(value));
      else if (key === 'cls') parts.push('class=' + JSON.stringify(value));
      else parts.push(key + '=' + JSON.stringify(value));
    }
    let line = parts.join(' ');
    const ctx = features.in;
    if (ctx) line += (ctx.indexOf('for ') === 0 ? ' | ' : ' | in ') + ctx;
    if (features.region) line += ' | ' + features.region;
    const flags = ['hidden', 'disabled', 'current'].filter((k) => features[k]).map((k) => features[k]);
    if (flags.length) line += ' | ' + flags.join(' ');
    return line;
  };
  // candidates._chain: a fingerprint's ancestors as 'li.cart-item < ul < main', and the
  // landmarks among them written like a candidate's region.
  const chainOf = (list: any[]) => {
    const parts: string[] = [];
    const found: [string, string][] = [];
    for (const anc of list.slice(0, 4)) {
      if (!anc || typeof anc !== 'object' || typeof anc.tag !== 'string') continue;
      const tag = anc.tag;
      const role = typeof anc.role === 'string' ? anc.role : '';
      for (const [name, tags, roles] of LANDMARKS) {
        if (tags.indexOf(tag) >= 0 || roles.indexOf(role) >= 0) {
          if (!found.length || found[found.length - 1][0] !== name) found.push([name, landmarkId(anc.id)]);
          break;
        }
      }
      if (parts.length >= 3) continue;
      let part = tag;
      if (typeof anc.id === 'string' && cUsable(anc.id, 40) && !generated(anc.id)) part += '#' + anc.id;
      const classes = anc.classes;
      if (Array.isArray(classes) && classes.length && typeof classes[0] === 'string' && classOk(classes[0])) part += '.' + classes[0];
      parts.push(part);
    }
    found.reverse();
    let reg = '';
    const dialog = found.filter(([name]) => name === 'dialog')[0];
    if (dialog) reg = 'dialog' + (dialog[1] ? '#' + dialog[1] : '');
    else reg = found.slice(-2).map(([name, ident]) => name + (ident ? '#' + ident : '')).join('>');
    return [parts.join(' < '), reg];
  };
  // candidates.fingerprint_line: what is known about the element the step used to match.
  const fingerprintLine = (fp: any) => {
    if (!fp || typeof fp !== 'object' || Array.isArray(fp) || !Object.keys(fp).length) return '';
    const attrs = fp.attrs && typeof fp.attrs === 'object' && !Array.isArray(fp.attrs) ? fp.attrs : {};
    const pick = (...names: string[]) => {
      for (const name of names) {
        for (const source of [fp, attrs]) {
          const value = source[name];
          if (typeof value === 'string' && cUsable(value, 400)) return value;
        }
      }
      return '';
    };
    const feats: Record<string, string> = {};
    const tag = fp.tag;
    if (typeof tag === 'string' && tag) {
      const typ = pick('type');
      const role = pick('role');
      if (tag === 'input') feats.tag = 'input[' + (typ || 'text') + ']';
      else if (role) feats.tag = tag + '[role=' + role + ']';
      else feats.tag = tag;
    }
    const mapping: [string, string[], number][] = [['text', ['text'], TEXT_CLIP], ['label', ['label'], TEXT_CLIP],
      ['id', ['id'], ATTR_CLIP], ['name', ['name'], ATTR_CLIP], ['testid', ['testid'].concat(TEST_ATTRS), ATTR_CLIP],
      ['aria', ['aria', 'aria-label'], ATTR_CLIP], ['ph', ['ph', 'placeholder'], ATTR_CLIP],
      ['title', ['title'], ATTR_CLIP], ['alt', ['alt'], ATTR_CLIP], ['auto', ['autocomplete'], 24]];
    for (const [key, names, limit] of mapping) {
      const value = pick(...names);
      if (value && !(key === 'auto' && (value === 'on' || value === 'off'))) feats[key] = clip(value, limit);
    }
    const h = pick('href');
    if (h) feats.href = href(h);
    if (Array.isArray(fp.classes)) {
      const good = fp.classes.filter((c: unknown) => typeof c === 'string' && classOk(c)).slice(0, 2);
      if (good.length) feats.cls = good.join(' ');
    }
    const ctx = pick('in', 'context', 'heading');
    if (ctx) feats.in = ctx.indexOf('"') === 0 || ctx.indexOf('item ') === 0 ? ctx : JSON.stringify(clip(ctx, 32));
    const [chain, reg0] = chainOf(Array.isArray(fp.ancestors) ? fp.ancestors : []);
    const reg = pick('region') || reg0;
    if (reg) feats.region = reg;
    if (Object.keys(feats).length <= 1 && 'tag' in feats && !chain) return '';
    if (!('tag' in feats)) feats.tag = '?';
    let line = featureLine(feats);
    if (chain) line += ' | within ' + chain;
    return line;
  };
  const SKIP_TAGS = ['html', 'head', 'body', 'title', 'meta', 'link', 'script', 'style', 'option'];
  // candidates.extract, before the ranking: every element of the page html whose role is
  // role, with its features, its line and its raw fingerprint read. index is its place among
  // the document's elements, which robustXpaths() takes.
  const candidates = (arg: { html: string; role: string; fingerprint?: unknown }) => {
    const doc = parse(arg.html);
    const all = allElements(doc);
    const labels = labelIndex(doc);
    const items: { index: number; features: Record<string, string>; line: string; raw: unknown }[] = [];
    all.forEach((el, index) => {
      if (SKIP_TAGS.indexOf(tagOf(el)) >= 0) return;
      if (arg.role && roleOf(el) === arg.role) {
        const features = featuresOf(el);
        items.push({ index: index, features: features, line: featureLine(features), raw: rawOf(el, labels) });
      }
    });
    const fp: any = arg.fingerprint && typeof arg.fingerprint === 'object' ? arg.fingerprint : null;
    let fpRegion = fp && typeof fp.region === 'string' ? fp.region : null;
    if (fpRegion === null && fp && Array.isArray(fp.ancestors)) fpRegion = chainOf(fp.ancestors)[1] || null;
    return { items: items, fingerprintLine: fingerprintLine(arg.fingerprint), fingerprintRegion: fpRegion };
  };
  // The place of the one element xpath matches in the page html, or null.
  const indexOfMatch = (arg: { html: string; xpath: string }) => {
    const doc = parse(arg.html);
    const hits = xpathElements(doc, arg.xpath);
    if (!hits || hits.length !== 1) return null;
    return allElements(doc).indexOf(hits[0]);
  };
  // candidates.robust_xpaths: up to limit XPaths that each match the element at index of
  // the page html and nothing else, most robust first. Never a bare position.
  const VISIBLE = "not(ancestor-or-self::*[@hidden or @aria-hidden='true' or contains(translate(@style, ' ', ''), 'display:none') or contains(translate(@style, ' ', ''), 'visibility:hidden')])";
  const textPred = (el: Element) => {
    const text = textOf(el);
    const n = pyLen(text);
    if (!(n > 0 && n <= 80) || !cUsable(text, 80)) return null;
    if (el.querySelector('svg')) return 'contains(normalize-space(), ' + xpathLiteral(text) + ')';
    return 'normalize-space()=' + xpathLiteral(text);
  };
  const robustOwn = (el: Element) => {
    const preds: string[] = [];
    const ident = el.getAttribute('id');
    if (cUsable(ident) && !generated(ident)) preds.push('@id=' + xpathLiteral(ident));
    for (const name of TEST_ATTRS) {
      const value = el.getAttribute(name);
      if (cUsable(value) && !generated(value)) { preds.push('@' + name + '=' + xpathLiteral(value)); break; }
    }
    for (const name of ['name', 'aria-label', 'placeholder', 'title', 'formcontrolname', 'for']) {
      const value = el.getAttribute(name);
      if (cUsable(value) && !(name === 'name' && generated(value))) preds.push('@' + name + '=' + xpathLiteral(value));
    }
    const text = textPred(el);
    if (text && FIELD_TAGS.indexOf(tagOf(el)) < 0) preds.push(text);
    const h = el.getAttribute('href');
    if (cUsable(h) && h.indexOf('javascript') !== 0 && h !== '#') preds.push('@href=' + xpathLiteral(h));
    const auto = el.getAttribute('autocomplete');
    if (auto && cUsable(auto)) preds.push('@autocomplete=' + xpathLiteral(auto));
    if (cUsable(ident) && generated(ident)) preds.push('@id=' + xpathLiteral(ident));
    return preds;
  };
  const labelAnchors = (el: Element) => {
    const out: string[] = [];
    const tag = tagOf(el);
    const ident = el.getAttribute('id');
    if (ident && cUsable(ident)) {
      for (const label of Array.from(el.ownerDocument.getElementsByTagName('label'))) {
        if (label.getAttribute('for') === ident) {
          const text = textOf(label);
          if (cUsable(text, 80) && text) out.push('//' + tag + '[@id=//label[normalize-space()=' + xpathLiteral(text) + ']/@for]');
          break;
        }
      }
    }
    const wrap = closestLabel(el);
    if (wrap) {
      const text = textOf(wrap);
      if (cUsable(text, 80) && text) out.push('//label[normalize-space()=' + xpathLiteral(text) + ']//' + tag);
    }
    return out;
  };
  const robustAnchors = (anc: Element, el: Element) => {
    const preds: string[] = [];
    for (const name of ['id'].concat(TEST_ATTRS, ['aria-label', 'name'])) {
      const value = anc.getAttribute(name);
      if (cUsable(value) && !(name === 'id' && generated(value))) preds.push('@' + name + '=' + xpathLiteral(value));
    }
    if (CONTAINER_TAGS.indexOf(tagOf(anc)) >= 0 || ITEM_ROLES.concat(['dialog', 'region', 'group']).indexOf(anc.getAttribute('role') || '') >= 0) {
      const probes = HEADINGS.concat(['label', 'strong', 'b', 'td', 'th', 'span', 'p']);
      let seen = 0;
      for (const node of iterTags(anc, probes)) {
        if (node === el || el.contains(node) || node.contains(el)) continue;
        const text = textOf(node);
        const n = pyLen(text);
        if (n > 0 && n <= 60 && cUsable(text, 60)) {
          const literal = '[normalize-space()=' + xpathLiteral(text) + ']';
          if (node.parentElement === anc) preds.push(tagOf(node) + literal);
          preds.push('.//' + tagOf(node) + literal);
          seen += 1;
          if (seen >= 2) break;
        }
      }
    }
    return preds;
  };
  const robustXpaths = (arg: { html: string; index: number; limit?: number }) => {
    const doc = parse(arg.html);
    const el = allElements(doc)[arg.index];
    if (!el) return [];
    const limit = arg.limit || 4;
    const tag = tagOf(el);
    const out: string[] = [];
    const near: string[] = [];
    const add = (xpath: string) => {
      if (out.indexOf(xpath) >= 0) return out.length >= limit;
      const hits = xpathElements(doc, xpath);
      if (hits && hits.length === 1 && hits[0] === el) out.push(xpath);
      else if (hits && hits.length && near.length < 40 && hits.indexOf(el) >= 0) near.push(xpath);
      return out.length >= limit;
    };
    const own = robustOwn(el);
    const typed: string[] = [];
    if (tag === 'input' && el.getAttribute('type')) typed.push('@type=' + xpathLiteral(el.getAttribute('type') as string));
    const role = el.getAttribute('role');
    if (role && cUsable(role)) typed.push('@role=' + xpathLiteral(role));
    for (const pred of own) if (add('//' + tag + '[' + pred + ']')) return out;
    for (const xpath of labelAnchors(el)) if (add(xpath)) return out;
    const pairs = own.slice(0, 5).concat(typed);
    for (let i = 0; i < pairs.length; i++) {
      for (const second of pairs.slice(i + 1)) {
        if (add('//' + tag + '[' + pairs[i] + ' and ' + second + ']')) return out;
      }
    }
    if (out.length >= 2) return out;
    const tails = [null as string | null].concat(own.slice(0, 4), typed.slice(0, 1));
    let depth = 0;
    for (const anc of ancestors(el)) {
      const name = tagOf(anc);
      if (name === 'html' || name === 'body') break;
      depth += 1;
      if (depth > 8) break;
      for (const apred of robustAnchors(anc, el)) {
        for (const pred of tails) {
          if (add('//' + name + '[' + apred + ']//' + tag + (pred ? '[' + pred + ']' : ''))) return out;
        }
      }
      if (['nav', 'header', 'footer', 'main', 'aside', 'form', 'dialog'].indexOf(name) >= 0) {
        for (const pred of own.slice(0, 4).concat(typed.slice(0, 1))) {
          if (add('//' + name + '//' + tag + '[' + pred + ']')) return out;
        }
      }
    }
    if (!out.length) {
      for (const xpath of near.slice()) if (add(xpath + '[' + VISIBLE + ']')) return out;
    }
    return out;
  };

  // --- CSS forms of an element (css.css_selectors) ---------------------------------------
  // Up to limit (3) CSS selectors that match el and nothing else in doc, most robust first:
  // el's own attributes, two of them together, then a container with an id, test id, name,
  // aria-label or class name around it. Each is made of forms with an exact XPath twin (a tag
  // name, #id, .class, [attr="value"] and the descendant combinator) and counts only when its
  // twin matches el alone. [] when none of these tells el apart, or when el is inside an <svg>
  // or <math>.
  const CSS_IDENT = /^-?[A-Za-z_][A-Za-z0-9_-]*$/;
  const CSS_ATTRS = ['name', 'aria-label', 'placeholder', 'title', 'alt', 'for', 'formcontrolname', 'autocomplete',
    'href', 'type', 'role'];
  const CSS_ANCHOR_ATTRS = ['id'].concat(TEST_ATTRS, ['name', 'aria-label']);
  const CSS_BARE = ['#'].concat(['id'].concat(TEST_ATTRS).map((name) => '[' + name + '='));
  const CSS_FOREIGN = ['svg', 'math'];
  const CSS_MAX_ANCESTORS = 6;
  // (css, xpath predicate) for an exact attribute value.
  const cssAttr = (name: string, value: string): [string, string] => (name === 'id' && CSS_IDENT.test(value)
    ? ['#' + value, '@id=' + xpathLiteral(value)]
    : ['[' + name + '=' + cssString(value) + ']', '@' + name + '=' + xpathLiteral(value)]);
  const cssClass = (token: string): [string, string] => ['.' + token,
    "contains(concat(' ', normalize-space(@class), ' '), " + xpathLiteral(' ' + token + ' ') + ')'];
  // The (css, xpath predicate) pairs that describe el itself, best first.
  const cssParts = (el: Element) => {
    const parts: [string, string][] = [];
    for (const name of TEST_ATTRS) {
      const value = el.getAttribute(name);
      if (cUsable(value) && !generated(value)) {
        parts.push(cssAttr(name, value));
        break;
      }
    }
    const ident = el.getAttribute('id');
    if (cUsable(ident) && !generated(ident)) parts.push(cssAttr('id', ident));
    for (const name of CSS_ATTRS) {
      const value = el.getAttribute(name);
      if (!cUsable(value) || (name === 'name' && generated(value))) continue;
      // A query or fragment is often a session or tracking value.
      if (name === 'href' && (value.indexOf('javascript') === 0 || value.indexOf('?') >= 0 || value.indexOf('#') >= 0)) continue;
      parts.push(cssAttr(name, value));
    }
    for (const token of stableClasses(el.getAttribute('class')).slice(0, 3)) {
      if (classOk(token) && CSS_IDENT.test(token)) parts.push(cssClass(token));
    }
    return parts;
  };
  const cssAnchorParts = (anc: Element) => {
    const parts: [string, string][] = [];
    for (const name of CSS_ANCHOR_ATTRS) {
      const value = anc.getAttribute(name);
      if (cUsable(value) && !((name === 'id' || name === 'name') && generated(value))) parts.push(cssAttr(name, value));
    }
    for (const token of stableClasses(anc.getAttribute('class')).slice(0, 2)) {
      if (classOk(token) && CSS_IDENT.test(token)) parts.push(cssClass(token));
    }
    return parts;
  };
  const cssCompound = (tag: string, parts: [string, string][]): [string, string] =>
    [tag + parts.map((p) => p[0]).join(''), tag + parts.map((p) => '[' + p[1] + ']').join('')];
  const cssSelectors = (el: Element, doc: Document, limit?: number) => {
    const most = limit || 3;
    const own = el.localName || '';
    if (!CSS_IDENT.test(own) || ancestors(el).some((a) => CSS_FOREIGN.indexOf(a.localName) >= 0)) return [];
    const tag = own.toLowerCase();
    const out: string[] = [];
    const add = (css: string, xpath: string) => {
      if (out.indexOf(css) < 0) {
        const hits = xpathElements(doc, '//' + xpath);
        if (hits && hits.length === 1 && hits[0] === el) out.push(css);
      }
      return out.length >= most;
    };
    const parts = cssParts(el);
    // An id or a test id is written alone (#save, [data-testid="save"]), as people do;
    // everything else with its tag.
    const bare = parts.slice(0, 2).filter((p) => CSS_BARE.some((start) => p[0].indexOf(start) === 0))
      .map((p): [string, string] => [p[0], '*[' + p[1] + ']']);
    const targets = bare.concat(parts.map((p) => cssCompound(tag, [p])));
    const six = parts.slice(0, 6);
    for (let i = 0; i < six.length; i++) {
      for (let j = i + 1; j < six.length; j++) targets.push(cssCompound(tag, [six[i], six[j]]));
    }
    for (const [css, xpath] of targets) if (add(css, xpath)) return out;
    if (out.length) return out;
    const inner = ([[tag, tag]] as [string, string][]).concat(targets);
    let depth = 0;
    for (const anc of ancestors(el)) {
      const name = anc.localName || '';
      if (depth >= CSS_MAX_ANCESTORS || !CSS_IDENT.test(name)) break;
      for (const part of cssAnchorParts(anc)) {
        const [aCss, aXpath] = cssCompound(name.toLowerCase(), [part]);
        for (const [css, xpath] of inner) if (add(aCss + ' ' + css, aXpath + '//' + xpath)) return out;
      }
      if (out.length) return out;
      depth += 1;
    }
    return out;
  };
  // What xpath matches in the page html: how many elements (count), and the CSS forms of the
  // one it matches, else []; null when the XPath cannot run there.
  const cssForms = (arg: { html: string; xpath: string; limit?: number }) => {
    const doc = parse(arg.html);
    const hits = xpathElements(doc, arg.xpath);
    if (hits === null) return null;
    return { count: hits.length, forms: hits.length === 1 ? cssSelectors(hits[0], doc, arg.limit) : [] };
  };
  // cssForms() of xpath in each of htmls, in one round trip.
  const cssFormsEach = (arg: { htmls: string[]; xpath: string }) =>
    arg.htmls.map((html) => cssForms({ html: html, xpath: arg.xpath }));

  // --- the fingerprint tier in a shadow root (fingerprint.find on its HTML) -----------------
  // A shadow root's elements are scored in its HTML as a document, as the Python package scores
  // them, where an XPath runs: collect()'s elements of role in the page html, with their raw
  // reads and their places among the document's elements (index), which xpathAt() and
  // outsideAt() take.
  const collectHtml = (arg: { html: string; role: string }) => {
    const doc = parse(arg.html);
    const labels = labelIndex(doc);
    const raws: unknown[] = [];
    const indexes: number[] = [];
    allElements(doc).forEach((el, index) => {
      if (roleOf(el) !== arg.role) return;
      if (arg.role !== 'checkbox' && staticallyHidden(el)) return;
      raws.push(rawOf(el, labels));
      indexes.push(index);
    });
    return { raws: raws, indexes: indexes };
  };
  const xpathAt = (arg: { html: string; index: number }) => {
    const el = allElements(parse(arg.html))[arg.index];
    return el ? xpathFor(el) : null;
  };
  const outsideAt = (arg: { html: string; index: number; ids: string[] }) => {
    const el = allElements(parse(arg.html))[arg.index];
    return el ? outside(el, arg.ids) : null;
  };

  // --- lists (lists.py) ------------------------------------------------------------------
  // What a list's container and the elements of the page around it hold that tells them
  // apart: CONTAINER_ATTRS.
  const LIST_CONTAINER_ATTRS = ['id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa'];
  // lists._LEAF: text-level and empty elements, which never hold a list.
  const LIST_LEAF = ['a', 'abbr', 'area', 'audio', 'b', 'base', 'bdi', 'bdo', 'br', 'button', 'canvas', 'cite',
    'code', 'col', 'data', 'dfn', 'em', 'embed', 'hr', 'i', 'iframe', 'img', 'input', 'kbd', 'label', 'link', 'map',
    'mark', 'meta', 'meter', 'object', 'optgroup', 'option', 'output', 'param', 'picture', 'progress', 'q', 's',
    'samp', 'select', 'small', 'source', 'span', 'strong', 'sub', 'sup', 'textarea', 'time', 'track', 'u', 'var',
    'video', 'wbr'];

  // LIST_JS, the read the Python drivers make in the browser when a list lookup matched two or
  // more elements, and with place, LIST_PLACE_JS, which reads the list's label and where it
  // sits too. null unless two or more elements matched.
  const listRaw = (els: Element[], place?: unknown) => {
    if (!els || els.length < 2) return null;
    const top = els[0].getRootNode() as any;
    const shadow = !!top && top.nodeType === 11 && !!top.host;
    const one = recordRaw;
    const shape = (el: Element) => {
      const out: Record<string, number> = {};
      for (const c of Array.from(el.children)) {
        const t = '>' + c.tagName.toLowerCase();
        out[t] = (out[t] || 0) + 1;
      }
      let n = 0;
      for (const d of Array.from(el.querySelectorAll('*'))) {
        const t = d.tagName.toLowerCase();
        out[t] = (out[t] || 0) + 1;
        if (++n >= 500) break;
      }
      return out;
    };
    const last = els.length - 1;
    const picks = Array.from(new Set([0, Math.floor(last / 2), last]));
    const items = picks.map((i) => ({ fp: one([els[i]]), shape: shape(els[i]) }));
    let box: Element | null = els[0].parentElement;
    while (box && !els.every((e) => (box as Element).contains(e))) box = box.parentElement;
    let depth = 0;
    for (let n: Element | null = els[0]; n && n !== box; n = n.parentElement) depth++;
    let container: { tag: string; attrs: Record<string, string> } | null = null;
    if (box && !/^(body|html)$/i.test(box.tagName)) {
      const attrs: Record<string, string> = {};
      for (const a of LIST_CONTAINER_ATTRS) {
        const v = box.getAttribute(a);
        if (v !== null) attrs[a] = v;
      }
      container = { tag: box.tagName.toLowerCase(), attrs: attrs };
    }
    if (place !== true) return { count: els.length, depth: depth, items: items, container: container, shadow: shadow };
    const name = (e: Element) => e.tagName.toLowerCase();
    const unread = /^(template|noscript|script|style)$/;
    const heading = (e: Element) => !unread.test(name(e)) && (/^h[1-6]$/.test(name(e))
      || (e.getAttribute('role') || '').trim().toLowerCase() === 'heading');
    const headingIn = (e: Element) => {
      if (unread.test(name(e))) return null;
      if (heading(e)) return e;
      for (let c = e.lastElementChild; c; c = c.previousElementSibling) if (heading(c)) return c;
      return null;
    };
    const named = (e: Element) => {
      const ids = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
      const text = ids.map((i) => e.ownerDocument.getElementById(i)).filter(Boolean)
        .map((t) => (t as Element).textContent).join(' ');
      if (text.trim()) return text;
      const own = e.getAttribute('aria-label');
      return own && own.trim() ? own : null;
    };
    let label: string | null = '';
    if (box) {
      let node: Element = els[0];
      while (node.parentElement && node.parentElement !== box) node = node.parentElement;
      let holder: Element | null = box;
      search: while (holder) {
        for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
          const h = headingIn(s);
          if (h) { label = h.textContent; break search; }
        }
        if (/^(body|html)$/i.test(holder.tagName)) break;
        const own = named(holder);
        if (own) { label = own; break; }
        node = holder;
        holder = holder.parentElement;
      }
    }
    const test = (e: Element) => {
      for (const a of ['data-testid', 'data-test', 'data-qa']) {
        const v = e.getAttribute(a);
        if (v !== null) return v;
      }
      return '';
    };
    const raw = (e: Element) => {
      const p = e.parentElement;
      return [name(e), e.getAttribute('id') || '', test(e), e.getAttribute('class') || '',
        p ? name(p) : '', p ? p.getAttribute('id') || '' : '', p ? test(p) : '',
        p ? p.getAttribute('class') || '' : ''];
    };
    const chain: string[][] = [];
    const mine = new Set<Element>();
    for (let n: Element | null = box; n && !/^(body|html)$/i.test(n.tagName); n = n.parentElement) {
      chain.push(raw(n));
      mine.add(n);
    }
    for (const e of els) for (let n = e.parentElement; n && n !== box; n = n.parentElement) mine.add(n);
    const prune = /^(template|noscript|script|style|svg|math)$/;
    const leaf = new Set(LIST_LEAF);
    const skip = new Set(els);
    const seen = new Set<string>();
    const others: string[][] = [];
    const said = new Set<string | null>();
    const body = els[0].ownerDocument.body;
    const stack = body ? Array.from(body.children).reverse() : [];
    let read = 0;
    let cut = false;
    while (stack.length) {
      const e = stack.pop() as Element;
      if (skip.has(e) || prune.test(name(e))) continue;
      if (heading(e)) said.add(e.textContent);
      if (!leaf.has(name(e))) {
        const own = named(e);
        if (own) said.add(own);
      }
      if (!leaf.has(name(e)) && !mine.has(e)) {
        if (++read > 10000) { cut = true; break; }
        const r = raw(e);
        const k = r.join('\u0001');
        if (!seen.has(k)) { seen.add(k); others.push(r); }
      }
      for (let i = e.children.length - 1; i >= 0; i--) stack.push(e.children[i]);
    }
    const labels = Array.from(said).slice(0, 200);
    return { count: els.length, depth: depth, items: items, container: container, shadow: shadow, label: label,
             place: { chain: chain, others: others, labels: labels, cut: cut } };
  };

  // --- looking for a list on the captured page (lists.find) --------------------------------
  // lists.find() reads the captured page parsed with lxml; these read it parsed here, and
  // lists.ts scores what they read. Each element is named by its place among the document's
  // elements, which a parse of the same HTML gives again.
  const LIST_UNRENDERED = ['template', 'noscript', 'script', 'style'];
  const LIST_PRUNED = ['template', 'noscript', 'script', 'style', 'svg', 'math'];
  const PY_SPLIT = new RegExp('[' + PY_SPACE + ']+');
  // lists._elements: node's descendant elements in document order, without the inside of
  // <template>, <noscript>, <script> and <style>.
  const listElements = (node: Element): Element[] => {
    const out: Element[] = [];
    const stack = Array.from(node.children).reverse();
    while (stack.length) {
      const el = stack.pop() as Element;
      out.push(el);
      if (LIST_UNRENDERED.indexOf(tagOf(el)) < 0) {
        for (let i = el.children.length - 1; i >= 0; i--) stack.push(el.children[i]);
      }
    }
    return out;
  };
  // lists.shape_of: {'>tag': n} of el's children, and {tag: n} of its first 500 descendants.
  const listShape = (el: Element) => {
    const out: Record<string, number> = {};
    for (const child of Array.from(el.children)) {
      const t = '>' + tagOf(child);
      out[t] = (out[t] || 0) + 1;
    }
    let n = 0;
    for (const node of listElements(el)) {
      const t = tagOf(node);
      out[t] = (out[t] || 0) + 1;
      n += 1;
      if (n >= 500) break;
    }
    return out;
  };
  // lists._heading: h1 to h6, or role="heading".
  const listHeading = (el: Element) => {
    const tag = tagOf(el);
    return LIST_UNRENDERED.indexOf(tag) < 0 && ((tag.length === 2 && tag[0] === 'h' && '123456'.indexOf(tag[1]) >= 0)
      || pyStrip(el.getAttribute('role') || '').toLowerCase() === 'heading');
  };
  // lists._heading_in: el when it is a heading, else the last of its children that is one.
  const listHeadingIn = (el: Element) => {
    if (LIST_UNRENDERED.indexOf(tagOf(el)) >= 0) return null;
    if (listHeading(el)) return el;
    const kids = Array.from(el.children);
    for (let i = kids.length - 1; i >= 0; i--) if (listHeading(kids[i])) return kids[i];
    return null;
  };
  // lists._named: the text of el's aria-labelledby, or its aria-label, or null.
  const listNamed = (el: Element) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(PY_SPLIT).filter(Boolean);
    if (ids.length) {
      const doc = el.ownerDocument;
      const found = ids.map((i) => doc.getElementById(i)).filter(Boolean) as Element[];
      const text = found.map((f) => f.textContent || '').join(' ');
      if (pyStrip(text)) return text;
    }
    const own = el.getAttribute('aria-label');
    return own && pyStrip(own) ? own : null;
  };
  // lists._label_text: going out from the items, the nearest heading before them, or the
  // aria-labelledby or aria-label of box or an element around it, whichever comes first.
  const listLabelText = (first: Element, box: Element) => {
    let node: Element = first;
    while (node.parentElement && node.parentElement !== box) node = node.parentElement;
    let holder: Element | null = box;
    while (holder) {
      for (let s = node.previousElementSibling; s; s = s.previousElementSibling) {
        const found = listHeadingIn(s);
        if (found) return found.textContent || '';
      }
      if (['body', 'html'].indexOf(tagOf(holder)) >= 0) break;
      const own = listNamed(holder);
      if (own) return own;
      node = holder;
      holder = holder.parentElement;
    }
    return '';
  };
  // lists._tokens: the class names of el a group can be made of.
  const listTokens = (el: Element) => stableClasses(el.getAttribute('class'))
    .filter((t) => classOk(t) && CSS_IDENT.test(t));
  // The attributes of an element lists.ts reads: those of a container, and aria-busy.
  const LIST_NODE_ATTRS = LIST_CONTAINER_ATTRS.concat(['aria-busy']);
  // What lists.find() reads of the captured page html for the items' role: the repeated groups
  // (lists.groups), each with its label; every element of the role, with its raw fingerprint
  // read and shape, and the elements around them, with their tag, parent and attributes; the
  // labels of the page (_Places._said_now); and the holders (lists._holders): the elements
  // with the ids of arg.ids, each on the page once, then the one with the recorded
  // container's id or test id.
  const listScan = (arg: { html: string; role: string; ids: string[]; container: { id: string | null;
    test: string | null } }) => {
    const doc = parse(arg.html);
    const root = doc.documentElement;
    const all = allElements(doc);
    const index = new Map<Element, number>();
    all.forEach((el, i) => index.set(el, i));
    const labels = labelIndex(doc);
    const walked = listElements(root);
    const pool = walked.filter((el) => roleOf(el) === arg.role);
    const nodes: Record<number, any> = {};
    const add = (el: Element) => {
      const i = index.get(el) as number;
      if (!nodes[i]) {
        const parent = el.parentElement;
        nodes[i] = { tag: tagOf(el), parent: parent && index.has(parent) ? index.get(parent) : -1,
                     attrs: read(el, LIST_NODE_ATTRS) };
      }
      return nodes[i];
    };
    for (const el of pool) {
      const node = add(el);
      const raw = rawOf(el, labels);
      node.raw = raw;
      node.shape = listShape(el);
      if (raw.field) node.content = el.textContent || '';
      for (let p = el.parentElement; p; p = p.parentElement) add(p);
    }
    // lists.groups(): for each level, members by (container, the tags on the way down), then
    // for each class name two or more of them share, those that have it; each set once.
    const groups: { members: number[]; container: number; steps: string[]; label: string }[] = [];
    const seen = new Set<string>();
    const labelsOf = new Map<string, string>();
    for (let level = 1; level <= 3; level++) {
      const buckets = new Map<string, { container: Element; steps: string[]; members: Element[] }>();
      for (const el of pool) {
        let node: Element | null = el;
        const steps: string[] = [];
        for (let i = 0; i < level && node; i++) {
          steps.unshift(tagOf(node));
          node = node.parentElement;
        }
        if (!node || tagOf(node) === 'html') continue;
        const key = index.get(node) + ' ' + steps.join('/');
        const bucket = buckets.get(key);
        if (bucket) bucket.members.push(el);
        else buckets.set(key, { container: node, steps: steps, members: [el] });
      }
      for (const bucket of Array.from(buckets.values())) {
        const members = bucket.members;
        if (members.length < 2) continue;
        const subsets = [members];
        const counted = new Map<string, Element[]>();
        for (const el of members) {
          for (const token of listTokens(el)) {
            const have = counted.get(token);
            if (have) have.push(el);
            else counted.set(token, [el]);
          }
        }
        for (const subset of Array.from(counted.values())) {
          if (subset.length >= 2 && subset.length < members.length) subsets.push(subset);
        }
        for (const subset of subsets) {
          const ids = subset.map((el) => index.get(el) as number);
          const ident = ids.slice().sort((a, b) => a - b).join(',');
          if (subset.length < 2 || seen.has(ident)) continue;
          seen.add(ident);
          const container = index.get(bucket.container) as number;
          const labelKey = container + ' ' + ids[0];
          if (!labelsOf.has(labelKey)) labelsOf.set(labelKey, listLabelText(subset[0], bucket.container));
          groups.push({ members: ids, container: container, steps: bucket.steps,
                        label: labelsOf.get(labelKey) as string });
        }
      }
    }
    // _Places._said_now: the headings' text, and the aria-labelledby or aria-label of the
    // other elements (not of text-level ones).
    const said: string[] = [];
    for (const el of walked) {
      const tag = tagOf(el);
      if (LIST_PRUNED.indexOf(tag) >= 0) continue;
      if (listHeading(el)) said.push(el.textContent || '');
      if (LIST_LEAF.indexOf(tag) < 0) {
        const own = listNamed(el);
        if (own) said.push(own);
      }
    }
    // lists._holders, in its order: fingerprint._containers, then the container's id and test id.
    const holders: [string, number][] = [];
    const byId = new Map<string, Element[]>();
    for (const el of iterAll(root)) {
      const raw = el.getAttribute('id');
      if (raw === null) continue;
      const value = pySlice(fpNorm(raw), 120);
      if (arg.ids.indexOf(value) < 0) continue;
      const have = byId.get(value);
      if (have) have.push(el);
      else byId.set(value, [el]);
    }
    const set = (name: string, el: Element) => {
      const at = holders.findIndex((h) => h[0] === name);
      if (at >= 0) holders[at] = [name, index.get(el) as number];
      else holders.push([name, index.get(el) as number]);
    };
    byId.forEach((els, value) => {
      if (els.length === 1) set(value, els[0]);
    });
    const wanted: [string, string | null, string[]][] = [['id', arg.container.id, ['id']],
      ['test', arg.container.test, ['data-testid', 'data-test', 'data-qa']]];
    for (const [name, value, names] of wanted) {
      if (!value) continue;
      const hits: Element[] = [];
      for (const el of walked) {
        for (const a of names) if (pySlice(fpNorm(el.getAttribute(a)), 120) === value) hits.push(el);
      }
      if (hits.length === 1) set(name + ':' + value, hits[0]);
    }
    for (const [, i] of holders) add(all[i]);
    return { nodes: nodes, pool: pool.map((el) => index.get(el) as number), groups: groups, said: said,
             holders: holders };
  };
  // lists.group_xpath: [xpath, css] that match the group's members and nothing else in doc, or
  // [null, null]. The XPath has no position. Tried in order: the members' tag and a class name
  // they share, then the container's id, test id, name, aria-label or class and the tags down
  // to the members, with and without that class name, then fingerprint.xpath_for() of the
  // container. css is the same selector in CSS, null for the last form.
  const groupXpath = (doc: Document, members: Element[], container: Element, steps: string[]) => {
    const tag = steps[steps.length - 1];
    if (!CSS_IDENT.test(tag)) return [null, null];
    const rest = members.slice(1).map((m) => listTokens(m));
    const tokens = listTokens(members[0]).filter((t) => rest.every((r) => r.indexOf(t) >= 0));
    const forms: [string, string | null][] = [];
    for (const token of tokens) {
      const [css, pred] = cssClass(token);
      forms.push(['//' + tag + '[' + pred + ']', tag + css]);
    }
    const mid = steps.slice(0, -1);
    const ctag = tagOf(container);
    if (CSS_IDENT.test(ctag) && ctag !== 'html' && ctag !== 'body') {
      const xPath = mid.map((s) => '/' + s).join('');
      const cPath = mid.map((s) => ' > ' + s).join('');
      for (const [aCss, aPred] of cssAnchorParts(container)) {
        const baseX = '//' + ctag + '[' + aPred + ']' + xPath;
        const baseC = ctag + aCss + cPath;
        for (const token of tokens) {
          const [css, pred] = cssClass(token);
          forms.push([baseX + '/' + tag + '[' + pred + ']', baseC + ' > ' + tag + css]);
        }
        forms.push([baseX + '/' + tag, baseC + ' > ' + tag]);
      }
      const anchor = xpathFor(container);
      if (anchor) {
        for (const token of tokens) forms.push([anchor + xPath + '/' + tag + '[' + cssClass(token)[1] + ']', null]);
        forms.push([anchor + xPath + '/' + tag, null]);
      }
    }
    for (const [xpath, css] of forms) {
      const hits = xpathElements(doc, xpath);
      if (hits === null) continue;
      if (hits.length === members.length && hits.every((h) => members.indexOf(h) >= 0)) return [xpath, css];
    }
    return [null, null];
  };
  // lists._alike: [i, n], el is the i-th of the n elements of the page with its tag and the
  // same stable classes, in page order; [1, 1] when it is the only one.
  const listAlike = (el: Element, root: Element) => {
    const tag = tagOf(el);
    const classes = stableClasses(el.getAttribute('class'));
    const same = listElements(root).filter((e) => {
      if (tagOf(e) !== tag) return false;
      const theirs = stableClasses(e.getAttribute('class'));
      return theirs.length === classes.length && theirs.every((c) => classes.indexOf(c) >= 0);
    });
    const at = same.indexOf(el);
    return at < 0 ? [1, 1] : [at + 1, same.length];
  };
  // groupXpath() and listAlike() of a group listScan() read, in the page html.
  const listXpath = (arg: { html: string; members: number[]; container: number; steps: string[] }) => {
    const doc = parse(arg.html);
    const all = allElements(doc);
    const members = arg.members.map((i) => all[i]);
    const container = all[arg.container];
    if (!container || !members.length || members.some((m) => !m)) return { xpath: null, css: null, alike: [1, 1] };
    const [xpath, css] = groupXpath(doc, members, container, arg.steps);
    return { xpath: xpath, css: css, alike: listAlike(container, doc.documentElement) };
  };
  // lists.validate, the static check of a list heal, past its brittle and position checks:
  // ok when xpath matches two or more elements of role in the page html and none of them is
  // inside another. taken says whether a quarantined selector of the key reaches one of them.
  const listValidate = (arg: { xpath: string; html: string; role: string; quarantined?: string[] }) => {
    const doc = parse(arg.html);
    const error = xpathError(doc, arg.xpath);
    if (error) return { ok: false, reason: 'xpath error: ' + error, count: 0, taken: false };
    const matches = xpathElements(doc, arg.xpath) || [];
    const count = matches.length;
    if (count === 0) return { ok: false, reason: 'no match', count: 0, taken: false };
    if (count === 1) return { ok: false, reason: 'it matches 1 element, and a list heal needs 2 or more', count: 1,
                              taken: false };
    for (const el of matches) {
      const got = roleOf(el);
      if (got !== arg.role) return { ok: false, reason: 'role mismatch: got ' + got + ' want ' + arg.role, count: count,
                                     taken: false };
    }
    if (matches.some((el) => ancestors(el).some((a) => matches.indexOf(a) >= 0))) {
      return { ok: false, reason: 'its elements are inside one another', count: count, taken: false };
    }
    let taken = false;
    for (const selector of arg.quarantined || []) {
      const hits = xpathElements(doc, selector);
      if (hits && hits.some((h) => matches.indexOf(h) >= 0)) taken = true;
    }
    return { ok: true, reason: 'ok', count: count, taken: taken };
  };

  // ITEMS_JS, for the live gate of a list: what each match is, and whether it is visible as
  // Playwright counts it (a box with a size, and not visibility: hidden). At most 200.
  const listItems = (els: Element[]) => els.slice(0, 200).map((e) => ({
    tag: e.tagName.toLowerCase(), role: e.getAttribute('role') || '',
    type: e.getAttribute('type') || '',
    visible: e.getClientRects().length > 0
      && (e.ownerDocument.defaultView || window).getComputedStyle(e).visibility !== 'hidden' }));

  return {
    roleOf: roleOf, rawOf: rawOf, recordRaw: recordRaw, staticallyHidden: staticallyHidden, hostsOf: hostsOf,
    placed: placed, shadowHtml: shadowHtml, shadowInventory: shadowInventory, shadowRoots: shadowRoots,
    stableClasses: stableClasses, generatedId: generatedId, generated: generated, xpathFor: xpathFor,
    collect: collect, outside: outside, validate: validate, describe: describe, inventory: inventory,
    cleanHtml: cleanHtml, featuresOf: featuresOf, featureLine: featureLine, fingerprintLine: fingerprintLine,
    candidates: candidates, indexOfMatch: indexOfMatch, robustXpaths: robustXpaths, xpathLiteral: xpathLiteral,
    meaningfulClass: meaningfulClass, cssSelectors: cssSelectors, cssForms: cssForms, cssFormsEach: cssFormsEach,
    collectHtml: collectHtml, xpathAt: xpathAt, outsideAt: outsideAt, listRaw: listRaw, listItems: listItems,
    listScan: listScan, listXpath: listXpath, listValidate: listValidate,
  };
}

export type MendwrightLib = ReturnType<typeof mendwrightLib>;
