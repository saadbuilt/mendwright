// The redacted heal bundle that goes to a model provider (Python's mendwright/capture.py).
//
// Redaction is best-effort. Before upload, quoted value="..." attributes are removed,
// absolute URLs become [URL] and runs of 12 to 19 digits become [redacted number], in the
// HTML, the broken selectors, the expected text, the intent and every string of the
// element inventory. Visible text and other attribute values (aria-label, title,
// placeholder, data-*) are sent as they are.
//
// The key's saved fingerprint goes into the bundle only for a provider that asks for it
// (wantsFingerprint), which LocalModelProvider does: a model on your own machine.

import { valid as validFingerprint } from './fingerprint';
import type { Bundle } from './prompt';
import type { KeyMeta } from './store';

const VALUE_ATTR = /\svalue=("[^"]*"|'[^']*')/g;
const URL = /https?:\/\/[^\s"'<>]+/g;
const LONGNUM = /(?<!\p{Nd})\p{Nd}{12,19}(?!\p{Nd})/gu;

function scrub(text: string): string {
  return text.replace(URL, '[URL]').replace(LONGNUM, '[redacted number]');
}

/** Drop value="..." attributes, then scrub URLs and long numbers. */
export function redact(text: unknown): string {
  if (typeof text !== 'string') return text as string;
  return scrub(text.replace(VALUE_ATTR, ''));
}

/** Redact every string of a JSON structure; a "value" key is dropped. */
export function redactTree(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(redactTree);
  if (node && typeof node === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node)) {
      if (k.toLowerCase() === 'value') continue;
      out[k] = redactTree(v);
    }
    return out;
  }
  if (typeof node === 'string') return redact(node);
  return node;
}

export interface BundleContext {
  url?: string | null;
  page?: string | null;
  lastPage?: string | null;
}

/** The bundle for one heal. html is the page as captured, cleaned already with
 * redact() before and after (see the coordinator). */
export function buildBundle(key: string, meta: KeyMeta, broken: string[], inventory: unknown[], html: string,
                            context: BundleContext, withFingerprint = false): Bundle {
  const text = Array.isArray(meta.expectedText) ? meta.expectedText : [meta.expectedText as unknown as string];
  const bundle: Bundle = {
    key,
    expectedRole: meta.expectedRole,
    expectedText: text.map((t) => (typeof t === 'string' ? redact(t) : t)),
    intent: typeof meta.intent === 'string' && meta.intent ? redact(meta.intent) : null,
    destructive: Boolean(meta.destructive),
    candidates: broken.filter((x) => typeof x === 'string' && x).map(redact),
    inventory: redactTree(inventory) as unknown[],
    html,
    url: scrub(redact(context.url || '')) || null,
    page: scrub(redact(context.page || '')) || null,
    lastPage: scrub(redact(context.lastPage || '')) || null,
    stage: null,
    version: null,
  };
  if (withFingerprint && validFingerprint(meta.fingerprint)) bundle.fingerprint = redactTree(meta.fingerprint);
  return bundle;
}
