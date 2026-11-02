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

import { fromRaw as fingerprintFromRaw, norm, stableClasses, storedText, valid as validFingerprint, VALUE_LIMIT,
  generatedId, type Fingerprint } from './fingerprint';
import { isDigitChar, pyCompare, pyRepr, pySlice, pySorted, pySplit, pyStrip } from './py';
import { roleFor } from './roles';

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
