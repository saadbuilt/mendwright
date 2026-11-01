// expectValue and open({ expectValues }) (values.ts): the parsers, the named checks and the
// texts against the Python package's mendwright.values, run from this repository on the same
// tables. The Python package's own tables are here too, with their answers, so a rule that is
// taken out fails even without Python. Python's character classes, written out in values.ts,
// are compared with Python's over every character both know.

import { strict as assert } from 'node:assert';
import { describe, test } from 'node:test';

import { isDigitChar } from '../../src/py';
import {
  CHECKS, Expectation, NAMES, VALUE_LIMIT, WORD, casefold, decimalValue, expectation, expectations, isCombining,
  parseMoney, parseNumber, quoted, readText, refusalReason, refusedMessage,
} from '../../src/values';
import { needsPython, runPython } from '../support/python';

const skip = needsPython();

// Characters a test cannot show: spaces that are not one, and a minus sign that looks like -.
const NBSP = String.fromCharCode(0xa0);
const NNBSP = String.fromCharCode(0x202f);   // narrow no-break space
const THIN = String.fromCharCode(0x2009);
const MINUS = String.fromCharCode(0x2212);
const EN_DASH = String.fromCharCode(0x2013);
const EM_DASH = String.fromCharCode(0x2014);
const TAB = String.fromCharCode(9);
const DOTLESS_I = String.fromCharCode(0x131);
const DOTTED_I = String.fromCharCode(0x130);
const ARABIC_THOUSANDS = String.fromCharCode(0x66c);
const ARABIC_DECIMAL = String.fromCharCode(0x66b);
const ARABIC_PERCENT = String.fromCharCode(0x66a);
const ADLAM = (digits: string) => [...digits].map((d) => String.fromCodePoint(0x1e950 + Number(d))).join('');
const BOLD = (digits: string) => [...digits].map((d) => String.fromCodePoint(0x1d7ce + Number(d))).join('');

const names = [...NAMES.keys()];

// --- the Python package's tables (tests/test_values.py), with their answers -------------------

const NUMBERS: [string, string][] = [
  ['1,299.00', '1299.00'], ['1.299,00', '1299.00'], ['1 299,00', '1299.00'],
  ['1' + NBSP + '299,00', '1299.00'], ['1' + NNBSP + '299,00', '1299.00'], ["1'299.00", '1299.00'],
  ['1’299.50', '1299.50'], ['1,29,999.00', '129999.00'], ['1.299.000,5', '1299000.5'],
  ['1,299', '1299'], ['1.299', '1299'], ['0,299', '0.299'], ['1234.567', '1234.567'],
  ['12,5', '12.5'], ['-12', '-12'], ['+3.5', '3.5'], [MINUS + '12', '-12'],
  ['１２３', '123'], ['١٢٣', '123'], ['  42 ', '42'],
];
const NOT_NUMBERS: unknown[] = ['12 34', '1 299.000,00', '1,2,3', 'abc', '12kg', '$5', '', '1.', '4.5 out of 5', null, 12];

const MONEY: [string, string][] = [
  ['$1,299.00', '1299.00'], ['1.299,00 €', '1299.00'], ['1 299,00' + NBSP + '€', '1299.00'],
  ['129 kr', '129'], ['129 kr.', '129'], ['kr 129', '129'], ['129,-', '129'],
  ['129:-', '129'], ['12.' + EN_DASH, '12'], ['129,- kr', '129'], ['CHF 12.50', '12.50'],
  ['USD 1299', '1299'], ['12 USD', '12'], ['$12.00 USD', '12.00'], ['US$ 12', '12'],
  ['R1,299.00', '1299.00'], ['Rs. 1,299', '1299'], ['₹1,29,999.00', '129999.00'],
  ['¥1,299', '1299'], ['£9.99', '9.99'], ['12 zł', '12'], ['1 290 Kč', '1290'],
  ['-$12', '-12'], ['$-12', '-12'], ['-12 €', '-12'], ['€0', '0'],
];
const NOT_MONEY: unknown[] = ['1,299.00', '4.5', 'SKU 12345', '€', 'Free shipping', '$12.99 $15.99', '-$-12', '12 kg',
  '(128 reviews)', null];

const THREE_DECIMALS: [string, string][] = [
  ['KWD 1.250', '1.250'], ['1.250 KWD', '1.250'], ['BHD 12.500', '12.500'],
  ['BHD 0.500', '0.500'], ['OMR 0,750', '0.750'], ['KWD 1,250.000', '1250.000'],
  ['JOD 1 250,500', '1250.500'], ['TND 12', '12'], ['OMR 1.250.000', '1250000'],
  ['EUR 1.250', '1250'],
];
const TWO_WAYS = ['KWD 1,250', 'TND 12,500', '1,250 BHD'];

const NAMED: [string, string[], string[]][] = [
  ['money', ['$1,299.00', '1.299,00 €', '129 kr'], ['4.5', '1,299.00', 'Free']],
  ['number', ['1,299.00', '4.5', '-3'], ['$5', '12 kg', '(128 reviews)']],
  ['integer', ['1,299', '12', '-3'], ['12.00', '1.5', 'twelve']],
  ['percent', ['12%', '12.5 %', '-15%', '%20', '١٢' + ARABIC_PERCENT], ['12', '12 % off']],
  ['date', ['2026-10-15', '2026-10-15T14:30:00Z', '15/10/2026', '10/15/2026', '15.10.2026',
    'Oct 15, 2026', '15 October 2026', '15. Oktober 2026', '15 de octubre de 2026',
    '1er octobre 2026', 'Thu, 15 Oct 2026 14:30', 'Oct 15, 2026 at 2:30 PM',
    '2026年10月15日', 'October 2026', '15 Oct'],
  ['31/02/2026', '13/13/2026', 'In stock', '$12.00', '4.5', '2 days ago', 'Sold out']],
  ['email', ['ada@example.com', 'mailto:ada@example.com?subject=hi'], ['a@b', 'Contact']],
  ['url', ['https://shop.example/p/1', '/p/1', './p', '//cdn.example/x.png'],
    ['javascript:void(0)', '#', 'p/1', 'https:// spaced.example']],
];

const ISO_GOOD = ['2026-10-15T10:00', '2026-10-15T10:00:00', '2026-10-15T10:00:00+02:00',
  '2026-10-15T10:00:00.000+02:00', '2026-10-15T10:00:00Z', '2026-10-15T10:00:00+0200',
  '2026-10-15T10:00:00-05:00', '2026-10-15 10:00:00+02:00', '2026-10-15t10:00',
  '15 octobre 2026 à 10h30', '15 octobre 2026 10h30', 'le 15 octobre 2026 à 10:30'];
const ISO_BAD = ['2026-02-30T10:00', '2026-10-15T', '2026-10-15T10', 'T10:00', '2026-10-15T25:00x'];

// --- the parity table: Python's tables, the guide's examples, and more ------------------------

const GUIDE = ['$1,299.00', '1.299,00 €', '129 kr', 'CHF 12.50', 'USD 1299', '$12.00 USD', 'R1,299.00', '₹1,29,999.00',
  '129,-', '-$12', 'KWD 1.250', '1,250 TND', '0,299', '1234.567', '1 299,500', '1,299.00', '1.299,00', '1 299,00',
  "1'299.00", '4.5', '-3', '1,299', '12', '12.00', '12%', '12.5 %', '%20', '31/02/2026', '2026-10-15',
  '2026-10-15T10:00', '2026-10-15T10:00:00+02:00', '2026-10-15 10:00', '15/10/2026', '10/15/2026', '15.10.2026',
  '2026年10月15日', 'Oct 15, 2026', '15. Oktober 2026', '15 de octubre de 2026'];

const SCRIPTS = ['１２３', '１，２９９．００', '¥１,２９９', '１２３４５６', '١٢٣', '١' + ARABIC_THOUSANDS + '٢٩٩' + ARABIC_DECIMAL + '٥٠',
  ARABIC_PERCENT + '١٢', '١٢' + ARABIC_PERCENT, '۱۲۳', '१२३', '₹१,२९९.००', '๑๒๓', '১২৩', BOLD('123'), ADLAM('12'),
  ADLAM('1') + ',' + ADLAM('299'), '$' + ADLAM('12'), '፩፪', 'Oct ፩', '1 299,00' + NNBSP + '€', '12' + NBSP + '%',
  'CHF' + NBSP + '12.50', '1' + THIN + '299,00 €', '1' + NBSP + '299' + NBSP + '000', MINUS + '$12', '$' + MINUS + '12',
  '²³', '①②', '12' + TAB + 'kr', '12 ' + NBSP + ' kr'];

const NUMBERS_MORE = ['0,0000001', '0.0000000', '0,000000', '0,00000012', '-0', '-0,00', '+0', '000.5', '00012',
  '1,234,567.891', '1.234.567,891', '-1.299,50', "1'299'000", '1’299', '1 299 000', '1 2', '12 345 678', '1,2345',
  '12,34,567', '1,23,45,678', '123,45,678', '1,23,456', '01,299', '0,2999', '1.2.3', "1'299.000", '1 299,500',
  '1 299.5', '- 12', '+ 12', '--12', '12-', '.5', '5.', '1e5', '0x1F', '٫٥', '1' + ARABIC_THOUSANDS + '299',
  '12 345,678.9', "1'234,5", '99999999999999999999999.999', '1,999,999,999,999,999,999.99'];

const MONEY_MORE = ['kr. 129', 'Kr 129', 'KR129', 'zł12', 'Ft 1 290', '1 290 Ft', 'lei 12', '12 lei', 'R$ 12,50', 'C$12',
  'NZ$ 12', 'HK$12', 'ABCD$12', '12 $', '12$', '$ 12', '- $12', '+$12', '$+12', '€-0', 'руб 129', '129 руб.',
  '129 р.', '12 грн', '12 ден', '12 din', '12 дин', 'Dhs 12', 'dhs. 12', '12 TL', '₺12', '₿1', '12 abcd', 'abc 12',
  '12 eur', 'EUR12', 'eur 1.250', 'KWD 1.250.500', 'KWD 1 250,500', 'KWD 12', 'BHD -1.250', '-BHD 1.250',
  '129,' + EN_DASH, '129.' + EM_DASH, '129,- €', '€ 1 299,00', '$1,299.00 USD extra', 'USD', '$', '12,-,-',
  'kwd 1,250.5', 'TND 1.250,500', 'jod 0.750', 'OMR 1,25', 'EUR 1,25', 'Rp 15.000', 'RM 12.90', 'dh 12', 'fr. 12.50',
  'sFr. 12.50', 'SEK 1 299:-', 'kn 12,99', '12 Kc', '12 KČ', 'Kč 12', '12' + NBSP + 'Kč', 'US$-12', '-US$12',
  'r 12', '12 r', 'R 12', '12r'];

const DATES_MORE = ['October 15, 2026', 'Thursday, October 15, 2026', 'Donnerstag, 15. Oktober 2026',
  '15. März 2026', '15. Maerz 2026', 'jeudi 15 octobre 2026', '1er août 2026', '1 Août 2026', 'le 15 octobre 2026 à 10h30',
  'jueves, 15 de octubre de 2026', '1º de mayo de 2026', '15 ottobre 2026', 'giovedì 15 ottobre 2026',
  '15 oktober 2026', 'donderdag 15 oktober 2026', '15 mrt 2026', '15 de outubro de 2026',
  'quinta-feira, 15 de outubro de 2026', '15 de março de 2026', 'Oct 15', '2026 Oct 15', 'Oct 2026', '29 Feb',
  '30 Feb', 'Feb 29 2027', 'Feb 29 2028', 'Mar 15 2026 Mar', 'mar 15 mar 2026', 'martes 15 mar 2026',
  'the 15th of October 2026', 'October 15th, 2026', '15th Oct 26', '29/02/2028', '02/29/2028', '29/02/2027',
  '2026/10/15', '2026.10.15', '15-10-26', '15.10.26.', '15/10-2026', '1.2.3', '15/10/202', '015/10/2026',
  '2026 년 10 월 15 일', '2026년 10월 15일', '２０２６年１０月１５日', '2026年2月30日', '10/15/2026 2:30 pm',
  '15.10.2026 14:30 Uhr', '15.10.2026 um 14:30', 'Oct 15, 2026, 2:30 p.m.', '15 ottobre 2026 alle 14:30',
  '15 oktober 2026 om 14:30', '15 de outubro de 2026 às 14:30', '14:30', 'at 14:30', '2026-13-01', '0000-02-29',
  '1900-02-29', '2000-02-29', 'OCTOBER 15 2026', 'ÖKTOBER 15 2026', '15 Oct 2026 14:30:00', '2026-10-15 at 10:00',
  'Oct 15 15', '15 15 Oct', 'Oct Nov 2026', '2026', 'Oct', '15 Oct 2026 Thu', 'Thu Oct 15 2026',
  'sept. 15, 2026', '15 sept 2026', '1st May', 'May 1st, 2026', '2026-10-15T10:00:00.123456+02:00',
  '2026-10-15T10:00 +02:00', '2026-10-15T10:00Z', '2026-1-5', '2026-10-15T10:00:00+2:00'];

const EMAILS = ['ada@example.com', 'mailto:ada@example.com', 'MAILTO:ada@example.com', 'ma' + DOTLESS_I + 'lto:ada@example.com',
  'MA' + DOTTED_I + 'LTO:ada@example.com', 'mailto:ada@example.com?subject=hi', 'ada@example', 'a@b', 'ada@@example.com',
  'ada lovelace@example.com', ' ada@example.com ', 'ada@exa mple.com', 'ada@example.com.', '"ada"@example.com',
  'ada@例え.jp', 'ada@example.com?x y', 'ada@example..com', 'ada@.example.com', 'ada@example.c', 'a.b+c@x.y.z',
  'ada@example.com?', 'mailto:', 'ada@example.com' + NBSP, NBSP + 'ada@example.com', 'ada@example.com' + NBSP + 'x',
  'ada@ex(ample).com', "o'neil@example.com", 'ada@example.com;bob@example.com'];

const URLS = ['https://shop.example/p/1', 'http://shop.example', 'HTTPS://SHOP.EXAMPLE', '/p/1', './p', '../p',
  '//cdn.example/x.png', '//', 'https://', 'https:///p', 'ftp://x.example', 'javascript:void(0)', '#', 'p/1',
  'https:// spaced.example', 'https://[::1]/', 'https://[::1]:8080/', 'https://[v1.fe]/', 'https://[v1.]/',
  'https://[1.2.3.4]/', 'https://[::1', 'https://::1]/', 'https://x[::1]/', 'https://[::1]x/', 'https://user@[::1]/',
  'https://[fe80::1%25eth0]/', 'https://[fe80::1%]/', 'https://[::ffff:1.2.3.4]/', 'https://[::ffff:1.2.3.04]/',
  'https://[1:2:3:4:5:6:7:8]/', 'https://[1:2:3:4:5:6:7:8:9]/', 'https://[::1::]/', 'https://[]/', 'https://[:1]/',
  'https://[1::]/', 'https://[1:2:3:4:5:6:7::]/', 'https://[1:2:3:4:5:6:7:8::]/', 'https://[12345::]/', 'https://[g::]/',
  'https://[u]@host/', 'https://user:pw@shop.example/', 'https://shop.example:99999/', 'https://ℂ.example/',
  'https://a℀b/', 'https://exa' + TAB + 'mple.com', 'http:shop.example', 'https:', 'mailto:a@b.c', '  /p/1  ',
  'https://shop.example/p?q=1#top', 'https://@/', 'https://:80/', 'https://shop.example' + NBSP, '/', './', '..',
  '.', '//x', 'HTTP://x', 'https://%/', 'https://a%b/', 'https://x.example\\p', 'data:text/plain,hi',
  ' https://shop.example', 'https://shop.example/a b', '1https://x', 'h ttps://x'];

const PERCENTS = ['12%', '12 %', '%20', '12.5%', '-15%', '+15 %', ARABIC_PERCENT + '٢٠', '12%%', '% 12', '12 % off', '%',
  '%-5', '1,299%', '1.299,5 %', '%%20'];

const INTEGERS = ['1,299', '12', '-3', '12.00', '1.5', '1 299', '1.299', '0', '-0', '1,29,999', '1.299,00'];

const TABLE: (string | null)[] = [...new Set<string>([...GUIDE, ...NUMBERS.map(([t]) => t), ...NOT_NUMBERS.filter((t) => typeof t === 'string') as string[],
  ...MONEY.map(([t]) => t), ...NOT_MONEY.filter((t) => typeof t === 'string') as string[], ...THREE_DECIMALS.map(([t]) => t),
  ...TWO_WAYS, ...NAMED.flatMap(([, good, bad]) => [...good, ...bad]), ...ISO_GOOD, ...ISO_BAD, ...SCRIPTS,
  ...NUMBERS_MORE, ...MONEY_MORE, ...DATES_MORE, ...EMAILS, ...URLS, ...PERCENTS, ...INTEGERS]), null];

type Row = [string | null, string | null, boolean[]];

async function tsRow(text: string | null): Promise<Row> {
  const passes: boolean[] = [];
  for (const name of names) passes.push(await (expectation(name) as Expectation).passes(text));
  return [parseNumber(text), parseMoney(text), passes];
}

describe('parsing, as the Python package parses', () => {
  test('numbers as locales write them', () => {
    for (const [text, value] of NUMBERS) assert.equal(parseNumber(text), value, text);
    for (const text of NOT_NUMBERS) assert.equal(parseNumber(text), null, String(text));
  });

  test('money as locales write it', () => {
    for (const [text, value] of MONEY) assert.equal(parseMoney(text), value, text);
    for (const text of NOT_MONEY) assert.equal(parseMoney(text), null, String(text));
  });

  test('a currency with three decimals: its . is the decimal point, and a , that reads two ways is money with no amount',
    async () => {
      for (const [text, value] of THREE_DECIMALS) assert.equal(parseMoney(text), value, text);
      for (const text of TWO_WAYS) {
        assert.equal(parseMoney(text), null, text);
        assert.equal(await (expectation('money') as Expectation).passes(text), true, text);
      }
    });

  test("the amount is written as Python's str(Decimal): trailing zeros kept, and E notation where Python uses it", () => {
    assert.equal(parseNumber('1.299,00'), '1299.00');
    assert.equal(parseNumber('0,0000001'), '1E-7');
    assert.equal(parseNumber('0,00000012'), '1.2E-7');
    assert.equal(parseNumber('0.0000000'), '0E-7');
    assert.equal(parseNumber('0,000000'), '0.000000');
    assert.equal(parseNumber('-0,00'), '-0.00');
    assert.equal(parseNumber('000.5'), '0.5');
    assert.equal(parseNumber('1,999,999,999,999,999,999.99'), '1999999999999999999.99');   // no precision lost
  });

  test('the named checks', async () => {
    for (const [name, good, bad] of NAMED) {
      const check = expectation(name) as Expectation;
      for (const value of good) assert.equal(await check.passes(value), true, name + ': ' + value);
      for (const value of bad) assert.equal(await check.passes(value), false, name + ': ' + value);
      assert.equal(await check.passes(null), false);
    }
  });

  test('ISO 8601 date-times are dates', async () => {
    const check = expectation('date') as Expectation;
    for (const value of ISO_GOOD) assert.equal(await check.passes(value), true, value);
    for (const value of ISO_BAD) assert.equal(await check.passes(value), false, value);
  });

  test("every text of the table gives what the Python package's parse_number, parse_money and checks give",
    { skip }, async () => {
      const py = runPython<Row[]>(`
import json, sys
from mendwright.values import NAMES, expectation, parse_money, parse_number
checks = [expectation(n) for n in NAMES]
out = []
for text in json.load(sys.stdin):
    n, m = parse_number(text), parse_money(text)
    out.append([None if n is None else str(n), None if m is None else str(m), [c.passes(text) for c in checks]])
print(json.dumps(out))`, TABLE);
      assert.equal(py.length, TABLE.length);
      assert.ok(TABLE.length > 400, 'the table has ' + TABLE.length + ' texts');
      for (let i = 0; i < TABLE.length; i++) assert.deepEqual(await tsRow(TABLE[i]), py[i], JSON.stringify(TABLE[i]));
      // The table reaches every answer: each check passes and fails, and both parsers give amounts.
      for (let n = 0; n < names.length; n++) {
        assert.ok(py.some((row) => row[2][n]) && py.some((row) => !row[2][n]), names[n]);
      }
      assert.ok(py.filter((row) => row[0] !== null).length > 50 && py.filter((row) => row[1] !== null).length > 50);
    });

  test("Python's character classes, as values.ts writes them, for every character both know", { skip }, () => {
    const py = runPython<Record<string, unknown>>(`
import json, re, unicodedata
WORD = re.compile(r'[^\\W\\d_]')
keys = ['assigned', 'word', 'decimal', 'digit', 'nfd', 'combining', 'nfkc']
spans = {k: [] for k in keys}
start = {k: None for k in keys}
folds, values = [], []
for cp in range(0x110001):
    c = chr(cp) if cp < 0x110000 else ''
    known = cp < 0x110000 and unicodedata.category(c) not in ('Cn', 'Cs')
    nfd = known and unicodedata.normalize('NFD', c) == c
    nfkc = known and unicodedata.normalize('NFKC', c) == c
    now = {'assigned': known, 'word': known and bool(WORD.match(c)), 'decimal': known and c.isdecimal(),
           'digit': known and c.isdigit(), 'nfd': nfd, 'combining': nfd and unicodedata.combining(c) != 0,
           'nfkc': nfkc}
    for k in keys:
        if now[k] and start[k] is None:
            start[k] = cp
        elif not now[k] and start[k] is not None:
            spans[k].append([start[k], cp - 1])
            start[k] = None
    if nfkc and c.casefold() != c:
        folds.append([cp, c.casefold()])
    if known and c.isdecimal():
        values.append([cp, unicodedata.decimal(c)])
print(json.dumps({'spans': spans, 'folds': folds, 'values': values}))`) as {
      spans: Record<string, [number, number][]>; folds: [number, string][]; values: [number, number][];
    };
    const inside = (key: string) => {
      const set = new Set<number>();
      for (const [a, b] of py.spans[key]) for (let cp = a; cp <= b; cp++) set.add(cp);
      return set;
    };
    const [word, decimal, digit, nfd, combining, nfkc] = ['word', 'decimal', 'digit', 'nfd', 'combining', 'nfkc'].map(inside);
    const folds = new Map(py.folds);
    const values = new Map(py.values);
    const wordRe = new RegExp('^' + WORD + '$', 'u');
    const decimalRe = /^\p{Nd}$/u;
    const wrong: string[] = [];
    let seen = 0;
    for (const [a, b] of py.spans.assigned) {
      for (let cp = a; cp <= b; cp++) {
        seen += 1;
        const ch = String.fromCodePoint(cp);
        const hex = cp.toString(16);
        if (wordRe.test(ch) !== word.has(cp)) wrong.push('word ' + hex);
        if (decimalRe.test(ch) !== decimal.has(cp)) wrong.push('decimal ' + hex);
        if (decimal.has(cp) && decimalValue(ch) !== values.get(cp)) wrong.push('decimal value ' + hex);
        if (isDigitChar(ch) !== digit.has(cp)) wrong.push('isdigit ' + hex);
        if (nfd.has(cp) && isCombining(ch) !== combining.has(cp)) wrong.push('combining ' + hex);
        if (nfkc.has(cp) && casefold(ch) !== (folds.get(cp) ?? ch)) wrong.push('casefold ' + hex);
      }
    }
    assert.ok(seen > 140_000, 'Python knows ' + seen + ' characters');
    assert.ok(combining.size > 800 && folds.size > 1000 && values.size > 600);
    assert.deepEqual(wrong, []);
  });
});

// --- expectValue -----------------------------------------------------------------------------------

describe('what expectValue takes', () => {
  test('the names, in any case and with spaces around them', () => {
    assert.deepEqual(names, ['money', 'number', 'integer', 'percent', 'date', 'email', 'url']);
    assert.equal(expectation(' Money ')?.spec, 'money');
    assert.equal(expectation('money')?.shown, "expectValue: 'money'");
    assert.equal(expectation('integer')?.refusal, 'is not a whole number');
    assert.equal(expectation(null), null);
    assert.equal(expectation(undefined), null);
    assert.deepEqual([...CHECKS.keys()], names);
  });

  test('a RegExp searches the value, as re.search() does, and never reads or moves its lastIndex', async () => {
    const sku = expectation(/^SKU-\d+$/) as Expectation;
    assert.equal(await sku.passes('SKU-123'), true);
    assert.equal(await sku.passes('Item SKU-123'), false);
    assert.equal(await (expectation(/SKU-\d+/) as Expectation).passes('Item SKU-123'), true);
    for (const flags of ['g', 'y', 'gy', 'gi']) {
      const pattern = new RegExp('SKU-\\d+', flags);
      const check = expectation(pattern) as Expectation;
      for (let i = 0; i < 3; i++) assert.equal(await check.passes('Item SKU-123'), true, flags);
      assert.equal(pattern.lastIndex, 0, flags);
      pattern.lastIndex = 5;
      assert.equal(await check.passes('SKU-1'), true, flags);
      assert.equal(pattern.lastIndex, 5, flags);
    }
    assert.equal(sku.shown, 'expectValue: /^SKU-\\d+$/');
    assert.equal(sku.refusal, 'does not match /^SKU-\\d+$/');
    assert.equal(await (expectation(/sku/i) as Expectation).passes('SKU'), true);
  });

  test('a function decides, gets the value as a string, and refuses when it throws or its promise rejects', async () => {
    const seen: unknown[] = [];
    function isSku(text: string): boolean {
      seen.push(text);
      return text.startsWith('SKU-');
    }
    const check = expectation(isSku) as Expectation;
    assert.equal(await check.passes('SKU-1'), true);
    assert.equal(await check.passes('x'), false);
    assert.equal(check.shown, 'expectValue: isSku');
    assert.equal(check.refusal, 'was refused by isSku()');
    const boom = expectation((() => () => { throw new Error('boom'); })()) as Expectation;
    assert.equal(await boom.passes('x'), false);
    assert.equal(boom.refusal, 'was refused by the function');
    assert.equal(boom.shown, 'expectValue: a function');
    assert.equal(await (expectation(async (t: string) => t === 'ok') as Expectation).passes('ok'), true);
    assert.equal(await (expectation(async (t: string) => t === 'ok') as Expectation).passes('no'), false);
    assert.equal(await (expectation(() => Promise.reject(new Error('no'))) as Expectation).passes('x'), false);
    assert.equal(await (expectation(() => 'yes') as Expectation).passes('x'), true);
    assert.equal(await (expectation(() => 0) as Expectation).passes('x'), false);
    assert.equal(expectation(isSku.bind(null))?.shown, 'expectValue: a function');
    // A name JavaScript gave a function from where it was written is not the function's own.
    const options = { expectValue: (t: string) => Boolean(t) };
    assert.equal(options.expectValue.name, 'expectValue');
    assert.equal(expectation(options.expectValue, 'price')?.refusal, 'was refused by the function');
    const checks = { price: (t: string) => Boolean(t) };
    assert.equal(expectations(checks).get('price')?.shown, 'expectValue: a function');
    assert.equal(expectation(isSku, 'price')?.refusal, 'was refused by isSku()');
    // null passes no check, and the function is not called for it.
    seen.length = 0;
    assert.equal(await check.passes(null), false);
    assert.equal(await check.passes(undefined), false);
    assert.deepEqual(seen, []);
  });

  test('anything else throws TypeError at once, naming the key', () => {
    const fails = (spec: unknown, pattern: RegExp, key?: string) => assert.throws(() => expectation(spec, key),
      (e: unknown) => e instanceof TypeError && pattern.test((e as Error).message));
    fails('price', /^mendwright: expectValue for 'price' takes 'money', 'number', 'integer', 'percent', 'date', 'email' or 'url', a RegExp or a function, got 'price'\.$/, 'price');
    fails('cash', /^mendwright: expectValue takes 'money'.* got 'cash'\.$/);
    fails('', /got ''\./);
    fails('toString', /got 'toString'\./);
    fails('constructor', /got 'constructor'\./);
    fails(5, /a RegExp or a function that takes the value and returns true when it is right, got number\.$/);
    fails(true, /got boolean\./);
    fails([], /got array\./);
    fails({}, /got object\./);
    fails(Symbol('x'), /got symbol\./);
  });

  test('open({ expectValues }) takes an object or a Map of keys to checks', () => {
    const map = expectations({ '#price': 'money', '#x': null, '#y': undefined, '#date': 'date' });
    assert.deepEqual([...map.keys()], ['#price', '#date']);
    assert.ok(map.get('#price') instanceof Expectation);
    assert.deepEqual([...expectations(new Map([['#price', 'money']])).keys()], ['#price']);
    assert.equal(expectations(null).size, 0);
    assert.equal(expectations(undefined).size, 0);
    assert.equal(expectations(Object.create(null)).size, 0);
    assert.throws(() => expectations(['money']), /^TypeError: mendwright: expectValues takes an object of keys to checks, such as \{ '#price': 'money' \}, got array\.$/);
    assert.throws(() => expectations('money'), /got string\./);
    assert.throws(() => expectations(new Date()), /got object\./);
    assert.throws(() => expectations({ '': 'money' }), /^TypeError: mendwright: expectValues takes keys as strings, got ''\.$/);
    assert.throws(() => expectations(new Map([[1, 'money']])), /takes keys as strings, got 1\./);
    assert.throws(() => expectations({ '#price': 'cash' }), /expectValue for '#price' takes 'money'/);
  });
});

// --- the texts ---------------------------------------------------------------------------------------

describe('the texts mendwright logs, raises and journals', () => {
  test('a read is named as TypeScript calls it, without its options, and a value is quoted and cut', () => {
    assert.equal(readText('innerText', [undefined]), 'innerText()');
    assert.equal(readText('getAttribute', ['content', { timeout: 500 }]), "getAttribute('content')");
    assert.equal(quoted('4.5'), "'4.5'");
    assert.equal(quoted("it's"), '"it\'s"');
    assert.equal(quoted(null), 'null');
    assert.equal(quoted('x'.repeat(100)), "'" + 'x'.repeat(VALUE_LIMIT - 4) + '...');
    assert.equal(quoted('x'.repeat(100)).length, VALUE_LIMIT);
    // The spaces a browser puts in a price are written as Python's repr() writes them.
    const BS = String.fromCharCode(92);
    assert.equal(quoted('1' + NNBSP + '299,00' + NBSP + '€'), "'1" + BS + 'u202f299,00' + BS + "xa0€'");
    const money = expectation('money') as Expectation;
    assert.equal(refusalReason('innerText', [], money), 'expect_value: innerText() is not money');
    assert.equal(refusalReason('getAttribute', ['content', {}], money), "expect_value: getAttribute('content') is not money");
  });

  test("a value is quoted as the Python package's quoted() quotes it", { skip }, () => {
    const ch = (code: number) => String.fromCodePoint(code);
    const values = ['4.5', "it's", 'say "hi"', 'both \' and "', 'back' + ch(92) + 'slash', 'tab' + TAB + 'and' + ch(10) + 'line',
      '1' + NNBSP + '299,00' + NBSP + '€', 'soft' + ch(0xad) + 'hyphen', 'zero' + ch(0x200b) + 'width', 'next' + ch(0x85) + 'line',
      'line' + ch(0x2028) + 'sep', 'private' + ch(0xe000), 'tag' + ch(0xe0001), 'smile' + ch(0x1f600), ch(0x7f) + ch(0x9f),
      'ideographic' + ch(0x3000) + 'space', 'lone' + String.fromCharCode(0xd800), 'x'.repeat(76) + NBSP + 'tail',
      'x'.repeat(79) + ch(0x202f), '', 'ü' + ch(0x301) + 'é', THIN + MINUS + '12'];
    const py = runPython<string[]>(`
import json, sys
from mendwright.values import quoted
print(json.dumps([quoted(v) for v in json.load(sys.stdin)]))`, values);
    assert.deepEqual(values.map(quoted), py);
  });

  test("the refusal is the Python package's, in TypeScript idiom", { skip }, () => {
    const verbs: Record<string, string> = {
      inner_text: 'innerText', text_content: 'textContent', input_value: 'inputValue', get_attribute: 'getAttribute',
    };
    const cases: [string, string, string, string[], string | null, string][] = [
      ['price', "//span[@class='rating']", 'inner_text', [], '4.5', 'money'],
      ['#price', "//span[@class='rating']", 'text_content', [], '(128 reviews)', 'integer'],
      ['seller', "//a[@class='seller']", 'get_attribute', ['title'], null, 'url'],
      ['eta', '//time', 'get_attribute', ['datetime'], 'Thursday', 'date'],
      ['qty', "//input[@name='qty']", 'input_value', [], '2', 'date'],
      ["it's", '//p', 'inner_text', [], 'x'.repeat(120), 'email'],
      ['p', '//p', 'inner_text', [], "it's \"quoted\"\nand cut", 'percent'],
      ['n', '//p', 'inner_text', [], 'Free', 'number'],
      ['price', "//span[@class='amount']", 'inner_text', [], '1' + NNBSP + '299,00' + NBSP + '€', 'integer'],
    ];
    const py = runPython<{ messages: string[]; reasons: string[] }>(`
import json, sys
from mendwright.values import expectation, refusal_reason, refused_message
cases = json.load(sys.stdin)
print(json.dumps({
  'messages': [refused_message(k, x, v, tuple(a), value, expectation(n)) for k, x, v, a, value, n in cases],
  'reasons': [refusal_reason(v, tuple(a), expectation(n)) for k, x, v, a, value, n in cases]}))`, cases);
    const idiom = (text: string) => {
      let out = text.split('change expect_value=.').join('change expectValue.')
        .replace(/\(expect_value='(\w+)'\)/, "(expectValue: '$1')")
        .replace(/there is None,/, 'there is null,');
      for (const [python, ts] of Object.entries(verbs)) out = out.split(python + '(').join(ts + '(');
      return out;
    };
    cases.forEach(([key, xpath, verb, args, value, name], i) => {
      const expect = expectation(name) as Expectation;
      const tsArgs = [...args, { timeout: 100 }];   // the options object is not shown
      assert.equal(refusedMessage(key, xpath, verbs[verb], tsArgs, value, expect), idiom(py.messages[i]), key);
      assert.equal(refusalReason(verbs[verb], tsArgs, expect), idiom(py.reasons[i]), key);
      assert.ok(!refusalReason(verbs[verb], tsArgs, expect).includes(String(value)) || value === '2', 'the reason holds the value');
    });
    assert.ok(refusedMessage('price', '//x', 'innerText', [], '4.5', expectation('money') as Expectation).endsWith(
      "(expectValue: 'money'). The heal probably found another element, so mendwright quarantined it and did not return "
      + "the value. The next lookup of 'price' heals it again, and cannot pick that element. If the value is right, change "
      + 'expectValue.'));
  });
});
