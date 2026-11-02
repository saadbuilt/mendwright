// CSS selectors as the Python package writes them (mendwright/css.py): the quoting of an
// attribute value, in the selector of an iframe (see frames.ts) and of a shadow host, and the
// XPath twin of a CSS selector of the forms mendwright builds for a healed element.
//
// A heal inside an open shadow root (see shadow.ts) is checked as an XPath in the HTML of that
// shadow root and saved as a CSS selector for the same element, since no XPath runs inside a
// shadow root. Those CSS selectors are built from forms that each have an exact XPath twin: a
// tag name, #id, .class, [attr="value"] and the descendant combinator. A form counts only if
// its XPath twin matches the element and nothing else in that HTML. CSS can't match text, so
// an element only its text tells apart has no form, and its heal is refused. Building them
// needs a parsed document and XPath, which only the page has, so the Python package's
// css_selectors() is the page library's cssSelectors() (browser/lib.ts), next to xpathFor();
// cssXpath() reads a saved form back as XPath, for a quarantine in a shadow root.

import { pyStrip } from './py';

/** value as a double-quoted CSS string (Python's css.css_string): a double quote and a
 * backslash escaped with a backslash, a line feed, a carriage return and a form feed written
 * as a backslash, their code in hex and a space, and everything else as it is. */
export function cssString(value: string): string {
  let out = '"';
  for (const ch of value) {
    if (ch === '"' || ch === '\\') out += '\\' + ch;
    else if (ch === '\n' || ch === '\r' || ch === '\f') out += '\\' + (ch.codePointAt(0) as number).toString(16) + ' ';
    else out += ch;
  }
  return out + '"';
}

/** value as an XPath 1.0 string literal (Python's candidates.xpath_literal). */
export function xpathLiteral(value: string): string {
  if (!value.includes("'")) return "'" + value + "'";
  if (!value.includes('"')) return '"' + value + '"';
  return 'concat(' + value.split("'").map((part) => "'" + part + "'").join(', "\'", ') + ')';
}

/** The XPath predicate of the class name token, as mendwright writes it. */
export function classPredicate(token: string): string {
  return "contains(concat(' ', normalize-space(@class), ' '), " + xpathLiteral(' ' + token + ' ') + ')';
}

// A tag name, and one part of a compound: #id, .class or [attr="value"] in cssString's quoting.
const TAG_AT = /-?[A-Za-z_][A-Za-z0-9_-]*/y;
const PART_AT = /#(-?[A-Za-z_][A-Za-z0-9_-]*)|\.(-?[A-Za-z_][A-Za-z0-9_-]*)|\[([A-Za-z_][A-Za-z0-9_:.-]*)="((?:[^"\\\r\n\f]|\\["\\]|\\[0-9a-f]{1,6} )*)"\]/y;
const ESCAPE = /\\(["\\])|\\([0-9a-f]{1,6}) /g;

/** The value of a cssString() body: each escape read back. Throws RangeError for a code that
 * is no character, as Python's chr() raises. */
function unquote(body: string): string {
  return body.replace(ESCAPE, (_m, plain: string | undefined, hex: string | undefined) => plain
    ?? String.fromCodePoint(parseInt(hex as string, 16)));
}

/** The XPath twin of a CSS selector of the forms mendwright builds (Python's css.css_xpath):
 * compounds of a tag name, #id, .class and [attr="value"] (cssString's quoting), joined by
 * single spaces (the descendant combinator). null for anything else, which mendwright then
 * cannot match in captured HTML. Throws for an escape of a code that is no character. */
export function cssXpath(css: unknown): string | null {
  if (typeof css !== 'string') return null;
  const text = pyStrip(css);
  if (!text) return null;
  const steps: string[] = [];
  let i = 0;
  for (;;) {
    TAG_AT.lastIndex = i;
    const tag = TAG_AT.exec(text);
    let name = '*';
    if (tag) {
      name = tag[0].toLowerCase();
      i = TAG_AT.lastIndex;
    }
    const preds: string[] = [];
    for (;;) {
      PART_AT.lastIndex = i;
      const part = PART_AT.exec(text);
      if (!part) break;
      i = PART_AT.lastIndex;
      if (part[1] !== undefined) preds.push('@id=' + xpathLiteral(part[1]));
      else if (part[2] !== undefined) preds.push(classPredicate(part[2]));
      else preds.push('@' + part[3] + '=' + xpathLiteral(unquote(part[4])));
    }
    if (!tag && !preds.length) return null;
    steps.push(name + preds.map((p) => '[' + p + ']').join(''));
    if (i === text.length) return '//' + steps.join('//');
    if (text[i] !== ' ' || i + 1 === text.length) return null;
    i += 1;
  }
}
