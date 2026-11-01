// CSS selectors as the Python package writes them (mendwright/css.py). For now the quoting
// of an attribute value in the selector of an iframe (see frames.ts); the CSS forms of a
// healed element, for a heal inside an open shadow root, come with them.

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
