// Each half of the gate on its own: the check against the captured page and the check
// against the live page, so a test fails when either one stops refusing.

import { strict as assert } from 'node:assert';
import { describe, test } from 'node:test';

import { PlaywrightDriver } from '../../src/driver';
import { NOT_LIVE, UNREADABLE, liveReason, validateStatic, xpathForms } from '../../src/gate';
import { FakePage } from '../support/fakepage';

const HTML = '<html><body><form><input id="a" name="email"><input id="b"><button id="c">Go</button>'
  + '<input type="checkbox" id="t" data-fake-hidden></form></body></html>';

function driver(html = HTML) {
  return new PlaywrightDriver(new FakePage(html) as never);
}

describe('the captured page', () => {
  test('one element of the role passes', async () => {
    assert.deepEqual(await validateStatic(driver(), "//input[@id='a']", HTML, 'input'), { ok: true, reason: 'ok', count: 1 });
  });

  test('refuses an index-only path, no match, several matches and another role', async () => {
    const d = driver();
    assert.equal((await validateStatic(d, '/html[1]/body[1]/form[1]/input[1]', HTML, 'input')).reason, 'brittle absolute-index path');
    assert.equal((await validateStatic(d, "//input[@id='zz']", HTML, 'input')).reason, 'no match');
    assert.equal((await validateStatic(d, '//input[not(@type)]', HTML, 'input')).reason, 'not unique (2)');
    assert.equal((await validateStatic(d, "//button[@id='c']", HTML, 'input')).reason, 'role mismatch: got button want input');
    assert.match((await validateStatic(d, '//input[', HTML, 'input')).reason, /^xpath error/);
  });

  test('refuses an element a quarantined selector reaches, whatever the spelling', async () => {
    const result = await validateStatic(driver(), "//input[@name='email']", HTML, 'input', ["//input[@id='a']"]);
    assert.deepEqual(result, { ok: false, reason: 'quarantined', count: 1 });
  });

  test('reads the page as captured, not the live page', async () => {
    const captured = '<html><body><input id="old"></body></html>';
    assert.equal((await validateStatic(driver(), "//input[@id='a']", captured, 'input')).reason, 'no match');
  });
});

describe('the live page', () => {
  test('one visible element of the role passes', async () => {
    assert.equal(await liveReason(driver(), "//input[@id='a']", { expectedRole: 'input' }), '');
  });

  test('refuses several, none, another role, and a hidden element', async () => {
    const d = driver();
    assert.equal(await liveReason(d, '//input[not(@type)]', { expectedRole: 'input' }), NOT_LIVE);
    assert.equal(await liveReason(d, "//input[@id='zz']", { expectedRole: 'input' }), NOT_LIVE);
    assert.equal(await liveReason(d, "//button[@id='c']", { expectedRole: 'input' }), 'live element role mismatch: got button want input');
    const hidden = driver(HTML.replace('<input id="a"', '<input data-fake-hidden id="a"'));
    assert.equal(await liveReason(hidden, "//input[@id='a']", { expectedRole: 'input' }), NOT_LIVE);
  });

  test('takes a hidden checkbox', async () => {
    assert.equal(await liveReason(driver(), "//input[@id='t']", { expectedRole: 'checkbox' }), '');
  });

  test('refuses an element it cannot read (fail closed)', async () => {
    const d = driver();
    (d as unknown as { describe: () => Promise<null> }).describe = async () => null;
    assert.equal(await liveReason(d, "//input[@id='a']", { expectedRole: 'input' }), UNREADABLE);
  });

  test('refuses on any error of the page (fail closed)', async () => {
    const d = driver();
    (d as unknown as { count: () => Promise<number> }).count = async () => { throw new Error('the page navigated'); };
    assert.equal(await liveReason(d, "//input[@id='a']", { expectedRole: 'input' }), NOT_LIVE);
  });
});

test('a proposal is stored as XPath, with the prefix only where it is needed', () => {
  assert.deepEqual(xpathForms("//input[@id='a']"), ["//input[@id='a']", "//input[@id='a']"]);
  assert.deepEqual(xpathForms("xpath=id('a')"), ["id('a')", "xpath=id('a')"]);
});
