// OpenAICompatibleProvider: one adapter for every service that speaks OpenAI's
// /chat/completions format. baseUrl picks the service: OpenAI by default
// (OPENAI_BASE_URL overrides it), or a local Ollama through forOllama(). The model comes
// from model or OPENAI_MODEL (there is no default), the key from apiKey or OPENAI_API_KEY.
// A local Ollama never gets the OpenAI key.

import { buildPrompt, parseProposal, type Bundle, type Proposal } from '../prompt';
import type { HealProvider } from './base';
import { Describer, type Hints } from './errors';
import { fetchHttp, parseJson, type Http } from './http';

export const OPENAI_BASE_URL = 'https://api.openai.com/v1';
// 127.0.0.1, not localhost: on Windows localhost tries IPv6 first and can stall about 2
// seconds before it falls back to IPv4, where Ollama listens.
export const OLLAMA_BASE_URL = 'http://127.0.0.1:11434/v1';

export interface OpenAIOptions {
  model?: string;
  apiKey?: string;
  baseUrl?: string;
  responseFormatJson?: boolean;
  timeoutMs?: number;
  http?: Http;
}

export class OpenAICompatibleProvider implements HealProvider {
  readonly model: string;
  readonly baseUrl: string;
  readonly timeoutMs: number;
  lastError: string | null = null;
  private readonly apiKey: string | undefined;
  private readonly responseFormatJson: boolean;
  private readonly http: Http;
  private ollama = false;

  constructor(options: OpenAIOptions = {}) {
    this.baseUrl = (options.baseUrl || process.env.OPENAI_BASE_URL || OPENAI_BASE_URL).replace(/\/+$/, '');
    // A key is optional (Ollama needs none); '' means no key, never OPENAI_API_KEY.
    this.apiKey = options.apiKey !== undefined ? (options.apiKey || undefined) : process.env.OPENAI_API_KEY || undefined;
    const model = options.model || process.env.OPENAI_MODEL;
    if (!model) {
      throw new TypeError("OpenAICompatibleProvider: no model configured. Pass model or set the OPENAI_MODEL "
        + "environment variable (use a current id from your provider's model list).");
    }
    this.model = model;
    this.responseFormatJson = options.responseFormatJson ?? true;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.http = options.http ?? fetchHttp;
  }

  /** A local, keyless Ollama at baseUrl (default http://127.0.0.1:11434/v1). */
  static forOllama(model: string, options: Omit<OpenAIOptions, 'model'> = {}): OpenAICompatibleProvider {
    const provider = new OpenAICompatibleProvider({ ...options, model, baseUrl: options.baseUrl ?? OLLAMA_BASE_URL,
                                                    apiKey: options.apiKey ?? '' });
    provider.ollama = true;
    return provider;
  }

  private service(): string {
    let host = '';
    try {
      host = new URL(this.baseUrl).hostname.toLowerCase();
    } catch {
      host = '';
    }
    if (this.ollama || this.baseUrl.includes(':11434')) return 'Ollama';
    if (host === 'api.openai.com') return 'OpenAI';
    return host || 'the server';
  }

  private errors(): Describer {
    const service = this.service();
    let hints: Hints;
    if (service === 'Ollama') {
      hints = {
        auth: 'Check the apiKey you passed (Ollama itself needs none).',
        model: "Check OLLAMA_MODEL (or model), and run 'ollama pull <model>' if it is not downloaded yet.",
        refused: "Is Ollama running? Start it with 'ollama serve', or set OLLAMA_HOST (or baseUrl) to where it listens.",
        connect: 'Check OLLAMA_HOST (or baseUrl) and that Ollama is running.',
        reply: 'Try a larger model (OLLAMA_MODEL or model).',
      };
    } else if (service === 'OpenAI') {
      hints = {
        auth: 'Check OPENAI_API_KEY, or pass apiKey.',
        model: 'Check the model id in OPENAI_MODEL, or pass model.',
        connect: 'Check your network connection, and baseUrl if you use a proxy.',
        reply: 'Try a stronger model (OPENAI_MODEL or model).',
      };
    } else {
      hints = {
        auth: 'Check the apiKey you passed for ' + service + '.',
        model: 'Check the model id (OPENAI_MODEL or model) and baseUrl.',
        connect: 'Check baseUrl and that the server is running.',
        reply: 'Try a stronger model (OPENAI_MODEL or model).',
      };
    }
    return new Describer(service, { url: this.baseUrl, hints, secrets: [this.apiKey] });
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
    const body: Record<string, unknown> = {
      model: this.model,
      messages: [{ role: 'system', content: prompt.system }, { role: 'user', content: prompt.user }],
    };
    if (this.responseFormatJson) body.response_format = { type: 'json_object' };
    const headers: Record<string, string> = {};
    if (this.apiKey) headers.Authorization = 'Bearer ' + this.apiKey;
    let response;
    try {
      response = await this.http.post(this.baseUrl + '/chat/completions', body, headers, this.timeoutMs);
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
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== 'string' || !content) {
      this.lastError = this.errors().noText();
      return null;
    }
    const proposal = parseProposal(content);
    if (proposal === null) this.lastError = this.errors().reply(content);
    return proposal;
  }

  reportOutcome(): void {
    // A direct provider has nowhere to report to: the journal and the store keep the outcome.
  }
}
