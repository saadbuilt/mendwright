// Roles: what kind of element a key points at, in the gate's vocabulary (Python's
// mendwright/roles.py and validation._role_of). The gate gives every element one role:
// 'checkbox', 'radio', 'button', 'link', 'combobox' or 'input' for the controls it knows,
// and the tag name for anything else. A heal is accepted only when the element has the
// key's expected role.

import { CSS, XPATH, classify, stripPrefix } from './selectors';
import { pyRepr } from './py';

export const GATE_ROLES = ['button', 'checkbox', 'radio', 'link', 'input', 'combobox'] as const;
/** role="..." values the gate takes as they are. */
export const GATE_ROLE_ATTRS = ['checkbox', 'button', 'link', 'combobox', 'radio'];
/** ARIA widget roles on a non-native element, mapped to the gate role of the native control. */
export const ARIA_ROLE_ATTRS: Record<string, string> = {
  textbox: 'input', searchbox: 'input', spinbutton: 'input', slider: 'input',
  switch: 'checkbox', listbox: 'combobox',
};

/** ARIA role names (and a few common words) mapped to the gate roles they can mean. */
export const ALIASES: Record<string, string[]> = {
  textbox: ['input', 'textarea'],
  searchbox: ['input'],
  spinbutton: ['input'],
  slider: ['input'],
  listbox: ['combobox'],
  select: ['combobox'],
  dropdown: ['combobox'],
  switch: ['checkbox', 'button'],
  a: ['link'],
  image: ['img'],
  img: ['img'],
  listitem: ['li'],
  list: ['ul', 'ol'],
  heading: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
  paragraph: ['p'],
  navigation: ['nav'],
  row: ['tr'],
  cell: ['td'],
  columnheader: ['th'],
};

// lxml.html.defs.tags plus the newer elements the Python package adds, as it accepts them.
const HTML_TAGS = new Set(('a abbr acronym address applet area article aside audio b base basefont bdi '
  + 'bdo big blink blockquote body br button canvas caption center cite code col colgroup command '
  + 'data datalist dd del details dfn dialog dir div dl dt em embed fieldset figcaption figure font '
  + 'footer form frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html i iframe img input ins '
  + 'isindex kbd keygen label legend li link main map mark marquee math menu meta meter nav '
  + 'noscript object ol optgroup option output p param picture portal pre progress q rp rt ruby s '
  + 'samp script search section select slot small source span strike strong style sub summary sup '
  + 'svg table tbody td template textarea tfoot th thead time title tr track tt u ul var video '
  + 'wbr').split(' '));
const CUSTOM_ELEMENT = /^[a-z][a-z0-9._]*-[a-z0-9._-]*$/;

/** The gate's role for an element known by its tag, role attribute and type attribute. */
export function roleFor(tag: string | null | undefined, roleAttr?: string | null,
                        typeAttr?: string | null): string {
  const role = (roleAttr || '').trim().toLowerCase();
  if (GATE_ROLE_ATTRS.includes(role)) return role;
  const name = (tag || '').toLowerCase();
  const type = (typeAttr || '').trim().toLowerCase();
  if (name === 'input') {
    if (type === 'checkbox') return 'checkbox';
    if (type === 'radio') return 'radio';
    if (type === 'submit' || type === 'button') return 'button';
    return 'input';
  }
  if (name === 'button') return 'button';
  if (name === 'a') return 'link';
  if (name === 'select') return 'combobox';
  if (name !== 'textarea' && role in ARIA_ROLE_ATTRS) return ARIA_ROLE_ATTRS[role];
  return name || 'other';
}

function unknownRole(role: unknown): TypeError {
  return new TypeError('mendwright: unknown role ' + pyRepr(role) + '. Use one of '
    + GATE_ROLES.join(', ') + ', an ARIA role such as textbox, switch or listbox, or an HTML tag '
    + "name such as textarea or div (role: 'h1' for a heading). Other ARIA roles, such as tab or "
    + "menuitem, are not supported: pass the element's tag name, or leave role out.");
}

/** The gate roles a role option can mean (the first is the default), or null for none.
 * Throws TypeError for a value that is not a gate role, a known ARIA alias, an HTML tag or
 * a custom element name. */
export function roleChoices(role: unknown): string[] | null {
  if (role === null || role === undefined) return null;
  if (typeof role !== 'string' || !role.trim()) throw unknownRole(role);
  const name = role.trim().toLowerCase();
  if ((GATE_ROLES as readonly string[]).includes(name)) return [name];
  if (name in ALIASES) return [...ALIASES[name]];
  if (HTML_TAGS.has(name) || CUSTOM_ELEMENT.test(name)) return [roleFor(name)];
  throw unknownRole(role);
}

// --- guessing from the selector text -----------------------------------------------------

const ATTR_EQ = /@(role|type)\s*=\s*(['"])([^'"]*)\2/g;
const CSS_ATTR = /\[\s*(role|type)\s*=\s*(['"]?)([^'"\]]*)\2\s*\]/g;
const XPATH_NAME = /^[A-Za-z][\w.-]*/;
const CSS_NAME = /^[A-Za-z][\w-]*/;

function topLevelSplit(text: string, sep: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let start = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (quote) {
      if (ch === quote) quote = null;
    } else if (depth === 0 && text.startsWith(sep, i)) {
      parts.push(text.slice(start, i));
      i += sep.length;
      start = i;
      continue;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '[' || ch === '(') {
      depth += 1;
    } else if (ch === ']' || ch === ')') {
      depth = Math.max(0, depth - 1);
    }
    i += 1;
  }
  parts.push(text.slice(start));
  return parts;
}

function predicates(step: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let start: number | null = null;
  for (let i = 0; i < step.length; i++) {
    const ch = step[i];
    if (quote) {
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '[') {
      if (depth === 0) start = i + 1;
      depth += 1;
    } else if (ch === ']') {
      depth -= 1;
      if (depth === 0 && start !== null) out.push(step.slice(start, i));
    }
  }
  return out;
}

function fromParts(tag: string | null, roleAttr: string | undefined, typeAttr: string | undefined): string {
  const role = (roleAttr || '').trim().toLowerCase() || null;
  const type = (typeAttr || '').trim().toLowerCase() || null;
  if (tag === null || tag === '*' || tag === 'node()') {
    if (role && GATE_ROLE_ATTRS.includes(role)) return role;
    if (role && role in ARIA_ROLE_ATTRS) return ARIA_ROLE_ATTRS[role];
    if (type && ['checkbox', 'radio', 'submit', 'button'].includes(type)) return roleFor('input', null, type);
    return 'unknown';
  }
  return roleFor(tag.toLowerCase(), role, type);
}

function inferXpath(xpath: string): string {
  const text = xpath.trim();
  if (topLevelSplit(text, '|').length > 1) return 'unknown';
  if (text.startsWith('(')) {
    const inner = topLevelSplit(text.slice(1), ')');
    if (inner.length >= 2 && inner.slice(1).every((p) => !p.trim() || p.trim().startsWith('['))) {
      return inferXpath(inner[0]);
    }
    return 'unknown';
  }
  const steps = topLevelSplit(text, '/').filter((s) => s.trim());
  if (!steps.length) return 'unknown';
  let step = steps[steps.length - 1].trim();
  const axis = step.indexOf('::');
  if (axis >= 0) {
    const name = step.slice(0, axis).trim();
    if (!['self', 'child', 'descendant', 'descendant-or-self'].includes(name)) return 'unknown';
    step = step.slice(axis + 2);
  }
  const match = XPATH_NAME.exec(step);
  const tag = step.startsWith('*') ? '*' : match ? match[0] : null;
  if (tag === null || ['text', 'comment', 'processing-instruction'].includes(tag)) return 'unknown';
  const attrs: Record<string, string> = {};
  for (const predicate of predicates(step)) {
    if (predicate.includes('[') || predicate.includes('/')) continue;
    for (const m of predicate.matchAll(ATTR_EQ)) {
      if (!(m[1] in attrs)) attrs[m[1]] = m[3];
    }
  }
  return fromParts(tag, attrs.role, attrs.type);
}

function inferCss(css: string): string {
  const compounds = css.trim().split(/\s*[>+~]\s*|\s+/);
  const last = compounds.length ? compounds[compounds.length - 1] : '';
  if (!last || css.includes(',')) return 'unknown';
  const match = CSS_NAME.exec(last);
  const tag = match ? match[0].toLowerCase() : '*';
  if (tag !== '*' && !HTML_TAGS.has(tag) && !CUSTOM_ELEMENT.test(tag)) return 'unknown';
  const attrs: Record<string, string> = {};
  for (const m of last.matchAll(CSS_ATTR)) {
    if (!(m[1] in attrs)) attrs[m[1]] = m[3];
  }
  return fromParts(tag, attrs.role, attrs.type);
}

/** Guess the gate role from the selector text alone, or 'unknown'. */
export function inferRole(selector: unknown): string {
  if (typeof selector !== 'string' || !selector.trim()) return 'unknown';
  try {
    const kind = classify(selector);
    const body = stripPrefix(selector);
    if (kind === XPATH) return inferXpath(body);
    if (kind === CSS) return inferCss(body);
  } catch {
    return 'unknown';
  }
  return 'unknown';
}
