// Popups mendwright was never told about: open(page, { dismiss: true }) (the Python
// package's mendwright/dismiss.py, rule for rule).
//
// With dismiss: true, mendwright dismisses a popup that blocks an action and that no
// interruption registered with heal.interrupt() or the cookies preset accounts for: a
// newsletter or discount modal, a prompt for an address or a location that can be put off,
// an app-install banner, a survey, a chat invite, a consent banner when cookies is not set.
// It is off by default, and then mendwright makes no browser call for it.
//
// It acts only on evidence, and does nothing when unsure:
//
// - The action's element is covered. Before an action, once the registered interruptions
//   the page shows are handled, the driver looks at the point in the middle of the element,
//   when it is inside the window, and asks the browser which element is on top there
//   (document.elementFromPoint, into open shadow roots). The element is covered when that is
//   neither the element, nor inside it, nor one of its ancestors. After an action failed
//   because something covered its element (flow.isInterceptionError), and no registered
//   interruption is on the page, it looks the same way.
// - The covering element belongs to a popup (popupOf): the nearest dialog it is in, or else
//   the outermost fixed or sticky element it is in that takes pointer events and covers at
//   least a quarter of the window. A popup that holds the action's element is being used,
//   and is left alone.
// - It tries the least risky step first and looks again after each: Escape, when the
//   keyboard focus is on the page's body or in the popup (Escape goes to the focused
//   element) and cookies: 'accept' is not set, then a close button of the popup, then a
//   decline button (classify). It never clicks a button whose label says accept, subscribe,
//   sign up, yes, continue and the like (NEVER), a link that goes somewhere, a button that
//   submits a form or one in a label, a radio, a checkbox, a switch, a tab or an option,
//   and it never types, checks or selects anything.
// - It leaves alone a popup that asks a question: role="alertdialog" (nothing is pressed or
//   clicked), and one with a button that says cancel or keep (Escape only). A close or
//   decline button whose verb the popup's own text says ("Close your account?" with Close,
//   "Skip next week's delivery?" with Skip) is not one (Setting). A bare Skip or Dismiss is
//   one only in a popup whose own text reads as a promotion or onboarding (Setting.promo):
//   "New sign-in detected" with Dismiss and Review is left alone.
//
// The driver reports what the page shows (browser/popup.ts, the Python package's scripts);
// the rules are here. Text is read as Python reads it: NFKC, Python's lower(), split() on
// what str.isspace() is true for, isdigit(), and lengths and cuts in code points, so each
// rule gives the Python package's answer (a test runs both on the same tables).

import { scrub } from './capture';
import { CLOSE_NAMES, PUNCT } from './interruptions';
import { isDigitChar, pyLen, pyRstrip, pySlice, pySplit, pyStrip, pyTruthy, strOr } from './py';

/** The actions whose element is not looked at before they run: focus() and setInputFiles()
 * work on an element whatever is on top of it, and a file input is often hidden. */
export const NOT_LOOKED_AT: ReadonlySet<string> = new Set(['focus', 'setInputFiles']);

/** The least part of the window a fixed or sticky element must cover to count as a popup. A
 * header, a footer or a toolbar covers less, and is never dismissed. */
export const MIN_AREA = 0.25;

/** What may follow "continue without": what the button leaves out. "Continue without
 * accepting" declines; "Continue without delay" is not a decline button. */
export const WITHOUT: readonly string[] = ['accepting', 'accepting all', 'accepting cookies', 'agreeing', 'an account',
  'account', 'signing in', 'signing up', 'logging in', 'registering', 'subscribing', 'cookies'];

/** The phrases a decline button's label says, in any popup but those of PROMO_ONLY. Each only
 * puts something off, it answers nothing. */
export const DECLINE: readonly string[] = ['no thanks', 'no thank you', 'not now', 'maybe later', 'later', 'skip',
  'skip for now', 'dismiss', 'not interested', ...WITHOUT.map((w) => 'continue without ' + w)];

/** The DECLINE phrases that decline only in a popup whose own text reads as a promotion or
 * onboarding (Setting.promo). Elsewhere a bare Skip or Dismiss may acknowledge something
 * that matters: "New sign-in detected. [Dismiss] [Review]". */
export const PROMO_ONLY: readonly string[] = ['skip', 'skip for now', 'dismiss'];

/** What a popup's own text says when it is a promotion or an onboarding step, as whole words
 * and phrases. Not stems: "Your subscription payment failed" is not "subscribe". Not
 * "notifications" alone, and not "welcome back", which a sign-in notice says. A part off
 * ("10% off"), getting the app and cookie or consent text count too (promoText). */
export const PROMO: readonly string[] = ['newsletter', 'newsletters', 'subscribe', 'subscribing', 'sign up for',
  'offer', 'offers', 'discount', 'discounts', 'sale', 'deal', 'deals', 'coupon', 'coupons', 'promo', 'promos',
  'promo code', 'promotion', 'promotions', 'promotional', 'free shipping', 'turn on notifications',
  'enable notifications', 'allow notifications', 'push notifications', 'get notifications', 'survey', 'surveys',
  'feedback', 'rate us', 'tour', 'tours', 'tips', 'what s new', 'whats new', 'welcome', 'getting started',
  'chat with us'];

const escapeRe = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const longestFirst = (items: readonly string[]) => [...items].sort((a, b) => b.length - a.length);

const PROMO_TEXT = new RegExp('(?<!\\S)(?:' + longestFirst([...new Set(PROMO)].filter((p) => p !== 'welcome'))
  .map(escapeRe).join('|')
  + '|welcome(?! back(?!\\S))'                        // "Welcome back" greets a sign-in
  // The app: download, install or get the app, our app, the Acme app, our free Acme app.
  + '|(?:download|install|get) (?:the|our)(?: \\S+){0,2} app)(?!\\S)'
  // A part off, "10% off" (words() keeps the %).
  + '|% ?off(?!\\S)');

/** The phrases that decline only in a popup whose text speaks of cookies (Setting.cookies).
 * Elsewhere they are the answer to a request: reject an expense, decline an invitation. */
export const COOKIE_DECLINE: readonly string[] = ['reject', 'reject all', 'decline'];

/** The words that may come with a decline phrase in a label, at most MAX_FILLER of them. */
export const FILLER: ReadonlySet<string> = new Set(['i', 'im', 'id', 'ill', 'ive', 'm', 'd', 'll', 've', 'am', 'me',
  'you', 'it', 'this', 'that', 'the', 'a', 'for', 'now', 'today', 'time', 'right', 'just', 'all', 'please', 'thanks',
  'thank', 'no', 'not', 'but', 'rather', 'really', 'maybe', 'later', 'again', 'good', 'fine', 'browsing', 'remind',
  'ask', 'step', 'tour', 'intro', 'survey', 'question', 'questions']);
export const MAX_FILLER = 4;

/** The verbs of a close or decline button that a popup may ask about, with the forms its text
 * may say them in: "Close your account?" with a Close button. Such a button may be the
 * answer, so it is not clicked. */
export const ECHO: Readonly<Record<string, readonly string[]>> = {
  close: ['close', 'closes', 'closed', 'closing', 'closure'],
  skip: ['skip', 'skips', 'skipped', 'skipping'],
  dismiss: ['dismiss', 'dismisses', 'dismissed', 'dismissing', 'dismissal'],
};

/** A control whose label has a word that starts with one of these makes the popup a
 * question: "Cancel", "Keep it". Then none of its other buttons is clicked. */
export const ASKS: readonly string[] = ['cancel', 'keep'];

/** Words that keep a button from ever being clicked, anywhere in its label (its text, hidden
 * text included, value, aria-label, title and what its aria-labelledby names). Each matches as
 * a word stem (see stem): 'pay' also catches "payment". 'continue' is one too, unless the label
 * says 'continue without' and what it leaves out (WITHOUT). */
export const NEVER: readonly string[] = ['accept', 'agree', 'allow', 'subscribe', 'sign up', 'sign in', 'log in',
  'join', 'register', 'yes', 'ok', 'confirm', 'buy', 'pay', 'order', 'save', 'submit', 'delete', 'remove', 'install',
  'enable', 'notify', 'start', 'get', 'claim', 'try',
  // The same, spelled otherwise, and others like them.
  'signup', 'signin', 'login', 'unsubscribe', 'subscription', 'registration', 'submission', 'signing up',
  'signing in', 'logging in', 'trial', 'tried', 'tries', 'bought', 'paid', 'sent', 'discard', 'create', 'purchase',
  'checkout', 'cancel', 'send', 'share', 'download', 'upgrade', 'unlock', 'redeem', 'apply', 'book', 'donate',
  'follow', 'open', 'next', 'proceed', 'verify', 'activate', 'connect', 'log out', 'logout', 'sign out', 'leave'];

// Punctuation and the minus sign read as spaces.
const TRANSLATE = new Set([...PUNCT, '\u2212']);
// What a label is read for: its text, the text it hides, its value, aria-label, title, and
// the text of the elements its aria-labelledby names.
const LABEL_KEYS = ['text', 'all', 'value', 'aria', 'title', 'labelledby'];

/** A control of a popup, as POPUP_CONTROLS_JS lists it: tag, type, role, href, text, all,
 * value, aria, title, disabled, link, label, field, submits, labelledby and checked. */
export type Control = Record<string, unknown>;
/** One element of a probe's chain (POPUP_PROBE_JS). */
export type ChainElement = Record<string, unknown>;

const FORMAT = /^\p{Cf}$/u;
const DASH = /^\p{Pd}$/u;
const CURRENCY = /^\p{Sc}$/u;

const isDict = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === 'object' && !Array.isArray(value);

/** One character of a label as words() reads it: a dash of any kind is punctuation, and a
 * format character (a soft hyphen, a zero-width space, a byte order mark) is nothing. */
function plain(c: string): string {
  if (FORMAT.test(c)) return '';
  return DASH.test(c) ? ' ' : c;
}

/** text the way labels are compared: its compatibility form (NFKC: a ligature such as ﬁ is
 * fi, a fullwidth letter is the letter), in lower case, with punctuation and dashes as
 * spaces, format characters dropped and the spaces normalized. */
export function words(text: unknown): string {
  let out = '';
  for (const c of strOr(text).normalize('NFKC')) out += plain(c);
  let spaced = '';
  for (const c of out.toLowerCase()) spaced += TRANSLATE.has(c) ? ' ' : c;
  return pySplit(spaced).join(' ');
}

/** How a NEVER word matches: as the start of a word, without a last e or y in a word of more
 * than three letters, so 'save' catches "saving" and 'notify' "notification". */
export function stem(word: string): string {
  return pyLen(word) > 3 && /[ey]$/.test(word) ? word.slice(0, -1) : word;
}

type Entry = [string, string[], string];

function entry(word: string): Entry {
  const parts = pySplit(word);
  return [word, parts.slice(0, -1), stem(parts[parts.length - 1])];
}

const NEVER_STEMS: Entry[] = NEVER.map(entry);
const CONTINUE = entry('continue');

/** Whether the words found say a NEVER entry: its words before the last one exactly, then a
 * word that starts with the last one's stem. */
function matches(found: string[], [, head, stemmed]: Entry): boolean {
  const n = head.length;
  for (let i = 0; i < found.length - n; i++) {
    if (head.every((w, j) => found[i + j] === w) && found[i + n].startsWith(stemmed)) return true;
  }
  return false;
}

const LEFT_OUT = new RegExp('(?<!\\S)(?:' + longestFirst(WITHOUT).map((w) => escapeRe('continue without ' + w))
  .join('|') + ')(?!\\S)', 'g');

/** label, already words(), with each "continue without <what it leaves out>" taken out. */
function leftOut(label: string): string {
  return pySplit(label.replace(LEFT_OUT, ' ')).join(' ');
}

const labelText = (control: Control) => LABEL_KEYS.map((k) => strOr(control[k])).join(' ');

/** The NEVER word a control's label says (see stem), or null. */
export function forbidden(control: Control): string | null {
  const found = pySplit(leftOut(words(labelText(control))));
  if (matches(found, CONTINUE)) return 'continue';
  const hit = NEVER_STEMS.find((e) => matches(found, e));
  return hit ? hit[0] : null;
}

/** Whether a control's label has a digit or a currency sign in it, as in "Skip the line -
 * $9.99" or "No, I want 10% off": such a button may cost or decide something, so it is never
 * a close or decline button. */
export function priced(control: Control): boolean {
  for (const c of labelText(control)) if (isDigitChar(c) || CURRENCY.test(c)) return true;
  return false;
}

// A javascript: link whose code is one of these (spaces and a last ; left out) does nothing.
const NOWHERE = ['', 'void(0)', 'void0', 'void(null)'];

/** Whether a link's href goes nowhere: none, '#', or javascript: code that does nothing
 * (javascript:, javascript:void(0), javascript:;). Any other javascript: code runs, and may
 * load a page: javascript:location.assign('/checkout'). */
export function goesNowhere(href: unknown): boolean {
  if (href === null || href === undefined) return true;
  const text = pyStrip(String(href)).toLowerCase();
  if (text === '#') return true;
  if (!text.startsWith('javascript:')) return false;
  const code = pySplit(text.slice('javascript:'.length)).join('').replace(/;+$/, '');
  return NOWHERE.includes(code);
}

/** A control mendwright may click: a button, an element with role=button, an input of type
 * button or submit, or a link that goes nowhere (goesNowhere).
 *
 * Never one that is disabled; one with another role (a radio, checkbox, switch, tab, option,
 * link, menu item, ...) or with aria-checked, aria-pressed or aria-selected, since a click on
 * it chooses or toggles something; a link with a real href, whatever its role; one inside a
 * link that goes somewhere or that holds one; one in a <label>, or a label itself; one that
 * holds a field; or one that submits or resets its form, whatever the form's method. */
export function isControl(control: Control): boolean {
  if (['disabled', 'link', 'label', 'field', 'submits', 'checked'].some((k) => pyTruthy(control[k]))) return false;
  const tag = control.tag;
  const role = pyTruthy(control.role) ? control.role : '';
  if (role !== '' && role !== 'button') return false;
  if (tag === 'a') return goesNowhere(control.href);
  if (tag === 'input') return control.type === 'button' || control.type === 'submit';
  if (tag === 'label') return false;
  return tag === 'button' || role === 'button';
}

/** The DECLINE phrase (or, with cookies, COOKIE_DECLINE phrase) one label, already words(),
 * declines by, or null: the phrase in it, and no other word than FILLER, at most MAX_FILLER
 * of them. */
function declinePhrase(label: string, cookies = false): string | null {
  const found = pySplit(label);
  for (const phrase of cookies ? [...DECLINE, ...COOKIE_DECLINE] : DECLINE) {
    const size = pySplit(phrase).length;
    for (let i = 0; i < found.length - size + 1; i++) {
      if (found.slice(i, i + size).join(' ') !== phrase) continue;
      const rest = [...found.slice(0, i), ...found.slice(i + size)];
      if (rest.length <= MAX_FILLER && rest.every((w) => FILLER.has(w))) return phrase;
    }
  }
  return null;
}

/** What a control shows: its rendered text (text hidden with display: none left out), and
 * that with an input's value, as words(). */
function shown(control: Control): [string, string] {
  const raw = pySplit(strOr(control.text)).join(' ');
  return [raw, words(raw + ' ' + strOr(control.value))];
}

/** Whether a control is an icon: it shows one character or none, and has no value. */
function icon(control: Control): boolean {
  const [raw, text] = shown(control);
  return text === '' || (pyLen(raw) <= 1 && !pyStrip(strOr(control.value)));
}

/** An icon's name, as words(): its aria-label, or without one its title. */
function nameOf(control: Control): string {
  return words(control.aria) || words(control.title);
}

/** The label a control declines by: what it shows, or for an icon its name. */
function declineLabel(control: Control): string {
  return icon(control) ? nameOf(control) : shown(control)[1];
}

/** Whether a popup's own text (Setting.prose) reads as a promotion or onboarding: it says a
 * PROMO word or phrase, get the app or the like, a part off ("10% off"), or a word that
 * starts with cookie or consent. */
function promoText(prose: string): boolean {
  return PROMO_TEXT.test(prose) || pySplit(prose).some((w) => w.startsWith('cookie') || w.startsWith('consent'));
}

/** What a popup says around its controls, which changes what a control means there.
 *
 * cookies: its text speaks of cookies, so reject and decline are declines (COOKIE_DECLINE).
 * prose: its own text outside its controls, and its aria-label, as words(): a close, skip or
 * dismiss button whose verb it says (ECHO) may be the answer to it. asks: one of its controls
 * says cancel or keep (ASKS), so it asks a question and no close or decline button of it
 * counts. promo, read from cookies and prose: the popup reads as a promotion or onboarding,
 * so a bare skip or dismiss declines there (PROMO_ONLY). */
export class Setting {
  readonly cookies: boolean;
  readonly prose: string;
  readonly asks: boolean;

  constructor(options: { cookies?: boolean; prose?: string; asks?: boolean } = {}) {
    this.cookies = options.cookies ?? false;
    this.prose = options.prose ?? '';
    this.asks = options.asks ?? false;
  }

  get promo(): boolean {
    return this.cookies || promoText(this.prose);
  }
}

const labelWords = (control: Control) => pySplit(words(labelText(control)));

/** The Setting of a popup (a Popup, or null) with these controls (POPUP_CONTROLS_JS). A
 * driver that does not say what the popup's own text is (prose) gets its whole text, its
 * buttons' labels included, which only keeps more buttons from a click. */
export function setting(popup: Popup | null, controls: unknown[] | null | undefined): Setting {
  const element: Record<string, unknown> = popup ? popup.element : {};
  let own = element.prose;
  if (typeof own !== 'string') own = element.text;
  const prose = words(strOr(own) + ' ' + strOr(element.aria));
  const text = words(element.text) + ' ' + prose;
  const asks = (controls ?? []).some((control) => isDict(control)
    && labelWords(control).some((w) => ASKS.some((a) => w.startsWith(a))));
  return new Setting({ cookies: pySplit(text).some((w) => w.startsWith('cookie')), prose, asks });
}

/** Whether a popup's own text (Setting.prose) says verb in one of its ECHO forms. */
function echoes(prose: string, verb: string): boolean {
  const forms = Object.prototype.hasOwnProperty.call(ECHO, verb) ? ECHO[verb] : [];
  return pySplit(prose).some((w) => forms.includes(w));
}

/** 'close', 'decline' or null for one control of a popup, in a popup that says what where
 * says.
 *
 * close: the label it shows is exactly a close name ("Close", "Close dialog", "Close this
 * banner", ... as for the cookies preset, CLOSE_NAMES), or it is an icon (it shows one
 * character or none) whose aria-label, or without one its title, is one. A bare ×, or an icon
 * named "Close position", is not one. decline: what it shows (for an icon, its name) says a
 * DECLINE phrase, or in a popup that speaks of cookies reject or decline, with no other words
 * than a few harmless ones (FILLER). Skip, skip for now and dismiss decline only in a popup
 * that reads as a promotion or onboarding (Setting.promo).
 *
 * Neither for a control mendwright never clicks (isControl, forbidden), one whose label has a
 * digit or a currency sign (priced), any control of a popup that asks, or a close, skip or
 * dismiss button whose verb the popup's own text says (ECHO). */
export function classify(control: Control, where?: Setting | null): 'close' | 'decline' | null {
  if (!isControl(control) || forbidden(control) || priced(control)) return null;
  const place = where ?? new Setting();
  if (place.asks) return null;
  if (CLOSE_NAMES.includes(shown(control)[1]) || (icon(control) && CLOSE_NAMES.includes(nameOf(control)))) {
    return echoes(place.prose, 'close') ? null : 'close';
  }
  const label = declineLabel(control);
  const phrase = label ? declinePhrase(label, place.cookies) : null;
  if (phrase === null || (PROMO_ONLY.includes(phrase) && !place.promo)) return null;
  return echoes(place.prose, pySplit(phrase)[0]) ? null : 'decline';
}

/** Whether a decline control's label is a decline phrase and nothing else, such as "No
 * thanks". pick() prefers those. */
export function wholeDecline(control: Control, where?: Setting | null): boolean {
  const label = declineLabel(control);
  return DECLINE.includes(label) || Boolean(where && where.cookies && COOKIE_DECLINE.includes(label));
}

/** What pick() chose: [index, control] of the first close control and of the decline control. */
export interface Picked {
  close?: [number, Control];
  decline?: [number, Control];
}

/** The first close control in the order the page lists them, and the first decline control
 * whose label is a decline phrase and nothing else ("No thanks"), else the first decline
 * control. popup is the Popup they are in, whose text changes what they mean (setting()). */
export function pick(controls: unknown[] | null | undefined, popup: Popup | null = null): Picked {
  const where = setting(popup, controls);
  const out: Picked = {};
  let loose: [number, Control] | null = null;
  (controls ?? []).forEach((control, index) => {
    if (!isDict(control)) return;
    const kind = classify(control, where);
    if (kind === 'close' && !out.close) {
      out.close = [index, control];
    } else if (kind === 'decline' && !out.decline) {
      if (wholeDecline(control, where)) out.decline = [index, control];
      else if (loose === null) loose = [index, control];
    }
  });
  if (!out.decline && loose !== null) out.decline = loose;
  return out;
}

/** How a log line names a control: what it shows, else (for an icon that shows one character
 * or none) its aria-label or title, cut to 40 characters. */
export function controlText(control: Control): string {
  const texts = ['text', 'value', 'aria', 'title'].map((k) => pySplit(strOr(control[k])).join(' '));
  let text = texts[0] || texts[1];
  if (pyLen(text) <= 1) text = texts[2] || texts[3] || text;
  return scrub(pySlice(text, 40));
}

// --- what the page showed ------------------------------------------------------------------

const ELEMENT_KEYS = ['tag', 'id', 'cls', 'role', 'modal', 'open', 'pos', 'area', 'holds', 'text'];
const WHY = ['free', 'hidden', 'outside'];

/** What POPUP_PROBE_JS saw at the middle of an element: covered, the elements from the one on
 * top up to (not including) body when it is covered, and, when a popup was passed as known,
 * whether it is gone (removed or hidden). gone is null when nothing was passed. why says why
 * an element is not covered: 'free' (it, or part of it, is on top), 'hidden' (it has no size
 * or is hidden) or 'outside' (its middle is outside the window); '' when the element is
 * covered or the driver did not say. */
export interface Probe {
  covered: boolean;
  chain: ChainElement[];
  gone: boolean | null;
  why: string;
}

/** POPUP_PROBE_JS's result as a Probe, or null for anything else (the element matched none
 * or several, or a driver answered something mendwright does not know), which counts as not
 * covered. */
export function parseProbe(raw: unknown): Probe | null {
  if (!isDict(raw) || typeof raw.covered !== 'boolean') return null;
  const gone = typeof raw.gone === 'boolean' ? raw.gone : null;
  if (!raw.covered) {
    const why = raw.why;
    return { covered: false, chain: [], gone, why: typeof why === 'string' && WHY.includes(why) ? why : '' };
  }
  const chain = raw.chain;
  if (!Array.isArray(chain) || !chain.length
      || !chain.every((el) => isDict(el) && ELEMENT_KEYS.every((k) => k in el))) return null;
  return { covered: true, chain: [...chain] as ChainElement[], gone, why: '' };
}

/** role=dialog or alertdialog, aria-modal=true, or an open <dialog>. */
export function isDialog(element: ChainElement): boolean {
  return element.role === 'dialog' || element.role === 'alertdialog' || element.modal === true || element.open === true;
}

/** The popup over an element: how many steps up from the element on top its root is, the root
 * as the probe saw it, and how log lines name it (summary()). alert: it is, or is inside, a
 * role="alertdialog", which asks for an answer (see asksAnswer()). */
export interface Popup {
  depth: number;
  element: ChainElement;
  summary: string;
  alert: boolean;
}

/** Whether a popup is, or is inside, a role="alertdialog". ARIA's alert dialog is one that
 * asks for the user's answer: "Close your position?", "Delete this project?". Escape or any of
 * its buttons would answer it, so mendwright leaves it alone. */
export function asksAnswer(popup: Popup): boolean {
  return Boolean(popup.alert || popup.element.role === 'alertdialog');
}

/** float() of what Python reads as a number here: a number, a bool, or a number written out.
 * NaN for anything else. */
function pyFloat(value: unknown): number {
  if (typeof value === 'number') return value;
  if (typeof value === 'boolean') return value ? 1 : 0;
  if (typeof value !== 'string') return NaN;
  const text = pyStrip(value).replace(/(?<=\d)_(?=\d)/g, '');
  if (/^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$/.test(text)) return Number(text);
  if (/^[+-]?(?:inf|infinity)$/i.test(text)) return text.startsWith('-') ? -Infinity : Infinity;
  return NaN;
}

const popupFrom = (depth: number, element: ChainElement, alert: boolean): Popup =>
  ({ depth, element, summary: summary(element), alert });

/** The Popup covering the element, or null: when it is not covered, when the element is
 * inside the popup (the dialog that covers it holds it too), or when what covers it is not a
 * popup: no dialog, and no fixed or sticky element outside the element's own containers that
 * takes pointer events and covers at least MIN_AREA of the window. A sticky header is not one,
 * nor the full-window layer with pointer-events: none that toast libraries put a small toast
 * in. */
export function popupOf(probe: Probe | null): Popup | null {
  if (probe === null || !probe.covered) return null;
  const chain = probe.chain;
  const alert = chain.some((element) => element.role === 'alertdialog');
  for (let depth = 0; depth < chain.length; depth++) {
    const element = chain[depth];
    if (isDialog(element)) return pyTruthy(element.holds) ? null : popupFrom(depth, element, alert);
  }
  const fixed: number[] = [];
  chain.forEach((element, depth) => {
    if ((element.pos === 'fixed' || element.pos === 'sticky') && !pyTruthy(element.holds) && element.events !== false) {
      fixed.push(depth);
    }
  });
  if (!fixed.length) return null;
  const depth = fixed[fixed.length - 1];   // the outermost
  const element = chain[depth];
  return pyFloat(element.area) >= MIN_AREA ? popupFrom(depth, element, alert) : null;
}

// What a consent banner's text says, in English and in the words of the GDPR's languages that
// consent managers use.
const CONSENT_TEXT = new RegExp('cookie|consent|value your privacy|privacy (?:settings|preferences|choices|center)'
  + '|partners|personal data|tracker|traceur|datenschutz|privacidad|confidentialit'
  + '|donn[ée]es personnelles|datos personales|dati personali|einwilligung', 'iu');
// What a consent banner's id, class or aria-label says: those and the names of the GDPR
// (DSGVO, RGPD), of consent managers (CMP, TCF, Sourcepoint's sp_message, ...).
const CONSENT_NAME = new RegExp('cookie|consent|gdpr|privacy|dsgvo|rgpd|datenschutz|cmp|tcf|sp_message|didomi'
  + '|onetrust|cybot|usercentrics|truste|quantcast|osano|cmplz|iubenda|axeptio|klaro'
  + '|tarteaucitron', 'iu');

/** What an accept button's label says, as the start of a word, in the languages consent
 * managers use. */
export const ACCEPT_STEMS: readonly string[] = ['accept', 'agree', 'allow', 'consent', 'akzept', 'akcept', 'zustimm',
  'einverstanden', 'erlaub', 'autoris', 'accett', 'acept', 'aceit', 'permit', 'toestaan', 'godkend', 'godta',
  'hyväksy'];

/** Whether a popup looks like a consent banner: its text says cookie, consent, your privacy,
 * partners, personal data, trackers or the like, or its id, class or aria-label says cookie,
 * consent, gdpr, privacy, dsgvo, rgpd, cmp, tcf or a consent manager's name. */
export function isConsent(popup: Popup): boolean {
  const element = popup.element;
  const name = ['id', 'cls', 'aria'].map((k) => strOr(element[k])).join(' ');
  return CONSENT_TEXT.test(dotted(strOr(element.text))) || CONSENT_NAME.test(dotted(name));
}

// Python's re.IGNORECASE matches i to the Turkish İ and ı, which JavaScript's case folding
// does not (it does match ſ to s and the Kelvin sign to k, as Python does).
const dotted = (text: string) => text.replace(/[\u0130\u0131]/g, 'i');

/** Whether one of a popup's controls says accept, agree, allow or consent, in English or
 * another language (ACCEPT_STEMS): the popup asks for consent, whatever its text or name.
 * With cookies: 'accept' mendwright leaves such a popup alone. */
export function asksConsent(controls: unknown[] | null | undefined): boolean {
  return (controls ?? []).some((control) => isDict(control)
    && labelWords(control).some((w) => ACCEPT_STEMS.some((s) => w.startsWith(s))));
}

function attr(value: unknown, limit = 60): string {
  return pySlice(pySplit(strOr(value)).join(' '), limit).replace(/"/g, "'");
}

/** How a log line names a popup: its tag with its id, role and first classes, and the first
 * 60 characters of its text, with runs of 12 to 19 digits redacted. For example
 * <div role="dialog" class="nl"> ("Get 10% off your first order…"). */
export function summary(element: ChainElement): string {
  let head = '<' + attr(element.tag, 20);
  const parts: [string, unknown][] = [['id', element.id], ['role', element.role],
    ['class', pySplit(strOr(element.cls)).slice(0, 3).join(' ')]];
  for (const [name, value] of parts) {
    if (pyTruthy(value)) head += ' ' + name + '="' + attr(value) + '"';
  }
  head += '>';
  const text = scrub(pySplit(strOr(element.text)).join(' '));
  if (text) {
    const cut = pyRstrip(pySlice(text, 60));
    head += ' ("' + cut + (pyLen(text) > 60 ? '…' : '') + '")';
  }
  return head;
}

/** What POPUP_ROOT_JS checks the popup's root against before it returns it. */
export function expectOf(popup: Popup): { depth: number; tag: unknown; id: unknown } {
  return { depth: popup.depth, tag: popup.element.tag || '', id: popup.element.id || '' };
}

/** What POPUP_CONTROL_JS checks a control against before it returns it for a click: its
 * number, and every fact POPUP_CONTROLS_JS gave of it. A control that changed in any of them
 * since it was listed, such as an input whose value is now "Yes, subscribe me", is not
 * clicked. */
export function expectControl(index: number, control: Control): { index: number; facts: Control } {
  return { index, facts: { ...control } };
}
