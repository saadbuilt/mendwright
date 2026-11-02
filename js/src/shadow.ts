// Open shadow roots, as the Python package handles them (mendwright/shadow.py). Since 0.10.0
// it saves where a key's element was found as the key's shadowHosts: the selectors of the
// shadow hosts around it, outermost first, such as ['my-app[id="root"]'], or null for a host
// nothing tells apart. A heal inside a shadow root is saved as the Playwright chain of the
// hosts and a CSS selector, 'my-app[id="root"] >> [data-testid="save"]', with the hosts as
// the candidate's shadowHosts.
//
// A candidate saved inside one runs strictly, as the Python package runs it: each host must
// match exactly one element in the document or shadow root before it and have an open shadow
// root, and the CSS counts only what matches in that shadow root (PlaywrightDriver.placed),
// never in the host's light DOM, a shadow root inside it, or another host with the same tag.
// Playwright's own CSS pierces every open shadow root, so a chain run as Playwright runs it
// could find an element somewhere else.
//
// A key last found inside a shadow root heals only there, through a ShadowDriver (scope.ts):
// the fingerprint tier and the model get that shadow root's HTML and nothing else, and the
// XPath they find is checked in that HTML and saved as a CSS selector for the same element
// (css.ts), once that CSS passed the live check inside the shadow root (gate.apply). A key
// whose own XPath matches nothing on the page but one element in one open shadow root is
// followed there, through a CSS form of that element (Healer.followIntoShadow): this module
// holds the rules and texts of that follow, which the tests compare with the Python package's.
//
// Closed shadow roots are out of reach: the browser gives a script no handle on one, so their
// elements are never found, recorded or healed.

import { InvalidSelector } from './errors';
import type { PlanRules } from './frames';
import { pyRepr, pyStrip } from './py';
import { CSS, XPATH, checkSyntax, classify, stripPrefix } from './selectors';
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

/** The shadow hosts that a heal the Python package saved inside a shadow root names in its
 * text, for one that has lost its shadowHosts: the Python package 0.9.0 drops every
 * shadowHosts when it writes the store, a candidate's as well as the key's (Python's
 * shadow.chain_hosts). [] unless the candidate is a heal with no frameXPath and no
 * shadowHosts field, whose text is the chain of hosts of the form the Python package builds
 * and a CSS selector. No other heal has ' >> ' in it. The text is read as textHosts reads
 * it when the store is read. */
export function chainHosts(candidate: Candidate | null | undefined): string[] {
  if (!candidate || typeof candidate !== 'object' || candidate.source !== 'heal') return [];
  if (candidate.frameXPath || HOSTS in candidate) return [];
  return textHosts(candidate.xpath);
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

// A shadow host's selector as the Python package builds it (shadow.host_selector): its tag
// and one of these attributes with a value in CSS string quoting, or a custom element's tag
// alone.
const HOST_ATTRS = ['data-testid', 'data-test-id', 'data-test', 'data-qa', 'data-cy', 'data-e2e',
  'data-automation-id', 'id', 'name'];
const HOST_FORM = new RegExp('^(?:[a-z][a-z0-9_-]*\\[(?:' + HOST_ATTRS.map((a) => a.replace(/-/g, '\\-')).join('|')
  + ')="(?:[^"\\\\\\r\\n\\f]|\\\\["\\\\]|\\\\[0-9a-f]{1,6} )*"\\]|[a-z][a-z0-9_]*-[a-z0-9_-]*)$');

/** The shadow hosts of a heal saved inside a shadow root, read back from its text
 * (Python's shadow.text_hosts): the parts of the chain before its last, when each is a host
 * selector of the form the Python package builds and the last is CSS; [] for any other
 * text. The Python package 0.9.0 drops shadowHosts when it writes the store, and saves no
 * heal of this form itself, so the store reads them back from here. */
export function textHosts(text: unknown): string[] {
  if (typeof text !== 'string' || !text.includes(' >> ')) return [];
  const parts = splitText(text);
  if (parts.length < 2 || !parts.slice(0, -1).every((p) => HOST_FORM.test(p))) return [];
  try {
    if (checkSyntax(parts[parts.length - 1]) !== CSS) return [];
  } catch {
    return [];
  }
  return parts.slice(0, -1);
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

// --- inside one shadow root ---------------------------------------------------------------

/** A shadow root's HTML (its innerHTML) as a document, as the gate, the fingerprint tier and
 * the model read it (Python's shadow.as_document). The host is left out: a selector that
 * names it would not run inside its shadow root. */
export function asDocument(inner: string): string {
  return '<html><head></head><body>' + inner + '</body></html>';
}

/** The CSS of a shadow host's selector, without a css= prefix (Python's shadow.host_css).
 * InvalidSelector for one that is empty or not CSS: a host is found with querySelectorAll(). */
export function hostCss(selector: unknown): string {
  let kind: string;
  try {
    kind = checkSyntax(selector);
  } catch (error) {
    if (error instanceof InvalidSelector) throw error;
    throw InvalidSelector.build(null, 'a shadow host is not saved as a selector.',
      'Check the shadowHosts of the key in overrides.json.');
  }
  if (kind !== CSS) {
    throw InvalidSelector.build(selector as string, pyRepr(selector) + ' is not a CSS selector, and a shadow host is '
      + 'looked up with CSS.', 'Check the shadowHosts of the key in overrides.json.');
  }
  return stripPrefix(selector as string);
}

/** The CSS of a selector that runs inside a shadow root, without a css= prefix (Python's
 * shadow.inner_css). InvalidSelector for any other kind: XPath does not reach into a shadow
 * root, and neither does a Playwright engine selector run that way. */
export function innerCss(selector: unknown): string {
  const kind = checkSyntax(selector);
  if (kind !== CSS) {
    throw InvalidSelector.build(selector as string, pyRepr(selector) + ' is not CSS, and only a CSS selector runs '
      + 'inside a shadow root.', 'Use a CSS selector, such as \'#save\' or \'button[name="save"]\'.');
  }
  return stripPrefix(selector as string);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** What the page library's cssForms() gives for an XPath in a document: how many elements it
 * matches, and the CSS forms of the one it matches; null when it cannot run there. */
export type FormsRead = { count: number; forms: string[] } | null;

/** A cssForms() result read from the page, null for anything malformed. */
export function formsRead(value: unknown): FormsRead {
  if (!isRecord(value) || typeof value.count !== 'number' || !Array.isArray(value.forms)) return null;
  return { count: value.count, forms: value.forms.filter((f): f is string => typeof f === 'string') };
}

// --- following a key's own selector into a shadow root ----------------------------------------

/** How many shadow roots, and how many characters of their HTML, the driver reads at most for
 * an XPath (see PlaywrightDriver.shadowMatches). */
export const SHADOW_ROOTS_LIMIT = 200;
export const SHADOW_HTML_BUDGET = 2_000_000;

/** An open shadow root of the page, as PlaywrightDriver.shadowMatches() reads it: the
 * selectors of its hosts, outermost first (null for a host nothing tells apart), how many
 * elements a CSS selector matches there (null without one), and its HTML for an XPath (null
 * without one). */
export interface ShadowMatch {
  hosts: Hosts;
  count: number | null;
  html: string | null;
}

/** The page script's argument for a lookup of selector in the open shadow roots of the page,
 * or null for a selector that is neither CSS nor XPath: an engine selector is not looked for
 * in shadow roots (Python's drivers.base.shadow_arg). Throws InvalidSelector for a malformed
 * one. */
export function shadowArg(selector: string): { css: string | null; limit: number; budget: number } | null {
  const kind = checkSyntax(selector);
  if (kind !== CSS && kind !== XPATH) return null;
  return { css: kind === CSS ? stripPrefix(selector) : null, limit: SHADOW_ROOTS_LIMIT, budget: SHADOW_HTML_BUDGET };
}

/** The page script's result as [roots, cut] (Python's shadow.roots_of): cut says the read
 * left some roots out. [[], true] for anything malformed, which mendwright then does not act
 * on. */
export function rootsOf(raw: unknown): [ShadowMatch[], boolean] {
  if (!isRecord(raw) || !Array.isArray(raw.roots)) return [[], true];
  const roots: ShadowMatch[] = [];
  for (const item of raw.roots) {
    if (!isRecord(item) || !Array.isArray(item.hosts)) return [[], true];
    const { count, html } = item;
    roots.push({
      hosts: item.hosts.map((h) => (typeof h === 'string' && pyStrip(h) ? h : null)),
      count: typeof count === 'number' && Number.isInteger(count) ? count : null,
      html: typeof html === 'string' ? html : null,
    });
  }
  return [roots, raw.cut === true];
}

/** A shadow root one of a key's own selectors matches in (Python's shadow_found): its hosts,
 * how many elements the selector matches there, and the CSS selectors to look it up with
 * there: the selector itself for CSS, the CSS forms of its one element for an XPath. */
export interface ShadowFound {
  hosts: Hosts;
  count: number;
  forms: string[];
}

/** The open shadow roots of roots that selector, one of a key's own selectors, matches in
 * (Python's facade.shadow_found). An XPath, which runs inside no shadow root, is evaluated in
 * each root's HTML by read (the page library's cssFormsEach(), one round trip for all of
 * them); a read that fails finds nothing. */
export async function shadowFound(selector: string, roots: ShadowMatch[],
                                  read: (docs: string[], xpath: string) => Promise<unknown>): Promise<ShadowFound[]> {
  const kind = classify(selector);
  const body = stripPrefix(selector);
  if (kind === CSS) {
    return roots.filter((root) => root.count).map((root) => ({ hosts: root.hosts, count: root.count as number,
      forms: [body] }));
  }
  if (kind !== XPATH) return [];
  const withHtml = roots.filter((root) => root.html !== null);
  if (!withHtml.length) return [];
  let reads: unknown;
  try {
    reads = await read(withHtml.map((root) => asDocument(root.html as string)), body);
  } catch {
    return [];
  }
  if (!Array.isArray(reads)) return [];
  const out: ShadowFound[] = [];
  withHtml.forEach((root, i) => {
    const hit = formsRead(reads[i]);
    if (hit && hit.count) out.push({ hosts: root.hosts, count: hit.count, forms: hit.count === 1 ? hit.forms : [] });
  });
  return out;
}

/** [the shadow root of found that key can be followed into with selector, its own selector,
 * null], else [null, why not] (Python's Healer._shadow_plan): the refusals of a follow into an
 * iframe (see frames.framePlan), and several shadow roots or elements, a host on the way that
 * nothing tells apart, an XPath whose element no CSS selector tells apart, a lookup there that
 * was quarantined or superseded, and, for an XPath, a page with more shadow roots than
 * mendwright reads (cut). */
export function shadowPlan(key: string, selector: string, found: ShadowFound[], cut: boolean,
                           rules: PlanRules): [ShadowFound, null] | [null, string] {
  if (rules.disabled) return [null, 'healing is turned off by MENDWRIGHT_DISABLE=1'];
  if (rules.dryRun) return [null, 'dry-run mode is on'];
  if (rules.destructive) return [null, "'" + key + "' is marked as a destructive control"];
  if (found.length > 1) return [null, 'it matches in more than one shadow root'];
  if (cut && classify(selector) === XPATH) {
    return [null, 'the page has more shadow roots than mendwright reads, so it cannot tell that the selector matches '
      + 'in that one only'];
  }
  const root = found[0];
  if (root.count !== 1) return [null, 'it matches ' + root.count + ' elements there'];
  if (root.hosts.some((h) => h === null)) {
    return [null, 'nothing tells one of the shadow hosts on the way apart from the elements around it (no test id, id '
      + 'or name of its own there, and not the only element of its custom tag)'];
  }
  if (!root.forms.length) {
    return [null, 'no CSS selector tells its element apart there, and only CSS runs inside a shadow root'];
  }
  const statuses = root.forms.map((form) => rules.status(shadowText(root.hosts as string[], form)));
  if (statuses.every((s) => s === 'quarantined')) {
    return [null, 'it was looked up in that shadow root before and an action on it failed there, so that lookup is '
      + 'quarantined'];
  }
  if (statuses.every((s) => s === 'quarantined' || s === 'superseded')) {
    return [null, 'it was looked up in that shadow root before and that lookup was superseded'];
  }
  return [root, null];
}

/** Why a key whose selector matches only inside an open shadow root was not followed there
 * (Python's facade._shadow_message): found are where selector matches, the first one named. */
export function shadowMessage(key: string, selector: string, found: ShadowFound[], why: string): string {
  let where = placeText(found[0].hosts);
  if (found.length > 1) where += ' (and ' + (found.length - 1) + ' more)';
  return "'" + key + "' is inside a shadow root: " + selector + ' matches nothing on the page, but matches inside '
    + where + '. mendwright did not look for it there, because ' + why + ', and did not ask a model to heal it. Fix '
    + 'the selector in your code.';
}
