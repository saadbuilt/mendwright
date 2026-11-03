// mendwright fix, the Python package's command, run from this repository on a TypeScript test:
// the heals this package made and confirmed are written into the strings of the .ts file, in
// the quotes each needs, comments are left alone, the file still parses, and a getBy* call is
// listed, never rewritten. This package has no command line, so its README tells a TypeScript
// team to run this one (pipx run mendwright fix --glob "*.ts").

import { strict as assert } from 'node:assert';
import { spawnSync } from 'node:child_process';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { beforeEach, describe, test } from 'node:test';

import * as ts from 'typescript';

import { cleanEnv, tempDir, world } from '../support/env';
import { REPO, needsPython, pythonCommand } from '../support/python';

beforeEach(cleanEnv);

const V1 = "<html><body><main><form id='signup' onsubmit='return false'>"
  + "<label for='email'>Email</label> <input id='email' name='email' type='email'>"
  + "<label><input type='checkbox' id='terms'> I accept the terms</label>"
  + "<button id='create' type='submit'>Create account</button></form></main></body></html>";
// A redesign: the field's and the checkbox's ids changed, and the button's name.
const V2 = "<html><body><main><form id='signup' onsubmit='return false'>"
  + "<label for='user-email'>Email</label> <input id='user-email' name='email' type='email'>"
  + "<label><input type='checkbox' id='accept-terms' data-testid='terms'> I accept the terms</label>"
  + "<button id='create' type='submit'>Sign up</button></form></main></body></html>";

const SPEC = `import { test, expect } from 'mendwright/test';

const EMAIL = '#email';

test('sign up', async ({ page, heal }) => {
  await page.goto('https://shop.test/signup');
  await page.fill('#email', 'ada@example.com');
  await heal.locator('terms', "//input[@id='terms']", { role: 'checkbox' }).check();
  // '#email' in a comment is not a selector
  await page.getByRole('button', { name: 'Create account' }).click();
  await expect(page.locator(EMAIL)).toBeVisible();
  const again = \`#email\`;
});
`;

/** python -m mendwright args, from cwd: [exit status, stdout, stderr]. */
function cli(args: string[], cwd: string): [number, string, string] {
  const command = pythonCommand();
  if (!command) throw new Error('no Python that imports mendwright from ' + REPO);
  const result = spawnSync(command, ['-m', 'mendwright', ...args], {
    cwd, encoding: 'utf-8', env: { ...process.env, PYTHONPATH: REPO, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  return [result.status ?? -1, result.stdout, result.stderr];
}

describe('mendwright fix on a TypeScript test', { skip: needsPython() }, () => {
  test('writes the heals this package confirmed into the strings of the .ts file, which still parses', async () => {
    const w = world(V1);
    for (const html of [V1, V2]) {
      w.page.goto(html);
      const heal = w.open({ healGetBy: true });
      await heal.page.fill('#email', 'ada@example.com');
      await heal.locator('terms', "//input[@id='terms']", { role: 'checkbox' }).check();
      await heal.page.getByRole('button', { name: 'Create account' }).click();
      heal.confirmAll(true, { by: 'test', test: 'signup.spec.ts:sign up' });
    }
    const project = tempDir();
    const spec = path.join(project, 'tests', 'signup.spec.ts');
    fs.mkdirSync(path.dirname(spec), { recursive: true });
    fs.writeFileSync(spec, SPEC);
    const store = ['--store', w.storePath, '--path', project, '--glob', '*.ts'];

    // Without --glob, mendwright fix looks at Python files only.
    const [plain, plainOut, plainErr] = cli(['fix', '--store', w.storePath, '--path', project], project);
    assert.equal(plain, 0, plainErr);
    assert.ok(!plainOut.includes('signup.spec.ts:'), plainOut);

    const [dry, diff, dryErr] = cli(['fix', ...store], project);
    assert.equal(dry, 0, dryErr);
    assert.equal(fs.readFileSync(spec, 'utf-8'), SPEC, 'a dry run changes nothing');
    const [applied, out, err] = cli(['fix', '--apply', ...store], project);
    assert.equal(applied, 0, err);
    const fixed = fs.readFileSync(spec, 'utf-8');
    assert.ok(diff.includes('--- a/tests/signup.spec.ts'), diff);

    // Each string whose whole value was the old selector, in the quotes the new one needs.
    assert.ok(fixed.includes(`const EMAIL = "//input[@id='user-email']";`), fixed);
    assert.ok(fixed.includes(`await page.fill("//input[@id='user-email']", 'ada@example.com');`), fixed);
    assert.ok(fixed.includes(`heal.locator('terms', "//input[@data-testid='terms']", { role: 'checkbox' })`), fixed);
    assert.ok(fixed.includes("const again = `//input[@id='user-email']`;"), fixed);
    // A comment is left alone, and so is the getBy* call, which is listed with its heal.
    assert.ok(fixed.includes("  // '#email' in a comment is not a selector"), fixed);
    assert.ok(fixed.includes("await page.getByRole('button', { name: 'Create account' }).click();"), fixed);
    assert.ok(out.includes('get_by_role("button", name="Create account")'), out);
    // The file still parses as TypeScript.
    const parsed = ts.transpileModule(fixed, { reportDiagnostics: true, compilerOptions: { module: ts.ModuleKind.CommonJS } });
    assert.deepEqual(parsed.diagnostics?.map((d) => ts.flattenDiagnosticMessageText(d.messageText, '\n')), []);
    // Once the code holds the new selectors, there is nothing left to write.
    const [again, rest, restErr] = cli(['fix', ...store], project);
    assert.equal(again, 0, restErr);
    assert.ok(rest.startsWith('mendwright fix: nothing to write back.'), rest);
  });
});
