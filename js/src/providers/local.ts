// LocalModelProvider: mendwright's own small selector model, served by Ollama on your
// machine (Python's providers/local.py). The model does not write an XPath: mendwright lists
// at most 40 elements of the step's role from the captured page and asks the model which
// one the step meant, as {"pick": <n>}, or {"pick": null, "why": "..."} when none is. It then
// builds a unique XPath for the picked element itself, from its id, test id, name,
// aria-label, label, text or the row or heading around it, never from its position, and the
// result goes through the same gate as any proposal.
//
// When the gate refuses a pick, the coordinator asks once more. The provider then asks the
// model again without that element only when the reason says the element was not uniquely
// present or not visible on the live page; for any other reason it does not guess a second
// time, so a wrong first pick cannot turn into a wrong second one. A pick the model is less
// than minConfidence sure of (from the token probabilities Ollama returns) is declined.
//
// This is the only provider that gets the key's fingerprint, and nothing leaves the machine
// unless baseUrl points elsewhere.

import { NOT_LIVE } from '../gate';
import type { Bundle, Proposal } from '../prompt';
import type { HealProvider, ProviderTools } from './base';
import {
  MAX_CANDIDATES, WHY, answerConfidence, chooseCandidates, formatTask, parseAnswer,
  type Candidates, type PageCandidate,
} from './candidates';
import { Describer, NOT_HERE_HINT, clip, sentence } from './errors';
import { fetchHttp, parseJson, type Http } from './http';

export const LOCAL_MODEL = 'mendwright-selector';
export const LOCAL_BASE_URL = 'http://127.0.0.1:11434';
export const MIN_CONFIDENCE = 0.8;
const RETRY_REASONS = [NOT_LIVE, 'not unique'];

/** OLLAMA_HOST as a plain http://host:port: a missing scheme added, an OpenAI-style /v1
 * dropped, and localhost or 0.0.0.0 read as 127.0.0.1. */
export function ollamaHost(value: string | undefined | null): string {
  let text = (value || '').trim().replace(/\/+$/, '');
  if (!text) return LOCAL_BASE_URL;
  if (!text.includes('://')) text = 'http://' + text;
  if (text.endsWith('/v1')) text = text.slice(0, -3);
  const [scheme, rest] = [text.slice(0, text.indexOf('://')), text.slice(text.indexOf('://') + 3)];
  const slash = rest.indexOf('/');
  const hostport = slash < 0 ? rest : rest.slice(0, slash);
  const tail = slash < 0 ? '' : rest.slice(slash);
  const colon = hostport.indexOf(':');
  let host = colon < 0 ? hostport : hostport.slice(0, colon);
  const portText = colon < 0 ? '' : hostport.slice(colon + 1);
  if (['localhost', '0.0.0.0'].includes(host.toLowerCase())) host = '127.0.0.1';
  return (scheme + '://' + host + ':' + (portText || '11434') + tail).replace(/\/+$/, '');
}

/** MENDWRIGHT_LOCAL_MIN_CONFIDENCE as a number from 0 to 1, else MIN_CONFIDENCE. */
export function envConfidence(): number {
  const text = (process.env.MENDWRIGHT_LOCAL_MIN_CONFIDENCE || '').trim();
  if (!text) return MIN_CONFIDENCE;
  const value = Number(text);
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    throw new TypeError('MENDWRIGHT_LOCAL_MIN_CONFIDENCE must be a number from 0 to 1, got ' + JSON.stringify(text));
  }
  return value;
}

const HINTS = {
  model: "Install it with 'mendwright model install' (the Python package's command), or set "
    + 'MENDWRIGHT_LOCAL_MODEL to the name you gave it in Ollama.',
  refused: "Is Ollama running? Start it with 'ollama serve', or set OLLAMA_HOST (or baseUrl) to where it listens.",
  connect: 'Check OLLAMA_HOST (or baseUrl) and that Ollama is running.',
  timeout: 'The first call loads the model and can take a while; retry, or pass a larger timeoutMs to LocalModelProvider.',
  reply: 'Check that MENDWRIGHT_LOCAL_MODEL names the mendwright selector model, not a general chat model.',
};

export interface LocalOptions {
  model?: string;
  baseUrl?: string;
  minConfidence?: number;
  timeoutMs?: number;
  numCtx?: number;
  keepAlive?: string | null;
  maxCandidates?: number;
  http?: Http;
}

export class LocalModelProvider implements HealProvider {
  readonly wantsFingerprint = true;
  readonly model: string;
  readonly baseUrl: string;
  readonly minConfidence: number;
  readonly timeoutMs: number;
  lastError: string | null = null;
  /** What the model answered last time, for logs and tests. */
  lastAnswer: { pick: number | null; why: string | null; candidates: number; confidence: number | null } | null = null;
  private readonly numCtx: number;
  private readonly keepAlive: string | null;
  private readonly maxCandidates: number;
  private readonly http: Http;

  constructor(options: LocalOptions = {}) {
    this.model = options.model || process.env.MENDWRIGHT_LOCAL_MODEL || LOCAL_MODEL;
    this.baseUrl = ollamaHost(options.baseUrl || process.env.OLLAMA_HOST);
    this.minConfidence = options.minConfidence ?? envConfidence();
    this.timeoutMs = options.timeoutMs ?? 60_000;
    this.numCtx = options.numCtx ?? 4096;
    this.keepAlive = options.keepAlive === undefined ? '10m' : options.keepAlive;
    this.maxCandidates = options.maxCandidates ?? MAX_CANDIDATES;
    this.http = options.http ?? fetchHttp;
  }

  private errors(): Describer {
    return new Describer('Ollama', { url: this.baseUrl, hints: HINTS });
  }

  private async extract(bundle: Bundle, tools: ProviderTools): Promise<Candidates> {
    const found = await tools.lib<{ items: PageCandidate[]; fingerprintLine: string; fingerprintRegion: string | null }>(
      'candidates', { html: bundle.html, role: bundle.expectedRole, fingerprint: bundle.fingerprint ?? null });
    return chooseCandidates(bundle, found.items, found.fingerprintLine, found.fingerprintRegion, this.maxCandidates);
  }

  /** cands without the element xpath selects, renumbered; null when xpath does not select
   * exactly one listed element. */
  private async without(cands: Candidates, bundle: Bundle, xpath: string, tools: ProviderTools): Promise<Candidates | null> {
    let index: number | null;
    try {
      index = await tools.lib<number | null>('indexOfMatch', { html: bundle.html, xpath });
    } catch {
      return null;
    }
    if (index === null || index < 0 || !cands.items.some((c) => c.index === index)) return null;
    const kept = cands.items.filter((c) => c.index !== index).map((c, n) => ({ ...c, number: n + 1 }));
    return { ...cands, items: kept, total: Math.max(0, cands.total - 1) };
  }

  /** Never throws: a failure gives null and a reason in lastError. */
  async propose(bundle: Bundle, tools?: ProviderTools): Promise<Proposal | null> {
    this.lastError = null;
    this.lastAnswer = null;
    if (!tools) {
      this.lastError = sentence('the local model needs the page to read its candidates, and none was given');
      return null;
    }
    let cands: Candidates;
    try {
      cands = await this.extract(bundle, tools);
    } catch (error) {
      this.lastError = sentence('could not read the captured page (' + clip(String((error as Error)?.message ?? error), 120) + ')');
      return null;
    }
    const rejected = bundle.rejectedXpath;
    if (rejected) {
      const reason = String(bundle.rejectReason ?? '');
      if (!RETRY_REASONS.some((r) => reason.startsWith(r))) {
        this.lastError = sentence("the local model's pick was refused by mendwright's checks (" + clip(reason, 120)
          + '), and it does not guess a second time');
        return null;
      }
      const left = await this.without(cands, bundle, rejected, tools);
      if (left === null) {
        this.lastError = sentence("the local model's pick was refused by mendwright's checks (" + clip(reason, 120)
          + '), and it could not be told apart from the others');
        return null;
      }
      cands = left;
    }
    if (!cands.items.length) {
      this.lastError = sentence('the local model says no element on this page fits: the page has no '
        + (bundle.expectedRole || 'matching') + ' element', NOT_HERE_HINT);
      return null;
    }
    const [system, user] = formatTask(bundle, cands);
    const reply = await this.chat(system, user);
    if (reply === null) return null;
    const [content, confidence] = reply;
    const answer = parseAnswer(content);
    if (answer === null) {
      this.lastError = sentence('the local model reply did not say which element it picked: ' + clip(content, 80), HINTS.reply);
      return null;
    }
    this.lastAnswer = { pick: answer.pick, why: answer.why, candidates: cands.items.length, confidence };
    if (answer.pick === null) {
      const why = answer.why || 'removed';
      this.lastError = sentence('the local model says no element on this page fits: ' + (WHY[why] ?? clip(why, 80)), NOT_HERE_HINT);
      return null;
    }
    const number = answer.pick;
    if (confidence !== null && confidence < this.minConfidence) {
      const picked = number <= cands.items.length ? cands.items[number - 1].line : String(number);
      this.lastError = sentence('the local model was not sure which element the step meant (it picked ' + clip(picked, 80)
        + ' with confidence ' + confidence.toFixed(2) + ', below ' + this.minConfidence.toFixed(2) + ')', NOT_HERE_HINT);
      return null;
    }
    if (number > cands.items.length) {
      this.lastError = sentence('the local model picked candidate ' + number + ', but there are only ' + cands.items.length,
        HINTS.reply);
      return null;
    }
    const cand = cands.items[number - 1];
    let xpaths: string[] = [];
    try {
      xpaths = await tools.lib<string[]>('robustXpaths', { html: bundle.html, index: cand.index, limit: 4 });
    } catch {
      xpaths = [];
    }
    if (!Array.isArray(xpaths) || !xpaths.length) {
      this.lastError = sentence('the local model picked ' + clip(cand.line, 100) + ', but mendwright could not build a '
        + 'unique XPath for it that does not depend on its position');
      return null;
    }
    return { xpath: xpaths[0], fallbacks: xpaths.slice(1),
             reasoning: 'local model picked candidate ' + number + ' of ' + cands.items.length + ': ' + clip(cand.line, 160) };
  }

  reportOutcome(): void {
    // Nothing to report to: the journal and the store keep the outcome.
  }

  private async chat(system: string, user: string): Promise<[string, number | null] | null> {
    const body: Record<string, unknown> = {
      model: this.model, stream: false, format: 'json', logprobs: true,
      messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
      options: { temperature: 0, num_ctx: this.numCtx, num_predict: 24 },
    };
    if (this.keepAlive !== null) body.keep_alive = this.keepAlive;
    let response;
    try {
      response = await this.http.post(this.baseUrl + '/api/chat', body, {}, this.timeoutMs);
    } catch (error) {
      this.lastError = this.errors().exception(error);
      return null;
    }
    if (response.status !== 200) {
      this.lastError = this.errors().status(response.status, response.text);
      return null;
    }
    const data = parseJson(response.text) as any;
    if (data === undefined) {
      this.lastError = this.errors().badBody();
      return null;
    }
    const content = data?.message?.content;
    if (typeof content !== 'string' || !content.trim()) {
      this.lastError = this.errors().noText();
      return null;
    }
    return [content, answerConfidence(data?.logprobs)];
  }
}
