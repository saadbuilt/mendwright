// Run the Python package from this repository, for the tests that check the two packages
// agree: the same scores, the same prompt and task text, and one store file both read and
// write. The Python side gets its input as JSON on stdin and prints JSON.
//
// Without a Python that can import mendwright (and lxml), those tests are skipped, unless
// MENDWRIGHT_REQUIRE_PYTHON=1 says they must run (CI sets it), and then they fail.

import { spawnSync } from 'node:child_process';
import * as path from 'node:path';

export const REPO = path.resolve(__dirname, '..', '..', '..', '..');

let found: string | null | undefined;

function candidates(): string[] {
  const own = process.env.MENDWRIGHT_PYTHON;
  return own ? [own] : ['python3', 'python'];
}

/** The Python command that imports mendwright from this repository, or null. */
export function pythonCommand(): string | null {
  if (found !== undefined) return found;
  found = null;
  for (const command of candidates()) {
    const result = spawnSync(command, ['-c', 'import lxml, mendwright, sys; print(mendwright.__file__)'], {
      cwd: REPO, env: { ...process.env, PYTHONPATH: REPO }, encoding: 'utf-8',
    });
    if (result.status === 0 && path.resolve(result.stdout.trim()).startsWith(path.join(REPO, 'mendwright'))) {
      found = command;
      break;
    }
  }
  return found;
}

/** The skip option for a test that needs Python: false to run it, else the reason. */
export function needsPython(): false | string {
  if (pythonCommand()) return false;
  if (process.env.MENDWRIGHT_REQUIRE_PYTHON === '1') return false;   // run, and fail in runPython()
  return 'no Python that imports mendwright from this repository (set MENDWRIGHT_PYTHON)';
}

/** Run code with input as JSON on stdin (read it with json.load(sys.stdin)), and return
 * what it prints as JSON. */
export function runPython<T = unknown>(code: string, input: unknown = null, options: { cwd?: string } = {}): T {
  const command = pythonCommand();
  if (!command) throw new Error('no Python that imports mendwright from ' + REPO + ' was found, and MENDWRIGHT_REQUIRE_PYTHON=1');
  const result = spawnSync(command, ['-c', code], {
    cwd: options.cwd ?? REPO, input: JSON.stringify(input), encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024,
    env: { ...process.env, PYTHONPATH: REPO, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  });
  if (result.status !== 0) throw new Error('python failed (' + result.status + '):\n' + result.stderr);
  return JSON.parse(result.stdout) as T;
}
