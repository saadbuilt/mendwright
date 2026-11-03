// Playwright's getBy* calls as keys, for open(page, { healGetBy: true }) (the Python package's
// mendwright/getby.py).
//
// With healGetBy on, heal.page.getByRole('button', { name: 'Sign in' }) and the six other
// getBy* calls on heal.page are looked up the way heal.page.locator(selector) is, under a key
// written as the call in the Python package's spelling, so one store serves both packages:
//
//     get_by_role("button", name="Sign in")
//
// The key is the call in one canonical spelling (canonical): strings in double quotes with
// JSON escapes, which are also Python string literals with the same value; a regular
// expression as re.compile("...", re.IGNORECASE); the options of get_by_role in the order
// name, exact, level; exact=True only where it changes the match. parse() reads any spelling of
// such a call back, without running anything in it (see pysyntax.ts). A string parse() reads
// as a call is reserved for heal.page.getBy*: selectors.classify() gives it the kind 'get_by',
// which heal.locator() and the others refuse as a key or a selector.
//
// Some calls are never keys (callOf): getByRole() with a state filter (checked, disabled,
// expanded, pressed, selected) or includeHidden: true, an empty role, name or text, arguments
// that are not text, a RegExp or a whole number, a RegExp with flags other than i, m and s, one
// whose source the Python package cannot read back (pysyntax.pyReadable), an option this
// version does not read, or a key longer than MAX_KEY characters. Those go to Playwright as
// written.
//
// A call key is healed only from its fingerprint, never by a model (see
// HealCoordinator.healGetBy), and a heal of a getByRole() call must have the call's ARIA role,
// by Playwright's own matcher, on the live page (gate.liveReason). A lookup that misses throws
// GetByNotFound, which is both AttentionRequired and Playwright's TimeoutError.

import type { Locator, Page } from '@playwright/test';

import { pyJson, pyLen, pyRepr, pySlice, pyStrip } from './py';
import { pyReadable, readCall, type Pattern, type PyValue } from './pysyntax';

export type { Pattern } from './pysyntax';

/** The getBy* methods mendwright heals, as the Python package names them, in the order its
 * guide lists them. */
export const METHODS = ['get_by_role', 'get_by_text', 'get_by_label', 'get_by_placeholder', 'get_by_alt_text',
  'get_by_title', 'get_by_test_id'] as const;
export type PyMethod = typeof METHODS[number];

/** The same methods as Playwright for TypeScript names them, in the same order. */
export const TS_METHODS = ['getByRole', 'getByText', 'getByLabel', 'getByPlaceholder', 'getByAltText', 'getByTitle',
  'getByTestId'] as const;
export type TsMethod = typeof TS_METHODS[number];

const TS_OF = new Map<string, TsMethod>(METHODS.map((m, i) => [m, TS_METHODS[i]]));
const PY_OF = new Map<string, PyMethod>(TS_METHODS.map((m, i) => [m, METHODS[i]]));
// The ones that take a text and exact.
const TEXT_METHODS: ReadonlySet<string> = new Set(METHODS.slice(1, 6));

/** The longest key mendwright saves, in code points. */
export const MAX_KEY = 1000;

/** One getBy* call, as a key stands for it: value is the role for get_by_role, else the text
 * or test id; name and level are get_by_role's; exact is true only where it changes the
 * match. A string or a Pattern where text goes. */
export interface GetByCall {
  readonly method: PyMethod;
  readonly value: string | Pattern;
  readonly name: string | Pattern | null;
  readonly exact: boolean;
  readonly level: number | null;
}

// The parameters of Playwright's methods, as the Python package binds a call to them (its
// Playwright 1.53 signatures), the first one positional.
const SIGNATURES: Record<PyMethod, readonly string[]> = {
  get_by_role: ['role', 'checked', 'disabled', 'expanded', 'include_hidden', 'level', 'name', 'pressed', 'selected',
    'exact'],
  get_by_text: ['text', 'exact'],
  get_by_label: ['text', 'exact'],
  get_by_placeholder: ['text', 'exact'],
  get_by_alt_text: ['text', 'exact'],
  get_by_title: ['text', 'exact'],
  get_by_test_id: ['test_id'],
};

// The options of Playwright for TypeScript each method takes, and their names in the Python
// package. A later Playwright can add options: a call with one of them is not a key.
const OPTIONS: Record<PyMethod, Readonly<Record<string, string>>> = {
  get_by_role: { checked: 'checked', disabled: 'disabled', expanded: 'expanded', includeHidden: 'include_hidden',
    level: 'level', name: 'name', pressed: 'pressed', selected: 'selected', exact: 'exact' },
  get_by_text: { exact: 'exact' },
  get_by_label: { exact: 'exact' },
  get_by_placeholder: { exact: 'exact' },
  get_by_alt_text: { exact: 'exact' },
  get_by_title: { exact: 'exact' },
  get_by_test_id: {},
};

// getByRole()'s state filters, in the order of Playwright's signature.
const STATES = ['checked', 'disabled', 'expanded', 'pressed', 'selected'];

// The flags a regular expression in a key may have, in the order a key writes them.
const FLAG_NAMES: [string, string][] = [['i', 'IGNORECASE'], ['m', 'MULTILINE'], ['s', 'DOTALL']];

export function stateReason(state: string): string {
  return 'its ' + state + ' option picks the element by its state, which a saved selector cannot keep';
}
export const HIDDEN_REASON = 'includeHidden: true looks for hidden elements, and mendwright heals only onto a visible one';
export function emptyReason(what: string): string {
  return 'its ' + what + ' is empty, so it does not say which element is meant';
}
export const TYPE_REASON = 'its arguments are not text, a regular expression or a whole number';
export const SURROGATE_REASON = 'its text has characters mendwright cannot save';
export const FLAGS_REASON = 'its regular expression has flags mendwright cannot save: only i, m and s';
/** Why a RegExp whose source the Python package's re module cannot read the same way is not a
 * key (see pysyntax.pyReadable): the store is shared with it. */
export const PATTERN_REASON = "its regular expression uses syntax the Python package's re module cannot read back the "
  + 'same way, and the two packages share the store';
export function unknownReason(option: string): string {
  return 'it passes the option ' + option + ', which this version of mendwright does not read';
}
export const BIND_REASON = 'it passes arguments this version of mendwright does not read';
export const LONG_REASON = 'its arguments are longer than mendwright saves in a key (1,000 characters)';

// Why a call goes to Playwright as written (see Healer.getByLocator), in its DEBUG line.
export const REJECTED = 'was rejected by the browser';
export const HIDDEN = 'matches 1 hidden element';
export const STARTED_TO_MATCH = 'started to match something other than one visible element';

/** Why a call key is not followed into an iframe (see Healer.getByFrames). */
export const NOT_FOLLOWED = 'mendwright does not follow a getBy* call into an iframe';

/** The live gate's reason when the browser cannot be asked whether the element has the ARIA
 * role of a getByRole() call (see gate.liveReason). */
export const ROLE_UNREADABLE = "could not check the element's ARIA role on the live page";

/** The sentence a decline of a call key ends with (see HealCoordinator.healGetBy). */
export const NO_MODEL = 'mendwright asks no model to heal a getBy* call: the call names its element by role and words, '
  + 'and those are what changed, so a model could only guess. Change the call by hand.';

function isPattern(value: unknown): value is Pattern {
  return value !== null && typeof value === 'object' && typeof (value as Pattern).source === 'string'
    && typeof (value as Pattern).flags === 'string';
}

function isText(value: unknown): value is string | Pattern {
  return typeof value === 'string' || isPattern(value);
}

function isEmpty(value: string | Pattern): boolean {
  return typeof value === 'string' ? !pyStrip(value) : value.source === '';
}

/** False for text with a lone surrogate, which UTF-8 cannot hold. */
function savable(value: string | Pattern): boolean {
  const text = typeof value === 'string' ? value : value.source;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = text.charCodeAt(i + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      i += 1;
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      return false;
    }
  }
  return true;
}

// A pattern as callOf() first reads it: with extra, the flags it has besides i, m and s.
interface PatternIn extends Pattern {
  readonly extra: string;
}

/** [call, ''] for a call bound as the Python package binds one: positional args and keyword
 * kwargs, by its parameter names; or [null, why] for a call that is never a key (the Python
 * package's getby.call_of). */
function bind(method: PyMethod, args: readonly unknown[], kwargs: ReadonlyMap<string, unknown>):
    [GetByCall, ''] | [null, string] {
  const signature = SIGNATURES[method];
  for (const name of kwargs.keys()) {
    if (!signature.includes(name)) return [null, unknownReason(name)];
  }
  const first = signature[0];
  if (args.length > 1 || (args.length === 1 && kwargs.has(first)) || (args.length === 0 && !kwargs.has(first))) {
    return [null, BIND_REASON];
  }
  const given = new Map(kwargs);
  if (args.length) given.set(first, args[0]);
  const get = (name: string): unknown => (given.has(name) ? given.get(name) : null);
  let value: unknown;
  let name: unknown = null;
  let level: unknown = null;
  const exact = get('exact');
  let texts: [string, string | Pattern][];
  if (method === 'get_by_role') {
    for (const state of STATES) {
      if (get(state) !== null && get(state) !== undefined) return [null, stateReason(state)];
    }
    const hidden = get('include_hidden');
    if (hidden === true) return [null, HIDDEN_REASON];
    value = get('role');
    name = get('name');
    level = get('level');
    if (typeof value !== 'string' || (name !== null && !isText(name)) || !(hidden === null || hidden === false)
        || (level !== null && !(typeof level === 'number' && Number.isSafeInteger(level) && level >= 0))) {
      return [null, TYPE_REASON];
    }
    texts = [['role', value]];
    if (name !== null) texts.push(['name', name as string | Pattern]);
  } else {
    value = get(first);
    if (!isText(value)) return [null, TYPE_REASON];
    texts = [[method === 'get_by_test_id' ? 'test id' : 'text', value]];
  }
  if (exact !== null && typeof exact !== 'boolean') return [null, TYPE_REASON];
  for (const [what, text] of texts) {
    if (isEmpty(text)) return [null, emptyReason(what)];
  }
  for (const [, text] of texts) {
    if (!savable(text)) return [null, SURROGATE_REASON];
  }
  for (const [, text] of texts) {
    if (isPattern(text) && (text as PatternIn).extra) return [null, FLAGS_REASON];
  }
  for (const [, text] of texts) {
    if (isPattern(text) && !pyReadable(text.source)) return [null, PATTERN_REASON];
  }
  const plain = (text: unknown): string | Pattern => (isPattern(text) ? { source: text.source, flags: text.flags }
    : text as string);
  const call: GetByCall = {
    method,
    value: plain(value),
    name: name === null ? null : plain(name),
    exact: exact === true && ((TEXT_METHODS.has(method) && typeof value === 'string')
      || (method === 'get_by_role' && typeof name === 'string')),
    level: level as number | null,
  };
  if (pyLen(canonical(call)) > MAX_KEY) return [null, LONG_REASON];
  return [call, ''];
}

/** The Python package's name of a getBy* method, from either spelling, or null. */
export function pyMethodOf(method: unknown): PyMethod | null {
  if (typeof method !== 'string') return null;
  if ((METHODS as readonly string[]).includes(method)) return method as PyMethod;
  return PY_OF.get(method) ?? null;
}

/** The TypeScript name of a getBy* method (getByRole for get_by_role). */
export function tsMethodOf(method: PyMethod): TsMethod {
  return TS_OF.get(method) as TsMethod;
}

const OTHER = { other: true };

/** An argument of a TypeScript call as the Python package would hold it: a RegExp as a
 * pattern (JavaScript's empty one, whose source is '(?:)', as the empty pattern), and
 * anything that is not text, a RegExp, a number, a boolean or null as something no check
 * takes. */
function held(value: unknown): unknown {
  if (value instanceof RegExp) {
    const flags = value.flags;
    return {
      source: value.source === '(?:)' ? '' : value.source,
      flags: FLAG_NAMES.map(([letter]) => letter).filter((l) => flags.includes(l)).join(''),
      extra: [...flags].filter((l) => !'ims'.includes(l)).join(''),
    } as PatternIn;
  }
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return value;
  return OTHER;
}

/** [call, ''] for heal.page.<method>(...args), method in either spelling, or [null, why] for
 * a call that is never a key (see the module comment). Arguments left out at the end
 * (undefined) are not there, and so is an option whose value is undefined, as for Playwright. */
export function callOf(method: string, args: readonly unknown[]): [GetByCall, ''] | [null, string] {
  const py = pyMethodOf(method);
  if (py === null) return [null, BIND_REASON];
  const given = [...args];
  while (given.length && given[given.length - 1] === undefined) given.pop();
  const kwargs = new Map<string, unknown>();
  const options = given[1];
  if (options !== undefined && options !== null) {
    const proto = typeof options === 'object' ? Object.getPrototypeOf(options) : undefined;
    if (proto !== Object.prototype && proto !== null) return [null, BIND_REASON];
    for (const [option, value] of Object.entries(options as object)) {
      if (value === undefined) continue;
      const name = OPTIONS[py][option];
      if (name === undefined) return [null, unknownReason(option)];
      kwargs.set(name, held(value));
    }
  }
  if (given.length === 0 || given.length > 2) return [null, BIND_REASON];
  return bind(py, [held(given[0])], kwargs);
}

function literal(value: string | Pattern): string {
  if (typeof value === 'string') return pyJson(value, false);
  const flags = FLAG_NAMES.filter(([letter]) => value.flags.includes(letter)).map(([, name]) => 're.' + name).join(' | ');
  return 're.compile(' + pyJson(value.source, false) + (flags ? ', ' + flags : '') + ')';
}

/** The key of a GetByCall: the call in its one canonical spelling, such as
 * get_by_role("button", name="Sign in"). */
export function canonical(call: GetByCall): string {
  const parts = [literal(call.value)];
  if (call.method === 'get_by_role') {
    if (call.name !== null) parts.push('name=' + literal(call.name));
    if (call.exact) parts.push('exact=True');
    if (call.level !== null) parts.push('level=' + String(call.level));
  } else if (call.exact) {
    parts.push('exact=True');
  }
  return call.method + '(' + parts.join(', ') + ')';
}

// --- reading a key ------------------------------------------------------------------------

const PARSED = new Map<string, GetByCall | null>();

/** The GetByCall a key (or any spelling of a get_by_* call) stands for, or null. Nothing in
 * the text is run (see pysyntax.readCall). null for anything that is not a string, is longer
 * than MAX_KEY characters, does not start with get_by_, or is not such a call. */
export function parse(text: unknown): GetByCall | null {
  if (typeof text !== 'string' || pyLen(text) > MAX_KEY) return null;
  const body = pyStrip(text);
  if (!body.startsWith('get_by_')) return null;
  if (PARSED.has(body)) return PARSED.get(body) as GetByCall | null;
  const read = readCall(body);
  let call: GetByCall | null = null;
  if (read && (METHODS as readonly string[]).includes(read.name)) {
    call = bind(read.name as PyMethod, read.args, read.kwargs as Map<string, PyValue>)[0];
  }
  if (PARSED.size >= 1024) PARSED.delete(PARSED.keys().next().value as string);
  PARSED.set(body, call);
  return call;
}

/** True when key is a get_by_* call (see parse): a key mendwright keeps for heal.page.getBy*,
 * which heal.locator() and the others refuse. */
export function isKey(key: unknown): boolean {
  return parse(key) !== null;
}

/** The key mendwright saves a getBy* call under, for a key written as that call in any
 * spelling parse() reads, such as get_by_role('button', name='Pay'); any other key as it is.
 * destructiveKeys, expectValues and heal.present() read their keys through it, so a mark
 * written that way still applies to the call. */
export function canonicalKey<T>(key: T): T | string {
  const call = parse(key);
  return call === null ? key : canonical(call);
}

// A get_by_* call written on an object, such as page.get_by_role(...): the call from get_by_.
// [A-Za-z_]\w* as Python's \w reads it in a str.
const ON_OBJECT = /^(?:[A-Za-z_][\p{L}\p{N}_]*\.)+(get_by_[\s\S]*)$/u;

/** The GetByCall of a key written as a get_by_* call on an object, such as
 * page.get_by_role('button', name='Pay') from a log line of the Python package, or null. Such
 * a key is a key of its own, not the call's (canonicalKey leaves it as it is), so
 * destructiveKeys and expectValues log a WARNING for one (onObjectMessage). */
export function onObject(key: unknown): GetByCall | null {
  if (typeof key !== 'string' || pyLen(key) > MAX_KEY) return null;
  const match = ON_OBJECT.exec(pyStrip(key));
  return match === null ? null : parse(match[1]);
}

/** The WARNING (without the 'mendwright: ' the log adds) for a key of where (destructiveKeys,
 * expectValues) written as call on an object (see onObject). */
export function onObjectMessage(where: string, key: string, call: GetByCall): string {
  return where + ' has the key ' + pyRepr(key) + ', which no heal.page.' + tsMethodOf(call.method) + "() call uses. "
    + "That call's key is " + canonical(call) + ', as getByKey() writes it.';
}

/** onObjectMessage() for key of where, or null when key is no get_by_* call on an object. */
export function onObjectWarning(where: string, key: unknown): string | null {
  const call = onObject(key);
  return call === null ? null : onObjectMessage(where, key as string, call);
}

/** The Locator for call on ctx, a Page, Frame, Locator or FrameLocator: its getBy* method with
 * the call's arguments (a pattern as a RegExp with its flags). */
export function build(ctx: Pick<Page, TsMethod>, call: GetByCall): Locator {
  const value = jsValue(call.value);
  if (call.method === 'get_by_role') {
    const options: { name?: string | RegExp; exact?: boolean; level?: number } = {};
    if (call.name !== null) options.name = jsValue(call.name);
    if (call.exact) options.exact = true;
    if (call.level !== null) options.level = call.level;
    return ctx.getByRole(value as Parameters<Page['getByRole']>[0], options);
  }
  const method = tsMethodOf(call.method) as Exclude<TsMethod, 'getByRole' | 'getByTestId'>;
  if (call.method === 'get_by_test_id') return ctx.getByTestId(value);
  return call.exact ? ctx[method](value, { exact: true }) : ctx[method](value);
}

function jsValue(value: string | Pattern): string | RegExp {
  return typeof value === 'string' ? value : new RegExp(value.source, value.flags);
}

// --- the call as messages show it -----------------------------------------------------------

/** An argument as a message shows it, in TypeScript: 'Continue' (a string as Python's repr()
 * writes it), { name: 'Continue', exact: true }, /Continue/i. A string longer than 60
 * characters is cut, as the Python package cuts it. nested shows what is inside an array or
 * an object (heal.page passes one that names its locators). */
export function argText(value: unknown, nested: (v: unknown) => string = argText): string {
  const cut = (text: string) => (pyLen(text) <= 60 ? text : pySlice(text, 57) + '...');
  if (typeof value === 'string') return cut(pyRepr(value));
  if (value === undefined) return 'undefined';
  if (value === null) return 'null';
  if (value instanceof RegExp) return cut(String(value));
  if (typeof value === 'function') return '<function>';
  if (Array.isArray(value)) return '[' + value.map(nested).join(', ') + ']';
  if (typeof value === 'object') {
    const parts = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined)
      .map(([k, v]) => (/^[A-Za-z_$][\w$]*$/.test(k) ? k : pyRepr(k)) + ': ' + nested(v));
    return parts.length ? '{ ' + parts.join(', ') + ' }' : '{}';
  }
  return String(value);
}

/** A call as a message shows it: getByRole('button', { name: 'Go' }). Arguments left out at
 * the end (undefined) are not shown. */
export function callText(name: string, args: readonly unknown[], show: (v: unknown) => string = argText): string {
  const shown = [...args];
  while (shown.length && shown[shown.length - 1] === undefined) shown.pop();
  return name + '(' + shown.map((v) => show(v)).join(', ') + ')';
}

// --- the public key builder ----------------------------------------------------------------

interface GetByCalls {
  getByRole: Page['getByRole'];
  getByText: Page['getByText'];
  getByLabel: Page['getByLabel'];
  getByPlaceholder: Page['getByPlaceholder'];
  getByAltText: Page['getByAltText'];
  getByTitle: Page['getByTitle'];
  getByTestId: Page['getByTestId'];
  get_by_role: Page['getByRole'];
  get_by_text: Page['getByText'];
  get_by_label: Page['getByLabel'];
  get_by_placeholder: Page['getByPlaceholder'];
  get_by_alt_text: Page['getByAltText'];
  get_by_title: Page['getByTitle'];
  get_by_test_id: Page['getByTestId'];
}

/** A getBy* method getByKey() takes: Playwright's name, or the Python package's. */
export type GetByMethod = keyof GetByCalls;

/** The key a heal.page.<method>(...args) call is saved under, for destructiveKeys, expectValues
 * and heal.present(), and to read the store (the Python package's mendwright.get_by_key()):
 *
 *     getByKey('getByRole', 'button', { name: 'Pay' })   // 'get_by_role("button", name="Pay")'
 *
 * method is Playwright's name or the Python package's (get_by_role), and the arguments are
 * Playwright's for TypeScript. A key matches one call: { name: /pay/i } or exact: true makes
 * another key. Throws TypeError for a method mendwright does not heal and for a call that is
 * never a key. */
export function getByKey<M extends GetByMethod>(method: M, ...args: Parameters<GetByCalls[M]>): string {
  const py = pyMethodOf(method);
  if (py === null) {
    throw new TypeError('mendwright: getByKey(): ' + pyRepr(method) + ' is not a getBy* method mendwright heals. Use '
      + 'one of ' + TS_METHODS.slice(0, -1).join(', ') + ' or ' + TS_METHODS[TS_METHODS.length - 1] + '.');
  }
  const [call, why] = callOf(py, args);
  if (call === null) {
    throw new TypeError('mendwright: getByKey(): page.' + callText(tsMethodOf(py), args) + ' is never healed, so it '
      + 'has no key: ' + why + '.');
  }
  return canonical(call);
}

// --- messages -----------------------------------------------------------------------------

/** The INFO line for a heal.page.getBy*() call that is never a key (see callOf). */
export function notHealedMessage(step: string, why: string): string {
  return step + ' is not healed: ' + why + '. It goes to Playwright as written.';
}

/** The DEBUG line for a call that goes to Playwright as written. */
export function throughMessage(key: string, why: string): string {
  return key + ' ' + why + '; Playwright runs it as written.';
}

/** The error for two keys of where (expectValues) that name one call. */
export function twoSpellingsMessage(where: string, first: string, second: string): string {
  return 'mendwright: ' + where + ' has two keys for the call ' + canonicalKey(first) + ': ' + pyRepr(first) + ' and '
    + pyRepr(second) + '. Keep one of them.';
}

/** The TypeError for a key of heal.locator() and the others written as a call. */
export function reservedKeyMessage(key: string, entry: string): string {
  return 'mendwright: ' + pyRepr(key) + ' is how mendwright names a heal.page.'
    + tsMethodOf((parse(key) as GetByCall).method) + '() call, so ' + entry + ' cannot take it as a key. Pick another key.';
}

/** The live gate's reason when Playwright does not give the element call's ARIA role. */
export function roleMismatch(call: GetByCall): string {
  let text = 'live element role mismatch: Playwright does not give it the ARIA role ' + pyRepr(call.value);
  if (call.level !== null) text += ' at level ' + String(call.level);
  return text;
}

export function tierOffMessage(key: string): string {
  return 'the fingerprint tier is off (fingerprint: false or MENDWRIGHT_FINGERPRINT=0), and mendwright heals a getBy* '
    + "call only from its fingerprint, so '" + key + "' was not healed. Turn the fingerprint tier on to heal getBy* calls.";
}

export function noFingerprintMessage(key: string): string {
  return 'mendwright heals a getBy* call only from the fingerprint it records each time the call matches one element, '
    + "and '" + key + "' has none in this store: it has not matched one element here while the fingerprint tier was on. "
    + 'A call that never matched is more often a mistake in the call than a redesign. Check its arguments against the '
    + 'page.';
}

export function roleUnknownMessage(key: string): string {
  return "mendwright does not know what kind of element '" + key + "' is: it could not read the element when the call "
    + 'last matched, so it cannot check a repaired one. Run the step again while the call matches.';
}
