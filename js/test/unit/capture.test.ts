// What goes to a hosted model: the page redacted, and the bundle without the fingerprint.

import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { buildBundle, redact, redactTree } from '../../src/capture';

test('redaction drops quoted value attributes, URLs and long numbers', () => {
  const html = '<input value="ada@example.com" name="e"><a href="https://shop.test/u/42?t=secret">x</a> card 4242424242424242';
  assert.equal(redact(html), '<input name="e"><a href="[URL]">x</a> card [redacted number]');
  assert.deepEqual(redactTree([{ value: 'typed', text: 'see https://x.test/a' }]), [{ text: 'see [URL]' }]);
});

test('the fingerprint goes into the bundle only for a model that asks for it', () => {
  const meta = { expectedRole: 'input', expectedText: [], destructive: false, maxCandidates: 5, lastPage: '/signup', pages: [],
    steps: [], intent: null, fingerprint: { v: 1, tag: 'input', attrs: { name: 'email' } }, listFingerprint: null };
  assert.ok(!('fingerprint' in buildBundle('email', meta, ['#email'], [], '<html></html>', { page: '/signup' })));
  const local = buildBundle('email', meta, ['#email'], [], '<html></html>', { page: '/signup' }, true);
  assert.deepEqual(local.fingerprint, meta.fingerprint);
});
