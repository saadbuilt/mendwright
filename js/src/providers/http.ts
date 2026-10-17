// One JSON POST over fetch, with a timeout, for the providers. A test or a custom client
// can pass its own Http instead.

export interface HttpResponse {
  status: number;
  text: string;
}

export interface Http {
  post(url: string, body: unknown, headers: Record<string, string>, timeoutMs: number): Promise<HttpResponse>;
}

/** A real User-Agent: Cloudflare and some proxies reject requests without one. */
export const USER_AGENT = 'mendwright-js/0.1.0';

export const fetchHttp: Http = {
  async post(url, body, headers, timeoutMs) {
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'user-agent': USER_AGENT, ...headers },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    return { status: response.status, text: await response.text() };
  },
};

export function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}
