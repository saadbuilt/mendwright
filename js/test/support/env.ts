// Test helpers: a fresh store directory, a log sink that records lines, a fake model, and
// the environment variables mendwright reads, unset for each test.

import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

import { open, type OpenOptions } from '../../src/healer';
import { resetLimiters } from '../../src/limits';
import type { HealProvider } from '../../src/providers/base';
import type { Bundle, Proposal } from '../../src/prompt';
import { FakePage, type FakePageOptions } from './fakepage';

const VARIABLES = ['MENDWRIGHT_STORE', 'MENDWRIGHT_TIMEOUT_MS', 'MENDWRIGHT_DRY_RUN', 'MENDWRIGHT_DISABLE',
  'MENDWRIGHT_FINGERPRINT', 'MENDWRIGHT_PROVIDER', 'MENDWRIGHT_GATEWAY', 'MENDWRIGHT_LOCAL_MODEL',
  'MENDWRIGHT_LOCAL_MIN_CONFIDENCE', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_BASE_URL', 'OPENAI_API_KEY',
  'OPENAI_MODEL', 'OPENAI_BASE_URL', 'GEMINI_API_KEY', 'OLLAMA_MODEL', 'OLLAMA_HOST',
  // The run id every journal line gets, which a CI job on GitHub Actions sets.
  'MENDWRIGHT_RUN_ID', 'GITHUB_ACTIONS', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'];

export function cleanEnv(): void {
  for (const name of VARIABLES) delete process.env[name];
  resetLimiters();
}

export function tempDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'mw-test-'));
}

export function journalOf(dir: string): Record<string, unknown>[] {
  const file = path.join(dir, 'journal.jsonl');
  if (!fs.existsSync(file)) return [];
  return fs.readFileSync(file, 'utf-8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

/** A model that answers with the proposals it was given, in order, and keeps what it was
 * sent. null answers "no proposal", with lastError set to error. */
export class FakeModel implements HealProvider {
  readonly model = 'fake-model';
  readonly bundles: Bundle[] = [];
  lastError: string | null = null;
  outcomes: [string, string, boolean][] = [];

  constructor(private readonly replies: (Proposal | string | null)[] = [], private readonly error: string | null = null) {}

  async propose(bundle: Bundle): Promise<Proposal | null> {
    this.bundles.push(JSON.parse(JSON.stringify(bundle)));
    const reply = this.replies.shift();
    if (reply === undefined || reply === null) {
      this.lastError = this.error;
      return null;
    }
    this.lastError = null;
    return typeof reply === 'string' ? { xpath: reply, fallbacks: [], reasoning: 'fake' } : reply;
  }

  reportOutcome(key: string, xpath: string, success: boolean): void {
    this.outcomes.push([key, xpath, success]);
  }
}

export interface World {
  dir: string;
  storePath: string;
  page: FakePage;
  lines: string[];
  open(options?: OpenOptions): ReturnType<typeof open>;
}

/** A fake page on html and a store in a new directory; world.open() opens a healer on them
 * with a short timeout and a log sink that keeps the lines. */
export function world(html: string, pageOptions: FakePageOptions = {}): World {
  const dir = tempDir();
  const storePath = path.join(dir, 'overrides.json');
  const page = new FakePage(html, pageOptions);
  const lines: string[] = [];
  return {
    dir, storePath, page, lines,
    open(options: OpenOptions = {}) {
      return open(page as never, {
        storePath, timeoutMs: 100, log: (level, message) => lines.push(level + ': ' + message), ...options,
      });
    },
  };
}
