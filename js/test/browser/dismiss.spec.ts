// open({ dismiss: true }) in real browsers: popups nobody registered, over a signup form. The
// page logs every key press and every button its popups' buttons run into #log, so a test can
// tell what mendwright pressed and clicked. No model is called.

import { DISMISS_KEY, InterruptionLoop, AttentionRequired, loadStore, open } from 'mendwright';
import { expect as mwExpect, test as mwTest } from 'mendwright/test';
import { expect, test, type Page, type Route } from '@playwright/test';

import { expectControl, expectOf, parseProbe, popupOf } from '../../src/dismiss';
import { journal, serve, tempStore } from './helpers';

const FORM = `<form id="signup"><label for="email">Email</label><input id="email" type="email">
  <button type="button" id="go" onclick="done('go')">Go</button></form>
<p>Done: <span id="done"></span> Log: <span id="log"></span></p>
<script>
function done(what) { document.getElementById('done').textContent += what + ' '; }
function log(what) { document.getElementById('log').textContent += what + ' '; }
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') log('key:Escape'); });
</script>`;

const html = (body: string) => '<!doctype html><html><head><title>Popup</title></head><body>' + body + '</body></html>';

/** A newsletter modal over the whole page: a dialog with an email field and buttons. It
 * closes on Escape only with escape. */
function modal(buttons: string[], options: { escape?: boolean; extra?: string } = {}): string {
  let out = '<div id="nl" class="nl" role="dialog" aria-modal="true" aria-label="Newsletter" '
    + 'style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">'
    + '<div style="background: #fff; width: 320px; margin: 140px auto; padding: 16px">'
    + '<p>Get 10% off your first order.</p><input id="nl-email" type="email" placeholder="Your email"> '
    + buttons.join(' ') + (options.extra ?? '') + '</div></div>';
  if (options.escape) {
    out += "<script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { "
      + "const m = document.getElementById('nl'); if (m) m.remove(); } });</script>";
  }
  return out;
}

const CLOSE = "document.getElementById('nl').remove()";
const SIGN_UP = '<button type="button" id="nl-signup" onclick="log(\'signup\')">Sign up</button>';
const NO_THANKS = '<button type="button" id="nl-no" onclick="log(\'no-thanks\'); ' + CLOSE + '">No thanks</button>';
const SUBSCRIBE = '<button type="button" onclick="log(\'subscribe\')">Subscribe</button>';
const YES = '<button type="button" onclick="log(\'yes\')">Yes, sign me up</button>';

interface Opened {
  heal: ReturnType<typeof open>;
  lines: string[];
  store: string;
}

async function load(page: Page, body: string, options: Parameters<typeof open>[1] = {}): Promise<Opened> {
  await serve(page, { '/popup': html(body) });
  const store = tempStore('dismiss');
  const lines: string[] = [];
  const heal = open(page, { dismiss: true, storePath: store, timeoutMs: 1000, log: (_l, m) => lines.push(m),
    ...options });
  return { heal, lines, store };
}

const logged = async (page: Page) => ((await page.locator('#log').textContent()) ?? '').split(/\s+/).filter(Boolean);
const doneText = (page: Page) => page.locator('#done').textContent();
const gone = async (page: Page, id = 'nl') => (await page.locator('#' + id).count()) === 0;
const popupLines = (o: Opened) => o.lines.filter((l) => l.includes('popup'));

/** Click selector's element with a popup over it that mendwright must leave: the browser's own
 * error, and nothing recorded. */
async function leftAlone(o: Opened, selector = '#go', key = 'go',
                         error = /intercepts pointer events/): Promise<void> {
  const thrown = await o.heal.locator(key, selector).click({ timeout: 1500 }).then(() => null, (e: unknown) => e);
  expect(String(thrown)).toMatch(error);
  expect(thrown).not.toBeInstanceOf(AttentionRequired);
  expect(o.heal.handledInterruptions).toEqual([]);
}

test.describe('popups nobody registered', () => {
  test('a newsletter modal is dismissed with No thanks, never Sign up', async ({ page }) => {
    const o = await load(page, FORM + modal([SIGN_UP, NO_THANKS]));
    await o.heal.locator('go', '#go').click();
    expect(await doneText(page)).toBe('go ');
    expect(await logged(page)).toEqual(['key:Escape', 'no-thanks']);   // never Sign up
    expect(await gone(page)).toBe(true);
    const [event] = o.heal.handledInterruptions;
    expect([event.key, event.before, event.how, event.label]).toEqual([DISMISS_KEY, 'go', 'decline', 'No thanks']);
    expect(event.popup!.startsWith('<div id="nl" role="dialog" class="nl"> ("Get 10% off your first order.')).toBe(true);
    expect(o.lines.some((l) => l.startsWith("dismissed a popup in the way of 'go': " + event.popup
      + ' with “No thanks”, after Escape did not close it'))).toBe(true);
    expect(journal(o.store).filter((e) => e.event === 'flow.dismiss').map((e) => [e.outcome, e.how, e.label, e.tried]))
      .toEqual([['dismissed', 'decline', 'No thanks', [{ how: 'escape' }]]]);
  });

  test('a modal that closes on Escape is dismissed by Escape alone', async ({ page }) => {
    const o = await load(page, FORM + modal([SUBSCRIBE], { escape: true }));
    await o.heal.locator('email', '#email').fill('ada@example.com');
    await expect(page.locator('#email')).toHaveValue('ada@example.com');
    expect(await logged(page)).toEqual(['key:Escape']);
    expect(await gone(page)).toBe(true);
    expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['escape', null]]);
  });

  test('an icon close button is clicked', async ({ page }) => {
    const close = '<button type="button" aria-label="Close" onclick="log(\'close\'); ' + CLOSE + '">&times;</button>';
    const o = await load(page, FORM + modal([SUBSCRIBE, close]));
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'close']);
    expect(await doneText(page)).toBe('go ');
    expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['close', 'Close']]);
  });

  test('a modal with only Subscribe and Yes is left alone, and the action fails with the browser error',
    async ({ page }) => {
      const o = await load(page, FORM + modal([SUBSCRIBE, YES]));
      await leftAlone(o);
      expect(await logged(page)).toEqual(['key:Escape']);
      expect(await gone(page)).toBe(false);
      expect(await doneText(page)).toBe('');
      const [warning] = popupLines(o);
      expect(warning.startsWith("could not dismiss a popup in the way of 'go': <div id=\"nl\" role=\"dialog\" "
        + 'class="nl"> ("Get 10% off your first order.')).toBe(true);
      expect(warning).toContain('mendwright pressed Escape, which did not close it, and it has no close or decline '
        + 'button');
      expect(warning).toContain("heal.interrupt(\"<name>_field\", \"<selector of the field>\", { action: 'fill', "
        + "args: ['<value>'] })");
    });

  test('a No thanks link that goes somewhere is not clicked', async ({ page }) => {
    const link = '<a id="nl-no" href="/no-thanks" onclick="log(\'link\'); return false;">No thanks</a>';
    const o = await load(page, FORM + modal([SUBSCRIBE, link]));
    await leftAlone(o);
    expect(await logged(page)).not.toContain('link');
    expect(await gone(page)).toBe(false);
  });

  test('the element inside the modal is used, and nothing is dismissed', async ({ page }) => {
    const o = await load(page, FORM + modal([SIGN_UP, NO_THANKS]));
    await o.heal.locator('nl_email', '#nl-email').fill('ada@example.com');
    await o.heal.locator('nl_email', '#nl-email').click();
    await expect(page.locator('#nl-email')).toHaveValue('ada@example.com');
    expect(await logged(page)).toEqual([]);
    expect(await gone(page)).toBe(false);
    expect(o.heal.handledInterruptions).toEqual([]);
  });

  test('a small fixed header over the element is left alone', async ({ page }) => {
    const header = '<header id="top" style="position: fixed; top: 0; left: 0; right: 0; height: 60px; z-index: 5; '
      + 'background: #eee">Shop <button type="button" onclick="log(\'header\')">Close</button></header>';
    const go = '<button type="button" id="go" onclick="done(\'go\')" style="position: fixed; top: 15px; left: 300px; '
      + 'height: 30px; z-index: 1">Go</button>';
    const o = await load(page, header + go + FORM.replace('id="go"', 'id="other"'));
    await leftAlone(o);
    expect(await logged(page)).toEqual([]);
    expect(await doneText(page)).toBe('');
  });

  test('a small sticky header over the top edge is left alone', async ({ page }) => {
    const header = '<header id="top" style="position: sticky; top: 0; height: 40px; z-index: 5; background: #eee">Shop '
      + '<button type="button" onclick="log(\'header\')">Close</button></header>';
    const go = '<button type="button" id="go" onclick="done(\'go\')" style="position: absolute; top: 40px; left: 200px; '
      + 'height: 40px">Go</button>';
    const o = await load(page, header + go + FORM.replace('id="go"', 'id="other"'));
    await o.heal.locator('go', '#go').click();
    expect(await doneText(page)).toBe('go ');
    expect(await logged(page)).toEqual([]);
    expect(o.heal.handledInterruptions).toEqual([]);
  });

  test('a consent banner is declined with cookies unset, and left alone with cookies accept', async ({ page }) => {
    const banner = `<div id="cmp" role="dialog" aria-label="Privacy choices"
      style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.4)">
      <div style="position: absolute; left: 0; right: 0; bottom: 0; padding: 16px; background: #fff">
        <p>We and our partners use cookies to measure traffic.</p>
        <button type="button" onclick="log('accept'); this.closest('#cmp').remove()">Accept all</button>
        <button type="button" onclick="log('settings')">Settings</button>
        <button type="button" onclick="log('reject'); this.closest('#cmp').remove()">Reject all</button>
      </div></div>`;
    const o = await load(page, FORM + banner);   // no cookies option
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'reject']);
    expect(await gone(page, 'cmp')).toBe(true);
    expect(await doneText(page)).toBe('go ');

    const accept = await load(page, FORM + banner, { cookies: 'accept' });
    await leftAlone(accept);
    expect(await logged(page)).toEqual([]);   // no Escape, no click
    expect(await gone(page, 'cmp')).toBe(false);
    expect(popupLines(accept).some((l) => l.startsWith("left a popup in the way of 'go' alone: <div id=\"cmp\" "
      + 'role="dialog"> ("We and our partners use cookies'))).toBe(true);
  });

  test('with dismiss off, the modal stops the click as before', async ({ page }) => {
    const o = await load(page, FORM + modal([SIGN_UP, NO_THANKS]), { dismiss: false });
    await leftAlone(o);
    expect(await logged(page)).toEqual([]);
    expect(await gone(page)).toBe(false);
  });

  test('a popup that comes back each time stops the step', async ({ page }) => {
    const again = '<button type="button" id="nl-no" onclick="log(\'no-thanks\'); ' + CLOSE
      + "; document.body.insertAdjacentHTML('beforeend', window.popupHtml)\">No thanks</button>";
    const popup = modal([SIGN_UP, again]);
    const o = await load(page, FORM + popup + '<script>window.popupHtml = ' + JSON.stringify(popup) + ';</script>');
    const error = await o.heal.locator('go', '#go').click().then(() => null, (e: unknown) => e);
    expect(error).toBeInstanceOf(InterruptionLoop);
    expect(String((error as Error).message)).toMatch(/^mendwright: 3 interruptions and popups were handled for 'go' and a popup is still in the way/);
    const log = await logged(page);
    expect(log.filter((l) => l === 'no-thanks')).toHaveLength(3);
    expect(log).not.toContain('signup');
    expect(o.heal.handledInterruptions.map((e) => e.key)).toEqual([DISMISS_KEY, DISMISS_KEY, DISMISS_KEY]);
    expect(await doneText(page)).toBe('');
    expect(journal(o.store).filter((e) => e.event === 'flow.stop').map(({ ts: _ts, ...e }) => e))
      .toEqual([{ event: 'flow.stop', key: '<popup>', code: 'popup_loop', before: 'go' }]);
  });
});

test.describe('where Escape goes', () => {
  test('Escape is not pressed while a field the script filled has the focus', async ({ page }) => {
    const o = await load(page, FORM + '<input id="q" type="search">');
    await o.heal.locator('q', '#q').fill('red shoes');   // the focus stays in #q
    await page.evaluate((popup) => document.body.insertAdjacentHTML('beforeend', popup), modal([SIGN_UP, NO_THANKS]));
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['no-thanks']);
    expect(await doneText(page)).toBe('go ');
    await expect(page.locator('#q')).toHaveValue('red shoes');
    expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['decline', 'No thanks']]);
  });

  test('an alert dialog gets no Escape and no click', async ({ page }) => {
    const account = `<div id="cf" role="alertdialog" aria-modal="true" aria-labelledby="cf-t"
      style="position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, 0.5)">
      <div style="background: #fff; width: 340px; margin: 120px auto; padding: 16px">
      <h2 id="cf-t">Close your account? This cannot be undone.</h2>
      <button type="button" onclick="log('cancel'); document.getElementById('cf').remove()">Cancel</button>
      <button type="button" onclick="log('closed'); document.getElementById('cf').remove()">Close</button>
      </div></div>
      <script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') {
        const d = document.getElementById('cf'); if (d) { log('answered'); d.remove(); } } });</script>`;
    const o = await load(page, FORM + account);
    await leftAlone(o);
    expect(await logged(page)).toEqual([]);
    expect(await gone(page, 'cf')).toBe(false);
    const [warning] = popupLines(o);
    expect(warning.startsWith("left a popup in the way of 'go' alone: <div id=\"cf\" role=\"alertdialog\">")).toBe(true);
    expect(warning).toContain('role="alertdialog"), which asks for an answer');
  });

  for (const [title, buttons] of [
    ['Close your account? This cannot be undone.', [['Close', 'closed']]],
    ['Delete this project?', [['Cancel', 'cancel'], ['Close', 'closed']]],
    ['Your box ships on Monday, with free shipping.', [['Keep it', 'kept'], ['Skip', 'skipped']]],
  ] as [string, [string, string][]][]) {
    test('a dialog that asks a question gets Escape only: ' + title, async ({ page }) => {
      const inner = buttons.map(([label, tag]) => '<button type="button" onclick="log(\'' + tag + '\'); ' + CLOSE + '">'
        + label + '</button>').join('');
      const dialog = '<div id="nl" role="dialog" aria-modal="true" style="position: fixed; inset: 0; z-index: 10; '
        + 'background: rgba(0, 0, 0, 0.5)"><div style="background: #fff; width: 340px; margin: 120px auto; padding: 16px">'
        + '<h2>' + title + '</h2>' + inner + '</div></div>';
      const o = await load(page, FORM + dialog);
      await leftAlone(o);
      expect(await logged(page)).toEqual(['key:Escape']);
      expect(await gone(page)).toBe(false);
    });
  }
});

/** A dialog over the whole page with its own text and the given buttons. Escape does not close it. */
function notice(text: string, ...buttons: string[]): string {
  return '<div id="nl" role="dialog" aria-modal="true" style="position: fixed; inset: 0; z-index: 10; '
    + 'background: rgba(0, 0, 0, 0.5)"><div style="background: #fff; width: 340px; margin: 120px auto; padding: 16px">'
    + '<p>' + text + '</p>' + buttons.join(' ') + '</div></div>';
}

const button = (label: string, tag: string, closes = true, aria?: string) => '<button type="button"'
  + (aria ? ' aria-label="' + aria + '"' : '') + ' onclick="log(\'' + tag + '\')' + (closes ? '; ' + CLOSE : '') + '">'
  + label + '</button>';

test.describe('which buttons are clicked', () => {
  test('a bare Skip or Dismiss outside a promotion is never clicked', async ({ page }) => {
    const o = await load(page, FORM + notice('New sign-in detected. We noticed a sign-in to your account from a new '
      + 'device.', button('Dismiss', 'dismiss'), button('Review', 'review', false)));
    await leftAlone(o);
    expect(await logged(page)).toEqual(['key:Escape']);
    expect(await gone(page)).toBe(false);
  });

  test('a bare Skip or Dismiss in a promotion dismisses it', async ({ page }) => {
    const o = await load(page, FORM + notice('Sign up for our newsletter and never miss a sale.',
      button('Subscribe', 'subscribe', false), button('Skip', 'skip')));
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'skip']);
    expect(await doneText(page)).toBe('go ');
    expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['decline', 'Skip']]);
  });

  test('Skip the line for money is never clicked, and No thanks is', async ({ page }) => {
    const skip = '<button type="button" onclick="log(\'skip-the-line\')">Skip the line - $9.99</button>';
    const o = await load(page, FORM + modal([skip, NO_THANKS]));
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'no-thanks']);
    expect(await doneText(page)).toBe('go ');
  });

  test('a disabled No thanks is passed over', async ({ page }) => {
    const disabled = '<button type="button" disabled onclick="log(\'disabled\')">No thanks</button>';
    const aria = '<button type="button" aria-disabled="true" onclick="log(\'aria-disabled\')">No thanks</button>';
    const notNow = '<button type="button" onclick="log(\'not-now\'); ' + CLOSE + '">Not now</button>';
    const o = await load(page, FORM + modal([disabled, aria, notNow]));
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'not-now']);
    expect(await doneText(page)).toBe('go ');
  });

  for (const [what, buttons, clicked] of [
    ['a button inside a link that goes somewhere', ['<a href="#/upgrade-plan" onclick="log(\'link\')"><button '
      + 'type="button">No thanks</button></a>'], 'link'],
    ['a button in a label', ['<label><input type="checkbox" id="mkt" onchange="log(\'checked\')"> <span role="button" '
      + 'onclick="log(\'span\')">No thanks</span></label>'], 'checked'],
    ['a button that submits its form', ['<form onsubmit="log(\'submitted\'); return false"><input name="email" '
      + 'value="me@x.test"> <button>No thanks</button></form>'], 'submitted'],
    ['a button of a dialog form', ['<form method="dialog" onsubmit="log(\'submitted\')"><button>No thanks</button>'
      + '</form>'], 'submitted'],
    ['a radio', ['<button type="button" role="radio" aria-checked="false" onclick="log(\'radio\')">Not interested'
      + '</button>'], 'radio'],
    ['a toggle', ['<button type="button" aria-pressed="false" onclick="log(\'toggle\')">Not now</button>'], 'toggle'],
    ['a javascript: link whose code runs', ['<a href="javascript:log(\'js-link\')">Not now</a>'], 'js-link'],
    ['hidden words it must not say', ['<button type="button" onclick="log(\'soft\')">No thanks<span style="display: '
      + 'none"> and subscribe me</span></button>'], 'soft'],
    ['what aria-labelledby names', ['<h3 id="cta">Subscribe to weekly deals</h3><button type="button" '
      + 'aria-labelledby="cta" onclick="log(\'labelledby\')">No thanks</button>'], 'labelledby'],
    ['a bare ×', ['<button type="button" onclick="log(\'bare\')">&times;</button>'], 'bare'],
    ['Reject outside a popup about cookies', ['<button type="button" onclick="log(\'reject\')">Reject</button>'],
      'reject'],
  ] as [string, string[], string][]) {
    test('never clicked: ' + what, async ({ page }) => {
      const o = await load(page, FORM + modal(buttons));
      const url = page.url();
      await leftAlone(o);
      const log = await logged(page);
      expect(log).toEqual(['key:Escape']);
      expect(log).not.toContain(clicked);
      expect(await gone(page)).toBe(false);
      expect(page.url()).toBe(url);
      if (clicked === 'checked') expect(await page.locator('#mkt').isChecked()).toBe(false);
    });
  }

  test("the click script refuses an input whose value changed", async ({ page }) => {
    const later = '<input type="button" id="nl-no" value="No thanks" onclick="log(\'no-thanks\'); ' + CLOSE + '">';
    const o = await load(page, FORM + modal([later]));
    const driver = o.heal.driver;
    const native = driver.locator('#go');
    const popup = popupOf(parseProbe(await driver.popupProbe(native)))!;
    const root = (await driver.popupRoot(native, expectOf(popup)))!;
    const controls = (await driver.popupControls(native, root)) as Record<string, unknown>[];
    const index = controls.findIndex((c) => c.value === 'No thanks');
    expect(await driver.popupRoot(native, { ...expectOf(popup), id: 'other' })).toBeNull();
    await page.evaluate(() => document.getElementById('nl-no')!.setAttribute('value', 'Yes, subscribe me'));
    expect(await driver.popupClick(native, root, expectControl(index, controls[index]), 1000)).toBe(false);
    expect(await logged(page)).toEqual([]);
    expect(await gone(page)).toBe(false);
    await page.evaluate(() => document.getElementById('nl-no')!.setAttribute('value', 'No thanks'));
    expect(await driver.popupClick(native, root, expectControl(index, controls[index]), 1000)).toBe(true);
    expect(await logged(page)).toEqual(['no-thanks']);
  });
});

test.describe('what is a popup', () => {
  test('a small toast in a full-window layer that lets pointer events through is not a popup', async ({ page }) => {
    const toast = `<div id="toaster" style="position: fixed; inset: 0; pointer-events: none; z-index: 50">
      <div id="toast" role="status" style="position: absolute; pointer-events: auto; width: 220px; height: 60px;
      background: #333; color: #fff">Item added <button type="button" onclick="log('toast-dismiss')">Dismiss</button>
      </div></div>
      <script>const r = document.getElementById('go').getBoundingClientRect();
      const t = document.getElementById('toast');
      t.style.left = (r.left + r.width / 2 - 30) + 'px'; t.style.top = (r.top + r.height / 2 - 30) + 'px';</script>`;
    const o = await load(page, FORM + toast);
    await leftAlone(o);
    expect(await logged(page)).toEqual([]);
  });

  test('a modal <dialog> is a popup', async ({ page }) => {
    const dialog = '<dialog id="rate"><p>Rate us</p><button type="button" onclick="log(\'close\'); '
      + 'this.closest(\'dialog\').close()">Close</button></dialog>'
      + '<script>document.getElementById("rate").showModal();</script>';
    const o = await load(page, FORM + dialog);
    await o.heal.locator('go', '#go').click();
    expect(await page.evaluate(() => (document.getElementById('rate') as HTMLDialogElement).open)).toBe(false);
    expect(await doneText(page)).toBe('go ');
    const [event] = o.heal.handledInterruptions;
    expect(event.key).toBe(DISMISS_KEY);
    expect(event.popup!.startsWith('<dialog id="rate">')).toBe(true);
  });

  for (const [name, banner] of [
    ['the popup', `<cmp-banner></cmp-banner><script>
      customElements.define('cmp-banner', class extends HTMLElement {
        connectedCallback() {
          const root = this.attachShadow({mode: 'open'});
          root.innerHTML = '<div role="dialog" aria-label="Privacy" style="position: fixed; inset: 0; '
            + 'z-index: 10; background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p>'
            + '<button type="button" id="a">Accept all</button> <button type="button" id="r">Reject all</button></div>';
          root.getElementById('a').onclick = () => { log('accept'); this.remove(); };
          root.getElementById('r').onclick = () => { log('reject'); this.remove(); };
        }
      });</script>`],
    ['its buttons', `<div id="cmp" role="dialog" aria-modal="true" style="position: fixed; inset: 0;
      z-index: 10; background: rgba(0, 0, 0, 0.4)"><p>We use cookies.</p><cmp-buttons></cmp-buttons></div>
      <script>customElements.define('cmp-buttons', class extends HTMLElement {
        connectedCallback() {
          const root = this.attachShadow({mode: 'open'});
          root.innerHTML = '<button type="button" id="a">Accept all</button> <button type="button" id="r">Reject all</button>';
          root.getElementById('a').onclick = () => { log('accept'); document.getElementById('cmp').remove(); };
          root.getElementById('r').onclick = () => { log('reject'); document.getElementById('cmp').remove(); };
        }
      });</script>`],
  ]) {
    test('a banner in an open shadow root is declined: ' + name, async ({ page }) => {
      const o = await load(page, FORM + banner);
      await o.heal.locator('go', '#go').click();
      expect(await logged(page)).toEqual(['key:Escape', 'reject']);
      expect(await doneText(page)).toBe('go ');
      expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['decline', 'Reject all']]);
    });
  }

  test('a popup inside an iframe is dismissed there, with no Escape, through a heal saved with its iframe',
    async ({ page }) => {
      const inner = html('<label><input type="checkbox" data-testid="legal-consent"> I accept the terms</label>'
        + modal([SIGN_UP, NO_THANKS], { escape: true }).replace(/log\(/g, 'void(')
        + "<script>document.addEventListener('keydown', () => { parent.postMessage('key', '*'); });</script>");
      const outer = '<h1>Outer page</h1><p>Log: <span id="log"></span></p><iframe id="consent-frame" width="600" '
        + 'height="400" srcdoc="' + inner.replace(/&/g, '&amp;').replace(/"/g, '&quot;') + '"></iframe>'
        + "<script>window.addEventListener('message', () => { document.getElementById('log').textContent += 'key '; });"
        + "document.addEventListener('keydown', () => { document.getElementById('log').textContent += 'outer-key '; });"
        + '</script>';
      await serve(page, { '/popup': html(outer) });
      const store = tempStore('dismiss-frame');
      // A heal the Python package saved inside the iframe: this package uses it as it is.
      const saved = loadStore(store);
      saved.setCodeCandidates('terms', ["//input[@id='terms']"]);
      saved.setMeta('terms', { role: 'checkbox' });
      saved.addCandidate('terms', "xpath=//input[@data-testid='legal-consent']", { frameXPath: 'iframe[id="consent-frame"]' });
      const heal = open(page, { dismiss: true, storePath: store, timeoutMs: 1000, log: () => undefined });
      await heal.locator('terms', "//input[@id='terms']", { role: 'checkbox' }).check();
      const frame = page.frameLocator('#consent-frame');
      expect(await frame.locator('[data-testid=legal-consent]').isChecked()).toBe(true);
      expect(await frame.locator('#nl').count()).toBe(0);
      expect(heal.handledInterruptions.map((e) => [e.key, e.how, e.label])).toEqual([[DISMISS_KEY, 'decline', 'No thanks']]);
      expect(await logged(page)).toEqual([]);   // Escape was never pressed, in the iframe or around it
    });
});

test.describe('what a step did', () => {
  test('a close click whose navigation does not commit is reported, and nothing more is clicked', async ({ page, browserName }) => {
    // Under load, WebKit can let Playwright's click return before the navigation its handler
    // starts is scheduled, so the click does not raise, and mendwright goes on to the next
    // button as it should: whether the click raises is up to the browser's timing there.
    test.skip(browserName === 'webkit', "whether WebKit's click waits for the navigation depends on timing");
    const away = '<button type="button" aria-label="Close" onclick="location.href = \'/stuck\'">&times;</button>';
    const o = await load(page, FORM + modal([away, NO_THANKS]));
    const held: Route[] = [];
    await page.route('**/stuck', (route) => {
      held.push(route);   // not answered yet
    });
    let error: unknown = null;
    try {
      error = await o.heal.locator('go', '#go').click({ timeout: 1500 }).then(() => null, (e: unknown) => e);
    } finally {
      for (const route of held) await route.abort().catch(() => undefined);
    }
    expect(error).not.toBeNull();
    expect(error).not.toBeInstanceOf(AttentionRequired);   // the browser's own
    expect(held.map((r) => r.request().url())).toEqual(['https://shop.test/stuck']);   // the click went through
    expect(o.heal.handledInterruptions).toEqual([]);
    const [warning] = popupLines(o);
    expect(warning.startsWith("Clicked “Close” on a popup in the way of 'go'")).toBe(true);
    expect(warning).toContain('the click raised an error after it may have reached the page');
    expect(warning).not.toContain('“No thanks”');
    expect(journal(o.store).filter((e) => e.event === 'flow.dismiss').map((e) => [e.outcome, e.tried]))
      .toEqual([['lost', [{ how: 'escape' }, { how: 'close', label: 'Close' }]]]);
  });

  test('Escape that hides the element is not counted as a dismissal', async ({ page }) => {
    const checkout = `<div id="checkout" role="dialog" aria-label="Checkout" style="position: fixed;
      left: 200px; top: 60px; width: 400px; height: 300px; z-index: 5; background: #fff">
      <input id="card" value="4242 4242">
      <button type="button" id="pay" onclick="done('pay')">Place order</button></div>
      <div id="chat" style="position: fixed; left: 0; top: 0; width: 100%; height: 60%; z-index: 20;
      background: rgba(0, 0, 0, 0.3)"><p>Chat with us!</p>
      <button type="button" onclick="log('chat')">Chat</button></div>
      <script>document.addEventListener('keydown', (e) => { if (e.key === 'Escape') {
        document.getElementById('checkout').style.display = 'none'; log('checkout-closed'); } });</script>`;
    const o = await load(page, FORM + checkout);
    await leftAlone(o, '#pay', 'pay', /element is not visible|intercepts pointer events/);
    expect(await logged(page)).toEqual(['key:Escape', 'checkout-closed']);
    expect(await doneText(page)).toBe('');
    const lines = popupLines(o);
    expect(lines).toHaveLength(1);
    expect(lines[0].startsWith("Pressed Escape on a popup in the way of 'pay': <div id=\"chat\">")).toBe(true);
    expect(lines[0]).toContain('it is hidden or outside the window');
  });

  test('a popup that closes a moment after the click is dismissed', async ({ page }) => {
    const later = '<button type="button" id="nl-no" onclick="log(\'no-thanks\'); setTimeout(() => ' + CLOSE
      + ', 100)">No thanks</button>';
    const o = await load(page, FORM + modal([SIGN_UP, later]), { timeoutMs: 300 });
    await o.heal.locator('go', '#go').click();
    expect(await logged(page)).toEqual(['key:Escape', 'no-thanks']);
    expect(await doneText(page)).toBe('go ');
    expect(o.heal.handledInterruptions.map((e) => [e.how, e.label])).toEqual([['decline', 'No thanks']]);
  });

  for (const how of ['locator', 'getByRole']) {
    test('an element below the window gets the look before the action: ' + how, async ({ page }) => {
      const low = FORM.replace('<form id="signup">', '<div style="height: 2500px"></div><form id="signup">');
      const o = await load(page, low + modal([SIGN_UP, NO_THANKS]));
      const started = Date.now();
      if (how === 'locator') await o.heal.locator('go', '#go').click({ timeout: 8000 });
      else await o.heal.page.getByRole('button', { name: 'Go', exact: true }).click({ timeout: 8000 });
      expect(await doneText(page)).toBe('go ');
      expect(await logged(page)).toEqual(['key:Escape', 'no-thanks']);
      expect(Date.now() - started).toBeLessThan(6000);   // waiting on the popup would take the click's whole 8 s
    });
  }
});

mwTest.describe('the fixtures', () => {
  mwTest.use({ mendwrightOptions: { dismiss: true, timeoutMs: 1000 }, mendwrightStore: tempStore('dismiss-fixture') });

  mwTest('getByRole with dismiss, and expect', async ({ page, heal }) => {
    await serve(page as Page, { '/popup': html(FORM + modal([SIGN_UP, NO_THANKS])) });
    const go = page.getByRole('button', { name: 'Go' });
    await go.click({ timeout: 5000 });
    await mwExpect(go).toHaveText('Go');
    await page.locator('#signup').locator('#email').fill('ada@example.com');
    expect(await doneText(page as Page)).toBe('go ');
    await mwExpect(page.locator('#email')).toHaveValue('ada@example.com');
    expect(await logged(page as Page)).toEqual(['key:Escape', 'no-thanks']);
    expect(heal.handledInterruptions.map((e) => e.before)).toEqual(["page.getByRole('button', { name: 'Go' })"]);
    expect(heal.store.candidates("page.getByRole('button', { name: 'Go' })")).toEqual([]);
  });
});
