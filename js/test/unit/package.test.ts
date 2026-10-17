// What the package publishes: its version in one place for code and npm, the entry points
// package.json names, and the browser code sent to the page as self-contained source.

import { strict as assert } from 'node:assert';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { test } from 'node:test';

import * as ts from 'typescript';

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

test('mendwright/test has types under each moduleResolution a Playwright project can use', () => {
  // A project with this package in node_modules as npm installs it: package.json, the files
  // it lists, and in dist one .d.ts for each module of src, as the build makes them.
  const project = path.join(ROOT, 'a-project-that-is-not-there');
  const installed = path.join(project, 'node_modules', 'mendwright');
  const inPackage = (file: string): string | null => {
    const relative = path.relative(installed, path.resolve(file));
    if (relative.startsWith('..') || path.isAbsolute(relative)) return null;
    if (relative === '') return '';
    const top = relative.split(/[\\/]/)[0];
    return top === 'package.json' || (pkg.files as string[]).includes(top) ? relative : null;
  };
  const isFile = (file: string): boolean => {
    const relative = inPackage(file);
    if (relative === null) return false;
    const built = /^dist[\\/](.+)\.d\.ts$/.exec(relative);
    if (built) return fs.existsSync(path.join(ROOT, 'src', built[1] + '.ts'));
    return fs.existsSync(path.join(ROOT, relative)) && fs.statSync(path.join(ROOT, relative)).isFile();
  };
  const host: ts.ModuleResolutionHost = {
    fileExists: isFile,
    readFile: (file) => (isFile(file) && !/\.d\.ts$/.test(file) ? fs.readFileSync(path.join(ROOT, inPackage(file)!), 'utf-8') : undefined),
    directoryExists: (dir) => {
      const at = path.resolve(dir);
      if (at === project || at === path.dirname(installed)) return true;
      const relative = inPackage(at);
      return relative !== null && (relative === '' || relative === 'dist');
    },
  };
  const spec = path.join(project, 'a.spec.ts');
  const modes: [string, ts.CompilerOptions][] = [
    ['module commonjs (node10)', { module: ts.ModuleKind.CommonJS }],
    ['node10', { module: ts.ModuleKind.CommonJS, moduleResolution: ts.ModuleResolutionKind.Node10 }],
    ['node16', { module: ts.ModuleKind.Node16, moduleResolution: ts.ModuleResolutionKind.Node16 }],
    ['nodenext', { module: ts.ModuleKind.NodeNext, moduleResolution: ts.ModuleResolutionKind.NodeNext }],
    ['bundler', { module: ts.ModuleKind.ESNext, moduleResolution: ts.ModuleResolutionKind.Bundler }],
  ];
  for (const [name, options] of modes) {
    for (const [specifier, types] of [['mendwright/test', 'test.d.ts'], ['mendwright', 'index.d.ts']]) {
      const found = ts.resolveModuleName(specifier, spec, options, host).resolvedModule;
      assert.equal(found && path.relative(installed, found.resolvedFileName), path.join('dist', types), name + ': ' + specifier);
    }
  }
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
