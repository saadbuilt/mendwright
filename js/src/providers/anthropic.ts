// AnthropicProvider: calls the Anthropic Messages API over HTTPS, as the Python package's
// AnthropicProvider does without the SDK. The model comes from model or ANTHROPIC_MODEL
// (there is no default), the key from apiKey or ANTHROPIC_API_KEY, and the endpoint from
// baseUrl, ANTHROPIC_BASE_URL, else https://api.anthropic.com. It only proposes; it
// never applies anything.

import { buildPrompt, parseProposal, type Bundle, type Proposal } from '../prompt';
import type { HealProvider } from './base';
import { Describer } from './errors';
import { fetchHttp, parseJson, type Http } from './http';

export const ANTHROPIC_BASE_URL = 'https://api.anthropic.com';
export const ANTHROPIC_VERSION = '2023-06-01';

const HINTS = {
  auth: 'Check ANTHROPIC_API_KEY, or pass apiKey to AnthropicProvider.',
  model: 'Check the model id in ANTHROPIC_MODEL, or pass model to AnthropicProvider.',
  connect: 'Check your network connection, and baseUrl if you use a proxy.',
  reply: 'Try a stronger model (ANTHROPIC_MODEL or model).',
};

export interface AnthropicOptions {
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  maxTokens?: number;
  timeoutMs?: number;
  http?: Http;
}

export class AnthropicProvider implements HealProvider {
  readonly model: string;
  readonly baseUrl: string;
  readonly maxTokens: number;
  readonly timeoutMs: number;
  lastError: string | null = null;
  private readonly apiKey: string | undefined;
  private readonly http: Http;

  constructor(options: AnthropicOptions = {}) {
    const model = options.model || process.env.ANTHROPIC_MODEL;
    if (!model) throw new TypeError('AnthropicProvider requires a model: pass model or set ANTHROPIC_MODEL');
    this.model = model;
    this.apiKey = options.apiKey || process.env.ANTHROPIC_API_KEY || undefined;
    this.baseUrl = (options.baseUrl || process.env.ANTHROPIC_BASE_URL || ANTHROPIC_BASE_URL).replace(/\/+$/, '');
    this.maxTokens = options.maxTokens ?? 1024;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.http = options.http ?? fetchHttp;
  }

  private errors(): Describer {
    return new Describer('Anthropic', { url: this.baseUrl, hints: HINTS, secrets: [this.apiKey] });
  }

  /** Never throws: a failure gives null and a reason in lastError. */
  async propose(bundle: Bundle): Promise<Proposal | null> {
    this.lastError = null;
    let prompt;
    try {
      prompt = buildPrompt(bundle);
    } catch (error) {
      this.lastError = this.errors().exception(error);
      return null;
    }
    const body = { model: this.model, max_tokens: this.maxTokens, system: prompt.system,
                   messages: [{ role: 'user', content: prompt.user }] };
    const headers: Record<string, string> = { 'anthropic-version': ANTHROPIC_VERSION };
    if (this.apiKey) headers['x-api-key'] = this.apiKey;
    let response;
    try {
      response = await this.http.post(this.baseUrl + '/v1/messages', body, headers, this.timeoutMs);
    } catch (error) {
      this.lastError = this.errors().exception(error);
      return null;
    }
    if (response.status !== 200) {
      this.lastError = this.errors().status(response.status, response.text);
      return null;
    }
    const data = parseJson(response.text);
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      this.lastError = this.errors().badBody();
      return null;
    }
    const blocks = (data as Record<string, unknown>).content;
    const text = Array.isArray(blocks)
      ? blocks.filter((b) => b && typeof b === 'object' && (b as Record<string, unknown>).type === 'text')
        .map((b) => String((b as Record<string, unknown>).text ?? '')).join('')
      : '';
    if (!text) {
      const stop = (data as Record<string, unknown>).stop_reason;
      this.lastError = this.errors().noText(typeof stop === 'string' && stop ? 'stop reason: ' + stop : null);
      return null;
    }
    const proposal = parseProposal(text);
    if (proposal === null) this.lastError = this.errors().reply(text);
    return proposal;
  }

  reportOutcome(): void {
    // A direct provider has nowhere to report to: the journal and the store keep the outcome.
  }
}
