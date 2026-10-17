// The heal itself (Python's mendwright/coordinator.py). When every candidate selector of a
// key misses, the coordinator declines first what must never heal, then looks for the
// element from the key's fingerprint (no model), then asks the provider, checks what it
// proposes with the gate, saves it, and the step is retried in the same call.
//
// Declined before anything is tried, with the reason: a key marked destructive, a page the
// key was never found on (the page guard), a key whose role is unknown, and a guard that
// returns false. The fingerprint tier needs no provider and does not count toward the rate
// limit. A model heal is tried at most twice per attempt: a refused proposal goes back to
// the model with the reason, once. A heal is confirmed or quarantined later by its canary.

import { buildBundle, redact } from './capture';
import type { PlaywrightDriver } from './driver';
import { AttentionRequired } from './errors';
import { fmt, valid as validFingerprint } from './fingerprint';
import { flowChangeMessage } from './flow';
import { apply, validateStatic } from './gate';
import type { JournalLike } from './journal';
import type { Guardrails } from './limits';
import { log, type LogLevel, type LogSink } from './log';
import { MAX_PROMPT_PART, type Bundle, type Proposal } from './prompt';
import { pyRepr } from './py';
import { providerName, type HealProvider, type ProviderTools } from './providers/base';
import { hasPosition } from './selectors';
import type { KeyMeta, SelectorStore } from './store';
import { findMatch } from './tier';

export const FINGERPRINT_PROVIDER = 'fingerprint';
export const DISABLED_REASON = 'healing is turned off by MENDWRIGHT_DISABLE=1, so the selector was not repaired. '
  + 'Unset MENDWRIGHT_DISABLE to let mendwright heal again.';
export const GUARD_REASON = 'a transaction guard is currently blocking heals (a payment or registration submission '
  + 'is pending), so mendwright did not try to heal. Run the step again once the guard allows heals.';

export interface HealRecord {
  key: string;
  old: string[];
  new: string;
  confirmed: boolean | null;
  source: string;
}

export interface FlowContext {
  url: string;
  page: string | null;
  lastPage: string | null;
  pages: string[];
}

export function destructiveMessage(key: string): string {
  return "'" + key + "' is marked as a destructive control, and mendwright never auto-heals those, because a "
    + 'validated but wrong selector here could act on a live account. Fix the selector in your code by hand.';
}

function roleNeeded(key: string): string {
  return "does not know what kind of element '" + key + "' is (its expected role is unknown), so it cannot "
    + "check a repaired selector. Pass role to heal.locator(), for example role: 'textbox' for a text field, "
    + "'button', 'checkbox', 'link' or 'combobox'.";
}

function noFingerprint(key: string): string {
  return "'" + key + "' has no fingerprint yet (mendwright records one each time its selector matches)";
}

export interface CoordinatorOptions {
  provider: HealProvider;
  driver: PlaywrightDriver;
  journal: JournalLike;
  guard?: () => boolean;
  guardrails?: Guardrails | null;
  dryRun?: boolean;
  fingerprint?: boolean;
  healAcrossPages?: boolean;
  disabled?: boolean;
  entry?: string;
  clock?: () => number;
  /** Where this healer's lines go (default: warnings on stderr). */
  log?: LogSink;
}

export class HealCoordinator {
  readonly store: SelectorStore;
  provider: HealProvider;
  readonly driver: PlaywrightDriver;
  readonly journal: JournalLike;
  readonly guard: () => boolean;
  readonly guardrails: Guardrails | null;
  readonly dryRun: boolean;
  readonly fingerprint: boolean;
  readonly healAcrossPages: boolean;
  readonly disabled: boolean;
  readonly entry: string;
  private readonly clock: () => number;
  private readonly sink: LogSink | null;
  /** key -> the heal this process applied and has not confirmed yet. */
  readonly pending = new Map<string, string>();
  /** key -> why its last heal attempt applied nothing. */
  readonly healReasons = new Map<string, string>();
  readonly heals: HealRecord[] = [];

  constructor(store: SelectorStore, options: CoordinatorOptions) {
    this.store = store;
    this.provider = options.provider;
    this.driver = options.driver;
    this.journal = options.journal;
    this.guard = options.guard ?? (() => true);
    this.guardrails = options.guardrails ?? null;
    this.dryRun = Boolean(options.dryRun);
    this.fingerprint = Boolean(options.fingerprint);
    this.healAcrossPages = Boolean(options.healAcrossPages);
    this.disabled = Boolean(options.disabled);
    this.entry = options.entry ?? 'mendwright.open()';
    this.clock = options.clock ?? (() => performance.now());
    this.sink = options.log ?? null;
  }

  emit(level: LogLevel, message: string): void {
    if (!this.sink) {
      log(level, message);
      return;
    }
    try {
      this.sink(level, message);
    } catch {
      // a broken sink must not break a heal
    }
  }

  decline(key: string, reason: string, level: LogLevel = 'warning', logged?: string | null): false {
    this.healReasons.set(key, reason);
    this.emit(level, 'did not heal ' + pyRepr(key) + ': ' + (logged || reason));
    return false;
  }

  explain(key: string): string {
    return this.healReasons.get(key) ?? '';
  }

  /** The reason to decline when this page is not one the key was found on, else ''. */
  flowChange(key: string, context: FlowContext): string {
    if (this.healAcrossPages) return '';
    const here = context.page;
    const seen = context.pages.filter((p) => typeof p === 'string' && p);
    if (context.lastPage && !seen.includes(context.lastPage)) seen.unshift(context.lastPage);
    if (!(here && seen.length) || seen.includes(here)) return '';
    this.journal.event('flow.page_guard', { key, last_page: seen[0], page: here });
    return flowChangeMessage(key, seen[0], here, this.entry);
  }

  private tools(): ProviderTools {
    const driver = this.driver;
    return { lib: (name, arg) => driver.lib(name, arg) };
  }

  /** Heal key and save a checked selector. Returns the saved XPath, or false with the
   * reason in healReasons. */
  async tryHeal(key: string, meta: KeyMeta, broken: string[], context: FlowContext): Promise<string | false> {
    if (meta.destructive) return this.decline(key, destructiveMessage(key));
    const moved = this.flowChange(key, context);
    if (moved) return this.decline(key, moved);
    const role = meta.expectedRole;
    const roleUnknown = !role || role === 'unknown';
    const unavailableRaw = (this.provider as { unavailableReason?: unknown }).unavailableReason;
    let unavailable = typeof unavailableRaw === 'string' && unavailableRaw ? unavailableRaw : null;
    const summaryRaw = (this.provider as { unavailableSummary?: unknown }).unavailableSummary;
    const summary = typeof summaryRaw === 'string' ? summaryRaw : null;
    if (unavailable && (roleUnknown || !this.fingerprint)) {
      if (roleUnknown) unavailable = unavailable.trimEnd() + ' Also, mendwright ' + roleNeeded(key);
      return this.decline(key, unavailable, 'warning', summary);
    }
    if (roleUnknown) return this.decline(key, 'mendwright ' + roleNeeded(key));
    if (!this.guard()) return this.decline(key, GUARD_REASON);
    const started = this.clock();
    let html: string | null = null;
    let requested = false;
    const source = !broken.length && meta.intent ? 'intent' : 'model';
    if (source === 'intent' && unavailable) {
      return this.decline(key, "'" + key + "' has no selector yet, and only a model can find it from intent, but no model "
        + 'provider is configured. ' + unavailable,
      'warning', summary ? "'" + key + "' has no selector yet, and " + summary : null);
    }
    if (this.fingerprint && source !== 'intent') {
      html = await this.driver.content();
      const [applied, note, asked] = await this.fingerprintTier(key, meta, broken, html, started);
      requested = asked;
      if (applied !== null) return applied;
      if (unavailable) {
        return this.decline(key, note + ', and no model provider is configured to try instead. ' + unavailable,
          'warning', summary ? note + ', and ' + summary : null);
      }
    }
    if (this.guardrails && !this.guardrails.allow(key)) {
      this.journal.event('heal.rejected', { key, reason: 'rate/spend cap' });
      return this.decline(key, "the hourly rate cap for '" + key + "' was reached, so mendwright stopped (fail-closed). "
        + 'Wait for the hourly window to reset, or raise the cap with open({ ratePerKeyPerHour, rateGlobalPerHour }).');
    }
    const tag = source !== 'model' ? source : null;
    if (!requested) this.journal.event('heal.requested', { key });
    if (html === null) html = await this.driver.content();
    const wants = (this.provider as HealProvider).wantsFingerprint === true;
    // The model reads a cleaned, redacted copy of the page; the gate checks the page as captured.
    let cleaned = '';
    try {
      cleaned = await this.driver.lib<string>('cleanHtml', redact(html));
    } catch {
      cleaned = redact(html);
    }
    cleaned = redact(cleaned);
    // A model that reads a prompt sees at most MAX_PROMPT_PART characters of the page. When
    // the end was cut off, positions there no longer match the live page.
    const cut = !wants && [...cleaned].length > MAX_PROMPT_PART;
    let bundle: Bundle = buildBundle(key, meta, broken, await this.driver.inventory(), cleaned, context, wants);
    let reason = 'no proposal';
    for (let attempt = 0; attempt < 2; attempt++) {
      let proposal: Proposal | null = null;
      try {
        proposal = await this.provider.propose(bundle, this.tools());
      } catch (error) {
        const detail = (error as Error)?.message ?? String(error);
        this.journal.event('heal.rejected', { key, reason: 'no proposal', error: detail });
        return this.decline(key, providerName(this.provider) + " failed for '" + key + "': " + detail);
      }
      if (!proposal || typeof proposal !== 'object') {
        const error = this.providerError();
        if (error) {
          this.journal.event('heal.rejected', { key, reason: 'no proposal', error });
          return this.decline(key, providerName(this.provider) + " returned no selector for '" + key + "': " + error);
        }
        this.journal.event('heal.rejected', { key, reason: 'no proposal' });
        return this.decline(key, "the model returned no usable selector for '" + key + "'. Try a different or stronger "
          + 'model, or repair the selector by hand.');
      }
      this.journal.event('heal.proposed', { key, xpath: proposal.xpath, reasoning: proposal.reasoning, source: tag });
      if (this.dryRun) {
        let ok = false;
        for (const xpath of [proposal.xpath, ...(proposal.fallbacks ?? [])]) {
          if (!xpath || (cut && hasPosition(xpath))) continue;
          if ((await validateStatic(this.driver, xpath, html, meta.expectedRole, this.store.quarantined(key, ''))).ok) {
            ok = true;
            break;
          }
        }
        this.emit('info', '[heal dry-run] key=' + key + ' proposed=' + proposal.xpath + ' valid=' + ok + ' (not applied)');
        return this.decline(key, "dry-run mode is on, so the proposal for '" + key + "' was checked but not applied. "
          + 'Turn off dryRun to let mendwright apply and retry.', 'info');
      }
      const [applied, why] = await apply(this.store, key, proposal, html, meta, this.driver, !cut);
      reason = why;
      if (applied) {
        this.recordApplied(key, broken, applied, started, providerName(this.provider), this.providerModel(), source);
        return applied;
      }
      this.journal.event('heal.rejected', { key, xpath: proposal.xpath, reason, source: tag });
      bundle = { ...bundle, rejectedXpath: proposal.xpath, rejectReason: reason };
    }
    this.decline(key, "no selector the model proposed for '" + key + "' passed mendwright's local gate (last reason: "
      + reason + '). The gate requires a non-brittle xpath that matches exactly one element of the expected role and '
      + 'is present on the live page.');
    return false;
  }

  /** The first tier: [applied xpath, or false when dry run declined it, or null to go on to
   * the model; why it did not heal; whether heal.requested was journaled]. */
  private async fingerprintTier(key: string, meta: KeyMeta, broken: string[], html: string,
                                started: number): Promise<[string | false | null, string, boolean]> {
    const stored = meta.fingerprint;
    if (!validFingerprint(stored)) return [null, noFingerprint(key), false];
    this.journal.event('heal.requested', { key });
    const match = await findMatch(stored, meta.expectedRole, this.driver);
    if (!match.xpath) {
      return [null, "the fingerprint match for '" + key + "' was not confident: " + match.reason, true];
    }
    const reasoning = 'fingerprint score ' + fmt(match.score) + ', next best ' + fmt(match.runnerUp);
    this.journal.event('heal.proposed', { key, xpath: match.xpath, reasoning, source: FINGERPRINT_PROVIDER });
    if (this.dryRun) {
      const ok = (await validateStatic(this.driver, match.xpath, html, meta.expectedRole, this.store.quarantined(key, ''))).ok;
      this.emit('info', '[heal dry-run] key=' + key + ' proposed=' + match.xpath + ' (fingerprint) valid=' + ok + ' (not applied)');
      return [this.decline(key, "dry-run mode is on, so the fingerprint match for '" + key + "' was checked but not "
        + 'applied. Turn off dryRun to let mendwright apply and retry.', 'info'), '', true];
    }
    const [applied, reason] = await apply(this.store, key, { xpath: match.xpath, fallbacks: [], reasoning }, html, meta, this.driver);
    if (applied) {
      this.recordApplied(key, broken, applied, started, FINGERPRINT_PROVIDER, null, FINGERPRINT_PROVIDER);
      return [applied, '', true];
    }
    this.journal.event('heal.rejected', { key, xpath: match.xpath, reason, source: FINGERPRINT_PROVIDER });
    return [null, "the fingerprint match for '" + key + "', " + match.xpath + ", failed mendwright's checks (" + reason + ')', true];
  }

  private recordApplied(key: string, broken: string[], applied: string, started: number, provider: string,
                        model: string | null, source: string): void {
    const seconds = Math.max(0, (this.clock() - started) / 1000);
    this.journal.event('heal.applied', { key, xpath: applied, old: broken, provider, model,
                                         latency_ms: Math.round(seconds * 1000), source: source !== 'model' ? source : null });
    this.healReasons.delete(key);
    this.heals.push({ key, old: [...broken], new: applied, confirmed: null, source });
    const timing = ' (' + provider + ', ' + seconds.toFixed(1) + 's)';
    if (source === 'intent') {
      this.emit('warning', 'found ' + pyRepr(key) + ' from its intent: ' + applied + timing);
      return;
    }
    const was = !broken.length ? '(no stored selector)' : broken.length === 1 ? broken[0]
      : broken[0] + ' (+' + (broken.length - 1) + ' more)';
    this.emit('warning', 'healed ' + pyRepr(key) + ': ' + was + ' -> ' + applied + timing);
  }

  private providerModel(): string | null {
    const model = (this.provider as { model?: unknown }).model;
    return typeof model === 'string' && model ? model : null;
  }

  private providerError(): string {
    const error = (this.provider as { lastError?: unknown }).lastError;
    return typeof error === 'string' && error.trim() ? error.trim() : '';
  }

  /** The canary: report a heal's outcome, and quarantine a heal whose check failed. A
   * fingerprint heal is not reported to the provider, which did not propose it. by says what
   * decided it ('test', 'expect', 'confirm' or 'action'). */
  confirm(key: string, xpath: string, success: boolean, options: { reason?: string; by?: string; test?: string } = {}): void {
    const heal = [...this.heals].reverse().find((h) => h.key === key && h.new === xpath && h.confirmed === null);
    if (!heal || heal.source !== FINGERPRINT_PROVIDER) {
      try {
        this.provider.reportOutcome?.(key, xpath, success);
      } catch {
        // reporting an outcome must never fail a step
      }
    }
    this.journal.event('heal.canary', { key, xpath, success: Boolean(success), reason: options.reason, by: options.by,
                                        test: options.test });
    if (heal) heal.confirmed = Boolean(success);
    if (!success) {
      this.store.quarantine(key, xpath);
      this.journal.event('heal.quarantine', { key, xpath, reason: options.reason });
    }
  }

  isPending(key: string, xpath: string | null): boolean {
    return xpath !== null && this.pending.get(key) === xpath;
  }

  /** Report the outcome for a heal of key made in this process, then forget it. With xpath,
   * only when the heal waiting is the one that applied xpath. */
  confirmPending(key: string, success: boolean, options: { reason?: string; xpath?: string; by?: string; test?: string } = {}): void {
    if (options.xpath !== undefined && this.pending.get(key) !== options.xpath) return;
    const xpath = this.pending.get(key);
    this.pending.delete(key);
    if (xpath !== undefined) this.confirm(key, xpath, success, options);
  }
}

/** Resolve; when every candidate missed, heal once and resolve again in the same call.
 * Re-raises the miss with the reason when nothing was healed. */
export async function resolveWithHeal<T>(resolveFn: () => Promise<T>, coordinator: HealCoordinator, key: string,
                                         context: FlowContext, missed: AttentionRequired): Promise<T> {
  const store = coordinator.store;
  const broken = store.candidates(key).map((c) => c.xpath);
  const applied = await coordinator.tryHeal(key, store.metaOf(key), broken, context);
  if (!applied) {
    const reason = coordinator.explain(key);
    if (reason) throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
    throw missed;
  }
  coordinator.pending.set(key, applied);
  return resolveFn();
}
