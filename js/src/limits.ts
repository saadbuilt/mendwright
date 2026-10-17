// In-process limits for a model provider (Python's mendwright/limits.py): a sliding-window
// cap on heal attempts per key and per hour. A blocked attempt records nothing and the
// heal is declined without calling the provider (fail closed).

export const HOUR_MS = 3600_000;

export class RateLimiter {
  readonly perKeyPerHour: number | null;
  readonly globalPerHour: number | null;
  private readonly windowMs: number;
  private readonly clock: () => number;
  private readonly perKey = new Map<string, number[]>();
  private readonly global: number[] = [];

  constructor(options: { perKeyPerHour?: number | null; globalPerHour?: number | null;
                         windowMs?: number; clock?: () => number } = {}) {
    this.perKeyPerHour = options.perKeyPerHour ?? null;
    this.globalPerHour = options.globalPerHour ?? null;
    this.windowMs = options.windowMs ?? HOUR_MS;
    this.clock = options.clock ?? (() => performance.now());
  }

  private prune(stamps: number[], cutoff: number): void {
    let i = 0;
    while (i < stamps.length && stamps[i] <= cutoff) i += 1;
    if (i) stamps.splice(0, i);
  }

  /** Would a heal attempt for key be allowed now? Consumes nothing. */
  check(key: string): boolean {
    const cutoff = this.clock() - this.windowMs;
    const stamps = this.perKey.get(key);
    if (stamps) this.prune(stamps, cutoff);
    this.prune(this.global, cutoff);
    if (this.perKeyPerHour !== null && (stamps?.length ?? 0) >= this.perKeyPerHour) return false;
    if (this.globalPerHour !== null && this.global.length >= this.globalPerHour) return false;
    return true;
  }

  /** Record a heal attempt for key and return true when both caps allow it; otherwise
   * record nothing and return false. */
  allow(key: string): boolean {
    if (!this.check(key)) return false;
    const now = this.clock();
    const stamps = this.perKey.get(key) ?? [];
    stamps.push(now);
    this.perKey.set(key, stamps);
    this.global.push(now);
    return true;
  }
}

/** The caps a heal attempt must pass. Anything with allow(key) works. */
export interface Guardrails {
  allow(key: string): boolean;
}

// One limiter per store file and caps for the whole process, so opening several healers on
// one store (each test opens its own) does not multiply the hourly budget.
const shared = new Map<string, RateLimiter>();

export function sharedLimiter(storeFile: string, perKey: number | null, perHour: number | null): RateLimiter {
  const key = JSON.stringify([storeFile, perKey, perHour]);
  let limiter = shared.get(key);
  if (!limiter) {
    limiter = new RateLimiter({ perKeyPerHour: perKey, globalPerHour: perHour });
    shared.set(key, limiter);
  }
  return limiter;
}

/** Forget the shared limiters (tests). */
export function resetLimiters(): void {
  shared.clear();
}
