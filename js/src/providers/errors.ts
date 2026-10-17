// Short, readable reasons for a failed propose() (Python's providers/_errors.py). The text
// never holds an API key, an auth header or a request body: known secrets become
// [redacted], key-shaped tokens and the user info of any URL are masked, and a URL is shown
// without its query string or user info. It says what happened, then one next step when
// there is a useful one.

import { notHereReason } from '../prompt';

export const MAX_DETAIL = 200;
export const NOT_HERE_HINT = 'If the step moved to another page or the element was removed, update your script.';

const STATUS_WORDS: Record<number, string> = {
  400: 'bad request', 401: 'unauthorized', 403: 'forbidden', 404: 'not found', 405: 'method not allowed',
  408: 'request timeout', 409: 'conflict', 413: 'request too large', 422: 'unprocessable request',
  429: 'rate limited', 500: 'server error', 502: 'bad gateway', 503: 'service unavailable',
  504: 'gateway timeout', 529: 'overloaded',
};
const GENERIC_KINDS = new Set(['error', 'ok', 'failed', 'failure', 'needs_human']);

export type HintKind = 'auth' | 'model' | 'rate' | 'server' | 'timeout' | 'connect' | 'refused' | 'reply' | 'body';
export type Hints = Partial<Record<HintKind, string>>;

const DEFAULT_HINTS: Hints = {
  rate: 'Wait a few minutes and retry, or heal less often with open({ ratePerKeyPerHour }).',
  server: 'This is usually temporary, so retry later.',
  timeout: 'Retry, or give the provider more time with its timeoutMs option.',
};

const KEYLIKE = new RegExp(
  '\\b(?:sk|rk|pk|gsk|xai)[-_][A-Za-z0-9_\\-*.]{6,}'
  + '|\\bAIza[0-9A-Za-z_\\-]{10,}'
  + '|\\b(?:AKIA|ASIA)[0-9A-Z]{16}\\b'
  + '|\\beyJ[A-Za-z0-9_\\-]{8,}\\.[A-Za-z0-9_\\-]{8,}\\.[A-Za-z0-9_\\-]*'
  + '|\\b[0-9a-fA-F]{32,}\\b', 'g');
const AUTH_VALUE = /\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-*]{16,}/gi;
const HEADER_SECRET = /\b((?:x-)?(?:api[_-]?key|goog-api-key|auth[_-]?token|access[_-]?token|secret|authorization|heal-key)\s*[:=]\s*)(?:"|')?(?!\[redacted\])[^\s"',;&<>]{8,}/gi;
const QUERY_SECRET = /([?&](?:key|api[_-]?key|access[_-]?token|token|sig|signature|x-amz-[a-z-]+)=)[^&\s"'<>]+/gi;
// The user info of a URL in text, up to the last @ before its host: scheme://user:pass@.
const USER_INFO = /\b([a-z][a-z0-9+.-]*:\/\/)[^\s/?#"'<>]*@/gi;
// The same at the start of a URL that may not parse.
const LEADING_USER_INFO = /^(\s*[a-z][a-z0-9+.-]*:\/\/)[^/?#]*@/i;
const HTML_PAGE = /<\s*(!doctype|html|head|body|title)\b/i;

/** Remove known secrets and anything that looks like a key or token from text. */
export function scrub(text: unknown, secrets: (string | null | undefined)[] = []): string {
  let out = String(text);
  for (const secret of secrets) {
    if (typeof secret === 'string' && secret.length >= 4) out = out.split(secret).join('[redacted]');
  }
  out = out.replace(USER_INFO, '$1[redacted]@');
  out = out.replace(QUERY_SECRET, '$1[redacted]');
  out = out.replace(AUTH_VALUE, '$1 [redacted]');
  out = out.replace(HEADER_SECRET, '$1[redacted]');
  return out.replace(KEYLIKE, '[redacted]');
}

/** Collapse whitespace and cut text to limit characters. */
export function clip(text: unknown, limit = MAX_DETAIL): string {
  const t = String(text).split(/\s+/).filter(Boolean).join(' ');
  if ([...t].length <= limit) return t;
  return [...t].slice(0, limit - 3).join('').trimEnd() + '...';
}

/** url as scheme://host[:port]/path, without user info, query or fragment. */
export function safeUrl(url: unknown): string {
  try {
    const u = new URL(String(url));
    return u.protocol + '//' + u.host + u.pathname.replace(/\/+$/, '');
  } catch {
    return String(url).replace(LEADING_USER_INFO, '$1').split('?')[0].split('#')[0];
  }
}

/** Throw a TypeError when url has a user name or password in it. fetch() refuses such a
 * URL with the whole URL in its message; this names only scheme://host. where says what
 * the URL comes from. */
export function refuseCredentials(who: string, url: string, where: string): void {
  let shown: string;
  try {
    const u = new URL(url);
    if (!u.username && !u.password) return;
    shown = u.protocol + '//' + u.host;
  } catch {
    if (!LEADING_USER_INFO.test(url)) return;
    shown = safeUrl(url).replace(/^(\s*[a-z][a-z0-9+.-]*:\/\/[^/]*).*$/i, '$1');
  }
  throw new TypeError(who + ': the base URL ' + shown + ' (' + where + ') has a user name or password in it, which '
    + 'mendwright does not send. Remove them from the URL; if your proxy needs them, pass the provider an http '
    + 'client that sends them in a header.');
}

/** text and an optional hint as one or two sentences. */
export function sentence(text: string, hint?: string | null): string {
  let t = String(text).trimEnd();
  if (!/[.!?]$/.test(t)) t += '.';
  return t + (hint ? ' ' + hint : '');
}

/** [kind, message] from a vendor error body (Anthropic, OpenAI, Ollama shapes). */
export function vendorError(body: unknown): [string | null, string | null] {
  if (Array.isArray(body) && body.length && body[0] && typeof body[0] === 'object') body = body[0];
  if (typeof body === 'string') return [null, body.trim() ? body : null];
  if (!body || typeof body !== 'object') return [null, null];
  const b = body as Record<string, unknown>;
  const err = b.error;
  if (typeof err === 'string') return [null, err];
  const node = (err && typeof err === 'object' ? err : b) as Record<string, unknown>;
  let kind: string | null = null;
  for (const name of ['code', 'type', 'status']) {
    const value = node[name];
    if (typeof value === 'string' && value && !GENERIC_KINDS.has(value.toLowerCase())) {
      kind = value;
      break;
    }
  }
  let message: string | null = null;
  for (const name of ['message', 'reason', 'detail', 'error_description']) {
    const value = node[name];
    if (typeof value === 'string' && value.trim()) {
      message = value;
      break;
    }
  }
  return [kind, message];
}

function category(status: number, message: string | null): HintKind | null {
  const lowered = (message || '').toLowerCase();
  if (status === 401 || status === 403) return 'auth';
  if ((status === 400 || status === 422) && (lowered.includes('api key') || lowered.includes('api_key'))) return 'auth';
  if (status === 404 || ((status === 400 || status === 422) && lowered.includes('model'))) return 'model';
  if (status === 408 || status === 504) return 'timeout';
  if (status === 429) return 'rate';
  if (status >= 500) return 'server';
  return null;
}

function readBody(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text && text.trim() && !text.includes('<') ? text : null;
  }
}

/** What a failed fetch says, or null when it is not a network failure. */
function networkReason(error: unknown): string | null {
  let network = false;
  for (let node: any = error, depth = 0; node && depth < 6; node = node.cause, depth++) {
    const name = String(node.name || '');
    const code = String(node.code || '');
    const text = String(node.message || '').toLowerCase();
    if (name === 'TimeoutError' || name === 'AbortError' || /TIMEOUT/.test(code) || code === 'ETIMEDOUT'
        || text.includes('timed out')) return 'timed out';
    if (code === 'ECONNREFUSED' || text.includes('refused')) return 'connection refused';
    if (code === 'ENOTFOUND' || code === 'EAI_AGAIN' || text.includes('getaddrinfo')) return 'host name not found';
    if (/CERT|TLS|SSL|SELF_SIGNED/.test(code) || text.includes('certificate')) return 'TLS error';
    if (code === 'ECONNRESET' || code === 'UND_ERR_SOCKET') return 'connection reset';
    if (text.includes('fetch failed') || code.startsWith('UND_ERR') || /^E[A-Z]+$/.test(code)) network = true;
  }
  return network ? 'network error' : null;
}

export class Describer {
  readonly who: string;
  readonly url: string | null;
  readonly hints: Hints;
  readonly secrets: string[];

  constructor(who: string, options: { url?: string | null; hints?: Hints; secrets?: (string | null | undefined)[] } = {}) {
    this.who = who;
    this.url = options.url ?? null;
    this.hints = { ...DEFAULT_HINTS };
    for (const [k, v] of Object.entries(options.hints ?? {})) if (v) this.hints[k as HintKind] = v;
    this.secrets = (options.secrets ?? []).filter((s): s is string => typeof s === 'string' && s.length > 0);
  }

  private clean(text: string): string {
    return clip(scrub(text, this.secrets));
  }

  /** '<who> returned HTTP <status> (<kind>): <vendor message>. <hint>' */
  status(status: number, text: string): string {
    const body = readBody(text);
    let [kind, message] = vendorError(body);
    if (message && HTML_PAGE.test(message)) message = null;
    const label = kind || STATUS_WORDS[status] || (status >= 500 ? 'server error' : null);
    let out = this.who + ' returned HTTP ' + status;
    if (label) out += ' (' + clip(scrub(label, this.secrets), 60) + ')';
    if (message) out += ': ' + this.clean(message);
    const hint = category(status, message);
    return sentence(out, hint ? this.hints[hint] : null);
  }

  exception(error: unknown): string {
    const reason = networkReason(error);
    if (reason !== null) return this.network(reason);
    const name = (error as Error)?.name || 'Error';
    const detail = this.clean(String((error as Error)?.message ?? error));
    let out = 'the request to ' + this.who + ' failed (' + name + ')';
    if (detail && detail !== name) out += ': ' + detail;
    return sentence(out);
  }

  network(reason: string): string {
    const target = this.url ? safeUrl(this.url) : this.who;
    if (reason === 'timed out') return sentence('the request to ' + target + ' timed out', this.hints.timeout);
    const hint = reason === 'connection refused' ? this.hints.refused : undefined;
    return sentence('could not connect to ' + target + ' (' + reason + ')', hint || this.hints.connect);
  }

  badBody(): string {
    return sentence(this.who + ' returned a response that was not valid JSON', this.hints.body);
  }

  noText(detail?: string | null): string {
    let out = this.who + ' returned a reply with no text';
    if (detail) out += ' (' + this.clean(detail) + ')';
    return sentence(out, this.hints.reply);
  }

  /** Why parseProposal() found no proposal in the model's reply. */
  reply(text: unknown): string {
    const notHere = typeof text === 'string' ? notHereReason(text) : null;
    if (notHere) {
      return sentence('the model says no element on this page fits: ' + this.clean(notHere).replace(/\.+$/, ''), NOT_HERE_HINT);
    }
    const hint = this.hints.reply;
    if (text === null || text === undefined || !String(text).trim()) return sentence('the model returned an empty reply', hint);
    const match = String(text).match(/\{[\s\S]*\}/);
    let obj: unknown = null;
    try {
      obj = match ? JSON.parse(match[0]) : null;
    } catch {
      obj = null;
    }
    if (obj === null) return sentence('the model reply was not valid JSON', hint);
    if (typeof obj !== 'object' || Array.isArray(obj)) return sentence('the model reply was JSON but not an object', hint);
    if (!(obj as Record<string, unknown>).xpath) return sentence('the model reply did not include an xpath', hint);
    return sentence('the model reply could not be used', hint);
  }
}
