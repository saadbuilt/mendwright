// Helpers that reproduce the Python behaviour mendwright's shared formats rely on, so a
// prompt, a fingerprint or a store written here reads the same as one the Python package
// writes: str(), truthiness, json.dumps() with its default separators and ASCII escapes,
// slicing and len() by code point, repr() of a string, and urlsplit().

/** Python's str() for the values a prompt or message holds. */
export function pyStr(v: unknown): string {
  if (v === null || v === undefined) return 'None';
  if (v === true) return 'True';
  if (v === false) return 'False';
  return String(v);
}

/** Python's truthiness. */
export function pyTruthy(v: unknown): boolean {
  if (v === null || v === undefined || v === false || v === 0 || v === '') return false;
  if (Array.isArray(v)) return v.length > 0;
  if (typeof v === 'object') return Object.keys(v as object).length > 0;
  return true;
}

const ESCAPES: Record<string, string> = {
  '"': '\\"', '\\': '\\\\', '\n': '\\n', '\r': '\\r', '\t': '\\t', '\b': '\\b', '\f': '\\f',
};

function jsonString(s: string, ascii: boolean): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    const code = s.charCodeAt(i);
    if (ESCAPES[ch]) out += ESCAPES[ch];
    else if (code < 0x20 || (ascii && code > 0x7e)) out += '\\u' + code.toString(16).padStart(4, '0');
    else out += ch;
  }
  return out + '"';
}

/** Python's json.dumps(v) with its defaults: ', ' and ': ' separators, non-ASCII escaped
 * as \uXXXX (ensureAscii false keeps it as it is, as ensure_ascii=False does). */
export function pyJson(v: unknown, ensureAscii = true): string {
  if (v === null || v === undefined) return 'null';
  if (v === true) return 'true';
  if (v === false) return 'false';
  if (typeof v === 'number') {
    if (Number.isNaN(v)) return 'NaN';
    if (!Number.isFinite(v)) return v > 0 ? 'Infinity' : '-Infinity';
    return String(v);
  }
  if (typeof v === 'string') return jsonString(v, ensureAscii);
  if (Array.isArray(v)) return '[' + v.map((x) => pyJson(x, ensureAscii)).join(', ') + ']';
  if (typeof v === 'object') {
    return '{' + Object.keys(v as object).map((k) => jsonString(k, ensureAscii) + ': '
      + pyJson((v as Record<string, unknown>)[k], ensureAscii)).join(', ') + '}';
  }
  return 'null';
}

/** JSON as Python's json.dumps(v, separators=(',', ':')) writes it: compact, with every
 * character outside ASCII escaped. The store file is written this way, so a file the
 * TypeScript and the Python package both write keeps one spelling. */
export function compactJson(v: unknown): string {
  return JSON.stringify(v).replace(
    /[\u007f-\uffff]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));
}

/** The code points of s, as Python indexes a str. */
export function points(s: string): string[] {
  return Array.from(s);
}

/** len(s) in Python: code points, not UTF-16 units. */
export function pyLen(s: string): number {
  let n = 0;
  for (const _ of s) n += 1;
  return n;
}

/** s[:n] in Python. */
export function pySlice(s: string, n: number): string {
  if (s.length <= n) return s;
  let units = 0;
  let count = 0;
  while (units < s.length && count < n) {
    const c = s.charCodeAt(units);
    const pair = c >= 0xd800 && c <= 0xdbff && units + 1 < s.length
      && (s.charCodeAt(units + 1) & 0xfc00) === 0xdc00;
    units += pair ? 2 : 1;
    count += 1;
  }
  return s.slice(0, units);
}

/** repr() of a str, as Python writes it in a message: single quotes, or double quotes when
 * the text has a single quote and no double one. */
export function pyRepr(value: unknown): string {
  if (typeof value !== 'string') {
    if (value === null || value === undefined) return 'None';
    if (value === true) return 'True';
    if (value === false) return 'False';
    if (Array.isArray(value)) return '[' + value.map(pyRepr).join(', ') + ']';
    return String(value);
  }
  const quote = value.includes("'") && !value.includes('"') ? '"' : "'";
  let out = quote;
  for (const ch of value) {
    if (ch === '\\') out += '\\\\';
    else if (ch === quote) out += '\\' + ch;
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else {
      const code = ch.codePointAt(0) as number;
      if (code < 0x20 || code === 0x7f) out += '\\x' + code.toString(16).padStart(2, '0');
      else out += ch;
    }
  }
  return out + quote;
}

// The characters str.isspace() is true for, which str.strip() takes off both ends.
const PY_SPACE = '\t\n\x0b\x0c\r\x1c-\x1f \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000';
const PY_STRIP = new RegExp('^[' + PY_SPACE + ']+|[' + PY_SPACE + ']+$', 'g');

/** str.strip() with no argument. */
export function pyStrip(s: string): string {
  return s.replace(PY_STRIP, '');
}

const PY_RSTRIP = new RegExp('[' + PY_SPACE + ']+$');

/** str.rstrip() with no argument. */
export function pyRstrip(s: string): string {
  return s.replace(PY_RSTRIP, '');
}

/** sum() of floats as CPython 3.12 and later add them: Neumaier's compensated summation,
 * so a score here equals the Python package's to the last digit. */
export function pySum(values: number[]): number {
  let f = 0.0;
  let c = 0.0;
  for (const x of values) {
    const t = f + x;
    if (Math.abs(f) >= Math.abs(x)) c += (f - t) + x;
    else c += (x - t) + f;
    f = t;
  }
  if (c && Number.isFinite(c)) f += c;
  return f;
}

/** str.isdigit() for a word mendwright split from a text (a run of decimal digits). */
export function isDigits(s: string): boolean {
  return s.length > 0 && /^\p{Nd}+$/u.test(s);
}

export interface SplitUrl {
  scheme: string;
  netloc: string;
  path: string;
  query: string;
  fragment: string;
}

const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;

/** urllib.parse.urlsplit(): scheme, netloc, path, query and fragment, as Python splits
 * them. Throws for a netloc with an unbalanced IPv6 bracket, as Python raises ValueError. */
export function urlsplit(url: string): SplitUrl {
  let rest = url.replace(/[\t\r\n]/g, '').replace(/^[\x00-\x20]+/, '');
  let scheme = '';
  const m = SCHEME.exec(rest);
  if (m) {
    scheme = m[0].slice(0, -1).toLowerCase();
    rest = rest.slice(m[0].length);
  }
  let netloc = '';
  if (rest.startsWith('//')) {
    const end = rest.slice(2).search(/[/?#]/);
    netloc = end < 0 ? rest.slice(2) : rest.slice(2, 2 + end);
    rest = end < 0 ? '' : rest.slice(2 + end);
    if ((netloc.includes('[') && !netloc.includes(']'))
        || (netloc.includes(']') && !netloc.includes('['))) {
      throw new Error('Invalid IPv6 URL');
    }
  }
  let fragment = '';
  const hash = rest.indexOf('#');
  if (hash >= 0) {
    fragment = rest.slice(hash + 1);
    rest = rest.slice(0, hash);
  }
  let query = '';
  const q = rest.indexOf('?');
  if (q >= 0) {
    query = rest.slice(q + 1);
    rest = rest.slice(0, q);
  }
  return { scheme, netloc, path: rest, query, fragment };
}

/** The hostname of a urlsplit() netloc, lower case, without user info, port or IPv6
 * brackets, or null when it has none (SplitResult.hostname). */
export function hostname(netloc: string): string | null {
  let host = netloc.slice(netloc.lastIndexOf('@') + 1);
  if (host.startsWith('[')) {
    const end = host.indexOf(']');
    host = end < 0 ? host.slice(1) : host.slice(1, end);
  } else {
    const colon = host.indexOf(':');
    if (colon >= 0) host = host.slice(0, colon);
  }
  return host ? host.toLowerCase() : null;
}

/** The port of a urlsplit() netloc (SplitResult.port): null when it has none, and an error
 * for one that is not a number from 0 to 65535, as Python raises ValueError. */
export function port(netloc: string): number | null {
  let host = netloc.slice(netloc.lastIndexOf('@') + 1);
  if (host.startsWith('[')) {
    const end = host.indexOf(']');
    host = end < 0 ? '' : host.slice(end + 1);
  }
  const colon = host.indexOf(':');
  if (colon < 0) return null;
  const text = host.slice(colon + 1);
  if (!text) return null;
  if (!/^\d+$/.test(text)) throw new Error('Port could not be cast to integer value');
  const value = Number(text);
  if (value > 65535) throw new Error('Port out of range 0-65535');
  return value;
}

/** Python's datetime.now(timezone.utc).replace(microsecond=0).isoformat(), the time format
 * of the store and the journal: 2026-10-10T19:07:53+00:00. The store orders candidates by
 * comparing these strings, so both packages must write the same format. */
export function nowIso(date: Date = new Date()): string {
  return date.toISOString().replace(/\.\d{3}Z$/, '+00:00');
}
