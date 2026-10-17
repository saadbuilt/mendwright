// Open shadow roots, as the Python package records them (mendwright/shadow.py). Since 0.10.0
// it saves where a key's element was found as the key's shadowHosts: the selectors of the
// shadow hosts around it, outermost first, such as ['my-app[id="root"]'], or null for a host
// nothing tells apart. A heal it made inside a shadow root is saved as the Playwright chain
// of the hosts and a CSS selector, 'my-app[id="root"] >> [data-testid="save"]', with the
// hosts as the candidate's shadowHosts.
//
// This package does not heal inside a shadow root, and never heals a key last found inside
// one on the page around it (see Healer.heal). It runs a candidate saved inside one strictly,
// as the Python package does: each host must match exactly one element in the document or
// shadow root before it and have an open shadow root, and the CSS counts only what matches
// in that shadow root (PlaywrightDriver.placed), never in the host's light DOM, a shadow
// root inside it, or another host with the same tag. Playwright's own CSS pierces every open
// shadow root, so a chain run as Playwright runs it could find an element somewhere else.

import { InvalidSelector } from './errors';
import { pyRepr, pyStrip } from './py';
import { CSS, checkSyntax, stripPrefix } from './selectors';
import type { Candidate } from './store';

/** The candidate field, and the key's field, that hold shadow hosts. */
export const HOSTS = 'shadowHosts';

export type Hosts = (string | null)[];

/** A saved shadowHosts value: [] for none (missing, null, '' or []), else one entry per
 * host, its CSS selector or null for a host nothing tells apart. Anything malformed counts
 * as a host nothing tells apart, so it never reads as the page. */
export function savedHosts(value: unknown): Hosts {
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)) return [];
  if (!Array.isArray(value)) return [null];
  return value.map((item) => (typeof item === 'string' && pyStrip(item) ? item : null));
}

/** The shadow hosts a candidate runs inside. */
export function hostsOf(candidate: Candidate | null | undefined): Hosts {
  return candidate && typeof candidate === 'object' ? savedHosts(candidate[HOSTS]) : [];
}

/** The shadow root the hosts lead to, in words. */
export function placeText(hosts: Hosts): string {
  const names = hosts.map((h) => h || 'an element nothing tells apart');
  if (!names.length) return 'the page';
  let text = 'the shadow root of ' + names[names.length - 1];
  if (names.length > 1) text += ' inside ' + names.slice(0, -1).reverse().join(' inside ');
  return text;
}

/** The parts of a Playwright selector chain, split at each ' >> ' outside quotes and
 * brackets, each one trimmed (Python's shadow.split_text). */
export function splitText(text: string): string[] {
  const parts: string[] = [];
  let quote: string | null = null;
  let depth = 0;
  let start = 0;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (quote) {
      if (ch === '\\') {
        i += 2;
        continue;
      }
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '[' || ch === '(') {
      depth += 1;
    } else if (ch === ']' || ch === ')') {
      depth = Math.max(0, depth - 1);
    } else if (depth === 0 && text.startsWith(' >> ', i)) {
      parts.push(text.slice(start, i).trim());
      i += ' >> '.length;
      start = i;
      continue;
    }
    i += 1;
  }
  parts.push(text.slice(start).trim());
  return parts;
}

/** The text a selector inside the shadow root that hosts lead to is saved with. */
export function shadowText(hosts: string[], css: string): string {
  return [...hosts.map((h) => stripPrefix(h)), stripPrefix(css)].join(' >> ');
}

function cssOnly(selector: string, what: string): string {
  let kind: string;
  try {
    kind = checkSyntax(selector);
  } catch (error) {
    if (error instanceof InvalidSelector) throw error;
    throw InvalidSelector.build(null, what + ' is not saved as a selector.', 'Check the key in overrides.json.');
  }
  if (kind !== CSS) {
    throw InvalidSelector.build(selector, pyRepr(selector) + ' is not a CSS selector, and ' + what
      + ' is looked up with CSS.', 'Check the key in overrides.json.');
  }
  return stripPrefix(selector);
}

/** Where a candidate runs. */
export interface Target {
  /** The selector to run: the candidate's text, or for one inside a shadow root its CSS. */
  selector: string;
  frame: string | null;
  /** The CSS of each shadow host, outermost first; [] for the page or an iframe. */
  hosts: string[];
}

/** Where a candidate runs (Python's shadow.run_target). Raises InvalidSelector for a
 * candidate inside a shadow root whose text is not the chain of its hosts and a CSS
 * selector, whose hosts are not all CSS selectors (a host nothing tells apart included), or
 * that is also inside an iframe, which this package does not reach. */
export function runTarget(candidate: Candidate): Target {
  const hosts = hostsOf(candidate);
  const frame = typeof candidate.frameXPath === 'string' && candidate.frameXPath ? candidate.frameXPath : null;
  if (!hosts.length) return { selector: candidate.xpath, frame, hosts: [] };
  const text = candidate.xpath;
  if (frame) {
    throw InvalidSelector.build(text, pyRepr(text) + ' was saved inside a shadow root in the iframe ' + frame
      + ', and mendwright for TypeScript does not reach into a shadow root inside an iframe.',
      'Let the Python package run it, or fix the selector in your code.');
  }
  if (hosts.some((h) => h === null)) {
    throw InvalidSelector.build(text, pyRepr(text) + ' was saved inside a shadow root whose host nothing tells '
      + 'apart, so mendwright cannot find that shadow root.', 'Give the host an id or a test id, or fix the selector '
      + 'in your code.');
  }
  const css = (hosts as string[]).map((h) => cssOnly(h, 'a shadow host'));
  const parts = typeof text === 'string' ? splitText(text) : [];
  if (parts.length !== css.length + 1 || parts.slice(0, -1).some((p, i) => p !== css[i])) {
    throw InvalidSelector.build(typeof text === 'string' ? text : null, pyRepr(text) + ' was saved inside a shadow '
      + 'root, and it is not the chain of its shadow hosts and a CSS selector (' + shadowText(css, 'CSS') + ').',
      'Check the key in overrides.json.');
  }
  return { selector: cssOnly(parts[parts.length - 1], 'a selector inside a shadow root'), frame: null, hosts: css };
}
