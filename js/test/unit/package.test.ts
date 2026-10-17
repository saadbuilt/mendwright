// What the package publishes: its version in one place for code and npm, the entry points
// package.json names, and the browser code sent to the page as self-contained source.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import { mendwrightLib } from '../../src/browser/lib';
import * as api from '../../src/index';

const ROOT = path.resolve(__dirname, '..', '..', '..');
const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf-8'));

test('the version in the code is the one in package.json', () => {
  assert.equal(api.version, pkg.version);
});

test('package.json names the entry points the build makes', () => {
  assert.equal(pkg.name, 'mendwright');
  assert.deepEqual(Object.keys(pkg.exports), ['.', './test', './package.json']);
  assert.equal(pkg.exports['.'].default, './dist/index.js');
  assert.equal(pkg.exports['./test'].default, './dist/test.js');
  assert.deepEqual(pkg.dependencies ?? {}, {});
  for (const file of ['README.md', 'CHANGELOG.md', 'LICENSE']) assert.ok(fs.existsSync(path.join(ROOT, file)), file);
});

test('the browser code is self-contained: it runs from its source text alone', () => {
  const source = '(' + mendwrightLib.toString() + ')()';
  const lib = new Function('return ' + source)();
  assert.equal(typeof lib.roleOf, 'function');
  assert.equal(typeof lib.robustXpaths, 'function');
  // No helper the compiler adds, and nothing it reaches outside itself.
  assert.ok(!/__awaiter|__spreadArray|__assign|require\(|exports\./.test(source));
});

test('the public API', () => {
  for (const name of ['open', 'Healer', 'HealLocator', 'AttentionRequired', 'InvalidSelector', 'AnthropicProvider',
    'OpenAICompatibleProvider', 'LocalModelProvider', 'autoProvider', 'loadStore', 'SelectorStore', 'Journal']) {
    assert.ok(name in api, name);
  }
});
