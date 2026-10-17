// The task the local selector model was trained on (Python's mendwright/candidates.py): the
// broken step and a numbered list of at most MAX_CANDIDATES elements of its role, read from
// the captured page. The model answers {"pick": <n>}, or {"pick": null, "why": "..."} when no
// element on the page is the one the step means. mendwright builds the XPath for the picked
// element itself, never from its position.
//
// The page is read in the browser (browser/lib.ts: candidates() and robustXpaths()); this
// module ranks the elements when there are too many and writes the task, so the model sees
// the same text the Python package gives it for the same page.

import { fromRaw, score, valid } from '../fingerprint';
import { pyJson, pyLen, pyRstrip, pySlice, pyStrip } from '../py';
import type { Bundle } from '../prompt';

export const MAX_CANDIDATES = 40;

export const SYSTEM_PROMPT = 'Pick the candidate that is the same control the broken step used, even if its id, '
  + 'classes, text, language or place changed. Reply with JSON only: {"pick": <number>}, '
  + 'or {"pick": null, "why": "removed"} when it is not on this page, "ambiguous" when '
  + 'several fit equally, "moved" when the step now belongs to another page. Never pick a '
  + 'control that is only similar: another action, another row or item, or a destructive '
  + 'one.';

export const WHY: Record<string, string> = {
  removed: 'the element is not on this page',
  ambiguous: 'several elements fit equally and nothing tells them apart',
  moved: 'the step seems to have moved to another page',
};

export interface PageCandidate {
  index: number;
  features: Record<string, string>;
  line: string;
  raw: unknown;
}

export interface Candidate {
  number: number;
  index: number;
  features: Record<string, string>;
  line: string;
}

export interface Candidates {
  items: Candidate[];
  total: number;
  role: string | null;
  fingerprintLine: string;
}

const WORD = /[a-z0-9]+/g;
const CAMEL = /([a-z0-9])([A-Z])/g;
const LITERAL = /'([^']*)'|"([^"]*)"/g;
const NOISE = new Set(['the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'normalize', 'space',
  'contains', 'text', 'xpath', 'css', 'class', 'concat', 'div', 'span', 'id', 'name', 'type', 'data',
  'testid', 'aria', 'label', 'btn', 'button', 'input', 'link']);

/** Lower-case word tokens; camelCase and snake/kebab case are split. */
export function tokens(text: unknown): string[] {
  if (!text) return [];
  return String(text).replace(CAMEL, '$1 $2').toLowerCase().match(WORD) ?? [];
}

function selectorWords(selector: string): string[] {
  const words: string[] = [];
  for (const m of (selector || '').matchAll(LITERAL)) words.push(...tokens(m[1] || m[2]));
  const bare = (selector || '').replace(LITERAL, ' ');
  for (const m of bare.matchAll(/[#.]([A-Za-z][\w-]*)/g)) words.push(...tokens(m[1]));
  return words;
}

/** The words that describe the step, for ranking candidates when there are too many. */
export function stepWords(bundle: Partial<Bundle>): Set<string> {
  const words = [...tokens(bundle.key)];
  for (const sel of bundle.candidates ?? []) words.push(...selectorWords(sel));
  for (const text of bundle.expectedText ?? []) words.push(...tokens(text));
  words.push(...tokens(bundle.intent));
  const fp = bundle.fingerprint;
  if (fp && typeof fp === 'object' && !Array.isArray(fp)) {
    for (const [key, value] of Object.entries(fp)) {
      if (typeof value === 'string' && !['tag', 'region', 'role'].includes(key)) words.push(...tokens(value));
    }
  }
  return new Set(words.filter((w) => !NOISE.has(w) && w.length > 1));
}

function rank(features: Record<string, string>, words: Set<string>, fpRegion: string | null): number {
  const have = new Set<string>();
  for (const [key, value] of Object.entries(features)) {
    if (['tag', 'region', 'hidden', 'disabled', 'current'].includes(key)) continue;
    for (const t of tokens(value)) have.add(t);
  }
  let s = 0.0;
  for (const w of words) if (have.has(w)) s += 1.0 + Math.min(w.length, 10) / 10.0;
  for (const w of words) {
    if (have.has(w) || w.length < 4) continue;
    if ([...have].some((h) => h.length >= 4 && (h.startsWith(w.slice(0, 4)) || w.startsWith(h.slice(0, 4))))) s += 0.4;
  }
  const region = features.region || '';
  const lastPart = (r: string) => r.split('>').pop()!.split('#')[0];
  if (fpRegion && region === fpRegion) s += 1.0;
  else if (fpRegion && lastPart(region) === lastPart(fpRegion)) s += 0.5;
  if (features.hidden === 'hidden') s -= 0.3;
  return s;
}

/** The candidate list from the page's elements of the role: all of them when they fit, else
 * the limit that share the most with the step, in page order, numbered from 1. */
export function chooseCandidates(bundle: Partial<Bundle>, found: PageCandidate[], fingerprintLine: string,
                                 fingerprintRegion: string | null, limit = MAX_CANDIDATES): Candidates {
  let kept = found;
  if (found.length > limit) {
    const words = stepWords(bundle);
    const fp = bundle.fingerprint;
    const similar = found.map((item) => {
      if (!valid(fp)) return 0.0;
      const next = fromRaw(item.raw);
      return next ? score(fp, next).value : 0.0;
    });
    const ranked = found.map((item, i) => ({ i, s: rank(item.features, words, fingerprintRegion) + 3.0 * similar[i] }))
      .sort((a, b) => b.s - a.s || a.i - b.i).slice(0, limit).map((x) => x.i).sort((a, b) => a - b);
    kept = ranked.map((i) => found[i]);
  }
  return {
    items: kept.map((item, n) => ({ number: n + 1, index: item.index, features: item.features, line: item.line })),
    total: found.length,
    role: bundle.expectedRole ?? null,
    fingerprintLine,
  };
}

function clip(text: string, limit: number): string {
  const t = pyStrip(text.replace(/[ \t\r\n]+/g, ' '));
  if (pyLen(t) <= limit) return t;
  return pyRstrip(pySlice(t, limit - 1)) + '\u2026';
}

function pageLine(bundle: Partial<Bundle>): string {
  const page = bundle.page;
  const last = bundle.lastPage;
  if (page && last) {
    if (page === last) return 'page: ' + page + ' (where it was last found)';
    return 'page: ' + page + ' (it was last found on ' + last + ')';
  }
  if (page) return 'page: ' + page;
  if (last) return 'page: unknown (it was last found on ' + last + ')';
  return '';
}

function plural(role: string, n: number): string {
  const word = role || 'element';
  if (n === 1) return word;
  if (word.endsWith('x') || word.endsWith('s')) return word + 'es';
  return word + 's';
}

/** The (system, user) messages for one heal. */
export function formatTask(bundle: Partial<Bundle>, candidates: Candidates): [string, string] {
  const role = bundle.expectedRole || 'unknown';
  const lines = ['Step', 'key: ' + String(bundle.key ?? '')];
  lines.push('role: ' + role);
  for (const sel of (bundle.candidates ?? []).slice(0, 3)) lines.push('old selector: ' + clip(String(sel), 160));
  const texts = (bundle.expectedText ?? []).filter((t) => typeof t === 'string' && t.trim());
  if (texts.length) lines.push('expected text: ' + texts.slice(0, 3).map((t) => pyJson(clip(t, 60), false)).join(', '));
  if (typeof bundle.intent === 'string' && bundle.intent.trim()) lines.push('intent: ' + clip(bundle.intent, 120));
  if (candidates.fingerprintLine) lines.push('it was: ' + candidates.fingerprintLine);
  const page = pageLine(bundle);
  if (page) lines.push(page);
  lines.push('');
  const n = candidates.items.length;
  let head = 'Candidates (' + n + ' ' + plural(role, n);
  if (candidates.total > n) head += ', the ' + n + ' closest of ' + candidates.total;
  lines.push(head + ')');
  if (!n) lines.push('(none)');
  for (const c of candidates.items) lines.push(c.number + '. ' + c.line);
  return [SYSTEM_PROMPT, lines.join('\n')];
}

/** The model's reply as {pick, why}, or null when it is not a JSON object with a "pick"
 * that is a positive whole number or null. */
export function parseAnswer(text: unknown): { pick: number | null; why: string | null } | null {
  if (typeof text !== 'string') return null;
  const match = text.match(/\{[\s\S]*?\}/);
  if (!match) return null;
  let obj: unknown;
  try {
    obj = JSON.parse(match[0]);
  } catch {
    return null;
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj) || !('pick' in obj)) return null;
  let pick = (obj as Record<string, unknown>).pick;
  const whyRaw = (obj as Record<string, unknown>).why;
  const why = typeof whyRaw === 'string' && whyRaw.trim() ? whyRaw.trim() : null;
  if (pick === null) return { pick: null, why: why || 'removed' };
  if (typeof pick === 'boolean') return null;
  // 2.0 is a float to Python's json, not a pick.
  if (typeof pick === 'number' && !/"pick"\s*:\s*\d+\s*[,}]/.test(match[0])) return null;
  if (typeof pick === 'string' && /^\d+$/.test(pick.trim())) pick = Number(pick.trim());
  if (typeof pick !== 'number' || !Number.isInteger(pick) || pick < 1) return null;
  return { pick, why: null };
}

/** How sure the model was of the value it gave for "pick", from the per-token log
 * probabilities Ollama returns with logprobs: true. null when they are not there. */
export function answerConfidence(logprobs: unknown): number | null {
  if (!Array.isArray(logprobs) || !logprobs.length) return null;
  let text = '';
  let total: number | null = null;
  for (const item of logprobs) {
    if (!item || typeof item !== 'object' || typeof (item as any).token !== 'string') return null;
    const token: string = (item as any).token;
    const value = (item as any).logprob;
    if (typeof value !== 'number') return null;
    if (total === null) {
      text += token;
      const key = text.indexOf('"pick"');
      if (key >= 0 && text.slice(key + 6).includes(':')) {
        total = value;
        const rest = text.slice(key + 6).split(':').slice(1).join(':');
        if (/[,}]/.test(rest)) break;
      }
      continue;
    }
    if (/[,}]/.test(token)) {
      if (/^\s*(?:\d+|null)/.test(token)) total += value;
      break;
    }
    total += value;
  }
  return total === null ? null : Math.exp(total);
}
