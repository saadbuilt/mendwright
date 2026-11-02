// The scripts open({ dismiss: true }) runs in the page, to see what covers an element and
// what a popup over it holds (the Python package's mendwright/drivers/base.py: _POPUP_HELPERS,
// POPUP_PROBE_JS, POPUP_ROOT_JS, POPUP_CONTROLS_JS, POPUP_CONTROL_JS and POPUP_FOCUS_JS).
// They only report what the page shows; dismiss.ts decides. Each is a function expression
// with its helpers inside it, so it is self-contained, and it is the Python package's
// character for character: a test compares them. The driver turns each into a function and
// Playwright sends that to the page as its source text.

/* eslint-disable */
const HELPERS = String.raw`
  // An element's parent, out of a shadow root to its host.
  const up = (n) => n.parentElement || (n.parentNode && n.parentNode.host) || null;
  // Whether a is b or holds it, across shadow roots.
  const holds = (a, b) => { for (let n = b; n; n = up(n)) { if (n === a) return true; } return false; };
  const style = (el) => (el.ownerDocument.defaultView || window).getComputedStyle(el);
  const shown = (el) => {
    if (!el || !el.isConnected) return false;
    const s = style(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
  const clean = (s, n) => String(s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  // What is on top at the middle of t: {hit, w, h}, or {why} when t is not covered or
  // cannot be: it has no size, is hidden, or its middle is outside the window.
  const cover = (t) => {
    const win = t.ownerDocument.defaultView || window;
    const w = win.innerWidth, h = win.innerHeight;
    const r = t.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0) || !shown(t)) return {why: 'hidden'};
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= w || y >= h) return {why: 'outside'};
    let hit = t.ownerDocument.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(x, y);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    if (!hit || holds(t, hit) || holds(hit, t)) return {why: 'free'};
    return {hit: hit, w: w, h: h};
  };
  // What may be a button: mendwright.dismiss.is_control decides which of them count.
  const control = (el) => el.tagName === 'BUTTON' || el.tagName === 'A' || el.tagName === 'INPUT'
    || (el.getAttribute('role') || '').trim().toLowerCase() === 'button';
  // The controls of a popup in page order, open shadow roots included.
  const controls = (root) => {
    const out = [];
    const walk = (scope) => {
      for (const el of scope.querySelectorAll('*')) {
        if (control(el) && shown(el)) out.push(el);
        if (el.shadowRoot) walk(el.shadowRoot);
        if (out.length >= 50) return;
      }
    };
    if (root.shadowRoot) walk(root.shadowRoot);
    walk(root);
    return out.slice(0, 50);
  };
  // A popup's own text: its text outside its controls (open shadow roots included), as
  // mendwright.dismiss.setting reads it: "Close your account?" around [Cancel] [Close].
  const prose = (root) => {
    const parts = [];
    let size = 0;
    const walk = (scope) => {
      for (const node of scope.childNodes) {
        if (size > 3000) return;
        if (node.nodeType === 3) { parts.push(node.data); size += node.data.length; continue; }
        if (node.nodeType !== 1 || control(node)
            || /^(SCRIPT|STYLE|TEMPLATE|NOSCRIPT)$/.test(node.tagName)) continue;
        if (style(node).display === 'none') continue;
        if (node.shadowRoot) walk(node.shadowRoot);
        walk(node);
      }
    };
    if (root.shadowRoot) walk(root.shadowRoot);
    walk(root);
    return clean(parts.join(' '), 1500);
  };
  // A link that goes somewhere: an href other than '#' and javascript: code that does
  // nothing (javascript:, javascript:void(0), javascript:;), as dismiss.goes_nowhere says.
  const real = (a) => {
    if (!a.hasAttribute('href')) return false;
    const href = a.getAttribute('href').trim().toLowerCase();
    if (href === '#') return false;
    if (!href.startsWith('javascript:')) return true;
    const code = href.slice('javascript:'.length).replace(/\s+/g, '').replace(/;+$/, '');
    return !['', 'void(0)', 'void0', 'void(null)'].includes(code);
  };
  // Whether a click on el may follow a link: one holds el, or el holds one.
  const linked = (el) => {
    for (let n = up(el); n; n = up(n)) { if (n.tagName === 'A' && real(n)) return true; }
    for (const a of el.querySelectorAll('a[href]')) { if (real(a)) return true; }
    return false;
  };
  // Whether el is a <label> or in one: a click on it may check the label's box.
  const labelled = (el) => {
    for (let n = el; n; n = up(n)) { if (n.tagName === 'LABEL') return true; }
    return false;
  };
  const FIELDS = 'input:not([type=button]):not([type=submit]):not([type=reset])'
    + ':not([type=hidden]):not([type=image]), select, textarea, [contenteditable]';
  // Whether a click on el submits or resets its form. A form with method="dialog" counts
  // too: it fires its submit event, and the page's handlers of it run.
  const submits = (el) => {
    const form = el.form;
    if (!form || !(el.tagName === 'BUTTON' || el.tagName === 'INPUT')) return false;
    const type = String(el.type || '').toLowerCase();
    return type === 'reset' || type === 'submit' || type === 'image';
  };
  // The text of the elements el's aria-labelledby names: its accessible name.
  const labelledby = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    const scope = el.getRootNode();
    return ids.map((id) => {
      const n = (scope && scope.getElementById ? scope.getElementById(id) : null)
        || el.ownerDocument.getElementById(id);
      return n ? n.textContent : '';
    }).join(' ');
  };
  // A control's facts. text is what it shows (innerText leaves out the text hidden with
  // display: none), all its whole text. The value attribute is read for button-like inputs
  // only, whose value is their label: a text field's can hold what was typed.
  const facts = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').trim().toLowerCase();
    return {
      tag: tag, type: type, role: (el.getAttribute('role') || '').trim().toLowerCase(),
      href: tag === 'a' ? el.getAttribute('href') : null,
      text: clean(typeof el.innerText === 'string' ? el.innerText : el.textContent, 200),
      all: clean(el.textContent, 200),
      value: tag === 'input' && ['button', 'submit', 'reset'].includes(type)
        ? clean(el.getAttribute('value'), 200) : '',
      aria: clean(el.getAttribute('aria-label'), 200),
      title: clean(el.getAttribute('title'), 200),
      disabled: el.disabled === true
        || (el.getAttribute('aria-disabled') || '').trim().toLowerCase() === 'true',
      link: linked(el), label: labelled(el), field: el.querySelector(FIELDS) !== null,
      submits: submits(el), labelledby: clean(labelledby(el), 200),
      // A radio, checkbox, switch, option or toggle: a click on it chooses something.
      checked: ['aria-checked', 'aria-pressed', 'aria-selected'].some((a) => el.hasAttribute(a)),
    };
  };
`;

// For a Locator's evaluateAll: null unless one element matched. Else {covered, gone} and,
// when covered, chain: the element on top and each container up to (not including) body,
// with what dismiss.popupOf reads; when not covered, why (see cover above). gone says
// whether arg.known (a popup's root from POPUP_ROOT_JS) was removed or hidden. With
// arg.scroll, an element whose middle is outside the window is scrolled to the middle of it
// first, as the action would scroll it.
export const POPUP_PROBE_JS = String.raw`(els, arg) => {`
  + HELPERS + String.raw`
  if (!els || els.length !== 1) return null;
  const t = els[0];
  const known = arg && arg.known ? arg.known : null;
  const gone = known ? !shown(known) : null;
  let c = cover(t);
  if (c.why === 'outside' && arg && arg.scroll) {
    try { t.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'}); }
    catch (e) { t.scrollIntoView({block: 'center', inline: 'center'}); }
    c = cover(t);
  }
  if (!c.hit) return {covered: false, why: c.why, gone: gone};
  const doc = c.hit.ownerDocument;
  const chain = [];
  for (let n = c.hit; n && n.nodeType === 1 && n !== doc.body && n !== doc.documentElement
       && chain.length < 60; n = up(n)) {
    const s = style(n), r = n.getBoundingClientRect();
    const iw = Math.max(0, Math.min(r.right, c.w) - Math.max(r.left, 0));
    const ih = Math.max(0, Math.min(r.bottom, c.h) - Math.max(r.top, 0));
    const role = (n.getAttribute('role') || '').trim().toLowerCase();
    const modal = (n.getAttribute('aria-modal') || '').trim().toLowerCase() === 'true';
    const open = n.tagName === 'DIALOG' && n.open === true;
    const inside = holds(n, t);
    const named = !inside && (role === 'dialog' || role === 'alertdialog' || modal || open
                              || s.position === 'fixed' || s.position === 'sticky');
    chain.push({tag: n.tagName.toLowerCase(), id: clean(n.getAttribute('id'), 100),
                cls: clean(n.getAttribute('class'), 200), role: role, modal: modal, open: open,
                pos: s.position, area: c.w * c.h ? (iw * ih) / (c.w * c.h) : 0, holds: inside,
                events: s.pointerEvents !== 'none',
                aria: named ? clean(n.getAttribute('aria-label'), 200) : '',
                text: named ? clean(n.innerText || n.textContent, 1500) : '',
                prose: named ? prose(n) : ''});
  }
  return {covered: true, gone: gone, chain: chain};
}`;

// For a Locator's evaluateHandle (element first): the element arg.depth steps up from the
// one on top at the middle of el, when its tag and id are still arg.tag and arg.id; else null.
export const POPUP_ROOT_JS = String.raw`(t, arg) => {`
  + HELPERS + String.raw`
  const c = cover(t);
  if (!c.hit) return null;
  let n = c.hit;
  for (let i = 0; n && i < arg.depth; i++) n = up(n);
  if (!n || n.tagName.toLowerCase() !== arg.tag || clean(n.getAttribute('id'), 100) !== arg.id) return null;
  return n;
}`;

// For a root's evaluate: the facts of each control in it, in page order.
export const POPUP_CONTROLS_JS = String.raw`(root) => {`
  + HELPERS + String.raw`
  return controls(root).map(facts);
}`;

// For a root's evaluateHandle: control number arg.index, when every fact of it is still
// what arg.facts says (dismiss.expectControl); else null.
export const POPUP_CONTROL_JS = String.raw`(root, arg) => {`
  + HELPERS + String.raw`
  const el = controls(root)[arg.index];
  const want = (arg && arg.facts) || {};
  if (!el || Object.keys(want).length === 0) return null;
  const f = facts(el);
  const same = (a, b) => JSON.stringify(a === undefined ? null : a)
    === JSON.stringify(b === undefined ? null : b);
  return Object.keys(want).every((k) => same(f[k], want[k])) ? el : null;
}`;

// For a root's evaluate, before Escape is pressed: true when Escape would reach the popup,
// because the keyboard focus (through open shadow roots) is on the body, or nowhere, or
// inside the root, in a top-level page. In an iframe it is false: the key goes to the
// focused element of the page, which may be in the page around the iframe.
export const POPUP_FOCUS_JS = String.raw`(root) => {`
  + HELPERS + String.raw`
  const doc = root.ownerDocument;
  const win = doc.defaultView || window;
  if (win !== win.top) return false;
  let a = doc.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  return !a || a === doc.body || a === doc.documentElement || holds(root, a);
}`;
