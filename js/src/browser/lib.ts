// The code mendwright runs inside the page. mendwrightLib() is sent to the browser as its
// source text (see dom.ts), so it must stay self-contained: no imports, no references to
// anything outside the function, and no syntax the TypeScript compiler would rewrite with
// a helper. It returns the helpers by name.
//
// It ports the parts of the Python package that read a document with lxml:
// validation._role_of, fingerprint.raw_of_element / FINGERPRINT_JS / xpath_for /
// statically_hidden, gate's static check (validation.validate), capture.clean_html, the
// driver's describe() and inventory(), and the candidate list and XPaths of the local
// selector model (candidates.py). Text rules follow the Python ones: lengths and cuts count
// code points, and str.strip() strips what Python's strips.

/* eslint-disable */
export function mendwrightLib() {
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
    return new DOMParser().parseFromString(text, 'text/html');
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
  // FINGERPRINT_JS, the read the Python drivers make in the browser when a selector
  // matched: null unless exactly one element matched.
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
          for (const l of Array.from(el.ownerDocument.getElementsByTagName('label'))) {
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
    return { tag: tag, attrs: read(el, ATTRS), field: field, text: field ? null : el.textContent, label: label, ancestors: chain };
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
  const inventory = (limit: number) => {
    const out: Record<string, string>[] = [];
    const nodes = document.querySelectorAll('button, a, input, select, textarea, [role]');
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

  return {
    roleOf: roleOf, rawOf: rawOf, recordRaw: recordRaw, staticallyHidden: staticallyHidden,
    stableClasses: stableClasses, generatedId: generatedId, generated: generated, xpathFor: xpathFor,
    collect: collect, outside: outside, validate: validate, describe: describe, inventory: inventory,
    cleanHtml: cleanHtml, featuresOf: featuresOf, featureLine: featureLine, fingerprintLine: fingerprintLine,
    candidates: candidates, indexOfMatch: indexOfMatch, robustXpaths: robustXpaths, xpathLiteral: xpathLiteral,
    meaningfulClass: meaningfulClass,
  };
}

export type MendwrightLib = ReturnType<typeof mendwrightLib>;
