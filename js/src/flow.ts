// Flow changes (Python's mendwright/flow.py): the page path the store keeps per key and the
// page guard compares, the address it keeps for `mendwright check`, how a failed action is
// read, and the texts mendwright logs or raises for interruptions, skipped steps and the
// page guard. Both packages write and compare the same values.

import { hostname, isDigits, port, pyLen, pyRepr, pySlice, pyStrip, urlsplit } from './py';

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

/** The WARNING for an interruption mendwright handled. fn names the run function of
 * heal.interrupt() that handled it, if any. */
export function handledMessage(key: string, before: string, fn?: string | null): string {
  return 'handled interruption ' + pyRepr(key) + ' before ' + pyRepr(before) + (fn ? ' with ' + fn + '()' : '');
}

// What Python's str.splitlines() splits at. Only the first line is used, so \r\n needs no
// case of its own.
const LINE_BREAK = /[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]/;

/** The type name of what was thrown, as Python's type(error).__name__ gives it: the error's
 * name (TypeError, AttentionRequired), else the name of its class, else 'Error'. */
export function errorType(error: unknown): string {
  if (error !== null && error !== undefined) {
    const name = (error as { name?: unknown }).name;
    if (typeof name === 'string' && name) return name;
    const kind = (Object(error) as { constructor?: { name?: unknown } }).constructor?.name;
    if (typeof kind === 'string' && kind) return kind;
  }
  return 'Error';
}

/** The first line of what error says, at most 200 characters: Python's
 * (str(error).strip().splitlines() or [type(error).__name__])[0][:200]. */
function firstLine(error: unknown): string {
  const text = pyStrip(String((error as Error)?.message ?? error));
  return pySlice(text ? text.split(LINE_BREAK)[0] : errorType(error), 200);
}

/** A value as a message about a run function shows it, in TypeScript: undefined, null, 3,
 * true, a string as Python's repr() quotes it (cut after 60 characters), [1, 2], { a: 1 },
 * <function>, and an instance of a class by its class, as <Locator>. What is nested deeper
 * than three arrays or objects is shown as ... */
export function valueText(value: unknown, depth = 0): string {
  if (value === undefined) return 'undefined';
  if (value === null) return 'null';
  if (typeof value === 'string') {
    const text = pyRepr(value);
    return pyLen(text) <= 60 ? text : pySlice(text, 57) + '...';
  }
  if (typeof value === 'function') return '<function>';
  if (typeof value !== 'object') return String(value);
  const proto = Object.getPrototypeOf(value);
  const plain = proto === Object.prototype || proto === null;
  if ((Array.isArray(value) || plain) && depth >= 3) return '...';
  if (Array.isArray(value)) return '[' + value.map((v) => valueText(v, depth + 1)).join(', ') + ']';
  if (plain) {
    const parts = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined)
      .map(([k, v]) => (/^[A-Za-z_$][\w$]*$/.test(k) ? k : pyRepr(k)) + ': ' + valueText(v, depth + 1));
    return parts.length ? '{ ' + parts.join(', ') + ' }' : '{}';
  }
  const kind = (value as { constructor?: { name?: unknown } }).constructor?.name;
  return '<' + (typeof kind === 'string' && kind ? kind : 'object') + '>';
}

/** The WARNING for the run function of interruption key, named fn, that threw error while
 * mendwright handled it before before. The error is then thrown as it is. */
export function runFailedMessage(key: string, before: string, fn: string, error: unknown): string {
  return fn + '(), the run function of interruption ' + pyRepr(key) + ', raised an error while mendwright handled it '
    + 'before ' + pyRepr(before) + ' (' + errorType(error) + ': ' + firstLine(error) + '). The error is raised as it '
    + 'is.';
}

/** The TypeError text for a run function, named fn, that returned result, which is not a
 * promise: mendwright waits for the promise it returns before it goes on. */
export function runPlainMessage(key: string, fn: string, result: unknown): string {
  return 'mendwright: ' + fn + '(), the run function of interruption ' + pyRepr(key) + ', returned ' + valueText(result)
    + ", not a promise. heal.page's calls return promises, and a function that makes them without await does not "
    + 'wait for them, so mendwright cannot either: make it an async function and await them, or return the promise.';
}

/** The TypeError text for heal.interrupt(key, null, { run }). */
export function runRemovesMessage(key: unknown): string {
  return 'mendwright: heal.interrupt(' + pyRepr(key) + ', null) removes an interruption. To register one with run, '
    + "pass the selector of what shows it, such as '#session-expired'.";
}

export function unhandledMessage(key: string, before: string, error: unknown): string {
  const first = (String((error as Error)?.message ?? error).trim().split('\n')[0] || 'Error').slice(0, 200);
  return 'could not handle interruption ' + pyRepr(key) + ' before ' + pyRepr(before) + ' ('
    + ((error as Error)?.name ?? 'Error') + ': ' + first + '). Check the selector and action passed '
    + 'to heal.interrupt(' + pyRepr(key) + ', ...).';
}

/** The InterruptionLoop text. fn names the run function of heal.interrupt() that handled
 * key, if any. */
export function loopMessage(before: string, key: string, count: number, fn?: string | null): string {
  return 'mendwright: ' + count + ' interruptions were handled for ' + pyRepr(before) + ' and '
    + pyRepr(key) + ' is still on the page, so mendwright stopped instead of handling it again. '
    + checkCloses(key, fn);
}

/** What loopMessage() tells you to check: the action of interruption key, or its run
 * function. */
function checkCloses(key: string, fn?: string | null): string {
  if (fn) {
    return 'Check that ' + fn + '(), the run function registered with heal.interrupt(' + pyRepr(key) + ', ...), '
      + 'really closes it.';
  }
  return 'Check that the action registered with heal.interrupt(' + pyRepr(key) + ', ...) really closes '
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

// --- popups nobody registered: open({ dismiss: true }) (see dismiss.ts) -----------------------

/** The key handledInterruptions lists a popup under that open({ dismiss: true }) dismissed:
 * nobody registered it, so it has no key of its own. */
export const DISMISS_KEY = '<popup>';

// What a browser library says when the page went away under a call: a click that started a
// navigation, say.
const PAGE_WENT = /navigat|context was destroyed|target closed|has been closed|detached/i;

/** Whether a click that threw error may have reached the page all the same: Playwright's call
 * log says the click action was done (it then waited for a navigation, say, and timed out),
 * or the page navigated or closed under the call. False for an error that came before the
 * click, such as a timeout waiting for the element to be visible or another element that
 * intercepted it. */
export function clickMayHaveRun(error: unknown): boolean {
  const text = String((error as Error)?.message ?? error);
  return ACTION_RAN.test(text.toLowerCase()) || PAGE_WENT.test(text);
}

function listed(items: string[]): string {
  if (items.length < 2) return items.join('');
  return items.slice(0, -1).join(', ') + ' and ' + items[items.length - 1];
}

/** How a log line names one step on a popup: 'Escape', or a button's label in quotes, as
 * people read buttons. */
export function stepText(how: string, label?: string | null): string {
  if (how === 'escape') return 'Escape';
  return label ? '\u201c' + label + '\u201d' : 'an unlabelled button';
}

/** What mendwright did, from stepText() results: 'pressed Escape and clicked “Close”'. */
function did(tried: string[]): string {
  return listed(tried.map((t) => (t === 'Escape' ? 'pressed Escape' : 'clicked ' + t)));
}

/** The WARNING for a popup open({ dismiss: true }) dismissed. popup is how dismiss.summary()
 * names it, how is stepText() of the step that closed it, and tried lists the steps before
 * that did not close it. */
export function dismissedMessage(before: string, popup: string, how: string, tried: string[]): string {
  let text = 'dismissed a popup in the way of ' + pyRepr(before) + ': ' + popup + ' with ' + how;
  if (tried.length) text += ', after ' + listed(tried) + ' did not close it';
  return text;
}

const REGISTER = 'Register the button that closes it with heal.interrupt("<name>", "<selector of the button>"). '
  + 'If it needs data first, register its field before the button: heal.interrupt("<name>_field", '
  + "\"<selector of the field>\", { action: 'fill', args: ['<value>'] }).";

/** The WARNING for a popup in the way of a step that open({ dismiss: true }) found and could
 * not dismiss. tried lists the steps it took, in order (stepText()). escaped false says it did
 * not press Escape, because the keyboard focus was outside the popup or the popup is in an
 * iframe, or with consent because cookies: 'accept' is set. changed says the popup changed
 * before mendwright could act. */
export function undismissedMessage(before: string, popup: string, tried: string[],
                                   options: { escaped?: boolean; changed?: boolean; consent?: boolean } = {}): string {
  const { escaped = true, changed = false, consent = false } = options;
  let text: string;
  if (changed) {
    text = 'It changed before mendwright could act on it, so mendwright left it alone';
  } else if (tried.length) {
    const onlyEscape = tried.length === 1 && tried[0] === 'Escape';
    text = 'mendwright ' + did(tried) + ', which did not close it, and it has no ' + (onlyEscape ? '' : 'other ')
      + 'close or decline button that mendwright may click';
  } else {
    text = 'It has no close or decline button that mendwright may click, so mendwright left it alone';
  }
  if (!escaped && !changed && consent) {
    text += ". mendwright did not press Escape: with cookies: 'accept' it never does, because every key listener "
      + "of the page gets the key, a consent banner's too, which may take it as a refusal";
  } else if (!escaped && !changed) {
    text += '. mendwright did not press Escape, which goes to the element that has the keyboard focus: that was '
      + 'outside the popup, or the popup is in an iframe';
  }
  return 'could not dismiss a popup in the way of ' + pyRepr(before) + ': ' + popup + '. ' + text + '. The action '
    + "goes ahead, and fails with the browser's own error if the popup blocks it. " + REGISTER;
}

/** The WARNING for an element that could not be looked at any more once mendwright did done
 * ('pressed Escape', 'clicked “No thanks”') on a popup in its way, or, with raised, for a
 * click that threw after it may have reached the page. */
export function lostMessage(before: string, popup: string, done: string, raised = false): string {
  const what = raised
    ? ', and the click raised an error after it may have reached the page (a navigation it started did not finish '
      + 'in time, say), so mendwright cannot tell what it did. mendwright'
    : ', and then could not look at the element of ' + pyRepr(before) + ' as before (it matches no element or '
      + 'several now, it is hidden or outside the window, or it left the page). That step may have changed the page, '
      + 'so mendwright';
  return done.charAt(0).toUpperCase() + done.slice(1) + ' on a popup in the way of ' + pyRepr(before) + ': ' + popup
    + what + ' does not count the popup as dismissed and does not try a failed action again.';
}

/** The WARNING for a popup that looks like a consent banner, with cookies: 'accept' and
 * dismiss: true: dismissing it would turn the consent down. */
export function consentMessage(before: string, popup: string): string {
  return 'left a popup in the way of ' + pyRepr(before) + ' alone: ' + popup + '. It looks like a consent banner the '
    + "cookies: 'accept' preset does not know, and Escape, a close or a decline button could turn the consent down, "
    + "so mendwright does not dismiss it. The action goes ahead, and fails with the browser's own error if the "
    + 'banner blocks it. Register its accept button with heal.interrupt("<name>", "<selector of the button>").';
}

/** The WARNING for a popup that is a role="alertdialog": it asks for an answer, and Escape or
 * any of its buttons would give one. */
export function alertMessage(before: string, popup: string): string {
  return 'left a popup in the way of ' + pyRepr(before) + ' alone: ' + popup + '. It is an alert dialog '
    + '(role="alertdialog"), which asks for an answer, such as "Delete this project?", and Escape or any of its '
    + 'buttons would give one, so mendwright does not press or click anything on it. The action goes ahead, and '
    + "fails with the browser's own error if the dialog blocks it. If your script should answer it, register the "
    + 'button with heal.interrupt("<name>", "<selector of the button>").';
}

/** The InterruptionLoop text for a step whose budget is used up while a popup still covers its
 * element. again: a popup was dismissed in this step already, so it is likely one that comes
 * back each time; false: the budget went to interruptions. */
export function dismissLoopMessage(before: string, popup: string, count: number, again = true): string {
  if (again) {
    return 'mendwright: ' + count + ' interruptions and popups were handled for ' + pyRepr(before) + ' and a popup is '
      + 'still in the way: ' + popup + '. It comes back each time it is dismissed, so mendwright stopped instead of '
      + 'dismissing it again. Register the button that closes it for good with heal.interrupt(), or find out what '
      + 'opens it again.';
  }
  return 'mendwright: ' + count + ' interruptions were handled for ' + pyRepr(before) + ' and a popup is still in the '
    + 'way: ' + popup + '. A step handles at most ' + MAX_INTERRUPTIONS_PER_STEP + ', so mendwright stopped instead of '
    + 'dismissing it. Find out what keeps opening them, or register the button that closes the popup with '
    + 'heal.interrupt().';
}
