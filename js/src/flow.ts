// Flow changes (Python's mendwright/flow.py): the page path the store keeps per key and the
// page guard compares, the address it keeps for `mendwright check`, how a failed action is
// read, and the texts mendwright logs or raises for interruptions, skipped steps and the
// page guard. Both packages write and compare the same values.

import { hostname, isDigits, port, pyRepr, urlsplit } from './py';

/** How many interruptions one step handles before it stops. */
export const MAX_INTERRUPTIONS_PER_STEP = 3;
/** After an interruption's action, how long mendwright waits for it to go away. */
export const SETTLE_MS = 2000;

const UUID = /^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$/;
const HEX = /^[0-9a-f]{16,}$/;
const TOKEN = /^[a-z0-9_-]{16,}$/;
const NO_PATH = ['about:', 'data:', 'blob:', 'javascript:'];

function isId(part: string): boolean {
  if (isDigits(part) || UUID.test(part) || HEX.test(part)) return true;
  return TOKEN.test(part) && [...part].filter((c) => /[0-9]/.test(c)).length >= 2 && /[a-z]/.test(part);
}

/** The page part of url that mendwright compares between runs: the path in lower case,
 * without scheme, host, query, fragment or empty segments, with record ids as '*'. null for
 * about:blank, data: URLs and the site root. */
export function pagePath(url: unknown): string | null {
  if (typeof url !== 'string' || !url.trim()) return null;
  const text = url.trim();
  if (NO_PATH.some((p) => text.toLowerCase().startsWith(p))) return null;
  let path: string;
  try {
    path = urlsplit(text).path;
  } catch {
    return null;
  }
  const segments = path.toLowerCase().split('/').filter(Boolean).map((part) => (isId(part) ? '*' : part));
  if (!segments.length) return null;
  return '/' + segments.join('/');
}

const URL_SEGMENT = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,63}$/;
const HOST = /^[a-z0-9_-]+(?:\.[a-z0-9_-]+)*\.?$/;
const IPV6 = /^[0-9a-f:.]+$/;
const DEFAULT_PORTS: Record<string, number> = { http: 80, https: 443 };
export const MAX_URL = 2048;

function masked(part: string): boolean {
  if (!URL_SEGMENT.test(part) || isId(part.toLowerCase())) return true;
  if (part.includes('.') && (part.startsWith('eyJ')
      || part.split('.').some((piece) => [...piece].length >= 16 && isId(piece.toLowerCase())))) {
    return true;
  }
  return false;
}

/** The address the store keeps per key ('lastUrl'), or null: scheme and host in lower case,
 * a port unless it is the default, no user info, query or fragment, and path segments that
 * could hold a token or a person's data as '*'. */
export function pageUrl(url: unknown): string | null {
  if (typeof url !== 'string') return null;
  let scheme: string;
  let host: string | null;
  let portNumber: number | null;
  let path: string;
  try {
    const parts = urlsplit(url);
    scheme = parts.scheme.toLowerCase();
    host = hostname(parts.netloc);
    portNumber = port(parts.netloc);
    path = parts.path;
  } catch {
    return null;
  }
  if (!(scheme in DEFAULT_PORTS) || !host) return null;
  if (host.includes(':')) {
    if (!IPV6.test(host)) return null;
    host = '[' + host + ']';
  } else if (!HOST.test(host)) {
    return null;
  }
  if (portNumber !== null && portNumber !== DEFAULT_PORTS[scheme]) host += ':' + portNumber;
  let joined = path.split('/').map((part) => (part && masked(part) ? '*' : part)).join('/');
  if (!joined.startsWith('/')) joined = '/' + joined;
  const address = scheme + '://' + host + joined;
  return [...address].length <= MAX_URL ? address : null;
}

const ACTION_RAN = /\baction done\b|waiting for scheduled navigations/;

/** True when an action failed because another element covered its element, or the element
 * could not be reached, and the action did not reach the page: Playwright's call log says
 * "intercepts pointer events" or "element is not visible", and not "action done". An
 * action that may have taken effect is never run again. */
export function isInterceptionError(error: unknown): boolean {
  const text = String((error as Error)?.message ?? error).toLowerCase();
  if (ACTION_RAN.test(text)) return false;
  return text.includes('intercepts pointer events') || text.includes('element is not visible');
}

// --- messages ------------------------------------------------------------------------------

export function flowChangeMessage(key: string, last: string, here: string, entry = 'mendwright.open()'): string {
  return "'" + key + "' was last found on " + last + ', and this page is ' + here + '. That looks '
    + 'like a change in the flow, not a renamed element, so mendwright did not heal it. If the step '
    + 'moved, update your script. If the URL itself changed in a redesign, pass healAcrossPages: true '
    + 'to ' + entry + '.';
}

export function handledMessage(key: string, before: string): string {
  return 'handled interruption ' + pyRepr(key) + ' before ' + pyRepr(before);
}

export function unhandledMessage(key: string, before: string, error: unknown): string {
  const first = (String((error as Error)?.message ?? error).trim().split('\n')[0] || 'Error').slice(0, 200);
  return 'could not handle interruption ' + pyRepr(key) + ' before ' + pyRepr(before) + ' ('
    + ((error as Error)?.name ?? 'Error') + ': ' + first + '). Check the selector and action passed '
    + 'to heal.interrupt(' + pyRepr(key) + ', ...).';
}

export function loopMessage(before: string, key: string, count: number): string {
  return 'mendwright: ' + count + ' interruptions were handled for ' + pyRepr(before) + ' and '
    + pyRepr(key) + ' is still on the page, so mendwright stopped instead of handling it again. '
    + 'Check that the action registered with heal.interrupt(' + pyRepr(key) + ', ...) really closes '
    + "it, for example with action: 'press', args: ['Escape'], or a selector for its close button.";
}

export function severalMessage(name: string, selector: string, count: number, looked: number): string {
  let text = 'interruption ' + pyRepr(name) + ' was skipped: its selector ' + pyRepr(selector)
    + ' matches ' + count + ' elements, and ';
  if (looked === 0) {
    text += 'mendwright acts on a CSS or Playwright selector only when it matches one (an XPath is '
      + 'narrowed to its first visible match).';
  } else {
    text += 'mendwright looks at the first ' + looked + ' of them only, and none of those is visible.';
  }
  return text + ' Use a selector that matches only the element to act on: its id or test id, its '
    + 'role and name, as in role=button[name="Accept all"], its whole text, as in text="Accept all", '
    + "or an XPath of its tag and text, as in //button[normalize-space()='Accept all'].";
}

export function nothingMatchedMessage(before: string, keys: string[], preset: boolean): string {
  const looked: string[] = [];
  if (keys.length) looked.push('heal.interrupt(): ' + keys.map(pyRepr).join(', '));
  if (preset) looked.push('the cookies preset');
  const text = 'the action on ' + pyRepr(before) + ' failed because something covered its element or '
    + 'it could not be reached, and none of the interruptions mendwright looks for ('
    + looked.join('; ') + ') matched a visible element, so it did not try the action again.';
  if (keys.length) {
    return text + ' If the element in the way is one of those registered with heal.interrupt(), its '
      + 'selector no longer matches: update it. Otherwise register the button that closes the element '
      + 'in the way with heal.interrupt()'
      + (preset ? ', for example for a cookie banner the cookies preset does not know.' : '.');
  }
  return text + ' If the element in the way is a cookie banner the preset does not know, or anything '
    + 'else, register the button that closes it with heal.interrupt().';
}

export function skipMessage(key: string, reason: string, before?: string | null): string {
  if (reason === 'interruption handled') {
    const where = before ? ' before ' + pyRepr(before) : '';
    return 'skipped ' + pyRepr(key) + ': it was already handled as an interruption' + where
      + ', and it is not on the page now.';
  }
  if (reason === 'interruption absent') {
    return 'skipped ' + pyRepr(key) + ': it is registered with heal.interrupt() and is not on the '
      + 'page, so there was nothing to do.';
  }
  return 'skipped ' + pyRepr(key) + ': it is optional (optional: true) and is not on the page.';
}

export function absentMessage(key: string): string {
  return "mendwright: '" + key + "' was not on the page, so this step was skipped and there is no "
    + 'element to read. Check proxy.present before reading from an optional step.';
}
