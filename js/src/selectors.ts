// Selector kinds, as the Python package reads them (mendwright/selectors.py): XPath, CSS,
// or a Playwright engine selector. A string written as a get_by_* call is how the Python
// package names a heal.page.get_by_*() call in the store, so it is never a selector or a
// key here.
//
// - "xpath=...", or anything starting with "/", "(", "./" or ".." is XPath.
// - "css=..." is CSS.
// - "text=", "role=", "data-testid=", "internal:..." and any other "name=" prefix is a
//   Playwright selector engine, and so is a chain such as "//form >> text=Save".
// - Anything else ("#save", "form button.primary") is CSS.
//
// checkSyntax() catches what can be caught without a browser. XPath and CSS are checked by
// the browser the first time they run, and an error there becomes the same InvalidSelector.

import { InvalidSelector } from './errors';
import { pyRepr } from './py';

export const XPATH = 'xpath';
export const CSS = 'css';
export const ENGINE = 'engine';
export const GET_BY = 'get_by';
export type SelectorKind = typeof XPATH | typeof CSS | typeof ENGINE | typeof GET_BY;

const XPATH_STARTS = ['/', '(', './', '..'];
const ENGINE_PREFIX = /^(?:internal:|_?[A-Za-z][\w-]*=)/;
const ENGINE_PREFIX_ONLY = /^(?:internal:|_?[A-Za-z][\w-]*=)$/;
const GET_BY_CALL = /^get_by_(?:role|text|label|placeholder|alt_text|title|test_id)\s*\([\s\S]*\)$/;

const TYPE_HINT = "pass a selector string such as '#save' or \"role=button[name='Save']\"; "
  + "Locator objects can't be stored";

/** True for a string written as a get_by_* call, such as get_by_role("button", name="Go"):
 * the Python package's key for a heal.page.get_by_*() call. */
export function isGetByCall(text: unknown): boolean {
  return typeof text === 'string' && text.length <= 1000 && GET_BY_CALL.test(text.trim());
}

function requireString(selector: unknown): asserts selector is string {
  if (typeof selector !== 'string') {
    const what = selector === null ? 'null' : Array.isArray(selector) ? 'an array'
      : typeof selector === 'object' ? (selector as object).constructor?.name ?? 'an object' : typeof selector;
    throw new TypeError('mendwright: got ' + what + ' where a selector was expected; ' + TYPE_HINT + '.');
  }
}

function isChain(text: string): boolean {
  let quote: string | null = null;
  let depth = 0;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === '\\') {
        i += 1;
        continue;
      }
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '[' || ch === '(') {
      depth += 1;
    } else if (ch === ']' || ch === ')') {
      depth = Math.max(0, depth - 1);
    } else if (depth === 0 && text.startsWith(' >> ', i)) {
      return true;
    }
  }
  return false;
}

/** 'xpath', 'css', 'engine' or 'get_by' for a selector string. */
export function classify(selector: unknown): SelectorKind {
  requireString(selector);
  const text = selector.trim();
  if (isGetByCall(text)) return GET_BY;
  if (isChain(text)) return ENGINE;
  if (text.startsWith('xpath=')) return XPATH;
  if (XPATH_STARTS.some((start) => text.startsWith(start))) return XPATH;
  if (text.startsWith('css=')) return CSS;
  if (ENGINE_PREFIX.test(text)) return ENGINE;
  return CSS;
}

/** The selector without a leading 'xpath=' or 'css=' and surrounding whitespace. */
export function stripPrefix(selector: string): string {
  const kind = classify(selector);
  const text = selector.trim();
  if (kind === XPATH && text.startsWith('xpath=')) return text.slice('xpath='.length).trim();
  if (kind === CSS && text.startsWith('css=')) return text.slice('css='.length).trim();
  return text;
}

/** The spelling mendwright stores a selector under: trimmed, and without an 'xpath=' or
 * 'css=' prefix that does not change its kind. Python's selectors.normalize(). */
export function normalize(selector: string): string {
  const kind = classify(selector);
  const body = stripPrefix(selector);
  if (kind === ENGINE || kind === GET_BY) return body;
  if (body && classify(body) === kind) return body;
  return (kind === XPATH ? 'xpath=' : 'css=') + body;
}

/** Check what can be checked without a browser, and return the selector's kind. A
 * non-string raises TypeError; an empty selector, one that is only a prefix, or a get_by_*
 * call raises InvalidSelector. */
export function checkSyntax(selector: unknown): SelectorKind {
  requireString(selector);
  if (!selector.trim()) {
    throw InvalidSelector.build(selector, 'the selector is empty.',
      "Pass an XPath such as \"//button[@id='save']\" or a CSS selector such as '#save'.");
  }
  const kind = classify(selector);
  if (kind === GET_BY) {
    throw InvalidSelector.build(selector, pyRepr(selector) + ' is a get_by_* call, not a selector.',
      'mendwright for TypeScript does not heal get_by_* calls yet. Pass an XPath, CSS or '
      + 'Playwright selector.');
  }
  const body = stripPrefix(selector);
  if (!body || (kind === ENGINE && ENGINE_PREFIX_ONLY.test(body))) {
    throw InvalidSelector.build(selector, pyRepr(selector) + ' has a prefix but nothing after it.',
      'Add the expression after the prefix, for example "css=#save" or "text=Save".');
  }
  return kind;
}

/** The selector as the Python package saves it for a lookup inside an iframe: with its
 * 'xpath=' or 'css=' prefix written out, so it never shares a store row with the same text
 * on the page. null for an engine selector or a get_by_* call. */
export function frameSpelling(selector: string): string | null {
  const kind = classify(selector);
  if (kind !== XPATH && kind !== CSS) return null;
  const body = stripPrefix(selector);
  let alt = (kind === XPATH ? 'xpath=' : 'css=') + body;
  if (alt === normalize(selector)) {
    if (kind !== XPATH) return null;
    alt = 'xpath=(' + body + ')';
  }
  return alt;
}

/** The string Playwright's page.locator() takes for a stored selector: XPath as
 * 'xpath=...', CSS as 'css=...', an engine selector as it is. */
export function playwrightSelector(selector: string): string {
  const kind = checkSyntax(selector);
  if (kind === XPATH) return 'xpath=' + stripPrefix(selector);
  if (kind === CSS) return 'css=' + stripPrefix(selector);
  return selector.trim();
}

// A position in an XPath: a predicate that is only a number ([2], [ 2 ], [(2)], [1+1]) or
// only number(...), or any use of position() or last(). Quoted strings are ignored.
const POSITIONAL = /\[[\s\d.+\-*()]*\d[\s\d.+\-*()]*\]|\[\s*number\s*\([^\]]*\)\s*\]|\bposition\s*\(|\blast\s*\(/;
const QUOTED = /'[^']*'|"[^"]*"/g;

/** True when an XPath depends on an element's position (Python's fit.has_position). */
export function hasPosition(xpath: string): boolean {
  return POSITIONAL.test((xpath || '').replace(QUOTED, "''"));
}

const PLAIN_STEP = /^[A-Za-z][\w:-]*(\[\d+\])?$/;

/** True for a pure absolute-index path such as /html[1]/body[1]/div[3]/input[1]. */
export function isBrittle(xpath: string): boolean {
  const segments = xpath.trim().split(/\/+/).filter(Boolean);
  if (!segments.length) return false;
  return segments.every((s) => PLAIN_STEP.test(s)) && segments.some((s) => s.includes('['));
}
