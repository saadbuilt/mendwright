// The rules of open({ dismiss: true }) (dismiss.ts), the texts they log, the page scripts and
// the flow.dismiss journal line, against the Python package's mendwright.dismiss, flow and
// journal, run from this repository on the same tables. Each table also says what the answer
// must be, as the Python package's tests do, so a rule that is taken out fails here even
// without Python.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { describe, test } from 'node:test';

import * as popupScripts from '../../src/browser/popup';
import {
  MIN_AREA, NEVER, Setting, asksAnswer, asksConsent, classify, controlText, expectControl, expectOf, forbidden,
  goesNowhere, isConsent, isControl, parseProbe, pick, popupOf, priced, setting, summary, words,
  type Control, type Popup,
} from '../../src/dismiss';
import {
  alertMessage, clickMayHaveRun, consentMessage, dismissLoopMessage, dismissedMessage, lostMessage, stepText,
  undismissedMessage,
} from '../../src/flow';
import { Journal } from '../../src/journal';
import { tempDir } from '../support/env';
import { needsPython, runPython } from '../support/python';

const skip = needsPython();

/** A control as POPUP_CONTROLS_JS lists it (the Python tests' _control). */
function control(text = '', tag = 'button', more: Control = {}): Control {
  return {
    tag, type: tag === 'button' ? 'button' : '', role: '', href: null, text, all: text, value: '', aria: '',
    title: '', disabled: false, link: false, label: false, field: false, submits: false, ...more,
  };
}

/** One element of a probe's chain (the Python tests' _el). */
function el(more: Record<string, unknown> = {}, tag = 'div'): Record<string, unknown> {
  return { tag, id: '', cls: '', role: '', modal: false, open: false, pos: 'static', area: 0.0, holds: false, text: '',
    ...more };
}

function popupWith(text: string, prose?: string): Popup {
  const element = el({ role: 'dialog', text, ...(prose === undefined ? {} : { prose }) });
  return { depth: 0, element, summary: summary(element), alert: false };
}

// --- which buttons close or decline -------------------------------------------------------------

const CONTROLS: [Control, string | null][] = [
  [control('Close'), 'close'],
  [control('Close dialog'), 'close'],
  [control('×', 'button', { aria: 'Close' }), 'close'],
  [control('', 'button', { aria: 'Close newsletter' }), 'close'],
  [control('×', 'button', { aria: 'Close this dialog' }), 'close'],
  [control('×', 'button', { aria: 'Close cart' }), 'close'],
  [control('✖', 'button', { title: 'Close' }), 'close'],
  [control('X', 'button', { aria: 'Close' }), 'close'],
  [control('Close the banner'), 'close'],
  // A bare glyph is not a close button: in a cart or a list of filters it removes one row.
  [control('✕'), null],
  [control('X'), null],
  [control('×'), null],
  [control('✖'), null],
  [control('X', 'button', { aria: 'Twitter' }), null],
  [control('Learn more', 'button', { aria: 'Close' }), null],
  // An icon named for what it closes, other than the popup.
  [control('×', 'button', { aria: 'Close position EURUSD' }), null],
  [control('×', 'button', { title: 'Close position' }), null],
  [control('', 'button', { title: 'Close ticket' }), null],
  [control('', 'button', { aria: 'Close account' }), null],
  [control('', 'button', { aria: 'Close my account' }), null],
  [control('', 'button', { aria: 'Close trade' }), null],
  [control('', 'button', { aria: 'Close the deal' }), null],
  [control('Close my account'), null],
  [control('×', 'button', { aria: 'Close', title: 'Remove item' }), null],
  [control('No thanks'), 'decline'],
  [control('No, thanks'), 'decline'],
  [control('No thank you'), 'decline'],
  [control("No thanks, I'd rather not"), 'decline'],
  [control("No thanks, I'll pay full price"), null],
  [control('Not now'), 'decline'],
  [control('Not now, thanks'), 'decline'],
  [control('Maybe later'), 'decline'],
  [control('Remind me later'), 'decline'],
  // A bare Skip or Dismiss declines only in a popup that reads as a promotion or onboarding.
  [control('Skip'), null],
  [control('Skip for now'), null],
  [control('Dismiss'), null],
  // Outside a popup about cookies, reject and decline answer a request.
  [control('Reject'), null],
  [control('Reject all'), null],
  [control('Decline'), null],
  [control('Decline offer'), null],
  [control("I'm not interested"), 'decline'],
  [control('Continue without accepting'), 'decline'],
  [control('Continue without an account'), 'decline'],
  // A decline phrase with words that do more is not a decline button.
  [control('No'), null],
  [control('No, I want 10% off'), null],
  [control('Got it, thanks'), null],
  [control('Count me in, thanks'), null],
  [control('Thanks, I am in!'), null],
  [control('No-risk trial'), null],
  [control('Skip the line - $9.99'), null],
  [control('Skip the line'), null],
  [control('Skip to payment'), null],
  [control('Continue without delay'), null],
  [control('Remind me in 7 days'), null],
  [control('Not now', 'button', { aria: 'Not now, 5 € off' }), null],
  [control('', 'button', { aria: 'Dismiss' }), null],
  [control('No thanks', 'a'), 'decline'],
  [control('No thanks', 'a', { href: '#' }), 'decline'],
  [control('No thanks', 'a', { href: 'javascript:void(0)' }), 'decline'],
  [control('No thanks', 'a', { href: 'javascript: void 0;' }), 'decline'],
  [control('No thanks', 'a', { href: 'javascript:;' }), 'decline'],
  [control('Not now', 'a', { href: "javascript:location.assign('/checkout?plan=pro')" }), null],
  [control('', 'input', { type: 'button', value: 'Not now' }), 'decline'],
  [control('Not now', 'div', { role: 'button' }), 'decline'],
  // Never: a label that also says something it must not do.
  [control('Subscribe'), null],
  [control('Yes, sign me up'), null],
  [control('Sign up'), null],
  [control('Accept all'), null],
  [control('No thanks, subscribe me'), null],
  [control('OK'), null],
  [control('Continue'), null],
  [control('No thanks, continue'), null],
  [control('Continue without saving'), null],
  [control('Allow location'), null],
  [control('Install the app'), null],
  [control('Get 10% off'), null],
  [control('Try it free'), null],
  [control('×', 'button', { aria: 'Subscribe' }), null],
  [control('X', 'button', { aria: 'Share on X' }), null],
  [control('No thanks', 'button', { title: 'Delete my account' }), null],
  // Not a decline or a close button at all.
  [control('Close account'), null],
  [control('Cancel subscription'), null],
  [control('Learn more'), null],
  [control('Read our privacy policy'), null],
  // Not a control mendwright clicks.
  [control('No thanks', 'a', { href: '/no-thanks' }), null],
  [control('No thanks', 'a', { href: 'https://shop.example/' }), null],
  [control('No thanks', 'a', { role: 'button', href: '/no-thanks' }), null],
  [control('No thanks', 'button', { disabled: true }), null],
  [control('No thanks', 'input', { type: 'text' }), null],
  [control('No thanks', 'input', { type: 'checkbox' }), null],
  [control('No thanks', 'div'), null],
  [control('No thanks', 'button', { link: true }), null],
  [control('No thanks', 'span', { role: 'button', label: true }), null],
  [control('No thanks', 'label', { role: 'button' }), null],
  [control('No thanks', 'div', { role: 'button', field: true }), null],
  [control('No thanks', 'button', { submits: true }), null],
  // A radio, switch, tab, link or toggle: a click on it chooses or follows something.
  [control('Not interested', 'button', { role: 'radio' }), null],
  [control('Not now', 'button', { role: 'switch' }), null],
  [control('Maybe later', 'button', { role: 'link' }), null],
  [control('Not now', 'button', { role: 'tab' }), null],
  [control('Not now', 'button', { role: 'option' }), null],
  [control('Not now', 'div', { role: 'menuitemradio' }), null],
  [control('Not interested', 'button', { checked: true }), null],
  [control('', 'input', { type: 'submit', value: 'No thanks' }), 'decline'],
  // What it shows counts, not text it hides; hidden text still keeps it from a click.
  [control('Keep me updated', 'button', { all: 'No thanksKeep me updated' }), null],
  [control('No thanks', 'button', { all: 'Subscribe No thanks' }), null],
  // A word it must not say, written with a soft hyphen, a non-breaking hyphen, a ligature, a
  // zero-width space or fullwidth letters, or in what aria-labelledby names.
  [control('No thanks', 'button', { all: 'No thanks and sub\u00adscribe me' }), null],
  [control('Not now', 'button', { title: 'Sign‑up for weekly deals' }), null],
  [control('Not now', 'button', { title: 'Conﬁrm details' }), null],
  [control('Not now', 'button', { aria: 'Ac\u200bcept terms' }), null],
  [control('No thanks', 'button', { aria: 'Ｓｕｂｓｃｒｉｂｅ' }), null],
  [control('No thanks', 'button', { labelledby: 'Subscribe to weekly deals' }), null],
  [control('No thanks', 'button', { labelledby: 'Weekly deals' }), 'decline'],
  // Digits as Python's isdigit() reads them: superscripts and circled digits are digits, ½ is not.
  [control('Not now', 'button', { aria: 'Not now ²' }), null],
  [control('Not now', 'button', { aria: 'Not now ①' }), null],
  [control('Not now', 'button', { aria: 'Not now ٣' }), null],
  [control('Not now', 'button', { aria: 'Not now ½' }), 'decline'],
  [control('Not now', 'button', { aria: 'Not now ₹' }), null],
  [control('Not now', 'button', { aria: 'Not now £' }), null],
];

// Where a popup's text changes what a button means: [control, where, kind], where as
// { cookies, prose (read with words()), asks }.
type Where = { cookies?: boolean; prose?: string; asks?: boolean };
const WHERE: [Control, Where, string | null][] = [
  [control('Reject all'), { cookies: true }, 'decline'],
  [control('Reject'), { cookies: true }, 'decline'],
  [control('Decline'), { cookies: true }, 'decline'],
  [control('Decline offer'), { cookies: true }, null],
  [control('Close'), { prose: 'Close your account? This cannot be undone.' }, null],
  [control('Close'), { prose: 'Close position: 100 AAPL at market?' }, null],
  [control('×', 'button', { aria: 'Close' }), { prose: 'Closing this banner means you agree' }, null],
  [control('Skip'), { prose: "Skip next week's delivery? Free shipping on every box." }, null],
  [control('Dismiss'), { prose: 'Dismiss these tips for good?' }, null],
  [control('Close'), { asks: true }, null],
  [control('No thanks'), { asks: true }, null],
  [control('Skip'), { prose: 'Take a quick tour?', asks: true }, null],
  [control('Close'), { prose: 'Find your closest store' }, 'close'],
  [control('Skip'), { prose: 'Take a quick tour?' }, 'decline'],
  [control('No thanks'), { prose: 'Skip the queue with Plus' }, 'decline'],
  [control('Dismiss'), { prose: 'New sign-in detected. We noticed a sign-in from a new device.' }, null],
  [control('Skip'), { prose: 'Update your payment method' }, null],
  [control('Skip for now'), { prose: 'Update your payment method' }, null],
  [control('', 'button', { aria: 'Dismiss' }), { prose: 'Your password was changed' }, null],
  [control('Dismiss all'), { prose: 'Notifications: your password was changed' }, null],
  [control('Skip'), { prose: 'Sign up for our newsletter' }, 'decline'],
  [control('Skip for now'), { prose: 'Get 10% off your first order' }, 'decline'],
  [control('Dismiss'), { prose: 'Get the Acme app for faster checkout' }, 'decline'],
  [control('', 'button', { aria: 'Dismiss' }), { prose: "What's new in Acme 2" }, 'decline'],
  [control('Dismiss'), { cookies: true }, 'decline'],
  [control('No thanks'), { prose: 'Update your payment method' }, 'decline'],
  [control('Not now'), { prose: 'Update your payment method' }, 'decline'],
  [control('Maybe later'), { prose: 'Your password was changed' }, 'decline'],
  [control('Not interested'), { prose: 'Your password was changed' }, 'decline'],
  [control('Continue without an account'), { prose: 'Your order is on its way' }, 'decline'],
  [control('Close'), { prose: 'New sign-in detected' }, 'close'],
  [control('×', 'button', { aria: 'Close dialog' }), { prose: 'New sign-in detected' }, 'close'],
];

const PROMO: [string, boolean][] = [
  ['Sign up for our newsletter', true], ['Subscribe to weekly deals', true], ['Get 10% off your first order', true],
  ['Save 20 % off today', true], ['A special offer for you', true], ['Members get a discount', true],
  ['Summer sale ends Sunday', true], ['Use coupon SPRING at checkout', true], ['Enter a promo code', true],
  ['Free shipping on orders this week', true], ['Download our app', true], ['Get the Acme app', true],
  ['Install the free Acme app', true], ['Turn on notifications to hear about restocks', true],
  ['Take our survey', true], ['Tell us what you think: share your feedback', true], ['Enjoying Acme? Rate us', true],
  ['Take a quick tour', true], ['Tips for your first week', true], ["What's new in Acme 2", true],
  ['Welcome to Acme!', true], ['Getting started', true], ['Questions? Chat with us', true],
  ['We value your consent', true], ['This site uses cookies', true],
  ['New sign-in detected. We noticed a sign-in from a new device.', false], ['Update your payment method', false],
  ['Your subscription payment failed', false], ['Welcome back, Ada. A new sign-in from Berlin.', false],
  ['Notifications: your password was changed', false], ['You were promoted to admin', false],
  ['Your order is on its way', false], ['The app could not save your changes', false],
  ['Contact sales for an invoice', false], ['', false],
];

// Text that Python and JavaScript read differently unless the port takes care: Unicode
// digits, ligatures, format characters, dashes, currency signs and spaces, and case.
const TEXTS = ['x²', '٣', '①', '½', '⑩', 'Ⅷ', '𝟘', 'ﬁnd', 'sub\u00adscribe',
  'ac\u200bcept', '\ufeffok', 'Ｓｕｂ', '5−3', 'a–b—c', 'Sign‑up', '€5', '£5',
  '₹5', 'no\u00a0thanks', 'no\u1680thanks', 'no\u2007thanks', 'a\u001cb\u001fc', 'a\u0085b', 'a\u3000b',
  'KİLİM', 'Straße', 'ǄERP', 'ODOΣ Σ', 'Kelvin', 'ſkip', 'No, thanks!', "Don't",
  'Close×', 'x/y_z', '[ok]', 'café é', 'e\u0301', 'Ⅰ', 'a  \t b', '', 'Mixed CASE', 'emoji 😀 here'];

describe('the rules', () => {
  test('which buttons close or decline', () => {
    for (const [c, kind] of CONTROLS) assert.equal(classify(c), kind, JSON.stringify(c));
  });

  test('the words a button is never clicked for', () => {
    assert.equal(forbidden(control('Join now')), 'join');
    assert.equal(forbidden(control('No thanks', 'button', { aria: 'Notify me' })), 'notify');
    assert.equal(forbidden(control('Continue to checkout')), 'continue');
    assert.equal(forbidden(control('Continue without an account')), null);
    assert.equal(forbidden(control('Not interested')), null);
    assert.equal(isControl(control('', 'input', { type: 'submit' })), true);
    assert.equal(isControl(control('', 'input', { type: 'image' })), false);
    for (const word of [...NEVER.slice(0, 26), 'continue']) {
      assert.equal(forbidden(control(word + ' later')), word);
      assert.equal(classify(control('No thanks', 'button', { aria: word + ' me' })), null, word);
    }
    assert.equal(classify(control('No thanks', 'button', { aria: 'Me' })), 'decline');
    const stems: [string, string][] = [['Skip to payment', 'pay'], ['Paying later', 'pay'],
      ['Subscribed? Not now', 'subscribe'], ['No-risk trial', 'trial'], ['Not now, I am ordering', 'order'],
      ['Saving later', 'save'], ['No notifications', 'notify'], ['Continue without saving', 'continue'],
      ['Continue without delay', 'continue']];
    for (const [label, word] of stems) assert.equal(forbidden(control(label)), word, label);
  });

  test('what a popup says changes what its buttons mean', () => {
    for (const [c, where, kind] of WHERE) {
      const place = new Setting({ cookies: where.cookies, prose: words(where.prose ?? ''), asks: where.asks });
      assert.equal(classify(c, place), kind, JSON.stringify([c.text, c.aria, where]));
    }
  });

  test('what reads as a promotion', () => {
    for (const [prose, promo] of PROMO) {
      assert.equal(new Setting({ prose: words(prose) }).promo, promo, prose);
      assert.equal(setting(popupWith(prose, prose), []).promo, promo, prose);
    }
  });

  test('the setting is read from the popup and its buttons', () => {
    const banner = popupWith('We use cookies. Reject all Accept all', 'We use cookies.');
    const where = setting(banner, [control('Reject all'), control('Accept all')]);
    assert.deepEqual([where.cookies, where.prose, where.asks, where.promo], [true, 'we use cookies', false, true]);
    const notice = popupWith('New sign-in detected. Dismiss Review', 'New sign-in detected.');
    assert.deepEqual(pick([control('Dismiss'), control('Review')], notice), {});
    const app = popupWith('Get the Acme app. Install Dismiss', 'Get the Acme app.');
    assert.deepEqual(pick([control('Install'), control('Dismiss')], app), { decline: [1, control('Dismiss')] });
    assert.deepEqual(pick([control('Get the app'), control('Dismiss')], popupWith('Shop faster. Get the app Dismiss',
      'Shop faster.')), {});
    const box = popupWith('Your box ships Monday. Keep it Skip', 'Your box ships Monday.');
    assert.equal(setting(box, [control('Keep it'), control('Skip')]).asks, true);
    assert.deepEqual(pick([control('Keep it'), control('Skip')], box), {});
    assert.deepEqual(pick([control('Cancel subscription'), control('No thanks')], popupWith('Your Premium plan',
      'Your Premium plan')), {});
    // A driver that does not give the popup's own text: its whole text, labels and all.
    assert.ok(setting(popupWith('Close your account? Cancel Close'), []).prose.split(' ').includes('close'));
    assert.deepEqual(pick([control('Close')], popupWith('Rate us Close')), {});
    assert.deepEqual(pick([control('Close')], popupWith('Rate us Close', 'Rate us')), { close: [0, control('Close')] });
    const expense = popupWith('Expense report from Ada is waiting for you. Approve Reject',
      'Expense report from Ada is waiting for you.');
    assert.deepEqual(pick([control('Approve'), control('Reject')], expense), {});
    // A cart drawer: one × per item removes it, the drawer's own close button is named.
    const cart = [control('×'), control('×'), control('×', 'button', { aria: 'Close cart' })];
    assert.deepEqual(pick(cart, popupWith('Linen shirt × Canvas tote × ×', 'Linen shirt Canvas tote')),
      { close: [2, cart[2]] });
    assert.deepEqual(pick([control('Facebook'), control('X'), control('Email')],
      popupWith('Share this article Facebook X Email', 'Share this article')), {});
  });

  test('pick takes the first close and decline buttons in page order', () => {
    const controls: unknown[] = [control('Subscribe'), control('No thanks'), control('×', 'button', { aria: 'Close' }),
      control('Not now'), 'not a dict'];
    const chosen = pick(controls);
    assert.deepEqual(chosen.close, [2, controls[2]]);
    assert.deepEqual(chosen.decline, [1, controls[1]]);
    const loose = [control("No thanks, I'd rather not"), control('Not now')];
    assert.deepEqual(pick(loose).decline, [1, loose[1]]);
    assert.deepEqual(pick(loose.slice(0, 1)).decline, [0, loose[0]]);
    assert.deepEqual(pick([control('Subscribe'), control('Yes')]), {});
    assert.equal(controlText(control('×', 'button', { aria: 'Close' })), 'Close');
    assert.equal(controlText(control('No thanks')), 'No thanks');
    assert.equal(controlText(control('', 'input', { value: 'Not now' })), 'Not now');
  });

  test('a javascript: link goes nowhere only when its code does nothing', () => {
    for (const href of [null, undefined, '#', ' # ', 'javascript:', 'javascript:void(0)', 'JavaScript:void(0);',
      'javascript: void 0', 'javascript:;']) {
      assert.equal(goesNowhere(href), true, String(href));
    }
    for (const href of ['', '/no-thanks', '#/upgrade', 'javascript:location.assign("/checkout")',
      'javascript:closeModal()', 'https://shop.example/']) {
      assert.equal(goesNowhere(href), false, href);
    }
  });

  test('which element is the popup', () => {
    const chains: [Record<string, unknown>[], number | null][] = [
      [[el(), el({ role: 'dialog', id: 'nl' })], 1],
      [[el({ modal: true })], 0],
      [[el({ open: true }, 'dialog')], 0],
      [[el({ role: 'alertdialog', area: 0.01 })], 0],
      [[el({ pos: 'fixed', area: 0.3 })], 0],
      [[el({ pos: 'fixed', area: 0.1 }), el({ pos: 'fixed', area: 0.5 })], 1],
      [[el({ pos: 'sticky', area: 1.0 })], 0],
      [[el({ pos: 'fixed', area: 1.0 }), el({ pos: 'fixed', area: 1.0, holds: true })], 0],
      [[el({ pos: 'fixed', area: 0.25 })], 0],
      [[el({ pos: 'fixed', area: 0.3 }), el({ pos: 'fixed', area: 1.0, events: false })], 0],
      [[el({ pos: 'fixed', area: 0.08 })], null],
      [[el({ pos: 'fixed', area: 0.249 })], null],
      [[el(), el({ pos: 'fixed', area: 1.0, events: false })], null],
      [[el({ pos: 'fixed', area: 0.5 }), el({ pos: 'fixed', area: 0.1 })], null],
      [[el(), el({ pos: 'absolute', area: 0.9 })], null],
      [[el(), el({ role: 'dialog', holds: true })], null],
      [[el({ pos: 'fixed', area: 1.0, holds: true })], null],
    ];
    for (const [chain, depth] of chains) {
      const popup = popupOf(parseProbe({ covered: true, chain }));
      assert.equal(popup ? popup.depth : null, depth, JSON.stringify(chain));
    }
    assert.equal(MIN_AREA, 0.25);
  });

  test('an alert dialog asks for an answer', () => {
    const alert = popupOf(parseProbe({ covered: true, chain: [el({ role: 'alertdialog' })] }))!;
    assert.equal(alert.alert, true);
    assert.equal(asksAnswer(alert), true);
    const inner = popupOf(parseProbe({ covered: true, chain: [el({ role: 'dialog', id: 'inner' }),
      el({ role: 'alertdialog', id: 'outer' })] }))!;
    assert.equal(inner.element.id, 'inner');
    assert.equal(asksAnswer(inner), true);
    const plain = popupOf(parseProbe({ covered: true, chain: [el({ role: 'dialog' })] }))!;
    assert.equal(asksAnswer(plain), false);
  });

  test('only a probe mendwright knows counts', () => {
    assert.equal(parseProbe(null), null);
    assert.equal(parseProbe([]), null);
    assert.equal(parseProbe({ tag: 'input', html: '<input>' }), null);
    assert.equal(parseProbe({ covered: true, chain: [{ tag: 'div' }] }), null);
    assert.deepEqual(parseProbe({ covered: false, gone: true }), { covered: false, chain: [], gone: true, why: '' });
    assert.deepEqual(parseProbe({ covered: false, why: 'hidden' }), { covered: false, chain: [], gone: null, why: 'hidden' });
    assert.equal(parseProbe({ covered: false, why: 'what' })!.why, '');
    assert.equal(popupOf(parseProbe({ covered: false })), null);
  });

  test('what looks like a consent banner, and what asks for consent', () => {
    const elements: [Record<string, unknown>, boolean][] = [
      [el({ role: 'dialog', text: 'We and our partners use cookies to measure traffic.' }), true],
      [el({ role: 'dialog', aria: 'Privacy choices', text: 'Your choices' }), true],
      [el({ id: 'cookie-law-info-bar', text: 'Choose' }), true],
      [el({ cls: 'consent-modal', text: 'Choose' }), true],
      [el({ role: 'dialog', text: 'Get 10% off your first order. We respect your privacy.' }), false],
      [el({ id: 'sp_message_container_1234', cls: 'message-container', text: 'We value your privacy. We and our '
        + 'partners store and/or access information on a device' }), true],
      [el({ id: 'dsgvo-layer', text: 'Wir speichern Informationen' }), true],
      [el({ cls: 'cmp-layer', text: 'Choose' }), true],
      [el({ id: 'rgpd-banner', text: 'Nous utilisons des traceurs' }), true],
      [el({ text: 'Nous et nos partenaires utilisons des traceurs pour mesurer' }), true],
      [el({ text: 'Wir und unsere Partner: Datenschutzeinstellungen' }), true],
      [el({ text: 'We process personal data for personalised ads' }), true],
      // Python's re.IGNORECASE reads the Turkish İ and ı, the long ſ and the Kelvin sign as i, s and k.
      [el({ text: 'COOKİE settings' }), true],
      [el({ text: 'Your prıvacy' }), false],
      [el({ text: 'CooKie notice' }), true],
      [el({ text: 'DONNÉES PERSONNELLES' }), true],
    ];
    for (const [element, consent] of elements) {
      assert.equal(isConsent({ depth: 0, element, summary: summary(element), alert: false }), consent,
        JSON.stringify(element));
    }
    const labels: [string[], boolean][] = [[['Accept'], true], [['Alle akzeptieren'], true],
      [['Accepter et fermer'], true], [['I agree'], true], [['Allow all'], true], [['Aceptar todo'], true],
      [['Zustimmen'], true], [['Tout accepter'], true], [['Continuer sans accepter'], true],
      [['No thanks', 'Subscribe'], false], [['Close'], false], [[], false]];
    for (const [names, consent] of labels) assert.equal(asksConsent(names.map((n) => control(n))), consent, names.join());
  });

  test('how a popup and a control are named', () => {
    assert.equal(summary(el({ role: 'dialog', cls: 'nl  modal big shadow', text: 'Get 10% off' })),
      '<div role="dialog" class="nl modal big"> ("Get 10% off")');
    assert.equal(summary(el({ id: 'promo', text: 'Your card 4111111111111111 ' + 'x'.repeat(80) })),
      '<div id="promo"> ("Your card [redacted number] ' + 'x'.repeat(32) + '…")');
    assert.equal(summary(el({ pos: 'fixed' }, 'aside')), '<aside>');
    assert.deepEqual(expectOf(popupWith('x')), { depth: 0, tag: 'div', id: '' });
    const c = control('No thanks');
    const expected = expectControl(3, c);
    assert.deepEqual(expected, { index: 3, facts: c });
    assert.notEqual(expected.facts, c);
  });

  test('a click that may have reached the page', () => {
    assert.equal(clickMayHaveRun(new Error('Timeout 1000ms exceeded.\n  - click action done\n  - waiting for scheduled '
      + 'navigations to finish')), true);
    assert.equal(clickMayHaveRun(new Error('Execution context was destroyed, most likely because of a navigation')), true);
    assert.equal(clickMayHaveRun(new Error('Target page, context or browser has been closed')), true);
    assert.equal(clickMayHaveRun(new Error('Timeout 1000ms exceeded.\n  - <div id="nl"> intercepts pointer events')), false);
    assert.equal(clickMayHaveRun(new Error('Timeout 1000ms exceeded.\n  - element is not visible')), false);
  });
});

// --- the same answers as the Python package ------------------------------------------------------

describe('parity with the Python package', { skip }, () => {
  const PY_HEAD = `
import json, sys
from mendwright.dismiss import *
from mendwright.dismiss import _scrub
data = json.load(sys.stdin)
`;

  test('classify, forbidden, priced, isControl and controlText on every control', () => {
    const controls = [...CONTROLS.map(([c]) => c), ...TEXTS.map((t) => control(t)),
      ...TEXTS.map((t) => control('No thanks', 'button', { aria: t })), ...TEXTS.map((t) => control('', 'input',
        { type: 'button', value: t }))];
    const py = runPython<{ classify: unknown[]; forbidden: unknown[]; priced: unknown[]; control: unknown[];
      text: unknown[] }>(PY_HEAD + `
print(json.dumps({'classify': [classify(c) for c in data], 'forbidden': [forbidden(c) for c in data],
                  'priced': [priced(c) for c in data], 'control': [is_control(c) for c in data],
                  'text': [control_text(c) for c in data]}))`, controls);
    assert.deepEqual(controls.map((c) => classify(c)), py.classify);
    assert.deepEqual(controls.map(forbidden), py.forbidden);
    assert.deepEqual(controls.map(priced), py.priced);
    assert.deepEqual(controls.map(isControl), py.control);
    assert.deepEqual(controls.map(controlText), py.text);
  });

  test('words() and the text rules on Unicode', () => {
    const py = runPython<{ words: string[]; priced: boolean[] }>(PY_HEAD + `
print(json.dumps({'words': [words(t) for t in data], 'priced': [priced({'text': t}) for t in data]}))`, TEXTS);
    assert.deepEqual(TEXTS.map(words), py.words);
    assert.deepEqual(TEXTS.map((t) => priced({ text: t })), py.priced);
  });

  test('classify in a popup that says something, setting, pick and promo', () => {
    const cases = WHERE.map(([c, where]) => ({ control: c, where }));
    const popups = [...PROMO.map(([prose]) => [prose, prose]), ['We use cookies. Reject all Accept all', 'We use cookies.'],
      ['Close your account? Cancel Close', null], ['Rate us Close', null], ['Rate us Close', 'Rate us'],
      ['Get the Acme app. Install Dismiss', 'Get the Acme app.'], ['Your box ships Monday. Keep it Skip', null],
      ['COOKIES Kept', 'x'], ['', ''], ['Privacy', 'Continue without cookies']];
    const sets = [[control('Reject all'), control('Accept all')], [control('Close'), control('Cancel')],
      [control('Dismiss'), control('Review')], [control('Install'), control('Dismiss')],
      [control('Keep it'), control('Skip')], [control("No thanks, I'd rather not"), control('Not now')],
      [control('×'), control('×'), control('×', 'button', { aria: 'Close cart' })], [], ['x', control('Skip for now')]];
    const py = runPython<{ classify: unknown[]; setting: unknown[]; pick: unknown[] }>(PY_HEAD + `
def where(w):
    return Setting(cookies=w.get('cookies', False), prose=words(w.get('prose', '')), asks=w.get('asks', False))
def popup(text, prose):
    element = {'tag': 'div', 'id': '', 'cls': '', 'role': 'dialog', 'modal': False, 'open': False, 'pos': 'static',
               'area': 0.0, 'holds': False, 'text': text}
    if prose is not None:
        element['prose'] = prose
    return Popup(0, element, summary(element))
out = {'classify': [classify(c['control'], where(c['where'])) for c in data['cases']], 'setting': [], 'pick': []}
for text, prose in data['popups']:
    for controls in data['sets']:
        s = setting(popup(text, prose), controls)
        out['setting'].append([s.cookies, s.prose, s.asks, s.promo])
        chosen = pick(controls, popup(text, prose))
        out['pick'].append({k: v[0] for k, v in chosen.items()})
print(json.dumps(out))`, { cases, popups, sets });
    assert.deepEqual(cases.map(({ control: c, where }) => classify(c, new Setting({ cookies: where.cookies,
      prose: words(where.prose ?? ''), asks: where.asks }))), py.classify);
    const settings: unknown[] = [];
    const picks: unknown[] = [];
    for (const [text, prose] of popups) {
      for (const controls of sets) {
        const s = setting(popupWith(text as string, prose ?? undefined), controls);
        settings.push([s.cookies, s.prose, s.asks, s.promo]);
        const chosen = pick(controls, popupWith(text as string, prose ?? undefined));
        picks.push(Object.fromEntries(Object.entries(chosen).map(([k, v]) => [k, v[0]])));
      }
    }
    assert.deepEqual(settings, py.setting);
    assert.deepEqual(picks, py.pick);
  });

  test('popupOf, asksAnswer, isConsent and summary over chains', () => {
    const chains = [
      [el(), el({ role: 'dialog', id: 'nl' })], [el({ modal: true })], [el({ open: true }, 'dialog')],
      [el({ role: 'alertdialog', area: 0.01 })], [el({ pos: 'fixed', area: 0.3 })],
      [el({ pos: 'fixed', area: 0.1 }), el({ pos: 'fixed', area: 0.5 })], [el({ pos: 'sticky', area: 1.0 })],
      [el({ pos: 'fixed', area: 1.0 }), el({ pos: 'fixed', area: 1.0, holds: true })], [el({ pos: 'fixed', area: 0.25 })],
      [el({ pos: 'fixed', area: 0.3 }), el({ pos: 'fixed', area: 1.0, events: false })], [el({ pos: 'fixed', area: 0.08 })],
      [el({ pos: 'fixed', area: 0.249 })], [el(), el({ pos: 'fixed', area: 1.0, events: false })],
      [el({ pos: 'fixed', area: 0.5 }), el({ pos: 'fixed', area: 0.1 })], [el(), el({ pos: 'absolute', area: 0.9 })],
      [el(), el({ role: 'dialog', holds: true })], [el({ pos: 'fixed', area: 1.0, holds: true })],
      [el({ role: 'dialog', id: 'inner' }), el({ role: 'alertdialog', id: 'outer' })],
      [el({ pos: 'fixed', area: '0.3' })], [el({ pos: 'fixed', area: ' 0.5 ' })], [el({ pos: 'fixed', area: 'x' })],
      [el({ pos: 'fixed', area: true })], [el({ pos: 'fixed', area: null })], [el({ pos: 'fixed', area: 1, holds: 0 })],
      [el({ pos: 'fixed', area: 1, holds: 'yes' })], [el({ pos: 'fixed', area: 1, events: 0 })],
      [el({ role: 'dialog', id: 'promo', cls: '  a   b c d ', text: 'Your card 4111111111111111 ' + 'x'.repeat(80) })],
      [el({ role: 'dialog', id: 'q"uote', text: 'We and our partners use cookies' })],
      [el({ tag: 'section', aria: 'Privacy settings', text: 'Choose 😀'.repeat(20) })],
      [el({ role: 'dialog', text: 'x'.repeat(59) + '   y' })], [el({ role: 'dialog', text: '\u3000\u0085 a\u001cb ' })],
    ];
    const raws: unknown[] = [...chains.map((chain) => ({ covered: true, chain })), null, [], { covered: 1 },
      { covered: false, why: 'free', gone: false }, { covered: false, why: 'outside' }, { covered: true, chain: [] },
      { covered: true, chain: [{ tag: 'div' }] }, { covered: false, gone: 'yes', why: 'hidden' }];
    const py = runPython<unknown[]>(PY_HEAD + `
out = []
for raw in data:
    probe = parse_probe(raw)
    popup = popup_of(probe)
    out.append({'probe': None if probe is None else [probe.covered, probe.gone, probe.why, len(probe.chain)],
                'popup': None if popup is None else [popup.depth, popup.summary, popup.alert, asks_answer(popup),
                                                     is_consent(popup), expect_of(popup)]})
print(json.dumps(out))`, raws);
    const ts = raws.map((raw) => {
      const probe = parseProbe(raw);
      const popup = popupOf(probe);
      return {
        probe: probe === null ? null : [probe.covered, probe.gone, probe.why, probe.chain.length],
        popup: popup === null ? null : [popup.depth, popup.summary, popup.alert, asksAnswer(popup), isConsent(popup),
          expectOf(popup)],
      };
    });
    assert.deepEqual(ts, py);
  });

  test('isConsent, asksConsent and goesNowhere', () => {
    const texts = ['We and our partners use cookies', 'COOKİE settings', 'Your prıvacy', 'CooKie',
      'DONNÉES PERSONNELLES', 'Datos personales', 'Einwilligung', 'nothing here', 'Privacy Center',
      'PRIVACY  settings', 'confidentialité', 'ſp_message'];
    const names = ['cookie-bar', 'OneTrust-Banner', 'tcf-x', 'x', 'GDPR', 'didomi-host', 'xcmpx', 'sp_MESSAGE',
      'prİvacy', 'CooKie'];
    const labels = [['Accept'], ['ACCEPTER'], ['hyväksy'], ['HYVÄKSY'], ['Permitir'], ['Godta alle'],
      ['Akceptuj'], ['No thanks'], ['accèpt'], ['Keep'], ['agree\u00ad'], ['Al\u200blow']];
    const hrefs = [null, '#', ' # ', '\u3000#\u3000', 'javascript:', 'JAVASCRIPT:VOID(0)', 'javascript:void(0);;',
      'javascript:\u3000void(0)', 'javascript:\u001cvoid(0)', 'javascript:void(null)', 'javascript:void 0 ; ',
      'javascript:x()', '', '/a', '#/x', 'javascript:void(1)'];
    const py = runPython<{ text: boolean[]; name: boolean[]; asks: boolean[]; nowhere: boolean[] }>(PY_HEAD + `
mk = lambda **kw: Popup(0, kw, '')
print(json.dumps({'text': [is_consent(mk(text=t)) for t in data['texts']],
                  'name': [is_consent(mk(id=n, text='')) for n in data['names']] ,
                  'asks': [asks_consent([{'text': l} for l in ls]) for ls in data['labels']],
                  'nowhere': [goes_nowhere(h) for h in data['hrefs']]}))`, { texts, names, labels, hrefs });
    const mk = (element: Record<string, unknown>) => ({ depth: 0, element, summary: '', alert: false });
    assert.deepEqual(texts.map((t) => isConsent(mk({ text: t }))), py.text);
    assert.deepEqual(names.map((n) => isConsent(mk({ id: n, text: '' }))), py.name);
    assert.deepEqual(labels.map((ls) => asksConsent(ls.map((l) => ({ text: l })))), py.asks);
    assert.deepEqual(hrefs.map(goesNowhere), py.nowhere);
  });

  test('the texts mendwright logs and raises, in TypeScript idiom', () => {
    const popup = '<div id="nl" role="dialog" class="nl"> ("Get 10% off")';
    const cases = {
      dismissed: [['go', popup, '“No thanks”', []], ['go', popup, 'Escape', []], ["it's", popup, '“Close”',
        ['Escape', '“No thanks”', 'an unlabelled button']]],
      undismissed: [['go', popup, [], true, false, false], ['go', popup, ['Escape'], true, false, false],
        ['go', popup, ['Escape', '“Close”'], true, false, false], ['go', popup, [], false, false, false],
        ['go', popup, [], false, false, true], ['go', popup, ['“Close”'], false, false, true],
        ['go', popup, [], true, true, false], ['go', popup, ['Escape'], false, true, true]],
      lost: [['go', popup, 'pressed Escape', false], ['go', popup, 'clicked “No thanks”', true]],
      consent: [['go', popup]], alert: [['go', popup]],
      loop: [['go', popup, 3, true], ['go', popup, 3, false]],
      step: [['escape', null], ['close', 'Close'], ['decline', ''], ['decline', null]],
    };
    const py = runPython<Record<string, string[]>>(`
import json, sys
from mendwright.flow import *
d = json.load(sys.stdin)
print(json.dumps({
  'dismissed': [dismissed_message(*a) for a in d['dismissed']],
  'undismissed': [undismissed_message(b, p, t, escaped=e, changed=c, consent=k) for b, p, t, e, c, k in d['undismissed']],
  'lost': [lost_message(b, p, did, raised=r) for b, p, did, r in d['lost']],
  'consent': [consent_message(*a) for a in d['consent']], 'alert': [alert_message(*a) for a in d['alert']],
  'loop': [dismiss_loop_message(*a) for a in d['loop']], 'step': [step_text(*a) for a in d['step']]}))`, cases);
    // The only differences: the option spelling of TypeScript.
    const idiom = (text: string) => text.split('cookies="accept"').join("cookies: 'accept'")
      .split('action="fill", args=("<value>",)').join("{ action: 'fill', args: ['<value>'] }");
    const ts = {
      dismissed: cases.dismissed.map(([b, p, h, t]) => dismissedMessage(b as string, p as string, h as string, t as string[])),
      undismissed: cases.undismissed.map(([b, p, t, escaped, changed, consent]) => undismissedMessage(b as string,
        p as string, t as string[], { escaped: escaped as boolean, changed: changed as boolean, consent: consent as boolean })),
      lost: cases.lost.map(([b, p, did, raised]) => lostMessage(b as string, p as string, did as string, raised as boolean)),
      consent: cases.consent.map(([b, p]) => consentMessage(b, p)),
      alert: cases.alert.map(([b, p]) => alertMessage(b, p)),
      loop: cases.loop.map(([b, p, n, again]) => dismissLoopMessage(b as string, p as string, n as number, again as boolean)),
      step: cases.step.map(([how, label]) => stepText(how as string, label)),
    };
    for (const key of Object.keys(ts) as (keyof typeof ts)[]) assert.deepEqual(ts[key], py[key].map(idiom), key);
    assert.ok(ts.undismissed.some((t) => t.includes("{ action: 'fill', args: ['<value>'] }")));
    assert.ok(ts.consent[0].includes("cookies: 'accept'"));
  });

  test("the page scripts are the Python package's, character for character", () => {
    const names = ['POPUP_PROBE_JS', 'POPUP_ROOT_JS', 'POPUP_CONTROLS_JS', 'POPUP_CONTROL_JS', 'POPUP_FOCUS_JS'] as const;
    const py = runPython<Record<string, string>>(`
import json
from mendwright.drivers import base
print(json.dumps({n: getattr(base, n) for n in ${JSON.stringify(names)}}))`);
    for (const name of names) assert.equal(popupScripts[name], py[name], name);
  });

  test('a flow.dismiss line is the one the Python Journal writes for the same fields', () => {
    const dir = tempDir();
    const tried = [{ how: 'escape' }, { how: 'close', label: 'Close' }, { how: 'decline', label: '' }];
    const events = [
      { before: 'go', popup: '<div id="nl"> ("Get 10% off…")', outcome: 'dismissed', how: 'decline', label: 'No thanks',
        tried },
      { before: 'go', popup: '<div>', outcome: 'dismissed', how: 'escape', label: null, tried: [] },
      { before: "page.getByRole('button', { name: 'Go' })", popup: '<aside>', outcome: 'failed', how: null, label: null,
        tried: [{ how: 'escape' }] },
      { before: 'go', popup: '<div>', outcome: 'lost', how: null, label: null, tried },
    ];
    const file = path.join(dir, 'ts.jsonl');
    const journal = new Journal(file, { clock: () => 'T', run: null });
    for (const e of events) journal.event('flow.dismiss', { key: '<popup>', ...e });
    journal.event('flow.stop', { key: '<popup>', code: 'popup_loop', before: 'go' });
    const py = runPython<string[]>(`
import json, sys
from mendwright.flow import DISMISS_KEY, journal_event
from mendwright.journal import Journal
d = json.load(sys.stdin)
j = Journal(d['file'], clock=lambda: 'T', run=None)
for e in d['events']:
    journal_event(j, 'flow.dismiss', DISMISS_KEY, **e)
journal_event(j, 'flow.stop', DISMISS_KEY, code='popup_loop', before='go')
print(json.dumps(open(d['file'], encoding='utf-8').read().splitlines()))`, { file: path.join(dir, 'py.jsonl'), events });
    const ts = fs.readFileSync(file, 'utf-8').split('\n').filter(Boolean);
    assert.equal(ts.length, py.length);
    ts.forEach((line, i) => {
      const [a, b] = [JSON.parse(line), JSON.parse(py[i])];
      assert.deepEqual(a, b);
      assert.deepEqual(Object.keys(a), Object.keys(b));   // the same fields, in the same order
    });
    assert.equal(JSON.parse(ts[1]).label, undefined);   // null is left out, as Python leaves None out
  });
});
