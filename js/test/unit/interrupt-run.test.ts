// heal.interrupt(key, selector, { run }) on a fake page: a function that answers an
// interruption that needs a form, such as a session-expired sign-in. The cases are the Python
// package's (tests/test_flow.py), for its async healer. Each safety rule has a test here that
// fails without it; the dismissal ones are in dismiss-flow.test.ts.

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import type { Page } from '@playwright/test';

import { AttentionRequired, InterruptionLoop, InvalidSelector } from '../../src/errors';
import {
  errorType, handledMessage, loopMessage, runFailedMessage, runPlainMessage, runRemovesMessage, valueText,
} from '../../src/flow';
import { type Healer } from '../../src/healer';
import { checkRun, functionName, makeInterruption } from '../../src/interruptions';
import { Journal } from '../../src/journal';
import { FakeModel, cleanEnv, journalOf, tempDir, world, type World } from '../support/env';
import { REPO, needsPython, pythonCommand, runPython } from '../support/python';

beforeEach(cleanEnv);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const FORM = '<form id="signup"><label for="email">Email</label><input id="email" name="email" type="email">'
  + '<button id="go" type="button">Go</button></form>';

// A session-expired popup: it closes once Sign in is clicked.
const RELOGIN = '<div id="relogin" role="dialog" aria-label="Session expired"><p>Your session expired. Sign in again.</p>'
  + '<input id="user" name="user"><input id="password" name="password" type="password">'
  + '<button id="signin" type="button" data-click-removes="#relogin">Sign in</button></div>';
// One whose Sign in does not close it: the password is wrong.
const STICKY = RELOGIN.replace(' data-click-removes="#relogin"', '');

const html = (...parts: string[]) => '<html><body>' + parts.join('') + '</body></html>';

/** A page with the popup over the form: an action outside it fails as Playwright's does. */
function covered(...parts: string[]): World {
  return world(html(...parts, FORM), { blocker: '#relogin' });
}

/** The run function for RELOGIN, as a user writes it once: it fills the popup in through the
 * page it gets and signs in, which closes it. calls collects the page it got, once per call;
 * fail, when given, is thrown before it signs in. */
function relogin(calls: unknown[], fail?: unknown) {
  return async function relogin(page: Page): Promise<void> {
    calls.push(page);
    await page.fill('#user', 'ada');
    await page.fill('#password', 'not-a-real-password');
    if (fail !== undefined) throw fail;
    await page.click('#signin');
  };
}

const warnings = (w: World) => w.lines.filter((l) => l.startsWith('warning: ') && !l.startsWith('warning: open(): '))
  .map((l) => l.slice('warning: '.length));
const events = (w: World) => journalOf(w.dir).map(({ ts: _ts, ...rest }) => rest);
const done = (w: World) => w.page.actions.map(([verb, selector]) => verb + ' ' + selector.replace(/^css=/, ''));
const gone = (w: World, id: string) => w.page.document.getElementById(id) === null;
const valueOf = (w: World, id: string) => (w.page.document.getElementById(id) as HTMLInputElement).value;

describe('the arguments', () => {
  const run = async () => undefined;

  test('run with action or args, a run that is not a function, and a null selector throw TypeError at once', () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    const message = (fn: () => void) => {
      try {
        fn();
      } catch (error) {
        assert.ok(error instanceof TypeError, String(error));
        return (error as Error).message;
      }
      assert.fail('no TypeError');
    };
    assert.equal(message(() => heal.interrupt('relogin', '#relogin', { action: 'fill', args: ['ada'], run })),
      "mendwright: heal.interrupt('relogin', ...) got run and action: 'fill' and args: ['ada']. run does the whole "
      + 'interruption itself, so it takes no action or args: drop them, or drop run.');
    assert.match(message(() => heal.interrupt('relogin', '#relogin', { action: 'click', run })),
      /got run and action: 'click'\. run does/);
    assert.match(message(() => heal.interrupt('relogin', '#relogin', { args: 'Escape' as never, run })),
      /got run and args: 'Escape'\. run does/);
    assert.equal(message(() => heal.interrupt('relogin', '#relogin', { run: 'relogin' as never })),
      "mendwright: heal.interrupt('relogin', ..., { run }) takes the function that handles the interruption, which "
      + "gets the page, such as async function relogin(page) { ... }, got 'relogin'.");
    assert.equal(message(() => heal.interrupt('relogin', null, { run })),
      "mendwright: heal.interrupt('relogin', null) removes an interruption. To register one with run, pass the "
      + "selector of what shows it, such as '#session-expired'.");
    assert.throws(() => heal.interrupt('relogin', '', { run }), InvalidSelector);   // the selector comes first
    assert.throws(() => heal.interrupt('relogin', [], { run: 'relogin' as never }), /got an empty list/);
    // Nothing was registered, and nothing touched the page.
    assert.deepEqual(w.page.calls, []);
    assert.deepEqual(w.page.actions, []);
    assert.equal((heal as unknown as { interruptions: Map<string, unknown> }).interruptions.size, 0);
  });

  test('a run of null or undefined is no run, and args: [] is no args', () => {
    const plain = makeInterruption('cookies', '#accept', { run: undefined });
    assert.deepEqual([plain.action, plain.run], ['click', null]);
    assert.equal(makeInterruption('cookies', '#accept', { run: null as never }).run, null);
    const item = makeInterruption('relogin', ['#relogin', '#relogin'], { args: [], role: 'dialog', run: relogin([]) });
    assert.deepEqual([item.action, item.args, item.selectors, item.roles], ['run', [], ['#relogin'], ['dialog']]);
    const w = covered(RELOGIN);
    const heal = w.open();
    heal.interrupt('relogin', '#relogin', { run: relogin([]) });
    heal.interrupt('relogin', null, { run: undefined });   // removes it
    assert.equal((heal as unknown as { interruptions: Map<string, unknown> }).interruptions.size, 0);
  });

  test('a function is named by its own name, else <anonymous>', () => {
    async function signIn(_page: Page) {}
    class Session {
      async renew(_page: Page) {}
    }
    const options = { run: async (_page: Page) => undefined };
    assert.equal(functionName(signIn), 'signIn');
    assert.equal(functionName(signIn.bind(null)), 'signIn');
    assert.equal(functionName(new Session().renew), 'renew');
    assert.equal(functionName(options.run), '<anonymous>');   // named after the option it was written for
    assert.equal(functionName((() => async () => undefined)()), '<anonymous>');
    assert.equal(functionName(Object.defineProperty(() => undefined, 'name', { value: 'not a name' })), '<anonymous>');
  });
});

describe('a popup that needs a form', () => {
  test('is filled in by its run function, with heal.page, before the action, which then goes on', async () => {
    const w = covered(RELOGIN);
    const model = new FakeModel(['//never']);
    const heal = w.open({ provider: model });
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', { run: relogin(calls) });

    await heal.locator('email', '#email').fill('ada@example.com');

    // Once: its own actions on the popup look for no interruption, so they never call it again.
    assert.equal(calls.length, 1);
    assert.equal(calls[0], heal.page);
    assert.ok(gone(w, 'relogin'));
    assert.equal(valueOf(w, 'email'), 'ada@example.com');
    assert.deepEqual(done(w), ['fill #user', 'fill #password', 'click #signin', 'fill #email']);
    assert.deepEqual(warnings(w), ["handled interruption 'relogin' before 'email' with relogin()"]);
    assert.deepEqual(heal.handledInterruptions, [{ key: 'relogin', before: 'email', popup: null, how: null, label: null }]);
    assert.deepEqual(events(w).filter((e) => e.event === 'flow.interrupt'), [
      { event: 'flow.interrupt', key: 'relogin', before: 'email', action: 'run', selector: '#relogin', function: 'relogin' }]);
    assert.deepEqual(model.bundles, []);
  });

  test('its own selectors heal as usual: a renamed field of the popup is found from its fingerprint', async () => {
    const w = covered(RELOGIN);
    const first = w.open();
    first.interrupt('relogin', '#relogin', { run: relogin([]) });
    await first.locator('email', '#email').fill('x');   // '#user' matches, and its fingerprint is saved
    w.page.goto(html(RELOGIN.replace('id="user"', 'id="login-user"'), FORM));

    const heal = w.open();
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', { run: relogin(calls) });
    await heal.locator('email', '#email').fill('ada@example.com');

    assert.equal(calls.length, 1);
    assert.deepEqual(heal.heals.map((h) => [h.key, h.old, h.source]), [['#user', ['#user'], 'fingerprint']]);
    // The popup is gone with its field: the field got what the function filled in.
    assert.deepEqual([...w.page.filled].slice(-3).map(([el, value]) => [el.id, value]),
      [['login-user', 'ada'], ['password', 'not-a-real-password'], ['email', 'ada@example.com']]);
  });

  test('its own actions handle another interruption, such as a cookie banner over its form', async () => {
    const banner = '<div id="banner">We use cookies. <button id="cookie-accept" data-click-removes="#banner">OK</button></div>';
    // While the banner is there, it covers everything else, the popup's fields too.
    const w = world(html(banner, RELOGIN, FORM), { blocker: '#banner, #relogin' });
    const heal = w.open();
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', { run: relogin(calls) });
    heal.interrupt('cookies', '#cookie-accept');

    await heal.locator('email', '#email').fill('ada@example.com');

    // The banner was handled before the function's own field, and the function was not called again.
    assert.equal(calls.length, 1);
    assert.ok(gone(w, 'banner') && gone(w, 'relogin'));
    assert.equal(valueOf(w, 'email'), 'ada@example.com');
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['cookies', '#user'], ['relogin', 'email']]);
  });

  test('its own actions never handle its own interruption, so it is never called from inside itself', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    let calls = 0;
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        calls += 1;
        if (calls > 1) throw new Error('relogin() was called from inside itself');
        await page.fill('#user', 'ada');   // the popup is still there while it fills it in
        await page.click('#signin');
      },
    });
    await heal.locator('email', '#email').fill('x');
    assert.equal(calls, 1);
    assert.equal(valueOf(w, 'email'), 'x');
  });

  test('after it returns, mendwright waits for the popup to go away, as after an action', async () => {
    const fades = RELOGIN.replace('data-click-removes="#relogin"', 'data-click-removes="#relogin" data-click-removes-after="50"');
    const w = covered(fades);
    const heal = w.open();
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', { run: relogin(calls) });
    await heal.locator('email', '#email').fill('x');
    assert.equal(calls.length, 1);
    // The action came once the popup was gone, and was not intercepted.
    assert.equal(w.page.actions.filter(([verb, selector]) => verb === 'fill' && selector.endsWith('#email')).length, 1);
    assert.equal(valueOf(w, 'email'), 'x');
  });

  test('a callback it leaves behind is not inside it once it returned', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    const calls: unknown[] = [];
    let reopen!: () => void;
    const reopened = new Promise<void>((resolve) => (reopen = resolve));
    const later: { step?: Promise<void> } = {};
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        calls.push(page);
        // A timer runs in the async context it was set in, after the function returned.
        if (calls.length === 1) {
          setTimeout(async () => {
            await reopened;
            later.step = heal.locator('go', '#go').click();
          }, 0);
        }
        await page.fill('#user', 'ada');
        await page.click('#signin');
      },
    });
    await heal.locator('email', '#email').fill('x');
    w.page.document.body.insertAdjacentHTML('afterbegin', RELOGIN);   // the session expires again
    reopen();
    while (!later.step) await sleep(5);
    await later.step;   // a step as any other: it handles the popup first
    assert.equal(calls.length, 2);
    assert.deepEqual(heal.handledInterruptions.map((e) => [e.key, e.before]), [['relogin', 'email'], ['relogin', 'go']]);
  });

  test('one that hides the page is handled when the lookup misses, and nothing is healed', { timeout: 10_000 }, async () => {
    const w = world(html(RELOGIN, FORM.replace('<form id="signup">', '<form id="signup" data-fake-hidden>')));
    const model = new FakeModel(["//input[@name='email']"]);
    const heal = w.open({ provider: model });
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        await page.fill('#user', 'ada');
        await page.click('#signin');
        w.page.document.getElementById('signup')!.removeAttribute('data-fake-hidden');   // the site shows its form
      },
    });
    await heal.locator('email', '#email', { role: 'input' }).fill('ada@example.com');
    assert.equal(valueOf(w, 'email'), 'ada@example.com');
    assert.deepEqual(model.bundles, []);
    assert.deepEqual(heal.heals, []);
  });

  test('a popup it leaves on the page counts as handled, so the step stops after 3, naming it', { timeout: 20_000 },
    async () => {
      const w = world(html(STICKY, FORM.replace('<form id="signup">', '<form id="signup" data-fake-hidden>')));
      const model = new FakeModel(['//input']);
      const heal = w.open({ provider: model });
      const calls: unknown[] = [];
      heal.interrupt('relogin', '#relogin', { run: relogin(calls) });

      let caught: unknown;
      await heal.locator('email', '#email', { role: 'input' }).fill('x').catch((error) => (caught = error));

      assert.ok(caught instanceof InterruptionLoop, String(caught));
      assert.equal((caught as Error).message, loopMessage('email', 'relogin', 3, 'relogin'));
      assert.ok((caught as Error).message.endsWith("Check that relogin(), the run function registered with "
        + "heal.interrupt('relogin', ...), really closes it."));
      assert.equal(calls.length, 3);
      assert.equal(w.page.clicked.filter((el) => el.id === 'signin').length, 3);
      assert.equal(heal.handledInterruptions.length, 3);
      assert.deepEqual(model.bundles, []);
      assert.deepEqual(events(w).filter((e) => e.event === 'flow.stop'),
        [{ event: 'flow.stop', key: 'relogin', code: 'interruption_loop', before: 'email' }]);
    });

  test('a popup that stays over the form ends in the browser\'s own error, after one call before and one after', async () => {
    const w = covered(STICKY);
    const heal = w.open();
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', { run: relogin(calls) });
    await assert.rejects(heal.locator('email', '#email').fill('x'), /intercepts pointer events/);
    // Before the action, and once more after it was intercepted, then never again.
    assert.equal(calls.length, 2);
    assert.equal(w.page.actions.filter(([verb, selector]) => verb === 'fill' && selector.endsWith('#email')).length, 2);
    assert.equal(valueOf(w, 'email'), '');
  });
});

describe('what it throws', () => {
  test('is thrown as it is, after one WARNING and a flow.interrupt event with outcome failed', async () => {
    const w = covered(RELOGIN);
    const heal = w.open({ provider: new FakeModel(['//input']) });
    const calls: unknown[] = [];
    const error = Object.freeze(Object.assign(new Error('wrong password'), { name: 'ValueError' }));
    heal.interrupt('relogin', '#relogin', { run: relogin(calls, error) });

    let caught: unknown;
    await heal.locator('email', '#email').fill('ada@example.com').catch((e) => (caught = e));

    assert.equal(caught, error);
    assert.equal(calls.length, 1);
    assert.equal(valueOf(w, 'email'), '');   // the step was not tried
    assert.deepEqual(heal.handledInterruptions, []);
    assert.equal(runFailedMessage('relogin', 'email', 'relogin', error),
      "relogin(), the run function of interruption 'relogin', raised an error while mendwright handled it before "
      + "'email' (ValueError: wrong password). The error is raised as it is.");
    assert.deepEqual(warnings(w), [runFailedMessage('relogin', 'email', 'relogin', error)]);
    assert.deepEqual(events(w).filter((e) => e.event === 'flow.interrupt'), [{ event: 'flow.interrupt', key: 'relogin',
      before: 'email', action: 'run', selector: '#relogin', function: 'relogin', outcome: 'failed', error: 'ValueError' }]);
  });

  test('an AttentionRequired from it is not taken for a miss of the step: nothing is healed or skipped', async () => {
    const hidden = FORM.replace('<form id="signup">', '<form id="signup" data-fake-hidden>');
    for (const optional of [false, true]) {
      const w = world(html(RELOGIN, hidden));
      const model = new FakeModel(["//input[@name='email']"]);
      const heal = w.open({ provider: model });
      // Frozen: an error that cannot be marked is still told apart.
      const error = Object.freeze(new AttentionRequired('mendwright: the sign-in form changed'));
      heal.interrupt('relogin', '#relogin', { run: relogin([], error) });

      let caught: unknown;
      await heal.locator('email', '#email', { role: 'input', optional }).fill('x').catch((e) => (caught = e));

      assert.equal(caught, error, 'optional: ' + optional);
      assert.deepEqual(model.bundles, []);   // 'email' was not healed
      assert.deepEqual(events(w).map((e) => e.event), ['flow.interrupt'], 'optional: ' + optional);   // nor skipped
    }
  });

  test('a read whose check threw looks again on the next read', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    const calls: unknown[] = [];
    const error = new Error('wrong password');
    heal.interrupt('relogin', '#relogin', { run: relogin(calls, error) });
    const email = heal.locator('email', '#email');
    for (let i = 0; i < 2; i++) {
      let caught: unknown;
      await email.getAttribute('name').catch((e) => (caught = e));
      assert.equal(caught, error);
    }
    assert.equal(calls.length, 2);
  });

  test('a function that returns something that is not a promise throws TypeError when it is handled', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    heal.interrupt('relogin', '#relogin', { run: ((_page: Page) => undefined) as never });

    let caught: unknown;
    await heal.locator('email', '#email').fill('x').catch((e) => (caught = e));
    assert.ok(caught instanceof TypeError);
    assert.equal((caught as Error).message, runPlainMessage('relogin', '<anonymous>', undefined));
    assert.ok((caught as Error).message.startsWith("mendwright: <anonymous>(), the run function of interruption "
      + "'relogin', returned undefined, not a promise."));
    assert.deepEqual(events(w).map((e) => [e.outcome, e.error]), [['failed', 'TypeError']]);
    assert.equal(valueOf(w, 'email'), '');

    // A plain function that returns the promise is awaited.
    heal.interrupt('relogin', '#relogin', { run: (page: Page) => page.click('#signin') });
    await heal.locator('email', '#email').fill('x');
    assert.ok(gone(w, 'relogin'));
    assert.equal(valueOf(w, 'email'), 'x');
  });
});

describe('steps that run at the same time', () => {
  test('a step awaited next to the one that called it waits, and does not call it a second time', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    const calls: unknown[] = [];
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        calls.push(page);
        await sleep(50);   // a slow sign-in
        await page.fill('#user', 'ada');
        await page.click('#signin');
      },
    });
    await Promise.all([heal.locator('email', '#email').fill('ada@example.com'), heal.locator('go', '#go').click()]);
    assert.equal(calls.length, 1);
    const order = done(w);
    assert.deepEqual(order.slice(0, 2), ['fill #user', 'click #signin']);
    assert.deepEqual(order.slice(2).sort(), ['click #go', 'fill #email']);
  });

  test('a step in another context takes the mutex and waits until the run function is done', async () => {
    const w = covered(RELOGIN);
    const heal = w.open();
    let started!: () => void;
    const running = new Promise<void>((resolve) => (started = resolve));
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        started();
        await sleep(50);
        await page.fill('#user', 'ada');
        await page.click('#signin');
        w.page.calls.push(['run', 'relogin() returned']);
      },
    });
    const first = heal.locator('email', '#email').fill('ada@example.com');   // calls relogin() before its action
    await running;
    const second = heal.locator('go', '#go').click();                          // from outside relogin()
    await Promise.all([first, second]);
    const calls = w.page.calls.map(([what, on]) => what + ' ' + on);
    const returned = calls.indexOf('run relogin() returned');
    const looked = calls.findIndex((c) => /^count (css=)?#go$/.test(c));
    assert.ok(returned >= 0 && looked > returned, "'go' was looked up while relogin() ran: " + calls.join(', '));
  });

  test("a run function's own steps awaited together wait for a run function one of them called", async () => {
    // relogin() fills its two fields at once, and a one-time code popup over its form has a run
    // function of its own, which only one of them calls.
    const otp = '<div id="otp"><input id="code"><button id="otp-ok" data-click-removes="#otp">OK</button></div>';
    const w = world(html(otp, RELOGIN, FORM), { blocker: '#otp, #relogin' });
    const heal = w.open();
    const codes: unknown[] = [];
    heal.interrupt('relogin', '#relogin', {
      run: async function relogin(page: Page) {
        await Promise.all([page.fill('#user', 'ada'), page.fill('#password', 'not-a-real-password')]);
        await page.click('#signin');
      },
    });
    heal.interrupt('otp', '#otp', {
      run: async function enterCode(page: Page) {
        codes.push(page);
        await sleep(30);
        await page.fill('#code', '123456');
        await page.click('#otp-ok');
      },
    });
    await heal.locator('email', '#email').fill('ada@example.com');
    assert.equal(codes.length, 1);
    assert.ok(gone(w, 'otp') && gone(w, 'relogin'));
    assert.deepEqual(heal.handledInterruptions.map((e) => e.key), ['otp', 'relogin']);
    assert.equal(valueOf(w, 'email'), 'ada@example.com');
  });
});

describe('the texts and the journal lines are the Python package\'s', { skip: needsPython() }, () => {
  const PY = `
import json, sys
from mendwright.facade import ACTIONS, Healer
from mendwright.flow import handled_message, loop_message, run_failed_message, run_plain_message
from mendwright.interruptions import make_interruption
`;

  test('handled, loop, failed and removes texts', () => {
    const errors: [string, string][] = [['ValueError', 'wrong password'], ['LookupError', 'no password for ada in the test vault'],
      ['RuntimeError', ''], ['RuntimeError', '   '], ['TimeoutError', 'first line\nsecond line'],
      ['Exception', '  padded \r\n second'], ['Exception', 'x'.repeat(300)], ['Exception', 'a b'],
      ['Exception', 'a\x0bb'], ['Exception', 'été \u{1F600}'.repeat(60)]];
    const py = runPython<{ handled: string[]; loop: string[]; failed: string[]; removes: string }>(PY + `
d = json.load(sys.stdin)
failed = []
for name, message in d['errors']:
    error = type(name, (Exception,), {})(message)
    failed.append(run_failed_message('relogin', "it's", 'relogin', error))
def relogin(page):
    pass
try:
    Healer.interrupt(object.__new__(Healer), 'relogin', None, run=relogin)
except TypeError as exc:
    removes = str(exc)
print(json.dumps({
  'handled': [handled_message('relogin', 'email', 'relogin'), handled_message('cookies', 'email')],
  'loop': [loop_message('email', 'relogin', 3, 'relogin'), loop_message('email', 'nag', 3)],
  'failed': failed, 'removes': removes}))`, { errors });
    const ts = (text: string) => text.split('the run= function').join('the run function')
      .split('action="press", args=("Escape",) or').join("action: 'press', args: ['Escape'], or")
      .split("heal.interrupt('relogin', None)").join("heal.interrupt('relogin', null)")
      .split('with run=,').join('with run,');
    assert.deepEqual([handledMessage('relogin', 'email', 'relogin'), handledMessage('cookies', 'email')], py.handled.map(ts));
    assert.deepEqual([loopMessage('email', 'relogin', 3, 'relogin'), loopMessage('email', 'nag', 3)], py.loop.map(ts));
    const thrown = errors.map(([name, message]) => Object.assign(new Error(message), { name }));
    assert.deepEqual(thrown.map((e) => runFailedMessage('relogin', "it's", 'relogin', e)), py.failed.map(ts));
    assert.equal(runRemovesMessage('relogin'), ts(py.removes));
  });

  test('the argument checks of run', () => {
    // Python's spelling of each value, and TypeScript's.
    const cases: [string, unknown, unknown, string, unknown, unknown][] = [
      ['action and args', 'fill', ['ada'], "action='fill' and args=('ada',)", 'fill', ['ada']],
      ['action', 'click', null, "action='click'", 'click', undefined],
      ['args', null, 'Escape', "args='Escape'", undefined, 'Escape'],
    ];
    const py = runPython<{ given: string[]; callable: string }>(PY + `
def relogin(page):
    pass
def message(**kwargs):
    try:
        make_interruption('relogin', '#relogin', actions=ACTIONS, awaits=True, **kwargs)
    except TypeError as exc:
        return str(exc)
given = [message(action='fill', args=('ada',), run=relogin), message(action='click', run=relogin),
         message(args='Escape', run=relogin)]
print(json.dumps({'given': given, 'callable': message(run='relogin')}))`);
    cases.forEach(([what, , , pyGiven, action, args], i) => {
      let tsMessage = '';
      try {
        checkRun('relogin', async () => undefined, action, args);
      } catch (error) {
        tsMessage = (error as Error).message;
      }
      const tsGiven = what === 'action and args' ? "action: 'fill' and args: ['ada']"
        : what === 'action' ? "action: 'click'" : "args: 'Escape'";
      assert.equal(tsMessage, py.given[i].split('got run= and ' + pyGiven).join('got run and ' + tsGiven)
        .split('run= does the whole').join('run does the whole').split('no action= or args=: drop them, or drop run=.')
        .join('no action or args: drop them, or drop run.'), what);
    });
    let callable = '';
    try {
      checkRun('relogin', 'relogin', undefined, undefined);
    } catch (error) {
      callable = (error as Error).message;
    }
    assert.equal(callable, py.callable.split('..., run=...)').join('..., { run })')
      .split('such as def relogin(page): ...,').join('such as async function relogin(page) { ... },'));
  });

  test("the returned value's text, in TypeScript idiom", () => {
    const py = runPython<string[]>(PY + `
print(json.dumps([run_plain_message('relogin', '<lambda>', v) for v in (None, 3, 'done', True)]))`);
    const tsValues: unknown[] = [undefined, 3, 'done', true];
    py.forEach((text, i) => {
      const head = text.split(' On mendwright.aio.open()')[0];
      const ours = runPlainMessage('relogin', '<lambda>', tsValues[i]);
      assert.equal(ours.split(" heal.page's calls return promises")[0], head.split('the run= function').join('the run function')
        .replace(/returned (None|True), /, (_m, v: string) => 'returned ' + (v === 'None' ? 'undefined' : 'true') + ', ')
        .split('not an awaitable.').join('not a promise.'));
    });
    assert.equal(valueText(null), 'null');
    assert.equal(valueText({ a: [1, 'x'], 'b c': undefined }), "{ a: [1, 'x'] }");
    assert.equal(valueText('x'.repeat(100)), "'" + 'x'.repeat(56) + '...');
    const page = new (class Locator {})();
    assert.equal(valueText(page), '<Locator>');
    assert.equal(valueText([[[[1]]]]), '[[[...]]]');
    assert.equal(errorType('a string'), 'String');
    assert.equal(errorType(null), 'Error');
  });

  test('a flow.interrupt line for a run, handled and failed, is the one the Python Journal writes', async () => {
    const lines = async (fail?: unknown): Promise<string[]> => {
      const w = covered(RELOGIN);
      const file = path.join(w.dir, 'ts.jsonl');
      const heal: Healer = w.open({ journal: new Journal(file, { clock: () => 'T', run: null }) });
      heal.interrupt('relogin', '#relogin', { run: relogin([], fail) });
      await heal.locator('email', '#email').fill('x').catch(() => undefined);
      return fs.readFileSync(file, 'utf-8').split('\n').filter(Boolean);
    };
    const ts = [...(await lines()), ...(await lines(Object.assign(new Error('wrong password'), { name: 'ValueError' })))];
    const py = runPython<string[]>(`
import json, sys
from mendwright.flow import journal_event
from mendwright.journal import Journal
d = json.load(sys.stdin)
j = Journal(d['file'], clock=lambda: 'T', run=None)
journal_event(j, 'flow.interrupt', 'relogin', before='email', action='run', preset=None, selector='#relogin',
              function='relogin')
journal_event(j, 'flow.interrupt', 'relogin', before='email', action='run', selector='#relogin', function='relogin',
              outcome='failed', error='ValueError')
print(json.dumps(open(d['file'], encoding='utf-8').read().splitlines()))`, { file: path.join(tempDir(), 'py.jsonl') });
    assert.deepEqual(ts, py);
  });

  test("mendwright notify counts a handled one, and one whose function threw needs a human", async () => {
    process.env.MENDWRIGHT_RUN_ID = 'ts-run-relogin';
    const w = covered(RELOGIN);
    const heal = w.open();
    heal.interrupt('relogin', '#relogin', { run: relogin([]) });
    await heal.locator('email', '#email').fill('ada@example.com');
    w.page.document.body.insertAdjacentHTML('afterbegin', RELOGIN);   // the session expires again
    heal.interrupt('relogin', '#relogin', { run: relogin([], new Error('wrong password')) });
    await assert.rejects(heal.locator('go', '#go').click(), /wrong password/);
    const result = spawnSync(pythonCommand() as string, ['-m', 'mendwright', 'notify', '--store', w.storePath,
      '--run', 'ts-run-relogin', '--format', 'json', '--dry-run'], {
      cwd: REPO, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONUTF8: '1' },
    });
    assert.equal(result.status, 0, result.stderr);
    const data = JSON.parse(result.stdout);
    assert.equal(data.handled.interruptions, 1);
    assert.deepEqual(data.needs_human, [{ key: 'relogin', code: 'run_failed', about: 'interruption',
      text: 'could not be handled: its run= function raised an error' }]);
  });
});
