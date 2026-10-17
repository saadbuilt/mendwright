// The heal prompt: the model request built from a capture bundle, and the model's reply
// read back as a proposal. The same text as the Python package's mendwright/prompt.py and
// the reference gateway (examples/heal_gateway.mjs), so every provider in either package
// sends the model the same request. A reply may say that no element on the page fits:
// {"xpath": null, "reason": "..."}; parseProposal() gives null for it, and notHereReason()
// its reason.

import { pyJson, pySlice, pyStr, pyTruthy } from './py';

export const MAX_PROMPT_PART = 60000;

export interface Bundle {
  key: string;
  expectedRole: string | null;
  expectedText: string[];
  intent: string | null;
  destructive: boolean;
  candidates: string[];
  inventory: unknown[];
  html: string;
  url: string | null;
  page: string | null;
  lastPage: string | null;
  stage: string | null;
  version: string | null;
  fingerprint?: unknown;
  rejectedXpath?: string | null;
  rejectReason?: string | null;
}

export interface Proposal {
  xpath: string;
  fallbacks: string[];
  reasoning: string;
}

export function buildPrompt(b: Partial<Bundle>): { system: string; user: string } {
  const destructive = pyTruthy(b.destructive);
  const expectedRole = b.expectedRole;
  const expectedText = pyTruthy(b.expectedText) ? b.expectedText : [];
  let system = 'You repair ONE broken web-automation xpath selector. Reply with ONLY a JSON object: '
    + '{"xpath":"...","fallbacks":["..."],"reasoning":"..."}. '
    + 'Prefer role + nearby visible label text + stable attributes (href, formcontrolname, type) '
    + 'over structure. NEVER return absolute positional paths like /div[3]/div[2]. '
    + "The xpath must match EXACTLY ONE element whose role is '" + pyStr(expectedRole) + "'. "
    + 'If no element on this page is the one the step means, for example because the step '
    + 'moved to another page or the element was removed, reply {"xpath":null,"reason":"..."} '
    + 'instead. Do not pick an element that is only similar.';
  if (destructive) {
    system += ' This is a DESTRUCTIVE control: its exact visible text MUST be one of ' + pyJson(expectedText) + '.';
  }
  const user = 'key: ' + pyStr(b.key)
    + '\nexpectedRole: ' + pyStr(expectedRole)
    + '\nexpectedText: ' + pyJson(expectedText)
    + (pyTruthy(b.intent) ? '\nintent: ' + pyStr(b.intent) : '')
    + '\ndestructive: ' + (destructive ? 'true' : 'false')
    + (pyTruthy(b.page) ? '\npage: ' + pyStr(b.page) : '')
    + (pyTruthy(b.lastPage) ? '\nlast found on page: ' + pyStr(b.lastPage) : '')
    + '\nbroken candidates:\n' + ((pyTruthy(b.candidates) ? b.candidates : []) as string[]).join('\n')
    + (pyTruthy(b.rejectReason)
      ? '\nYour previous proposal ' + pyStr(b.rejectedXpath) + ' was rejected: ' + pyStr(b.rejectReason)
        + '. Propose a DIFFERENT selector.'
      : '')
    + '\nDOM inventory (redacted):\n' + pySlice(pyJson(pyTruthy(b.inventory) ? b.inventory : []), MAX_PROMPT_PART)
    + '\nHTML snapshot (redacted):\n' + pySlice(pyStr(pyTruthy(b.html) ? b.html : ''), MAX_PROMPT_PART);
  return { system, user };
}

function replyObject(text: unknown): Record<string, unknown> | null {
  if (!text) return null;
  const match = String(text).match(/\{[\s\S]*\}/);
  if (!match) return null;
  let o: unknown;
  try {
    o = JSON.parse(match[0]);
  } catch {
    return null;
  }
  if (o === null || typeof o !== 'object' || Array.isArray(o)) return null;
  return o as Record<string, unknown>;
}

/** {xpath, fallbacks, reasoning} from a model reply (text around the JSON object is
 * allowed), or null unless it has a non-empty xpath. */
export function parseProposal(text: unknown): Proposal | null {
  const o = replyObject(text);
  if (o === null || !pyTruthy(o.xpath)) return null;
  return {
    xpath: pyStr(o.xpath),
    fallbacks: Array.isArray(o.fallbacks) ? o.fallbacks.slice(0, 4).map(pyStr) : [],
    reasoning: pyStr(pyTruthy(o.reasoning) ? o.reasoning : ''),
  };
}

/** The model's reason when its reply says no element on the page fits the step, else null. */
export function notHereReason(text: unknown): string | null {
  const o = replyObject(text);
  if (o === null || pyTruthy(o.xpath)) return null;
  if (typeof o.reason === 'string' && o.reason.trim()) return o.reason.trim();
  return null;
}
