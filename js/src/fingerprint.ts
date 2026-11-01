// Fingerprint healing: the first tier, which needs no model. A port of the Python
// package's mendwright/fingerprint.py, with the same stored shape, weights, thresholds and
// text rules, so a fingerprint either package recorded scores the same in both.
//
// Each time a key's selector matches, mendwright records a fingerprint of the element: its
// tag, the attributes that tend to survive a redesign, its class tokens that do not look
// generated, its visible text (or its label, for a form field) and the tag, id, classes and
// role of up to 4 ancestors. Input values are never read.
//
// When the key's selectors stop matching, every element of the page that has the key's
// role is scored against the fingerprint. A heal is proposed only when the best score
// reaches THRESHOLD, the best element carries a strong identity signal, no other visible
// element comes within MARGIN of it, it is inside every recorded ancestor that still has
// its id on the page, and a unique XPath without positions can be built for it. The XPath
// then goes through the same gate as a model's proposal.
//
// The browser reads elements into the raw shape below (see browser/lib.ts); fromRaw() is
// the one place that normalizes it, here as in Python.

import { ratio as sequenceRatio } from './difflib';
import { isDigits, pyLen, pySlice, pyStrip, pySum, urlsplit } from './py';

/** Version of the stored shape. A fingerprint with another version is ignored. */
export const VERSION = 1;
export const THRESHOLD = 0.40;
export const MARGIN = 0.12;
export const ANCESTORS = 4;
export const TEXT_LIMIT = 80;
export const VALUE_LIMIT = 120;

export const ATTRS = ['id', 'name', 'type', 'role', 'class', 'data-testid', 'data-test', 'data-qa',
  'aria-label', 'placeholder', 'title', 'alt', 'href', 'autocomplete'];
export const ANCESTOR_ATTRS = ['id', 'class', 'role'];
export const TEST_ATTRS = ['data-testid', 'data-test', 'data-qa'];
const LOWER = ['type', 'role', 'autocomplete'];

/** What the browser reads from one element (Python's FINGERPRINT_JS / raw_of_element). */
export interface RawElement {
  tag: string;
  attrs: Record<string, string>;
  field: boolean;
  text: string | null;
  label: string | null;
  ancestors: { tag: string; attrs: Record<string, string> }[];
}

export interface AncestorPrint {
  tag: string;
  id?: string;
  classes?: string[];
  role?: string;
}

/** A stored fingerprint: the shape the store keeps under "fingerprint". */
export interface Fingerprint {
  v: number;
  tag: string;
  attrs?: Record<string, string>;
  classes?: string[];
  text?: string;
  label?: string;
  ancestors?: AncestorPrint[];
}

// XPath's normalize-space() collapses only these, so a non-breaking space stays text.
const SPACE = /[ \t\n\r\f]+/g;
const LONGNUM = /(?<!\p{Nd})\p{Nd}{12,19}(?!\p{Nd})/gu;

export function norm(text: unknown): string {
  if (typeof text !== 'string') return '';
  return pyStrip(text.replace(SPACE, ' '));
}

/** Visible text as stored: whitespace collapsed, long digit runs masked, cut short. */
export function storedText(text: unknown): string {
  return pySlice(norm(text).replace(LONGNUM, '#'), TEXT_LIMIT);
}

// --- class tokens ------------------------------------------------------------------------

const GENERATED_PREFIX = /^(?:css|sc|jsx|svelte|astro|emotion)-/i;
const MODULE_HASH = /(?:__|_)[A-Za-z0-9-]{5,}$/;
const STATE = new Set(['active', 'is-active', 'selected', 'is-selected', 'focus', 'focused',
  'hover', 'disabled', 'is-disabled', 'open', 'is-open', 'show', 'shown', 'collapsed',
  'expanded', 'visible', 'hidden', 'checked', 'current']);

function hashy(segment: string): boolean {
  const chars = Array.from(segment);
  if (chars.length < 4) return false;
  const hasDigit = chars.some((c) => /\p{Nd}/u.test(c));
  const hasAlpha = chars.some((c) => /\p{L}/u.test(c));
  if (hasDigit && hasAlpha) return true;
  return chars.slice(1).some((c) => /\p{Lu}/u.test(c)) && chars.some((c) => /\p{Ll}/u.test(c));
}

/** The class tokens of a class attribute that are likely to survive a rebuild. */
export function stableClasses(value: unknown): string[] {
  if (typeof value !== 'string') return [];
  const out: string[] = [];
  for (const token of value.split(/\s+/).filter(Boolean)) {
    const low = token.toLowerCase();
    if (STATE.has(low) || low.startsWith('ng-') || GENERATED_PREFIX.test(token)) continue;
    if (token.includes('__') && MODULE_HASH.test(token)) continue;
    if (token.split(/[-_]+/).some(hashy)) continue;
    if (!out.includes(token)) out.push(token);
  }
  return out.slice(0, 8);
}

/** True for an id that a framework generated (:r3:, mui-generated-8842, ember123). */
export function generatedId(value: unknown): boolean {
  if (typeof value !== 'string' || !value) return true;
  if (value.includes(':') || /\p{Nd}{3,}/u.test(value)) return true;
  return value.split(/[-_.]+/).some((part) => {
    const chars = Array.from(part);
    return chars.length >= 5 && chars.some((c) => /\p{Nd}/u.test(c)) && chars.some((c) => /\p{L}/u.test(c));
  });
}

/** The path of a link target, without scheme, host, query, fragment or trailing slash. */
export function hrefPath(value: unknown): string {
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text || ['#', 'javascript:', 'mailto:', 'tel:', 'data:'].some((p) => text.startsWith(p))) return '';
  let path: string;
  try {
    path = urlsplit(text).path;
  } catch {
    return '';
  }
  if (pyLen(path) > 1) path = path.replace(/\/+$/, '');
  return pySlice(path, VALUE_LIMIT);
}

// --- normalizing ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** A stored fingerprint from the browser's raw read, or null when raw is not that shape. */
export function fromRaw(raw: unknown): Fingerprint | null {
  if (!isRecord(raw) || typeof raw.tag !== 'string' || !raw.tag) return null;
  const attrs = raw.attrs;
  const ancestors = 'ancestors' in raw ? raw.ancestors : [];
  if (!isRecord(attrs) || !Array.isArray(ancestors)) return null;
  const fp: Fingerprint = { v: VERSION, tag: raw.tag.trim().toLowerCase() };
  const own: Record<string, string> = {};
  for (const name of ATTRS) {
    const value = attrs[name];
    if (name === 'class' || typeof value !== 'string') continue;
    let text = name === 'href' ? hrefPath(value) : pySlice(norm(value), VALUE_LIMIT);
    if (LOWER.includes(name)) text = text.toLowerCase();
    if (text) own[name] = text;
  }
  if (Object.keys(own).length) fp.attrs = own;
  const classes = stableClasses(attrs.class);
  if (classes.length) fp.classes = classes;
  if (!raw.field) {
    const text = storedText(raw.text);
    if (text) fp.text = text;
  }
  const label = storedText(raw.label);
  if (label) fp.label = label;
  const chain: AncestorPrint[] = [];
  for (const item of (ancestors as unknown[]).slice(0, ANCESTORS)) {
    if (!isRecord(item) || typeof item.tag !== 'string') return null;
    const a = isRecord(item.attrs) ? item.attrs : {};
    const node: AncestorPrint = { tag: item.tag.trim().toLowerCase() };
    if (typeof a.id === 'string' && norm(a.id)) node.id = pySlice(norm(a.id), VALUE_LIMIT);
    const ancestorClasses = stableClasses(a.class);
    if (ancestorClasses.length) node.classes = ancestorClasses;
    if (typeof a.role === 'string' && norm(a.role)) node.role = norm(a.role).toLowerCase();
    chain.push(node);
  }
  if (chain.length) fp.ancestors = chain;
  return fp;
}

/** True when fp is a stored fingerprint this version can score. */
export function valid(fp: unknown): fp is Fingerprint {
  return isRecord(fp) && fp.v === VERSION && typeof fp.tag === 'string' && Boolean(fp.tag);
}

// --- scoring -------------------------------------------------------------------------------

export const WEIGHTS: Record<string, number> = {
  tag: 1.0, id: 3.0, test: 3.0, name: 2.0, 'aria-label': 2.0, placeholder: 1.5,
  title: 1.0, alt: 1.0, href: 2.0, autocomplete: 2.0, type: 0.5, role: 0.5,
  classes: 1.0, text: 3.0, label: 3.0, ancestors: 2.0,
};
const RATIO_ATTRS = ['placeholder', 'title', 'alt'];
const EXACT_ATTRS = ['type', 'role'];
export const SAME_TEXT = 0.9;
export const LONG_SAME = 0.8;
export const LONG_TEXT = 16;
const VAGUE_AUTOCOMPLETE = ['on', 'off', 'nope', 'false', 'true', 'none'];
const TOKEN = /[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\p{Nd}+/gu;
const WORD = /[\p{L}\p{Nl}\p{No}]+|\p{Nd}+/gu;
const FILLER = new Set(['a', 'an', 'the', 'this', 'that', 'these', 'your', 'my', 'our', 'please']);

/** How alike two strings are, ignoring case (difflib's ratio). */
export function textRatio(a: string | undefined | null, b: string | undefined | null): number {
  if (!a || !b) return 0.0;
  const left = a.toLowerCase();
  const right = b.toLowerCase();
  if (left === right) return 1.0;
  return sequenceRatio(left, right);
}

function words(text: string): string[] {
  return text.toLowerCase().match(WORD) ?? [];
}

function typo(a: string, b: string): boolean {
  const la = pyLen(a);
  const lb = pyLen(b);
  return la >= 4 && Math.abs(la - lb) <= 1 && Array.from(a)[0] === Array.from(b)[0]
    && sequenceRatio(a, b) >= 0.8;
}

/** True when new is old with small edits only, so it names the same control. */
export function sameText(old: string | undefined | null, next: string | undefined | null, r: number): boolean {
  if (!old || !next || pyLen(old) < 2) return false;
  if (r < SAME_TEXT && !(r >= LONG_SAME && pyLen(old) >= LONG_TEXT)) return false;
  const a = words(old);
  const b = words(next);
  const digitsA = a.filter(isDigits);
  const digitsB = b.filter(isDigits);
  if (digitsA.length !== digitsB.length || digitsA.some((w, i) => w !== digitsB[i])) return false;
  if (a.join('') === b.join('')) return true;
  const theirs = new Set(b.filter((w) => !isDigits(w)));
  for (const word of a) {
    if (isDigits(word) || theirs.has(word) || FILLER.has(word)) continue;
    if (![...theirs].some((other) => typo(word, other))) return false;
  }
  return true;
}

function tokens(value: string | undefined): Set<string> {
  return new Set((value || '').match(TOKEN)?.map((t) => t.toLowerCase()) ?? []);
}

function jaccard(a: Iterable<string> | undefined, b: Iterable<string> | undefined): number {
  const left = new Set(a ?? []);
  const right = new Set(b ?? []);
  if (!left.size && !right.size) return 1.0;
  let both = 0;
  for (const x of left) if (right.has(x)) both += 1;
  return both / new Set([...left, ...right]).size;
}

function ancestorSimilarity(old: AncestorPrint, next: AncestorPrint): number {
  const weights = 1.0 + (old.id ? 2.0 : 0) + (old.classes?.length ? 1.0 : 0) + (old.role ? 0.5 : 0);
  let got = old.tag === next.tag ? 1.0 : 0.0;
  if (old.id) got += old.id === next.id ? 2.0 : 0.0;
  if (old.classes?.length) got += jaccard(old.classes, next.classes);
  if (old.role) got += old.role === next.role ? 0.5 : 0.0;
  return got / weights;
}

function chainSimilarity(old: AncestorPrint[] | undefined, next: AncestorPrint[] | undefined): number | null {
  if (!old || !old.length) return null;
  if (!next || !next.length) return 0.0;
  return pySum(old.map((a) => Math.max(...next.map((b) => ancestorSimilarity(a, b))))) / old.length;
}

export interface Score {
  value: number;
  strong: boolean;
}

function identifier(old: string, next: string | undefined, generated: boolean): [number, boolean] {
  if (old === next) return [1.0, !generated];
  return [next ? 0.6 * jaccard(tokens(old), tokens(next)) : 0.0, false];
}

/** How alike two fingerprints are, from 0 to 1, and whether next shares a strong identity
 * signal with the recorded old one. */
export function score(old: Fingerprint, next: Fingerprint): Score {
  const oldAttrs = old.attrs ?? {};
  const newAttrs = next.attrs ?? {};
  const parts: [number, number][] = [[WEIGHTS.tag, old.tag === next.tag ? 1.0 : 0.0]];
  let strong = false;
  if (oldAttrs.id) {
    const generated = generatedId(oldAttrs.id);
    const [sim, hit] = identifier(oldAttrs.id, newAttrs.id, generated);
    parts.push([generated ? 1.0 : WEIGHTS.id, sim]);
    strong = strong || hit;
  }
  const tests = TEST_ATTRS.filter((n) => oldAttrs[n]).map((n) => oldAttrs[n]);
  if (tests.length) {
    const theirs = new Set(TEST_ATTRS.filter((n) => newAttrs[n]).map((n) => newAttrs[n]));
    const hits = tests.filter((t) => theirs.has(t)).length;
    parts.push([WEIGHTS.test, hits / tests.length]);
    strong = strong || hits > 0;
  }
  if (oldAttrs.name) {
    const [sim, hit] = identifier(oldAttrs.name, newAttrs.name, false);
    parts.push([WEIGHTS.name, sim]);
    strong = strong || hit;
  }
  const auto = oldAttrs.autocomplete;
  if (auto && !VAGUE_AUTOCOMPLETE.includes(auto)) {
    const same = auto === newAttrs.autocomplete;
    parts.push([WEIGHTS.autocomplete, same ? 1.0 : 0.0]);
    strong = strong || same;
  }
  for (const name of EXACT_ATTRS) {
    if (oldAttrs[name]) parts.push([WEIGHTS[name], oldAttrs[name] === newAttrs[name] ? 1.0 : 0.0]);
  }
  for (const name of RATIO_ATTRS) {
    if (oldAttrs[name]) parts.push([WEIGHTS[name], textRatio(oldAttrs[name], newAttrs[name])]);
  }
  // A field's name on screen may move between a <label> and aria-label in a redesign.
  const names = [next.label, newAttrs['aria-label']];
  const named: [number, string | undefined][] = [[WEIGHTS['aria-label'], oldAttrs['aria-label']],
    [WEIGHTS.label, old.label]];
  for (const [weight, text] of named) {
    if (!text) continue;
    // max((ratio, other or '')) as Python compares the tuples: the higher ratio, and on a
    // tie the text that sorts last.
    let best: [number, string] | null = null;
    for (const other of names) {
      const candidate: [number, string] = [textRatio(text, other), other || ''];
      if (best === null || candidate[0] > best[0] || (candidate[0] === best[0] && candidate[1] > best[1])) {
        best = candidate;
      }
    }
    const [r, other] = best as [number, string];
    parts.push([weight, r]);
    strong = strong || sameText(text, other, r);
  }
  if (oldAttrs.href) {
    const same = oldAttrs.href === newAttrs.href;
    parts.push([WEIGHTS.href, same ? 1.0 : textRatio(oldAttrs.href, newAttrs.href)]);
    strong = strong || (same && oldAttrs.href !== '/');
  }
  if (old.classes?.length) parts.push([WEIGHTS.classes, jaccard(old.classes, next.classes)]);
  if (old.text) {
    const r = textRatio(old.text, next.text);
    parts.push([WEIGHTS.text, r]);
    strong = strong || sameText(old.text, next.text, r);
  }
  const chain = chainSimilarity(old.ancestors, next.ancestors);
  if (chain !== null) parts.push([WEIGHTS.ancestors, chain]);
  const total = pySum(parts.map(([w]) => w));
  return { value: total ? pySum(parts.map(([w, s]) => w * s)) / total : 0.0, strong };
}

/** The ids of the recorded ancestors that are not generated ones: the containers the
 * element must still be inside when one of them is on the page. */
export function containerIds(fp: Fingerprint): string[] {
  const out: string[] = [];
  for (const a of fp.ancestors ?? []) {
    if (isRecord(a) && a.id && !generatedId(a.id) && !out.includes(a.id)) out.push(a.id);
  }
  return out;
}

export function fmt(value: number): string {
  return value.toFixed(2);
}
