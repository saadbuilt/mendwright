// The healer: open(page) gives one, and heal.locator(key, selector) resolves the key when
// an action on it runs, heals it when its selectors broke, retries the step and arms the
// canary (Python's facade.Healer and aio.AsyncHealer). The selector in your code always
// wins over stored ones, the element's role is read from the page the first time it
// matches, and its fingerprint is recorded each time it matches, for the fingerprint tier.
//
// It also handles registered interruptions (heal.interrupt(), and the cookies preset),
// optional steps, and the page guard: a key is not healed on a page it was never found on.
// An interruption that needs a form is answered by its run function, which gets heal.page:
// its own calls run in an async context marked as inside it (RUN_CONTEXT), so they never
// handle its own interruption, dismiss no popup and skip the healer's mutex, which the step
// that called it holds while it waits.
//
// heal.step() and heal.runSteps() run a flow whose steps the site may show in any order (see
// steps.ts). The step whose function runs now lives in an async context too (STEP_CONTEXT):
// a key found during it is saved with the step's name, and while it runs again, such a key
// heals on the page the step showed on, whatever its address.
//
// Open shadow roots (see shadow.ts and scope.ts): each fingerprint read also reads where the
// element is, and saves it as the key's shadowHosts, as the Python package does. A key last
// found inside a shadow root heals only inside it, entered strictly through its hosts, never on
// the page around its host, where a look-alike can be, and is refused when that shadow root
// cannot be entered now. A heal saved inside one runs strictly there. A key whose own XPath
// matches nothing on the page but one element inside one open shadow root is followed there,
// through a CSS form of that element, with no model call (followIntoShadow).
//
// Iframes one level down (see frames.ts and scope.ts): a key whose own selector matches
// nothing on the page but one element inside one iframe is followed there, with no model call
// (followIntoFrame), and a key last found inside an iframe that is on the page now heals inside
// it, never on the page around it.
//
// With open({ healGetBy: true }), heal.page.getByRole() and the six other getBy* calls are looked
// up and healed too, under a key written as the call (see getby.ts and getByLocator): the call
// itself first, a saved heal only while it matches nothing, and a heal from its fingerprint only,
// never from a model. A call that matches several elements, or one hidden element, goes to
// Playwright as written, and a miss throws GetByNotFound, which is Playwright's TimeoutError too.

import { AsyncLocalStorage } from 'node:async_hooks';
import * as path from 'node:path';

import type { Frame, Locator, Page } from '@playwright/test';

import {
  DISABLED_REASON, HealCoordinator, resolveWithHeal, type FlowContext, type HealRecord, type ListOutcome, type Refusal,
} from './coordinator';
import {
  NOT_LOOKED_AT, asksAnswer, asksConsent, controlText, expectControl, expectOf, isConsent, parseProbe, pick, popupOf,
  type Popup, type Probe,
} from './dismiss';
import {
  PlaywrightDriver, UNSURE, driverPopupClick, driverPopupControls, driverPopupProbe, driverPopupRelease, driverPopupRoot,
  driverPressEscape, type PageLike, type Placement, type PopupRoot,
} from './driver';
import { AttentionRequired, GetByNotFound, InterruptionLoop, InvalidSelector } from './errors';
import {
  DISMISS_KEY, MAX_INTERRUPTIONS_PER_STEP, SETTLE_MS, absentMessage, alertMessage, consentMessage,
  dismissLoopMessage, dismissedMessage, errorType, handledMessage, isInterceptionError, loopMessage, lostMessage,
  nothingMatchedMessage, pagePath, pageUrl, runFailedMessage, runPlainMessage, runRemovesMessage, severalMessage,
  skipMessage, stepText, undismissedMessage, unhandledMessage,
} from './flow';
import { frameCandidates, frameMessage, framePlan, type FrameMatch } from './frames';
import { liveReason } from './gate';
import {
  HIDDEN, NOT_FOLLOWED, REJECTED, STARTED_TO_MATCH, TS_METHODS, argText as plainArgText, callOf, callText as plainCallText,
  canonical, canonicalKey, isKey, notHealedMessage, reservedKeyMessage, throughMessage, type GetByCall,
} from './getby';
import {
  COOKIE_BANNER_KEY, COOKIE_CHOICES, cookieChoiceError, cookiePreset, functionName, makeInterruption, nthTargets,
  type CookieChoice, type InterruptOptions, type Interruption, type InterruptionEvent, type RunFunction,
} from './interruptions';
import { Journal, runId, runIdProblem, type JournalLike } from './journal';
import { sharedLimiter, type Guardrails } from './limits';
import {
  itemRole, nonemptyArgsError, placed as listPlaced, tierOffText, validList, type ListFingerprint,
} from './lists';
import { log, type LogSink } from './log';
import { NullProvider, type HealProvider } from './providers/base';
import { autoProvider } from './providers/detect';
import { pyRepr } from './py';
import {
  THROUGH, noMatchMessage, peekVisible, present as presentNow, presentGetBy, requireVisible, resolve, resolveGetBy,
  splitRunnable,
} from './resolver';
import { inferRole, roleChoices } from './roles';
import { driverShadowed, followAllowed, healPlace, shadowUnreachable, type HealScope } from './scope';
import { CSS, XPATH, checkSyntax, classify, normalize } from './selectors';
import {
  hostsOf, placeText, runTarget, shadowFound, shadowMessage, shadowPlan, shadowText, type ShadowFound, type Target,
} from './shadow';
import {
  StepRun, WAITING_STEP, makeStep, plainMessage, registerStep, runArgs, scopeMessage, shows, type RunStepsOptions,
  type Step, type StepFunction, type StepOptions,
} from './steps';
import { DEFAULT_STORE_PATH, loadStore, localStorePath, type Candidate, type KeyMeta, type SelectorStore } from './store';
import {
  VALUE_READS, expectation, expectations, refusalReason, refusedMessage, type ExpectValue, type Expectation,
} from './values';

export { DEFAULT_STORE_PATH } from './store';
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

/** The reads that answer at once for a Locator that matches nothing, as Playwright's do, and
 * what they answer: the element went away, as a dialog does once its close button is clicked.
 * For an element found inside a shadow root they answer so too, instead of waiting for it
 * (see HealLocator.waitPlaced). */
const ABSENT_ANSWERS: ReadonlyMap<string, boolean> = new Map([['isVisible', false], ['isHidden', true]]);

/** One heal a healer applied: the key, the selectors that had stopped matching, the new
 * selector (an XPath, or inside an open shadow root the chain of its hosts and a CSS
 * selector), the canary result (null while unconfirmed), and where it came from:
 * 'fingerprint' (no model call), 'model', 'intent', 'frame' for a key whose own selector
 * matched only inside an iframe and is looked up there from now on, or 'shadow' for one whose
 * own XPath matched only inside an open shadow root and is looked up there through a CSS form
 * of its element (no model call for these two; old is that selector, and new the selector
 * saved there). */
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
  /** Dismiss a popup nobody registered when it covers the element of an action: Escape, its
   * close button, or a decline button such as No thanks, never one that accepts, subscribes,
   * pays or goes somewhere. Off by default. */
  dismiss?: boolean;
  /** { key: check }: expectValue (see LocatorOptions) for those keys on every lookup, those
   * heal.page makes too, whose key is the selector (or, for a getBy* call, getByKey()'s key). An
   * expectValue passed to heal.locator() wins over it. Not saved. */
  expectValues?: Record<string, ExpectValue | null | undefined> | Map<string, ExpectValue | null | undefined> | null;
  /** Look up and heal heal.page.getByRole() and the other getBy* calls too, under a key written
   * as the call, such as 'get_by_role("button", name="Sign in")' (getByKey() builds one). A call
   * that matches nothing for timeoutMs is healed from the fingerprint mendwright recorded when it
   * last matched, never by a model, and a miss throws GetByNotFound, which is Playwright's
   * TimeoutError too. A call that matches several elements, or one hidden element, goes to
   * Playwright as written. MENDWRIGHT_HEAL_GET_BY, else off; null reads the variable too. */
  healGetBy?: boolean | null;
}

export interface LocatorOptions {
  role?: string;
  text?: string | string[];
  destructive?: boolean;
  timeoutMs?: number;
  pollMs?: number;
  optional?: boolean;
  intent?: string;
  /** What a read of the element must return when the element was found through a selector a
   * heal saved, in this run or an earlier one: 'money', 'number', 'integer', 'percent',
   * 'date', 'email' or 'url', a RegExp, or a function that takes the value and returns
   * something truthy when it is right. The reads are innerText(), textContent(), inputValue()
   * and getAttribute(). A value that passes confirms a heal made in this process; one that
   * fails quarantines the heal, and the read throws AttentionRequired. A read through any
   * other selector is never checked. Without it, the check of open({ expectValues }) for the
   * key applies. Not saved. */
  expectValue?: ExpectValue | null;
}

/** The options of heal.locatorAll(). */
export interface LocatorAllOptions {
  /** The items' role, as for heal.locator(): read from the first item when left out. */
  role?: string;
  /** How long a lookup that matches nothing waits for the list, when it waits (see
   * Healer.locatorAll): the healer's timeout when left out. */
  timeoutMs?: number;
  pollMs?: number;
  /** The list may be empty at this point: a lookup that matches nothing gives [] at once. */
  optional?: boolean;
  /** The list always has items at this point, such as the rows of a cart the test just filled.
   * Such a lookup records where the list sits and its heading too, and when none of its
   * selectors matches, it waits, then looks for the list on the page and saves what it finds as
   * a proposed list heal for a person to check, never used (see Healer.locatorAll). */
  nonempty?: boolean;
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

/** Whether heal.page's getBy* calls are looked up and healed (see Healer.getByLocator): the
 * healGetBy option when it is not null or undefined, else MENDWRIGHT_HEAL_GET_BY (1 or 0), else
 * off (the Python package's get_by_setting). MENDWRIGHT_DISABLE=1 leaves it as it is: the calls
 * are looked up, and nothing heals. Throws TypeError for a MENDWRIGHT_HEAL_GET_BY that is not a
 * flag. */
export function getBySetting(healGetBy?: boolean | null): boolean {
  if (healGetBy !== undefined && healGetBy !== null) return Boolean(healGetBy);
  return Boolean(envFlag('MENDWRIGHT_HEAL_GET_BY'));
}

/** What turns the fingerprint tier off, in the words a message uses, with the precedence of
 * fingerprintSetting(): MENDWRIGHT_DISABLE=1, fingerprint: false, or MENDWRIGHT_FINGERPRINT=0
 * (the Python package's _fingerprint_off_by). */
function fingerprintOffBy(fingerprint?: boolean): string {
  if (envFlag('MENDWRIGHT_DISABLE')) return 'MENDWRIGHT_DISABLE=1';
  if (fingerprint !== undefined) return 'fingerprint: false';
  return 'MENDWRIGHT_FINGERPRINT=0';
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

/** One call of a run function of heal.interrupt(), while it runs (Python's _Run): the
 * healer, the interruption's key, and done, which settles when the call ends. */
interface RunRecord {
  readonly healer: Healer;
  readonly key: string;
  readonly done: Promise<void>;
}

// The run functions of heal.interrupt() that the code running now is inside, outermost first
// (Python's _IN_RUN). An async context rather than a field, so that a step awaited next to
// the step that called a run function, in a Promise.all, is told apart from the function's
// own calls. Healer.runs has the same records, from every context.
const RUN_CONTEXT = new AsyncLocalStorage<readonly RunRecord[]>();

/** The step runSteps() runs now (Python's _RUNNING_STEP): the healer, the step's name, the
 * path of the page it showed on (see flow.pagePath), and whether its function still runs. */
interface RunningStep {
  readonly healer: Healer;
  readonly name: string;
  readonly path: string | null;
  running: boolean;
}

// The step whose function the code running now is part of, or null (see Healer.stepNow()). An
// async context rather than a field, so that a lookup awaited next to runSteps(), in a
// Promise.all, is not taken for part of the step. A run function of heal.interrupt() runs
// with null here: it is part of no step (see Healer.runFunction()).
const STEP_CONTEXT = new AsyncLocalStorage<RunningStep | null>();

// What run functions threw (Python marks such an error _mendwright_run): it is thrown as it
// is, and an AttentionRequired is never taken for a miss of the step the interruption was
// handled for. A WeakSet, since an error may be frozen.
const RAISED_BY_RUN = new WeakSet<object>();

function raisedByRun(error: unknown): boolean {
  return error !== null && (typeof error === 'object' || typeof error === 'function') && RAISED_BY_RUN.has(error);
}

/** Whether value is what await waits for: an object or function with a then method. */
function isThenable(value: unknown): value is PromiseLike<unknown> {
  return value !== null && (typeof value === 'object' || typeof value === 'function')
    && typeof (value as { then?: unknown }).then === 'function';
}

/** What a healer's locate() gives a HealLocator: the real Locator (null for a skipped step),
 * the heal it was found through, and the CSS of the shadow hosts it runs inside, if any. */
interface Located {
  native: Locator | null;
  healed: string | null;
  hosts: string[] | null;
}

/** Why an action or read of an element found inside the shadow root that hosts lead to did
 * not run (the Python package's facade.moved_message). */
function movedMessage(key: string, hosts: string[], verb: string): string {
  return "mendwright: '" + key + "' was found inside " + placeText(hosts) + ', but when it came to ' + verb
    + "() its selector no longer matched one element there and nothing outside it: the page changed after the "
    + "lookup. Playwright's CSS reaches into every open shadow root, so mendwright did not run " + verb + '(), '
    + 'which could have reached an element outside that shadow root. Look the element up again with heal.locator().';
}

/** What one step has handled (see flow.MAX_INTERRUPTIONS_PER_STEP): how many interruptions
 * and popups, the keys of the ones handled, in order ('<popup>' for a dismissed popup), and
 * gaveUp, the popup open({ dismiss: true }) could not dismiss in this step, so it is tried
 * once per step and named in one WARNING. */
interface Budget {
  used: number;
  handled: string[];
  gaveUp: string | null;
}

const newBudget = (): Budget => ({ used: 0, handled: [], gaveUp: null });

/** What a look at an element after a step on the popup over it says: 'gone' when the popup's
 * root was removed or hidden, or the element is on top again ('free'); 'lost' when the
 * element could not be looked at (it matches none or several now) or is hidden or outside the
 * window now, so mendwright cannot tell what the step did; 'there' when the popup still
 * covers it. */
function popupState(probe: Probe | null): 'gone' | 'lost' | 'there' {
  if (probe === null) return 'lost';
  if (probe.gone || (!probe.covered && probe.why === 'free')) return 'gone';
  return probe.covered ? 'there' : 'lost';
}

/** One step taken on a popup: how ('escape', 'close' or 'decline') and the button's label
 * (null for Escape). */
type PopupStep = [string, string | null];

/** What HealLocator adds to an action Healer.runStep() runs: started after the registered
 * interruptions were handled, place right before each try, failed when the action failed in
 * the browser (the heal canary). */
interface StepHooks {
  started?: () => void;
  place?: () => Promise<unknown>;
  failed?: () => void;
}

/** An action's argument as Playwright takes it: the real Locator of a HealLocator (resolved)
 * or of one of heal.page's guarded locators. */
async function nativeOf(value: unknown): Promise<unknown> {
  if (value instanceof HealLocator) return value.locatorNow();
  if (value && typeof value === 'object' && PROXIES.has(value)) return PROXIES.get(value)!.native;
  return value;
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
  expect: Expectation | null;
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
  // Lookups, and the interruptions steps handle, take turns (see turn()).
  private readonly mutex = new Mutex();
  // Popups are dismissed one at a time: two actions awaited together never click the same one.
  private readonly popupMutex = new Mutex();
  // The run functions of heal.interrupt() that run now, in any context (see awaitRuns()).
  private readonly runs: RunRecord[] = [];
  // The steps heal.step() registered, by name, in the order registered (see steps.ts).
  private readonly stepBook = new Map<string, Step>();
  private pageProxy: Page | Frame | null = null;
  /** @internal open({ dismiss: true }): dismiss popups nobody registered (see dismiss()). */
  dismissOn = false;
  /** @internal open({ expectValues }): key -> the check a read through a heal must pass. */
  expectValues: Map<string, Expectation> = new Map();
  /** @internal open({ healGetBy: true }): heal.page's getBy* calls are looked up and healed (see
   * getByLocator()). */
  healGetBy = false;
  // The getBy* calls of heal.page that are never keys, logged once each (see noteGetBy()).
  private readonly getByNoted = new Set<string>();

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
   * heal.locator(selector, selector). With open({ healGetBy: true }), getByRole() and the other
   * getBy* calls on it are looked up and healed too (see getByLocator()). Everything else is the
   * real page's, except that with open({ dismiss: true }) the locators it does not heal (getBy*
   * without healGetBy, chained ones, ...) come back guarded: the real Locator, with the
   * interruption and popup checks before its actions. */
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
   * it.
   *
   * run, in place of action and args, is an async function that answers an interruption that
   * needs a form, such as a session-expired sign-in: it gets heal.page, so its own selectors
   * heal as usual, and selector only shows that the interruption is there. mendwright awaits
   * it, then waits for the element to go away as after an action. Its own calls never handle
   * its own interruption, so it is never called from inside itself, but they handle the
   * others, and no popup is dismissed while it runs. What it throws is thrown as it is.
   * Throws TypeError or InvalidSelector at once for arguments that can never work: run with
   * action or args, a run that is not a function, and interrupt(key, null, { run }). */
  interrupt(key: string, selector: string | string[] | null, options: InterruptOptions = {}): void {
    if (selector === null) {
      if (options.run !== undefined && options.run !== null) throw new TypeError(runRemovesMessage(key));
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
   * step that may not be there. expectValue says what a read through a heal must return.
   * Throws TypeError or InvalidSelector at once for arguments that can never work. */
  locator(key: string, selector?: string | string[] | null, options: LocatorOptions = {}): HealLocator {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('mendwright: heal.locator() needs a key, a non-empty name such as "email", got ' + pyRepr(key) + '.');
    }
    if (isKey(key)) throw new TypeError(reservedKeyMessage(key, 'heal.locator()'));
    const intent = options.intent;
    if (intent !== undefined && (typeof intent !== 'string' || !intent.trim())) {
      throw new TypeError('mendwright: intent for ' + pyRepr(key) + ' takes a description of the element in words, got '
        + pyRepr(intent) + ". For example, intent: 'the terms checkbox in the signup form'.");
    }
    const roles = roleChoices(options.role);
    const code = codeSelectors(key, selector);
    this.checkIntent(key, code, roles, intent);
    const expect = expectation(options.expectValue, key) ?? this.expectValues.get(key) ?? null;
    return new HealLocator(this, key, {
      roles, code, text: options.text, destructive: options.destructive,
      timeoutMs: options.timeoutMs ?? this.timeoutMs, pollMs: options.pollMs ?? 200,
      optional: Boolean(options.optional), intent, expect,
    });
  }

  /** True when one of key's stored selectors matches a single element now. No waiting and
   * no healing. For the key of a getBy* call (getByKey()), the call itself first: true when it
   * matches one visible element, false when it matches several or one hidden one, and its saved
   * heals only while it matches nothing (see resolver.presentGetBy). A call written in another
   * spelling, such as "get_by_role('button', name='Pay')", is read as the call's key. */
  present(key: string): Promise<boolean> {
    if (isKey(key)) return presentGetBy(this.store, this.driver, canonicalKey(key) as string);
    return presentNow(this.store, this.driver, key);
  }

  // --- heal.page.getBy*(), with open({ healGetBy: true }) (see getby.ts) ---------------------

  /** @internal The HealLocator an action or a waiting read of a heal.page.getBy*() call runs on
   * (the Python package's Healer._get_by_locator): call is its GetByCall, target the real
   * Locator, and the key is the call (getby.canonical), such as
   * 'get_by_role("button", name="Sign in")'.
   *
   * A. One look first, before anything is written: a call the browser rejects (level on a
   *    button), or that matches several elements or one hidden element (unless the key's role is
   *    checkbox), is not broken: it goes to Playwright as written (getByThrough), with nothing
   *    saved and no saved heal used. A call that matches nothing has the registered
   *    interruptions handled now, before the wait: a modal cookie banner can hide the rest of the
   *    page from the accessibility tree.
   * B. The lookup (resolver.resolveGetBy): the call itself first, as the key's code selector, and
   *    a saved heal only while the call matches nothing. A miss handles the registered
   *    interruptions and tries again. A call that starts to match several elements or a hidden
   *    one during the wait goes to Playwright at once.
   * C. After a miss: a call that matches only inside an iframe is not followed there, and no heal
   *    of it is either (getByFrames); else the coordinator heals it from its fingerprint only
   *    (HealCoordinator.healGetBy). What it cannot find or heal is thrown as a GetByNotFound,
   *    which is also Playwright's TimeoutError, so a catch of that around an optional step still
   *    works. InterruptionLoop, what a run function threw and the refusal of a heal on a key
   *    marked destructive are thrown as they are.
   * D. After a match, as locate() does: the role, the page and the fingerprint are saved. An
   *    action on the element the call itself found reports nothing to the heal canary; one on a
   *    heal's element does. */
  async getByLocator(call: GetByCall, target: Locator, timeoutMs?: number, pollMs = 200): Promise<HealLocator> {
    const key = canonical(call);
    const timeout = timeoutMs ?? this.timeoutMs;
    const plan: Plan = {
      roles: null, code: [key], text: undefined, destructive: undefined, timeoutMs: timeout, pollMs, optional: false,
      intent: undefined, expect: this.expectValues.get(key) ?? null,
    };
    const located = await this.turn(async (): Promise<Located | string> => {
      const budget = newBudget();
      const state = await this.getByState(key);
      if (typeof state === 'string') return state;
      const early = state[0] === 0 && this.others(key).length > 0
        && (await this.handleInterruptions(key, budget, timeout, pollMs));
      // The page changed when an interruption was handled: look at the call again.
      let first: [number, boolean | null] | null = early ? null : state;
      this.prime(key, plan);
      const attempt = (wait: number = timeout) => {
        const given = first;
        first = null;
        return resolveGetBy(this.store, this.driver, key, { timeoutMs: wait, pollMs, first: given });
      };
      let native: Locator | typeof THROUGH;
      try {
        try {
          native = await this.resolveStep(key, () => attempt(), plan, budget);
        } catch (error) {
          if (error instanceof InterruptionLoop || !(error instanceof AttentionRequired) || raisedByRun(error)) {
            throw error;
          }
          await this.getByFrames(key, error);
          native = await this.heal(key, error, attempt);
        }
      } catch (error) {
        // The page is blocked, or a run function threw it: as it is, not a miss of this step.
        if (error instanceof InterruptionLoop || !(error instanceof AttentionRequired) || raisedByRun(error)) {
          throw error;
        }
        throw new GetByNotFound(error.message, { cause: error });
      }
      if (native === THROUGH) return STARTED_TO_MATCH;
      this.checkHealedTarget(key);
      await this.learn(key, null);
      this.rememberPage(key);
      await this.recordFingerprint(key);
      const winner = this.store.lastSuccess(key);
      const healed = winner && winner.source === 'heal' && typeof winner.xpath === 'string' ? winner.xpath : null;
      const where = this.winnerTarget(key);
      return { native, healed, hosts: where && where.hosts.length ? where.hosts : null };
    });
    if (typeof located === 'string') return this.getByThrough(key, target, plan, located);
    return HealLocator.found(this, key, plan, located, located.healed !== null);
  }

  /** Step A of getByLocator(): [count, visible] of the call key on the page now, or why it goes
   * to Playwright as written. One round trip, two when it matches one element. */
  private async getByState(key: string): Promise<[number, boolean | null] | string> {
    const visible = requireVisible(this.store, key);
    let count: number;
    let shown: boolean | null = null;
    try {
      count = await this.driver.count(key);
      if (count === 1 && visible) shown = await this.driver.isVisible(key);
    } catch (error) {
      if (error instanceof InvalidSelector) return REJECTED;
      throw error;
    }
    if (count >= 2) return 'matches ' + count + ' elements';
    if (count === 1 && visible && !shown) return HIDDEN;
    return [count, shown];
  }

  /** The HealLocator of a getBy* call that goes to Playwright as written, and why, in a DEBUG
   * line: its actions handle the registered interruptions and, with dismiss: true, a popup over
   * the element first, and Playwright throws its own error (a strict mode violation, its wait
   * and timeout, a selector error). Nothing is saved, no saved heal is used, and it reports
   * nothing to the heal canary. */
  private getByThrough(key: string, target: Locator, plan: Plan, why: string): HealLocator {
    this.coordinator.emit('debug', throughMessage(key, why));
    return HealLocator.element(this, key, target, { ...plan, expect: null }, { due: true }, null);
  }

  /** Step C1 of getByLocator(): when the call itself matches inside an iframe, throw the
   * AttentionRequired that says mendwright does not follow a getBy* call there (the Python
   * package's _get_by_frames). Only the call is looked for, not its heals: nothing of a call key
   * is followed into an iframe. */
  private async getByFrames(key: string, missed: AttentionRequired): Promise<void> {
    let frames: FrameMatch[];
    try {
      frames = await this.driver.frameMatches(key);
    } catch {
      return;
    }
    if (frames.length) this.frameStop(key, missed, key, frames, NOT_FOLLOWED);
  }

  /** @internal Log, once per healer and call, that heal.page's getBy* call step is never a key
   * (see getby.callOf) and goes to Playwright as written (the Python package's _note_get_by). */
  noteGetBy(step: string, why: string): void {
    if (this.getByNoted.has(step)) return;
    this.getByNoted.add(step);
    this.coordinator.emit('info', notHealedMessage(step, why));
  }

  // --- lists: locatorAll() (see lists.ts) ---------------------------------------------------

  /** The elements of a list, such as the cards of a product grid: a HealLocator for each element
   * key's selectors match, in page order (the Python package's heal.locator_all()). mendwright
   * never gives a list that the key's own selectors don't match.
   *
   * selector, role (the items' role, read from the first item when it is left out), timeoutMs
   * and pollMs mean what they mean for locator(). The first of key's selectors that matches one
   * element or more gives the list, as written: the selector in your code first, then those
   * saved for the key. A heal saved under the same key is skipped: a heal of one element, such
   * as one heal.page made for page.click('.card'), is no heal of the list, and no list heal is
   * ever used. Each time one matches two or more, mendwright records what the items look like
   * (a list fingerprint, once per healer and selector), which says the list had items. When
   * none matches:
   *
   * - a list mendwright has not seen with two or more items, or one with optional: true, gives
   *   [] at once, as Locator.all() does, and so does any list while the fingerprint tier is off
   *   (fingerprint: false, MENDWRIGHT_FINGERPRINT=0 or MENDWRIGHT_DISABLE=1), which records no
   *   list fingerprint;
   * - any other waits up to timeoutMs, handling registered interruptions, and throws
   *   AttentionRequired, which says that either the list is empty at this point or its selector
   *   broke. mendwright can't tell the two apart: the page after a cart's items all moved to
   *   Saved for later can be the page after a redesign but for a class name. So it looks for no
   *   other list, asks no model and saves nothing, and a selector that broke is fixed in your
   *   code.
   *
   * nonempty: true says the list always has items at this point, such as the rows of a cart the
   * test just filled. Such a lookup records where the list sits and its heading with its list
   * fingerprint, and so does every later lookup of the key. When none of its selectors matches,
   * it always waits timeoutMs, then looks for the list on the page (see lists.ts) and never uses
   * what it finds: it saves it in the store as a proposed list heal and throws AttentionRequired
   * with the new selector, where the list was and where the group found is, and the commands to
   * check it (mendwright lists), accept it (mendwright lists accept KEY ID) and write it into
   * your code (mendwright fix --apply), all of the Python package. Only then does the list come
   * from it, as your selector. When it finds nothing it can propose, AttentionRequired says why.
   *
   * The elements' actions handle interruptions and popups as locator()'s do, and confirm or
   * quarantine nothing, and neither does their confirm(). The first read of any of them handles
   * the registered interruptions once for the whole list, and their reads are never checked
   * against expectValues. Throws TypeError or InvalidSelector at once, before anything touches
   * the page, for arguments that can never work (nonempty other than true or false, or
   * nonempty: true with optional: true). */
  locatorAll(key: string, selector?: string | string[] | null, options: LocatorAllOptions = {}): Promise<HealLocator[]> {
    if (typeof key !== 'string' || !key) {
      throw new TypeError('mendwright: heal.locatorAll() needs a key, a non-empty name such as "cards", got '
        + pyRepr(key) + '.');
    }
    if (isKey(key)) throw new TypeError(reservedKeyMessage(key, 'heal.locatorAll()'));
    if ((options as LocatorOptions).expectValue !== undefined) {
      throw new TypeError('mendwright: heal.locatorAll() takes no expectValue: no list is found through a heal, so the '
        + "reads of a list's elements are never checked.");
    }
    const roles = roleChoices(options.role);
    const code = codeSelectors(key, selector);
    const wrong = nonemptyArgsError(key, options.nonempty, options.optional);
    if (wrong) throw wrong;
    const plan: Plan = {
      roles, code, text: undefined, destructive: undefined, timeoutMs: options.timeoutMs ?? this.timeoutMs,
      pollMs: options.pollMs ?? 200, optional: Boolean(options.optional), intent: undefined, expect: null,
    };
    return this.lookUpList(key, plan, options.nonempty === true);
  }

  /** locatorAll() once its arguments were checked: the lookup under the healer's mutex, then a
   * HealLocator for each element. */
  private async lookUpList(key: string, plan: Plan, nonempty: boolean): Promise<HealLocator[]> {
    const natives = await this.turn(() => this.list(key, plan, nonempty));
    const reads = { due: true };   // one check of the interruptions for the reads of the whole list
    const target = natives.length ? this.winnerTarget(key) : null;
    const hosts = target && target.hosts.length ? target.hosts : null;
    return natives.map((native) => HealLocator.element(this, key, native, plan, reads, hosts));
  }

  /** The Locators of list key (see locatorAll), with its code primed first. What a lookup that
   * matches at once saves (the code's selectors, the first success, the page, the list
   * fingerprint) costs one write of the store (the Python package's Healer._list). */
  private async list(key: string, plan: Plan, nonempty: boolean): Promise<Locator[]> {
    const matched = await this.store.batchAsync(async () => {
      this.prime(key, plan);
      const found = await this.listLook(key, this.listRunnable(key));
      return found === null ? null : this.listFound(key, plan.roles, found, nonempty);
    });
    if (matched !== null) return matched;
    if (!nonempty && (plan.optional || !this.listWaits(key))) return [];
    let found: [string, number];
    try {
      found = await this.resolveStep(key, () => this.listAttempt(key, plan.timeoutMs, plan.pollMs), plan);
    } catch (error) {
      // The page is blocked (InterruptionLoop), or a run function threw it: as it is.
      if (error instanceof InterruptionLoop || !(error instanceof AttentionRequired) || raisedByRun(error)) throw error;
      if (nonempty) return this.listNonemptyMissing(key, plan.roles, error);
      throw this.listMissing(key, error);
    }
    return this.store.batchAsync(() => this.listFound(key, plan.roles, found, nonempty));
  }

  /** True when a list lookup of key that matches nothing waits and throws: key has a list
   * fingerprint and the fingerprint tier is on. With it off, such a lookup gives [] at once, as
   * the real page does. */
  private listWaits(key: string): boolean {
    return this.coordinator.fingerprint && validList(this.store.metaOf(key).listFingerprint);
  }

  /** key's candidates a list lookup tries: those on the page (a list is looked for neither
   * inside an iframe nor through a row saved inside a shadow root) that can run, without heals.
   * A list key can also be the key of locator() or of a heal.page call, and a heal of the first
   * card is no heal of the cards. A list heal is not used either: it can be another list that
   * holds the items of an emptied one. Throws the first InvalidSelector when none can run. */
  private listRunnable(key: string): Candidate[] {
    const candidates = this.store.candidates(key).filter((c) => !c.frameXPath && !hostsOf(c).length
      && c.source !== 'heal');
    if (!candidates.length) throw new AttentionRequired('No selector candidates for: ' + key);
    const [runnable, errors] = splitRunnable(key, candidates);
    if (!runnable.length) throw errors[0];
    return runnable;
  }

  /** [selector, count] for the first of runnable that matches one element or more now,
   * recorded as the key's success, or null. */
  private async listLook(key: string, runnable: Candidate[]): Promise<[string, number] | null> {
    for (const candidate of runnable) {
      let count: number;
      try {
        count = await this.driver.count(candidate.xpath);
      } catch (error) {
        if (error instanceof InvalidSelector) throw error;
        count = 0;
      }
      if (count) {
        this.store.recordSuccess(key, candidate.xpath);
        return [candidate.xpath, count];
      }
    }
    return null;
  }

  /** listLook() until one matches or waitMs runs out; AttentionRequired then. */
  private async listAttempt(key: string, waitMs: number, pollMs: number): Promise<[string, number]> {
    const runnable = this.listRunnable(key);
    const deadline = Date.now() + waitMs;
    for (;;) {
      const found = await this.listLook(key, runnable);
      if (found !== null) return found;
      if (Date.now() >= deadline) throw new AttentionRequired(noMatchMessage(key, runnable, []));
      await sleep(pollMs);
    }
  }

  /** The Locators of what found ([selector, count]) matched for list key, with the page saved
   * and, when it should be read (see listTarget), the list fingerprint: with where the list sits
   * when place (nonempty: true) or when the stored one already holds it. The key's list heals
   * that selector was replaced by are retired (see retireLists). */
  private async listFound(key: string, roles: string[] | null, found: [string, number], place = false):
      Promise<Locator[]> {
    const [selector, count] = found;
    this.rememberPage(key);
    place = place || this.listPlaced(key);
    if (this.listTarget(key, selector, count, place)) {
      this.keepList(key, selector, roles, await this.readList(selector, place), place);
    }
    this.retireLists(key, selector, count);
    return this.driver.handles(selector);
  }

  /** End key's list heals that selector, which just matched count elements, replaced
   * (SelectorStore.retireListHeals): a proposed one is no longer proposed, an accepted one is
   * quarantined, with a WARNING and a heal.quarantine journal event. A list heal names the
   * selectors it replaced, so another selector of the key, such as another page's, ends none. */
  private retireLists(key: string, selector: string, count: number): void {
    const rows = this.listRows(key);
    if (!rows.length) return;
    let changes: [string, string, string][];
    try {
      changes = this.store.retireListHeals(key, selector);
    } catch {
      return;
    }
    if (changes.length) this.coordinator.retiredLists(key, selector, count, changes, rows);
  }

  /** True when key's stored list fingerprint holds where the list sits. */
  private listPlaced(key: string): boolean {
    return listPlaced(this.store.metaOf(key).listFingerprint);
  }

  /** The list fingerprint of what selector matches, with where it sits when place; null on
   * any error, or for a result that is not one. */
  private async readList(selector: string, place: boolean): Promise<ListFingerprint | null> {
    try {
      const lfp = await this.driver.listFingerprint(selector, place);
      return validList(lfp) ? lfp : null;
    } catch {
      return null;
    }
  }

  /** True when the list fingerprint of what selector matched for key should be read: the
   * fingerprint tier is on, it matched two or more, this healer has not read it for that
   * selector yet (with where it sits, when place), and the key is not marked destructive. */
  private listTarget(key: string, selector: string, count: number, place: boolean): boolean {
    if (!this.coordinator.fingerprint || count < 2) return false;
    if (this.fingerprinted.has(JSON.stringify(['list', key, selector, place]))) return false;
    return !this.store.metaOf(key).destructive;
  }

  /** Save lfp as key's list fingerprint, and the items' role when role did not say. When place
   * was asked for and lfp has none, a stored list fingerprint that holds one is left as it is: a
   * failed read never removes a place. */
  private keepList(key: string, selector: string, roles: string[] | null, lfp: ListFingerprint | null,
                   place: boolean): void {
    this.fingerprinted.add(JSON.stringify(['list', key, selector, place]));
    if (place && !listPlaced(lfp) && this.listPlaced(key)) return;
    if (lfp === null) return;
    try {
      this.store.setMeta(key, { listFingerprint: lfp });
      if ((!roles || !roles.length) && this.store.roleIsGuess(key)) this.store.setMeta(key, { role: itemRole(lfp) });
    } catch {
      // a list fingerprint that cannot be saved is not recorded
    }
  }

  /** What a lookup of list key with nonempty: true does when none of its selectors matched
   * (missed): the Locators of a list that came while mendwright looked for it, or
   * AttentionRequired with the miss and why: the fingerprint tier is off, a list heal you
   * accepted waits for mendwright fix, or what HealCoordinator.proposeList() proposed or
   * refused. */
  private async listNonemptyMissing(key: string, roles: string[] | null, missed: AttentionRequired):
      Promise<Locator[]> {
    const blocked = this.nonemptyBlocked(key);
    if (blocked) throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + blocked);
    this.reloadLists(key);
    for (const row of this.listRows(key)) {
      if (row.status !== 'accepted') continue;
      const count = await this.countLive(row.xpath);
      if (count) throw this.waitsForFix(key, missed, row, count);
    }
    const outcome = await this.coordinator.proposeList(key, this.store.metaOf(key), this.listRunnable(key),
      this.flowContext(key));
    if (outcome.kind === 'came') {
      const natives = await this.store.batchAsync(async () => {
        const found = await this.listLook(key, this.listRunnable(key));
        if (found === null) return null;
        this.coordinator.came(key, found[0]);
        return this.listFound(key, roles, found, true);
      });
      if (natives !== null) return natives;
    }
    throw this.listOutcomeError(key, missed, outcome);
  }

  /** The refusal of a list looked up with nonempty: true while the fingerprint tier is off,
   * naming what turned it off, else ''. */
  private nonemptyBlocked(key: string): string {
    const coordinator = this.coordinator;
    if (coordinator.fingerprint) return '';
    return coordinator.declineNonempty(key, tierOffText(coordinator.fingerprintOffBy
      || "the HealCoordinator's fingerprint: false"));
  }

  /** Take in what the Python package's mendwright lists accept or reject did to key's list heals
   * since the store was read (SelectorStore.reloadListHeals), so a healer that lives across an
   * accept or a reject doesn't propose that list heal again. */
  private reloadLists(key: string): void {
    try {
      this.store.reloadListHeals(key);
    } catch {
      // read again on the next lookup
    }
  }

  /** key's list heals that wait (SelectorStore.listHeals). */
  private listRows(key: string): Candidate[] {
    try {
      return this.store.listHeals(key);
    } catch {
      return [];
    }
  }

  /** How many elements selector matches on the page now; 0 on any error. */
  private async countLive(selector: unknown): Promise<number> {
    try {
      return typeof selector === 'string' ? await this.driver.count(selector) : 0;
    } catch {
      return 0;
    }
  }

  /** The AttentionRequired for list key, when a list heal you accepted for it (row) matches
   * count elements and waits for mendwright fix. */
  private waitsForFix(key: string, missed: AttentionRequired, row: Candidate, count: number): AttentionRequired {
    const reason = this.coordinator.waitsForFix(key, this.store.metaOf(key), row, count);
    return new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
  }

  /** The AttentionRequired for what proposeList() did (see ListOutcome), but a list that came
   * and is still there: the miss and the reason. */
  private listOutcomeError(key: string, missed: AttentionRequired, outcome: ListOutcome): AttentionRequired {
    let reason: string;
    if (outcome.kind === 'came') {
      reason = this.coordinator.cameAndWent(key, this.store.metaOf(key), outcome.xpath, outcome.reason);
    } else if (outcome.kind === 'accepted') {
      reason = this.coordinator.waitsForFix(key, this.store.metaOf(key), outcome.row ?? { xpath: outcome.xpath },
        outcome.count);
    } else {
      reason = outcome.reason;
    }
    return new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
  }

  /** The AttentionRequired for list key, whose lookup missed when none of its selectors
   * matched: the miss and why the list is not healed (HealCoordinator.declineList), which the
   * log gets too. */
  private listMissing(key: string, missed: AttentionRequired): AttentionRequired {
    const reason = this.coordinator.declineList(key, this.store.metaOf(key));
    return new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
  }

  // --- steps in any order: step() and runSteps() (see steps.ts) ------------------------------

  /** Register a step of a flow whose steps the site may show in another order, on one page, or
   * not at all, for runSteps(), and return fn. when is what shows the step is on the page, its
   * landmark: one selector or a list, and any visible match counts. It is never healed, so pick
   * one that stays, such as a heading's text. url is a pattern for the page's path, such as
   * '/checkout/payment' or '*\/payment*', normalized as the page guard normalizes paths:
   * instead of when, or as one more condition with it. last: true marks the step that ends the
   * flow, such as placing the order; a flow has at most one. optional: true marks one that may
   * not show at all, such as an upsell.
   *
   * fn gets heal.page, so its selectors heal as usual, and returns a promise, as an async
   * function does. Registering a name again replaces its step, which keeps its place in the
   * order. Steps stay on the healer for every later runSteps() call: for another flow, open a
   * new healer. Throws TypeError or InvalidSelector at once for arguments that can never work. */
  step<F extends StepFunction>(name: string, options: StepOptions, fn: F): F {
    registerStep(this.stepBook, makeStep(name, options, fn));
    return fn;
  }

  /** Run the steps registered with step() in the order the site shows them, each once, until
   * the step with last: true ran (or, with no last step, every step that is not optional ran),
   * and resolve to their names in the order they ran.
   *
   * Each time, it waits up to timeoutMs (default: the healer's) for a step that has not run to
   * show, then awaits its function. Steps that show at the same time run in the order they were
   * registered, and the last step only when no other shows. Registered interruptions the page
   * shows meanwhile are handled, up to 3 in each wait.
   *
   * While a step runs, a key found during that step before heals on the page the step showed
   * on, whatever its address, where the page guard would decline a page the key was never
   * found on. Once the step's function goes to another page, such as with its own submit, and
   * outside runSteps(), the guard is as before.
   *
   * Rejects with AttentionRequired when a step that ran is on the page again with no new step
   * for the whole timeout (its submit probably failed, or the site added a step that shows part
   * of it: it is never run twice), when no step shows (the message names the page and every
   * landmark), and after 3 times as many step runs as there are steps; with TypeError when a
   * step's function returns something that is not a promise. What a step's function throws is
   * thrown as it is, after the flow.step journal event records it. */
  async runSteps(options: RunStepsOptions = {}): Promise<string[]> {
    const [timeoutMs, pollMs] = runArgs(this.stepBook, options, this.timeoutMs);
    const flow = new StepRun(this.stepBook, this.coordinator, timeoutMs);
    const page = this.page;
    while (!flow.done()) {
      flow.checkCap();
      const [step, path] = await this.nextStep(flow, timeoutMs, pollMs);
      flow.starting(step);
      const started = performance.now();
      const running: RunningStep = { healer: this, name: step.name, path, running: true };
      const fn = step.fn;
      try {
        await STEP_CONTEXT.run(running, async () => {
          const result = fn(page);
          if (!isThenable(result)) throw new TypeError(plainMessage(step.name, result));
          await result;
        });
      } catch (error) {
        running.running = false;
        flow.record(step, path, (performance.now() - started) / 1000, true, error);
        throw error;
      }
      running.running = false;
      flow.record(step, path, (performance.now() - started) / 1000);
    }
    return flow.finish();
  }

  /** Wait, up to timeoutMs, for a step that has not run to show, handling the registered
   * interruptions the page shows meanwhile: [the step, the path of its page]. The page's
   * address is read again after its landmarks, and they are looked at again when it changed
   * meanwhile, so the step is not given the page it came from. A landmark that matches more
   * elements than mendwright looks at is named in one WARNING per page. */
  private async nextStep(flow: StepRun, timeoutMs: number, pollMs: number): Promise<[Step, string | null]> {
    const deadline = performance.now() + timeoutMs;
    const budget = newBudget();
    const failed = new Set<string>();   // the interruptions whose action failed in this wait
    for (;;) {
      const url = this.driver.url();
      const several = (name: string, selector: string, count: number, looked: number) => {
        this.severalLandmark(name, selector, count, looked, url);
      };
      const shown: Step[] = [];
      for (const step of flow.waiting()) {
        if (await shows(this.driver, step, url, several)) shown.push(step);
      }
      if (shown.length) {
        if (pagePath(this.driver.url()) === pagePath(url) || performance.now() >= deadline) {
          return [StepRun.pick(shown), pagePath(url)];
        }
        continue;   // the page changed while its landmarks were looked at: look again
      }
      if (await this.interruptWaiting(budget, timeoutMs, pollMs, failed)) continue;
      if (performance.now() >= deadline) {
        const again: Step[] = [];
        for (const step of flow.ranSteps()) {
          if (await shows(this.driver, step, url)) again.push(step);
        }
        throw flow.stuck(again, pagePath(url));
      }
      await sleep(Math.max(pollMs, 20));
    }
  }

  /** While runSteps() waits: handle the registered interruptions the page shows, one key at a
   * time, under the healer's mutex as before an action, leaving out the keys in failed. A key
   * whose action failed (a WARNING said so and nothing was handled, though the budget went
   * down) is added to failed, so it is not tried again in this wait, and the others still are.
   * True when one was handled. */
  private async interruptWaiting(budget: Budget, timeoutMs: number, pollMs: number, failed: Set<string>): Promise<boolean> {
    if (this.others(WAITING_STEP).every((item) => failed.has(item.key))) return false;
    return this.turn(async () => {
      let handled = false;
      for (const item of this.others(WAITING_STEP)) {
        if (failed.has(item.key)) continue;
        const used = budget.used;
        if (await this.handleInterruptions(WAITING_STEP, budget, timeoutMs, pollMs, item.key)) handled = true;
        else if (budget.used > used) failed.add(item.key);
      }
      return handled;
    });
  }

  /** The WARNING for a landmark of step name that matches count elements of which the first
   * looked were looked at, none visible: once per step and page (Python's _several). */
  private severalLandmark(name: string, selector: string, count: number, looked: number, url: string): void {
    const seen = JSON.stringify(['landmark', name, pagePath(url)]);
    if (this.severalSeen.has(seen)) return;
    this.severalSeen.add(seen);
    this.coordinator.emit('warning', severalMessage(name, selector, count, looked, 'landmark'));
  }

  /** The step runSteps() runs now on this healer, when the code running now is part of it, or
   * null. Only in the async context its function runs in, and while it runs: a callback the
   * function left behind, such as a timer, is not part of it once it ended, nor is a lookup
   * awaited next to runSteps(), nor a run function of heal.interrupt(). */
  private stepNow(): RunningStep | null {
    const running = STEP_CONTEXT.getStore();
    return running && running.healer === this && running.running ? running : null;
  }

  /** While runSteps() runs a step, and key was found on the page that step showed on (page),
   * save the step's name with key (SelectorStore.setLastStep), so a later run lets key heal
   * wherever that step shows. Not for a key found after the step's function went to another
   * page: that page may be the next step's. */
  private rememberStep(key: string, page: string | null): void {
    const running = this.stepNow();
    if (running && page && page === running.path && typeof this.store.setLastStep === 'function') {
      this.store.setLastStep(key, running.name);
    }
  }

  /** The pages the page guard compares for key: the ones saved with it, and, while runSteps()
   * runs a step that key was found during before, this page (here) too when it is the page
   * that step showed on (its landmark, its url) when it started. So a site that moved the step
   * to another page or another place in the flow still heals the key there, and one INFO line
   * says so when the page is new to the key. Once the step's function went to another page,
   * such as with its own submit, that page may be the next step's, and nothing changes; nor
   * outside runSteps(), or for a key never found during the step that runs. */
  private stepPages(key: string, meta: KeyMeta, here: string | null): string[] {
    const pages = [...meta.pages];
    const running = this.stepNow();
    if (!running || !here || here !== running.path || !meta.steps.includes(running.name)) return pages;
    const last = meta.lastPage;
    if (!pages.includes(here) && here !== last && (pages.length || last) && !this.coordinator.healAcrossPages) {
      this.coordinator.emit('info', scopeMessage(key, running.name, here, last));
    }
    return [...pages, here];
  }

  // --- run functions of heal.interrupt() --------------------------------------------------

  /** The records of this healer's run functions that the code running now is inside, and
   * that still run: a callback a run function left behind, such as a timer, runs in its
   * context after it ended, and is not inside it then. */
  private ownRuns(): RunRecord[] {
    const mine = RUN_CONTEXT.getStore() ?? [];
    return mine.filter((run) => run.healer === this && this.runs.includes(run));
  }

  /** The run functions of this healer that run now in a context other than the code
   * running now. */
  private runsElsewhere(): RunRecord[] {
    const mine = RUN_CONTEXT.getStore() ?? [];
    return this.runs.filter((run) => !mine.includes(run));
  }

  /** fn under the healer's mutex, so that steps take turns, or at once inside a run function
   * of this healer (Python's AsyncHealer._turn): the step that called the function holds the
   * mutex while it waits for it, so the function's own calls would wait for that step
   * forever. A step in another context still takes the mutex, and waits. */
  private turn<T>(fn: () => Promise<T>): Promise<T> {
    return this.ownRuns().length ? fn() : this.mutex.run(fn);
  }

  /** The registered interruptions other than before itself, and other than those whose run
   * function the code running now is inside (Python's _others): the actions of a run
   * function do not handle its own interruption, which it is filling in, but they do handle
   * another one, such as a cookie banner over its form. */
  private others(before: string): Interruption[] {
    const mine = new Set(this.ownRuns().map((run) => run.key));
    return [...this.interruptions.values()].filter((item) => item.key !== before && !mine.has(item.key));
  }

  /** Wait for the run functions of this healer that run in another context (Python's
   * _await_runs): a step must not act on the page while one fills in its form, nor call it a
   * second time. It waits with no limit, as the step that called the function does. */
  private async awaitRuns(): Promise<void> {
    for (let elsewhere = this.runsElsewhere(); elsewhere.length; elsewhere = this.runsElsewhere()) {
      await Promise.all(elsewhere.map((run) => run.done));
    }
  }

  /** Call interruption item's run function with heal.page and wait for the promise it
   * returns (Python's _run_function). While it runs, its own calls run in an async context
   * marked as inside it (RUN_CONTEXT, and this.runs for every context): they do not handle
   * item again, dismiss no popup and skip the healer's mutex (see turn()), and the other
   * registered interruptions are still handled before their actions. It is part of no step
   * runSteps() runs (STEP_CONTEXT is null in it): it may run during any step, so the keys it
   * finds are not saved with the step, and the page guard is not widened for them. A step in
   * another context waits for it (see awaitRuns()). What it throws, and the TypeError for a
   * function that returned something that is not a promise, gets one WARNING and a
   * flow.interrupt event with outcome 'failed', and is thrown as it is, marked
   * (RAISED_BY_RUN): the step it was handled for does not take an AttentionRequired from it
   * for a miss of its own. */
  private async runFunction(item: Interruption, target: string, before: string, fn: string): Promise<void> {
    const run = item.run as RunFunction;
    let finish!: () => void;
    const record: RunRecord = { healer: this, key: item.key, done: new Promise<void>((resolve) => (finish = resolve)) };
    this.runs.push(record);
    try {
      await RUN_CONTEXT.run([...(RUN_CONTEXT.getStore() ?? []), record], () => STEP_CONTEXT.run(null, async () => {
        const result = run(this.page);
        if (!isThenable(result)) throw new TypeError(runPlainMessage(item.key, fn, result));
        await result;
      }));
    } catch (error) {
      try {
        this.coordinator.emit('warning', runFailedMessage(item.key, before, fn, error));
        this.coordinator.journal.event('flow.interrupt', {
          key: item.key, before, action: item.action, selector: target, function: fn, outcome: 'failed',
          error: errorType(error),
        });
      } catch {
        // an error whose name or message cannot be read is still thrown as it is
      }
      if (error !== null && (typeof error === 'object' || typeof error === 'function')) RAISED_BY_RUN.add(error);
      throw error;
    } finally {
      this.runs.splice(this.runs.indexOf(record), 1);
      finish();
    }
  }

  // --- resolution --------------------------------------------------------------------------

  /** @internal */
  locate(key: string, plan: Plan): Promise<Located> {
    return this.turn(async () => {
      this.prime(key, plan);
      const firstWait = this.interruptions.has(key) && this.handled.has(key) ? 0 : plan.timeoutMs;
      const attempt = (wait: number = plan.timeoutMs) => resolve(this.store, this.driver, key,
        { timeoutMs: wait, pollMs: plan.pollMs });
      let native: Locator;
      try {
        native = await this.resolveStep(key, () => attempt(firstWait), plan);
      } catch (error) {
        // What a run function threw is its own, not a miss of this step: nothing is healed or skipped.
        if (error instanceof InterruptionLoop || !(error instanceof AttentionRequired) || raisedByRun(error)) {
          throw error;
        }
        const skip = this.interruptions.has(key) ? (this.handled.has(key) ? 'interruption handled' : 'interruption absent')
          : plan.optional ? 'optional' : null;
        if (skip) {
          this.coordinator.emit('info', skipMessage(key, skip, this.handled.get(key)));
          this.coordinator.journal.event('flow.skip', { key, reason: skip });
          return { native: null, healed: null, hosts: null };
        }
        // Its own selector may match inside an iframe or an open shadow root now; only then is
        // anything healed.
        const followed = (await this.followIntoFrame(key, error, attempt))
          ?? (await this.followIntoShadow(key, error, attempt));
        native = followed ?? (await this.heal(key, error, attempt));
      }
      this.checkHealedTarget(key);
      await this.learn(key, plan.roles);
      this.rememberPage(key);
      await this.recordFingerprint(key);
      const winner = this.store.lastSuccess(key);
      const healed = winner && winner.source === 'heal' && typeof winner.xpath === 'string' ? winner.xpath : null;
      const target = this.winnerTarget(key);
      return { native, healed, hosts: target && target.hosts.length ? target.hosts : null };
    });
  }

  /** @internal Where the Locator of an element found inside the shadow root that hosts lead
   * to matches now: 'placed' when it still matches one element, and that one inside it (see
   * HealLocator.waitPlaced). */
  placement(native: Locator, hosts: string[]): Promise<Placement | null> {
    return this.driver.placement(native, hosts);
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

  /** attempt(), and after a miss, handle the registered interruptions and try again while one
   * was handled (the Python package's _resolve_step). Throws the last miss when none is left to
   * handle. budget is the step's, when it handled some already. */
  private async resolveStep<T>(key: string, attempt: () => Promise<T>, plan: Pick<Plan, 'timeoutMs' | 'pollMs'>,
                               budget: Budget = newBudget()): Promise<T> {
    let missed: unknown;
    try {
      return await attempt();
    } catch (error) {
      if (!(error instanceof AttentionRequired)) throw error;
      missed = error;
    }
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

  /** The heal context: the URL, and the pages the page guard compares (this page among them
   * while a step that key was found during runs, see stepPages()). */
  private flowContext(key: string): FlowContext {
    const url = this.driver.url();
    const meta = this.store.metaOf(key);
    const page = pagePath(url);
    return { url, page, lastPage: meta.lastPage, pages: this.stepPages(key, meta, page) };
  }

  /** Heal key through the coordinator after its lookup missed (missed), then retry the
   * lookup (the Python package's Healer._heal), where healScope() says. */
  private async heal<T>(key: string, missed: AttentionRequired, attempt: () => Promise<T>): Promise<T> {
    const context = this.flowContext(key);
    const [refusal, scope] = await this.healScope(key);
    return resolveWithHeal(() => attempt(), this.coordinator, key, context, missed, refusal, scope);
  }

  /** Where a heal of key runs (the Python package's Healer._heal_scope), as [refusal, scope]:
   * - A key whose element was last found inside an open shadow root (where the heal that
   *   matched last runs, or the hosts its chain names when the Python package 0.9.0 dropped
   *   its shadowHosts, else the key's shadowHosts) heals inside that shadow root, through a
   *   ShadowDriver: the fingerprint tier and the model get what the shadow root holds and
   *   nothing of the page around it. It is refused when its hosts lead to no open shadow root
   *   now (a host gone, matching several elements, or with a closed shadow root), when one of
   *   them is a host nothing tells apart, and when the shadow root is inside an iframe. Such a
   *   key is never healed on the page outside its shadow root. A refusal comes after the
   *   checks for a key marked destructive and the page guard.
   * - A key whose latest match was inside an iframe heals inside that iframe while it is on
   *   the page (its selector matches one iframe element with a document): the scope reads the
   *   iframe's document only, so an element that looks the same on the page around it is
   *   never picked.
   * - Otherwise, the iframe gone too, the key heals on the page, as the Python package's does. */
  private async healScope(key: string): Promise<[Refusal | null, HealScope | null]> {
    const { frame, hosts, why } = healPlace(this.store, key);
    if (why) return [{ code: 'shadow', reason: why }, null];
    if (hosts.length) {
      const shadowed = driverShadowed(this.driver, hosts);
      if (shadowed === null || (await shadowed.innerHtml()) === null) {
        return [{ code: 'shadow', reason: shadowUnreachable(key, hosts as string[]) }, null];
      }
      return [null, { kind: 'shadow', hosts: shadowed.hosts, driver: shadowed }];
    }
    if (!frame) return [null, null];
    const driver = await this.driver.framed(frame);
    return [null, driver ? { kind: 'frame', frame, driver } : null];
  }

  // --- following a key's own selector into an iframe (see frames.ts) ------------------------

  /** After key missed on the page: when one of its own selectors that are looked up in the
   * page itself matches one element inside one iframe directly in the page, look for it there
   * from now on, and return the retried lookup (the Python package's
   * Healer._follow_into_frame). No model is asked: the selector is the key's own, and only its
   * place changed. It counts as a heal with source 'frame'. Returns null when none of its
   * selectors matches in an iframe.
   *
   * Throws AttentionRequired, the miss with why it was not followed, in dry run, under
   * MENDWRIGHT_DISABLE=1 or while a guard blocks heals, for a key marked destructive, a match
   * in several iframes or several elements there, an iframe inside another iframe, an iframe
   * nothing tells apart from the others, a Playwright engine selector, a lookup quarantined or
   * superseded before, a page the key was never found on (the page guard), and an element that
   * fails the live check. */
  private async followIntoFrame(key: string, missed: AttentionRequired,
                                attempt: () => Promise<Locator>): Promise<Locator | null> {
    const matches: [string, FrameMatch[]][] = [];
    for (const selector of frameCandidates(this.store, key)) {
      let frames: FrameMatch[];
      try {
        frames = await this.driver.frameMatches(selector);
      } catch {
        continue;
      }
      if (frames.length) matches.push([selector, frames]);
    }
    if (!matches.length) return null;
    const [selector, frames, frame, alt] = this.pickFrame(key, missed, matches);
    this.frameGuard(key, missed);
    const why = await this.frameFollow(key, selector, frame, alt);
    if (why) this.frameStop(key, missed, selector, frames, why);
    return attempt();
  }

  /** From the key's selectors that match inside an iframe ([selector, frames]), the first one
   * that can be followed, as [selector, frames, the iframe's selector, the selector to save]
   * (see frames.framePlan). Throws the AttentionRequired of the first refusal when none can. */
  private pickFrame(key: string, missed: AttentionRequired,
                    matches: [string, FrameMatch[]][]): [string, FrameMatch[], string, string] {
    const coordinator = this.coordinator;
    const rules = {
      disabled: coordinator.disabled, dryRun: coordinator.dryRun, destructive: this.store.metaOf(key).destructive,
      status: (selector: string) => this.store.status(key, selector),
    };
    let refused: [string, FrameMatch[], string | null] | null = null;
    for (const [selector, frames] of matches) {
      const [frame, alt] = framePlan(key, selector, frames, rules);
      if (frame !== null) return [selector, frames, frame, alt as string];
      if (refused === null) refused = [selector, frames, alt];
    }
    const [selector, frames, why] = refused as [string, FrameMatch[], string | null];
    return this.frameStop(key, missed, selector, frames, why);
  }

  /** Throw as the page guard does when key was never found on this page. */
  private frameGuard(key: string, missed: AttentionRequired): void {
    const moved = this.coordinator.flowChange(key, this.flowContext(key));
    if (!moved) return;
    this.coordinator.decline(key, moved, 'warning', null, 'page_guard');
    throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + moved);
  }

  /** Check the guard, then alt inside the iframe frame on the live page (exactly one visible
   * element of the key's role), then save alt there, record it as a heal and wait for its
   * canary. '' when key was followed, else why not. */
  private async frameFollow(key: string, selector: string, frame: string, alt: string): Promise<string> {
    if (!this.coordinator.guard()) return 'a transaction guard is currently blocking heals';
    const reason = await liveReason(this.driver, alt, this.store.metaOf(key), frame);
    if (reason) return "its element there failed mendwright's checks (" + reason + ')';
    this.store.addCandidate(key, alt, { source: 'heal', frameXPath: frame, shadowHosts: [] });
    this.coordinator.recordFrame(key, selector, alt, frame);
    this.coordinator.pending.set(key, alt);
    return '';
  }

  /** Record and throw the AttentionRequired for a key found only inside an iframe that
   * mendwright did not follow it into: a heal.declined with the code 'frame' ('dry_run' and
   * 'disabled' on purpose), and the miss with why (frames.frameMessage). */
  private frameStop(key: string, missed: AttentionRequired, selector: string, frames: FrameMatch[],
                    why: string | null): never {
    const reason = frameMessage(key, selector, frames, why);
    this.coordinator.decline(key, reason, 'warning', null, this.coordinator.placeCode('frame'));
    throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
  }

  // --- following a key's own selector into an open shadow root (see shadow.ts) --------------

  /** After key missed on the page, and in no iframe: when none of its own selectors matches
   * anything on the page, and one of them matches one element inside one open shadow root of
   * the page, look for it there from now on, and return the retried lookup (the Python
   * package's Healer._follow_into_shadow). Playwright's CSS reaches into open shadow roots by
   * itself, so only an XPath is followed: it runs inside no shadow root, so it is evaluated in
   * each root's HTML, and its element is looked up through a CSS form of it, saved as the chain
   * of the hosts and the CSS with the hosts as its shadowHosts. No model is asked: the selector
   * is the key's own, and only its place changed. It counts as a heal with source 'shadow'.
   * Only a key mendwright has no record of, or one last found in that same shadow root, is
   * followed (scope.followAllowed); any other heals where it was last found. Returns null when
   * no selector of the key may be followed into a shadow root.
   *
   * Throws AttentionRequired, the miss with why it was not followed, for the refusals of
   * shadow.shadowPlan, on a page the key was never found on (the page guard), while a guard
   * blocks heals, and when no CSS form of the element passes the live check there. */
  private async followIntoShadow(key: string, missed: AttentionRequired,
                                 attempt: () => Promise<Locator>): Promise<Locator | null> {
    const matches = await this.shadowMatches(key);
    if (!matches.length) return null;
    const [selector, found, root] = this.pickShadow(key, missed, matches);
    this.frameGuard(key, missed);
    const why = await this.shadowFollow(key, selector, root);
    if (why) this.shadowStop(key, missed, selector, found, why);
    return attempt();
  }

  /** [selector, found, cut] for each of key's own selectors on the page that a follow may take
   * into open shadow roots (the Python package's Healer._shadow_matches): it matches inside them
   * (see shadow.shadowFound), where the key may be followed (scope.followAllowed). A CSS
   * selector is never one: Playwright's CSS pierces, so what it matches there it matches on
   * the page. [] when any of the key's selectors on the page matches something there (one
   * whose count throws counts as a match): the key's element may be that one. */
  private async shadowMatches(key: string): Promise<[string, ShadowFound[], boolean][]> {
    const selectors = frameCandidates(this.store, key);
    const read = (docs: string[], xpath: string) => this.driver.lib('cssFormsEach', { htmls: docs, xpath });
    const matches: [string, ShadowFound[], boolean][] = [];
    for (const selector of selectors) {
      let roots;
      let cut: boolean;
      try {
        if (classify(selector) === CSS) continue;
        [roots, cut] = await this.driver.shadowMatches(selector);
      } catch {
        continue;
      }
      const found = await shadowFound(selector, roots, read);
      if (found.length && followAllowed(this.store, key, found)) matches.push([selector, found, cut]);
    }
    if (matches.length) {
      for (const selector of selectors) {
        let count: number;
        try {
          count = await this.driver.count(selector);
        } catch {
          return [];
        }
        if (count !== 0) return [];
      }
    }
    return matches;
  }

  /** From the key's selectors that match inside shadow roots ([selector, found, cut]), the
   * first one that can be followed, as [selector, found, the shadow root] (see
   * shadow.shadowPlan). Throws the AttentionRequired of the first refusal when none can. */
  private pickShadow(key: string, missed: AttentionRequired,
                     matches: [string, ShadowFound[], boolean][]): [string, ShadowFound[], ShadowFound] {
    const coordinator = this.coordinator;
    const rules = {
      disabled: coordinator.disabled, dryRun: coordinator.dryRun, destructive: this.store.metaOf(key).destructive,
      status: (selector: string) => this.store.status(key, selector),
    };
    let refused: [string, ShadowFound[], string] | null = null;
    for (const [selector, found, cut] of matches) {
      const [root, why] = shadowPlan(key, selector, found, cut, rules);
      if (root !== null) return [selector, found, root];
      if (refused === null) refused = [selector, found, why as string];
    }
    const [selector, found, why] = refused as [string, ShadowFound[], string];
    return this.shadowStop(key, missed, selector, found, why);
  }

  /** Check the guard, then each CSS form of root inside its shadow root on the live page
   * (exactly one visible element of the key's role, not quarantined or superseded), and save
   * the first that passes there, as the chain of the hosts and the CSS with the hosts as its
   * shadowHosts, record it as a heal and wait for its canary. '' when key was followed, else
   * why not. */
  private async shadowFollow(key: string, selector: string, root: ShadowFound): Promise<string> {
    if (!this.coordinator.guard()) return 'a transaction guard is currently blocking heals';
    const hosts = root.hosts as string[];
    const shadowed = driverShadowed(this.driver, hosts);
    if (shadowed === null) return 'nothing tells one of the shadow hosts on the way apart';
    const meta = this.store.metaOf(key);
    let why = 'no CSS selector for it passed';
    for (const css of root.forms) {
      const saved = shadowText(hosts, css);
      const status = this.store.status(key, saved);
      if (status === 'quarantined' || status === 'superseded') continue;
      const reason = await liveReason(shadowed, css, meta);
      if (reason) {
        why = "its element there failed mendwright's checks (" + reason + ')';
        continue;
      }
      this.store.addCandidate(key, saved, { source: 'heal', frameXPath: '', shadowHosts: hosts });
      this.coordinator.recordShadow(key, selector, saved, hosts);
      this.coordinator.pending.set(key, saved);
      return '';
    }
    return why;
  }

  /** Record and throw the AttentionRequired for a key found only inside a shadow root that
   * mendwright did not follow it into: a heal.declined with the code 'shadow' ('dry_run' and
   * 'disabled' on purpose), and the miss with why (shadow.shadowMessage). */
  private shadowStop(key: string, missed: AttentionRequired, selector: string, found: ShadowFound[],
                     why: string): never {
    const reason = shadowMessage(key, selector, found, why);
    this.coordinator.decline(key, reason, 'warning', null, this.coordinator.placeCode('shadow'));
    throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
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
      // A heal saved before the key was marked: by says why, as the Python package's does.
      this.store.quarantine(key, xpath);
      this.coordinator.journal.event('heal.quarantine', { key, xpath, by: 'destructive' });
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

  /** Save the page key's element was just found on, its address, and the step runSteps() runs
   * now, if any (see rememberStep()), with at most one write of the store for all three. */
  private rememberPage(key: string): void {
    const url = this.driver.url();
    const page = pagePath(url);
    this.store.batch(() => {
      this.rememberStep(key, page);
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

  /** Handle each registered interruption that the page shows now, other than before itself
   * and one whose run function this is inside (see others()), or only the one registered as
   * only. True when one was handled.
   * Throws InterruptionLoop when the step's budget is used up and one is still there, and
   * what a run function threw. A run function of this healer that runs in another context
   * is waited for first, and again when one started there while an interruption was looked
   * at, so that the look and the action come with no wait between them: a run function is
   * never called a second time while it runs, and nothing acts next to it. */
  private async handleInterruptions(before: string, budget: Budget, timeoutMs: number, pollMs: number,
                                    only: string | null = null): Promise<boolean> {
    await this.awaitRuns();
    let handled = false;
    for (const item of this.others(before)) {
      if (only !== null && item.key !== only) continue;   // runSteps() handles one key at a time
      let target = await this.interruptionTarget(item);
      while (target !== null && this.runsElsewhere().length) {
        await this.awaitRuns();
        target = await this.interruptionTarget(item);
      }
      if (target === null) continue;
      if (budget.used >= MAX_INTERRUPTIONS_PER_STEP) {
        this.coordinator.journal.event('flow.stop', { key: item.key, code: 'interruption_loop', before });
        const fn = item.run ? functionName(item.run) : null;
        throw new InterruptionLoop(loopMessage(before, item.key, budget.used, fn));
      }
      budget.used += 1;
      if (await this.runInterruption(item, target, before, timeoutMs, pollMs)) {
        budget.handled.push(item.key);
        handled = true;
      }
    }
    return handled;
  }

  /** Perform the interruption's action on target, or call its run function (see
   * runFunction()), wait for it to go away, and record it. False (with a WARNING) when the
   * action failed in the browser; what a run function threw is thrown. One still there after
   * its run function returned counts as handled all the same, so the step's budget stops it. */
  private async runInterruption(item: Interruption, target: string, before: string, timeoutMs: number,
                                pollMs: number): Promise<boolean> {
    let fn: string | null = null;
    if (item.run) {
      fn = functionName(item.run);
      await this.runFunction(item, target, before, fn);
    } else {
      const loc = this.driver.locator(target) as unknown as Record<string, (...args: unknown[]) => Promise<unknown>>;
      const positional = ['fill', 'type', 'press', 'pressSequentially', 'selectOption', 'setChecked', 'setInputFiles',
        'dragTo'].includes(item.action) ? 1 : 0;
      const args = item.args.length === positional ? [...item.args, { timeout: Math.max(timeoutMs, 1000) }]
        : [...item.args];
      try {
        await loc[item.action](...args);
      } catch (error) {
        if (isUsageError(error)) throw error;
        this.coordinator.emit('warning', unhandledMessage(item.key, before, error));
        return false;
      }
    }
    // Wait, up to SETTLE_MS or the step's timeout, for it to go away (a banner that fades out).
    const limit = Math.min(Math.max(timeoutMs, 0), SETTLE_MS);
    const deadline = Date.now() + limit;
    while (limit > 0 && (await this.interruptionTarget(item, false)) !== null && Date.now() < deadline) {
      await sleep(Math.max(pollMs, 20));
    }
    if (!this.handled.has(item.key)) this.handled.set(item.key, before);
    this.interruptLog.push({ key: item.key, before, popup: null, how: null, label: null });
    this.coordinator.emit('warning', handledMessage(item.key, before, fn));
    this.coordinator.journal.event('flow.interrupt', {
      key: item.key, before, action: item.action, preset: item.preset, selector: item.preset ? null : target,
      function: fn,
    });
    return true;
  }

  /** @internal Handle what the page shows before an action or the first read of key. The
   * interruptions are handled under the healer's mutex (see turn()), as a lookup's are: two
   * steps awaited together never handle one at once. */
  async beforeAction(key: string, timeoutMs: number, pollMs: number): Promise<Budget> {
    const budget = newBudget();
    if (this.others(key).length) await this.turn(() => this.handleInterruptions(key, budget, timeoutMs, pollMs));
    return budget;
  }

  /** @internal After an action on key failed because something covered native's element:
   * handle the registered interruptions the page shows (under the mutex, as beforeAction()
   * does), else, with dismiss: true, dismiss a popup over the element. True when one was
   * handled or dismissed. */
  async unblock(key: string, native: Locator, budget: Budget, timeoutMs: number, pollMs: number): Promise<boolean> {
    if (this.others(key).length && (await this.turn(() => this.handleInterruptions(key, budget, timeoutMs, pollMs)))) {
      return true;
    }
    return this.dismiss(key, native, budget, timeoutMs, pollMs);
  }

  /** @internal Run verb on native as the step key: the registered interruptions the page
   * shows first, then, with dismiss: true, a popup over its element (not before focus() and
   * setInputFiles()), then the action. After an action that failed because something covered
   * its element, an interruption is handled, else a popup dismissed, and only then is the
   * action tried once more. */
  async runStep(key: string, native: Locator, verb: string, args: unknown[], timeoutMs: number, pollMs: number,
                hooks: StepHooks = {}): Promise<unknown> {
    const run = () => (native as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[verb](...args);
    const budget = await this.beforeAction(key, timeoutMs, pollMs);
    hooks.started?.();
    await this.dismissBefore(key, native, verb, budget, timeoutMs, pollMs);
    await hooks.place?.();
    try {
      return await run();
    } catch (error) {
      if (isUsageError(error)) throw error;
      const intercepted = isInterceptionError(error);
      const used = budget.used;
      if (!(intercepted && (await this.unblock(key, native, budget, timeoutMs, pollMs)))) {
        if (intercepted && budget.used === used && !budget.gaveUp) this.reportNothingMatched(key);
        hooks.failed?.();
        throw error;
      }
      await hooks.place?.();
      try {
        return await run();   // once, after an interruption was handled or a popup dismissed
      } catch (again) {
        if (isUsageError(again)) throw again;
        hooks.failed?.();
        throw again;
      }
    }
  }

  /** @internal An action of one of heal.page's guarded locators (with dismiss: true): verb on
   * the real Locator, as the step named step, with the checks of runStep() and no heal
   * canary: no heal found that element. */
  async runGuarded(step: string, native: Locator, verb: string, args: unknown[]): Promise<unknown> {
    const real: unknown[] = [];
    for (const arg of args) real.push(await nativeOf(arg));
    return this.runStep(step, native, verb, real, timeoutOf(args) ?? this.timeoutMs, 200);
  }

  // --- popups nobody registered: open({ dismiss: true }) -----------------------------------

  /** Before an action on native, after beforeAction() handled the registered interruptions
   * the page shows: with dismiss: true, dismiss a popup that covers its element (see
   * dismiss()). Not before focus() or setInputFiles(), which work on a covered element. With
   * dismiss off it makes no browser call. */
  private async dismissBefore(key: string, native: Locator, verb: string, budget: Budget, timeoutMs: number,
                              pollMs: number): Promise<boolean> {
    if (!this.dismissOn || NOT_LOOKED_AT.has(verb)) return false;
    return this.dismiss(key, native, budget, timeoutMs, pollMs);
  }

  /** With dismiss: true, dismiss each popup that covers native's element, while the step's
   * budget lasts. True when at least one was dismissed.
   *
   * What counts as covered and as a popup is dismiss.ts's to say: it never acts on a popup
   * that holds the element or on a small fixed bar. The look at what covers the element costs
   * one round trip; a popup is then dismissed under a lock, so two actions awaited together
   * never click the same one, and that looks again first. Nothing is dismissed while an
   * interruption the step handled is still on the page (one that fades out slowly may be the
   * popup over the element), nor a role="alertdialog", which asks for an answer, nor, with
   * cookies: 'accept', a popup that looks like a consent banner or has a button that says
   * accept, agree or allow. Each popup counts once against the budget, whatever was tried on
   * it, and InterruptionLoop is thrown when the budget is used up and one is still in the way
   * (one that comes back each time, say). A popup that could not be dismissed is named in one
   * WARNING and not looked at again in this step (budget.gaveUp). Nothing is dismissed while
   * a run function of heal.interrupt() runs, in this context or another (this.runs): it may
   * be filling in the very popup. That is looked at before the look at the element and again
   * once the lock is held, since a run function may have started while the look waited. */
  private async dismiss(before: string, native: Locator | null, budget: Budget, timeoutMs: number,
                        pollMs: number): Promise<boolean> {
    if (!this.dismissOn || native === null || budget.gaveUp || this.runs.length) return false;
    const seen = await driverPopupProbe(this.driver, native, null, true);
    if (popupOf(parseProbe(seen)) === null) return false;
    return this.popupMutex.run(() => this.dismissPopups(before, native, budget, timeoutMs, pollMs));
  }

  private async dismissPopups(before: string, native: Locator, budget: Budget, timeoutMs: number,
                              pollMs: number): Promise<boolean> {
    const driver = this.driver;
    let dismissed = false;
    let again = false;
    if (this.runs.length || (await this.interruptionLeft(budget))) return false;
    let probe = parseProbe(await driverPopupProbe(driver, native, null, true));
    for (;;) {
      const popup = popupOf(probe);
      if (popup === null) return dismissed;
      if (asksAnswer(popup)) {
        this.coordinator.emit('warning', alertMessage(before, popup.summary));
        budget.gaveUp = popup.summary;
        return dismissed;
      }
      if (this.acceptsConsent() && isConsent(popup)) {
        this.coordinator.emit('warning', consentMessage(before, popup.summary));
        budget.gaveUp = popup.summary;
        return dismissed;
      }
      if (budget.used >= MAX_INTERRUPTIONS_PER_STEP) {
        // No popup text in the event: the code and the step it was in front of.
        this.coordinator.journal.event('flow.stop', { key: DISMISS_KEY, code: 'popup_loop', before });
        throw new InterruptionLoop(dismissLoopMessage(before, popup.summary, budget.used,
          budget.handled.includes(DISMISS_KEY)));
      }
      const root = await driverPopupRoot(driver, native, expectOf(popup));
      if (root === null) {
        if (!again) {
          again = true;   // it changed between two looks: look once more
          probe = parseProbe(await driverPopupProbe(driver, native, null, true));
          continue;
        }
        this.coordinator.emit('warning', undismissedMessage(before, popup.summary, [], { changed: true }));
        budget.gaveUp = popup.summary;
        return dismissed;
      }
      again = false;
      if (this.acceptsConsent() && asksConsent(await driverPopupControls(driver, native, root))) {
        await driverPopupRelease(driver, root);
        this.coordinator.emit('warning', consentMessage(before, popup.summary));
        budget.gaveUp = popup.summary;
        return dismissed;
      }
      budget.used += 1;
      const [done, next] = await this.dismissPopup(before, native, popup, root, timeoutMs, pollMs);
      if (!done) {
        budget.gaveUp = popup.summary;
        return dismissed;
      }
      probe = next;
      dismissed = true;
      budget.handled.push(DISMISS_KEY);
    }
  }

  /** Whether an interruption the step handled is still on the page: the popup over the
   * element may be that one, a banner that fades out slowly, say, and declining it could undo
   * what its registered action did. */
  private async interruptionLeft(budget: Budget): Promise<boolean> {
    for (const key of budget.handled) {
      const item = this.interruptions.get(key);
      if (item && (await this.interruptionTarget(item)) !== null) return true;
    }
    return false;
  }

  /** Whether cookies: 'accept' registered its preset. */
  private acceptsConsent(): boolean {
    const item = this.interruptions.get(COOKIE_BANNER_KEY);
    return !!item && item.preset === 'accept';
  }

  /** Press Escape (when the keyboard focus lets the key reach the popup, and never with
   * cookies: 'accept': every key listener of the page gets the key, a consent banner's too),
   * then click the popup's close button, then a decline button, and look again after each
   * (popupSettle). root is the handle on the popup, which this lets go of. [true, the last
   * probe] once one worked; else [false, null] after the WARNING that names the popup. When
   * the element can no longer be looked at after a step, or a click threw after it may have
   * reached the page, mendwright cannot tell what that step did: it stops there and does not
   * count the popup as dismissed. */
  private async dismissPopup(before: string, native: Locator, popup: Popup, root: PopupRoot, timeoutMs: number,
                             pollMs: number): Promise<[boolean, Probe | null]> {
    const driver = this.driver;
    const limit = Math.min(Math.max(timeoutMs || 0, 0), SETTLE_MS);
    const done: PopupStep[] = [];   // each step that did not close it, in order
    let escaped = false;
    const consent = this.acceptsConsent();
    try {
      if (!consent && (await driverPressEscape(driver, native, root))) {
        escaped = true;
        const outcome = await this.afterStep(before, native, popup, root, limit, pollMs, ['escape', null], done);
        if (outcome) return outcome;
      }
      for (const kind of ['close', 'decline'] as const) {
        const chosen = pick(await driverPopupControls(driver, native, root), popup)[kind];
        if (!chosen) continue;
        const [index, control] = chosen;
        const clicked = await driverPopupClick(driver, native, root, expectControl(index, control),
          Math.max(limit, 1000));   // 0 is no limit to Playwright
        if (!clicked) continue;   // it changed since it was listed, and was not clicked
        const step: PopupStep = [kind, controlText(control)];
        if (clicked === UNSURE) {
          done.push(step);
          this.coordinator.emit('warning', lostMessage(before, popup.summary, 'clicked ' + stepText(...step), true));
          this.journalDismiss(before, popup, 'lost', null, null, done);
          return [false, null];
        }
        const outcome = await this.afterStep(before, native, popup, root, limit, pollMs, step, done);
        if (outcome) return outcome;
      }
      if (done.length) {
        // One last look: a popup that closes with an animation may be gone by now.
        const probe = parseProbe(await driverPopupProbe(driver, native, root));
        if (popupState(probe) === 'gone') {
          const [how, label] = done.pop() as PopupStep;
          this.recordDismissal(before, popup, how, label, done);
          return [true, probe];
        }
      }
    } finally {
      await driverPopupRelease(driver, root);
    }
    this.coordinator.emit('warning', undismissedMessage(before, popup.summary, done.map((s) => stepText(...s)),
      { escaped, consent }));
    if (done.length) this.journalDismiss(before, popup, 'failed', null, null, done);
    return [false, null];
  }

  /** Look again after one step on the popup. [true, probe] once it was recorded as
   * dismissed; [false, null] after the WARNING for an element that can no longer be looked
   * at; null when the popup is still there, with step added to done. */
  private async afterStep(before: string, native: Locator, popup: Popup, root: PopupRoot, limitMs: number,
                          pollMs: number, step: PopupStep, done: PopupStep[]): Promise<[boolean, Probe | null] | null> {
    const [probe, state] = await this.popupSettle(native, root, limitMs, pollMs);
    if (state === 'gone') {
      this.recordDismissal(before, popup, step[0], step[1], done);
      return [true, probe];
    }
    done.push(step);
    if (state === 'lost') {
      const [how, label] = step;
      const did = how === 'escape' ? 'pressed Escape' : 'clicked ' + stepText(how, label);
      this.coordinator.emit('warning', lostMessage(before, popup.summary, did));
      this.journalDismiss(before, popup, 'lost', null, null, done);
      return [false, null];
    }
    return null;
  }

  /** Look at native's element again until the popup's root is gone or hidden, or the element
   * is no longer covered, for up to limitMs. Looks at least once, even with no time to wait.
   * The last probe, and popupState() of it: 'gone', 'lost' or, at the end, 'there'. */
  private async popupSettle(native: Locator, root: PopupRoot, limitMs: number,
                            pollMs: number): Promise<[Probe | null, 'gone' | 'lost' | 'there']> {
    const deadline = Date.now() + limitMs;
    for (;;) {
      const probe = parseProbe(await driverPopupProbe(this.driver, native, root));
      const state = popupState(probe);
      if (state !== 'there' || Date.now() >= deadline) return [probe, state];
      await sleep(Math.max(pollMs, 20));
    }
  }

  /** One WARNING, a flow.dismiss journal event and an entry in handledInterruptions for a
   * popup that was dismissed with how ('escape', 'close' or 'decline'; label is the button's)
   * after the steps in done did not close it. */
  private recordDismissal(before: string, popup: Popup, how: string, label: string | null, done: PopupStep[]): void {
    this.interruptLog.push({ key: DISMISS_KEY, before, popup: popup.summary, how, label });
    this.coordinator.emit('warning', dismissedMessage(before, popup.summary, stepText(how, label),
      done.map((s) => stepText(...s))));
    this.journalDismiss(before, popup, 'dismissed', how, label, done);
  }

  /** The flow.dismiss journal event: outcome is 'dismissed' (how and label say with what),
   * 'failed' or 'lost'; tried lists each step before it that did not close the popup, so every
   * key mendwright pressed and button it clicked is on record. */
  private journalDismiss(before: string, popup: Popup, outcome: string, how: string | null, label: string | null,
                         done: PopupStep[]): void {
    const tried = done.map(([h, lab]) => (lab === null ? { how: h } : { how: h, label: lab }));
    this.coordinator.journal.event('flow.dismiss', {
      key: DISMISS_KEY, before, popup: popup.summary, outcome, how, label, tried,
    });
  }

  /** @internal value, which the read verb(...args) returned for key, when it passes expect
   * or key was not found through a heal (healed is null). A value that passes confirms a heal
   * of key made in this process, by 'expect_value'. One that fails quarantines the heal (a
   * heal.canary with success false and a heal.quarantine for one made in this process, else a
   * heal.quarantine), each with the reason and never the value, logs a WARNING and throws
   * AttentionRequired with the key, the heal and the value. */
  async checkValue(key: string, healed: string | null, verb: string, args: readonly unknown[], value: unknown,
                   expect: Expectation | null): Promise<unknown> {
    if (healed === null || expect === null) return value;
    const coordinator = this.coordinator;
    const ok = await expect.passes(value);
    const reason = ok ? undefined : refusalReason(verb, args, expect);
    if (coordinator.isPending(key, healed)) {
      coordinator.confirmPending(key, ok, { reason, xpath: healed, by: 'expect_value' });
    } else if (!ok) {
      this.store.quarantine(key, healed);
      coordinator.journal.event('heal.quarantine', { key, xpath: healed, reason });
    }
    if (ok) return value;
    const message = refusedMessage(key, healed, verb, args, value, expect);
    coordinator.emit('warning', message);
    throw new AttentionRequired('mendwright: ' + message);
  }

  /** @internal */
  reportNothingMatched(key: string): void {
    const others = this.others(key);
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
  private resolved: Promise<Located> | null = null;
  private nativeLocator: Locator | null = null;
  // The CSS of the shadow hosts of the shadow root the element was found in, else null.
  private hosts: string[] | null = null;
  private isPresent = true;
  // Whether a read still handles the registered interruptions first: until a read or an
  // action of the element did, once per lookup. The elements of one heal.locatorAll() share it.
  private reads: { due: boolean } = { due: true };
  // Whether the heal canary hears of the element's actions and confirm(): not for an element of
  // heal.locatorAll(), which no heal found.
  private canary = true;
  // The selector a heal saved that the element was found through, else null.
  private healed: string | null = null;

  constructor(healer: Healer, key: string, plan: Plan) {
    this.healer = healer;
    this.key = key;
    this.plan = plan;
  }

  /** @internal One element of a list heal.locatorAll() looked up (the Python package's
   * Healer._item): resolved already to native, with the heal canary off, since no list is found
   * through a heal, and reads shared by the elements of the lookup. hosts are those of the
   * shadow root the list was found in, as for an element heal.locator() found there. */
  static element(healer: Healer, key: string, native: Locator, plan: Plan, reads: { due: boolean },
                 hosts: string[] | null): HealLocator {
    const item = new HealLocator(healer, key, plan);
    item.resolved = Promise.resolve({ native, healed: null, hosts });
    item.nativeLocator = native;
    item.hosts = hosts;
    item.reads = reads;
    item.canary = false;
    return item;
  }

  /** @internal A lookup of a heal.page.getBy*() call, resolved to located (see
   * Healer.getByLocator): the heal canary hears of its actions only when canary is true, when the
   * element was found through a heal and not by the call itself. */
  static found(healer: Healer, key: string, plan: Plan, located: Located, canary: boolean): HealLocator {
    const item = new HealLocator(healer, key, plan);
    item.resolved = Promise.resolve(located);
    item.nativeLocator = located.native;
    item.hosts = located.hosts;
    item.healed = located.healed;
    item.canary = canary;
    return item;
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
    this.hosts = result.hosts;
    this.healed = result.healed;
    this.isPresent = result.native !== null;
    return result.native;
  }

  /** For an element found inside a shadow root (a heal the Python package saved there), right
   * before an action or a read: wait, up to the step's timeout, until its Locator matches one
   * element and that one inside the shadow root. Playwright resolves the Locator again for each
   * action and read, and its CSS pierces every open shadow root, so a re-render after the
   * lookup could otherwise send it to an element outside that shadow root. Throws
   * AttentionRequired when it never does, and reports nothing to the heal canary.
   *
   * Resolves to true when the action or read may run. For isVisible() and isHidden()
   * (ABSENT_ANSWERS) it resolves to false at once when the Locator matches nothing at all,
   * inside the shadow root or outside it: the element went away, and the read answers as
   * Playwright's does for such a Locator, without running. A match outside the shadow root,
   * or several matches, still waits, and throws. */
  private async waitPlaced(native: Locator, verb: string): Promise<boolean> {
    const hosts = this.hosts;
    if (!hosts) return true;
    const answers = ABSENT_ANSWERS.has(verb);
    const deadline = Date.now() + this.plan.timeoutMs;
    for (;;) {
      const where = await this.healer.placement(native, hosts);
      if (where === 'placed') return true;
      if (where === 'nowhere' && answers) return false;
      if (Date.now() >= deadline) throw new AttentionRequired(movedMessage(this.key, hosts, verb));
      await sleep(Math.max(this.plan.pollMs, 20));
    }
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

  /** Confirm (true) or quarantine (false) a heal of this key, from a check of your own. An
   * element of heal.locatorAll() reports nothing: no list is found through a heal, and a heal of
   * one element under the same key is not this element's. */
  confirm(success: boolean): this {
    if (this.canary) this.healer.coordinator.confirmPending(this.key, Boolean(success), { by: 'confirm' });
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
    for (let i = 0; i < args.length; i++) args[i] = await nativeOf(args[i]);
    const { timeoutMs, pollMs } = this.plan;
    const result = await this.healer.runStep(this.key, native, verb, args, timeoutMs, pollMs, {
      started: () => {
        this.reads.due = false;   // its reads need not look again
      },
      place: () => this.waitPlaced(native, verb),
      failed: () => {
        if (this.canary) this.healer.coordinator.confirmPending(this.key, false, { by: 'action' });
      },
    });
    if (expect) {
      let ok = false;
      try {
        ok = Boolean(await expect());
      } catch {
        ok = false;
      }
      if (this.canary) this.healer.coordinator.confirmPending(this.key, ok, { by: 'expect' });
    }
    return result;
  }

  /** @internal The real Locator, resolved, as an action's argument (dragTo's target). */
  locatorNow(): Promise<Locator | null> {
    return this.ensure();
  }

  /** @internal */
  async read(name: Read, args: unknown[]): Promise<unknown> {
    const native = await this.ensure();
    if (native === null) throw new Error(absentMessage(this.key));
    if (this.reads.due) {
      await this.healer.beforeAction(this.key, this.plan.timeoutMs, this.plan.pollMs);
      this.reads.due = false;
    }
    // A Locator that matches nothing: answered as Playwright does (see waitPlaced).
    if (!(await this.waitPlaced(native, name))) return ABSENT_ANSWERS.get(name);
    const value = await (native as unknown as Record<string, (...a: unknown[]) => Promise<unknown>>)[name](...args);
    // expectValue: the value of a read through a heal must pass the key's check.
    if (!VALUE_READS.has(name) || this.plan.expect === null || this.healed === null) return value;
    return this.healer.checkValue(this.key, this.healed, name, args, value, this.plan.expect);
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

// heal.page's locators: each proxy, with the real Locator it stands for and the name of its
// step, as log lines and handledInterruptions give it.
const PROXIES = new WeakMap<object, { native: unknown; step: string }>();

// What builds another locator on a Page, Frame, Locator or FrameLocator. With
// open({ dismiss: true }), heal.page hands back what they build as a guarded locator; with it
// off, the real one.
const LOCATOR_BUILDERS = new Set(['locator', 'getByRole', 'getByText', 'getByLabel', 'getByPlaceholder', 'getByAltText',
  'getByTitle', 'getByTestId', 'frameLocator', 'nth', 'filter', 'and', 'or', 'first', 'last', 'owner', 'contentFrame']);
const ACTION_NAMES: ReadonlySet<string> = new Set(ACTIONS);

/** @internal The step name of one of heal.page's locators, as log lines give it, or null for
 * anything else. */
export function stepOf(value: unknown): string | null {
  return value && typeof value === 'object' && PROXIES.has(value) ? PROXIES.get(value)!.step : null;
}

/** True for a Playwright Locator or FrameLocator: chainable (locator and frameLocator) with
 * nth(), and no document of its own (no content()), as a Page or Frame has. */
function looksLikeLocator(value: unknown): boolean {
  const v = value as Record<string, unknown> | null;
  return !!v && typeof v === 'object' && typeof v.locator === 'function' && typeof v.frameLocator === 'function'
    && typeof v.nth === 'function' && typeof v.content !== 'function';
}

/** An argument as a guarded locator's name shows it, in TypeScript: 'Continue' (a string as
 * Python's repr() writes it), { name: 'Continue', exact: true }, /Continue/i, and a locator by
 * its own step. A string longer than 60 characters is cut, as the Python package cuts it. */
function argText(value: unknown): string {
  if (value && typeof value === 'object' && PROXIES.has(value)) return PROXIES.get(value)!.step;
  if (value instanceof HealLocator) return 'heal.locator(' + pyRepr(value.key) + ')';
  if (looksLikeLocator(value)) return '<locator>';
  return plainArgText(value, argText);
}

/** A call as a guarded locator's name shows it: getByRole('button', { name: 'Go' }). Trailing
 * arguments left out (undefined) are not shown. */
function callText(name: string, args: unknown[]): string {
  return plainCallText(name, args, argText);
}

/** value as a guarded locator named name, when open({ dismiss: true }) was passed and value is
 * a Locator or FrameLocator; else value itself. */
function guard(healer: Healer, value: unknown, name: string): unknown {
  if (!healer.dismissOn || !looksLikeLocator(value)) return value;
  return guardedLocator(healer, value as Locator, name);
}

/** Member name of t (a step named step), as heal.page hands it back: with dismiss: true, a
 * locator it builds is guarded, named after the step and the call; anything else is t's. */
function chained(healer: Healer, step: string, t: object, name: string | symbol): unknown {
  const value = bound(t, name);
  if (!healer.dismissOn || typeof name !== 'string' || !LOCATOR_BUILDERS.has(name) || typeof value !== 'function') {
    return value;
  }
  return (...args: unknown[]) => guard(healer, (value as (...a: unknown[]) => unknown)(...args),
    step + '.' + callText(name, args));
}

/** With open({ dismiss: true }), what heal.page hands back for the locators it does not heal:
 * the getBy* locators, a locator built with options, a chained locator, frameLocator(),
 * and(), or(), first(), last(), nth(), filter(), and the Locators of
 * heal.page.locator(selector).all(). It is the real Locator (or FrameLocator), not healed, and
 * nothing about it is saved. Its actions run through the same checks as heal.locator()'s,
 * without the heal canary: registered interruptions are handled first, a popup that covers
 * the element is dismissed (see dismiss.ts), and an action that failed because something
 * covered the element is tried once more after one was. Locators built from it are guarded
 * the same way. expect() takes it, and native is the real one. */
function guardedLocator(healer: Healer, target: Locator, step: string): Locator {
  const proxy = new Proxy(target, {
    get(t, name) {
      if (name === 'native') return t;
      if (typeof name === 'string' && ACTION_NAMES.has(name)
          && typeof (t as unknown as Record<string, unknown>)[name] === 'function') {
        return (...args: unknown[]) => healer.runGuarded(step, t, name, args);
      }
      return chained(healer, step, t, name);
    },
  });
  PROXIES.set(proxy, { native: target, step });
  return proxy;
}

/** heal.page.locator(selector): its actions and waiting reads go through
 * heal.locator(selector, selector); everything else is the real Locator's. With
 * open({ dismiss: true }), the locators chained from it and those its all() gives are guarded
 * (see guardedLocator). */
function healingLocator(healer: Healer, target: Locator, selector: string): Locator {
  const step = 'page.locator(' + pyRepr(selector) + ')';
  const proxy = new Proxy(target, {
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
      if (name === 'all') {
        // A Locator for each element the selector matches, as Playwright gives them, never
        // healed and with nothing saved; each one guarded with dismiss: true.
        return async () => (await t.all()).map((item, i) => guard(healer, item, step + '.all()[' + i + ']'));
      }
      return chained(healer, step, t, name);
    },
  });
  PROXIES.set(proxy, { native: target, step });
  return proxy;
}

/** With open({ healGetBy: true }), what heal.page.getByRole() and the other getBy* calls return
 * (the Python package's HealingGetByLocator). Its actions and the reads that wait for the
 * element go through Healer.getByLocator(), under the call's key (see getby.ts): the call itself
 * first, a saved heal only while the call matches nothing, a heal from the call's fingerprint when
 * it matches nothing for the timeout, and Playwright as written when it matches several elements
 * or one hidden one. An action's timeout is the lookup's timeout too.
 *
 * Everything else is the real Locator's and is never healed: isVisible() (Playwright's own, so a
 * renamed button is not visible to it, while its click() heals), count(), waitFor(), isHidden(),
 * all() (as on heal.page.locator()), and Playwright's expect(). Locators built from it (first(),
 * nth(), filter(), getByText(), ...) are the real ones, or guarded ones with dismiss: true, named
 * as before. expect() takes it, and native is the Locator. */
function healingGetByLocator(healer: Healer, target: Locator, call: GetByCall, step: string): Locator {
  const proxy = new Proxy(target, {
    get(t, name) {
      if (name === 'native') return t;
      if (typeof name === 'string' && LOCATOR_HEALED.has(name)) {
        return async (...args: unknown[]) => {
          const found = await healer.getByLocator(call, t, timeoutOf(args));
          return (found as unknown as Record<string, (...a: unknown[]) => unknown>)[name](...args);
        };
      }
      if (name === 'all') {
        // A Locator for each element the call matches, as Playwright gives them, never healed and
        // with nothing saved; each one guarded with dismiss: true.
        return async () => (await t.all()).map((item, i) => guard(healer, item, step + '.all()[' + i + ']'));
      }
      return chained(healer, step, t, name);
    },
  });
  PROXIES.set(proxy, { native: target, step });
  return proxy;
}

const GET_BY_METHODS: ReadonlySet<string> = new Set(TS_METHODS);

/** heal.page: see Healer.page. */
function healingPage(healer: Healer): Page | Frame {
  const target = healer.native;
  return new Proxy(target, {
    get(t, name) {
      if (name === 'native') return t;
      if (name === 'locator') {
        return (selector: string, ...rest: unknown[]) => {
          const real = (t as Page).locator(selector, ...(rest as []));
          if (rest.some((r) => r !== undefined) || !healable(selector)) {
            // options, or a selector mendwright cannot store: the real Locator, guarded with
            // dismiss: true.
            return guard(healer, real, 'page.' + callText('locator', [selector, ...rest]));
          }
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
      if (typeof name === 'string' && GET_BY_METHODS.has(name)) {
        return (...args: unknown[]) => {
          // The real Locator first, so Playwright checks its own arguments, then, with
          // healGetBy: true and a call that can be a key, a healing one. Otherwise the real
          // Locator, guarded with dismiss: true. Nothing touches the page or the store here.
          const real = (t as unknown as Record<string, (...a: unknown[]) => Locator>)[name](...args);
          const step = 'page.' + callText(name, args);
          if (!healer.healGetBy) return guard(healer, real, step);
          const [call, why] = callOf(name, args);
          if (call === null) {
            healer.noteGetBy(step, why);
            return guard(healer, real, step);
          }
          return healingGetByLocator(healer, real, call, step);
        };
      }
      // With dismiss: true, frameLocator() and the other locators it builds come back guarded.
      return chained(healer, 'page', t, name);
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
  if (options.dismiss !== undefined && typeof options.dismiss !== 'boolean') {
    // A string such as 'no' would read as true.
    throw new TypeError('mendwright.open() takes dismiss: true or dismiss: false, got dismiss: '
      + argText(options.dismiss) + '.');
  }
  const healGetByOption = options.healGetBy;
  if (healGetByOption !== undefined && healGetByOption !== null && typeof healGetByOption !== 'boolean') {
    throw new TypeError('mendwright.open() takes healGetBy: true, healGetBy: false or healGetBy: null, got healGetBy: '
      + argText(healGetByOption) + '.');
  }
  // A key of destructiveKeys or expectValues written as a getBy* call on an object is named here.
  const warn = (message: string) => emit(options.log, 'warning', message);
  const checks = expectations(options.expectValues, warn);
  const healGetBy = getBySetting(healGetByOption);
  // A store URL (the Python package 0.11 reads one) is refused: this package keeps a local file.
  const storeWhere = options.storePath !== undefined ? 'storePath' : 'MENDWRIGHT_STORE';
  const storePath = options.store ? '' : path.resolve(localStorePath(
    options.storePath ?? process.env.MENDWRIGHT_STORE ?? DEFAULT_STORE_PATH, storeWhere));
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
  // A run id only groups journal lines, so a MENDWRIGHT_RUN_ID that is not one must not fail
  // the tests: it is ignored, and said once per open(), as the Python package says it.
  const badRunId = runId().bad;
  if (badRunId !== null) emit(options.log, 'warning', runIdProblem(badRunId));
  const store = options.store ?? loadStore(storePath, { destructiveKeys: options.destructiveKeys, warn });
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
    fingerprintOffBy: fingerprint ? null : fingerprintOffBy(options.fingerprint),
  });
  const healer = new Healer(native, driver, store, coordinator, timeoutMs);
  healer.dismissOn = Boolean(options.dismiss);
  healer.expectValues = checks;
  healer.healGetBy = healGetBy;
  if (cookies) {
    const preset = cookiePreset(cookies);
    (healer as unknown as { interruptions: Map<string, Interruption> }).interruptions.set(preset.key, preset);
  }
  return healer;
}
