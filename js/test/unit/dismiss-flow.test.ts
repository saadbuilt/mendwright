// open({ dismiss: true }) on a fake page with a layout (see support/fakepage.ts): the page
// scripts the browser runs (browser/popup.ts) run here too, on boxes from data-rect, so what
// they say about a page decides, as in a browser. Each safety rule has a test here that fails
// without it.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import { beforeEach, describe, test } from 'node:test';

import type { Page } from '@playwright/test';

import { DISMISS_KEY, alertMessage, consentMessage, dismissLoopMessage, dismissedMessage, lostMessage,
  undismissedMessage } from '../../src/flow';
import { expectControl, expectOf, parseProbe, popupOf } from '../../src/dismiss';
import { InterruptionLoop, AttentionRequired } from '../../src/errors';
import { stepOf, type Healer } from '../../src/healer';
import { cleanEnv, journalOf, world, type World } from '../support/env';
import { FakeLocator } from '../support/fakepage';

beforeEach(cleanEnv);

const FORM = '<form id="signup" data-rect="0 0 1000 200">'
  + '<label for="email" data-rect="10 10 100 30">Email</label><input id="email" name="email" data-rect="120 10 300 30">'
  + '<button id="go" type="button" data-rect="10 60 100 30">Go</button></form>';

/** A newsletter modal over the whole window: a dialog with a text, an email field and the
 * given buttons. Escape closes it only with escape. */
function modal(buttons: string, options: { escape?: boolean; extra?: string } = {}): string {
  return '<div id="nl" class="nl" role="dialog" aria-modal="true" style="position: fixed" data-rect="0 0 1000 800" '
    + 'data-z="10"' + (options.escape ? ' data-escape' : '') + '><div class="panel" data-rect="300 200 400 300">'
    + '<p>Get 10% off your first order.</p><input id="nl-email" type="email" placeholder="Your email"> '
    + buttons + (options.extra ?? '') + '</div></div>';
}

const SIGN_UP = '<button id="nl-signup" type="button">Sign up</button> ';
const NO_THANKS = '<button id="nl-no" type="button" data-click-removes="#nl">No thanks</button> ';
const nl = (text = 'Get 10% off your first order. Sign up No thanks') =>
  '<div id="nl" role="dialog" class="nl"> ("' + text + '")';

/** A dialog over the form with its own text and the given buttons. Escape does not close it. */
function notice(text: string, buttons: string, role = 'dialog'): string {
  return '<div id="cf" role="' + role + '" aria-modal="true" style="position: fixed" data-rect="0 0 1000 800" '
    + 'data-z="10"><p>' + text + '</p>' + buttons + '</div>';
}

/** A page with the form and the popups, every element inside a box given one of its own when
 * it has none (buttons in a row, inside the window), and a healer on it with dismiss: true. */
function page(...popups: string[]): World {
  const w = world('<html><body>' + FORM + popups.join('') + '</body></html>', { layout: true });
  layout(w);
  return w;
}

function layout(w: World): void {
  let i = 0;
  for (const el of Array.from(w.page.document.body.querySelectorAll('*'))) {
    if (el.hasAttribute('data-rect') || !el.parentElement?.closest('[data-rect]')) continue;
    el.setAttribute('data-rect', (310 + (i % 5) * 75) + ' ' + (300 + Math.floor(i / 5) * 35) + ' 70 30');
    i += 1;
  }
}

function add(w: World, html: string): void {
  w.page.document.body.insertAdjacentHTML('beforeend', html);
  layout(w);
}

// The WARNING lines, but the one open() logs once without a model provider.
const warnings = (w: World) => w.lines.filter((l) => l.startsWith('warning: ') && !l.startsWith('warning: open(): '))
  .map((l) => l.slice('warning: '.length));
const clicked = (w: World) => w.page.clicked.map((el) => el.id || el.localName);
const escapes = (w: World) => w.page.keys.filter(([key]) => key === 'Escape').length;
const gone = (w: World, id: string) => w.page.document.getElementById(id) === null;

/** The flow.* events, without their time. */
function events(w: World): Record<string, unknown>[] {
  return journalOf(w.dir).filter((e) => String(e.event).startsWith('flow.')).map(({ ts: _ts, ...rest }) => rest);
}

/** Counts the calls the healer makes to its driver's popup methods. */
function spy(heal: Healer): Record<string, number> {
  const counts: Record<string, number> = {};
  const driver = heal.driver as unknown as Record<string, (...a: unknown[]) => unknown>;
  for (const name of ['popupProbe', 'popupRoot', 'popupControls', 'popupClick', 'pressEscape']) {
    const real = driver[name].bind(driver);
    counts[name] = 0;
    driver[name] = (...args: unknown[]) => {
      counts[name] += 1;
      return real(...args);
    };
  }
  return counts;
}

const intercepted = /intercepts pointer events/;

describe('off by default', () => {
  test('dismiss is off by default and costs no browser call', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open();
    const calls = spy(heal);
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    await heal.locator('email', '#email').fill('ada@example.com');   // Playwright fills under it
    assert.deepEqual(Object.values(calls), [0, 0, 0, 0, 0]);
    assert.equal(gone(w, 'nl'), false);
    assert.deepEqual(clicked(w), []);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w), []);
    assert.ok(!warnings(w).some((l) => l.includes('popup')));
  });

  test('heal.page hands back the real locators', () => {
    const p = page().open().page as unknown as Record<string, any>;
    for (const loc of [p.getByTestId('go'), p.locator('#go', { hasText: 'Go' }), p.locator('#signup').locator('#go'),
      p.locator('#go').first(), p.locator('#go').nth(0)]) {
      assert.ok(loc instanceof FakeLocator);
      assert.equal(stepOf(loc), null);
    }
  });

  test('dismiss takes true or false', () => {
    const w = page();
    assert.throws(() => w.open({ dismiss: 'no' as never }),
      (e: unknown) => e instanceof TypeError && (e as Error).message
        === "mendwright.open() takes dismiss: true or dismiss: false, got dismiss: 'no'.");
    assert.throws(() => w.open({ dismiss: 1 as never }), /got dismiss: 1\./);
    assert.throws(() => w.open({ dismiss: null as never }), /got dismiss: null\./);
    assert.equal(w.open({ dismiss: false }).dismissOn, false);
  });
});

describe('dismissing', () => {
  test('a newsletter modal is declined with No thanks, never Sign up', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.ok(gone(w, 'nl'));
    assert.deepEqual(clicked(w), ['nl-no', 'go']);   // never Sign up
    assert.equal(escapes(w), 1);
    assert.deepEqual(warnings(w), [dismissedMessage('go', nl(), '“No thanks”', ['Escape'])]);
    assert.equal(warnings(w)[0], "dismissed a popup in the way of 'go': <div id=\"nl\" role=\"dialog\" class=\"nl\"> "
      + '("Get 10% off your first order. Sign up No thanks") with “No thanks”, after Escape did not close it');
    assert.deepEqual(heal.handledInterruptions, [
      { key: DISMISS_KEY, before: 'go', popup: nl(), how: 'decline', label: 'No thanks' }]);
    assert.deepEqual(events(w), [{ event: 'flow.dismiss', key: '<popup>', before: 'go', popup: nl(),
      outcome: 'dismissed', how: 'decline', label: 'No thanks', tried: [{ how: 'escape' }] }]);
    assert.deepEqual(heal.heals, []);
    assert.deepEqual(heal.pending(), {});
  });

  test('a fill under the modal dismisses it first', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    await heal.locator('email', '#email').fill('ada@example.com');
    assert.equal((w.page.document.getElementById('email') as HTMLInputElement).value, 'ada@example.com');
    assert.ok(gone(w, 'nl'));
    assert.deepEqual(heal.handledInterruptions.map((e) => e.before), ['email']);
  });

  test('a modal that closes on Escape is dismissed by Escape alone', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS, { escape: true }));
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['go']);
    assert.deepEqual(warnings(w), ["dismissed a popup in the way of 'go': " + nl() + ' with Escape']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['escape', null]]);
    assert.deepEqual(events(w).map((e) => [e.how, e.label, e.tried]), [['escape', undefined, []]]);
  });

  test('an icon close button is clicked before a decline button', async () => {
    const close = '<button id="nl-x" type="button" aria-label="Close" data-click-removes="#nl">×</button>';
    const w = page(modal(SIGN_UP + NO_THANKS + close));
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['nl-x', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['close', 'Close']]);
    assert.deepEqual(events(w).map((e) => [e.how, e.label]), [['close', 'Close']]);
  });

  test('a close button that does not close it is followed by No thanks', async () => {
    const stuck = '<button id="nl-x" type="button" aria-label="Close">×</button> ';
    const w = page(modal(SIGN_UP + stuck + NO_THANKS));
    w.open({ dismiss: true });
    await w.open({ dismiss: true }).locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['nl-x', 'nl-no', 'go']);
    const text = 'Get 10% off your first order. Sign up × No thanks';
    assert.deepEqual(warnings(w), [dismissedMessage('go', nl(text), '“No thanks”', ['Escape', '“Close”'])]);
    assert.deepEqual(events(w).map((e) => e.tried), [[{ how: 'escape' }, { how: 'close', label: 'Close' }]]);
  });

  test('a modal with only buttons it must not click is left alone, and the action fails as before', async () => {
    const yes = '<button id="nl-yes" type="button">Yes, sign me up</button>';
    const w = page(modal('<button id="nl-sub" type="button">Subscribe</button> ' + yes));
    const heal = w.open({ dismiss: true, cookies: 'reject' });
    await assert.rejects(heal.locator('go', '#go').click(), (e: unknown) => intercepted.test(String(e))
      && !(e instanceof AttentionRequired));
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 1);
    assert.equal(gone(w, 'nl'), false);
    assert.deepEqual(heal.handledInterruptions, []);
    const popup = nl('Get 10% off your first order. Subscribe Yes, sign me up');
    // One WARNING, and not the one that says no interruption matched: the popup was found.
    assert.deepEqual(warnings(w), [undismissedMessage('go', popup, ['Escape'])]);
    assert.ok(warnings(w)[0].includes('mendwright pressed Escape, which did not close it, and it has no close or '
      + 'decline button that mendwright may click'));
    assert.ok(warnings(w)[0].includes("heal.interrupt(\"<name>_field\", \"<selector of the field>\", { action: 'fill', "
      + "args: ['<value>'] })"));
    assert.deepEqual(events(w).map((e) => [e.outcome, e.tried]), [['failed', [{ how: 'escape' }]]]);
  });

  test('an alert dialog is left alone, without Escape or a click', async () => {
    const alert = notice('Close your account? This cannot be undone.', '<button id="cancel" type="button" '
      + 'data-click-removes="#cf">Cancel</button> <button id="close" type="button" data-click-removes="#cf">Close'
      + '</button> <button id="x" type="button" aria-label="Close" data-click-removes="#cf">×</button>', 'alertdialog')
      .replace('data-z="10"', 'data-z="10" data-escape');
    const w = page(alert);
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
    assert.equal(gone(w, 'cf'), false);
    const popup = '<div id="cf" role="alertdialog"> ("Close your account? This cannot be undone. Cancel Close ×")';
    assert.deepEqual(warnings(w), [alertMessage('go', popup)]);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w), []);
  });

  test('a dialog inside an alert dialog is left alone too', async () => {
    const inner = '<div id="outer" role="alertdialog" style="position: fixed" data-rect="0 0 1000 800" data-z="10">'
      + '<div id="inner" role="dialog" data-rect="0 0 1000 800"><p>Delete it?</p><button id="nope" type="button" '
      + 'data-click-removes="#outer">No thanks</button></div></div>';
    const w = page(inner);
    await assert.rejects(w.open({ dismiss: true }).locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
  });

  const ASKING: [string, string][] = [
    // A question whose verb is the button's: the button is the answer.
    ['Close your account? This cannot be undone.', '<button id="close" type="button" data-click-removes="#cf">Close'
      + '</button>'],
    ["Skip next week's delivery? Free shipping on every box.", '<button id="skip" type="button" '
      + 'data-click-removes="#cf">Skip</button>'],
    // A Cancel or Keep button: the popup asks, and no other button of it is clicked.
    ['Delete this project? This cannot be undone.', '<button id="cancel" type="button" data-click-removes="#cf">Cancel'
      + '</button> <button id="x" type="button" aria-label="Close" data-click-removes="#cf">×</button>'],
    ['Your box ships Monday, with free shipping.', '<button id="keep" type="button" data-click-removes="#cf">Keep it'
      + '</button> <button id="skip" type="button" data-click-removes="#cf">Skip</button>'],
    // Reject and decline outside a popup about cookies answer a request.
    ['Expense report from Ada is waiting for your approval.', '<button id="approve" type="button">Approve</button> '
      + '<button id="reject" type="button" data-click-removes="#cf">Reject</button>'],
    ['Ada invited you to join the Acme workspace.', '<button id="decline" type="button" data-click-removes="#cf">'
      + 'Decline</button>'],
    // A bare Skip or Dismiss outside a promotion may acknowledge something that matters.
    ['New sign-in detected. We noticed a sign-in to your account from a new device.', '<button id="dismiss" '
      + 'type="button" data-click-removes="#cf">Dismiss</button> <button id="review" type="button">Review</button>'],
    ['Update your payment method. Your card expires at the end of the month.', '<button id="skip" type="button" '
      + 'data-click-removes="#cf">Skip for now</button> <button id="update" type="button">Update</button>'],
    ['Notifications. Your password was changed.', '<button id="all" type="button" data-click-removes="#cf">Dismiss all'
      + '</button>'],
  ];

  for (const [text, buttons] of ASKING) {
    test('a popup that asks gets Escape only: ' + text.slice(0, 40), async () => {
      const w = page(notice(text, buttons));
      const heal = w.open({ dismiss: true });
      await assert.rejects(heal.locator('go', '#go').click(), intercepted);
      assert.deepEqual(clicked(w), []);
      assert.equal(escapes(w), 1);
      assert.equal(gone(w, 'cf'), false);
      assert.deepEqual(heal.handledInterruptions, []);
      const [warning] = warnings(w);
      assert.ok(warning.startsWith("could not dismiss a popup in the way of 'go': <div id=\"cf\" role=\"dialog\">"));
    });
  }

  const PROMOTIONS: [string, string, string, string][] = [
    ['Sign up for our newsletter and never miss a sale.', '<button id="sub" type="button">Subscribe</button> '
      + '<button id="skip" type="button" data-click-removes="#cf">Skip</button>', 'skip', 'Skip'],
    ['Get the Acme app for faster checkout.', '<button id="install" type="button">Install the app</button> '
      + '<button id="dismiss" type="button" data-click-removes="#cf">Dismiss</button>', 'dismiss', 'Dismiss'],
    ["Take a quick tour of what's new.", '<button id="skip" type="button" data-click-removes="#cf">Skip for now'
      + '</button>', 'skip', 'Skip for now'],
    ['Welcome to Acme!', '<button id="x" type="button" aria-label="Dismiss" data-click-removes="#cf">×</button>', 'x',
      'Dismiss'],
  ];

  for (const [text, buttons, id, label] of PROMOTIONS) {
    test('a bare Skip or Dismiss in a promotion declines it: ' + text, async () => {
      const w = page(notice(text, buttons));
      const heal = w.open({ dismiss: true });
      await heal.locator('go', '#go').click();
      assert.deepEqual(clicked(w), [id, 'go']);
      assert.ok(gone(w, 'cf'));
      assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', label]]);
    });
  }

  test('Skip the line for money is never clicked, and No thanks is', async () => {
    const line = '<button id="line" type="button" data-click-removes="#nl">Skip the line - $9.99</button> ';
    const w = page(modal(line + NO_THANKS));
    await w.open({ dismiss: true }).locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
  });

  test('a disabled No thanks is passed over for the next decline button', async () => {
    const off = '<button id="off" type="button" disabled data-click-removes="#nl">No thanks</button> '
      + '<button id="aria-off" type="button" aria-disabled="true" data-click-removes="#nl">No thanks</button> ';
    const later = '<button id="later" type="button" data-click-removes="#nl">Maybe later</button>';
    const w = page(modal(off + later));
    await w.open({ dismiss: true }).locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['later', 'go']);
  });

  // What the page script says of a control (POPUP_CONTROLS_JS's facts) keeps it from a click:
  // a link that goes somewhere, a label, a field, a form it would submit, a toggle, a disabled
  // one, and words in what it hides or what its aria-labelledby names.
  const NEVER_CLICKED: [string, string][] = [
    ['a link with a real href', '<a id="c" href="/no-thanks" data-click-removes="#nl">No thanks</a>'],
    ['a button inside a link', '<a href="/offer"><button id="c" type="button" data-click-removes="#nl">No thanks'
      + '</button></a>'],
    ['a button that holds a link', '<button id="c" type="button" data-click-removes="#nl"><a href="/x">No thanks</a>'
      + '</button>'],
    ['a button in a label', '<label><button id="c" type="button" data-click-removes="#nl">No thanks</button></label>'],
    ['a label', '<label id="c" role="button" data-click-removes="#nl">No thanks</label>'],
    ['a button that holds a field', '<div id="c" role="button" data-click-removes="#nl">No thanks <input '
      + 'type="checkbox"></div>'],
    ['a button that submits its form', '<form><button id="c" data-click-removes="#nl">No thanks</button></form>'],
    ['a reset button', '<form><button id="c" type="reset" data-click-removes="#nl">No thanks</button></form>'],
    ['a submit button of a dialog form', '<form method="dialog"><button id="c" type="submit" '
      + 'data-click-removes="#nl">No thanks</button></form>'],
    ['a submit input in a form', '<form><input id="c" type="submit" value="No thanks" data-click-removes="#nl">'
      + '</form>'],
    ['a disabled button', '<button id="c" type="button" disabled data-click-removes="#nl">No thanks</button>'],
    ['an aria-disabled button', '<button id="c" type="button" aria-disabled="true" data-click-removes="#nl">'
      + 'No thanks</button>'],
    ['a toggle', '<button id="c" type="button" aria-pressed="false" data-click-removes="#nl">No thanks</button>'],
    ['a selected tab-like control', '<button id="c" type="button" aria-selected="false" data-click-removes="#nl">'
      + 'No thanks</button>'],
    ['a radio', '<button id="c" type="button" role="radio" data-click-removes="#nl">No thanks</button>'],
    ['a switch', '<div id="c" role="switch" data-click-removes="#nl">Not now</div>'],
    ['hidden words', '<button id="c" type="button" data-click-removes="#nl">No thanks<span style="display: none"> '
      + 'and subscribe me</span></button>'],
    ['what its aria-labelledby names', '<span id="lbl">Subscribe to weekly deals</span><button id="c" type="button" '
      + 'aria-labelledby="lbl" data-click-removes="#nl">No thanks</button>'],
    ['its title', '<button id="c" type="button" title="Accept all" data-click-removes="#nl">No thanks</button>'],
    ['an input whose value says yes', '<input id="c" type="button" value="Yes please" data-click-removes="#nl">'],
    ['a bare ×', '<button id="c" type="button" data-click-removes="#nl">×</button>'],
    ['a link to a page in javascript', '<a id="c" href="javascript:location.assign(\'/checkout\')" '
      + 'data-click-removes="#nl">Not now</a>'],
  ];

  for (const [what, html] of NEVER_CLICKED) {
    test('never clicked: ' + what, async () => {
      const w = page(modal(html));
      const heal = w.open({ dismiss: true });
      await assert.rejects(heal.locator('go', '#go').click(), intercepted);
      assert.deepEqual(clicked(w), []);
      assert.equal(gone(w, 'nl'), false);
      assert.deepEqual(heal.handledInterruptions, []);
    });
  }

  const CLICKED: [string, string][] = [
    ['a link that goes nowhere', '<a id="c" href="#" data-click-removes="#nl">No thanks</a>'],
    ['a javascript: link that does nothing', '<a id="c" href="javascript:void(0)" data-click-removes="#nl">Not now</a>'],
    ['a link with no href', '<a id="c" data-click-removes="#nl">No thanks</a>'],
    ['an input button', '<input id="c" type="button" value="No thanks" data-click-removes="#nl">'],
    ['a submit input outside a form', '<input id="c" type="submit" value="No thanks" data-click-removes="#nl">'],
    ['an element with role button', '<div id="c" role="button" data-click-removes="#nl">Not now</div>'],
    ['a button in a form that does not submit it', '<form><button id="c" type="button" data-click-removes="#nl">'
      + 'No thanks</button></form>'],
    ['what its aria-labelledby names, harmless', '<span id="lbl">Weekly deals</span><button id="c" type="button" '
      + 'aria-labelledby="lbl" data-click-removes="#nl">No thanks</button>'],
  ];

  for (const [what, html] of CLICKED) {
    test('clicked: ' + what, async () => {
      const w = page(modal(html));
      await w.open({ dismiss: true }).locator('go', '#go').click();
      assert.deepEqual(clicked(w), ['c', 'go']);
    });
  }

  test('the element inside the popup is never a reason to dismiss it', async () => {
    const tip = '<span id="tip" data-rect="310 250 200 30" data-z="20">We never share your email</span>';
    const w = page(modal(SIGN_UP + NO_THANKS, { extra: tip }));
    w.page.document.getElementById('nl-email')!.setAttribute('data-rect', '310 250 200 30');
    const heal = w.open({ dismiss: true });
    await heal.locator('nl_email', '#nl-email').fill('ada@example.com');   // the user uses the popup
    await assert.rejects(heal.locator('nl_email', '#nl-email').click(), intercepted);   // covered, in the popup
    assert.equal(gone(w, 'nl'), false);
    assert.equal(escapes(w), 0);
    assert.deepEqual(clicked(w), []);
    assert.deepEqual(heal.handledInterruptions, []);
  });

  test('a small fixed header over the element is not a popup', async () => {
    const header = '<header id="top" style="position: sticky" data-rect="0 50 1000 60" data-z="5"><a href="/">Shop</a>'
      + ' <button type="button" data-click-removes="#top">Close</button></header>';
    const w = page(header);
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.equal(escapes(w), 0);
    assert.deepEqual(clicked(w), []);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(warnings(w), []);
  });

  test('a plain fixed layer is a popup when it covers a quarter of the window', async () => {
    const layer = (h: number) => '<div id="layer" style="position: fixed" data-rect="0 0 1000 ' + h + '" data-z="5">'
      + '<p>Chat with us</p><button id="later" type="button" data-click-removes="#layer">Maybe later</button></div>';
    let w = page(layer(200));   // 0.25 of 1000 x 800
    await w.open({ dismiss: true }).locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['later', 'go']);
    w = page(layer(199));
    await assert.rejects(w.open({ dismiss: true }).locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), []);
  });

  test('a toast in a full-window layer that lets pointer events through is not a popup', async () => {
    const toast = '<div id="layer" style="position: fixed; pointer-events: none" data-rect="0 0 1000 800" data-z="5">'
      + '<div id="toast" style="pointer-events: auto" data-rect="0 50 300 60">Saved. <button type="button" '
      + 'data-click-removes="#layer">Dismiss</button></div></div>';
    const w = page(toast);
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);   // the toast is on top of it
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
    assert.deepEqual(heal.handledInterruptions, []);
  });

  test('a modal <dialog> element that is open is a popup', async () => {
    const dialog = '<dialog id="d" open style="position: fixed" data-rect="0 0 1000 800" data-z="10"><p>Our offer</p>'
      + '<button id="no" type="button" data-click-removes="#d">No thanks</button></dialog>';
    const w = page(dialog);
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['no', 'go']);
    assert.ok(heal.handledInterruptions[0].popup!.startsWith('<dialog id="d">'));
  });

  test('a banner in an open shadow root is declined there', async () => {
    const host = '<cmp-banner id="host" style="position: fixed" data-rect="0 0 1000 800" data-z="10">'
      + '<template shadowrootmode="open"><div id="cmp" role="dialog" aria-modal="true" data-rect="0 0 1000 800">'
      + '<p data-rect="300 300 300 30">We use cookies.</p><button id="acc" type="button" data-rect="300 400 100 30">Accept '
      + 'all</button><button id="rej" type="button" data-rect="420 400 100 30" data-click-removes="#host">Reject all'
      + '</button></div></template></cmp-banner>';
    const w = page(host);
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['rej', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'Reject all']]);
  });

  test('a consent banner is declined and never accepted, with cookies unset', async () => {
    const banner = notice('We and our partners use cookies.', '<button id="accept" type="button" '
      + 'data-click-removes="#cf">Accept all</button> <button id="settings" type="button">Settings</button> '
      + '<button id="reject" type="button" data-click-removes="#cf">Reject all</button>');
    const w = page(banner);
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['reject', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'Reject all']]);
  });

  test('a popup that stops covering the element counts as dismissed', async () => {
    const folds = '<button id="nl-no" type="button" data-click-uncovers="#nl, #nl *">No thanks</button>';
    const w = page(modal(SIGN_UP + folds));
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.equal(gone(w, 'nl'), false);
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'No thanks']]);
  });

  test('a popup gone at the last look before giving up counts as dismissed', async () => {
    const late = '<button id="nl-no" type="button" data-click-removes="#nl" data-click-removes-after-looks="2">No thanks'
      + '</button>';
    const w = page(modal(SIGN_UP + late));
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go', { timeoutMs: 0 }).click();
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'No thanks']]);
    assert.deepEqual(warnings(w), [dismissedMessage('go', nl(), '“No thanks”', ['Escape'])]);
  });

  for (const [after, dismissed] of [[60, true], [1500, false]] as const) {
    test('a popup that closes ' + after + ' ms after the click is ' + (dismissed ? '' : 'not ') + 'dismissed',
      async () => {
        const late = '<button id="nl-no" type="button" data-click-removes="#nl" data-click-removes-after="' + after
          + '">No thanks</button>';
        const w = page(modal(SIGN_UP + late));
        const heal = w.open({ dismiss: true });
        const step = heal.locator('go', '#go', { timeoutMs: 300, pollMs: 20 });
        if (dismissed) {
          await step.click();
          assert.deepEqual(clicked(w), ['nl-no', 'go']);
          assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'No thanks']]);
        } else {
          await assert.rejects(step.click(), intercepted);
          assert.deepEqual(clicked(w), ['nl-no']);
          assert.deepEqual(heal.handledInterruptions, []);
          assert.ok(warnings(w)[0].startsWith("could not dismiss a popup in the way of 'go': " + nl() + '. mendwright '
            + 'pressed Escape and clicked “No thanks”, which did not close it'));
          assert.deepEqual(events(w).map((e) => [e.outcome, e.tried]), [['failed', [{ how: 'escape' },
            { how: 'decline', label: 'No thanks' }]]]);
        }
      });
  }
});

describe('the budget', () => {
  test('a popup that comes back each time stops the step after 3', async () => {
    const again = '<button id="nl-no" type="button" data-click-reopens="#nl">No thanks</button>';
    const w = page(modal(SIGN_UP + again));
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click(), (e: unknown) => e instanceof InterruptionLoop
      && e instanceof AttentionRequired && (e as Error).message === dismissLoopMessage('go', nl(), 3));
    assert.ok(dismissLoopMessage('go', nl(), 3).startsWith("mendwright: 3 interruptions and popups were handled for "
      + "'go' and a popup is still in the way: " + nl() + '. It comes back each time it is dismissed'));
    assert.deepEqual(clicked(w), ['nl-no', 'nl-no', 'nl-no']);
    assert.deepEqual(heal.handledInterruptions.map((e) => e.key), [DISMISS_KEY, DISMISS_KEY, DISMISS_KEY]);
    // One flow.stop before the throw: the code and the step, none of the popup's text.
    const stops = events(w).filter((e) => e.event === 'flow.stop');
    assert.deepEqual(stops, [{ event: 'flow.stop', key: '<popup>', code: 'popup_loop', before: 'go' }]);
    assert.equal(events(w)[events(w).length - 1].event, 'flow.stop');
  });

  test('a step whose budget went to interruptions says so', async () => {
    const banners = [0, 1, 2].map((i) => '<div id="b' + i + '" style="position: fixed" data-rect="' + (600 + i * 100)
      + ' 700 90 40" data-z="20"><button id="ok' + i + '" type="button" data-rect="' + (600 + i * 100) + ' 700 90 40" '
      + 'data-click-removes="#b' + i + '">OK</button></div>').join('');
    const w = page(banners, modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    for (const i of [0, 1, 2]) heal.interrupt('b' + i, '#ok' + i);
    await assert.rejects(heal.locator('go', '#go').click(),
      (e: unknown) => e instanceof InterruptionLoop && (e as Error).message === dismissLoopMessage('go', nl(), 3, false));
    assert.ok(dismissLoopMessage('go', nl(), 3, false).startsWith("mendwright: 3 interruptions were handled for 'go' and "
      + 'a popup is still in the way: ' + nl() + '. A step handles at most 3, so mendwright stopped instead of '
      + 'dismissing it.'));
    assert.deepEqual(clicked(w), ['ok0', 'ok1', 'ok2']);
  });

  test('a popup that could not be dismissed is tried once in a step', async () => {
    const w = page(modal('<button id="nl-sub" type="button">Subscribe</button>'));
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    // Before the action, and none after it failed: the step gave that popup up.
    assert.equal(calls.pressEscape, 1);
    assert.equal(calls.popupRoot, 1);
    assert.equal(warnings(w).length, 1);
  });
});

describe('what a step did', () => {
  test('a click that throws after it reached the page is recorded, and nothing more is clicked', async () => {
    const away = '<button id="nl-x" type="button" aria-label="Close" data-click-raises>×</button> ';
    const w = page(modal(SIGN_UP + away + NO_THANKS));
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), ['nl-x']);   // never No thanks after it
    const text = 'Get 10% off your first order. Sign up × No thanks';
    assert.deepEqual(warnings(w), [lostMessage('go', nl(text), 'clicked “Close”', true)]);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w).map((e) => [e.outcome, e.tried]), [['lost', [{ how: 'escape' },
      { how: 'close', label: 'Close' }]]]);
  });

  test('a step after which the element is gone is not a dismissal', async () => {
    const drops = '<button id="nl-no" type="button" data-click-removes="#nl, #signup">No thanks</button>';
    const w = page(modal(SIGN_UP + drops));
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go', { timeoutMs: 0, pollMs: 0 }).click(), /resolved to 0 elements/);
    assert.deepEqual(clicked(w), ['nl-no']);
    assert.deepEqual(warnings(w), [lostMessage('go', nl(), 'clicked “No thanks”')]);
    assert.ok(warnings(w)[0].startsWith("Clicked “No thanks” on a popup in the way of 'go': " + nl() + ', and then could '
      + "not look at the element of 'go' as before"));
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w).map((e) => [e.outcome, e.tried]), [['lost', [{ how: 'escape' },
      { how: 'decline', label: 'No thanks' }]]]);
  });

  test('a step that hides the element is not a dismissal', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    w.page.document.getElementById('signup')!.setAttribute('data-escape-hides', '');
    const heal = w.open({ dismiss: true });
    await assert.rejects(heal.locator('go', '#go').click());
    assert.equal(escapes(w), 1);
    assert.deepEqual(clicked(w), []);
    assert.equal(gone(w, 'nl'), false);
    assert.deepEqual(warnings(w), [lostMessage('go', nl(), 'pressed Escape')]);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w).map((e) => [e.outcome, e.tried]), [['lost', [{ how: 'escape' }]]]);
  });

  test('a popup that changed at both looks is left alone', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    (heal.driver as unknown as { popupRoot: () => Promise<null> }).popupRoot = async () => {
      calls.popupRoot += 1;
      return null;   // not what the look before saw any more
    };
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.equal(calls.popupRoot, 2);
    assert.equal(calls.popupProbe, 3);   // one look, then two under the lock
    assert.equal(escapes(w), 0);
    assert.deepEqual(clicked(w), []);
    assert.deepEqual(warnings(w), [undismissedMessage('go', nl(), [], { changed: true })]);
    assert.ok(warnings(w)[0].startsWith("could not dismiss a popup in the way of 'go': " + nl() + '. It changed before '
      + 'mendwright could act on it, so mendwright left it alone. The action goes ahead'));
    assert.deepEqual(heal.handledInterruptions, []);
  });

  test('a control that changed in any fact since it was listed is not clicked', async () => {
    const w = page(modal('<input id="nl-no" type="button" value="No thanks" data-click-removes="#nl">'));
    const heal = w.open({ dismiss: true });
    const driver = heal.driver;
    const native = driver.locator('#go');
    const popup = popupOf(parseProbe(await driver.popupProbe(native)))!;
    const root = (await driver.popupRoot(native, expectOf(popup)))!;
    const controls = (await driver.popupControls(native, root)) as Record<string, unknown>[];
    const index = controls.findIndex((c) => c.value === 'No thanks');
    const input = w.page.document.getElementById('nl-no')!;
    input.setAttribute('value', 'Yes, subscribe me');
    assert.equal(await driver.popupClick(native, root, expectControl(index, controls[index]), 1000), false);
    assert.deepEqual(clicked(w), []);
    input.setAttribute('value', 'No thanks');
    assert.equal(await driver.popupClick(native, root, expectControl(index, controls[index]), 1000), true);
    assert.deepEqual(clicked(w), ['nl-no']);
  });

  test('a popup that opens while the click runs is dismissed, and the click is tried once more', async () => {
    const w = page();
    const heal = w.open({ dismiss: true });
    const step = await heal.locator('go', '#go').resolve();
    let opened = false;
    const original = FakeLocator.prototype.click;
    FakeLocator.prototype.click = async function (this: FakeLocator) {
      if (!opened && this.selector.endsWith('#go')) {
        opened = true;
        add(w, modal(SIGN_UP + NO_THANKS));
      }
      return original.call(this);
    };
    try {
      await step.click();
    } finally {
      FakeLocator.prototype.click = original;
    }
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.deepEqual(w.page.actions.filter(([a]) => a === 'click').length, 2);   // intercepted, then again
    assert.deepEqual(heal.handledInterruptions.map((e) => e.before), ['go']);
  });

  test('focus() and setInputFiles() are not looked at', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    await heal.locator('email', '#email').focus();
    await heal.locator('email', '#email').setInputFiles('cv.pdf');
    assert.equal(calls.popupProbe, 0);
    assert.equal(gone(w, 'nl'), false);
  });

  test('reads are not looked at', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    assert.equal(await heal.locator('go', '#go').textContent(), 'Go');
    assert.equal(calls.popupProbe, 0);
  });
});

describe('where Escape goes', () => {
  test('Escape is not pressed while a field outside the popup has the focus', async () => {
    const w = page();
    const heal = w.open({ dismiss: true });
    await heal.locator('email', '#email').fill('ada@example.com');   // the focus stays there
    add(w, modal(SIGN_UP + NO_THANKS, { escape: true }));
    await heal.locator('go', '#go').click();
    assert.equal(escapes(w), 0);
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.equal((w.page.document.getElementById('email') as HTMLInputElement).value, 'ada@example.com');
    assert.deepEqual(warnings(w), [dismissedMessage('go', nl(), '“No thanks”', [])]);
    assert.deepEqual(events(w).map((e) => e.tried), [[]]);
  });

  test('Escape is pressed while the focus is in the popup', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS, { escape: true }));
    (w.page.document.getElementById('nl-email') as HTMLElement).focus();   // the popup put it on its field
    const heal = w.open({ dismiss: true });
    await heal.locator('go', '#go').click();
    assert.deepEqual(w.page.keys, [['Escape', 'nl-email']]);
    assert.deepEqual(clicked(w), ['go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => e.how), ['escape']);
  });
});

describe('registered interruptions first', () => {
  const CONSENT = '<div id="privacy-pop" class="pp" role="dialog" aria-label="Privacy" style="position: fixed" '
    + 'data-rect="0 0 1000 800" data-z="10"><p>We and our partners use cookies.</p>'
    + '<button id="accept" type="button" data-click-removes="#privacy-pop">Accept all</button> '
    + '<button id="reject" type="button" data-click-removes="#privacy-pop">Reject all</button></div>';
  const CONSENT_POPUP = '<div id="privacy-pop" role="dialog" class="pp"> ("We and our partners use cookies. Accept all '
    + 'Reject all")';

  test('a registered interruption that is also a popup is handled its way', async () => {
    const w = page(CONSENT);
    const heal = w.open({ dismiss: true });
    heal.interrupt('consent', '#accept');
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['accept', 'go']);
    assert.equal(escapes(w), 0);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['consent', 'go']]);
  });

  test('a registered interruption is handled first, a popup after it', async () => {
    const cookies = '<div id="cc" style="position: fixed" data-rect="0 750 1000 50" data-z="20"><button id="cookie-ok" '
      + 'type="button" data-rect="10 755 80 30" data-click-removes="#cc">OK</button></div>';
    const w = page(cookies, modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    heal.interrupt('cookies', '#cookie-ok');
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['cookie-ok', 'nl-no', 'go']);
    assert.equal(w.page.actions.filter(([a, s]) => a === 'click' && s.endsWith('#go')).length, 1);   // never hit the popup
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['cookies', 'go'], [DISMISS_KEY, 'go']]);
  });

  test('no popup is dismissed while an interruption the step handled is still there', async () => {
    const stays = CONSENT.replace('<button id="accept" type="button" data-click-removes="#privacy-pop">',
      '<button id="accept" type="button">');
    const w = page(stays);
    const heal = w.open({ dismiss: true });
    heal.interrupt('consent', '#accept');
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), ['accept', 'accept']);   // before the click, and after it
    assert.equal(escapes(w), 0);
  });

  test("with cookies: 'accept' a consent banner it does not know is left alone", async () => {
    const w = page(CONSENT);
    const heal = w.open({ dismiss: true, cookies: 'accept' });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
    assert.deepEqual(warnings(w).filter((l) => l.includes('popup')), [consentMessage('go', CONSENT_POPUP)]);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w), []);
  });

  test("with cookies: 'accept' a banner that speaks of cookies is left alone, with no accept button", async () => {
    // Nothing on it says accept: its text alone says it is a consent banner (isConsent).
    const banner = '<div id="pn" role="dialog" style="position: fixed" data-rect="0 0 1000 800" data-z="10">'
      + '<p>We and our partners use cookies.</p><button id="x" type="button" aria-label="Close" '
      + 'data-click-removes="#pn">×</button> <button id="rej" type="button" data-click-removes="#pn">Reject all</button>'
      + '</div>';
    const w = page(banner);
    const heal = w.open({ dismiss: true, cookies: 'accept' });
    const calls = spy(heal);
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
    assert.equal(calls.popupRoot, 0);   // left alone from what the look saw
    assert.deepEqual(warnings(w).filter((l) => l.includes('popup')), [consentMessage('go',
      '<div id="pn" role="dialog"> ("We and our partners use cookies. × Reject all")')]);
    assert.deepEqual(heal.handledInterruptions, []);
  });

  test("with cookies: 'accept' Escape is never pressed", async () => {
    const w = page(modal(SIGN_UP + NO_THANKS, { escape: true }));
    const heal = w.open({ dismiss: true, cookies: 'accept' });
    await heal.locator('go', '#go').click();
    assert.equal(escapes(w), 0);
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.how, e.label]), [['decline', 'No thanks']]);
  });

  test("with cookies: 'accept' a bare backdrop gets no Escape", async () => {
    const backdrop = '<div id="pn-bg" class="overlay" style="position: fixed" data-rect="0 0 1000 800" data-z="10" '
      + 'data-escape></div>';
    const w = page(backdrop);
    const heal = w.open({ dismiss: true, cookies: 'accept' });
    await assert.rejects(heal.locator('go', '#go').click(), intercepted);
    assert.equal(escapes(w), 0);
    assert.equal(gone(w, 'pn-bg'), false);
    assert.ok(warnings(w).includes(undismissedMessage('go', '<div id="pn-bg" class="overlay">', [],
      { escaped: false, consent: true })));
    assert.deepEqual(heal.handledInterruptions, []);
  });

  for (const accept of ['Accept', 'Alle akzeptieren', 'Accepter et fermer']) {
    test("with cookies: 'accept' a popup with an accept button is left alone: " + accept, async () => {
      const banner = '<div id="msg" class="message-container" style="position: fixed" data-rect="0 0 1000 800" '
        + 'data-z="10"><p>Choose how we use information.</p><button id="ok" type="button">' + accept + '</button> '
        + '<button id="x" type="button" aria-label="Close" data-click-removes="#msg">×</button></div>';
      const w = page(banner);
      const heal = w.open({ dismiss: true, cookies: 'accept' });
      await assert.rejects(heal.locator('go', '#go').click(), intercepted);
      assert.deepEqual(clicked(w), []);
      assert.equal(escapes(w), 0);
      const popup = '<div id="msg" class="message-container"> ("Choose how we use information. ' + accept + ' ×")';
      assert.deepEqual(warnings(w).filter((l) => l.includes('popup')), [consentMessage('go', popup)]);
      assert.deepEqual(heal.handledInterruptions, []);
    });
  }

  test("with cookies: 'reject' a consent banner it does not know is declined", async () => {
    const w = page(CONSENT);
    const heal = w.open({ dismiss: true, cookies: 'reject' });
    await heal.locator('go', '#go').click();
    assert.deepEqual(clicked(w), ['reject', 'go']);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.how, e.label]),
      [[DISMISS_KEY, 'decline', 'Reject all']]);
  });
});

describe('every way to an element', () => {
  for (const how of ['click', 'fill', 'locator']) {
    test('heal.page ' + how + ' dismisses too', async () => {
      const w = page(modal(SIGN_UP + NO_THANKS));
      const heal = w.open({ dismiss: true });
      if (how === 'click') await heal.page.click('#go');
      else if (how === 'fill') await heal.page.fill('#email', 'ada@example.com');
      else await heal.page.locator('#go').click();
      assert.deepEqual(clicked(w), how === 'fill' ? ['nl-no'] : ['nl-no', 'go']);
      assert.deepEqual(heal.handledInterruptions.map((e) => e.before), [how === 'fill' ? '#email' : '#go']);
    });
  }

  test("heal.page guards the locators it does not heal, and saves nothing for them", async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    const p = heal.page as unknown as Record<string, any>;
    const go = p.getByTestId('go');
    assert.equal(stepOf(go), "page.getByTestId('go')");
    assert.ok(go.native instanceof FakeLocator);
    w.page.document.getElementById('go')!.setAttribute('data-testid', 'go');
    await go.click();
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.deepEqual(heal.handledInterruptions, [{ key: DISMISS_KEY, before: "page.getByTestId('go')", popup: nl(),
      how: 'decline', label: 'No thanks' }]);
    assert.ok(!fs.existsSync(w.storePath) || JSON.stringify(JSON.parse(fs.readFileSync(w.storePath, 'utf-8')).selectors
      ?? {}) === '{}');
    const items = await p.locator('#go').all();
    const names = [p.locator('#go', { hasText: 'Go' }), p.locator('#signup').locator('#go'), p.locator('#go').first(),
      p.locator('#go').last(), p.locator('#go').nth(0), p.getByTestId('go').first(),
      p.locator('#signup').locator('#go').nth(0), items[0], p.frameLocator('#f').locator('#go'),
      p.locator('xpath=//button', { has: p.locator('#go') })];
    assert.deepEqual(names.map(stepOf), ["page.locator('#go', { hasText: 'Go' })",
      "page.locator('#signup').locator('#go')", "page.locator('#go').first()", "page.locator('#go').last()",
      "page.locator('#go').nth(0)", "page.getByTestId('go').first()", "page.locator('#signup').locator('#go').nth(0)",
      "page.locator('#go').all()[0]", "page.frameLocator('#f').locator('#go')",
      "page.locator('xpath=//button', { has: page.locator('#go') })"]);
    assert.equal(await p.locator('#go').count(), 1);   // not a locator: as it is
    await names[1].click();
    assert.deepEqual(clicked(w), ['nl-no', 'go', 'go']);
  });

  test('a guarded locator below the window is scrolled into view and looked at before the action', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const go = w.page.document.getElementById('go')!;
    go.setAttribute('data-rect', '10 1500 100 30');
    go.setAttribute('data-testid', 'go');
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    await (heal.page as unknown as Record<string, any>).getByTestId('go').click();
    assert.ok(w.page.scrollY > 0);
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.equal(w.page.actions.filter(([a]) => a === 'click').length, 1);   // the action once
    assert.ok(calls.popupProbe >= 2);
  });

  test('two actions awaited together dismiss one popup once', async () => {
    const w = page(modal(SIGN_UP + NO_THANKS));
    const heal = w.open({ dismiss: true });
    await Promise.all([heal.locator('email', '#email').fill('ada@example.com'), heal.locator('go', '#go').click()]);
    assert.deepEqual(clicked(w), ['nl-no', 'go']);
    assert.equal(heal.handledInterruptions.length, 1);
  });
});

describe('run functions of heal.interrupt()', () => {
  /** A session-expired popup below the form, not fixed; z is its data-z. */
  const relogin = (z = 0) => '<div id="relogin" role="dialog" aria-label="Session expired" data-rect="0 300 1000 200"'
    + (z ? ' data-z="' + z + '"' : '') + '><p data-rect="10 310 300 20">Your session expired.</p>'
    + '<input id="user" data-rect="10 340 200 30"><button id="signin" type="button" data-rect="10 380 100 30" '
    + 'data-click-removes="#relogin">Sign in</button></div>';
  /** The newsletter modal over the window from y on: from 250 it covers the popup's Sign in
   * button and not the form; from 50, Go too, and not the email field. */
  const lower = (y: number) => '<div id="nl" class="nl" role="dialog" aria-modal="true" style="position: fixed" '
    + 'data-rect="0 ' + y + ' 1000 ' + (800 - y) + '" data-z="10"><div class="panel" data-rect="300 400 400 300">'
    + '<p>Get 10% off your first order.</p>' + SIGN_UP + NO_THANKS + '</div></div>';

  test('no popup is dismissed while a run function fills in its own', async () => {
    const w = page(relogin(), lower(250));
    const heal = w.open({ dismiss: true });
    const calls = spy(heal);
    const runs: unknown[] = [];
    heal.interrupt('relogin', '#relogin', {
      run: async function signIn(p: Page) {
        runs.push(p);
        await p.click('#signin');   // the modal is over it
      },
    });

    await assert.rejects(heal.locator('email', '#email').fill('x'), intercepted);

    // The function's click failed with the browser's own error: nothing was looked at or
    // dismissed for it, and the step stopped there.
    assert.equal(runs.length, 1);
    assert.equal(calls.popupProbe, 0);
    assert.ok(!gone(w, 'nl') && !gone(w, 'relogin'));
    assert.deepEqual(clicked(w), []);
    assert.equal(escapes(w), 0);
    assert.deepEqual(heal.handledInterruptions, []);
    assert.deepEqual(events(w).map((e) => [e.event, e.outcome]), [['flow.interrupt', 'failed']]);
    // One WARNING, for the function: the click's own does not name the interruption it is filling in.
    assert.equal(warnings(w).length, 1);
    assert.ok(warnings(w)[0].startsWith("signIn(), the run function of interruption 'relogin', raised an error"));
  });

  test('a dismissal already under way when a run function is called ends before the function starts', async () => {
    // The modal covers Go, and Escape doesn't close it. While mendwright presses Escape on it,
    // the session popup opens under the modal and another step calls its run function: the
    // function waits until the dismissal is done, No thanks clicked and all, and nothing of the
    // dismissal happens while it runs.
    const w = page(lower(50));
    const heal = w.open({ dismiss: true });
    heal.interrupt('relogin', '#relogin', {
      run: async function signIn(p: Page) {
        w.page.calls.push(['run', 'started']);
        await p.click('#signin');
        w.page.calls.push(['run', 'returned']);
      },
    });
    let other: Promise<void> | null = null;
    const press = w.page.keyboard.press;
    w.page.keyboard.press = async (key: string) => {
      w.page.calls.push(['key', key]);
      const pressed = await press(key);
      if (!other) {
        add(w, relogin(5));
        other = heal.locator('email', '#email').fill('ada@example.com');
        await new Promise((resolve) => setTimeout(resolve, 200));   // the other step runs meanwhile
      }
      return pressed;
    };
    const clickOn = w.page.clickOn.bind(w.page);
    w.page.clickOn = (el: Element) => {
      w.page.calls.push(['click', el.id]);
      clickOn(el);
    };

    await heal.locator('go', '#go').click();
    await other;

    const order = w.page.calls.filter(([what]) => ['key', 'click', 'run'].includes(what))
      .map(([what, on]) => what + ' ' + on);
    const started = order.indexOf('run started');
    const returned = order.indexOf('run returned');
    assert.ok(started >= 0 && returned > started, order.join(', '));
    // Nothing of the dismissal while the function runs. (The step of Go, past its checks, may
    // click meanwhile, as on the Python package's async healer.)
    const during = order.slice(started, returned + 1);
    assert.ok(during.includes('click signin') && !during.some((s) => s === 'key Escape' || s.startsWith('click nl-')), order.join(', '));
    assert.ok(order.indexOf('click nl-no') >= 0 && order.indexOf('click nl-no') < started, order.join(', '));
    assert.ok(gone(w, 'nl') && gone(w, 'relogin'));
  });

  test('nor once a run function started while the element was looked at', async () => {
    // The modal covers Go. While mendwright looks at what covers it, the session popup opens
    // above the modal, and another step calls its run function: the modal is left alone until
    // the function is done, and dismissed after it.
    const w = page(lower(50));
    const heal = w.open({ dismiss: true });
    let started!: () => void;
    const running = new Promise<void>((resolve) => (started = resolve));
    heal.interrupt('relogin', '#relogin', {
      run: async function signIn(p: Page) {
        started();
        await new Promise((resolve) => setTimeout(resolve, 200));
        await p.click('#signin');
        w.page.calls.push(['run', 'signIn() returned']);
      },
    });
    // What happened on the page, in order: the keys pressed and the elements clicked.
    const press = w.page.keyboard.press;
    w.page.keyboard.press = async (key: string) => {
      w.page.calls.push(['key', key]);
      return press(key);
    };
    const clickOn = w.page.clickOn.bind(w.page);
    w.page.clickOn = (el: Element) => {
      w.page.calls.push(['click', el.id]);
      clickOn(el);
    };
    const driver = heal.driver as unknown as { popupProbe: (...args: unknown[]) => Promise<unknown> };
    const probe = driver.popupProbe.bind(driver);
    let other: Promise<void> | null = null;
    driver.popupProbe = async (...args: unknown[]) => {
      const seen = await probe(...args);
      if (!other) {
        add(w, relogin(20));
        other = heal.locator('email', '#email').fill('ada@example.com');
        await running;
      }
      return seen;
    };

    await heal.locator('go', '#go').click();
    await other;

    const order = w.page.calls.filter(([what]) => ['key', 'click', 'run'].includes(what))
      .map(([what, on]) => what + ' ' + on);
    const returned = order.indexOf('run signIn() returned');
    assert.ok(returned > 0, order.join(', '));
    assert.deepEqual(order.slice(0, returned), ['click signin'], order.join(', '));   // nothing else before it returned
    assert.ok(order.includes('click nl-no') && order.at(-1) === 'click go', order.join(', '));
    assert.ok(gone(w, 'nl') && gone(w, 'relogin'));
  });
});
