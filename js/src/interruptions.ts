// Interruptions: steps that can show up at any point of a flow, such as a cookie banner or a
// newsletter modal (Python's mendwright/interruptions.py). heal.interrupt(key, selector)
// registers one, and open({ cookies: 'reject' | 'accept' }) registers a ready-made one for
// the consent banners of common consent managers. mendwright looks for them only when a
// step needs help: when a lookup misses, before an action while the page shows one, and
// when an action failed because something covered its element.
//
// The preset's selectors are the Python package's, character for character:
//
// - OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, the
//   cookieconsent library, CookieYes (and the older Cookie Law Info plugin) and Complianz,
//   by the ids and classes of their buttons;
// - a fallback: a button or link whose own text, value, aria-label or title says reject,
//   decline, deny or necessary only ('reject'), or accept, allow or agree ('accept'), inside
//   a cookie banner. A Close button counts for 'reject' only when its whole label is Close
//   or Close followed by what it closes (Close banner, Close this dialog), or when it is an
//   icon whose aria-label or title says that. The banner is one element whose id, class or
//   aria label mentions cookie or consent, whose text mentions cookies and is at most 1500
//   characters, and that holds no main or nav element and no text field; never html, body
//   or main. So a consent screen that is not about cookies (an OAuth "Allow", a terms step)
//   is never clicked.

import { InvalidSelector } from './errors';
import { pyRepr } from './py';
import { roleChoices } from './roles';
import { XPATH, checkSyntax, classify, isGetByCall, normalize, stripPrefix } from './selectors';

export const COOKIE_BANNER_KEY = 'cookie_banner';
export const COOKIE_CHOICES = ['reject', 'accept'] as const;
export type CookieChoice = typeof COOKIE_CHOICES[number];

export interface Interruption {
  key: string;
  selectors: string[];
  action: string;
  args: unknown[];
  roles: string[];
  /** An XPath that matches when any of the XPath selectors could, checked first. */
  probe: string | null;
  preset: CookieChoice | null;
}

export interface InterruptionEvent {
  key: string;
  before: string;
}

export const ACTIONS = ['click', 'dblclick', 'fill', 'type', 'press', 'pressSequentially', 'check', 'uncheck',
  'setChecked', 'selectOption', 'hover', 'tap', 'clear', 'focus', 'setInputFiles', 'dragTo'];

/** Check heal.interrupt()'s arguments and build the Interruption. */
export function makeInterruption(key: unknown, selector: unknown,
                                 options: { action?: string; args?: unknown[]; role?: string } = {}): Interruption {
  if (typeof key !== 'string' || !key.trim()) {
    throw new TypeError("mendwright: heal.interrupt() needs a key, a non-empty name such as 'cookies', got "
      + pyRepr(key) + '.');
  }
  if (isGetByCall(key)) {
    throw new TypeError('mendwright: ' + pyRepr(key) + ' is how the Python package names a heal.page.get_by_*() '
      + 'call in the store, so heal.interrupt() cannot take it as a key. Pick another key.');
  }
  const items = Array.isArray(selector) ? selector : [selector];
  if (!items.length) {
    throw new TypeError('mendwright: heal.interrupt(' + pyRepr(key) + ', []) got an empty list. Pass the selector of '
      + "the element to act on, for example '#cookie-accept'.");
  }
  const selectors: string[] = [];
  for (const item of items) {
    try {
      checkSyntax(item);
    } catch (error) {
      if (error instanceof InvalidSelector) throw error.forKey(key);
      throw error;
    }
    selectors.push(normalize(item as string));
  }
  const action = options.action ?? 'click';
  if (typeof action !== 'string' || !ACTIONS.includes(action)) {
    throw new TypeError('mendwright: heal.interrupt(' + pyRepr(key) + ', ...) got action ' + pyRepr(action)
      + ', which mendwright cannot run. Use one of: ' + ACTIONS.join(', ') + '.');
  }
  const args = options.args ?? [];
  if (!Array.isArray(args)) {
    throw new TypeError('mendwright: heal.interrupt(' + pyRepr(key) + ', ...) takes args as an array, for example '
      + "{ action: 'press', args: ['Escape'] }.");
  }
  const roles = options.role !== undefined ? roleChoices(options.role) ?? [] : [];
  return { key, selectors: [...new Set(selectors)], action, args: [...args], roles, probe: null, preset: null };
}

/** Selectors that each point at one of count matches of selector: the selector itself when
 * it matches once, '(xpath)[n]' for an XPath that matches several, and nothing for another
 * kind that matches several. */
export function nthTargets(selector: string, count: number, limit = 5): string[] {
  if (count === 1) return [selector];
  if (count < 1 || classify(selector) !== XPATH) return [];
  const body = stripPrefix(selector);
  const out: string[] = [];
  for (let i = 1; i <= Math.min(count, limit); i++) out.push('xpath=(' + body + ')[' + i + ']');
  return out;
}

// --- the cookies preset -----------------------------------------------------------------------

const UPPER = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
const LOWER = 'abcdefghijklmnopqrstuvwxyz';
// Punctuation in a label, both apostrophes included, so "Don't allow" reads as 'don t allow'.
const PUNCT = ".,:;!?()[]'\u2019\u00d7-_/";

const cls = (name: string) => "contains(concat(' ', normalize-space(@class), ' '), ' " + name + " ')";
const ids = (...values: string[]) => values.map((i) => "@id='" + i + "'").join(' or ');
const union = (parts: string[]) => parts.join(' | ');
const spaces = (n: number) => ' '.repeat(n);

const LABEL = "concat(' ', translate(concat(normalize-space(.), ' ', @value, ' ', @aria-label, ' ', "
  + '@title), "' + UPPER + PUNCT + '", "' + LOWER + spaces([...PUNCT].length) + "\"), ' ')";
const BUTTONISH = "(self::button or self::a or @role='button' or (self::input and (@type='button' or @type='submit')))";
export const BANNER_MAX_TEXT = 1500;
const ATTRS = "translate(concat(@id, ' ', @class, ' ', @aria-label, ' ', @aria-labelledby, ' ', @data-testid), '"
  + UPPER + "', '" + LOWER + "')";
const TEXT_FIELD = ".//textarea or .//input[not(@type) or @type='text' or @type='email' or @type='password' or "
  + "@type='search' or @type='tel' or @type='url' or @type='number']";
const CONTAINER = 'ancestor-or-self::*[not(self::html or self::body or self::main)]'
  + '[contains(' + ATTRS + ", 'cookie') or contains(" + ATTRS + ", 'consent')]"
  + "[contains(translate(., '" + UPPER + "', '" + LOWER + "'), 'cookie')]"
  + '[string-length(normalize-space(.)) <= ' + BANNER_MAX_TEXT + ']'
  + '[not(.//main or .//nav or ' + TEXT_FIELD + ')]';

const says = (words: readonly string[]) => '(' + words.map((w) => 'contains(' + LABEL + ", ' " + w + " ')").join(' or ') + ')';
const saysNone = (words: readonly string[]) => 'not(' + says(words) + ')';

const SHOWN = "normalize-space(translate(concat(normalize-space(.), ' ', self::input/@value), \""
  + UPPER + PUNCT + '", "' + LOWER + spaces([...PUNCT].length) + '"))';
const CLOSE_WHAT = ['banner', 'notice', 'message', 'dialog', 'popup', 'pop up', 'modal', 'window', 'overlay', 'panel',
  'drawer', 'sheet', 'chat', 'survey', 'cart', 'menu', 'sidebar', 'search', 'lightbox', 'tour', 'video',
  'notification', 'tooltip', 'ad', 'advertisement', 'promotion', 'promo', 'newsletter', 'announcement', 'bar',
  'cookie banner', 'cookie notice', 'cookie message', 'cookie bar', 'cookies banner', 'cookies notice',
  'consent banner', 'privacy notice'];
/** The whole label of a Close button, in words. */
export const CLOSE_NAMES = ['close'].concat(...CLOSE_WHAT.map((w) => ['', 'the ', 'this '].map((a) => 'close ' + a + w)));
const CLOSE_LIST = "'|" + CLOSE_NAMES.join('|') + "|'";

function named(expr: string): string {
  const norm = 'normalize-space(translate(' + expr + ', "' + UPPER + PUNCT + '|", "' + LOWER
    + spaces([...PUNCT].length + 1) + '"))';
  return '(' + norm + " != '' and contains(" + CLOSE_LIST + ", concat('|', " + norm + ", '|')))";
}

const CLOSE = '(' + named("concat(normalize-space(.), ' ', self::input/@value)") + ' or (' + SHOWN
  + " = '' or string-length(normalize-space(.)) <= 1) and ((normalize-space(@aria-label) != '' and "
  + named('@aria-label') + ") or (normalize-space(@aria-label) = '' and " + named('@title') + ')))';

const REJECT_WORDS = ['reject', 'decline', 'deny', 'disagree', 'necessary only', 'only necessary',
  'necessary cookies only', 'essential only', 'only essential', 'without accepting', 'without agreeing'];
const ACCEPT_WORDS = ['accept', 'allow', 'agree'];
const NOT_ACCEPT = ['accept', 'allow', 'agree'];
const NOT_REJECT = ['reject', 'decline', 'deny', 'disagree', 'necessary', 'essential', 'not', 'don t', 'without'];

/** The fallback for consent banners no listed manager made. */
export function genericXpath(choice: CookieChoice): string {
  const label = choice === 'reject'
    ? '(' + says(REJECT_WORDS) + ' or ' + CLOSE + ') and ' + saysNone(NOT_ACCEPT)
    : says(ACCEPT_WORDS) + ' and ' + saysNone(NOT_REJECT);
  return '//*[' + BUTTONISH + ' and ' + CONTAINER + ' and string-length(normalize-space(.)) <= 40 and ' + label + ']';
}

// Per manager: [reject buttons, close buttons, accept buttons], as XPath expressions.
export const MANAGERS: Record<string, [string[], string[], string[]]> = {
  OneTrust: [
    ['//*[' + ids('onetrust-reject-all-handler') + ']', '//*[' + cls('ot-pc-refuse-all-handler') + ']'],
    ["//*[@id='onetrust-banner-sdk' or @id='onetrust-pc-sdk']//*[" + cls('onetrust-close-btn-handler') + ']'],
    ['//*[' + ids('onetrust-accept-btn-handler', 'accept-recommended-btn-handler') + ']'],
  ],
  Cookiebot: [
    ['//*[' + ids('CybotCookiebotDialogBodyButtonDecline', 'CybotCookiebotDialogBodyLevelButtonLevelOptinDeclineAll') + ']'],
    [],
    ['//*[' + ids('CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll', 'CybotCookiebotDialogBodyButtonAccept') + ']'],
  ],
  Didomi: [
    ['//*[' + ids('didomi-notice-disagree-button') + ']',
     "//*[@id='didomi-notice' or @id='didomi-popup' or @id='didomi-host']//*[" + cls('didomi-continue-without-agreeing') + ']'],
    [],
    ['//*[' + ids('didomi-notice-agree-button') + ']'],
  ],
  TrustArc: [
    ['//*[' + ids('truste-consent-required') + ']'],
    [],
    ['//*[' + ids('truste-consent-button') + ']'],
  ],
  Usercentrics: [
    ['//*[' + ids('uc-btn-deny-banner') + ']', "//*[@data-testid='uc-deny-all-button']"],
    [],
    ['//*[' + ids('uc-btn-accept-banner') + ']', "//*[@data-testid='uc-accept-all-button']"],
  ],
  'Quantcast Choice': [
    ["//*[@id='qc-cmp2-ui' or " + cls('qc-cmp2-container') + ']//button[' + says(['disagree', 'reject', 'reject all']) + ']'],
    [],
    ["//*[@id='qc-cmp2-ui' or " + cls('qc-cmp2-container') + "]//button[@mode='primary' and "
      + says(['agree', 'accept', 'accept all']) + ' and ' + saysNone(['disagree']) + ']'],
  ],
  Osano: [
    ['//*[' + cls('osano-cm-denyAll') + ']', '//*[' + cls('osano-cm-window') + ']//*[' + cls('osano-cm-deny') + ']'],
    ['//*[' + cls('osano-cm-window') + ']//*[' + cls('osano-cm-dialog__close') + ']'],
    ['//*[' + cls('osano-cm-accept-all') + ']', '//*[' + cls('osano-cm-window') + ']//*[' + cls('osano-cm-accept') + ']'],
  ],
  cookieconsent: [
    ['//*[' + cls('cc-window') + ']//*[' + cls('cc-deny') + ']',
     "//*[@id='cc-main']//*[@data-role='necessary' or @data-role='accept-necessary']"],
    [],
    ['//*[' + cls('cc-window') + ']//*[' + cls('cc-allow') + ' or ' + cls('cc-dismiss') + ']',
     "//*[@id='cc-main']//*[@data-role='all' or @data-role='accept-all']"],
  ],
  CookieYes: [
    ['//*[' + cls('cky-btn-reject') + " or @data-cky-tag='reject-button']", '//*[' + ids('cookie_action_close_header_reject') + ']'],
    ['//*[' + cls('cky-banner-btn-close') + " or @data-cky-tag='close-button']"],
    ['//*[' + cls('cky-btn-accept') + " or @data-cky-tag='accept-button']", '//*[' + ids('cookie_action_close_header') + ']'],
  ],
  Complianz: [
    ['//*[' + cls('cmplz-cookiebanner') + ']//*[' + cls('cmplz-deny') + ']'],
    ['//*[' + cls('cmplz-cookiebanner') + ']//*[' + cls('cmplz-close') + ']'],
    ['//*[' + cls('cmplz-cookiebanner') + ']//*[' + cls('cmplz-accept') + ']'],
  ],
};

/** Usercentrics' buttons inside its shadow root, which XPath cannot reach and Playwright's
 * CSS engine can. */
export const SHADOW_CSS: Record<CookieChoice, string> = {
  reject: '[data-testid="uc-deny-all-button"]',
  accept: '[data-testid="uc-accept-all-button"]',
};

export function cookieChoiceError(choice: unknown, entry = 'mendwright.open()'): string {
  return entry + ' got cookies ' + pyRepr(choice) + ". Use cookies: 'reject' to close consent banners without "
    + "accepting, cookies: 'accept' to accept them, or leave cookies out to click nothing you did not ask for.";
}

/** The selectors of the cookies preset, in the order they are tried. */
export function presetSelectors(choice: CookieChoice): string[] {
  if (!COOKIE_CHOICES.includes(choice)) throw new TypeError(cookieChoiceError(choice));
  const managers = Object.values(MANAGERS);
  const tiers = choice === 'reject'
    ? [managers.flatMap((m) => m[0]), managers.flatMap((m) => m[1])]
    : [managers.flatMap((m) => m[2])];
  const out = tiers.filter((tier) => tier.length).map((tier) => 'xpath=' + union(tier));
  out.push('xpath=' + genericXpath(choice));
  out.push(SHADOW_CSS[choice]);
  return out;
}

/** The Interruption that cookies: 'reject' or 'accept' registers. */
export function cookiePreset(choice: CookieChoice): Interruption {
  const selectors = presetSelectors(choice);
  const probe = 'xpath=' + union(selectors.filter((s) => classify(s) === XPATH).map(stripPrefix));
  return { key: COOKIE_BANNER_KEY, selectors, action: 'click', args: [], roles: [], probe, preset: choice };
}
