// The healer: open(page) gives one, and heal.locator(key, selector) resolves the key when
// an action on it runs, heals it when its selectors broke, retries the step and arms the
// canary (Python's facade.Healer and aio.AsyncHealer). The selector in your code always
// wins over stored ones, the element's role is read from the page the first time it
// matches, and its fingerprint is recorded each time it matches, for the fingerprint tier.
//
// It also handles registered interruptions (heal.interrupt(), and the cookies preset),
// optional steps, and the page guard: a key is not healed on a page it was never found on.
//
// Open shadow roots (see shadow.ts): each fingerprint read also reads where the element is,
// and saves it as the key's shadowHosts, as the Python package does. A key last found inside
// a shadow root is never healed by this package, so a look-alike on the page around its host
// is never picked; a heal the Python package saved inside one runs strictly there.

import * as path from 'node:path';

import type { Frame, Locator, Page } from '@playwright/test';

import { DISABLED_REASON, HealCoordinator, resolveWithHeal, type FlowContext, type HealRecord } from './coordinator';
import { PlaywrightDriver, type PageLike } from './driver';
import { AttentionRequired, InterruptionLoop, InvalidSelector } from './errors';
import {
  MAX_INTERRUPTIONS_PER_STEP, SETTLE_MS, absentMessage, handledMessage, isInterceptionError, loopMessage,
  nothingMatchedMessage, pagePath, pageUrl, severalMessage, skipMessage, unhandledMessage,
} from './flow';
import {
  COOKIE_CHOICES, cookieChoiceError, cookiePreset, makeInterruption, nthTargets,
  type CookieChoice, type Interruption, type InterruptionEvent,
} from './interruptions';
import { Journal, type JournalLike } from './journal';
import { sharedLimiter, type Guardrails } from './limits';
import { log, type LogSink } from './log';
import { NullProvider, type HealProvider } from './providers/base';
import { autoProvider } from './providers/detect';
import { pyRepr } from './py';
import { peekVisible, present as presentNow, resolve } from './resolver';
import { inferRole, roleChoices } from './roles';
import { XPATH, checkSyntax, classify, isGetByCall, normalize } from './selectors';
import { hostsOf, placeText, runTarget, type Target } from './shadow';
import { expandPath, loadStore, type Candidate, type SelectorStore } from './store';

export const DEFAULT_STORE_PATH = './.mendwright/overrides.json';
export const DEFAULT_TIMEOUT_MS = 10_000;

/** The actions a HealLocator runs with the heal canary. */
export const ACTIONS = ['click', 'dblclick', 'fill', 'type', 'press', 'pressSequentially', 'check', 'uncheck',
  'setChecked', 'selectOption', 'hover', 'tap', 'clear', 'focus', 'setInputFiles', 'dragTo'] as const;
/** The reads of a Locator that look at the page as it is now. The first one after a lookup
 * handles the registered interruptions first, as an action does. */
export const READS = ['textContent', 'innerText', 'innerHTML', 'getAttribute', 'inputValue', 'isVisible', 'isHidden',
  'isChecked', 'isEnabled', 'isDisabled', 'isEditable'] as const;

export type Action = typeof ACTIONS[number];
export type Read = typeof READS[number];

/** One heal a healer applied: the key, the selectors that had stopped matching, the new
 * XPath, the canary result (null while unconfirmed), and where it came from: 'fingerprint'
 * (no model call), 'model' or 'intent'. */
export interface HealEvent {
  key: string;
  old: string[];
  new: string;
  confirmed: boolean | null;
  source: string;
}

export interface OpenOptions {
  /** The model provider. Default: autoProvider(), from the environment. */
  provider?: HealProvider | null;
  /** Where heals are saved: MENDWRIGHT_STORE, else ./.mendwright/overrides.json. */
  storePath?: string;
  store?: SelectorStore;
  /** How long a lookup waits before it treats a selector as broken: MENDWRIGHT_TIMEOUT_MS, else 10000. */
  timeoutMs?: number;
  /** Ask for heals and check them, but apply none: MENDWRIGHT_DRY_RUN. */
  dryRun?: boolean;
  /** Keys that are never healed. */
  destructiveKeys?: Iterable<string>;
  /** While it returns false, heals are declined. */
  guard?: () => boolean;
  /** Model heal attempts per key per hour (default 3); null removes the limit. */
  ratePerKeyPerHour?: number | null;
  /** Model heal attempts per hour across all keys (default 60); null removes the limit. */
  rateGlobalPerHour?: number | null;
  guardrails?: Guardrails | null;
  journal?: JournalLike;
  /** 'reject' or 'accept' closes the consent banners of common consent managers. */
  cookies?: CookieChoice | null;
  /** Let a key heal on a page where it was never found. */
  healAcrossPages?: boolean;
  /** The fingerprint tier: MENDWRIGHT_FINGERPRINT, else on. */
  fingerprint?: boolean;
  /** Where mendwright's lines go (default: warnings on stderr). */
  log?: LogSink;
}

export interface LocatorOptions {
  role?: string;
  text?: string | string[];
  destructive?: boolean;
  timeoutMs?: number;
  pollMs?: number;
  optional?: boolean;
  intent?: string;
}

const TRUE = ['1', 'true', 'yes', 'on'];
const FALSE = ['0', 'false', 'no', 'off', ''];

/** true or false from a flag variable such as MENDWRIGHT_DRY_RUN=1, or null when unset. */
export function envFlag(name: string): boolean | null {
  const raw = process.env[name];
  if (raw === undefined) return null;
  const value = raw.trim().toLowerCase();
  if (TRUE.includes(value)) return true;
  if (FALSE.includes(value)) return false;
  throw new TypeError('mendwright: ' + name + ' must be 1 or 0, got ' + JSON.stringify(raw)
    + '. Set it to 1 to turn it on, or unset it.');
}

function envTimeout(): number | null {
  const raw = process.env.MENDWRIGHT_TIMEOUT_MS;
  if (raw === undefined || !raw.trim()) return null;
  const value = /^\d+$/.test(raw.trim()) ? Number(raw.trim()) : -1;
  if (value < 0) {
    throw new TypeError('mendwright: MENDWRIGHT_TIMEOUT_MS must be a whole number of milliseconds, got '
      + JSON.stringify(raw) + '. Fix it, or pass timeoutMs to open().');
  }
  return value;
}

/** Whether the fingerprint tier is on: the option, else MENDWRIGHT_FINGERPRINT, else on.
 * MENDWRIGHT_DISABLE=1 turns it off with every other heal. */
export function fingerprintSetting(fingerprint?: boolean): boolean {
  if (envFlag('MENDWRIGHT_DISABLE')) return false;
  if (fingerprint !== undefined) return Boolean(fingerprint);
  const value = envFlag('MENDWRIGHT_FINGERPRINT');
  return value === null ? true : value;
}

const USAGE = /\b(?:expected (?:float|integer|string|boolean|object|array|channel|binary|number|function|\w+), got \w+)/;

/** An error raised before an action reached the browser: a wrong argument. It says
 * nothing about the selector, so it never quarantines a heal. */
function isUsageError(error: unknown): boolean {
  return error instanceof TypeError || USAGE.test(String((error as Error)?.message ?? ''));
}

class Mutex {
  private tail: Promise<void> = Promise.resolve();

  async run<T>(fn: () => Promise<T>): Promise<T> {
    let release!: () => void;
    const next = new Promise<void>((r) => (release = r));
    const before = this.tail;
    this.tail = before.then(() => next);
    await before;
    try {
      return await fn();
    } finally {
      release();
    }
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

interface Budget {
  used: number;
}

interface Plan {
  roles: string[] | null;
  code: string[] | null;
  text: string | string[] | undefined;
  destructive: boolean | undefined;
  timeoutMs: number;
  pollMs: number;
  optional: boolean;
  intent: string | undefined;
}

// Healers made by open() that are still in use, for the Playwright Test fixture, which
// confirms the heals a test made when it passes.
const listeners = new Set<(healer: Healer) => void>();

export function addHealerListener(callback: (healer: Healer) => void): void {
  listeners.add(callback);
}

export function removeHealerListener(callback: (healer: Healer) => void): void {
  listeners.delete(callback);
}

function codeSelectors(key: string, selector: unknown): string[] | null {
  if (selector === undefined || selector === null) return null;
  const items = Array.isArray(selector) ? selector : [selector];
  if (!items.length) {
    throw new TypeError('mendwright: locator(' + pyRepr(key) + ', []) got an empty list. Pass at least one selector, '
      + "for example ['#save', \"//button[@id='save']\"].");
  }
  const out: string[] = [];
  for (const item of items) {
    try {
      checkSyntax(item);
    } catch (error) {
      if (error instanceof InvalidSelector) throw error.forKey(key);
      throw error;
    }
    out.push(normalize(item as string));
  }
  return [...new Set(out)];
}

/** What a page or selector call took as its options object: its timeout, as the lookup's. */
function timeoutOf(args: unknown[]): number | undefined {
  const last = args[args.length - 1];
  if (last && typeof last === 'object' && !Array.isArray(last)) {
    const value = (last as { timeout?: unknown }).timeout;
    if (typeof value === 'number' && value > 0) return Math.trunc(value);
  }
  return undefined;
}

export class Healer {
  readonly store: SelectorStore;
  readonly coordinator: HealCoordinator;
  readonly timeoutMs: number;
  /** The Page or Frame mendwright was opened on. */
  readonly native: PageLike;
  readonly driver: PlaywrightDriver;
  private readonly interruptions = new Map<string, Interruption>();
  private readonly handled = new Map<string, string>();
  private readonly interruptLog: InterruptionEvent[] = [];
  private readonly roleChecked = new Set<string>();
  private readonly fingerprinted = new Set<string>();
  private readonly severalSeen = new Set<string>();
  private readonly mutex = new Mutex();
  private pageProxy: Page | Frame | null = null;

  constructor(page: PageLike, driver: PlaywrightDriver, store: SelectorStore, coordinator: HealCoordinator,
              timeoutMs: number) {
    this.native = page;
    this.driver = driver;
    this.store = store;
    this.coordinator = coordinator;
    this.timeoutMs = timeoutMs;
    for (const callback of [...listeners]) {
      try {
        callback(this);
      } catch {
        // a listener that throws is ignored
      }
    }
  }

  /** The page to use in place of your own, so steps heal without changing them:
   * page.locator(selector) and the page methods that take a selector go through
   * heal.locator(selector, selector). Everything else is the real page's. */
  get page(): Page {
    if (!this.pageProxy) this.pageProxy = healingPage(this);
    return this.pageProxy as Page;
  }

  /** The interruptions this healer handled, oldest first. */
  get handledInterruptions(): InterruptionEvent[] {
    return [...this.interruptLog];
  }

  /** The heals this healer applied, oldest first. */
  get heals(): HealEvent[] {
    return this.coordinator.heals.map((h: HealRecord) => ({ ...h, old: [...h.old] }));
  }

  /** {key: healed xpath} for the heals that are not confirmed yet. */
  pending(): Record<string, string> {
    return Object.fromEntries(this.coordinator.pending);
  }

  /** Confirm (true) or quarantine (false) every pending heal, and return how many there were. */
  confirmAll(success: boolean, options: { by?: string; test?: string } = {}): number {
    const keys = [...this.coordinator.pending.keys()];
    for (const key of keys) this.coordinator.confirmPending(key, Boolean(success), { by: options.by ?? 'confirm', test: options.test });
    return keys.length;
  }

  /** Register a step that can show up at any point, such as a cookie banner: when it is in
   * the way of a step, mendwright performs its action (a click by default) first. A later
   * heal.locator(key) of the same key is skipped when its element is not there. null removes
   * it. */
  interrupt(key: string, selector: string | string[] | null,
            options: { action?: string; args?: unknown[]; role?: string } = {}): void {
    if (selector === null) {
      this.interruptions.delete(key);
      return;
    }
    const item = makeInterruption(key, selector, options);
    this.interruptions.delete(item.key);
    this.interruptions.set(item.key, item);
  }

  /** Resolve key when an action on it runs, healing and retrying it when its selectors miss.
   * selector is what your code says the element is: an XPath, CSS or Playwright selector, or
   * a list tried in order. role is the kind of element (mendwright reads it from the page when
   * left out). destructive: true marks a control that is never healed. optional: true marks a
   * step that may not be there. Throws TypeError or InvalidSelector at once for arguments
   * that can never work. */
  locator(key: string, selector?: string | string[] | null, options: LocatorOptions = {}): HealLocator {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('mendwright: heal.locator() needs a key, a non-empty name such as "email", got ' + pyRepr(key) + '.');
    }
    if (isGetByCall(key)) {
      throw new TypeError('mendwright: ' + pyRepr(key) + ' is how the Python package names a heal.page.get_by_*() call '
        + 'in the store, so heal.locator() cannot take it as a key. Pick another key.');
    }
    const intent = options.intent;
    if (intent !== undefined && (typeof intent !== 'string' || !intent.trim())) {
      throw new TypeError('mendwright: intent for ' + pyRepr(key) + ' takes a description of the element in words, got '
        + pyRepr(intent) + ". For example, intent: 'the terms checkbox in the signup form'.");
    }
    const roles = roleChoices(options.role);
    const code = codeSelectors(key, selector);
    this.checkIntent(key, code, roles, intent);
    return new HealLocator(this, key, {
      roles, code, text: options.text, destructive: options.destructive,
      timeoutMs: options.timeoutMs ?? this.timeoutMs, pollMs: options.pollMs ?? 200,
      optional: Boolean(options.optional), intent,
    });
  }

  /** True when one of key's stored selectors matches a single element now. No waiting and
   * no healing. */
  present(key: string): Promise<boolean> {
    return presentNow(this.store, this.driver, key);
  }

  // --- resolution --------------------------------------------------------------------------

  /** @internal */
  locate(key: string, plan: Plan): Promise<{ native: Locator | null; healed: string | null }> {
    return this.mutex.run(async () => {
      this.prime(key, plan);
      const firstWait = this.interruptions.has(key) && this.handled.has(key) ? 0 : plan.timeoutMs;
      const attempt = (wait: number = plan.timeoutMs) => resolve(this.store, this.driver, key,
        { timeoutMs: wait, pollMs: plan.pollMs });
      let native: Locator;
      try {
        native = await this.resolveStep(key, () => attempt(firstWait), plan);
      } catch (error) {
        if (error instanceof InterruptionLoop || !(error instanceof AttentionRequired)) throw error;
        const skip = this.interruptions.has(key) ? (this.handled.has(key) ? 'interruption handled' : 'interruption absent')
          : plan.optional ? 'optional' : null;
        if (skip) {
          this.coordinator.emit('info', skipMessage(key, skip, this.handled.get(key)));
          this.coordinator.journal.event('flow.skip', { key, reason: skip });
          return { native: null, healed: null };
        }
        native = await this.heal(key, error, attempt);
      }
      this.checkHealedTarget(key);
      await this.learn(key, plan.roles);
      this.rememberPage(key);
      await this.recordFingerprint(key);
      const winner = this.store.lastSuccess(key);
      const healed = winner && winner.source === 'heal' && typeof winner.xpath === 'string' ? winner.xpath : null;
      return { native, healed };
    });
  }

  private prime(key: string, plan: Plan): void {
    const store = this.store;
    if (plan.code) {
      store.setCodeCandidates(key, plan.code);
    } else if (!store.candidates(key).length) {
      if (plan.intent === undefined) {
        throw new AttentionRequired('mendwright: there is no stored selector for "' + key + '" yet, and none was passed. '
          + 'Pass a starting selector on the first call, for example heal.locator("' + key + '", "//your/xpath", '
          + "{ role: 'checkbox' }), or describe the element with intent and role so a model finds it on first use.");
      }
      this.checkIntent(key, plan.code, plan.roles, plan.intent);
    }
    this.applyMeta(key, plan.roles, plan.text, plan.destructive, plan.intent);
  }

  private checkIntent(key: string, code: string[] | null, roles: string[] | null, intent: string | undefined): void {
    if (intent === undefined || code || roles || this.store.candidates(key).length) return;
    const role = this.store.roleIsGuess(key) ? null : this.store.metaOf(key).expectedRole;
    if (role && role !== 'unknown') return;
    throw new TypeError("mendwright: '" + key + "' has no selector yet, so a model will look for it from intent, and "
      + "mendwright needs role to check what it finds. Pass role, for example heal.locator('" + key + "', null, "
      + '{ intent: ' + JSON.stringify(intent) + ", role: 'button' }).");
  }

  private applyMeta(key: string, roles: string[] | null, text: string | string[] | undefined,
                    destructive: boolean | undefined, intent: string | undefined): void {
    const store = this.store;
    if (intent !== undefined) store.setMeta(key, { intent });
    if (roles && roles.length) {
      const current = store.metaOf(key).expectedRole;
      if (roles.length === 1) {
        if (current !== roles[0] || store.roleIsGuess(key)) store.setMeta(key, { role: roles[0] });
      } else if (!roles.includes(current) || store.roleIsGuess(key)) {
        store.guessRole(key, roles[0], true);
      }
    }
    if (text !== undefined || destructive !== undefined) store.setMeta(key, { text, destructive });
    if ((!roles || !roles.length) && store.roleIsGuess(key)) {
      const top = store.candidates(key)[0];
      const guess = top ? inferRole(top.xpath) : 'unknown';
      if (guess !== 'unknown') store.guessRole(key, guess);
    }
  }

  private async resolveStep(key: string, attempt: () => Promise<Locator>, plan: Plan): Promise<Locator> {
    let missed: unknown;
    try {
      return await attempt();
    } catch (error) {
      if (!(error instanceof AttentionRequired)) throw error;
      missed = error;
    }
    const budget: Budget = { used: 0 };
    while (await this.handleInterruptions(key, budget, plan.timeoutMs, plan.pollMs)) {
      try {
        return await attempt();
      } catch (error) {
        if (!(error instanceof AttentionRequired)) throw error;
        missed = error;
      }
    }
    throw missed;
  }

  private flowContext(key: string): FlowContext {
    const url = this.driver.url();
    const meta = this.store.metaOf(key);
    return { url, page: pagePath(url), lastPage: meta.lastPage, pages: [...meta.pages] };
  }

  private async heal(key: string, missed: AttentionRequired, attempt: () => Promise<Locator>): Promise<Locator> {
    // A key last found inside an iframe (a heal the Python package saved there) is healed
    // inside that iframe by the Python package. This one does not heal inside an iframe, and
    // the page around it may hold an element that looks the same, so it does not look.
    const found = this.store.candidates(key).filter((c) => c.lastSucceededAt);
    const latest = found.length
      ? found.reduce((a, b) => (String(b.lastSucceededAt) > String(a.lastSucceededAt) ? b : a)) : null;
    if (latest && latest.frameXPath) {
      const reason = "'" + key + "' was last found inside the iframe " + latest.frameXPath + ', and mendwright for '
        + 'TypeScript does not heal inside an iframe yet, so it did not look for the element on the page around it. '
        + 'Fix the selector by hand, or let the Python package heal it.';
      this.coordinator.decline(key, reason);
      throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
    }
    // The same for a key last found inside an open shadow root: where the heal that matched
    // last runs, else the key's shadowHosts. Neither the page's HTML nor its elements hold
    // what a shadow root contains, so a heal here could only find something outside it.
    // Refused after the checks for a key marked destructive and the page guard.
    let hosts = hostsOf(latest);
    if (!hosts.length) hosts = this.store.shadowHosts(key);
    const refusal = hosts.length
      ? "'" + key + "' was last found inside " + placeText(hosts) + ', and mendwright for TypeScript does not heal '
        + 'inside a shadow root yet, so it did not look for the element on the page around it, where an element that '
        + 'looks the same could be. Fix the selector by hand, or let the Python package heal it.'
      : null;
    return resolveWithHeal(() => attempt(), this.coordinator, key, this.flowContext(key), missed, refusal);
  }

  /** Where the candidate that just matched runs (see shadow.runTarget), or null when it
   * cannot run here. */
  private winnerTarget(key: string): Target | null {
    const winner: Candidate | null = this.store.lastSuccess(key);
    if (!winner || typeof winner.xpath !== 'string') return null;
    try {
      return runTarget(winner);
    } catch {
      return null;
    }
  }

  /** Refuse a match that came from a heal when the key is marked destructive: the heal is
   * quarantined and nothing acts on it. */
  private checkHealedTarget(key: string): void {
    const winner = this.store.lastSuccess(key);
    if (!winner || winner.source !== 'heal' || !this.store.metaOf(key).destructive) return;
    const xpath = winner.xpath;
    if (this.coordinator.isPending(key, xpath)) {
      this.coordinator.confirmPending(key, false, { xpath, by: 'destructive' });
    } else {
      this.store.quarantine(key, xpath);
      this.coordinator.journal.event('heal.quarantine', { key, xpath });
    }
    const fix = this.store.isLockedDestructive(key) ? 'Fix the selector in your code.'
      : 'Fix the selector in your code, or pass destructive: false to heal.locator() if this control is safe to act on.';
    throw new AttentionRequired("mendwright: '" + key + "' matched " + xpath + ', a selector an earlier heal saved, but '
      + "'" + key + "' is marked as a destructive control, and a destructive control never acts through a healed "
      + 'selector. mendwright quarantined that selector and did not act on it. ' + fix);
  }

  /** After a match, learn from the live element: with role left out, save the role it has;
   * with one role given, warn once if the element disagrees. */
  private async learn(key: string, roles: string[] | null): Promise<void> {
    const userRole = roles && roles.length === 1 ? roles[0] : null;
    const needRole = userRole === null && this.store.roleIsGuess(key);
    const checkRole = userRole !== null && !this.roleChecked.has(key);
    if (!needRole && !checkRole) return;
    const target = this.winnerTarget(key);
    const described = target ? await this.driver.describe(target.selector, target.frame, target.hosts).catch(() => null) : null;
    if (needRole) {
      if (described) {
        let live = described.role || '';
        if (roles && live && !roles.includes(live)) {
          this.coordinator.emit('warning', 'role for ' + pyRepr(key) + ' means one of ' + roles.join('/') + ', but the element it matched '
            + "is a '" + live + "' to mendwright's gate, so a heal for it would be rejected. Pass role: '" + live
            + "', or leave role out and mendwright reads it from the page.");
          live = roles[0];
        } else if (roles && !live) {
          live = roles[0];
        }
        if (live) this.store.setMeta(key, { role: live });
      }
    }
    if (checkRole) {
      this.roleChecked.add(key);
      const live = described?.role;
      if (live && live !== userRole) {
        this.coordinator.emit('warning', "role '" + userRole + "' was given for " + pyRepr(key) + ", but the element it matched is a '"
          + live + "' to mendwright's gate, so a heal for it would be rejected. Pass role: '" + live
          + "', or leave role out and mendwright reads it from the page.");
      }
    }
  }

  /** Save the page key's element was just found on, and its address. */
  private rememberPage(key: string): void {
    const url = this.driver.url();
    const page = pagePath(url);
    this.store.batch(() => {
      if (page) this.store.setLastPage(key, page);
      if (page || !this.store.metaOf(key).lastPage) {
        const address = pageUrl(url);
        if (address && this.store.lastUrl(key) !== address && this.driver.topLevel()) this.store.setLastUrl(key, address);
      }
    });
  }

  /** Read and save the fingerprint of the element that just matched, once per key and
   * selector per healer: not for a key marked destructive, nor for a heal made in this
   * process that is not confirmed yet. The same read gives where the element is, saved as
   * the key's shadowHosts: the shadow hosts around it, or none for an element on the page. */
  private async recordFingerprint(key: string): Promise<void> {
    if (!this.coordinator.fingerprint) return;
    const winner = this.store.lastSuccess(key);
    if (!winner || typeof winner.xpath !== 'string') return;
    const id = JSON.stringify([key, winner.xpath]);
    if (this.fingerprinted.has(id) || this.coordinator.pending.get(key) === winner.xpath) return;
    if (this.store.metaOf(key).destructive) return;
    this.fingerprinted.add(id);
    const target = this.winnerTarget(key);
    if (!target) return;
    const read = await this.driver.fingerprintRead(target.selector, target.frame, target.hosts)
      .catch(() => ({ fp: null, hosts: undefined }));
    if (read.fp) {
      try {
        this.store.setMeta(key, read.hosts === undefined ? { fingerprint: read.fp }
          : { fingerprint: read.fp, shadowHosts: read.hosts });
      } catch {
        // a fingerprint that cannot be saved is not recorded
      }
    }
  }

  // --- interruptions -----------------------------------------------------------------------

  /** A selector that points at one visible element of the interruption now, or null. */
  private async interruptionTarget(item: Interruption, warn = true): Promise<string | null> {
    const driver = this.driver;
    let selectors = item.selectors;
    if (item.probe) {
      try {
        if (!(await driver.count(item.probe))) selectors = selectors.filter((s) => classify(s) !== XPATH);
      } catch {
        // look at each selector then
      }
    }
    let several: [string, number, number] | null = null;
    for (const selector of selectors) {
      let count: number;
      try {
        count = await driver.count(selector);
      } catch {
        continue;
      }
      const targets = nthTargets(selector, count);
      for (const target of targets) {
        try {
          if ((await driver.isVisible(target)) && (await this.roleFits(item, target))) return target;
        } catch {
          continue;
        }
      }
      if (count > targets.length && several === null) several = [selector, count, targets.length];
    }
    if (several && warn && !item.preset) {
      const seen = JSON.stringify([item.key, pagePath(driver.url())]);
      if (!this.severalSeen.has(seen)) {
        this.severalSeen.add(seen);
        this.coordinator.emit('warning', severalMessage(item.key, ...several));
      }
    }
    return null;
  }

  private async roleFits(item: Interruption, target: string): Promise<boolean> {
    if (!item.roles.length) return true;
    const described = await this.driver.describe(target);
    return Boolean(described) && item.roles.includes(described!.role);
  }

  /** Handle each registered interruption (other than before itself) that the page shows
   * now. True when one was handled. */
  private async handleInterruptions(before: string, budget: Budget, timeoutMs: number, pollMs: number): Promise<boolean> {
    let handled = false;
    for (const [key, item] of [...this.interruptions]) {
      if (key === before) continue;
      const target = await this.interruptionTarget(item);
      if (target === null) continue;
      if (budget.used >= MAX_INTERRUPTIONS_PER_STEP) throw new InterruptionLoop(loopMessage(before, item.key, budget.used));
      budget.used += 1;
      if (await this.runInterruption(item, target, before, timeoutMs, pollMs)) handled = true;
    }
    return handled;
  }

  private async runInterruption(item: Interruption, target: string, before: string, timeoutMs: number,
                                pollMs: number): Promise<boolean> {
    const loc = this.driver.locator(target) as unknown as Record<string, (...args: unknown[]) => Promise<unknown>>;
    const positional = ['fill', 'type', 'press', 'pressSequentially', 'selectOption', 'setChecked', 'setInputFiles', 'dragTo']
      .includes(item.action) ? 1 : 0;
    const args = item.args.length === positional ? [...item.args, { timeout: Math.max(timeoutMs, 1000) }] : [...item.args];
    try {
      await loc[item.action](...args);
    } catch (error) {
      if (isUsageError(error)) throw error;
      this.coordinator.emit('warning', unhandledMessage(item.key, before, error));
      return false;
    }
    // Wait, up to SETTLE_MS or the step's timeout, for it to go away (a banner that fades out).
    const limit = Math.min(Math.max(timeoutMs, 0), SETTLE_MS);
    const deadline = Date.now() + limit;
    while (limit > 0 && (await this.interruptionTarget(item, false)) !== null && Date.now() < deadline) {
      await sleep(Math.max(pollMs, 20));
    }
    if (!this.handled.has(item.key)) this.handled.set(item.key, before);
    this.interruptLog.push({ key: item.key, before });
    this.coordinator.emit('warning', handledMessage(item.key, before));
    this.coordinator.journal.event('flow.interrupt', {
      key: item.key, before, action: item.action, preset: item.preset, selector: item.preset ? null : target,
    });
    return true;
  }

  /** @internal Handle what the page shows before an action or the first read of key. */
  async beforeAction(key: string, timeoutMs: number, pollMs: number): Promise<Budget> {
    const budget: Budget = { used: 0 };
    if (this.interruptions.size) await this.handleInterruptions(key, budget, timeoutMs, pollMs);
    return budget;
  }

  /** @internal After an action was intercepted: true when an interruption was handled. */
  async unblock(key: string, budget: Budget, timeoutMs: number, pollMs: number): Promise<boolean> {
    if (!this.interruptions.size) return false;
    return this.handleInterruptions(key, budget, timeoutMs, pollMs);
  }

  /** @internal */
  reportNothingMatched(key: string): void {
    const others = [...this.interruptions.values()].filter((item) => item.key !== key);
    if (!others.length) return;
    this.coordinator.emit('warning', nothingMatchedMessage(key, others.filter((i) => !i.preset).map((i) => i.key),
      others.some((i) => i.preset)));
  }

  /** @internal heal.page's reads: whether selector should go to the real page as written. It
   * does when it matches something now, but not the one visible element mendwright looks for,
   * and none of the key's saved selectors finds one either. */
  async passesThrough(key: string, selector: string): Promise<boolean> {
    let count: number;
    try {
      count = await this.driver.count(selector);
    } catch {
      return false;
    }
    if (!count) return false;
    if (count === 1) {
      try {
        if (await this.driver.isVisible(selector)) return false;
      } catch {
        return false;
      }
    }
    try {
      return !(await this.present(key));
    } catch {
      return true;
    }
  }

  /** @internal */
  knows(key: string): boolean {
    return this.store.candidates(key).length > 0;
  }

  /** @internal */
  peekVisible(key: string): Promise<boolean> {
    return peekVisible(this.store, this.driver, key);
  }
}

/** The element a healer resolves, with the heal canary attached. Its actions resolve the key
 * first (and heal it when its selectors broke), then run on the real Locator, with the
 * options you pass. A successful action does not confirm a heal by itself: pass
 * { expect: () => ... } with the action's options, call confirm(), or let the Playwright Test
 * fixture confirm the test's heals when it passes. An action that fails in the browser
 * quarantines a heal made for it. */
export class HealLocator {
  readonly key: string;
  private readonly healer: Healer;
  private readonly plan: Plan;
  private resolved: Promise<{ native: Locator | null; healed: string | null }> | null = null;
  private nativeLocator: Locator | null = null;
  private isPresent = true;
  private readsDue = true;

  constructor(healer: Healer, key: string, plan: Plan) {
    this.healer = healer;
    this.key = key;
    this.plan = plan;
  }

  /** Resolve the key now (healing it if needed), and return this locator. */
  async resolve(): Promise<this> {
    await this.ensure();
    return this;
  }

  private async ensure(): Promise<Locator | null> {
    if (!this.resolved) this.resolved = this.healer.locate(this.key, this.plan);
    let result;
    try {
      result = await this.resolved;
    } catch (error) {
      this.resolved = null;   // a later call looks again
      throw error;
    }
    this.nativeLocator = result.native;
    this.isPresent = result.native !== null;
    return result.native;
  }

  /** The real Playwright Locator, once resolved. null for a skipped step. */
  get native(): Locator | null {
    if (!this.resolved) {
      throw new Error("mendwright: '" + this.key + "' has not been resolved yet, so there is no native Locator. "
        + "Resolve it first: const box = await heal.locator('" + this.key + "', ...).resolve().");
    }
    return this.nativeLocator;
  }

  /** False when the step was skipped because its element was not there (optional: true, or
   * an interruption), once resolved. */
  get present(): boolean {
    if (!this.resolved) {
      throw new Error("mendwright: '" + this.key + "' has not been resolved yet, so mendwright does not know whether "
        + "it is on the page. Resolve it first: const box = await heal.locator('" + this.key + "', ...).resolve().");
    }
    return this.isPresent;
  }

  /** Confirm (true) or quarantine (false) a heal of this key, from a check of your own. */
  confirm(success: boolean): this {
    this.healer.coordinator.confirmPending(this.key, Boolean(success), { by: 'confirm' });
    return this;
  }

  /** @internal */
  async act(verb: Action, args: unknown[]): Promise<unknown> {
    const native = await this.ensure();
    if (native === null) return undefined;   // a skipped step: nothing to act on
    let expect: (() => unknown) | undefined;
    const last = args[args.length - 1];
    if (last && typeof last === 'object' && !Array.isArray(last) && typeof (last as { expect?: unknown }).expect === 'function') {
      const { expect: check, ...rest } = last as { expect: () => unknown };
      expect = check;
      args = [...args.slice(0, -1), ...(Object.keys(rest).length ? [rest] : [])];
    }
    for (let i = 0; i < args.length; i++) {
      if (args[i] instanceof HealLocator) args[i] = await (args[i] as HealLocator).ensure();
    }
    const { timeoutMs, pollMs } = this.plan;
    const healer = this.healer;
    const run = () => (native as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[verb](...args);
    const budget = await healer.beforeAction(this.key, timeoutMs, pollMs);
    this.readsDue = false;
    let result: unknown;
    try {
      result = await run();
    } catch (error) {
      if (isUsageError(error)) throw error;
      const intercepted = isInterceptionError(error);
      const used = budget.used;
      if (!(intercepted && (await healer.unblock(this.key, budget, timeoutMs, pollMs)))) {
        if (intercepted && budget.used === used) healer.reportNothingMatched(this.key);
        this.healer.coordinator.confirmPending(this.key, false, { by: 'action' });
        throw error;
      }
      try {
        result = await run();   // once, after an interruption was handled
      } catch (again) {
        if (isUsageError(again)) throw again;
        this.healer.coordinator.confirmPending(this.key, false, { by: 'action' });
        throw again;
      }
    }
    if (expect) {
      let ok = false;
      try {
        ok = Boolean(await expect());
      } catch {
        ok = false;
      }
      this.healer.coordinator.confirmPending(this.key, ok, { by: 'expect' });
    }
    return result;
  }

  /** @internal */
  async read(name: Read, args: unknown[]): Promise<unknown> {
    const native = await this.ensure();
    if (native === null) throw new Error(absentMessage(this.key));
    if (this.readsDue) {
      await this.healer.beforeAction(this.key, this.plan.timeoutMs, this.plan.pollMs);
      this.readsDue = false;
    }
    return (native as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[name](...args);
  }

  click(options?: Parameters<Locator['click']>[0] & ExpectOption): Promise<void> {
    return this.act('click', [options]) as Promise<void>;
  }
  dblclick(options?: Parameters<Locator['dblclick']>[0] & ExpectOption): Promise<void> {
    return this.act('dblclick', [options]) as Promise<void>;
  }
  fill(value: string, options?: Parameters<Locator['fill']>[1] & ExpectOption): Promise<void> {
    return this.act('fill', [value, options]) as Promise<void>;
  }
  type(text: string, options?: Parameters<Locator['type']>[1] & ExpectOption): Promise<void> {
    return this.act('type', [text, options]) as Promise<void>;
  }
  press(key: string, options?: Parameters<Locator['press']>[1] & ExpectOption): Promise<void> {
    return this.act('press', [key, options]) as Promise<void>;
  }
  pressSequentially(text: string, options?: Parameters<Locator['pressSequentially']>[1] & ExpectOption): Promise<void> {
    return this.act('pressSequentially', [text, options]) as Promise<void>;
  }
  check(options?: Parameters<Locator['check']>[0] & ExpectOption): Promise<void> {
    return this.act('check', [options]) as Promise<void>;
  }
  uncheck(options?: Parameters<Locator['uncheck']>[0] & ExpectOption): Promise<void> {
    return this.act('uncheck', [options]) as Promise<void>;
  }
  setChecked(checked: boolean, options?: Parameters<Locator['setChecked']>[1] & ExpectOption): Promise<void> {
    return this.act('setChecked', [checked, options]) as Promise<void>;
  }
  selectOption(values: Parameters<Locator['selectOption']>[0],
               options?: Parameters<Locator['selectOption']>[1] & ExpectOption): Promise<string[]> {
    return this.act('selectOption', [values, options]) as Promise<string[]>;
  }
  hover(options?: Parameters<Locator['hover']>[0] & ExpectOption): Promise<void> {
    return this.act('hover', [options]) as Promise<void>;
  }
  tap(options?: Parameters<Locator['tap']>[0] & ExpectOption): Promise<void> {
    return this.act('tap', [options]) as Promise<void>;
  }
  clear(options?: Parameters<Locator['clear']>[0] & ExpectOption): Promise<void> {
    return this.act('clear', [options]) as Promise<void>;
  }
  focus(options?: Parameters<Locator['focus']>[0] & ExpectOption): Promise<void> {
    return this.act('focus', [options]) as Promise<void>;
  }
  setInputFiles(files: Parameters<Locator['setInputFiles']>[0],
                options?: Parameters<Locator['setInputFiles']>[1] & ExpectOption): Promise<void> {
    return this.act('setInputFiles', [files, options]) as Promise<void>;
  }
  dragTo(target: Locator | HealLocator, options?: Parameters<Locator['dragTo']>[1] & ExpectOption): Promise<void> {
    return this.act('dragTo', [target, options]) as Promise<void>;
  }

  textContent(options?: Parameters<Locator['textContent']>[0]): Promise<string | null> {
    return this.read('textContent', [options]) as Promise<string | null>;
  }
  innerText(options?: Parameters<Locator['innerText']>[0]): Promise<string> {
    return this.read('innerText', [options]) as Promise<string>;
  }
  innerHTML(options?: Parameters<Locator['innerHTML']>[0]): Promise<string> {
    return this.read('innerHTML', [options]) as Promise<string>;
  }
  getAttribute(name: string, options?: Parameters<Locator['getAttribute']>[1]): Promise<string | null> {
    return this.read('getAttribute', [name, options]) as Promise<string | null>;
  }
  inputValue(options?: Parameters<Locator['inputValue']>[0]): Promise<string> {
    return this.read('inputValue', [options]) as Promise<string>;
  }
  isVisible(options?: Parameters<Locator['isVisible']>[0]): Promise<boolean> {
    return this.read('isVisible', [options]) as Promise<boolean>;
  }
  isHidden(options?: Parameters<Locator['isHidden']>[0]): Promise<boolean> {
    return this.read('isHidden', [options]) as Promise<boolean>;
  }
  isChecked(options?: Parameters<Locator['isChecked']>[0]): Promise<boolean> {
    return this.read('isChecked', [options]) as Promise<boolean>;
  }
  isEnabled(options?: Parameters<Locator['isEnabled']>[0]): Promise<boolean> {
    return this.read('isEnabled', [options]) as Promise<boolean>;
  }
  isDisabled(options?: Parameters<Locator['isDisabled']>[0]): Promise<boolean> {
    return this.read('isDisabled', [options]) as Promise<boolean>;
  }
  isEditable(options?: Parameters<Locator['isEditable']>[0]): Promise<boolean> {
    return this.read('isEditable', [options]) as Promise<boolean>;
  }
}

export interface ExpectOption {
  /** Called after the action: something truthy confirms a heal made for this key, anything
   * else (or a throw) quarantines it. */
  expect?: () => unknown;
}

// --- heal.page -------------------------------------------------------------------------------

const PAGE_SHORTCUTS = ['click', 'dblclick', 'fill', 'check', 'uncheck', 'selectOption', 'press', 'type', 'hover',
  'isChecked', 'textContent', 'innerText', 'getAttribute', 'inputValue'];
const LOCATOR_HEALED = new Set<string>([...ACTIONS, 'isChecked', 'textContent', 'innerText', 'innerHTML',
  'getAttribute', 'inputValue', 'isEnabled', 'isDisabled', 'isEditable']);
// Playwright runs these on an element in any state, hidden included: a selector that matches
// something goes to the real page for them, unless a saved heal finds the element.
const ANY_STATE = new Set(['isChecked', 'textContent', 'innerText', 'innerHTML', 'getAttribute', 'inputValue',
  'isEnabled', 'isDisabled', 'isEditable', 'setInputFiles', 'focus']);

function healable(selector: unknown): selector is string {
  if (typeof selector !== 'string') return false;
  try {
    checkSyntax(selector);
    return true;
  } catch {
    return false;
  }
}

function bound(target: object, name: string | symbol): unknown {
  const value = Reflect.get(target, name, target);
  return typeof value === 'function' ? value.bind(target) : value;
}

/** heal.page.locator(selector): its actions and waiting reads go through
 * heal.locator(selector, selector); everything else is the real Locator's. */
function healingLocator(healer: Healer, target: Locator, selector: string): Locator {
  return new Proxy(target, {
    get(t, name) {
      if (name === 'native') return t;
      if (typeof name === 'string' && LOCATOR_HEALED.has(name)) {
        return async (...args: unknown[]) => {
          if (ANY_STATE.has(name) && (await healer.passesThrough(selector, selector))) {
            return (t as unknown as Record<string, (...a: unknown[]) => unknown>)[name](...args);
          }
          const proxy = healer.locator(selector, selector, { timeoutMs: timeoutOf(args) });
          return (proxy as unknown as Record<string, (...a: unknown[]) => unknown>)[name](...args);
        };
      }
      if (name === 'isVisible') {
        return async (...args: unknown[]) => {
          if (!healer.knows(selector)) return t.isVisible(...(args as []));
          return healer.peekVisible(selector);
        };
      }
      return bound(t, name);
    },
  });
}

/** heal.page: see Healer.page. */
function healingPage(healer: Healer): Page | Frame {
  const target = healer.native;
  return new Proxy(target, {
    get(t, name) {
      if (name === 'native') return t;
      if (name === 'locator') {
        return (selector: string, ...rest: unknown[]) => {
          const real = (t as Page).locator(selector, ...(rest as []));
          if (rest.some((r) => r !== undefined) || !healable(selector)) return real;
          return healingLocator(healer, real, selector);
        };
      }
      if (name === 'isVisible') {
        return async (selector: string, ...rest: unknown[]) => {
          if (!healable(selector) || !healer.knows(selector)) return (t as Page).isVisible(selector, ...(rest as []));
          return healer.peekVisible(selector);
        };
      }
      if (typeof name === 'string' && PAGE_SHORTCUTS.includes(name)) {
        return async (selector: string, ...args: unknown[]) => {
          if (!healable(selector) || (ANY_STATE.has(name) && (await healer.passesThrough(selector, selector)))) {
            return (t as unknown as Record<string, (...a: unknown[]) => unknown>)[name](selector, ...args);
          }
          // A Locator is always strict and takes no strict option.
          const options = args[args.length - 1];
          if (options && typeof options === 'object' && !Array.isArray(options) && 'strict' in options) {
            const { strict: _strict, ...rest } = options as Record<string, unknown>;
            args = [...args.slice(0, -1), rest];
          }
          const proxy = healer.locator(selector, selector, { timeoutMs: timeoutOf(args) });
          return (proxy as unknown as Record<string, (...a: unknown[]) => unknown>)[name](...args);
        };
      }
      return bound(t, name);
    },
  }) as Page | Frame;
}

// --- open() --------------------------------------------------------------------------------------

let warnedNoProvider = false;

function emit(sink: LogSink | undefined, level: 'warning' | 'info', message: string): void {
  if (sink) {
    try {
      sink(level, message);
    } catch {
      // a broken sink must not break open()
    }
  } else {
    log(level, message);
  }
}

function unwrap(page: unknown): PageLike {
  const native = (page as { native?: unknown })?.native;
  return (native && typeof native === 'object' ? native : page) as PageLike;
}

function isPageLike(page: unknown): page is PageLike {
  return !!page && typeof (page as Page).locator === 'function' && typeof (page as Page).frameLocator === 'function'
    && typeof (page as Page).content === 'function' && typeof (page as Page).evaluate === 'function';
}

/** Wrap a Playwright Page (or Frame) into a Healer. See OpenOptions for the settings;
 * an option left out comes from the environment. MENDWRIGHT_DISABLE=1 turns healing off:
 * selectors still resolve, nothing is healed and no provider is called. */
export function open(page: Page | Frame, options: OpenOptions = {}): Healer {
  const native = unwrap(page);
  if (!isPageLike(native)) {
    throw new TypeError('mendwright: open() takes a Playwright Page or Frame. A Locator points at elements, not at a '
      + 'page: pass locator.page() instead.');
  }
  const cookies = options.cookies;
  if (cookies !== undefined && cookies !== null && !COOKIE_CHOICES.includes(cookies)) {
    throw new TypeError(cookieChoiceError(cookies));
  }
  const storePath = path.resolve(expandPath(options.storePath ?? process.env.MENDWRIGHT_STORE ?? DEFAULT_STORE_PATH));
  const timeoutMs = options.timeoutMs ?? envTimeout() ?? DEFAULT_TIMEOUT_MS;
  const dryRun = options.dryRun ?? Boolean(envFlag('MENDWRIGHT_DRY_RUN'));
  const fingerprint = fingerprintSetting(options.fingerprint);
  const disabled = Boolean(envFlag('MENDWRIGHT_DISABLE'));
  let provider: HealProvider;
  if (disabled) {
    provider = new NullProvider(DISABLED_REASON);
  } else if (options.provider) {
    provider = options.provider;
  } else {
    try {
      provider = autoProvider();
    } catch (error) {
      let reason = String((error as Error).message).replace(/^mendwright: /, '').trim();
      if (!reason.includes('provider')) reason = reason.replace(/\.$/, '') + '. Alternatively, pass provider to open().';
      // Once per process on stderr: the Playwright Test fixture opens a healer for each test.
      if (options.log || !warnedNoProvider) {
        warnedNoProvider = true;
        emit(options.log, 'warning', 'open(): no model provider is available, so a broken selector is repaired only when '
          + (fingerprint ? 'its fingerprint finds the element with confidence (no model call). ' : 'never. ') + reason);
      }
      provider = new NullProvider(reason, 'no model provider is configured (see the open() warning)');
    }
  }
  const store = options.store ?? loadStore(storePath, { destructiveKeys: options.destructiveKeys });
  const storeFile = store.overridePath;
  const journal = options.journal ?? new Journal(path.join(path.dirname(storeFile), 'journal.jsonl'));
  let guardrails = options.guardrails ?? null;
  if (guardrails === null && !(provider instanceof NullProvider)) {
    guardrails = sharedLimiter(storeFile, options.ratePerKeyPerHour === undefined ? 3 : options.ratePerKeyPerHour,
      options.rateGlobalPerHour === undefined ? 60 : options.rateGlobalPerHour);
  }
  const driver = new PlaywrightDriver(native);
  const coordinator = new HealCoordinator(store, {
    provider, driver, journal, guard: options.guard, guardrails, dryRun, fingerprint,
    healAcrossPages: options.healAcrossPages, disabled, log: options.log,
  });
  const healer = new Healer(native, driver, store, coordinator, timeoutMs);
  if (cookies) {
    const preset = cookiePreset(cookies);
    (healer as unknown as { interruptions: Map<string, Interruption> }).interruptions.set(preset.key, preset);
  }
  return healer;
}
