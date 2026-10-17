// The append-only event journal, journal.jsonl next to the store: one JSON object per line
// for each heal step (heal.requested, heal.proposed, heal.rejected, heal.applied,
// heal.canary, heal.quarantine) and flow change (flow.interrupt, flow.skip,
// flow.page_guard), in the format the Python package writes. A write that fails never
// breaks a heal. The journal holds selectors, reasons, timings and the model's reasoning,
// never page HTML.

import * as fs from 'node:fs';
import * as path from 'node:path';

import { nowIso } from './py';

export interface JournalLike {
  event(name: string, fields?: Record<string, unknown>): void;
}

/** Writes nothing. */
export class NullJournal implements JournalLike {
  event(): void {}
}

const FRAME = ['ts', 'event'];
const FIRST = ['key', 'provider', 'model', 'latency_ms', 'tokens'];

export class Journal implements JournalLike {
  readonly path: string;
  private readonly clock: () => string;

  constructor(file: string, options: { clock?: () => string } = {}) {
    this.path = file;
    this.clock = options.clock ?? (() => nowIso());
  }

  /** Append one event line. Fields that are null or undefined are left out; false and 0 are
   * kept. Never throws. */
  event(name: string, fields: Record<string, unknown> = {}): void {
    try {
      const record: Record<string, unknown> = { ts: this.clock(), event: name };
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
