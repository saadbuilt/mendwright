// Reading the keys of heal.page.getBy*() calls, which are written in Python's call syntax (see
// getby.ts): a small reader of that syntax, and the check that a regular expression's source
// reads the same in Python's re module as in the browser.
//
// readCall() takes a string such as get_by_role('button', name=re.compile(r'pay', re.I)) and
// gives the call's name, its positional arguments and its keyword arguments, or null. Nothing
// in the text is run: it is a tokenizer for literals (str in every spelling Python has, with
// its escapes and implicit concatenation; whole numbers; True, False and None) and
// re.compile() of a str with flag names (re.I, re.IGNORECASE, re.M, re.MULTILINE, re.S,
// re.DOTALL, joined with |). Anything else gives null, also where Python's ast would read a
// call, as the Python package's getby.parse() gives None for any value that is not one of
// these: an f-string, bytes, a float, a name, an attribute, an operator. A few spellings Python
// reads give null here too: a \N{...} escape, an identifier that is not ASCII, more than 100
// nested brackets, and a regular expression that pyReadable() does not take.
//
// pyReadable() says whether a regular expression's source is one Python's re module compiles
// with no flags of its own, and that is valid JavaScript without the u flag, so that the key
// that holds it is read back by both packages as the same call. Playwright sends the source to
// the browser in both packages, where JavaScript's engine matches it, so only that both can
// read it matters. It takes a subset of the syntax of both, and refuses anything else (fail
// closed): a named group, a backreference, \c, \k, \p, an escape of a letter neither reads the
// same way, an inline flag, an empty class, a range from or to a class escape, the characters
// Python warns about in a class ([, --, &&, ~~, ||), a { that is not a quantifier, a quantified
// assertion, and a lookbehind whose width Python cannot fix.

/** A regular expression as a key holds it: its source, and its flags among i, m and s, in
 * that order (Python's re.IGNORECASE, re.MULTILINE and re.DOTALL). */
export interface Pattern {
  readonly source: string;
  readonly flags: string;
}

/** A value of a call's argument, as readCall() reads it. */
export type PyValue = string | boolean | number | null | Pattern;

/** What readCall() reads: the called name, the positional arguments, and the keyword ones by
 * name, in the order written. */
export interface PyCall {
  readonly name: string;
  readonly args: PyValue[];
  readonly kwargs: Map<string, PyValue>;
}

// Thrown inside the reader for text it does not take; readCall() gives null for it.
const UNREADABLE = { unreadable: true };

function fail(): never {
  throw UNREADABLE;
}

const NAME = /[A-Za-z_][A-Za-z0-9_]*/y;
const NUMBER = /0[xX](?:_?[0-9a-fA-F])+|0[oO](?:_?[0-7])+|0[bB](?:_?[01])+|[1-9](?:_?[0-9])*|0+(?:_?0)*/y;
// What a whole number may not be followed by: more of a number (a float, an imaginary number)
// or of a name (a character that is not ASCII is refused too).
const AFTER_NUMBER = /[0-9A-Za-z_.]/;
const FLAG_LETTERS: Record<string, string> = { I: 'i', IGNORECASE: 'i', M: 'm', MULTILINE: 'm', S: 's', DOTALL: 's' };
const MAX_DEPTH = 100;

class Reader {
  pos = 0;
  private depth = 0;

  constructor(private readonly text: string) {}

  peek(offset = 0): string | undefined {
    return this.text[this.pos + offset];
  }

  atEnd(): boolean {
    return this.pos >= this.text.length;
  }

  /** Skip what separates tokens: spaces, tabs, form feeds, comments, a backslash at the end
   * of a line, and inside brackets the ends of lines too. ends lets an end of line be skipped
   * outside brackets, after the call. */
  skip(ends = false): void {
    const text = this.text;
    for (;;) {
      const c = text[this.pos];
      if (c === ' ' || c === '\t' || c === '\f') {
        this.pos += 1;
      } else if (c === '#') {
        while (this.pos < text.length && text[this.pos] !== '\n' && text[this.pos] !== '\r') this.pos += 1;
      } else if (c === '\\' && (text[this.pos + 1] === '\n' || text[this.pos + 1] === '\r')) {
        this.pos += text.startsWith('\r\n', this.pos + 1) ? 3 : 2;
      } else if ((c === '\n' || c === '\r') && (this.depth > 0 || ends)) {
        this.pos += 1;
      } else {
        return;
      }
    }
  }

  expect(c: string): void {
    if (this.text[this.pos] !== c) fail();
    this.pos += 1;
  }

  open(): void {
    this.expect('(');
    this.depth += 1;
    if (this.depth > MAX_DEPTH) fail();
  }

  close(): void {
    this.expect(')');
    this.depth -= 1;
  }

  name(): string {
    NAME.lastIndex = this.pos;
    const m = NAME.exec(this.text);
    if (!m) fail();
    this.pos += m[0].length;
    // A name that goes on with a character that is not ASCII is one Python reads whole.
    const next = this.text.charCodeAt(this.pos);
    if (next >= 0x80) fail();
    return m[0];
  }

  /** One argument's value. */
  value(): PyValue {
    this.skip();
    const c = this.peek();
    if (c === undefined) fail();
    if (c === '(') {
      this.open();
      const inner = this.value();
      this.skip();
      this.close();
      return inner;
    }
    if (c === '"' || c === "'") return this.strings('');
    if (c >= '0' && c <= '9') return this.number();
    const name = this.name();
    const after = this.peek();
    if (after === '"' || after === "'") return this.strings(name);
    if (name === 'True') return true;
    if (name === 'False') return false;
    if (name === 'None') return null;
    if (name === 're') return this.compile();
    return fail();
  }

  /** One or more str literals written next to each other, which Python joins into one. The
   * first one's prefix is read already. A bytes literal, an f-string or a t-string is never
   * a str constant. */
  private strings(prefix: string): string {
    let out = this.oneString(prefix);
    for (;;) {
      const save = this.pos;
      this.skip();
      const c = this.peek();
      if (c === '"' || c === "'") {
        out += this.oneString('');
        continue;
      }
      if (c !== undefined && /[A-Za-z]/.test(c)) {
        NAME.lastIndex = this.pos;
        const m = NAME.exec(this.text);
        const quote = m ? this.text[this.pos + m[0].length] : undefined;
        if (m && (quote === '"' || quote === "'")) {
          this.pos += m[0].length;
          out += this.oneString(m[0]);
          continue;
        }
      }
      this.pos = save;
      return out;
    }
  }

  private oneString(prefix: string): string {
    const kind = prefix.toLowerCase();
    if (kind !== '' && kind !== 'r' && kind !== 'u') fail();   // bytes, f-strings, t-strings, or not a prefix
    const raw = kind === 'r';
    const text = this.text;
    const q = text[this.pos];
    const triple = text.startsWith(q + q + q, this.pos);
    this.pos += triple ? 3 : 1;
    let out = '';
    for (;;) {
      if (this.pos >= text.length) fail();
      const c = text[this.pos];
      if (triple ? text.startsWith(q + q + q, this.pos) : c === q) {
        this.pos += triple ? 3 : 1;
        return out;
      }
      if (c === '\r' || c === '\0') fail();
      if (c === '\n') {
        if (!triple) fail();
        out += c;
        this.pos += 1;
        continue;
      }
      if (c === '\\') {
        const d = text[this.pos + 1];
        if (d === undefined || d === '\r') fail();
        this.pos += 2;
        // In a raw string a backslash escapes nothing, but the quote after it does not end it.
        out += raw ? c + d : this.escape(d);
        continue;
      }
      out += c;
      this.pos += 1;
    }
  }

  /** What a backslash and d stand for in a str literal that is not raw. An escape of a
   * surrogate is refused: in Python it is a lone surrogate, even next to another one, which
   * no key can hold, where JavaScript would join two into one character. */
  private escape(d: string): string {
    const text = this.escapeText(d);
    if (text.length === 1 && isSurrogate(text.charCodeAt(0))) fail();
    return text;
  }

  private escapeText(d: string): string {
    switch (d) {
      case '\n': return '';
      case '\\': return '\\';
      case "'": return "'";
      case '"': return '"';
      case 'a': return '\x07';
      case 'b': return '\b';
      case 'f': return '\f';
      case 'n': return '\n';
      case 'r': return '\r';
      case 't': return '\t';
      case 'v': return '\v';
      case 'x': return String.fromCharCode(this.hex(2));
      case 'u': return String.fromCharCode(this.hex(4));
      case 'U': {
        const code = this.hex(8);
        if (code > 0x10ffff) fail();
        return String.fromCodePoint(code);
      }
      case 'N': return fail();      // \N{name}: the Unicode names are not here
      default:
        if (d >= '0' && d <= '7') {
          let digits = d;
          while (digits.length < 3 && /[0-7]/.test(this.text[this.pos] ?? '')) digits += this.text[this.pos++];
          return String.fromCharCode(parseInt(digits, 8));
        }
        return '\\' + d;          // an escape Python does not know stays as it is
    }
  }

  private hex(n: number): number {
    const digits = this.text.slice(this.pos, this.pos + n);
    if (digits.length !== n || !/^[0-9A-Fa-f]+$/.test(digits)) fail();
    this.pos += n;
    return parseInt(digits, 16);
  }

  private number(): number {
    NUMBER.lastIndex = this.pos;
    const m = NUMBER.exec(this.text);
    if (!m) fail();
    this.pos += m[0].length;
    if (AFTER_NUMBER.test(this.text[this.pos] ?? '') || this.text.charCodeAt(this.pos) >= 0x80) fail();
    const digits = m[0].replace(/_/g, '');
    const value = BigInt(digits.length > 1 && /^0[0-9]/.test(digits) ? '0' : digits);
    if (value > BigInt(Number.MAX_SAFE_INTEGER)) fail();
    return Number(value);
  }

  /** re.compile(<str>[, <flags>]), the name re read already. */
  private compile(): Pattern {
    this.skip();
    this.expect('.');
    this.skip();
    if (this.name() !== 'compile') fail();
    this.skip();
    this.open();
    const source = this.value();
    if (typeof source !== 'string') fail();
    this.skip();
    let flags = '';
    if (this.peek() === ',') {
      this.pos += 1;
      this.skip();
      if (this.peek() !== ')') {
        flags = this.flags();
        this.skip();
        if (this.peek() === ',') {
          this.pos += 1;
          this.skip();
        }
      }
    }
    this.close();
    if (!pyReadable(source)) fail();
    return { source, flags };
  }

  /** Flag names joined with |, such as re.I | re.DOTALL: the flags, as i, m and s. */
  private flags(): string {
    const letters = new Set<string>();
    for (;;) {
      this.skip();
      if (this.peek() === '(') {
        this.open();
        for (const letter of this.flags()) letters.add(letter);
        this.skip();
        this.close();
      } else {
        if (this.name() !== 're') fail();
        this.skip();
        this.expect('.');
        this.skip();
        const letter = FLAG_LETTERS[this.name()];
        if (!letter) fail();
        letters.add(letter);
      }
      this.skip();
      if (this.peek() === '|' && this.peek(1) !== '=') {
        this.pos += 1;
        continue;
      }
      return ['i', 'm', 's'].filter((l) => letters.has(l)).join('');
    }
  }

  /** The arguments of the call, after its opening bracket, up to and with its closing one. */
  args(): [PyValue[], Map<string, PyValue>] {
    const args: PyValue[] = [];
    const kwargs = new Map<string, PyValue>();
    for (;;) {
      this.skip();
      const c = this.peek();
      if (c === ')') break;
      if (c === '*') fail();
      let keyword: string | null = null;
      if (c !== undefined && /[A-Za-z_]/.test(c)) {
        const save = this.pos;
        const name = this.name();
        this.skip();
        if (this.peek() === '=' && this.peek(1) !== '=') {
          this.pos += 1;
          keyword = name;
        } else {
          this.pos = save;
        }
      }
      const value = this.value();
      if (keyword !== null) {
        if (kwargs.has(keyword)) fail();
        kwargs.set(keyword, value);
      } else {
        if (kwargs.size) fail();     // a positional argument after a keyword one
        args.push(value);
      }
      this.skip();
      if (this.peek() === ',') {
        this.pos += 1;
        continue;
      }
      if (this.peek() !== ')') fail();
      break;
    }
    this.close();
    return [args, kwargs];
  }
}

/** The call text stands for, in Python's syntax (see the module comment), or null. text is a
 * whole expression: one call of a plain name, and nothing after it but a comment. */
export function readCall(text: string): PyCall | null {
  const reader = new Reader(text);
  try {
    const name = reader.name();
    reader.skip();
    reader.open();
    const [args, kwargs] = reader.args();
    reader.skip(true);
    if (!reader.atEnd()) return null;
    return { name, args, kwargs };
  } catch (error) {
    if (error === UNREADABLE) return null;
    throw error;
  }
}

// --- a regular expression both packages read ------------------------------------------------

// Python's sre MAXREPEAT: a repeat with no upper bound.
const UNBOUNDED = Infinity;
const MAX_COUNT = 100_000;

interface Width {
  lo: number;
  hi: number;
}

// The letters an escape may have outside a class, and what each is: a class (\d), an
// assertion (\b), or one character.
const CLASS_ESCAPES = new Set(['d', 'D', 'w', 'W', 's', 'S']);
const CHAR_ESCAPES: Record<string, number> = { f: 0x0c, n: 0x0a, r: 0x0d, t: 0x09, v: 0x0b };

function isSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdfff;
}

function isHigh(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}

function isLow(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff;
}

/** True when source, a regular expression's source, is one both Python's re module (with no
 * flags of its own) and JavaScript without the u flag read (see the module comment). The width
 * of a lookbehind is counted as Python counts it, by code point, and must be fixed. */
export function pyReadable(source: string): boolean {
  let i = 0;
  let depth = 0;
  const n = source.length;

  const quantifierAt = (at: number): boolean => {
    const c = source[at];
    return c === '*' || c === '+' || c === '?' || c === '{';
  };

  // {n}, {n,} or {n,m} at i: [min, max], or null for anything else.
  const braces = (): [number, number] | null => {
    const m = /^\{(\d{1,6})(?:(,)(\d{1,6})?)?\}/.exec(source.slice(i, i + 16));
    if (!m) return null;
    const lo = Number(m[1]);
    const hi = m[2] === undefined ? lo : m[3] === undefined ? UNBOUNDED : Number(m[3]);
    if (lo > MAX_COUNT || (hi !== UNBOUNDED && (hi > MAX_COUNT || hi < lo))) return null;
    i += m[0].length;
    return [lo, hi];
  };

  // An escape of one character at i (after the backslash), as a class or outside one: its
  // code, or -1 for a class escape (\d), or null for one that is not taken.
  const charEscape = (inClass: boolean): number | null => {
    const c = source[i];
    if (c === undefined) return null;
    const code = source.charCodeAt(i);
    if (CLASS_ESCAPES.has(c)) {
      i += 1;
      return -1;
    }
    if (c in CHAR_ESCAPES) {
      i += 1;
      return CHAR_ESCAPES[c];
    }
    if (c === 'b' && inClass) {
      i += 1;
      return 0x08;
    }
    if (c === 'x' || c === 'u') {
      const size = c === 'x' ? 2 : 4;
      const digits = source.slice(i + 1, i + 1 + size);
      if (digits.length !== size || !/^[0-9A-Fa-f]+$/.test(digits)) return null;
      i += 1 + size;
      return parseInt(digits, 16);
    }
    if (c === '0') {
      if (/[0-9]/.test(source[i + 1] ?? '')) return null;
      i += 1;
      return 0;
    }
    if (code < 0x80) {
      if (/[A-Za-z0-9]/.test(c)) return null;   // \c, \k, \p, \1, \a, \A ... read differently or not at all
      i += 1;
      return code;
    }
    if (isSurrogate(code)) return null;
    i += 1;
    return code;
  };

  // A class, [ read already: true when it is one both read.
  const charClass = (): boolean => {
    if (source[i] === '^') i += 1;
    if (source[i] === ']') return false;           // [] and [^]: empty in JavaScript, not in Python
    // One member: its code, -1 for a class escape, or null.
    const member = (): number | null => {
      const c = source[i];
      if (c === undefined || c === '[') return null;
      if ('-&~|'.includes(c) && source[i + 1] === c) return null;   // Python warns of a set operation
      if (c === '\\') {
        i += 1;
        return charEscape(true);
      }
      const code = source.charCodeAt(i);
      if (isSurrogate(code)) return null;
      i += 1;
      return code;
    };
    for (;;) {
      if (i >= n) return false;
      if (source[i] === ']') {
        i += 1;
        return true;
      }
      const start = member();
      if (start === null) return false;
      if (source[i] === '-' && i + 1 < n && source[i + 1] !== ']') {
        i += 1;
        if (source[i] === '-') return false;
        const end = member();
        if (end === null || start < 0 || end < 0 || end < start) return false;
      }
    }
  };

  // An atom at i, the width Python gives it, or null.
  const atom = (): Width | null => {
    const c = source[i];
    const code = source.charCodeAt(i);
    if (c === '(') {
      if (source.startsWith('(?', i) && !source.startsWith('(?:', i)) return null;
      i += source.startsWith('(?:', i) ? 3 : 1;
      depth += 1;
      if (depth > 50) return null;
      const inner = disjunction();
      if (inner === null || source[i] !== ')') return null;
      i += 1;
      depth -= 1;
      return inner;
    }
    if (c === '[') {
      i += 1;
      return charClass() ? { lo: 1, hi: 1 } : null;
    }
    if (c === '\\') {
      i += 1;
      return charEscape(false) === null ? null : { lo: 1, hi: 1 };
    }
    if (c === '.' || c === ']' || c === '}') {
      i += 1;
      return { lo: 1, hi: 1 };
    }
    if ('*+?{)|^$'.includes(c)) return null;
    if (isHigh(code) && isLow(source.charCodeAt(i + 1))) {
      i += 2;                                      // one code point in Python
      return { lo: 1, hi: 1 };
    }
    if (isSurrogate(code)) return null;
    i += 1;
    return { lo: 1, hi: 1 };
  };

  // A term at i: an assertion, or an atom and its quantifier.
  const term = (): Width | null => {
    const c = source[i];
    let width: Width | null;
    if (c === '^' || c === '$') {
      i += 1;
      width = { lo: 0, hi: 0 };
    } else if (c === '\\' && (source[i + 1] === 'b' || source[i + 1] === 'B')) {
      i += 2;
      width = { lo: 0, hi: 0 };
    } else if (source.startsWith('(?=', i) || source.startsWith('(?!', i) || source.startsWith('(?<=', i)
               || source.startsWith('(?<!', i)) {
      const behind = source[i + 2] === '<';
      i += behind ? 4 : 3;
      depth += 1;
      if (depth > 50) return null;
      const inner = disjunction();
      if (inner === null || source[i] !== ')') return null;
      i += 1;
      depth -= 1;
      if (behind && inner.lo !== inner.hi) return null;   // Python: a lookbehind needs a fixed width
      width = { lo: 0, hi: 0 };
    } else {
      const found = atom();
      if (found === null) return null;
      if (!quantifierAt(i)) return found;
      let lo: number;
      let hi: number;
      if (source[i] === '{') {
        const counts = braces();
        if (counts === null) return null;
        [lo, hi] = counts;
      } else {
        lo = source[i] === '+' ? 1 : 0;
        hi = source[i] === '?' ? 1 : UNBOUNDED;
        i += 1;
      }
      if (source[i] === '?') i += 1;               // lazy
      if (quantifierAt(i)) return null;            // a repeat of a repeat
      return {
        lo: found.lo * lo,
        hi: found.hi === 0 ? 0 : hi === UNBOUNDED ? UNBOUNDED : found.hi * hi,
      };
    }
    return quantifierAt(i) ? null : width;         // an assertion is never repeated here
  };

  const alternative = (): Width | null => {
    let lo = 0;
    let hi = 0;
    while (i < n && source[i] !== '|' && source[i] !== ')') {
      const width = term();
      if (width === null) return null;
      lo += width.lo;
      hi += width.hi;
    }
    return { lo, hi };
  };

  function disjunction(): Width | null {
    let lo = Infinity;
    let hi = 0;
    for (;;) {
      const width = alternative();
      if (width === null) return null;
      lo = Math.min(lo, width.lo);
      hi = Math.max(hi, width.hi);
      if (source[i] !== '|') return { lo, hi };
      i += 1;
    }
  }

  const whole = disjunction();
  return whole !== null && i === n;
}
