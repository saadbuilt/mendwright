// The append-only event journal, journal.jsonl next to the store: one JSON object per line
// for each heal step (heal.requested, heal.proposed, heal.rejected, heal.applied,
// heal.canary, heal.quarantine, and heal.declined with a code when a heal was not tried or
// did not apply) and flow change (flow.interrupt, flow.skip, flow.page_guard, and flow.stop
// when a step cannot go on), in the format the Python package writes. A write that fails
// never breaks a heal. The journal holds selectors, reasons, timings and the model's
// reasoning, never page HTML.
//
// Every line carries the id of the run that wrote it in run, when one is known (see runId):
// MENDWRIGHT_RUN_ID, or on GitHub Actions gh-<run id>.<attempt>, as the Python package
// gives it, so the Python package's mendwright notify picks the events of one run whichever
// package wrote them.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { nowIso, pyRepr } from './py';

// A run id: 1 to 100 letters, digits, '.', '_', ':', '/' or '-', starting with a letter or
// digit (the Python package's journal.RUN_ID). ^ and $ without the m flag match the whole
// text only, so a trailing newline is not taken for the end of it.
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/;
const DIGITS = /^[0-9]+$/;

/** True when value is a run id mendwright takes. */
export function validRunId(value: unknown): value is string {
  return typeof value === 'string' && RUN_ID.test(value);
}

/** The run id journal events get, where it came from ('MENDWRIGHT_RUN_ID', 'github' or
 * null), and MENDWRIGHT_RUN_ID's value when it is set but not a run id (else null), which is
 * then ignored as if unset. MENDWRIGHT_RUN_ID comes first. On GitHub Actions (GITHUB_ACTIONS
 * is 'true' and GITHUB_RUN_ID is all digits) the id is gh-<GITHUB_RUN_ID>.<GITHUB_RUN_ATTEMPT>,
 * attempt 1 when GITHUB_RUN_ATTEMPT is missing or not digits. */
export function runId(env: Record<string, string | undefined> = process.env):
    { id: string | null; source: string | null; bad: string | null } {
  let bad: string | null = null;
  const value = env.MENDWRIGHT_RUN_ID || '';
  if (value) {
    if (validRunId(value)) return { id: value, source: 'MENDWRIGHT_RUN_ID', bad: null };
    bad = value;
  }
  const number = env.GITHUB_RUN_ID || '';
  if (env.GITHUB_ACTIONS === 'true' && DIGITS.test(number)) {
    const attempt = env.GITHUB_RUN_ATTEMPT || '';
    const tries = DIGITS.test(attempt) ? BigInt(attempt).toString() : '1';
    return { id: 'gh-' + BigInt(number).toString() + '.' + tries, source: 'github', bad };
  }
  return { id: null, source: null, bad };
}

/** What is wrong with value, a MENDWRIGHT_RUN_ID that is not a run id. open() logs it. */
export function runIdProblem(value: string): string {
  const shown = value.length <= 120 ? value : value.slice(0, 120) + '...';
  return "MENDWRIGHT_RUN_ID must be 1 to 100 letters, digits, '.', '_', ':', '/' or '-', starting with a letter or "
    + 'digit, got ' + pyRepr(shown) + '. It is ignored, so journal events get no run id from it.';
}

export interface JournalLike {
  event(name: string, fields?: Record<string, unknown>): void;
}

/** Writes nothing. */
export class NullJournal implements JournalLike {
  event(): void {}
}

// ts, event and run frame the line; a field of the same name never overwrites them.
const FRAME = ['ts', 'event', 'run'];
const FIRST = ['key', 'provider', 'model', 'latency_ms', 'tokens'];

export class Journal implements JournalLike {
  readonly path: string;
  /** The run id each line gets in run, or null for none. */
  readonly run: string | null;
  private readonly clock: () => string;

  /** run: the run id of every line; null for none; left out, the one runId() gives. */
  constructor(file: string, options: { clock?: () => string; run?: string | null } = {}) {
    this.path = file;
    this.clock = options.clock ?? (() => nowIso());
    if (options.run === undefined) {
      this.run = runId().id;
    } else {
      if (options.run !== null && !validRunId(options.run)) {
        throw new TypeError("mendwright: Journal's run takes null or a run id: 1 to 100 letters, digits, '.', '_', "
          + "':', '/' or '-', starting with a letter or digit, got " + pyRepr(options.run) + '.');
      }
      this.run = options.run;
    }
  }

  /** Append one event line. Fields that are null or undefined are left out; false and 0 are
   * kept. Never throws. */
  event(name: string, fields: Record<string, unknown> = {}): void {
    try {
      const record: Record<string, unknown> = { ts: this.clock(), event: name };
      if (this.run) record.run = this.run;
      for (const field of FIRST) {
        if (fields[field] !== undefined && fields[field] !== null) record[field] = fields[field];
      }
      for (const [field, value] of Object.entries(fields)) {
        if (value === undefined || value === null || FRAME.includes(field) || FIRST.includes(field)) continue;
        record[field] = value;
      }
      const line = JSON.stringify(record) + '\n';
      fs.mkdirSync(path.dirname(this.path), { recursive: true });
      // One write per line on a file opened for appending, so lines from several processes
      // (Playwright Test workers) never overwrite each other.
      fs.appendFileSync(this.path, line, { encoding: 'utf-8', mode: 0o600 });
    } catch {
      // A heal must never fail because its journal could not be written.
    }
  }
}
