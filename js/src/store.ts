// The selector store, .mendwright/overrides.json: the same schema-2 file the Python package
// reads and writes (mendwright/store.py), so a team that runs both shares one store. Each key
// holds an ordered list of candidate selectors and its metadata. A port of SelectorStore
// for a store with no seed file:
//
// - Candidates from code come first, in the order the code passes them; older code
//   selectors become 'superseded', and heals stay behind as fallbacks.
// - Only 'active' candidates are tried. 'quarantined' and 'superseded' ones stay in the file,
//   and so do the list heals ('proposed', 'accepted') the Python package keeps for a person
//   to check: this package never tries or changes those.
// - Per-key metadata: expectedRole, expectedText, destructive, lastPage and pages, lastUrl,
//   steps, intent, fingerprint, listFingerprint and shadowHosts (where the element was
//   found, see shadow.ts), written only when one of them changes.
// - A candidate the Python package saved inside an iframe or an open shadow root keeps its
//   frameXPath, css and shadowHosts, in a three-way merge of their own.
// - Writes take the lock file overrides.json.lock, take in what other processes (Python or
//   TypeScript, other Playwright Test workers) wrote since the last read with a three-way
//   merge, and replace the file atomically. On a conflict 'quarantined' wins, then
//   'accepted', and destructive: true wins.
// - A file that exists but is not valid JSON, or not a selector file, raises
//   StoreReadError when the store loads. It is never read as empty, because the next write
//   would replace it and lose every saved heal and quarantine. If a write fails, one
//   warning is logged and the store keeps working in memory.

import * as crypto from 'node:crypto';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { StoreReadError } from './errors';
import { valid as validFingerprint, type Fingerprint } from './fingerprint';
import { pageUrl } from './flow';
import { log } from './log';
import { compactJson, nowIso } from './py';
import { inferRole } from './roles';
import { frameSpelling } from './selectors';
import { savedHosts, type Hosts } from './shadow';

export interface Candidate {
  xpath: string;
  source?: string;
  addedAt?: string | null;
  lastSucceededAt?: string | null;
  status?: string;
  frameXPath?: string;
  css?: string;
  shadowHosts?: Hosts;
  list?: boolean;
  proposal?: Record<string, unknown>;
  acceptedAt?: string;
  [field: string]: unknown;
}

interface Entry {
  expectedRole?: string;
  expectedText?: string[];
  destructive?: boolean;
  maxCandidates?: number;
  lastPage?: string;
  pages?: string[];
  steps?: string[];
  intent?: string;
  fingerprint?: Fingerprint;
  listFingerprint?: unknown;
  lastUrl?: string;
  shadowHosts?: Hosts;
  candidates: Candidate[];
  [field: string]: unknown;
}

export interface StoreData {
  schema: 2;
  selectors: Record<string, Entry>;
}

export interface KeyMeta {
  expectedRole: string;
  expectedText: string[];
  destructive: boolean;
  maxCandidates: number;
  lastPage: string | null;
  pages: string[];
  steps: string[];
  intent: string | null;
  fingerprint: Fingerprint | null;
  listFingerprint: unknown;
}

/** Metadata fields saved in the store file next to the candidates. */
export const META_FIELDS = ['expectedRole', 'expectedText', 'destructive', 'lastPage', 'pages', 'intent',
  'fingerprint', 'steps', 'listFingerprint', 'lastUrl', 'shadowHosts'] as const;
type MetaField = typeof META_FIELDS[number];
type Meta = Partial<Record<MetaField, unknown>>;

export const MAX_PAGES = 5;
const LIST_WAITING = ['proposed', 'accepted'];
const MAX_LIST_HEALS = 3;
const PLACE_FIELDS = ['frameXPath', 'css', 'shadowHosts'] as const;
type Place = [string, string, string];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function knownRole(role: unknown): boolean {
  return Boolean(role) && role !== 'unknown';
}

function empty(): StoreData {
  return { schema: 2, selectors: {} };
}

function clone<T>(value: T): T {
  return value === undefined ? value : JSON.parse(JSON.stringify(value));
}

/** The time of a lock wait or a retry, without an event loop: the store's writes are
 * synchronous, as the Python package's are. */
function sleepMs(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/** A path with '~' expanded. */
export function expandPath(file: string): string {
  if (file === '~') return os.homedir();
  if (file.startsWith('~/') || file.startsWith('~\\')) return path.join(os.homedir(), file.slice(2));
  return file;
}

const RETRY_CODES = new Set(['EPERM', 'EACCES', 'EBUSY']);

/** The file's text, or null when it does not exist. A read Windows refuses for a moment
 * (another process replacing the file) is retried. Text that is not UTF-8 raises. */
function readText(file: string): string | null {
  let delay = 10;
  for (let attempt = 0; attempt < 8; attempt++) {
    let bytes: Buffer;
    try {
      bytes = fs.readFileSync(file);
    } catch (error: any) {
      if (error?.code === 'ENOENT' || error?.code === 'ENOTDIR') return null;
      if (RETRY_CODES.has(error?.code) && attempt < 7) {
        sleepMs(delay);
        delay = Math.min(delay * 2, 200);
        continue;
      }
      throw error;
    }
    try {
      const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
    } catch (error) {
      throw new StoreReadError('mendwright: ' + file + ' is not UTF-8 text (' + String((error as Error).message)
        + '). Save it as UTF-8 (PowerShell 5 writes UTF-16 with > and Out-File), or delete it.');
    }
  }
  return null;
}

/** Give a heal saved inside an iframe the spelling the Python package saves it with since
 * 0.6.1 (selectors.frameSpelling). */
function respellFrameHeals(data: StoreData): StoreData {
  for (const entry of Object.values(data.selectors ?? {})) {
    const rows = isRecord(entry) ? entry.candidates : null;
    if (!Array.isArray(rows)) continue;
    const texts = new Set(rows.filter(isRecord).map((c) => c.xpath));
    for (const c of rows) {
      if (!(isRecord(c) && c.source === 'heal' && c.frameXPath && typeof c.xpath === 'string')) continue;
      let spelled: string | null = null;
      try {
        spelled = frameSpelling(c.xpath);
      } catch {
        continue;
      }
      if (spelled && spelled !== c.xpath && !texts.has(spelled)) {
        texts.delete(c.xpath);
        c.xpath = spelled;
        texts.add(spelled);
      }
    }
  }
  return data;
}

/** Convert a schema-1 file ({"selectors": [{key, xpath}, ...]}) into schema 2. */
function migrateSchema1(data: Record<string, unknown>, destructiveKeys: Set<string>): StoreData {
  const out: Record<string, Entry> = {};
  for (const row of (data.selectors as unknown[]) ?? []) {
    if (!isRecord(row)) continue;
    const key = row.key;
    const xpath = row.xpath;
    if (typeof key !== 'string' || !key || typeof xpath !== 'string' || !xpath) continue;
    const entry = out[key] ?? (out[key] = {
      expectedRole: inferRole(xpath), expectedText: [], destructive: destructiveKeys.has(key),
      maxCandidates: 5, stage: row.stage ?? null, action: row.action ?? null, candidates: [],
    });
    if (entry.candidates.some((c) => c.xpath === xpath)) continue;
    const candidate: Candidate = { xpath, source: 'seed', addedAt: null, lastSucceededAt: null, status: 'active' };
    if (row.frameXPath) candidate.frameXPath = row.frameXPath as string;
    entry.candidates.push(candidate);
  }
  return { schema: 2, selectors: out };
}

/** [schema-2 data, true when the file itself was schema 2]. A missing or blank file is an
 * empty store. */
export function readStoreFile(file: string, destructiveKeys: Set<string> = new Set()): [StoreData, boolean] {
  const text = readText(file);
  if (text === null || !text.trim()) return [empty(), false];
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new StoreReadError('mendwright: ' + file + ' is not valid JSON (' + (error as Error).message
      + '). Fix it (a merge conflict or a hand edit?) or delete it.');
  }
  if (isRecord(data) && Object.keys(data).length === 0) return [empty(), false];
  const selectors = isRecord(data) ? data.selectors : undefined;
  if (isRecord(data) && data.schema === 2 && isRecord(selectors)) {
    return [respellFrameHeals(data as unknown as StoreData), true];
  }
  if (isRecord(data) && (data.schema === undefined || data.schema === null || data.schema === 1)
      && Array.isArray(selectors)) {
    return [migrateSchema1(data, destructiveKeys), false];
  }
  throw new StoreReadError('mendwright: ' + file + ' is not a selector file (expected {"schema": 2, '
    + '"selectors": {...}}). Point mendwright at the right file, or delete this one.');
}

function pagesOf(value: unknown): string[] {
  if (Array.isArray(value) && value.every((p) => typeof p === 'string' && p)) return value.slice(0, MAX_PAGES);
  return [];
}

function validUrl(value: unknown): value is string {
  return typeof value === 'string' && Boolean(value) && pageUrl(value) === value;
}

// The Python package's lists.valid(): a list fingerprint it can score. This package never
// makes or uses one, but keeps a valid one when it writes the file.
function validListFingerprint(lfp: unknown): boolean {
  return isRecord(lfp) && lfp.v === 1 && typeof lfp.count === 'number' && Number.isInteger(lfp.count)
    && lfp.count >= 2 && Array.isArray(lfp.items) && lfp.items.length > 0 && lfp.items.every(validFingerprint);
}

/** The saved metadata of one entry of the file, with malformed values dropped. */
function metaFields(entry: unknown): Meta {
  const out: Meta = {};
  if (!isRecord(entry)) return out;
  if (typeof entry.expectedRole === 'string' && entry.expectedRole) out.expectedRole = entry.expectedRole;
  if (Array.isArray(entry.expectedText) && entry.expectedText.every((t) => typeof t === 'string')) {
    out.expectedText = [...entry.expectedText];
  }
  if (typeof entry.destructive === 'boolean') out.destructive = entry.destructive;
  if (typeof entry.lastPage === 'string' && entry.lastPage) out.lastPage = entry.lastPage;
  if (validUrl(entry.lastUrl)) out.lastUrl = entry.lastUrl;
  const pages = pagesOf(entry.pages);
  if (pages.length) out.pages = pages;
  const steps = pagesOf(entry.steps);
  if (steps.length) out.steps = steps;
  if (typeof entry.intent === 'string' && entry.intent.trim()) out.intent = entry.intent;
  if (validFingerprint(entry.fingerprint)) out.fingerprint = entry.fingerprint;
  if (validListFingerprint(entry.listFingerprint)) out.listFingerprint = entry.listFingerprint;
  // A malformed value keeps a host nothing tells apart (null), so the key is never taken to
  // be on the page because of it.
  const hosts = savedHosts(entry.shadowHosts);
  if (hosts.length) out.shadowHosts = hosts;
  return out;
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function evidenceOf(c: unknown): Record<string, unknown> | null {
  const proposal = isRecord(c) ? c.proposal : null;
  if (isRecord(proposal) && typeof proposal.id === 'string' && typeof proposal.at === 'string'
      && typeof proposal.count === 'number' && Number.isInteger(proposal.count)) {
    return proposal;
  }
  return null;
}

function isListHeal(c: Candidate): boolean {
  return c.list === true && LIST_WAITING.includes(c.status as string);
}

/** A candidate's place field as it is compared: '' when it has none, its shadowHosts as
 * JSON. */
function placeValue(c: Candidate, field: typeof PLACE_FIELDS[number]): string {
  if (field === 'shadowHosts') {
    const hosts = savedHosts(c.shadowHosts);
    return hosts.length ? JSON.stringify(hosts) : '';
  }
  return (c[field] as string) || '';
}

function placeOf(c: Candidate): Place {
  return PLACE_FIELDS.map((field) => placeValue(c, field)) as Place;
}

function mergePlace(local: Candidate, theirs: Candidate, base: Place | undefined): void {
  PLACE_FIELDS.forEach((field, i) => {
    if (base === undefined) {
      if (field in theirs && !(field in local)) local[field] = clone(theirs[field]) as any;
    } else if (placeValue(local, field) === base[i] && placeValue(theirs, field) !== base[i]) {
      if (field === 'shadowHosts') local.shadowHosts = [...savedHosts(theirs.shadowHosts)];
      else local[field] = (theirs[field] as string) || '';
    }
  });
}

/** One candidate's status after a three-way merge. When both sides changed it,
 * 'quarantined' wins, then 'accepted', else this store's. */
export function mergeStatus(mine: string, theirs: string, base: string | undefined): string {
  if (mine === theirs) return mine;
  if (base !== undefined && mine === base) return theirs;
  if (base !== undefined && theirs === base) return mine;
  if (mine === 'quarantined' || theirs === 'quarantined') return 'quarantined';
  if (mine === 'accepted' || theirs === 'accepted') return 'accepted';
  return mine;
}

function mergeMeta(mine: Meta, theirs: Meta, base: Meta): Meta {
  const out: Meta = {};
  for (const field of META_FIELDS) {
    const m = mine[field];
    const t = theirs[field];
    const b = base[field];
    let value: unknown;
    if (same(m, b)) value = t;
    else if (same(t, b)) value = m;
    else if (field === 'destructive') value = Boolean(m) || Boolean(t);
    else if (field === 'pages' || field === 'steps') {
      const both = [...new Set([...pagesOf(m), ...pagesOf(t)])].slice(0, MAX_PAGES);
      value = both.length ? both : undefined;
    } else value = m;
    if (value !== undefined && value !== null) out[field] = value;
  }
  return out;
}

function proposalOrder(c: Candidate): [boolean, string, string] {
  const evidence = evidenceOf(c);
  return [evidence !== null, evidence ? (evidence.at as string) : '', (c.addedAt as string) || ''];
}

function compareTuples(a: unknown[], b: unknown[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const x = a[i] as any;
    const y = b[i] as any;
    if (x < y) return -1;
    if (x > y) return 1;
  }
  return a.length - b.length;
}

/** sorted(items, key=key, reverse=True), stable as Python's is. */
function sortedDesc<T>(items: T[], key: (item: T) => unknown[]): T[] {
  return items.map((item, i) => ({ item, i, k: key(item) }))
    .sort((a, b) => compareTuples(b.k, a.k) || a.i - b.i)
    .map((x) => x.item);
}

export interface StoreOptions {
  destructiveKeys?: Iterable<string>;
  readOnly?: boolean;
}

export class SelectorStore {
  static lockTimeoutMs = 15_000;
  static lockStaleAfterMs = 10_000;

  readonly overridePath: string;
  private data: StoreData;
  private readonly readOnly: boolean;
  private readonly destructiveKeys: Set<string>;
  private warned = false;
  private batchDepth = 0;
  private batchWanted = false;
  private readonly guessed = new Set<string>();
  private readonly lastSuccessByKey = new Map<string, Candidate>();
  private readonly preferred = new Map<string, string[]>();
  private readonly dirty = new Set<string>();
  // (key, xpath) of the list heals setCodeCandidates() took over as code: the file's "list",
  // proposal and acceptedAt are not taken back for them.
  private readonly delisted = new Set<string>();
  private meta = new Map<string, Meta>();
  private base = new Map<string, string>();
  private basePlace = new Map<string, Place>();
  private baseMeta = new Map<string, Meta>();

  constructor(data: StoreData, overridePath: string, options: StoreOptions & { override?: StoreData | null } = {}) {
    this.data = data;
    this.overridePath = path.resolve(expandPath(overridePath));
    this.readOnly = Boolean(options.readOnly);
    this.destructiveKeys = new Set(options.destructiveKeys ?? []);
    for (const [key, entry] of Object.entries(options.override?.selectors ?? {})) {
      const fields = metaFields(entry);
      if (Object.keys(fields).length) {
        this.meta.set(key, fields);
        this.overlayMeta(key, fields);
      }
    }
    this.rememberBase(options.override ?? { schema: 2, selectors: {} });
  }

  private static pair(key: string, xpath: string): string {
    return JSON.stringify([key, xpath]);
  }

  private overlayMeta(key: string, fields: Meta): void {
    const e = this.data.selectors[key];
    if (!isRecord(e)) return;
    if ('expectedRole' in fields) {
      e.expectedRole = fields.expectedRole as string;
      this.guessed.delete(key);
    }
    if ('expectedText' in fields) e.expectedText = [...(fields.expectedText as string[])];
    if ('destructive' in fields) e.destructive = fields.destructive as boolean;
    for (const name of ['lastPage', 'pages', 'steps', 'intent', 'fingerprint', 'listFingerprint', 'lastUrl'] as const) {
      if (name in fields) e[name] = (name === 'pages' || name === 'steps') ? [...(fields[name] as string[])] : fields[name] as any;
    }
    if ('shadowHosts' in fields) e.shadowHosts = [...(fields.shadowHosts as Hosts)];
    else delete e.shadowHosts;     // the element is on the page now, or nothing was recorded
  }

  /** True when destructiveKeys marks key destructive: nothing in the store can clear it. */
  isLockedDestructive(key: string): boolean {
    return this.destructiveKeys.has(key);
  }

  /** Whether the store has an entry for key. */
  has(key: string): boolean {
    return key in this.data.selectors;
  }

  keys(): string[] {
    return Object.keys(this.data.selectors);
  }

  metaOf(key: string): KeyMeta {
    const e = this.entry(key);
    return {
      expectedRole: e.expectedRole ?? 'unknown',
      expectedText: e.expectedText ?? [],
      destructive: Boolean(e.destructive) || this.isLockedDestructive(key),
      maxCandidates: e.maxCandidates ?? 5,
      lastPage: typeof e.lastPage === 'string' ? e.lastPage : null,
      pages: pagesOf(e.pages),
      steps: pagesOf(e.steps),
      intent: typeof e.intent === 'string' && e.intent ? e.intent : null,
      fingerprint: validFingerprint(e.fingerprint) ? e.fingerprint : null,
      listFingerprint: validListFingerprint(e.listFingerprint) ? e.listFingerprint : null,
    };
  }

  private byRecency(key: string): Candidate[] {
    const e = this.data.selectors[key];
    if (!e) return [];
    const active = (e.candidates ?? []).filter((c) => (c.status ?? 'active') === 'active');
    return sortedDesc(active, (c) => [c.lastSucceededAt || '']);
  }

  /** Active candidates: the selectors the code passed in this process first, in that
   * order, then the others, most recently succeeded first. */
  candidates(key: string): Candidate[] {
    let ranked = this.byRecency(key);
    const preferred = this.preferred.get(key);
    if (preferred && preferred.length) {
      const first = preferred.map((x) => ranked.find((c) => c.xpath === x)).filter((c): c is Candidate => !!c);
      ranked = first.concat(ranked.filter((c) => !preferred.includes(c.xpath)));
    }
    return ranked;
  }

  private entry(key: string): Entry {
    let e = this.data.selectors[key];
    if (!e) {
      e = { expectedRole: 'unknown', expectedText: [], destructive: this.destructiveKeys.has(key),
            maxCandidates: 5, stage: null, action: null, candidates: [] };
      this.data.selectors[key] = e;
    }
    if (!Array.isArray(e.candidates)) e.candidates = [];
    return e;
  }

  private find(key: string, xpath: string): Candidate | null {
    return this.entry(key).candidates.find((c) => c.xpath === xpath) ?? null;
  }

  /** Make selectors (in order) the selectors the code passes for key: each becomes an
   * active 'code' candidate, and older code candidates that are no longer passed become
   * 'superseded'. Heals stay as fallbacks. */
  setCodeCandidates(key: string, selectors: string[]): void {
    const entry = this.entry(key);
    const wanted = [...new Set(selectors)];
    let changed = false;
    for (const xpath of wanted) {
      const c = this.find(key, xpath);
      if (c === null) {
        entry.candidates.push({ xpath, source: 'code', addedAt: nowIso(), lastSucceededAt: null, status: 'active' });
      } else if (c.source === 'heal' || (c.status ?? 'active') !== 'active') {
        if (c.source === 'heal') c.source = 'code';
        // A heal inside a shadow root that mendwright fix wrote into the code as its
        // Playwright chain: the code's selector runs as Playwright runs it, as the Python
        // package does. [] stays as a key, so the merge does not take the hosts back.
        if (savedHosts(c.shadowHosts).length) c.shadowHosts = [];
        if (c.list === true && (LIST_WAITING.includes(c.status as string) || 'proposal' in c || 'acceptedAt' in c)) {
          delete c.list;
          delete c.proposal;
          delete c.acceptedAt;
          this.delisted.add(SelectorStore.pair(key, xpath));
        }
        c.status = 'active';
      } else {
        continue;
      }
      this.dirty.add(SelectorStore.pair(key, xpath));
      changed = true;
    }
    for (const c of entry.candidates) {
      if (c.source === 'code' && !wanted.includes(c.xpath) && (c.status ?? 'active') === 'active') {
        c.status = 'superseded';
        this.dirty.add(SelectorStore.pair(key, c.xpath));
        changed = true;
      } else if (c.list === true && c.status === 'accepted' && !wanted.includes(c.xpath)
                 && typeof c.css === 'string' && wanted.includes(c.css)) {
        c.status = 'superseded';
        this.dirty.add(SelectorStore.pair(key, c.xpath));
        changed = true;
      }
    }
    this.preferred.set(key, wanted);
    if (changed) this.persist();
  }

  /** Set a key's metadata; undefined leaves a field as it is. The file is written only when
   * a saved field changes. destructive: false clears a flag saved this way, never one from
   * destructiveKeys. shadowHosts is where the element was found: the selectors of the shadow
   * hosts around it, outermost first (null for a host nothing tells apart), or [] for the
   * page, which removes the field (see shadow.ts). */
  setMeta(key: string, fields: { role?: string; text?: string | string[]; destructive?: boolean;
                                 intent?: string; fingerprint?: Fingerprint; shadowHosts?: Hosts }): void {
    const e = this.entry(key);
    const changed: Meta = {};
    let cleared = false;
    if (fields.shadowHosts !== undefined) {
      if (!Array.isArray(fields.shadowHosts)) {
        throw new TypeError('mendwright: setMeta({ shadowHosts }) takes a list of host selectors, got '
          + JSON.stringify(fields.shadowHosts) + '.');
      }
      const hosts = savedHosts([...fields.shadowHosts]);
      if (hosts.length) {
        e.shadowHosts = hosts;
        changed.shadowHosts = [...hosts];
      } else {
        delete e.shadowHosts;
        cleared = true;
      }
    }
    if (fields.intent !== undefined) {
      if (typeof fields.intent !== 'string' || !fields.intent.trim()) {
        throw new TypeError('mendwright: intent must be a non-empty string, got ' + JSON.stringify(fields.intent) + '.');
      }
      e.intent = fields.intent.split(/\s+/).filter(Boolean).join(' ');
      changed.intent = e.intent;
    }
    if (fields.fingerprint !== undefined) {
      if (!validFingerprint(fields.fingerprint)) {
        throw new TypeError('mendwright: setMeta({ fingerprint }) takes a fingerprint from mendwright\'s fingerprint module.');
      }
      e.fingerprint = fields.fingerprint;
      changed.fingerprint = fields.fingerprint;
    }
    if (fields.role !== undefined) {
      e.expectedRole = fields.role;
      changed.expectedRole = fields.role;
      this.guessed.delete(key);
    }
    if (fields.text !== undefined) {
      e.expectedText = Array.isArray(fields.text) ? [...fields.text] : [fields.text];
      changed.expectedText = [...e.expectedText];
    }
    if (fields.destructive !== undefined) {
      e.destructive = Boolean(fields.destructive);
      changed.destructive = Boolean(fields.destructive);
    }
    const saved = this.meta.get(key) ?? {};
    const dropped = cleared && 'shadowHosts' in saved;
    if (Object.entries(changed).some(([f, v]) => !same(saved[f as MetaField], v)) || dropped) {
      const next: Meta = { ...saved, ...changed };
      if (cleared) delete next.shadowHosts;
      this.meta.set(key, next);
      this.persist();
    }
  }

  /** Record that key's element was just found on the page path (see flow.pagePath). */
  setLastPage(key: string, page: string | null): void {
    if (typeof page !== 'string' || !page) return;
    const e = this.entry(key);
    const pages = [page, ...pagesOf(e.pages).filter((p) => p !== page)].slice(0, MAX_PAGES);
    e.lastPage = page;
    e.pages = pages;
    const saved = this.meta.get(key) ?? {};
    if (saved.lastPage !== page || !same(saved.pages, pages)) {
      this.meta.set(key, { ...saved, lastPage: page, pages: [...pages] });
      const before = new Set(pagesOf(saved.pages));
      if (before.size !== pages.length || pages.some((p) => !before.has(p))) this.persist();
    }
  }

  /** Record the address of the page key's element was just found on (flow.pageUrl). */
  setLastUrl(key: string, url: string | null): void {
    if (!validUrl(url)) return;
    const e = this.entry(key);
    e.lastUrl = url;
    const saved = this.meta.get(key) ?? {};
    if (saved.lastUrl !== url) {
      this.meta.set(key, { ...saved, lastUrl: url });
      if (!('lastUrl' in saved)) this.persist();
    }
  }

  lastUrl(key: string): string | null {
    const value = this.data.selectors[key]?.lastUrl;
    return validUrl(value) ? value : null;
  }

  /** Use role as key's expectedRole for now, without saving it. */
  guessRole(key: string, role: string, replace = false): void {
    const e = this.entry(key);
    if (role && (replace || this.guessed.has(key) || !knownRole(e.expectedRole))) {
      e.expectedRole = role;
      this.guessed.add(key);
    }
  }

  /** True when key's expectedRole is unknown or only a guess. */
  roleIsGuess(key: string): boolean {
    const e = this.data.selectors[key];
    return this.guessed.has(key) || !knownRole(e?.expectedRole);
  }

  /** Where key's element was the last time it matched: the selectors of the shadow hosts
   * around it, outermost first (null for a host nothing tells apart), or [] for the page or
   * when nothing was recorded (see shadow.ts). */
  shadowHosts(key: string): Hosts {
    return savedHosts(this.data.selectors[key]?.shadowHosts);
  }

  /** The candidate recordSuccess() last marked for key in this process. */
  lastSuccess(key: string): Candidate | null {
    return this.lastSuccessByKey.get(key) ?? null;
  }

  /** The status of one stored candidate, or null when the key has none with that text. */
  status(key: string, xpath: string): string | null {
    const c = (this.data.selectors[key]?.candidates ?? []).find((x) => x.xpath === xpath);
    return c ? (c.status ?? 'active') : null;
  }

  /** The selectors of key's quarantined candidates in one place: frameXPath '' for the page
   * or an iframe's selector for that iframe, and shadowHosts the hosts of a shadow root ([]
   * or null for none). null for both lists them all. */
  quarantined(key: string, frameXPath: string | null = null, shadowHosts: Hosts | null = null): string[] {
    const whole = frameXPath === null && shadowHosts === null;
    const hosts = JSON.stringify(savedHosts([...(shadowHosts ?? [])]));
    return (this.data.selectors[key]?.candidates ?? [])
      .filter((c) => c.status === 'quarantined' && c.xpath
        && (whole || (((c.frameXPath as string) || '') === (frameXPath || '')
                      && JSON.stringify(savedHosts(c.shadowHosts)) === hosts)))
      .map((c) => c.xpath);
  }

  /** Mark xpath as the candidate of key that just matched. Returns true when the recency
   * order changed. */
  recordSuccess(key: string, xpath: string): boolean {
    const c = this.find(key, xpath);
    if (c === null) return false;
    const top = this.byRecency(key)[0];
    const wasTop = top === c;
    const first = (c.lastSucceededAt === null || c.lastSucceededAt === undefined) && c.source !== 'seed';
    this.lastSuccessByKey.set(key, c);
    c.lastSucceededAt = nowIso();
    c.status = 'active';
    this.dirty.add(SelectorStore.pair(key, xpath));
    if (!wasTop || first) this.persist();
    return !wasTop;
  }

  /** Add a candidate for key, or update the one with the same text. frameXPath '' moves
   * one saved inside an iframe to the page. */
  addCandidate(key: string, xpath: string, options: { source?: string; frameXPath?: string } = {}): void {
    const entry = this.entry(key);
    const found = this.find(key, xpath);
    if (!found) {
      const c: Candidate = { xpath, source: options.source ?? 'heal', addedAt: nowIso(), lastSucceededAt: null,
                             status: 'active' };
      if (options.frameXPath) c.frameXPath = options.frameXPath;
      entry.candidates.push(c);
    } else if (options.frameXPath !== undefined && ((found.frameXPath as string) || '') !== options.frameXPath) {
      found.frameXPath = options.frameXPath;
    }
    this.dirty.add(SelectorStore.pair(key, xpath));
    this.persist();
  }

  quarantine(key: string, xpath: string): void {
    const c = this.find(key, xpath);
    if (c !== null) {
      c.status = 'quarantined';
      this.dirty.add(SelectorStore.pair(key, xpath));
      this.persist();
    }
  }

  /** Write the file once for every change made inside fn. */
  batch<T>(fn: () => T): T {
    this.batchDepth += 1;
    try {
      return fn();
    } finally {
      this.batchDepth -= 1;
      if (!this.batchDepth && this.batchWanted) {
        this.batchWanted = false;
        this.persist();
      }
    }
  }

  // --- writing the file -------------------------------------------------------------------

  persist(): void {
    if (this.readOnly) return;
    if (this.batchDepth) {
      this.batchWanted = true;
      return;
    }
    try {
      withLock(this.overridePath, () => {
        // A file that became unreadable raises here, so it is never replaced.
        const [disk] = readStoreFile(this.overridePath, this.destructiveKeys);
        this.absorb(disk);
        const payload = this.payload();
        atomicWrite(this.overridePath, compactJson(payload));
        this.rememberBase(payload);
      });
    } catch (error) {
      this.persistFailed(error);
    }
  }

  private persistFailed(error: unknown): void {
    const detail = (error as Error)?.name + ((error as Error)?.message ? ': ' + (error as Error).message : '');
    if (this.warned) {
      log('debug', 'still cannot save ' + this.overridePath + ' (' + detail + ')');
      return;
    }
    this.warned = true;
    if (error instanceof StoreReadError) {
      log('warning', 'not saving selector changes to ' + this.overridePath + ', because the file there cannot '
        + 'be read now: ' + error.message + ' mendwright keeps working in memory, and leaves the file as it is. '
        + 'Fix or delete the file.');
      return;
    }
    log('warning', 'could not save selector changes to ' + this.overridePath + ' (' + detail + '). mendwright '
      + 'keeps working in memory, but changes made now are lost when the process ends. Point it at a writable '
      + 'file with storePath or MENDWRIGHT_STORE.');
  }

  private rememberBase(data: StoreData): void {
    this.base = new Map();
    this.basePlace = new Map();
    this.baseMeta = new Map();
    for (const [key, entry] of Object.entries(data.selectors ?? {})) {
      if (!isRecord(entry)) continue;
      for (const c of (entry.candidates as Candidate[]) ?? []) {
        if (isRecord(c) && typeof c.xpath === 'string') {
          this.base.set(SelectorStore.pair(key, c.xpath), c.status ?? 'active');
          this.basePlace.set(SelectorStore.pair(key, c.xpath), placeOf(c));
        }
      }
      this.baseMeta.set(key, metaFields(entry));
    }
  }

  /** Take in what other processes wrote since this store last read or wrote the file. */
  private absorb(disk: StoreData): void {
    for (const [key, entry] of Object.entries(disk.selectors ?? {})) {
      if (!isRecord(entry)) continue;
      if (!this.data.selectors[key]) {
        this.entry(key).candidates = [];
      }
      const mine = this.entry(key);
      for (const c of (entry.candidates as Candidate[]) ?? []) {
        if (!isRecord(c) || typeof c.xpath !== 'string') continue;
        const id = SelectorStore.pair(key, c.xpath);
        const local = this.find(key, c.xpath);
        if (local === null) {
          mine.candidates.push(clone(c));
          continue;
        }
        local.status = mergeStatus(local.status ?? 'active', c.status ?? 'active', this.base.get(id));
        const newest = [local.lastSucceededAt || '', c.lastSucceededAt || ''].sort()[1];
        local.lastSucceededAt = newest || null;
        mergePlace(local, c, this.basePlace.get(id));
        if (this.delisted.has(id)) continue;   // taken over as code here: no list heal any more
        if (c.list === true) local.list = true;
        const theirs = evidenceOf(c);
        if (theirs !== null) {
          const mineEvidence = evidenceOf(local);
          if (mineEvidence === null || (theirs.at as string) > (mineEvidence.at as string)) local.proposal = clone(theirs);
        }
        if (typeof c.acceptedAt === 'string' && c.acceptedAt
            && !(typeof local.acceptedAt === 'string' && local.acceptedAt)) {
          local.acceptedAt = c.acceptedAt;
        }
      }
      const merged = mergeMeta(this.meta.get(key) ?? {}, metaFields(entry), this.baseMeta.get(key) ?? {});
      if (!same(merged, this.meta.get(key) ?? {})) {
        this.meta.set(key, merged);
        this.overlayMeta(key, merged);
      }
    }
  }

  /** The file's content: per key, its candidates (active ones capped at maxCandidates,
   * most recently succeeded first; inactive ones with a cap of their own, an inactive seed
   * row always kept; list heals that wait apart, at most 3) and its saved metadata. */
  private payload(): StoreData {
    const selectors: Record<string, Entry> = {};
    for (const [key, e] of Object.entries(this.data.selectors)) {
      const cap = e.maxCandidates ?? 5;
      const rows = e.candidates ?? [];
      const recency = (c: Candidate) => [c.lastSucceededAt || c.addedAt || ''];
      const active = sortedDesc(rows.filter((c) => (c.status ?? 'active') === 'active'),
        (c) => [c.lastSucceededAt || '']).slice(0, cap);
      let inactive = rows.filter((c) => (c.status ?? 'active') !== 'active');
      const waiting = sortedDesc(inactive.filter((c) => LIST_WAITING.includes(c.status as string)),
        (c) => [c.status === 'accepted', ...proposalOrder(c)]).slice(0, MAX_LIST_HEALS);
      inactive = inactive.filter((c) => !LIST_WAITING.includes(c.status as string));
      const pinned = inactive.filter((c) => c.source === 'seed');
      const others = sortedDesc(inactive.filter((c) => c.source !== 'seed'), recency).slice(0, cap);
      const overlay: Candidate[] = [];
      for (const c of [...active, ...pinned, ...others, ...waiting]) {
        const item: Candidate = { xpath: c.xpath };
        for (const field of ['source', 'addedAt', 'lastSucceededAt', 'status'] as const) {
          if (field in c) item[field] = c[field] as any;
        }
        if ('frameXPath' in c) item.frameXPath = c.frameXPath;
        if (Array.isArray(c.shadowHosts) && c.shadowHosts.length) item.shadowHosts = [...c.shadowHosts];
        if (typeof c.css === 'string' && c.css) item.css = c.css;
        if (c.list === true) item.list = true;
        const evidence = evidenceOf(c);
        if (evidence !== null) item.proposal = evidence;
        if (typeof c.acceptedAt === 'string' && c.acceptedAt) item.acceptedAt = c.acceptedAt;
        overlay.push(item);
      }
      const saved: Record<string, unknown> = {};
      for (const [field, value] of Object.entries(this.meta.get(key) ?? {})) {
        if ((META_FIELDS as readonly string[]).includes(field)) saved[field] = value;
      }
      if (overlay.length || Object.keys(saved).length) selectors[key] = { ...saved, candidates: overlay } as Entry;
    }
    return { schema: 2, selectors };
  }
}

/** Hold the lock file next to file (file + '.lock') while fn runs, as every store write
 * of both packages does. A lock older than lockStaleAfterMs is taken as abandoned. */
export function withLock<T>(file: string, fn: () => T): T {
  const lock = file + '.lock';
  fs.mkdirSync(path.dirname(lock), { recursive: true });
  const deadline = Date.now() + SelectorStore.lockTimeoutMs;
  let delay = 5;
  for (;;) {
    let fd: number;
    try {
      fd = fs.openSync(lock, 'wx', 0o600);
    } catch (error: any) {
      if (error?.code !== 'EEXIST' && !RETRY_CODES.has(error?.code)) throw error;
      if (error?.code !== 'EEXIST' && !fs.existsSync(lock)) throw error;
      if (breakIfStale(lock)) continue;
      if (Date.now() >= deadline) {
        throw Object.assign(new Error('the lock file ' + lock + ' is held by another process'), { name: 'TimeoutError' });
      }
      sleepMs(delay);
      delay = Math.min(delay * 2, 100);
      continue;
    }
    try {
      fs.writeSync(fd, String(process.pid));
    } finally {
      fs.closeSync(fd);
    }
    break;
  }
  try {
    return fn();
  } finally {
    try {
      fs.unlinkSync(lock);
    } catch {
      // gone already
    }
  }
}

function breakIfStale(lock: string): boolean {
  let age: number;
  try {
    age = Date.now() - fs.statSync(lock).mtimeMs;
  } catch {
    return false;
  }
  if (age <= SelectorStore.lockStaleAfterMs) return false;
  try {
    fs.unlinkSync(lock);
  } catch (error: any) {
    if (error?.code !== 'ENOENT') return false;
  }
  log('warning', 'removed a stale lock file ' + lock + ' (' + Math.round(age / 1000) + ' seconds old)');
  return true;
}

/** Write text to a temporary file next to file, then rename it over file. On Windows the
 * rename fails while another process has the file open, so it is retried briefly. */
export function atomicWrite(file: string, text: string): void {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, '.sel-' + crypto.randomBytes(6).toString('hex') + '.json');
  try {
    fs.writeFileSync(tmp, text, { encoding: 'utf-8', mode: 0o600 });
    let delay = 10;
    for (let attempt = 0; attempt < 8; attempt++) {
      try {
        fs.renameSync(tmp, file);
        break;
      } catch (error: any) {
        if (!RETRY_CODES.has(error?.code) || attempt === 7) throw error;
        sleepMs(delay);
        delay = Math.min(delay * 2, 200);
      }
    }
  } finally {
    if (fs.existsSync(tmp)) {
      try {
        fs.unlinkSync(tmp);
      } catch {
        // left behind; harmless
      }
    }
  }
}

/** A store over the file at overridePath (created on the first write). A file that exists
 * but cannot be used raises StoreReadError. Relative paths are read from the current
 * directory, and '~' is expanded. */
export function loadStore(overridePath: string, options: StoreOptions = {}): SelectorStore {
  const file = path.resolve(expandPath(overridePath));
  const destructive = new Set(options.destructiveKeys ?? []);
  const [override, native] = readStoreFile(file, destructive);
  const data: StoreData = { schema: 2, selectors: {} };
  for (const [key, entry] of Object.entries(override.selectors)) {
    const rows = Array.isArray(entry?.candidates) ? clone(entry.candidates) : [];
    data.selectors[key] = { ...clone(entry), candidates: rows.filter((c) => isRecord(c) && typeof c.xpath === 'string') } as Entry;
  }
  return new SelectorStore(data, file, { ...options, destructiveKeys: destructive, override: native ? override : null });
}
