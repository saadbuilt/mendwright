// Lists (the Python package's mendwright/lists.py): what the items of a list looked like, for
// heal.locatorAll(), and the rules and texts of its lookups.
//
// Each time a list lookup matches two or more elements, mendwright records a list fingerprint
// in one round trip (listRaw in browser/lib.ts, the Python package's LIST_JS): the
// fingerprints of up to three of the items (the first, the middle and the last, see
// fingerprint.ts), the tags inside each one and of its children (its shape), how many items
// there were, their nearest common container (its tag, id, test id, aria-label, classes and
// role) and how many levels below it they sit. A list that matched fewer than two elements is
// not recorded: nothing repeats in it. A lookup with nonempty: true records it with the list's
// label too (the aria-labelledby or aria-label around it, or else the nearest heading before
// it) and where it sits: short hashes of its container and the elements around it, and of the
// page's other elements and labels (LIST_PLACE_JS). Once a key's list fingerprint holds those,
// every later recording of it reads them too.
//
// The list fingerprint says that the list had items, so a lookup of it that matches nothing
// waits and throws instead of giving [] at once. mendwright never uses a list its own selectors
// don't match: an emptied list, whose items can show in another list on the page, such as a
// cart's in Saved for later, can't be told from a list whose selector broke.
//
// The page reads the elements (browser/lib.ts); everything that reads its result, and every
// text, is here, the Python package's character for character, as the tests check.

import * as crypto from 'node:crypto';

import type { LibName } from './driver';
import {
  THRESHOLD as FINGERPRINT_THRESHOLD, VALUE_LIMIT, containerIds, fromRaw as fingerprintFromRaw, generatedId, jaccard, norm,
  score, stableClasses, storedText, valid as validFingerprint, type Fingerprint, type RawElement, type Score,
} from './fingerprint';
import {
  isDigitChar, pyCompare, pyFixed, pyJsonSorted, pyLen, pyRepr, pyRound, pySlice, pySorted, pySplit, pyStrip, pySum,
} from './py';
import { roleFor } from './roles';
import { reprText } from './values';

/** Version of the stored shape. A list fingerprint with another version is ignored. */
export const VERSION = 1;
/** Items recorded per list. */
export const SAMPLES = 3;
/** Tags kept in a shape. */
export const SHAPE_TAGS = 40;
/** Elements the live gate reads (ITEMS_JS). */
export const ITEMS = 200;
export const CONTAINER_ATTRS = ['id', 'class', 'role', 'aria-label', 'data-testid', 'data-test', 'data-qa'];
const CONTAINER_TESTS = ['data-testid', 'data-test', 'data-qa'];
/** Where the list was: at most PLACE_KEPT hashes of the other elements of the page, and of
 * PLACE_LABELS of its labels. */
export const PLACE_KEPT = 1000;
export const PLACE_LABELS = 200;

/** One recorded item: its fingerprint, and the tags inside it (its shape). */
export interface ListItem extends Fingerprint {
  shape: Record<string, number>;
}

/** A recorded container, as stored: its tag, and its id, aria-label, test id, stable classes
 * and role when it has them. */
export interface ContainerNode {
  tag: string;
  id?: string;
  aria?: string;
  test?: string;
  classes?: string[];
  role?: string;
}

/** Where a list was, as stored: the hashes of its container and of the elements around it,
 * the container first, those of the other elements of the page and of its labels, sorted, and
 * whether some elements were left out. */
export interface ListPlace {
  chain: string[];
  others: string[];
  labels: string[];
  cut: boolean;
}

/** A stored list fingerprint: the shape the store keeps under "listFingerprint". */
export interface ListFingerprint {
  v: number;
  count: number;
  depth: number;
  items: ListItem[];
  container?: ContainerNode;
  label?: string;
  place?: ListPlace;
  shadow?: true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isInt(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value);
}

// --- hashes ---------------------------------------------------------------------------------

// BLAKE2s (RFC 7693), as Python's hashlib.blake2s(digest_size=4) gives it: the digest size is
// part of the hash's parameters, so it is not a cut of a longer digest, and Node's crypto has
// no such size.
const IV = [0x6A09E667, 0xBB67AE85, 0x3C6EF372, 0xA54FF53A, 0x510E527F, 0x9B05688C, 0x1F83D9AB, 0x5BE0CD19];
const SIGMA = [
  [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
  [14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3],
  [11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4],
  [7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8],
  [9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13],
  [2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9],
  [12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11],
  [13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10],
  [6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5],
  [10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0],
];

const rotr = (x: number, n: number) => ((x >>> n) | (x << (32 - n))) >>> 0;

/** The BLAKE2s digest of data, size bytes long (1 to 32), with no key. */
export function blake2s(data: Uint8Array, size: number): Uint8Array {
  const h = new Uint32Array(IV);
  h[0] ^= 0x01010000 ^ size;
  const v = new Uint32Array(16);
  const m = new Uint32Array(16);
  const g = (a: number, b: number, c: number, d: number, x: number, y: number) => {
    v[a] = v[a] + v[b] + x;
    v[d] = rotr(v[d] ^ v[a], 16);
    v[c] = v[c] + v[d];
    v[b] = rotr(v[b] ^ v[c], 12);
    v[a] = v[a] + v[b] + y;
    v[d] = rotr(v[d] ^ v[a], 8);
    v[c] = v[c] + v[d];
    v[b] = rotr(v[b] ^ v[c], 7);
  };
  const compress = (block: Uint8Array, counted: number, last: boolean) => {
    for (let i = 0; i < 16; i++) {
      m[i] = block[i * 4] | (block[i * 4 + 1] << 8) | (block[i * 4 + 2] << 16) | (block[i * 4 + 3] << 24);
    }
    for (let i = 0; i < 8; i++) {
      v[i] = h[i];
      v[i + 8] = IV[i];
    }
    v[12] ^= counted >>> 0;
    v[13] ^= Math.floor(counted / 0x100000000) >>> 0;
    if (last) v[14] ^= 0xFFFFFFFF;
    for (const s of SIGMA) {
      g(0, 4, 8, 12, m[s[0]], m[s[1]]);
      g(1, 5, 9, 13, m[s[2]], m[s[3]]);
      g(2, 6, 10, 14, m[s[4]], m[s[5]]);
      g(3, 7, 11, 15, m[s[6]], m[s[7]]);
      g(0, 5, 10, 15, m[s[8]], m[s[9]]);
      g(1, 6, 11, 12, m[s[10]], m[s[11]]);
      g(2, 7, 8, 13, m[s[12]], m[s[13]]);
      g(3, 4, 9, 14, m[s[14]], m[s[15]]);
    }
    for (let i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
  };
  const block = new Uint8Array(64);
  let at = 0;
  // Every full block but the last is compressed as it comes; the last one, full or not (an
  // empty input is one block of zeros), with the last-block flag.
  while (data.length - at > 64) {
    block.set(data.subarray(at, at + 64));
    at += 64;
    compress(block, at, false);
  }
  block.fill(0);
  block.set(data.subarray(at));
  compress(block, data.length, true);
  const out = new Uint8Array(size);
  for (let i = 0; i < size; i++) out[i] = (h[i >> 2] >>> (8 * (i & 3))) & 0xff;
  return out;
}

/** A short hash of text (the Python package's lists._hash): the hex of its 4-byte BLAKE2s. */
export function shortHash(text: string): string {
  return Buffer.from(blake2s(new TextEncoder().encode(text), 4)).toString('hex');
}

/** The first 8 hex digits of the SHA-256 of text. */
export function sha256Prefix(text: string): string {
  return crypto.createHash('sha256').update(text, 'utf8').digest('hex').slice(0, 8);
}

// --- normalizing ----------------------------------------------------------------------------

/** A shape as stored: {tag: count} for at most SHAPE_TAGS tags, the most common first. */
export function shapeOf(raw: unknown): Record<string, number> {
  if (!isRecord(raw)) return {};
  const pairs: [string, number][] = [];
  for (const [t, n] of Object.entries(raw)) {
    const tag = pyStrip(t).toLowerCase();
    if (!tag || typeof n !== 'number' || !(n > 0)) continue;
    pairs.push([tag, Math.trunc(n)]);
  }
  // sorted(key=(-count, tag)), stable as Python's is.
  pairs.sort((a, b) => (b[1] - a[1]) || pyCompare(a[0], b[0]));
  const out: Record<string, number> = {};
  for (const [tag, n] of pairs.slice(0, SHAPE_TAGS)) out[tag] = n;
  return out;
}

/** A container as stored (the Python package's lists._node), or null for anything else. */
export function nodeOf(raw: unknown): ContainerNode | null {
  if (!isRecord(raw) || typeof raw.tag !== 'string' || !pyStrip(raw.tag)) return null;
  const attrs = isRecord(raw.attrs) ? raw.attrs : {};
  const node: ContainerNode = { tag: pyStrip(raw.tag).toLowerCase() };
  for (const [name, field] of [['id', 'id'], ['aria-label', 'aria']] as const) {
    const value = pySlice(norm(attrs[name]), VALUE_LIMIT);
    if (value) node[field] = value;
  }
  for (const name of CONTAINER_TESTS) {
    const value = pySlice(norm(attrs[name]), VALUE_LIMIT);
    if (value) {
      node.test = value;
      break;
    }
  }
  const classes = stableClasses(attrs.class);
  if (classes.length) node.classes = classes;
  const role = norm(attrs.role).toLowerCase();
  if (role) node.role = role;
  return node;
}

/** A short hash of an element as listRaw()'s raw() reads it: [tag, id, test id, class] of it
 * and of its parent. Its tag, its id unless a framework generated it, its test id and its
 * stable classes, and its parent's: two elements with the same hash look the same to a list
 * fingerprint, where they sit included (the Python package's lists._sig). */
export function sigOf(raw: string[]): string {
  const parts: string[] = [];
  for (const [tag, rawId, test, classes] of [raw.slice(0, 4), raw.slice(4, 8)]) {
    const ident = pySlice(norm(rawId), VALUE_LIMIT);
    parts.push(pyStrip(tag).toLowerCase() + '#' + (generatedId(ident) ? '' : ident) + '['
      + pySlice(norm(test), VALUE_LIMIT) + ']' + pySorted(stableClasses(classes)).join('.'));
  }
  return shortHash(parts.join('<'));
}

/** A label as two are compared: lower case, without digits, as a count in a heading such as
 * "Cart (3)" changes with the list. */
export function labelKey(text: string): string {
  return pySplit(Array.from(text.toLowerCase()).filter((c) => !isDigitChar(c)).join('')).join(' ');
}

function raws(value: unknown): string[][] | null {
  if (!Array.isArray(value)) return null;
  const out: string[][] = [];
  for (const raw of value) {
    if (!(Array.isArray(raw) && raw.length === 8 && raw.every((v) => typeof v === 'string'))) return null;
    out.push(raw as string[]);
  }
  return out;
}

/** Where a list was, as stored (the Python package's lists._place), or null for anything else. */
export function placeOf(raw: unknown): ListPlace | null {
  if (!isRecord(raw)) return null;
  const chain = raws(raw.chain);
  const others = raws(raw.others);
  if (chain === null || others === null) return null;
  const labels = Array.isArray(raw.labels) ? raw.labels : [];
  const said = new Set<string>();
  for (const text of labels.slice(0, PLACE_LABELS)) {
    if (typeof text !== 'string') continue;
    const key = labelKey(storedText(text));
    if (key) said.add(shortHash(key));
  }
  const kept = [...new Set(others.map(sigOf))];
  return { chain: chain.map(sigOf), others: pySorted(kept.slice(0, PLACE_KEPT)), labels: pySorted(said),
           cut: raw.cut === true || kept.length > PLACE_KEPT };
}

/** A stored list fingerprint from listRaw()'s result, or null when raw is not that shape or
 * counts fewer than two items (the Python package's lists.from_raw). */
export function listFromRaw(raw: unknown): ListFingerprint | null {
  if (!isRecord(raw)) return null;
  const count = raw.count;
  if (!isInt(count) || count < 2) return null;
  const items: ListItem[] = [];
  for (const item of (Array.isArray(raw.items) ? raw.items : []).slice(0, SAMPLES)) {
    if (!isRecord(item)) continue;
    const fp = fingerprintFromRaw(item.fp);
    if (fp !== null) items.push({ ...fp, shape: shapeOf(item.shape) });
  }
  if (!items.length) return null;
  const depth = raw.depth;
  const out: ListFingerprint = { v: VERSION, count, depth: isInt(depth) && depth > 0 && depth <= 50 ? depth : 1, items };
  const container = nodeOf(raw.container);
  if (container) out.container = container;
  if (typeof raw.label === 'string') out.label = storedText(raw.label);
  const place = placeOf(raw.place);
  if (place !== null) out.place = place;
  if (raw.shadow === true) out.shadow = true;   // the items were inside a shadow root
  return out;
}

/** True when lfp is a stored list fingerprint this version can score. */
export function validList(lfp: unknown): lfp is ListFingerprint {
  return isRecord(lfp) && lfp.v === VERSION && isInt(lfp.count) && lfp.count >= 2 && Array.isArray(lfp.items)
    && lfp.items.length > 0 && lfp.items.every(validFingerprint);
}

/** True when the items of the list fingerprint lfp were inside an open shadow root when it was
 * recorded: a list is looked for only in the page's own HTML, so such a list is never looked
 * for. */
export function inShadow(lfp: unknown): boolean {
  return isRecord(lfp) && lfp.shadow === true;
}

/** True when the list fingerprint lfp holds where the list was and its label (recorded with
 * nonempty: true), which a search needs to tell another list from it. */
export function placed(lfp: unknown): boolean {
  if (!isRecord(lfp)) return false;
  const place = lfp.place;
  return typeof lfp.label === 'string' && isRecord(place) && Array.isArray(place.chain) && Array.isArray(place.others);
}

/** The gate's role for the items of a list fingerprint, from its first item. */
export function itemRole(lfp: ListFingerprint): string {
  const item = lfp.items[0];
  const attrs = item.attrs ?? {};
  return roleFor(item.tag, attrs.role, attrs.type);
}

// --- texts ------------------------------------------------------------------------------------

/** What a refusal of a list whose groups are all other lists says: both things it can be. */
export const EMPTY_OR_BROKEN = 'either the list is empty at this point, or its selector broke and its items are not '
  + 'on the page';

/** How many elements a list had when its list fingerprint was recorded, in words. */
export function hadText(count: unknown): string {
  return isInt(count) && count >= 2 ? count + ' elements' : 'two or more elements';
}

/** Why a list whose selectors all match nothing is not healed (the Python package's
 * coordinator._list_missing_message). count is how many elements the list had when its list
 * fingerprint was recorded. */
export function listMissingMessage(key: string, count: unknown): string {
  return "'" + key + "' matched " + hadText(count) + ' when its list fingerprint was recorded, and none of its '
    + "selectors matches now. Either the list is empty at this point, or its selector broke: mendwright can't tell "
    + "the two apart, since the items of an emptied list can show in another list, such as a cart's in Saved for "
    + 'later, so it never heals a list that matches nothing. If the selector broke, change it in your code.';
}

/** What heal.locatorAll() throws for nonempty other than true or false (the Python package's
 * facade._nonempty_args), before anything touches the page. */
export function nonemptyArgsError(key: string, nonempty: unknown, optional: unknown): TypeError | null {
  if (nonempty !== undefined && typeof nonempty !== 'boolean') {
    const kind = nonempty === null ? 'null' : Array.isArray(nonempty) ? 'array' : typeof nonempty;
    return new TypeError('mendwright: heal.locatorAll(' + pyRepr(key) + ', ..., { nonempty: ... }) takes true or false, '
      + 'got ' + kind + '.');
  }
  if (nonempty === true && optional) {
    return new TypeError('mendwright: heal.locatorAll(' + pyRepr(key) + ', ..., { nonempty: true, optional: true }) '
      + 'asks for two opposite things: nonempty: true says the list always has items here, and optional: true says it '
      + 'may be empty. Pass one of them.');
  }
  return null;
}

// --- finding a list: nonempty: true (the Python package's lists.find) -------------------------
//
// find() lists the repeated groups of the captured page: two or more elements of the items' role
// that sit the same number of levels (one to three) below one container, with the same tags on
// the way down, and, for each class name two of them share, those that have it. The page parses
// the captured HTML and reads them, with every fact of an element find() needs (listScan in
// browser/lib.ts); they are scored here against the list fingerprint, from 0 to 1:
//
// - how much each member looks like one of the recorded items (fingerprint.ts's score()), on
//   average (a half of the score)
// - how alike the members' shapes are to the recorded ones (a quarter), left out when the
//   members and the recorded items have nothing inside them, which says nothing
// - how alike the group's container is to the recorded one (0.15)
// - how close the number of members is to the recorded count (0.10)
//
// and it finds a group only when all of these hold: the best group scores at least THRESHOLD,
// no group that shares less than half of its members with it comes within MARGIN, every member
// it looked at has a shape at least SHAPE_MIN alike, two of the recorded items (both, for a list
// recorded with two) are among its members, it is inside the recorded container and the
// recorded items' ancestors with an id when those are still on the page, it is not another list
// (see Places), and an XPath without positions matches its members and nothing else (listXpath
// in browser/lib.ts).

export const THRESHOLD = 0.65;
export const MARGIN = 0.10;
export const SHAPE_MIN = 0.5;
/** Recorded items that must be among a group's members for find() to take it. */
export const NEEDED = 2;
/** Members of a group compared with the recorded items. */
export const SCORED = 25;
/** Members of the best group looked through for the recorded items, spread over it. */
export const IDENTIFIED = 200;
/** Groups scored in full, of those that score best in a quick look. */
export const RANKED = 30;
const LEAF = new Set(['a', 'abbr', 'area', 'audio', 'b', 'base', 'bdi', 'bdo', 'br', 'button', 'canvas', 'cite',
  'code', 'col', 'data', 'dfn', 'em', 'embed', 'hr', 'i', 'iframe', 'img', 'input', 'kbd', 'label', 'link', 'map',
  'mark', 'meta', 'meter', 'object', 'optgroup', 'option', 'output', 'param', 'picture', 'progress', 'q', 's',
  'samp', 'select', 'small', 'source', 'span', 'strong', 'sub', 'sup', 'textarea', 'time', 'track', 'u', 'var',
  'video', 'wbr']);
const IDENTITY_ATTRS = ['id', 'name', 'autocomplete', 'aria-label', 'href', 'data-testid', 'data-test', 'data-qa'];
const IDENTITY_FIELDS = ['text', 'label'] as const;
/** A list fingerprint saved before where the list was and its label were recorded can't tell
 * another list from the list, so nothing is looked for from it. */
export const OLD_FINGERPRINT = 'the stored list fingerprint was saved by an earlier build, which did not record where '
  + 'the list was or its label, so another list could not be told from it; it is recorded anew by the next run in '
  + 'which the list matches';

/** One element of the captured page as listScan() reads it: its tag, its parent (its index, or
 * -1), the attributes find() reads, and for an element of the items' role its raw fingerprint
 * read, its shape, and its text when it is a field (raw.text is null for one). */
export interface ViewNode {
  tag: string;
  parent: number;
  attrs: Record<string, string>;
  raw?: RawElement;
  shape?: Record<string, number>;
  content?: string;
}

/** A repeated group as listScan() reads it: its members (in page order), its container, the
 * tags on the way down to its members, and its label (the raw text of lists._label_text). */
export interface ViewGroup {
  members: number[];
  container: number;
  steps: string[];
  label: string;
}

/** What listScan() reads of the captured page. */
export interface ListScan {
  nodes: Record<string, ViewNode>;
  pool: number[];
  groups: ViewGroup[];
  said: string[];
  holders: [string, number][];
}

interface Group extends ViewGroup {
  score: number;
  shape: number | null;
  xpath: string | null;
  css: string | null;
  alike: [number, number] | null;
  built: boolean;
}

/** find()'s answer. xpath (and css, its CSS form when it has one) is set only when it found the
 * list; reason says why not otherwise, and code and fragment say it once more for the refusal
 * mendwright throws, with every control character made printable. count is how many elements
 * the best group has, held how many of the recorded items are among them, of recorded ones.
 * best and runner are the best group and its nearest rival, label the best group's label,
 * container its container as stored (null for <body>), memberTexts the text of its first two
 * members, and alike where that container is among the elements of the page like it. */
export interface ListMatch {
  xpath: string | null;
  css: string | null;
  count: number;
  score: number;
  runnerUp: number;
  reason: string;
  /** Every group on the page is another list, or the list fingerprint can't tell one. */
  emptied: boolean;
  code: string;
  fragment: string;
  best: Group | null;
  runner: Group | null;
  held: number;
  recorded: number;
  label: string;
  container: ContainerNode | null;
  memberTexts: string[];
  alike: [number, number] | null;
}

/** A helper of the page library, run in the page (PlaywrightDriver.lib). */
export type LibCall = <T>(name: LibName, arg?: unknown) => Promise<T>;

function refused(code: string, reason: string, fragment: string | null = null,
                 fields: { emptied?: boolean; recorded?: number } = {}): ListMatch {
  return { xpath: null, css: null, count: 0, score: 0.0, runnerUp: 0.0, reason, emptied: fields.emptied ?? false, code,
           fragment: printable(fragment === null ? reason : fragment), best: null, runner: null, held: 0,
           recorded: fields.recorded ?? 0, label: '', container: null, memberTexts: [], alike: null };
}

/** round(x) in Python, for a float: to the nearest whole number, and on an exact half to the
 * even one. */
function roundHalfEven(x: number): number {
  const floor = Math.floor(x);
  const rest = x - floor;
  if (rest !== 0.5) return rest < 0.5 ? floor : floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** At most limit members, spread over the group. */
function sampleOf<T>(members: T[], limit: number): T[] {
  if (members.length <= limit) return [...members];
  const step = (members.length - 1) / (limit - 1);
  return Array.from({ length: limit }, (_, i) => members[roundHalfEven(i * step)]);
}

/** How alike two shapes are, from 0 to 1: the tags they share, weighted by how many of each
 * they have (a weighted Jaccard). null for two empty shapes: two elements with nothing inside
 * them say nothing about each other. */
export function shapeSimilarity(old: Record<string, number>, next: Record<string, number>): number | null {
  const tags = new Set([...Object.keys(old), ...Object.keys(next)]);
  if (!tags.size) return null;
  let low = 0;
  let high = 0;
  for (const t of tags) {
    low += Math.min(old[t] ?? 0, next[t] ?? 0);
    high += Math.max(old[t] ?? 0, next[t] ?? 0);
  }
  return low / high;
}

/** What two groups share: how many members, of the smaller one's. */
function overlap(a: Group, b: Group): number {
  const theirs = new Set(b.members);
  return a.members.filter((m) => theirs.has(m)).length / Math.min(a.members.length, b.members.length);
}

/** The recorded items as identified() compares them: each without the identity values every
 * one of them has (a test id, link, name or text all the items share). Unchanged for one. */
function distinctItems(items: ListItem[]): Fingerprint[] {
  if (items.length < 2) return items;
  const out = items.map((item) => ({ ...item, attrs: { ...(item.attrs ?? {}) } })) as (ListItem & {
    attrs: Record<string, string> })[];
  for (const name of IDENTITY_ATTRS) {
    const values = new Set<string | undefined>(items.map((item) => (item.attrs ?? {})[name]));
    if (values.size === 1 && !values.has(undefined)) for (const item of out) delete item.attrs[name];
  }
  for (const name of IDENTITY_FIELDS) {
    const values = new Set(items.map((item) => item[name]));
    if (values.size === 1 && !values.has(undefined)) for (const item of out) delete item[name];
  }
  return out;
}

/** The captured page as listScan() read it, with the reads find() makes of its elements. */
class View {
  readonly nodes: Map<number, ViewNode>;
  readonly pool: number[];
  readonly groups: Group[];
  readonly said: string[];
  readonly holders: [string, number][];

  constructor(scan: ListScan) {
    this.nodes = new Map(Object.entries(scan.nodes).map(([i, node]) => [Number(i), node]));
    this.pool = scan.pool;
    this.groups = scan.groups.map((g) => ({ ...g, score: 0, shape: 0, xpath: null, css: null, alike: null, built: false }));
    this.said = scan.said;
    this.holders = scan.holders;
  }

  node(el: number): ViewNode {
    const node = this.nodes.get(el);
    if (!node) throw new Error('mendwright: the read of the captured page left out element ' + el);
    return node;
  }

  /** The elements around el, the nearest first. */
  ancestors(el: number): number[] {
    const out: number[] = [];
    for (let p = this.node(el).parent; p >= 0; p = this.node(p).parent) out.push(p);
    return out;
  }

  /** An element's text the way XPath's normalize-space() sees it (candidates.text_of). */
  text(el: number): string {
    const node = this.node(el);
    const text = node.raw && node.raw.field ? node.content : node.raw?.text;
    return pyStrip((text || '').replace(/[ \t\r\n]+/g, ' '));
  }

  /** [tag, id, test id, class] of el and of its parent, as listRaw()'s raw() reads them. */
  raw(el: number): string[] {
    const own = (node: ViewNode) => [node.tag, node.attrs.id ?? '', testOf(node), node.attrs.class ?? ''];
    const node = this.node(el);
    return own(node).concat(node.parent >= 0 ? own(this.node(node.parent)) : ['', '', '', '']);
  }

  /** The element as a stored container node (lists._node_of). */
  container(el: number): ContainerNode | null {
    const node = this.node(el);
    return nodeOf({ tag: node.tag, attrs: node.attrs });
  }
}

function testOf(node: ViewNode): string {
  for (const name of CONTAINER_TESTS) if (node.attrs[name] !== undefined) return node.attrs[name];
  return '';
}

/** An element as a refusal names it: its tag, id, class, role and test id (lists._summary
 * with no text). */
function summary(view: View, el: number): string {
  const node = view.node(el);
  let attrs = '';
  for (const name of ['id', 'class', 'role', 'data-testid']) {
    const value = node.attrs[name];
    if (value !== undefined && pyLen(value) <= 120) attrs += ' ' + name + '="' + value.split('"').join("'") + '"';
  }
  return '<' + node.tag + attrs + '>';
}

/** How alike a recorded container and a group's container are, from 0 to 1, or null when no
 * container was recorded. */
function containerSimilarity(old: ContainerNode | undefined, view: View, el: number): number | null {
  if (!old) return null;
  const next: Partial<ContainerNode> = view.container(el) ?? {};
  let got = old.tag === next.tag ? 1.0 : 0.0;
  let weights = 1.0;
  for (const [name, weight] of [['id', 2.0], ['test', 2.0], ['aria', 1.0], ['role', 0.5]] as const) {
    if (old[name]) {
      weights += weight;
      got += old[name] === next[name] ? weight : 0.0;
    }
  }
  if (old.classes && old.classes.length) {
    weights += 1.0;
    got += jaccard(old.classes, next.classes);
  }
  return got / weights;
}

/** Scores the groups of one page against one list fingerprint, with each element's
 * fingerprint read once (the Python package's lists._Scorer). */
class Scorer {
  readonly items: ListItem[];
  readonly shapes: Record<string, number>[];
  readonly distinct: Fingerprint[];
  private readonly prints = new Map<number, Fingerprint | null>();
  private readonly results = new Map<number, Score[] | null>();

  constructor(readonly lfp: ListFingerprint, readonly view: View) {
    this.items = lfp.items;
    this.shapes = lfp.items.map((item) => item.shape ?? {});
    this.distinct = distinctItems(lfp.items);
  }

  fp(el: number): Fingerprint | null {
    if (!this.prints.has(el)) this.prints.set(el, fingerprintFromRaw(this.view.node(el).raw));
    return this.prints.get(el) as Fingerprint | null;
  }

  /** score() of el against each recorded item, or null when el has no fingerprint. */
  scores(el: number): Score[] | null {
    if (!this.results.has(el)) {
      const next = this.fp(el);
      this.results.set(el, next === null ? null : this.items.map((old) => score(old, next)));
    }
    return this.results.get(el) as Score[] | null;
  }

  private count(n: number): number {
    const recorded = this.lfp.count;
    return Math.min(n, recorded) / Math.max(n, recorded);
  }

  private total(item: number, shape: number | null, container: number | null, count: number): number {
    const parts: [number, number][] = [[0.5, item], [0.10, count]];
    if (shape !== null) parts.push([0.25, shape]);
    if (container !== null) parts.push([0.15, container]);
    return pySum(parts.map(([w, s]) => w * s)) / pySum(parts.map(([w]) => w));
  }

  /** How alike el's shape is to the most alike recorded item's, or null when el and a recorded
   * item both have nothing inside them, which says nothing. */
  private shaped(el: number): number | null {
    const shape = this.view.node(el).shape ?? {};
    const values = this.shapes.map((s) => shapeSimilarity(s, shape));
    if (values.includes(null)) return null;
    return Math.max(...(values as number[]));
  }

  /** A score from the first member alone, without text: to pick the groups worth scoring in
   * full. */
  quick(group: Group): number {
    const first = group.members[0];
    const tag = this.view.node(first).tag === this.items[0].tag ? 1.0 : 0.0;
    const container = containerSimilarity(this.lfp.container, this.view, group.container);
    return this.total(tag, this.shaped(first), container, this.count(group.members.length));
  }

  /** Score the group: its score, and the least alike shape of the members it looked at (null
   * when no shape said anything). */
  full(group: Group): Group {
    const scores: number[] = [];
    const shapes: number[] = [];
    for (const el of sampleOf(group.members, SCORED)) {
      const results = this.scores(el);
      if (results === null) {
        scores.push(0.0);
        continue;
      }
      scores.push(Math.max(...results.map((r) => r.value)));
      const shape = this.shaped(el);
      if (shape !== null) shapes.push(shape);
    }
    const container = containerSimilarity(this.lfp.container, this.view, group.container);
    const meanShape = shapes.length ? pySum(shapes) / shapes.length : null;
    group.score = this.total(pySum(scores) / scores.length, meanShape, container, this.count(group.members.length));
    group.shape = shapes.length ? Math.min(...shapes) : null;
    return group;
  }

  /** [found, shared]: how many of the recorded items are among the group's members, and
   * whether a member shares an identity signal with every recorded item. A recorded item is
   * among them when a member shares an id, test id, name, link, label or text with it that not
   * every recorded item has, and does not share one with every recorded item. With one recorded
   * item, any such member counts. */
  identified(group: Group): [number, boolean] {
    const found = new Set<number>();
    let shared = false;
    for (const el of sampleOf(group.members, IDENTIFIED)) {
      const next = this.fp(el);
      if (next === null) continue;
      if (this.items.length > 1 && (this.scores(el) as Score[]).every((r) => r.strong)) shared = true;
      const hits = this.distinct.map((old, i) => (score(old, next).strong ? i : -1)).filter((i) => i >= 0);
      if (hits.length && (hits.length < this.items.length || this.items.length === 1)) {
        for (const i of hits) found.add(i);
        if (found.size === this.items.length) break;
      }
    }
    return [found.size, shared];
  }
}

function shownLabel(text: string): string {
  return text ? reprText(text) : 'no heading or label';
}

/** The place and label tests of one page against one list fingerprint (the Python package's
 * lists._Places). An emptied list, such as a cart whose items all moved to Saved for later, must
 * not be found in the other list that holds its items now. Two things recorded with the list
 * fingerprint tell a group that is another list from the list after a redesign, each a plain
 * test, with no closeness scores:
 *
 * - place: going out from each member's parent, the first element that was on the page when
 *   the list was recorded must be the list's container or one around it. One that was
 *   elsewhere then, outside the list, holds another list. A redesign's new markup was not
 *   there, so the test goes on past it. One like both holds the list only under its own label.
 * - label: a group under another label than the list's is another list when that label was on
 *   the page when the list was recorded, or the list's own label still is. A heading renamed
 *   with the list says nothing. Digits are left out (labelKey). */
class Places {
  readonly label: string | null;
  readonly chain: Set<string> | null;
  readonly others: Set<string>;
  readonly said: Set<string>;
  readonly cut: boolean;
  private readonly sigs = new Map<number, string>();
  private now: Set<string> | null = null;

  constructor(lfp: ListFingerprint, readonly view: View) {
    this.label = typeof lfp.label === 'string' ? lfp.label : null;
    const place = lfp.place as unknown;
    const ok = isRecord(place) && Array.isArray(place.chain) && Array.isArray(place.others);
    this.chain = ok ? new Set(place.chain as string[]) : null;
    this.others = ok ? new Set(place.others as string[]) : new Set();
    this.said = ok ? new Set(Array.isArray(place.labels) ? (place.labels as string[]) : []) : new Set();
    this.cut = ok && place.cut === true;
  }

  labelOf(group: ViewGroup): string {
    return storedText(group.label);
  }

  /** True when the group has the list's label and it is not empty. */
  labelled(group: Group): boolean {
    return Boolean(this.label) && labelKey(this.labelOf(group)) === labelKey(this.label as string);
  }

  private sigOf(el: number): string {
    if (!this.sigs.has(el)) this.sigs.set(el, sigOf(this.view.raw(el)));
    return this.sigs.get(el) as string;
  }

  /** The labels of the page now, as labelKey() gives them. */
  private saidNow(): Set<string> {
    if (this.now === null) this.now = new Set(this.view.said.map((text) => labelKey(storedText(text))));
    return this.now;
  }

  /** Why the group is another list by its label, or ''. */
  byLabel(group: Group): string {
    if (this.label === null || this.chain === null) return '';
    const theirs = this.labelOf(group);
    const mine = labelKey(this.label);
    if (labelKey(theirs) === mine) return '';
    if (theirs && this.said.has(shortHash(labelKey(theirs)))) {
      return 'is under ' + reprText(theirs) + ', which labelled another part of the page when the list was recorded, '
        + 'and the list was under ' + shownLabel(this.label);
    }
    if (mine && this.saidNow().has(mine)) {
      return 'is under ' + shownLabel(theirs) + ', and the list was under ' + reprText(this.label)
        + ', which is still on the page';
    }
    return '';
  }

  /** Going out from start, the first element that was on the page when the list was recorded:
   * ['mine', el] for the list's container or one around it, ['other', el] for one elsewhere,
   * ['both', el] for one like each, or ['top', -1] for none; and whether markup that was not
   * there came first. */
  private up(start: number): [string, number, boolean] {
    let fresh = false;
    for (let el = start; el >= 0; el = this.view.node(el).parent) {
      const tag = this.view.node(el).tag;
      if (tag === 'body' || tag === 'html') break;
      if (!LEAF.has(tag)) {
        const sig = this.sigOf(el);
        const mine = (this.chain as Set<string>).has(sig);
        const other = this.others.has(sig);
        if (mine || other) return [mine && other ? 'both' : mine ? 'mine' : 'other', el, fresh];
        fresh = true;
      }
    }
    return ['top', -1, fresh];
  }

  /** Why the group is another list by where it is, or '': from each member's parent, so a
   * group that takes elements from two places is never the list. */
  byPlace(group: Group): string {
    if (this.chain === null) return '';
    const starts = [...new Set(group.members.map((m) => this.view.node(m).parent))];
    for (const start of starts) {
      const [kind, el, fresh] = this.up(start);
      if (kind === 'other') {
        return 'is inside ' + summary(this.view, el) + ', which was on the page, outside the list, when the list was '
          + 'recorded';
      }
      if (kind === 'both' && !this.labelled(group)) {
        return 'is inside ' + summary(this.view, el) + ', and when the list was recorded it was in an element like '
          + 'that one, which was not the only one, with no heading or label to tell them apart';
      }
      if (this.cut && fresh && !this.labelled(group)) {
        return 'is in markup that was not on the page when the list was recorded, and the page was too large then to '
          + 'tell where the list was';
      }
    }
    return '';
  }
}

/** Why the group is not the list but another list, or '' when it may be the list: outside a
 * recorded container that is still on the page, under another label, or in another place. */
function elsewhere(view: View, group: Group, holders: [string, number][], places: Places): string {
  if (holders.length) {
    const chain = new Set([group.container, ...view.ancestors(group.container)]);
    if (!holders.every(([, el]) => chain.has(el))) {
      return 'is outside ' + summary(view, holders[0][1]) + ', which held the list and is still on the page';
    }
  }
  return places.byLabel(group) || places.byPlace(group);
}

/** True when the group looks like the placeholders of a list that is still loading: no member
 * has text while the recorded items had (texts), or aria-busy="true" is on a member, the
 * container or above it. */
function placeholder(view: View, group: Group, texts: boolean): boolean {
  if (texts && !group.members.some((el) => view.text(el))) return true;
  const busy = (el: number) => pyStrip(view.node(el).attrs['aria-busy'] ?? '').toLowerCase() === 'true';
  return [...group.members, group.container, ...view.ancestors(group.container)].some(busy);
}

/** A note for a list that was not found, when exactly one element of the role looks like a
 * recorded item: one element is never taken for a list. '' otherwise. */
function lone(scorer: Scorer, view: View): string {
  const alike: number[] = [];
  for (const el of view.pool) {
    const next = scorer.fp(el);
    if (next === null) continue;
    if (scorer.items.some((old) => {
      const result = score(old, next);
      return result.strong && result.value >= FINGERPRINT_THRESHOLD;
    })) {
      alike.push(el);
      if (alike.length > 1) return '';
    }
  }
  if (!alike.length) return '';
  const text = view.text(alike[0]);
  const shown = text ? ' (' + reprText(pySlice(text, 40) + (pyLen(text) > 40 ? '...' : '')) + ')' : '';
  return '. The only element like the recorded items is one <' + view.node(alike[0]).tag + '>' + shown
    + '; mendwright never takes one element for a list';
}

const fmt2 = (value: number) => pyFixed(value, 2);

/** The values listScan() looks for the holders by: the recorded container's id (unless a
 * framework generated it) and test id. */
function holderValues(lfp: ListFingerprint): { id: string | null; test: string | null } {
  const container: Partial<ContainerNode> = lfp.container ?? {};
  return { id: container.id && !generatedId(container.id) ? container.id : null, test: container.test || null };
}

function readScan(raw: unknown): ListScan | null {
  if (!isRecord(raw) || !isRecord(raw.nodes) || !Array.isArray(raw.pool) || !Array.isArray(raw.groups)
      || !Array.isArray(raw.said) || !Array.isArray(raw.holders)) {
    return null;
  }
  return raw as unknown as ListScan;
}

export interface FindOptions {
  threshold?: number;
  margin?: number;
  /** Whether a group, by its XPath, is visible on the live page: false when none of its
   * elements is, so a hidden copy of a list (a menu for small screens, say) is neither the
   * answer nor a rival. null keeps the group. */
  visible?: ((xpath: string) => Promise<boolean | null>) | null;
}

/** Look for the list lfp (a list fingerprint) describes in html, the captured page, among the
 * repeated groups of elements whose role is role (the Python package's lists.find). lib runs
 * the page library's helpers that read the captured page (listScan, listXpath). */
export async function findList(lfp: unknown, html: string, role: string, lib: LibCall,
                               options: FindOptions = {}): Promise<ListMatch> {
  const threshold = options.threshold ?? THRESHOLD;
  const margin = options.margin ?? MARGIN;
  const visible = options.visible ?? null;
  if (!validList(lfp)) return refused('L2', 'the stored list fingerprint is not one this version reads');
  if (!placed(lfp)) return refused('L3', OLD_FINGERPRINT, null, { emptied: true });
  const unparsed = 'the captured page could not be parsed';
  if (typeof html !== 'string' || !html.trim()) return refused('L10', unparsed);
  let scan: ListScan | null;
  try {
    scan = readScan(await lib('listScan', { html, role, ids: containerIds(lfp.items[0]), container: holderValues(lfp) }));
  } catch {
    scan = null;
  }
  if (scan === null) return refused('L10', unparsed);
  const view = new View(scan);
  const scorer = new Scorer(lfp, view);
  const places = new Places(lfp, view);
  const recorded = scorer.items.length;
  const kept: Group[] = [];
  const others: [Group, string][] = [];
  for (const group of view.groups) {
    const why = elsewhere(view, group, view.holders, places);
    if (why) others.push([group, why]);   // another list: neither the answer nor a rival
    else kept.push(group);
  }
  const texts = lfp.items.some((item) => Boolean(item.text));
  const found = kept.filter((g) => !placeholder(view, g, texts));
  if (kept.length && !found.length) {
    return refused('L12', 'the only groups of ' + role + ' elements look like placeholders (no text where the recorded '
      + 'items had text, or marked aria-busy="true"), so the list may still be loading', null, { recorded });
  }
  let nearest: [Group, string] | null = null;
  let nearestScore = -Infinity;
  for (const pair of others) {
    const quick = scorer.quick(pair[0]);
    if (nearest === null || quick > nearestScore) [nearest, nearestScore] = [pair, quick];
  }
  if (!found.length && nearest) {
    const n = String(nearest[0].members.length);
    return refused('L11', 'each group of ' + role + ' elements on the page is another list. The one most like the list ('
      + n + ' elements) ' + nearest[1] + '. So ' + EMPTY_OR_BROKEN, 'each group of ' + role + ' elements on the page is '
      + 'another list: the one most like the list (' + n + ' elements) ' + nearest[1], { emptied: true, recorded });
  }
  // What a refusal adds when groups were left out as other lists: the one most like the list.
  const left = nearest ? '; left out as another list: a group of ' + nearest[0].members.length + ' elements that '
    + nearest[1] : '';
  if (!found.length) {
    return refused('L13', 'the page has no group of 2 or more ' + role + ' elements that repeat the same structure'
      + lone(scorer, view), null, { recorded });
  }
  // sorted(reverse=True), stable as Python's is: equal keys keep their order.
  const byQuick = found.map((g, i) => ({ g, i, key: scorer.quick(g) })).sort((a, b) => (b.key - a.key) || (a.i - b.i))
    .map((x) => x.g);
  const ranked = byQuick.slice(0, RANKED).map((g) => scorer.full(g)).map((g, i) => ({ g, i }))
    .sort((a, b) => (b.g.score - a.g.score) || (a.i - b.i)).map((x) => x.g);
  const build = async (group: Group): Promise<string | null> => {
    if (!group.built) {
      let read: { xpath?: unknown; css?: unknown; alike?: unknown } | null = null;
      try {
        read = await lib('listXpath', { html, members: group.members, container: group.container, steps: group.steps });
      } catch {
        read = null;
      }
      group.xpath = read && typeof read.xpath === 'string' ? read.xpath : null;
      group.css = read && typeof read.css === 'string' ? read.css : null;
      group.alike = read && Array.isArray(read.alike) && read.alike.length === 2 ? read.alike as [number, number] : null;
      group.built = true;
    }
    return group.xpath;
  };
  const shown: Group[] = [];
  for (const group of ranked) {
    if (visible !== null && shown.length < 3 && (await build(group)) !== null) {
      try {
        if ((await visible(group.xpath as string)) === false) continue;   // hidden on the live page
      } catch {
        // unknown: it stays
      }
    }
    shown.push(group);
  }
  if (!shown.length) {
    return refused('L14', 'no group of ' + role + ' elements is visible on the live page', null, { recorded });
  }
  const best = shown[0];
  const rivals = shown.slice(1).filter((g) => overlap(g, best) < 0.5);
  const rival = rivals.length ? rivals[0] : null;
  const runner = rival ? rival.score : 0.0;
  const label = places.labelOf(best);
  const n = String(best.members.length);
  const where = (group: Group) => {
    const text = places.labelOf(group);
    return text ? ' under ' + reprText(text) : '';
  };
  const container = best.container >= 0 && !['body', 'html'].includes(view.node(best.container).tag)
    ? view.container(best.container) : null;
  const memberTexts = best.members.slice(0, 2).map((el) => view.text(el));
  const refuse = (code: string, reason: string, fragment: string, held = 0): ListMatch => ({
    xpath: null, css: null, count: best.members.length, score: best.score, runnerUp: runner,
    reason: reason + left + lone(scorer, view), emptied: false, code, fragment: printable(fragment + left), best,
    runner: rival, held, recorded, label, container, memberTexts, alike: null,
  });
  if (best.score < threshold) {
    return refuse('L15', 'the best group (' + n + ' elements) scores ' + fmt2(best.score) + ', below the '
      + fmt2(threshold) + ' needed', 'the best group (' + n + ' elements' + where(best) + ') scores ' + fmt2(best.score)
      + ', below the ' + fmt2(threshold) + ' needed');
  }
  if (best.score - runner < margin) {
    return refuse('L16', 'two groups score alike (' + fmt2(best.score) + ' and ' + fmt2(runner) + '), so the list '
      + 'fingerprint does not tell them apart', 'two groups score alike (' + fmt2(best.score) + ' for ' + n + ' elements'
      + where(best) + ', ' + fmt2(runner) + ' for ' + (rival ? rival.members.length : 0) + ' elements'
      + (rival ? where(rival) : '') + '), so the list fingerprint does not tell them apart');
  }
  if (best.shape !== null && best.shape < SHAPE_MIN) {
    const shape = 'the elements of the best group are not all shaped like the recorded items (the least alike is '
      + fmt2(best.shape) + ', and ' + fmt2(SHAPE_MIN) + ' is needed)';
    return refuse('L17', shape, shape);
  }
  const need = Math.min(NEEDED, recorded);
  const [held, shared] = scorer.identified(best);
  if (held < need) {
    const head = 'the best group (score ' + fmt2(best.score) + ') ';
    if (held) {
      return refuse('L18a', head + 'has only ' + held + ' of the ' + recorded + ' recorded items among its elements (an '
        + 'element that shares an id, test id, name, link, label or text with one), and ' + need + ' are needed, so it '
        + 'may be another list in the same place', 'the best group (' + n + ' elements' + where(best) + ') holds only '
        + held + ' of the ' + recorded + ' items it recorded, and ' + need + ' are needed, so it may be another list in '
        + 'the same place', held);
    }
    if (shared) {
      return refuse('L18b', head + 'shares an id, test id, name, link, label or text only with every recorded item at '
        + 'once, which says what kind of list it is, not which items it holds', 'the best group shares an id, test id, '
        + 'name, link, label or text only with every recorded item at once, which says what kind of list it is, not '
        + 'which items it holds');
    }
    return refuse('L18c', head + 'shares no id, test id, name, link, label or text with the items that were recorded',
      'the best group shares no id, test id, name, link, label or text with the items it recorded, so a list whose '
      + "items change from run to run can't be found");
  }
  if ((await build(best)) === null) {
    const none = 'no selector without positions matches the best group and nothing else';
    return refuse('L19', none, none, held);
  }
  return { xpath: best.xpath, css: best.css, count: best.members.length, score: best.score, runnerUp: runner, reason: '',
           emptied: false, code: '', fragment: '', best, runner: rival, held, recorded, label, container, memberTexts,
           alike: best.alike ?? [1, 1] };
}

// --- what a person checks: state words, the evidence and its id --------------------------------
//
// A group find() takes is refused when its XPath picks it by a word that says a state the
// recorded list didn't have: the items of an emptied list often show in another list whose class
// or name says what happened to them (completed tasks, items saved for later). This says what the
// words mean and is no proof: a site whose class names say nothing gets no help from it. A list
// that was itself about a state (a "Completed" list, items with class done) has the word in what
// was recorded, so its redesigns are not refused for it.

export const STATE_WORDS: ReadonlySet<string> = new Set(['completed', 'complete', 'done', 'finished', 'archived', 'read',
  'seen', 'saved', 'removed', 'deleted', 'cancelled', 'canceled', 'expired', 'inactive', 'closed', 'resolved', 'past',
  'empty', 'unavailable', 'soldout']);

const LITERAL = /'([^']*)'|"([^"]*)"/g;
// Every C0 and C1 control character, DEL included.
const CONTROL = /[\x00-\x1f\x7f-\x9f]/;
const CONTROLS = /[\x00-\x1f\x7f-\x9f]/g;

/** The runs of the letters a to z in text, lower-cased ('saved-item': saved, item). */
function wordsOf(text: unknown): string[] {
  return typeof text === 'string' ? text.toLowerCase().match(/[a-z]+/g) ?? [] : [];
}

/** [token, word] when a string literal of xpath holds a word of STATE_WORDS that the recorded
 * list did not have, else null. token is the first such literal, stripped ('saved-item'), and
 * word the word ('saved'). What the list had: the words of the recorded items' classes, of the
 * recorded container's tag, id, test id, aria-label and classes, and of the list's label. */
export function stateWord(xpath: string | null, lfp: ListFingerprint): [string, string] | null {
  const had = new Set<string>();
  const add = (text: unknown) => wordsOf(text).forEach((word) => had.add(word));
  for (const item of lfp.items ?? []) for (const name of item.classes ?? []) add(name);
  const container: Partial<ContainerNode> = isRecord(lfp.container) ? lfp.container : {};
  for (const name of ['tag', 'id', 'test', 'aria'] as const) add(container[name]);
  for (const name of container.classes ?? []) add(name);
  add(lfp.label);
  for (const literal of (xpath || '').matchAll(LITERAL)) {
    const text = literal[1] !== undefined ? literal[1] : literal[2];
    for (const word of wordsOf(text)) {
      if (STATE_WORDS.has(word) && !had.has(word)) return [pyStrip(text), word];
    }
  }
  return null;
}

/** text with every control character (C0, C1 and DEL) as '?', so no escape sequence from a
 * page reaches a terminal. '' for anything but a string. */
export function printable(text: unknown): string {
  return typeof text === 'string' ? text.replace(CONTROLS, '?') : '';
}

/** Page text as a proposal keeps it: storedText() (whitespace collapsed, runs of 12 to 19
 * digits as #, at most 80 characters), then printable(). */
export function shown(text: unknown): string {
  return printable(storedText(text));
}

/** A stored container node as '<tag id="..." class="a b">', leaving out what it lacks; '' for
 * no container. */
export function shownContainer(node: unknown): string {
  if (!isRecord(node) || typeof node.tag !== 'string') return '';
  let out = '<' + printable(node.tag);
  if (node.id) out += ' id="' + printable(node.id).split('"').join("'") + '"';
  if (Array.isArray(node.classes) && node.classes.length) {
    out += ' class="' + printable(node.classes.join(' ')).split('"').join("'") + '"';
  }
  return out + '>';
}

/** True when two container nodes are not the same: their tags differ, or both have an id and
 * the ids differ, or either has stable classes and they share none. */
function containerDiffers(old: ContainerNode | null, next: ContainerNode | null): boolean {
  const a: Partial<ContainerNode> = old ?? {};
  const b: Partial<ContainerNode> = next ?? {};
  if (a.tag !== b.tag) return true;
  if (a.id && b.id && a.id !== b.id) return true;
  const mine = new Set(a.classes ?? []);
  const theirs = new Set(b.classes ?? []);
  return (mine.size > 0 || theirs.size > 0) && ![...mine].some((c) => theirs.has(c));
}

/** What a proposed list heal saves for a person to check (the proposal of its row in
 * overrides.json): where the list was when lfp was recorded and what it held (was), where the
 * group match found is and what it holds (now), what is not the same (differs: 'heading',
 * 'container'), and the id (proposalId). broken are the key's selectors that matched nothing,
 * page the page's path and at the time. Every string from the page goes through shown(). */
export interface Evidence {
  id: string;
  at: string;
  by: string;
  page: string;
  count: number;
  found: number;
  recorded: number;
  score: number;
  next: number;
  was: { selectors: string[]; count: number; label: string; container: string; items: string[] };
  now: { label: string; container: string; alike: number[]; items: string[] };
  differs: string[];
}

export function proposalEvidence(key: string, lfp: ListFingerprint, match: ListMatch, xpath: string, css: string | null,
                                 broken: string[], page: string, at: string): Evidence {
  const old = isRecord(lfp.container) ? lfp.container : null;
  const was = { selectors: broken.map(String), count: lfp.count, label: shown(lfp.label),
                container: shownContainer(old), items: lfp.items.filter((item) => item.text).map((item) => shown(item.text))
                  .slice(0, 2) };
  const now = { label: shown(match.label), container: shownContainer(match.container), alike: [...(match.alike ?? [1, 1])],
                items: match.memberTexts.map(shown) };
  const differs: string[] = [];
  if (labelKey(was.label) !== labelKey(now.label)) differs.push('heading');
  if (containerDiffers(old, match.container)) differs.push('container');
  const evidence: Evidence = { id: '', at, by: 'fingerprint', page, count: match.count, found: match.held,
                               recorded: match.recorded, score: pyRound(match.score, 2), next: pyRound(match.runnerUp, 2),
                               was, now, differs };
  evidence.id = proposalId(key, xpath, css, evidence);
  return evidence;
}

/** The id of a proposal: the first 8 hex digits of the SHA-256 of everything mendwright lists
 * shows of it but the time and the scores, so it changes whenever that does. The JSON is the
 * Python package's json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False), so a
 * proposal has the same id whichever package made it. */
export function proposalId(key: string, xpath: string, css: string | null, evidence: Partial<Evidence>): string {
  return sha256Prefix(pyJsonSorted([key, xpath, css || '', evidence.page ?? null, evidence.count ?? null,
    evidence.found ?? null, evidence.recorded ?? null, evidence.was ?? null, evidence.now ?? null,
    evidence.differs ?? null]));
}

/** '1st', '2nd', '3rd', '4th', '11th', ... */
export function ordinal(n: number): string {
  if (n % 100 >= 10 && n % 100 <= 20) return n + 'th';
  const suffix = ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string | undefined>)[n % 10];
  return n + (suffix ?? 'th');
}

// --- the texts of a lookup with nonempty: true (the Python package's coordinator.py) --------------

/** A key as the list messages quote it, with its control characters as ?. */
export function reprKey(key: unknown): string {
  return "'" + printable(String(key)) + "'";
}

/** n elements, in words: '1 element', '3 elements'. */
export function elementsText(n: number): string {
  return n + (n === 1 ? ' element' : ' elements');
}

/** '2026-10-10T09:30:00+00:00' as '2026-10-10 09:30:00 UTC' (the Python package's cli._when). */
export function whenText(ts: unknown): string {
  if (typeof ts !== 'string' || !ts) return 'never';
  const text = ts.split('T').join(' ');
  for (const suffix of ['+00:00', 'Z']) if (text.endsWith(suffix)) return text.slice(0, -suffix.length) + ' UTC';
  return text;
}

const PLAIN_KEY = /^[A-Za-z0-9_.-]+$/;
const SHELL_SPECIAL = /["`$\\]/;

/** [word, note]: key as a command a message prints puts it for a shell (the Python package's
 * cli.command_key). As it is when it has only letters, digits, _ . and -; else in double quotes
 * when it has no double quote, backtick, dollar, backslash or control character; else in single
 * quotes when it has no single quote or control character; else KEY, with note saying to quote
 * the key, which shows it with Python's repr() and its control characters as ?. */
export function commandKey(key: unknown): [string, string] {
  const text = String(key);
  if (text && PLAIN_KEY.test(text)) return [text, ''];
  const control = CONTROL.test(text);
  if (!control && !SHELL_SPECIAL.test(text)) return ['"' + text + '"', ''];
  if (!control && !text.includes("'")) return ["'" + text + "'", ''];
  return ['KEY', ' (put the key ' + printable(reprText(text)) + ' in quotes for your shell)'];
}

/** A refusal of a list looked up with nonempty: true when nothing was looked for. */
export function nonemptyUnsearched(key: string, text: string): string {
  return 'nonempty: true says ' + reprKey(key) + ' has items here, and none of its selectors matches. ' + text
    + ' If the selector broke, change it in your code.';
}

/** A refusal of a list looked up with nonempty: true after mendwright looked for it on the page:
 * fragment says what it found, or why it found nothing. */
export function nonemptySearched(key: string, count: unknown, fragment: string): string {
  return reprKey(key) + ' matched ' + hadText(count) + ' when its list fingerprint was recorded, and none of its '
    + 'selectors matches now. nonempty: true says the list has items here, so mendwright looked for it on the page, but '
    + fragment + '. If the selector broke, change it in your code. If the list can be empty here, remove nonempty: true.';
}

/** Why mendwright did not look: no list fingerprint. */
export const NEVER_SEEN = 'mendwright has never seen this list match 2 or more elements, so it has nothing to look '
  + "for. It records a list each time the list's selector matches 2 or more.";
/** Why mendwright did not look: the list was inside a shadow root. */
export const IN_SHADOW = 'The list was inside a shadow root when its list fingerprint was recorded, and mendwright '
  + "looks for a list only in the page's own HTML, where it would find another list's items, so it did not look.";
/** Why mendwright did not look: the list fingerprint holds no place. */
export const PLACELESS = 'its list fingerprint was recorded by mendwright 0.8.0 or before a call with nonempty: true '
  + "matched the list, so it doesn't hold where the list was or its heading, and another list that holds its items "
  + "can't be told from it. mendwright records them the next time the list matches in a call with nonempty: true";

/** Why mendwright did not look: the fingerprint tier is off, turned off by offBy. */
export function tierOffText(offBy: string): string {
  return 'mendwright looks for a list only while the fingerprint tier is on, and ' + offBy + ' turns it off.';
}

/** Why a group found was refused: its XPath picks it by token, whose word names a state the
 * recorded list didn't have. */
export function stateWordReason(xpath: string, token: string, word: string): string {
  return 'the group it found, ' + xpath + ', is picked by ' + reprText(printable(token)) + ', and ' + reprText(word)
    + " names a state the recorded list didn't have, so these are most likely the list's items in another state, or "
    + 'another list, such as completed tasks or items saved for later';
}

/** Why a group found was refused by the list gate (why). */
export function gateRefusal(xpath: string, why: string): string {
  return 'the group it found, ' + xpath + ", failed mendwright's list checks (" + why + ')';
}

/** Why a lookup in dry run saved nothing. */
export function dryRunReason(key: string, xpath: string): string {
  return 'dry-run mode is on, so the list mendwright found for ' + reprKey(key) + ', ' + xpath + ', was checked but '
    + 'not saved. Turn off dryRun to save it for you to check.';
}

/** Why the store could not save a proposal (why), so nothing was looked for. */
export function unsavedReason(key: string, why: string): string {
  return nonemptyUnsearched(key, "The store passed to mendwright.open() can't save a list heal for you to check ("
    + why + '), so mendwright did not look.');
}

/** ', under 'LABEL', in CONTAINER', for what of them there is. */
function placeClause(label: string, container: string): string {
  return (label ? ', under ' + reprText(label) : '') + (container ? ', in ' + container : '');
}

/** The reason a lookup that made a proposal throws with (the Python package's
 * HealCoordinator._proposal_message): where the list was, what was found, the XPath and its CSS
 * form, what is not the same, and the commands to check it, accept it (with the proposal's id)
 * and write it into your code. flag is the --store of the commands; accepted is another list
 * heal of the key a person accepted, which is not this one, if any. */
export function proposalMessage(key: string, lfp: ListFingerprint, xpath: string, css: string | null, role: string,
                                evidence: Evidence, flag: string,
                                accepted: { xpath: string; acceptedAt?: unknown } | null): string {
  const { was, now } = evidence;
  const [word, note] = commandKey(key);
  const alike = now.alike && now.alike.length === 2 ? now.alike : [1, 1];
  let here = placeClause(now.label, now.container);
  if (alike[1] > 1) here += ', the ' + ordinal(alike[0]) + ' of ' + alike[1] + ' alike';
  if (here) here += ',';
  let differs = '';
  if (evidence.differs.includes('heading')) {
    differs += ' The heading is not the same: the list was under ' + (was.label ? reprText(was.label) : 'no heading')
      + ', and these are under ' + (now.label ? reprText(now.label) : 'no heading') + '.';
  }
  if (evidence.differs.includes('container')) {
    differs += ' The container is not the same: the list was in ' + (was.container || '<body>') + ', and these are in '
      + (now.container || '<body>') + '.';
  }
  const other = accepted ? ' You accepted another list heal for ' + reprKey(key) + ' on '
    + (accepted.acceptedAt ? whenText(accepted.acceptedAt) : 'date unknown') + ', ' + accepted.xpath + ', which matches '
    + 'nothing now. mendwright fix writes neither until you accept this one or reject it.' : '';
  return reprKey(key) + ' matched ' + hadText(lfp.count) + ' when its list fingerprint was recorded'
    + placeClause(was.label, was.container) + ', and none of its selectors matches now. nonempty: true says the list has '
    + 'items here, so mendwright looked for it and found ' + evidence.count + ' ' + role + ' elements' + here
    + ' that hold ' + evidence.found + ' of the ' + evidence.recorded + ' items it recorded: ' + xpath
    + (css ? ' (as CSS: ' + css + ')' : '') + '.' + differs + ' mendwright never uses a list it finds, because the items '
    + 'of an emptied list can show in another list on the page. If this is the list, look at it with: mendwright lists'
    + flag + '. Accept it with: mendwright lists accept ' + word + ' ' + evidence.id + flag + note + '. Then write it '
    + 'into your code with: mendwright fix --apply' + flag + '.' + other + ' If the list can be empty here, remove '
    + 'nonempty: true.';
}

/** The one WARNING line of a proposal: what was found, and that it is not used. old are the
 * selectors it replaces, seconds how long the search took. */
export function foundLine(key: string, count: number, role: string, label: string, old: string[], xpath: string,
                          seconds: number): string {
  const was = old.length ? old[0] + (old.length > 1 ? ' (+' + (old.length - 1) + ' more)' : '') : '(no stored selector)';
  return 'found a list for ' + reprText(key) + ' (' + count + ' ' + role + ' elements'
    + (label ? ' under ' + reprText(label) : '') + '): ' + was + ' -> ' + xpath + ' (' + pyFixed(seconds, 1)
    + 's). It is not used. Check it with: mendwright lists';
}

/** Why list key, looked up with nonempty: true, throws when a list heal a person accepted for
 * it (row) matches live elements on the page now: mendwright takes a list only from the
 * selectors in the code, and mendwright fix puts it there. count is how many elements the list
 * had when it was recorded. */
export function waitsForFixMessage(key: string, count: unknown, row: { xpath?: unknown; acceptedAt?: unknown },
                                   live: number, flag: string): string {
  const when = row.acceptedAt;
  return reprKey(key) + ' matched ' + hadText(count) + ' when its list fingerprint was recorded, and none of its '
    + 'selectors matches now. You accepted a list heal for it on '
    + (typeof when === 'string' && when ? whenText(when) : 'date unknown') + ', '
    + (row.xpath === undefined || row.xpath === null ? 'None' : String(row.xpath)) + ', which matches '
    + elementsText(live) + ' now, but mendwright takes a list only from the selectors in your code. Write it there '
    + 'with: mendwright fix --apply' + flag + '. If the list can be empty here, remove nonempty: true.';
}

/** The INFO line of a list that came while mendwright looked for it. */
export function cameLine(key: string, selector: string): string {
  return reprText(key) + ' came while mendwright looked for it, so ' + selector + ' was used as it is.';
}

/** Why a list that came while mendwright looked for it, and matches nothing again, was refused. */
export function cameAndWentWhy(selector: string): string {
  return 'the selector in your code, ' + selector + ', matched while mendwright looked and matches nothing now';
}
