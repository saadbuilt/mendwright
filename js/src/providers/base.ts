// The model extension point. A provider turns a capture bundle into a proposal
// {xpath, fallbacks, reasoning}, or null when it has none, and leaves a readable reason in
// lastError. Everything a provider returns is untrusted: the gate checks every proposal
// against the captured and the live page before it is saved or used.

import type { LibName } from '../driver';
import type { Bundle, Proposal } from '../prompt';

/** What a provider may ask of the page while it proposes: one of mendwright's page
 * helpers, run on the captured HTML. Only LocalModelProvider uses it. */
export interface ProviderTools {
  lib<T>(name: Extract<LibName, 'candidates' | 'indexOfMatch' | 'robustXpaths'>, arg: unknown): Promise<T>;
}

export interface HealProvider {
  propose(bundle: Bundle, tools?: ProviderTools): Promise<Proposal | null> | Proposal | null;
  /** Called with the outcome of a heal the provider proposed. Optional. */
  reportOutcome?(key: string, xpath: string, success: boolean): unknown;
  /** Why the last propose() gave null, without any key or request body. */
  lastError?: string | null;
  model?: string;
  /** True for a provider that may get the key's saved fingerprint (a model on this machine). */
  wantsFingerprint?: boolean;
}

/** The provider name the journal and the log use. */
export function providerName(provider: unknown): string {
  const name = (provider as { constructor?: { name?: string } })?.constructor?.name;
  return name && name !== 'Object' && name !== 'Function' ? name : 'provider';
}

/** Used when no provider is configured: selectors still resolve, and only the fingerprint
 * tier heals. Its reason says what to set. */
export class NullProvider implements HealProvider {
  static readonly DEFAULT = 'no model provider is configured, so only fingerprint healing (no model) can '
    + 'repair a broken selector. Set a provider key and model (for example ANTHROPIC_API_KEY and '
    + 'ANTHROPIC_MODEL), or pass provider to open().';

  readonly unavailableReason: string;
  readonly unavailableSummary: string | null;

  constructor(reason?: string, summary?: string | null) {
    this.unavailableReason = reason || NullProvider.DEFAULT;
    this.unavailableSummary = summary ?? null;
  }

  propose(): null {
    return null;
  }
}
