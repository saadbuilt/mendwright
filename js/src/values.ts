// What a read must return: heal.locator(key, selector, { expectValue }) and
// open(page, { expectValues: { key: check } }) (Python's mendwright/values.py).
//
// A heal can pass every check of the gate and still be the wrong element. After a redesign, a
// price selector that heals onto the rating next to the price reads "4.5", and a scraper
// saves that. expectValue says what a read of the key must look like. When a read goes
// through a selector that a heal saved, mendwright checks the value it returns: one that
// passes confirms a heal made in this process, as expect on an action does, and one that
// fails quarantines the heal and throws AttentionRequired instead of returning the value. A
// read through the selector in your code is never checked, so a heal is the only thing this
// can block.
//
// The checks (expectation()): one of NAMES, a RegExp (it searches the value, as Python's
// re.search() does) or a function that takes the value as a string and returns something
// truthy when it is right.
//
// parseNumber() and parseMoney() read numbers as most locales write them: 1,299.00,
// 1.299,00, 1 299,00 and 1'299.00, with an Indian 1,29,999.00 too. A single . or , followed by
// exactly three digits, as in 1.299 or 1,299, is read as a thousands separator, as a price
// usually is; 0,299 and 1234.567 have a decimal part. In an amount of a currency with three
// decimals (KWD, BHD, OMR, JOD, TND), that . is the decimal point, as those amounts are
// written (KWD 1.250 is 1.250), and that , could be either (1,250 TND is 1.250 in Tunisia and
// 1250 in English), so parseMoney() gives null for it, though it is money. The text is read
// in its compatibility form first, so a non-breaking space is a space and a fullwidth digit a
// digit, and digits of other scripts count as digits.
//
// The amounts are Python's Decimal written as str() writes it: a string such as '1299.00',
// with its trailing zeros, so nothing loses precision. Every test here is the Python
// package's, character class for character class: Python's \d, \w and \s, str.isdigit(),
// str.casefold(), unicodedata.decimal() and unicodedata.combining() are written out below,
// and the tests compare them with Python over every character.

import { PY_SPACE, isDigitChar, pyLen, pyLstrip, pyRepr, pyRstrip, pySlice, pySplit, pyStrip, urlsplit } from './py';

/** The checks expectValue takes by name, and how a message says a value is not one. */
export const NAMES: ReadonlyMap<string, string> = new Map([
  ['money', 'is not money'],
  ['number', 'is not a number'],
  ['integer', 'is not a whole number'],
  ['percent', 'is not a percentage'],
  ['date', 'is not a date'],
  ['email', 'is not an email address'],
  ['url', 'is not a URL'],
]);

/** A check expectValue takes by name. */
export type ValueName = 'money' | 'number' | 'integer' | 'percent' | 'date' | 'email' | 'url';

/** What expectValue takes: one of the names, a RegExp the value must match somewhere in (as
 * Python's re.search(); anchor it with ^ and $ to match the whole value), or a function that
 * gets the value as a string and returns something truthy (or a promise of it) when it is
 * right. */
export type ExpectValue = ValueName | RegExp | ((value: string) => unknown);

/** The reads whose value is checked: the Locator reads that return a value. */
export const VALUE_READS: ReadonlySet<string> = new Set(['innerText', 'textContent', 'inputValue', 'getAttribute']);

/** How much of a value a message quotes. */
export const VALUE_LIMIT = 80;

// --- Python's character classes ------------------------------------------------------------

// \d of a Python regular expression of str: a decimal digit of any script, as
// str.isdecimal() (Unicode category Nd).
const DECIMAL = /^\p{Nd}$/u;
// [^\W\d_] of a Python regular expression of str: a letter, or a number that is not a
// decimal digit (str.isalnum() without \d and _).
export const WORD = '[\\p{L}\\p{Nl}\\p{No}]';
const SPACE = new RegExp('[' + PY_SPACE + ']');

const digitValues = new Map<number, number>();

/** unicodedata.decimal() of a decimal digit: each script's digits are ten code points in a
 * row, 0 to 9, so the value is how far it is from the first of its run, modulo 10 (the
 * mathematical digits are five runs in a row). */
export function decimalValue(ch: string): number {
  const code = ch.codePointAt(0) as number;
  let value = digitValues.get(code);
  if (value === undefined) {
    let start = code;
    while (start > 0 && DECIMAL.test(String.fromCodePoint(start - 1))) start -= 1;
    value = (code - start) % 10;
    digitValues.set(code, value);
  }
  return value;
}

/** str.isdigit(): not empty, and every character a digit (a decimal one, or a superscript
 * and the like). */
function isDigitText(text: string): boolean {
  if (!text) return false;
  for (const ch of text) if (!isDigitChar(ch)) return false;
  return true;
}

/** int() of a str.isdigit() text that mendwright cleaned (ASCII digits). A digit that is not
 * a decimal one, such as the Ethiopic ፩, throws, as int() raises ValueError for it. */
function pyInt(text: string): number {
  if (!/^[0-9]+$/.test(text)) throw new RangeError('invalid literal for int(): ' + pyRepr(text));
  return Number(text);
}

/** The first and last character (code point) of text, or ''. */
function firstChar(text: string): string {
  const code = text.codePointAt(0);
  return code === undefined ? '' : String.fromCodePoint(code);
}

function lastChar(text: string): string {
  const chars = Array.from(text.slice(-2));
  return chars.length ? chars[chars.length - 1] : '';
}

/** str.casefold() of a text in its compatibility form (NFKC), which every caller here has:
 * the upper case of each character in lower case, but for the characters whose fold is not
 * that (the dotless ı, ẞ and Cherokee, which folds to its capitals). */
export function casefold(text: string): string {
  let out = '';
  for (const ch of text) {
    const code = ch.codePointAt(0) as number;
    if (code === 0x131) out += ch;
    else if (code === 0x1e9e) out += 'ss';
    else if (code >= 0x13a0 && code <= 0x13f5) out += ch;
    else if (code >= 0x13f8 && code <= 0x13fd) out += String.fromCodePoint(code - 8);
    else if (code >= 0xab70 && code <= 0xabbf) out += String.fromCodePoint(code - 0xab70 + 0x13a0);
    else out += ch.toUpperCase().toLowerCase();
  }
  return out;
}

const combiningMarks = new Map<number, boolean>();

/** unicodedata.combining(ch) != 0 for a character of a decomposed (NFD or NFKD) text: the
 * canonical ordering of NFD moves it before a mark of class 1 that follows it when its class
 * is above 1, and before a mark of class 230 that comes first when its class is from 1 to
 * 229. A character of class 0 never moves. */
export function isCombining(ch: string): boolean {
  const code = ch.codePointAt(0) as number;
  let value = combiningMarks.get(code);
  if (value === undefined) {
    const after = 'a' + ch + '\u0334';
    const before = 'a\u0301' + ch;
    value = after.normalize('NFD') !== after || before.normalize('NFD') !== before;
    combiningMarks.set(code, value);
  }
  return value;
}

// --- numbers -------------------------------------------------------------------------------

// Characters that group the digits of a number. ' ' also stands for a non-breaking, narrow or
// thin space, which the compatibility form turns into a space.
const GROUPERS = "'’ ";
// The texts these run on are cleaned (clean()), so every decimal digit is an ASCII one.
const BODY = /^([+-]?) ?([0-9](?:[0-9.,'’ ]*[0-9])?)$/u;
const GROUP_MARKS = /[.,'’ ]/;

/** text in its compatibility form (NFKC), with the decimal digits of any script as ASCII
 * digits, a minus sign as -, the Arabic separators and percent sign as , . and %, and runs of
 * whitespace as one space, trimmed (Python's _clean). */
export function clean(text: string): string {
  let out = '';
  for (const ch of text.normalize('NFKC')) {
    if (DECIMAL.test(ch)) out += String(decimalValue(ch));
    else if (ch === '\u2212') out += '-';
    else if (ch === '\u066c') out += ',';
    else if (ch === '\u066b') out += '.';
    else if (ch === '\u066a') out += '%';
    else out += ch;
  }
  return pySplit(out).join(' ');
}

function count(text: string, mark: string): number {
  return text.split(mark).length - 1;
}

/** True when the digit groups of a number's whole part are grouped as numbers are: the first
 * one 1 to 3 digits and each other one 3 (1,299,000), or the last one 3 and the ones before
 * it 2 (an Indian 1,29,999). */
function grouped(groups: string[]): boolean {
  const first = pyLen(groups[0]);
  if (!groups.every(isDigitText) || !(first >= 1 && first <= 3)) return false;
  if (groups.slice(1).every((g) => pyLen(g) === 3)) return true;
  return first <= 2 && pyLen(groups[groups.length - 1]) === 3 && groups.slice(1, -1).every((g) => pyLen(g) === 2);
}

/** str(Decimal(sign + digits + '.' + frac)) as Python writes it: trailing zeros kept, a
 * leading + and leading zeros dropped, a - kept even on zero, and scientific notation once
 * the exponent of the first digit is below -6 (Decimal('0.0000001') is 1E-7). */
function decimalText(sign: string, digits: string, frac: string): string {
  const coefficient = (digits + frac).replace(/^0+/, '') || '0';
  const exponent = -frac.length;
  const left = exponent + coefficient.length;
  const dot = left > -6 ? left : 1;
  let whole: string;
  let part: string;
  if (dot <= 0) {
    whole = '0';
    part = '.' + '0'.repeat(-dot) + coefficient;
  } else if (dot >= coefficient.length) {
    whole = coefficient + '0'.repeat(dot - coefficient.length);
    part = '';
  } else {
    whole = coefficient.slice(0, dot);
    part = '.' + coefficient.slice(dot);
  }
  const shift = left - dot;
  const exp = shift === 0 ? '' : 'E' + (shift > 0 ? '+' : '') + shift;
  return (sign === '-' ? '-' : '') + whole + part + exp;
}

/** [amount, has a decimal part] for a cleaned text that is a number and nothing else, such as
 * -1.299,50, else null. thousandths is for an amount of a currency with three decimals: a
 * single . before exactly three digits is its decimal point, and a single , there could be
 * the point or a thousands separator, which gives [null, true]. */
function number(text: string, thousandths = false): [string | null, boolean] | null {
  const match = BODY.exec(text);
  if (!match) return null;
  const sign = match[1];
  const body = match[2];
  let unsure = false;
  const spaced = [...body].some((ch) => GROUPERS.includes(ch));
  const dots = count(body, '.');
  const commas = count(body, ',');
  let point: string | null;
  if (dots && commas) {
    point = body.lastIndexOf('.') > body.lastIndexOf(',') ? '.' : ',';
    if (count(body, point) !== 1) return null;
  } else if (dots || commas) {
    const mark = dots ? '.' : ',';
    const at = body.lastIndexOf(mark);
    const whole = body.slice(0, at);
    const tail = body.slice(at + 1);
    if (count(body, mark) > 1) {
      point = null;            // 1.299.000: it groups
    } else if (spaced) {
      point = mark;            // 1 299,50: the spaces group, so the mark is the point
    } else if (pyLen(tail) === 3 && pyLen(whole) >= 1 && pyLen(whole) <= 3 && !whole.startsWith('0')) {
      point = null;            // 1,299 and 1.299 read as thousands, as prices do
      if (thousandths && mark === '.') point = mark;   // KWD 1.250: one dinar and 250 fils
      else if (thousandths) unsure = true;             // 1,250 TND: 1.250 in Tunisia, 1250 in English
    } else {
      point = mark;
    }
  } else {
    point = null;
  }
  let whole = body;
  let frac = '';
  if (point) {
    const at = body.lastIndexOf(point);
    whole = body.slice(0, at);
    frac = body.slice(at + 1);
  }
  if (point && !isDigitText(frac)) return null;
  const kinds = new Set([...whole].filter((ch) => !isDigitChar(ch)).map((ch) => (ch === '’' ? "'" : ch)));
  if (kinds.size > 1) return null;   // two kinds of separators in the whole part: 1 299.000
  const groups = whole.split(GROUP_MARKS);
  if (groups.length > 1 && !grouped(groups)) return null;
  const digits = groups.join('');
  if (unsure) return [null, true];
  return [decimalText(sign, digits, frac), Boolean(point)];
}

/** The number text is, as Python's Decimal writes it (str()), or null when it is not a number
 * and nothing else: parseNumber('1.299,00') and parseNumber('1,299.00') are '1299.00',
 * parseNumber('1 299,5') is '1299.5', and '12 kg' or '$5' is null (see parseMoney()). The
 * string keeps every digit, the trailing zeros too, so nothing loses precision: pass it to
 * Number() or a decimal library yourself. */
export function parseNumber(text: unknown): string | null {
  if (typeof text !== 'string') return null;
  const found = number(clean(text));
  return found ? found[0] : null;
}

// ISO 4217 codes and the local abbreviations prices are written with, in lower case.
const CODES: ReadonlySet<string> = new Set([
  'usd', 'eur', 'gbp', 'jpy', 'cny', 'rmb', 'chf', 'cad', 'aud', 'nzd', 'sek', 'nok', 'dkk',
  'isk', 'pln', 'czk', 'huf', 'ron', 'bgn', 'rsd', 'uah', 'rub', 'try', 'ils', 'inr', 'pkr',
  'bdt', 'lkr', 'npr', 'idr', 'myr', 'sgd', 'thb', 'vnd', 'php', 'krw', 'twd', 'hkd', 'brl',
  'mxn', 'ars', 'clp', 'cop', 'pen', 'uyu', 'zar', 'ngn', 'kes', 'ghs', 'egp', 'mad', 'tnd',
  'aed', 'sar', 'qar', 'kwd', 'bhd', 'omr', 'jod',
  'kr', 'kn', 'zł', 'zl', 'kč', 'kc', 'ft', 'lei', 'лв', 'руб', 'р', 'rs', 'r', 'rm', 'rp',
  'fr', 'sfr', 'tl', 'dh', 'dhs', 'грн', 'din', 'дин', 'ден',
]);
// The codes of CODES whose currency has three decimals: KWD 1.250 is one dinar 250 fils.
const THOUSANDTHS: ReadonlySet<string> = new Set(['kwd', 'bhd', 'omr', 'jod', 'tnd']);
const DOLLAR_PREFIX = /^([A-Z]{1,3})\$/;          // US$, C$, A$, R$, NZ$, HK$
const WORD_PREFIX = new RegExp('^(' + WORD + '{1,4})\\.? ?(?=[0-9+-])', 'u');
const WORD_SUFFIX = new RegExp('(?<=[0-9\\-–—]) ?(' + WORD + '{1,4})\\.?$', 'u');
// 129,- or 129:- or 12.– : a whole amount, written without its cents.
const NO_CENTS = /(?<=[0-9])[.,:][-–—]$/u;
const CURRENCY_SIGN = /^\p{Sc}$/u;

/** [currency, rest] when text starts with a currency sign or code, else [null, text]. */
function prefix(text: string): [string | null, string] {
  const first = firstChar(text);
  if (first && CURRENCY_SIGN.test(first)) return [first, pyLstrip(text.slice(first.length))];
  let match = DOLLAR_PREFIX.exec(text);
  if (match) return [match[0], pyLstrip(text.slice(match[0].length))];
  match = WORD_PREFIX.exec(text);
  if (match && CODES.has(casefold(match[1]))) return [match[1], text.slice(match[0].length)];
  return [null, text];
}

/** [currency, rest] when text ends with a currency sign (with symbols) or code, else
 * [null, text]. */
function suffix(text: string, symbols = true): [string | null, string] {
  const last = lastChar(text);
  if (symbols && last && CURRENCY_SIGN.test(last)) return [last, pyRstrip(text.slice(0, -last.length))];
  const match = WORD_SUFFIX.exec(text);
  if (match && CODES.has(casefold(match[1]))) return [match[1], pyRstrip(text.slice(0, match.index))];
  return [null, text];
}

/** [amount, currency] for a cleaned text that is an amount with a currency sign or code
 * before or after it, or written as 129,- or 129:-, else null. A sign may come before the
 * currency or the number. The amount is null for an amount that is money but could be read
 * two ways (see number()'s thousandths). */
function money(text: string): [string | null, string] | null {
  let sign = '';
  const second = firstChar(text.slice(1));
  if ((text.startsWith('-') || text.startsWith('+')) && second && !isDigitText(second)) {
    sign = text[0];
    text = pyLstrip(text.slice(1));
  }
  let [currency, rest] = prefix(text);
  if (currency !== null) {
    rest = suffix(rest, false)[1];    // $12.00 USD
  } else {
    [currency, rest] = suffix(text);
  }
  if (NO_CENTS.test(rest)) {
    currency = currency || rest.slice(-2);
    rest = rest.slice(0, -2);
  }
  if (currency === null || (sign && (rest.startsWith('-') || rest.startsWith('+')))) return null;
  const found = number(sign + rest, THOUSANDTHS.has(casefold(currency)));
  return found ? [found[0], currency] : null;
}

/** The amount of a price, as Python's Decimal writes it, or null when text is not an amount
 * with a currency sign ($, €, £, ¥, ₹ and the other Unicode currency signs, or US$ and the
 * like) or a currency code (USD, EUR, ...) or abbreviation (kr, zł, Kč, Ft, lei, Rs, R, CHF,
 * ...) before or after it, and nothing else. parseMoney('$1,299.00'), parseMoney('1.299,00 €')
 * and parseMoney('USD 1299') are '1299.00' or '1299', parseMoney('129 kr') is '129', and
 * '129,-' or '129:-' reads as 129 too. A minus sign may come before the currency or the
 * amount. '1,299.00' without a currency is null (see parseNumber()). In a currency with three
 * decimals (KWD, BHD, OMR, JOD, TND), a single . before three digits is the decimal point, so
 * parseMoney('KWD 1.250') is '1.250', and a single , there is null, since 'TND 1,250' is
 * 1.250 in Tunisia and 1250 in English. */
export function parseMoney(text: unknown): string | null {
  if (typeof text !== 'string') return null;
  const found = money(clean(text));
  return found ? found[0] : null;
}

/** The number of a percentage such as 12.5 %, -15% or %20, else null. */
function percent(text: string): string | null {
  let rest: string;
  if (text.endsWith('%')) rest = pyRstrip(text.slice(0, -1));
  else if (text.startsWith('%')) rest = pyLstrip(text.slice(1));
  else return null;
  const found = number(rest);
  return found ? found[0] : null;
}

// --- dates -----------------------------------------------------------------------------------

/** word in lower case without its accents: Août is aout, März is marz. */
function bare(word: string): string {
  let out = '';
  for (const ch of casefold(word).normalize('NFKD')) if (!isCombining(ch)) out += ch;
  return out;
}

// Month names and abbreviations in English, German, French, Spanish, Italian, Dutch and
// Portuguese, without accents.
const MONTH_WORDS = [
  'january jan januar janner janvier janv enero ene gennaio gen januari janeiro',
  'february feb februar fevrier fevr fev febrero febbraio februari fevereiro',
  'march mar marz mrz mars marzo maart mrt marco',
  'april apr avril avr abril abr aprile',
  'may mai mayo maggio mag mei maio',
  'june jun juni juin junio giugno giu junho',
  'july jul juli juillet juil julio luglio lug julho',
  'august aug aout agosto ago augustus',
  'september sep sept septembre septiembre setiembre set settembre setembro',
  'october oct oktober okt octobre octubre ottobre ott outubro out',
  'november nov novembre noviembre novembro',
  'december dec dezember dez decembre diciembre dic dicembre dezembro',
];
const MONTHS: ReadonlyMap<string, number> = new Map(
  MONTH_WORDS.flatMap((words, i) => words.split(' ').map((word) => [word, i + 1] as [string, number])));
const WEEKDAYS: ReadonlySet<string> = new Set((
  'monday mon tuesday tue tues wednesday wed thursday thu thur thurs friday fri saturday '
  + 'sat sunday sun montag dienstag mittwoch donnerstag freitag samstag sonnabend sonntag mo '
  + 'di mi do fr sa so lundi mardi mercredi jeudi vendredi samedi dimanche lun mar mer jeu ven '
  + 'sam dim lunes martes miercoles jueves viernes sabado domingo mie jue vie sab dom lunedi '
  + 'martedi mercoledi giovedi venerdi domenica maandag dinsdag woensdag donderdag vrijdag '
  + 'zaterdag zondag ma wo vr za zo segunda terca quarta quinta sexta feira').split(' '));
// Words a written date may have besides its parts: 15 de octubre de 2026, the 15th of
// October, 1er octobre, 1º de mayo (º reads as o in compatibility form).
const FILLERS: ReadonlySet<string> = new Set('de del of the le la am den el du st nd rd th er e o'.split(' '));
const ISO = /^([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})(?:[t ][0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)? ?(?:z|[+-][0-9]{2}:?[0-9]{2})?)?$/u;
const NUMERIC = /^([0-9]{1,4})([./-])([0-9]{1,2})\2([0-9]{1,4})\.?$/u;
const CJK = /^([0-9]{4}) ?[年년] ?([0-9]{1,2}) ?[月월] ?([0-9]{1,2}) ?[日일]$/u;
const TIME = /,? ?(?:(?:at|um|à|a|às|as|alle|om) )?(?:[0-9]{1,2}(?::[0-9]{2}){1,2}(?: ?[ap]\.?m\.?)?(?: uhr)?|[0-9]{1,2}h[0-9]{2})$/u;
const PIECES = new RegExp('[0-9]+|' + WORD + '+', 'gu');

const DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/** True for a calendar date. year null is any leap year. */
function real(year: number | null, month: number, day: number): boolean {
  if (!(month >= 1 && month <= 12) || day < 1) return false;
  const y = year === null ? 2000 : year;
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  return day <= DAYS[month - 1] + (month === 2 && leap ? 1 : 0);
}

/** A year from 2 or 4 digits (26 is 2026), else null. */
function yearOf(text: string): number | null {
  if (pyLen(text) === 4) return pyInt(text);
  return pyLen(text) === 2 ? 2000 + pyInt(text) : null;
}

/** True when a sep b sep c is a date as Y-M-D, D-M-Y or M-D-Y. */
function numericDate(a: string, b: string, c: string): boolean {
  if (a.length === 4) return real(pyInt(a), pyInt(b), pyInt(c));
  const year = yearOf(c);
  if (year === null || a.length > 2) return false;
  return real(year, pyInt(b), pyInt(a)) || real(year, pyInt(a), pyInt(b));
}

/** True when the pieces of a date written with its month's name are a day, the month and a
 * year in an order dates are written in. */
function wordedDate(pieces: string[]): boolean {
  let months = pieces.flatMap((p, i) => (MONTHS.has(p) ? [i] : []));
  if (months.length > 1 && WEEKDAYS.has(pieces[0])) {
    pieces = pieces.slice(1);
    months = months.slice(1).map((i) => i - 1);
  }
  if (months.length !== 1) return false;
  const at = months[0];
  const month = MONTHS.get(pieces[at]) as number;
  const rest = pieces.filter((_, i) => i !== at);
  if (!rest.length || !rest.every(isDigitText) || rest.length > 2) return false;
  if (rest.length === 1) {
    const value = rest[0];
    if (pyLen(value) === 4) return at === 0;           // October 2026
    return pyLen(value) <= 2 && real(null, month, pyInt(value));   // 15 Oct, Oct 15
  }
  const [first, second] = rest;
  if (at === 0 || (at === 1 && pyLen(first) <= 2)) {   // Oct 15 2026, 15 Oct 2026
    const year = yearOf(second);
    return pyLen(first) <= 2 && year !== null && real(year, month, pyInt(first));
  }
  if (at === 1) {                                      // 2026 Oct 15
    return pyLen(first) === 4 && pyLen(second) <= 2 && real(pyInt(first), month, pyInt(second));
  }
  return false;
}

function isDate(value: string): boolean {
  let text = casefold(clean(value));
  // An ISO 8601 date-time first: cutting its time off would leave 2026-10-15t.
  let match = ISO.exec(text);
  if (match) return real(pyInt(match[1]), pyInt(match[2]), pyInt(match[3]));
  text = pyStrip(text.replace(TIME, ''));
  if (!text) return false;
  match = ISO.exec(text);
  if (match) return real(pyInt(match[1]), pyInt(match[2]), pyInt(match[3]));
  match = NUMERIC.exec(text);
  if (match) return numericDate(match[1], match[3], match[4]);
  match = CJK.exec(text);
  if (match) return real(pyInt(match[1]), pyInt(match[2]), pyInt(match[3]));
  const pieces = (text.match(PIECES) ?? []).map(bare)
    .filter((p) => !FILLERS.has(p) && !(WEEKDAYS.has(p) && !MONTHS.has(p)));
  return wordedDate(pieces);
}

// --- the other checks ------------------------------------------------------------------------

// Python's _EMAIL, compiled with re.IGNORECASE: mailto: in any case (its i also matches the
// dotless ı and the dotted İ, as Python's does), and \s and \S as Python reads them.
const ADDRESS = '[^' + PY_SPACE + '@<>()\\[\\],;:"\']';
const HOST = '[^' + PY_SPACE + '@<>()\\[\\],;:"\'.]';
const LABEL = '[^' + PY_SPACE + '@<>()\\[\\],;:"\'.?]';
const EMAIL = new RegExp('^(?:[mM][aA][iIıİ][lL][tT][oO]:)?' + ADDRESS + '+@' + HOST + '+(?:\\.' + LABEL + '+)+'
  + '(?:\\?[^' + PY_SPACE + ']*)?$', 'u');

const IPV4_OCTET = /^[0-9]{1,3}$/;
const HEX = /^[0-9a-fA-F]*$/;

/** ipaddress.IPv4Address(text) does not raise. */
function isIPv4(text: string): boolean {
  if (!text || text.includes('/')) return false;
  const octets = text.split('.');
  return octets.length === 4 && octets.every((o) => IPV4_OCTET.test(o) && (o === '0' || o[0] !== '0') && Number(o) <= 255);
}

/** ipaddress.IPv6Address(text) does not raise. */
function isIPv6(text: string): boolean {
  if (text.includes('/')) return false;
  const cut = text.indexOf('%');
  if (cut >= 0) {
    const scope = text.slice(cut + 1);
    if (!scope || scope.includes('%')) return false;
    text = text.slice(0, cut);
  }
  if (!text) return false;
  const parts = text.split(':');
  if (parts.length < 3) return false;
  if (parts[parts.length - 1].includes('.')) {
    if (!isIPv4(parts.pop() as string)) return false;
    parts.push('0', '0');
  }
  if (parts.length > 9) return false;
  let skip: number | null = null;
  for (let i = 1; i < parts.length - 1; i++) {
    if (!parts[i]) {
      if (skip !== null) return false;
      skip = i;
    }
  }
  let hi: number;
  let lo: number;
  if (skip !== null) {
    hi = skip;
    lo = parts.length - skip - 1;
    if (!parts[0] && --hi) return false;
    if (!parts[parts.length - 1] && --lo) return false;
    if (8 - (hi + lo) < 1) return false;
  } else {
    if (parts.length !== 8 || !parts[0] || !parts[parts.length - 1]) return false;
    hi = parts.length;
    lo = 0;
  }
  const used = [...parts.slice(0, hi), ...(lo ? parts.slice(-lo) : [])];
  return used.every((p) => p.length >= 1 && p.length <= 4 && HEX.test(p));
}

/** urllib.parse's _check_bracketed_netloc() does not raise: a netloc whose host is in
 * brackets has nothing before them and only a port after them, and holds an IPv6 address or
 * an IPvFuture one. */
function bracketsHold(netloc: string): boolean {
  const hostinfo = netloc.slice(netloc.lastIndexOf('@') + 1);
  const open = hostinfo.indexOf('[');
  let host: string;
  if (open >= 0) {
    if (open > 0) return false;
    const inside = hostinfo.slice(open + 1);
    const close = inside.indexOf(']');
    host = close < 0 ? inside : inside.slice(0, close);
    const port = close < 0 ? '' : inside.slice(close + 1);
    if (port && !port.startsWith(':')) return false;
  } else {
    const colon = hostinfo.indexOf(':');
    host = colon < 0 ? hostinfo : hostinfo.slice(0, colon);
  }
  if (host.startsWith('v')) return /^v[a-fA-F0-9]+\..+$/u.test(host);
  return !isIPv4(host) && isIPv6(host);
}

/** urllib.parse's _checknetloc() does not raise: no character of a netloc that is not ASCII
 * turns into / ? # @ or : in compatibility form. */
function netlocNormal(netloc: string): boolean {
  if (!netloc || /^[\x00-\x7f]*$/.test(netloc)) return true;
  const n = netloc.replace(/[@:#?]/g, '');
  const normal = n.normalize('NFKC');
  return n === normal || ![...'/?#@:'].some((c) => normal.includes(c));
}

/** The hostname urlsplit(url).hostname gives (Python 3.12), or null; it throws where
 * urlsplit() raises ValueError. */
function urlHost(url: string): { scheme: string; host: string | null } {
  const parts = urlsplit(url);   // throws for a bracket without its pair
  const netloc = parts.netloc;
  if (netloc.includes('[') && netloc.includes(']') && !bracketsHold(netloc)) throw new Error('Invalid IPv6 URL');
  if (!netlocNormal(netloc)) throw new Error('netloc contains invalid characters under NFKC normalization');
  const hostinfo = netloc.slice(netloc.lastIndexOf('@') + 1);
  const open = hostinfo.indexOf('[');
  let host: string;
  if (open >= 0) {
    const inside = hostinfo.slice(open + 1);
    const close = inside.indexOf(']');
    host = close < 0 ? inside : inside.slice(0, close);
  } else {
    const colon = hostinfo.indexOf(':');
    host = colon < 0 ? hostinfo : hostinfo.slice(0, colon);
  }
  return { scheme: parts.scheme, host: host || null };
}

/** An http or https URL with a host, one that starts with // (no scheme), or a path that
 * starts with /, ./ or ../, as getAttribute('href') gives for a relative link. No spaces. */
function isUrl(text: string): boolean {
  if (!text || SPACE.test(text)) return false;
  if (text.startsWith('./') || text.startsWith('../') || (text.startsWith('/') && !text.startsWith('//'))) return true;
  try {
    const { scheme, host } = urlHost(text.startsWith('//') ? 'https:' + text : text);
    return (scheme.toLowerCase() === 'http' || scheme.toLowerCase() === 'https') && Boolean(host);
  } catch {
    return false;
  }
}

function isInteger(text: string): boolean {
  const found = number(clean(text));
  return found !== null && !found[1];
}

/** The checks expectValue takes by name, each on a string. */
export const CHECKS: ReadonlyMap<string, (text: string) => boolean> = new Map([
  ['money', (text: string) => money(clean(text)) !== null],
  ['number', (text: string) => number(clean(text)) !== null],
  ['integer', isInteger],
  ['percent', (text: string) => percent(clean(text)) !== null],
  ['date', isDate],
  ['email', (text: string) => EMAIL.test(pyStrip(text))],
  ['url', (text: string) => isUrl(pyStrip(text))],
]);

// --- expectValue -------------------------------------------------------------------------------

const IDENTIFIER = /^[\p{ID_Start}$_][\p{ID_Continue}$\u200c\u200d]*$/u;

function isRegExp(value: unknown): value is RegExp {
  return Object.prototype.toString.call(value) === '[object RegExp]';
}

/** The type of value as a message names it: string, number, array, null, ... */
function typeName(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (isRegExp(value)) return 'RegExp';
  return typeof value;
}

function listed(names: Iterable<string>): string {
  const all = [...names].map((n) => "'" + n + "'");
  return all.slice(0, -1).join(', ') + ' or ' + all[all.length - 1];
}

/** What expectValue asked for. passes(value) says whether a read's value is that; shown is
 * how a message names the argument, refusal how it says a value is not that. */
export class Expectation {
  constructor(readonly spec: string | RegExp | ((value: string) => unknown),
              readonly test: (value: string) => unknown, readonly shown: string, readonly refusal: string) {}

  /** True when value is a string that the check accepts. A check that throws, or whose
   * promise rejects, refuses. null passes no check, and a function is not called for it. */
  async passes(value: unknown): Promise<boolean> {
    if (typeof value !== 'string') return false;
    try {
      return Boolean(await this.test(value));
    } catch {
      return false;
    }
  }
}

/** The Expectation for an expectValue argument, or null for null and undefined. Throws
 * TypeError for anything that is not one of NAMES, a RegExp or a function, at once. key names
 * the key in the message. */
export function expectation(spec: unknown, key?: string | null): Expectation | null {
  if (spec === null || spec === undefined) return null;
  if (spec instanceof Expectation) return spec;
  const where = key === undefined || key === null ? 'expectValue' : 'expectValue for ' + pyRepr(key);
  if (typeof spec === 'string') {
    const name = pyStrip(spec).toLowerCase();
    const refusal = NAMES.get(name);
    if (refusal === undefined) {
      throw new TypeError('mendwright: ' + where + ' takes ' + listed(NAMES.keys()) + ', a RegExp or a function, got '
        + pyRepr(spec) + '.');
    }
    return new Expectation(name, CHECKS.get(name) as (text: string) => boolean, 'expectValue: ' + pyRepr(name), refusal);
  }
  if (isRegExp(spec)) {
    // A search, as re.search() is: never sticky, and it never reads or moves the lastIndex of
    // a RegExp with the g or y flag.
    const plain = new RegExp(spec.source, spec.flags.replace(/[gy]/g, ''));
    return new Expectation(spec, (value) => plain.test(value), 'expectValue: ' + String(spec),
      'does not match ' + String(spec));
  }
  if (typeof spec === 'function') {
    const fn = spec as (value: string) => unknown;
    // JavaScript names a function written in place after where it was written: { expectValue:
    // (v) => ... } is named expectValue, and { price: (v) => ... } of expectValues price. Such
    // a name is not the function's own, so the message says "the function".
    const own = typeof fn.name === 'string' && fn.name !== 'expectValue' && fn.name !== key;
    const named = own && IDENTIFIER.test(fn.name) ? fn.name : '';
    return new Expectation(fn, fn, 'expectValue: ' + (named || 'a function'),
      'was refused by ' + (named ? named + '()' : 'the function'));
  }
  throw new TypeError('mendwright: ' + where + ' takes ' + listed(NAMES.keys()) + ', a RegExp or a function that takes '
    + 'the value and returns true when it is right, got ' + typeName(spec) + '.');
}

/** {key: Expectation} for open({ expectValues }): an object (or a Map) of keys to what
 * expectValue takes. A key whose check is null or undefined has none. Throws TypeError for
 * anything else. */
export function expectations(specs: unknown): Map<string, Expectation> {
  const out = new Map<string, Expectation>();
  if (specs === null || specs === undefined) return out;
  let entries: [unknown, unknown][];
  const proto = typeof specs === 'object' ? Object.getPrototypeOf(specs) : undefined;
  if (specs instanceof Map) {
    entries = [...specs.entries()];
  } else if (proto === Object.prototype || proto === null) {
    entries = Object.entries(specs as object);
  } else {
    throw new TypeError("mendwright: expectValues takes an object of keys to checks, such as { '#price': 'money' }, got "
      + typeName(specs) + '.');
  }
  for (const [key, spec] of entries) {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('mendwright: expectValues takes keys as strings, got ' + pyRepr(key) + '.');
    }
    const found = expectation(spec, key);
    if (found !== null) out.set(key, found);
  }
  return out;
}

/** How a message names a read: innerText(), getAttribute('content'). The options object is
 * left out, as the Python package leaves out keyword arguments. */
export function readText(verb: string, args: readonly unknown[]): string {
  return verb + '(' + args.filter((a) => typeof a === 'string').map(pyRepr).join(', ') + ')';
}

// The characters str.isprintable() is false for, which repr() writes as an escape: control and
// format characters, surrogates, private use, unassigned, and separators but the space.
const UNPRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;
const BACKSLASH = '\\';

/** repr() of a string, as Python writes it: pyRepr(), with the characters it leaves as they
 * are that Python escapes, such as a non-breaking or a narrow space in a price, written as
 * Python writes them (\xa0, \u202f, \U000e0001). */
function reprText(value: string): string {
  let out = '';
  for (const ch of pyRepr(value)) {
    if (ch === ' ' || !UNPRINTABLE.test(ch)) {
      out += ch;
      continue;
    }
    const code = ch.codePointAt(0) as number;
    const [mark, width] = code < 0x100 ? ['x', 2] : code < 0x10000 ? ['u', 4] : ['U', 8];
    out += BACKSLASH + mark + code.toString(16).padStart(width, '0');
  }
  return out;
}

/** A value as a message quotes it, cut to VALUE_LIMIT characters: a string as Python's repr()
 * writes it, and null as null. */
export function quoted(value: unknown): string {
  const text = typeof value === 'string' ? reprText(value) : value === null || value === undefined ? 'null' : String(value);
  return pyLen(text) <= VALUE_LIMIT ? text : pySlice(text, VALUE_LIMIT - 3) + '...';
}

/** The reason a journal line gives for a read that failed its check, without the value:
 * "expect_value: innerText() is not money". The prefix is the Python package's. */
export function refusalReason(verb: string, args: readonly unknown[], expect: Expectation): string {
  return 'expect_value: ' + readText(verb, args) + ' ' + expect.refusal;
}

/** The text of the AttentionRequired (and the WARNING) for a read through a heal whose value
 * failed its check. */
export function refusedMessage(key: string, xpath: string, verb: string, args: readonly unknown[], value: unknown,
                               expect: Expectation): string {
  return "'" + key + "' was healed to " + xpath + ', and its ' + readText(verb, args) + ' there is ' + quoted(value)
    + ', which ' + expect.refusal + ' (' + expect.shown + '). The heal probably found another element, so mendwright '
    + "quarantined it and did not return the value. The next lookup of '" + key + "' heals it again, and cannot pick "
    + 'that element. If the value is right, change expectValue.';
}
