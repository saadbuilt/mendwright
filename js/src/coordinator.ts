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
//
// A heal of a key last found inside an iframe or an open shadow root runs there (a HealScope,
// see scope.ts): the capture, the inventory, the fingerprint tier, the model's tools and the
// gate all read the iframe's document, or what the shadow root holds, through the scope's
// driver, and what is applied is saved with the iframe's selector, or as the chain of the
// shadow hosts and a CSS selector with the hosts. The scope is passed along, never set on the
// coordinator, so two heals that run at once never read each other's document. A key last
// found inside a shadow root is never healed without its scope (outsideShadow). A key's own
// selector followed into an iframe or a shadow root is recorded here too (recordFrame,
// recordShadow), as a heal with source 'frame' or 'shadow'.
//
// The key of a heal.page.getBy*() call (see getby.ts) is healed from its fingerprint only, never
// by a model (healGetBy).

import { buildBundle, redact } from './capture';
import { TrustedTypesRefused, type PlaywrightDriver } from './driver';
import { AttentionRequired } from './errors';
import { fmt, valid as validFingerprint } from './fingerprint';
import { flowChangeMessage } from './flow';
import { REJECTED_LIST, apply, listGate, quarantinedBodies, validateStatic } from './gate';
import { NO_MODEL, isKey, noFingerprintMessage, roleUnknownMessage, tierOffMessage } from './getby';
import type { JournalLike } from './journal';
import type { Guardrails } from './limits';
import {
  IN_SHADOW, NEVER_SEEN, PLACELESS, cameAndWentWhy, cameLine, dryRunReason, findList, foundLine, gateRefusal, inShadow,
  listMissingMessage, nonemptySearched, nonemptyUnsearched, placed, proposalEvidence, proposalMessage,
  retiredAcceptedLine, retiredProposedLine, stateWord, stateWordReason, unsavedReason, validList, waitsForFixMessage,
  type ListFingerprint, type ListMatch,
} from './lists';
import { log, type LogLevel, type LogSink } from './log';
import { MAX_PROMPT_PART, type Bundle, type Proposal } from './prompt';
import { nowIso, pyFixed, pyRepr } from './py';
import { providerName, type HealProvider, type ProviderTools } from './providers/base';
import { NOT_HERE_HINT } from './providers/errors';
import { healPlace, type HealDriver, type HealScope } from './scope';
import { hasPosition } from './selectors';
import { placeText } from './shadow';
import { storeFlag, type Candidate, type KeyMeta, type SelectorStore } from './store';
import { findMatch, shadowVisible } from './tier';
import { reprText } from './values';

export const FINGERPRINT_PROVIDER = 'fingerprint';
/** The provider and source of a key's own selector that matched only inside an iframe and is
 * looked up there from now on (see Healer.followIntoFrame): no model and no new selector, only
 * a new place. */
export const FRAME_PROVIDER = 'frame';
/** The provider and source of a key's own selector that matched only inside an open shadow
 * root and is looked up there from now on, as a CSS form of its element (see
 * Healer.followIntoShadow): no model is asked. */
export const SHADOW_PROVIDER = 'shadow';
export const DISABLED_REASON = 'healing is turned off by MENDWRIGHT_DISABLE=1, so the selector was not repaired. '
  + 'Unset MENDWRIGHT_DISABLE to let mendwright heal again.';
export const GUARD_REASON = 'a transaction guard is currently blocking heals (a payment or registration submission '
  + 'is pending), so mendwright did not try to heal. Run the step again once the guard allows heals.';

/** The code of each heal.declined event, as the Python package names them
 * (coordinator.DECLINE_CODES), which its mendwright notify turns into its own words. dry_run
 * and disabled are declines on purpose. list_empty is a list whose selectors all match nothing,
 * list_not_found one looked up with nonempty: true that nothing could be proposed for,
 * list_waits_fix one whose list heal a person accepted, not yet in the code, and get_by a
 * getBy* call its fingerprint could not heal (no model is asked). */
export const DECLINE_CODES = ['destructive', 'page_guard', 'disabled', 'no_provider', 'role_unknown', 'guard',
  'rate_limit', 'not_here', 'no_proposal', 'gate', 'dry_run', 'list_empty', 'list_not_found', 'list_waits_fix',
  'get_by', 'frame', 'shadow', 'other'] as const;
export type DeclineCode = typeof DECLINE_CODES[number];

export interface HealRecord {
  key: string;
  old: string[];
  new: string;
  confirmed: boolean | null;
  source: string;
}

/** Why a key last found inside an open shadow root is healed nowhere (see scope.healPlace and
 * scope.shadowUnreachable): it sits in an iframe, behind a host nothing tells apart, or the
 * shadow root cannot be entered now. The code of that decline is 'shadow'. */
export interface Refusal {
  reason: string;
  code: 'shadow';
}

/** What HealCoordinator.proposeList() did (the Python package's ListOutcome): kind is
 * 'proposed' (saved for a person to check: reason is the message, xpath the selector, row the
 * store's row), 'came' (a selector of the key's code matches now: reason is that selector, xpath
 * the group found), 'refused' (reason says why), 'accepted' (the group found is a list heal a
 * person accepted: row is it, count how many elements it matched on the captured page) or
 * 'dry_run'. */
export interface ListOutcome {
  kind: 'proposed' | 'came' | 'refused' | 'accepted' | 'dry_run';
  reason: string;
  xpath: string | null;
  row: Candidate | null;
  count: number;
}

/** A list proposeList() found and saved as a proposed list heal (HealCoordinator.held). */
export interface HeldList {
  key: string;
  xpath: string;
  id: string;
  label: string;
  count: number;
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

function roleNeeded(key: string, call = 'heal.locator()'): string {
  return "does not know what kind of element '" + key + "' is (its expected role is unknown), so it cannot "
    + 'check a repaired selector. Pass role to ' + call + ", for example role: 'textbox' for a text field, "
    + "'button', 'checkbox', 'link' or 'combobox'.";
}

/** Why the page was not sent to the model: it could not be cleaned. failure is what the
 * page answered or threw. */
function notCleaned(key: string, failure: unknown): string {
  const why = failure instanceof TrustedTypesRefused
    ? failure.message + ". Add 'mendwright' to that directive on your test site, or, in Chromium, set "
      + 'bypassCSP: true in the use section of your Playwright config'
    : 'the page navigated or closed while mendwright read it, or the browser could not parse it';
  return "mendwright could not remove the scripts and styles from the page, so it sent the model nothing for '" + key
    + "': " + why + '.';
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
  /** What turned the fingerprint tier off, in the words a message uses ('fingerprint: false',
   * 'MENDWRIGHT_FINGERPRINT=0' or 'MENDWRIGHT_DISABLE=1'), which open() sets. */
  fingerprintOffBy?: string | null;
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
  /** What turned the fingerprint tier off (see CoordinatorOptions), or null. */
  readonly fingerprintOffBy: string | null;
  private readonly clock: () => number;
  private readonly sink: LogSink | null;
  /** key -> the heal this process applied and has not confirmed yet. */
  readonly pending = new Map<string, string>();
  /** key -> why its last heal attempt applied nothing. */
  readonly healReasons = new Map<string, string>();
  readonly heals: HealRecord[] = [];
  /** Every list this coordinator found for heal.locatorAll() with nonempty: true and saved as a
   * proposed list heal, oldest first. None was used. */
  readonly held: HeldList[] = [];

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
    this.fingerprintOffBy = options.fingerprintOffBy ?? null;
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

  /** Record why a heal did not apply, log it, journal it as heal.declined with code, and
   * return false. logged is a shorter reason for the log line. */
  decline(key: string, reason: string, level: LogLevel = 'warning', logged?: string | null,
          code: DeclineCode = 'other'): false {
    this.healReasons.set(key, reason);
    this.emit(level, 'did not heal ' + pyRepr(key) + ': ' + (logged || reason));
    this.journal.event('heal.declined', { key, code, reason });
    return false;
  }

  /** The code of a decline because no model provider can be asked. */
  private unavailableCode(): DeclineCode {
    return this.disabled ? 'disabled' : 'no_provider';
  }

  /** @internal The code of a heal refused for where the key's element is (code): a key last
   * found inside a shadow root that cannot be healed in ('shadow'), or one found only inside
   * an iframe or a shadow root it was not followed into ('frame', 'shadow'). 'disabled' under
   * MENDWRIGHT_DISABLE=1 and 'dry_run' in dry run, declines on purpose, as the Python package
   * gives them. */
  placeCode(code: DeclineCode): DeclineCode {
    return this.disabled ? 'disabled' : this.dryRun ? 'dry_run' : code;
  }

  /** Why a heal of key may not run here, or '' (the Python package's
   * coordinator._outside_shadow): it would run on the page or in an iframe (scope is not a
   * shadow root's), and the key's element was last found inside an open shadow root
   * (scope.healPlace). The healer heals such a key inside that shadow root, or refuses it;
   * without that scope a heal would look for it outside, where an element that looks the same
   * can be. */
  private outsideShadow(key: string, scope: HealScope | null): string {
    if (scope && scope.kind === 'shadow') return '';
    const { hosts } = healPlace(this.store, key);
    if (!hosts.length) return '';
    return "'" + key + "' was last found inside " + placeText(hosts) + ', and this heal would look for it on the page '
      + 'outside that shadow root, where an element that looks the same can be, so mendwright did not heal it. '
      + 'mendwright.open() heals it inside that shadow root; or fix the selector in your code.';
  }

  explain(key: string): string {
    return this.healReasons.get(key) ?? '';
  }

  // --- lists (see lists.ts) -----------------------------------------------------------------

  /** Decline to heal list key, whose selectors all match nothing (see Healer.locatorAll), and
   * return the reason, recorded, logged and journaled as for any heal that did not apply, with
   * the code list_empty (the Python package's decline_list). mendwright never heals such a
   * list: an emptied list whose items show in another list on the page now, such as a cart's
   * in Saved for later, can be the same page as a redesign but for a class name or a heading's
   * text. So nothing is looked for on the page, no model is asked and nothing is saved. */
  declineList(key: string, meta: KeyMeta): string {
    const lfp = meta.listFingerprint as { count?: unknown } | null;
    this.decline(key, listMissingMessage(key, lfp && typeof lfp === 'object' ? lfp.count : null), 'warning', null,
      'list_empty');
    return this.explain(key);
  }

  /** Look for list key, which heal.locatorAll() with nonempty: true says has items here and
   * whose selectors broken all match nothing, and save what is found as a proposed list heal for
   * a person to check (see lists.ts). It is never used: only the selectors in your code give a
   * list. Returns a ListOutcome; every refusal is recorded, logged and journaled as a heal that
   * did not apply (the Python package's propose_list).
   *
   * In order, it refuses a key marked destructive, a key with no list fingerprint, a list whose
   * items were inside a shadow root, a list fingerprint without where the list sat (recorded
   * before a call with nonempty: true matched the list), a page the key was never found on, an
   * unknown role and the guard. Then it captures the page and looks (lists.findList), refuses a
   * group picked by a word for a state the list didn't have (lists.stateWord), and runs the list
   * gate (gate.listGate). No provider is asked, and the rate limit is not touched. */
  async proposeList(key: string, meta: KeyMeta, broken: Candidate[], context: FlowContext): Promise<ListOutcome> {
    const lfp = meta.listFingerprint;
    if (meta.destructive) return this.listRefused(key, destructiveMessage(key), 'destructive');
    if (!validList(lfp)) return this.listRefused(key, nonemptyUnsearched(key, NEVER_SEEN));
    if (inShadow(lfp)) return this.listRefused(key, nonemptyUnsearched(key, IN_SHADOW));
    const count = lfp.count;
    if (!placed(lfp)) return this.listRefused(key, nonemptySearched(key, count, PLACELESS));
    const moved = this.flowChange(key, context);
    if (moved) return this.listRefused(key, moved, 'page_guard');
    const role = meta.expectedRole;
    if (!role || role === 'unknown') {
      return this.listRefused(key, 'mendwright ' + roleNeeded(key, 'heal.locatorAll()'), 'role_unknown');
    }
    if (!this.guard()) return this.listRefused(key, GUARD_REASON, 'guard');
    const html = await this.driver.content();
    this.journal.event('heal.requested', { key, list: true });
    const started = this.clock();
    const match = await findList(lfp, html, role, (name, arg) => this.driver.lib(name, arg),
      { visible: (xpath) => this.listVisible(xpath) });
    if (!match.xpath) {
      if (!['L2', 'L3', 'L10'].includes(match.code)) {
        this.journal.event('heal.rejected', { key, list: true, reason: match.reason, source: FINGERPRINT_PROVIDER });
      }
      return this.listRefused(key, nonemptySearched(key, count, match.fragment));
    }
    const state = stateWord(match.xpath, lfp);
    if (state) {
      const reason = stateWordReason(match.xpath, state[0], state[1]);
      this.journal.event('heal.rejected', { key, xpath: match.xpath, list: true, reason, source: FINGERPRINT_PROVIDER });
      return this.listRefused(key, nonemptySearched(key, count, reason));
    }
    const reasoning = 'list fingerprint score ' + pyFixed(match.score, 2) + ', next best ' + pyFixed(match.runnerUp, 2)
      + ', ' + match.count + ' elements, ' + match.held + ' of ' + match.recorded + ' recorded items';
    this.journal.event('heal.proposed', { key, xpath: match.xpath, reasoning, source: FINGERPRINT_PROVIDER, list: true });
    const [kind, why, , css] = await listGate(this.store, key, match.xpath, html, meta, this.driver, match.css);
    if (kind === 'came') return { kind: 'came', reason: why, xpath: match.xpath, row: null, count: 0 };
    if (kind === 'accepted') {
      const rows = this.store.listHeals(key).filter((r) => r.xpath === match.xpath);
      return { kind: 'accepted', reason: '', xpath: match.xpath, row: rows[0] ?? null, count: match.count };
    }
    if (kind === 'refused') {
      this.journal.event('heal.rejected', { key, xpath: match.xpath, list: true, reason: why, source: FINGERPRINT_PROVIDER });
      return this.listRefused(key, nonemptySearched(key, count, gateRefusal(match.xpath, why)));
    }
    if (this.dryRun) {
      this.emit('info', '[heal dry-run] key=' + key + ' proposed=' + match.xpath + ' (list) valid=true (not saved)');
      const reason = dryRunReason(key, match.xpath);
      this.decline(key, reason, 'info', null, 'dry_run');
      return { kind: 'dry_run', reason, xpath: match.xpath, row: null, count: 0 };
    }
    return this.holdList(key, meta, lfp, match, css, broken, context, started);
  }

  /** Save what proposeList() found as a proposed list heal: journal heal.held, remember it in
   * held, record the message for explain() and log one WARNING. */
  private holdList(key: string, meta: KeyMeta, lfp: ListFingerprint, match: ListMatch, css: string | null,
                   broken: Candidate[], context: FlowContext, started: number): ListOutcome {
    const xpath = match.xpath as string;
    const old = broken.map((c) => c.xpath).filter(Boolean).map(String);
    const page = typeof context.page === 'string' ? context.page : '';
    const evidence = proposalEvidence(key, lfp, match, xpath, css, old, page, nowIso());
    let row: Candidate;
    try {
      row = this.store.proposeListHeal(key, xpath, { css, proposal: evidence as unknown as Record<string, unknown> });
    } catch (error) {
      const detail = String((error as Error)?.name) + ((error as Error)?.message ? ': ' + (error as Error).message : '');
      return this.listRefused(key, unsavedReason(key, 'it raised ' + detail));
    }
    // The write takes in the file: a person may have accepted or rejected this list heal since
    // the store was read, and then it is not proposed.
    if (row.status === 'accepted') return { kind: 'accepted', reason: '', xpath, row, count: match.count };
    if (row.status === 'quarantined') {
      this.journal.event('heal.rejected', { key, xpath, list: true, reason: REJECTED_LIST, source: FINGERPRINT_PROVIDER });
      return this.listRefused(key, nonemptySearched(key, lfp.count, gateRefusal(xpath, REJECTED_LIST)));
    }
    const seconds = Math.max(0, (this.clock() - started) / 1000);
    const now = evidence.now;
    this.journal.event('heal.held', { key, provider: FINGERPRINT_PROVIDER, latency_ms: Math.round(seconds * 1000), xpath,
                                      css, id: evidence.id, list: true, items: match.count, label: now.label,
                                      differs: evidence.differs, old, page: evidence.page, source: FINGERPRINT_PROVIDER });
    this.held.push({ key, xpath, id: evidence.id, label: now.label, count: match.count });
    const role = meta.expectedRole;
    const accepted = this.store.listHeals(key).find((r) => r.status === 'accepted' && r.xpath !== xpath) ?? null;
    const message = proposalMessage(key, lfp, xpath, css, role, evidence, storeFlag(this.store.overridePath),
      accepted ? { xpath: accepted.xpath, acceptedAt: accepted.acceptedAt } : null);
    this.healReasons.set(key, message);
    this.emit('warning', foundLine(key, match.count, role, now.label, old, xpath, seconds));
    return { kind: 'proposed', reason: message, xpath, row, count: match.count };
  }

  /** Decline a list looked up with nonempty: true (see decline()), as a ListOutcome. code is
   * 'list_not_found' unless a check every heal has refused it. */
  private listRefused(key: string, reason: string, code: DeclineCode = 'list_not_found'): ListOutcome {
    this.decline(key, reason, 'warning', null, code);
    return { kind: 'refused', reason, xpath: null, row: null, count: 0 };
  }

  /** Decline list key, looked up with nonempty: true, before anything was looked for: text
   * says why (see lists.nonemptyUnsearched). Returns the reason. */
  declineNonempty(key: string, text: string): string {
    const reason = nonemptyUnsearched(key, text);
    this.decline(key, reason, 'warning', null, 'list_not_found');
    return reason;
  }

  /** Log that list key came while mendwright looked for it: its own selector gave the list, and
   * nothing was proposed. */
  came(key: string, selector: string): void {
    this.healReasons.delete(key);
    this.emit('info', cameLine(key, selector));
  }

  /** Decline list key when a selector of its code matched while mendwright looked for the list
   * and matches nothing again now. Returns the reason. */
  cameAndWent(key: string, meta: KeyMeta, xpath: string | null, selector: string): string {
    const why = cameAndWentWhy(selector);
    this.journal.event('heal.rejected', { key, xpath, list: true, reason: why, source: FINGERPRINT_PROVIDER });
    const lfp = meta.listFingerprint as { count?: unknown } | null;
    const reason = nonemptySearched(key, lfp && typeof lfp === 'object' ? lfp.count : null,
      gateRefusal(String(xpath), why));
    this.decline(key, reason, 'warning', null, 'list_not_found');
    return reason;
  }

  /** Record, log and journal (heal.declined, code list_waits_fix) why list key, looked up with
   * nonempty: true, throws when a list heal a person accepted for it (row) matches count
   * elements on the page now: mendwright takes a list only from the selectors in the code, and
   * mendwright fix puts it there. Returns the reason. */
  waitsForFix(key: string, meta: KeyMeta, row: { xpath?: unknown; acceptedAt?: unknown }, count: number): string {
    const lfp = meta.listFingerprint as { count?: unknown } | null;
    const reason = waitsForFixMessage(key, lfp && typeof lfp === 'object' ? lfp.count : null, row, count,
      storeFlag(this.store.overridePath));
    this.healReasons.set(key, reason);
    this.emit('warning', reprText(key) + ' waits for mendwright fix: ' + reason);
    this.journal.event('heal.declined', { key, code: 'list_waits_fix', reason });
    return reason;
  }

  /** Log, and journal, the list heals of key that SelectorStore.retireListHeals() ended when
   * its selector matched count elements (the Python package's retired_lists): changes are its
   * [xpath, old status, new status], rows the list heals as they were before. A proposed one is
   * no longer proposed (an INFO line); an accepted one is quarantined, with a WARNING and a
   * heal.quarantine event. */
  retiredLists(key: string, selector: string, count: number, changes: [string, string, string][],
               rows: Candidate[]): void {
    const before = new Map(rows.map((r) => [r.xpath, r]));
    for (const [xpath, old] of changes) {
      const row = before.get(xpath) ?? ({} as Partial<Candidate>);
      if (old === 'proposed') {
        const evidence = row.proposal && typeof row.proposal === 'object' ? row.proposal : {};
        this.emit('info', retiredProposedLine(key, selector, (evidence as { at?: unknown }).at));
        continue;
      }
      this.emit('warning', retiredAcceptedLine(key, selector, count, row.acceptedAt, xpath));
      this.journal.event('heal.quarantine', { key, xpath, list: true, reason: 'the selector it replaced matches again' });
    }
  }

  /** For findList(): false when the live page has elements of xpath and none of them is
   * visible, true when one is, null when it could not be read or has none. */
  private async listVisible(xpath: string): Promise<boolean | null> {
    let items;
    try {
      items = await this.driver.listItems(xpath);
    } catch {
      return null;
    }
    if (!items.length) return null;
    return items.some((item) => item.visible);
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

  private tools(driver: HealDriver): ProviderTools {
    return { lib: (name, arg) => driver.lib(name, arg) };
  }

  /** Heal key and save a checked selector. Returns the saved selector, or false with the
   * reason in healReasons. refusal, when given, declines every heal after the checks for a
   * key marked destructive and the page guard: the key was last found inside an open shadow
   * root where no heal can run now (see scope.healPlace). scope, when given, is where the heal
   * runs instead of the page: inside the iframe or the shadow root the key was last found in
   * (see scope.ts). A key last found inside a shadow root is never healed without that
   * shadow root's scope. The key of a getBy* call is healed from its fingerprint only (see
   * healGetBy). */
  async tryHeal(key: string, meta: KeyMeta, broken: string[], context: FlowContext,
                refusal: Refusal | null = null, scope: HealScope | null = null): Promise<string | false> {
    if (meta.destructive) return this.decline(key, destructiveMessage(key), 'warning', null, 'destructive');
    const moved = this.flowChange(key, context);
    if (moved) return this.decline(key, moved, 'warning', null, 'page_guard');
    if (refusal) return this.decline(key, refusal.reason, 'warning', null, this.placeCode(refusal.code));
    const outside = this.outsideShadow(key, scope);
    if (outside) return this.decline(key, outside, 'warning', null, this.placeCode('shadow'));
    if (isKey(key)) return this.healGetBy(key, meta, broken, scope);
    const driver: HealDriver = scope ? scope.driver : this.driver;
    const frame = scope && scope.kind === 'frame' ? scope.frame : null;
    const hosts = scope && scope.kind === 'shadow' ? scope.hosts : null;
    const role = meta.expectedRole;
    const roleUnknown = !role || role === 'unknown';
    const unavailableRaw = (this.provider as { unavailableReason?: unknown }).unavailableReason;
    let unavailable = typeof unavailableRaw === 'string' && unavailableRaw ? unavailableRaw : null;
    const summaryRaw = (this.provider as { unavailableSummary?: unknown }).unavailableSummary;
    const summary = typeof summaryRaw === 'string' ? summaryRaw : null;
    if (unavailable && (roleUnknown || !this.fingerprint)) {
      if (roleUnknown) unavailable = unavailable.trimEnd() + ' Also, mendwright ' + roleNeeded(key);
      return this.decline(key, unavailable, 'warning', summary, this.unavailableCode());
    }
    if (roleUnknown) return this.decline(key, 'mendwright ' + roleNeeded(key), 'warning', null, 'role_unknown');
    if (!this.guard()) return this.decline(key, GUARD_REASON, 'warning', null, 'guard');
    const started = this.clock();
    let html: string | null = null;
    let requested = false;
    const source = !broken.length && meta.intent ? 'intent' : 'model';
    if (source === 'intent' && unavailable) {
      return this.decline(key, "'" + key + "' has no selector yet, and only a model can find it from intent, but no model "
        + 'provider is configured. ' + unavailable,
      'warning', summary ? "'" + key + "' has no selector yet, and " + summary : null, this.unavailableCode());
    }
    if (this.fingerprint && source !== 'intent') {
      html = await driver.content();
      const [applied, note, asked] = await this.fingerprintTier(key, meta, broken, html, started, scope);
      requested = asked;
      if (applied !== null) return applied;
      if (unavailable) {
        return this.decline(key, note + ', and no model provider is configured to try instead. ' + unavailable,
          'warning', summary ? note + ', and ' + summary : null, this.unavailableCode());
      }
    }
    if (this.guardrails && !this.guardrails.allow(key)) {
      this.journal.event('heal.rejected', { key, reason: 'rate/spend cap' });
      return this.decline(key, "the hourly rate cap for '" + key + "' was reached, so mendwright stopped (fail-closed). "
        + 'Wait for the hourly window to reset, or raise the cap with open({ ratePerKeyPerHour, rateGlobalPerHour }).',
      'warning', null, 'rate_limit');
    }
    const tag = source !== 'model' ? source : null;
    if (!requested) this.journal.event('heal.requested', { key });
    if (html === null) html = await driver.content();
    const wants = (this.provider as HealProvider).wantsFingerprint === true;
    // The model reads a cleaned, redacted copy of the page; the gate checks the page as captured.
    // A page that cannot be cleaned is not sent at all: as captured, it has its scripts and
    // styles, and the state a script keeps inline (fail closed).
    let read: unknown;
    try {
      read = await driver.lib<string>('cleanHtml', redact(html));
    } catch (error) {
      read = error;
    }
    if (typeof read !== 'string') {
      this.journal.event('heal.rejected', { key, reason: 'page not cleaned' });
      return this.decline(key, notCleaned(key, read), 'warning', null, 'other');
    }
    const cleaned = redact(read);
    // A model that reads a prompt sees at most MAX_PROMPT_PART characters of the page. When
    // the end was cut off, positions there no longer match the live page.
    const cut = !wants && [...cleaned].length > MAX_PROMPT_PART;
    let bundle: Bundle = buildBundle(key, meta, broken, await driver.inventory(), cleaned, context, wants);
    let reason = 'no proposal';
    for (let attempt = 0; attempt < 2; attempt++) {
      let proposal: Proposal | null = null;
      try {
        proposal = await this.provider.propose(bundle, this.tools(driver));
      } catch (error) {
        const detail = (error as Error)?.message ?? String(error);
        this.journal.event('heal.rejected', { key, reason: 'no proposal', error: detail });
        return this.decline(key, providerName(this.provider) + " failed for '" + key + "': " + detail,
          'warning', null, 'no_proposal');
      }
      if (!proposal || typeof proposal !== 'object') {
        const error = this.providerError();
        if (error) {
          this.journal.event('heal.rejected', { key, reason: 'no proposal', error });
          // The model found no element on this page that fits, or was not sure which: the
          // providers end that sentence with NOT_HERE_HINT, so the code does not depend on its wording.
          return this.decline(key, providerName(this.provider) + " returned no selector for '" + key + "': " + error,
            'warning', null, error.includes(NOT_HERE_HINT) ? 'not_here' : 'no_proposal');
        }
        this.journal.event('heal.rejected', { key, reason: 'no proposal' });
        return this.decline(key, "the model returned no usable selector for '" + key + "'. Try a different or stronger "
          + 'model, or repair the selector by hand.', 'warning', null, 'no_proposal');
      }
      this.journal.event('heal.proposed', { key, xpath: proposal.xpath, reasoning: proposal.reasoning, source: tag });
      if (this.dryRun) {
        let ok = false;
        for (const xpath of [proposal.xpath, ...(proposal.fallbacks ?? [])]) {
          if (!xpath || (cut && hasPosition(xpath))) continue;
          if ((await validateStatic(driver, xpath, html, meta.expectedRole,
            quarantinedBodies(this.store, key, frame ?? '', hosts ?? []))).ok) {
            ok = true;
            break;
          }
        }
        this.emit('info', '[heal dry-run] key=' + key + ' proposed=' + proposal.xpath + ' valid=' + ok + ' (not applied)');
        return this.decline(key, "dry-run mode is on, so the proposal for '" + key + "' was checked but not applied. "
          + 'Turn off dryRun to let mendwright apply and retry.', 'info', null, 'dry_run');
      }
      const [applied, why] = await apply(this.store, key, proposal, html, meta, driver, !cut, frame, hosts);
      reason = why;
      if (applied) {
        this.recordApplied(key, broken, applied, started, providerName(this.provider), this.providerModel(), source,
          hosts);
        return applied;
      }
      this.journal.event('heal.rejected', { key, xpath: proposal.xpath, reason, source: tag });
      bundle = { ...bundle, rejectedXpath: proposal.xpath, rejectReason: reason };
    }
    this.decline(key, "no selector the model proposed for '" + key + "' passed mendwright's local gate (last reason: "
      + reason + '). The gate requires a non-brittle xpath that matches exactly one element of the expected role and '
      + 'is present on the live page.', 'warning', null, 'gate');
    return false;
  }

  /** tryHeal() for the key of a heal.page.getBy*() call (see getby.ts), after the checks for a
   * key marked destructive, the page and the shadow root: the fingerprint tier only (the
   * Python package's _heal_get_by). A call names its element by role and words, and when it
   * breaks those are what changed, so a model given the old call could only guess. The
   * fingerprint holds what survives a redesign (ids, test ids, names, classes, ancestors), and
   * the tier needs a strong sign of identity and a margin before it proposes anything. So no
   * provider is asked, no page leaves the machine, and the rate limit is not used.
   *
   * Declined, in order: MENDWRIGHT_DISABLE=1, the fingerprint tier off, no fingerprint in the
   * store (a call that never matched is more often a mistake in the call than a redesign), an
   * unknown role, a guard that blocks; then whatever the tier itself does not heal. Its pick
   * goes through apply(), whose live check also asks Playwright for the ARIA role of a
   * getByRole() call (gate.liveReason). */
  private async healGetBy(key: string, meta: KeyMeta, broken: string[], scope: HealScope | null):
      Promise<string | false> {
    if (this.disabled) return this.decline(key, DISABLED_REASON, 'warning', null, 'disabled');
    if (!this.fingerprint) return this.decline(key, tierOffMessage(key), 'warning', null, 'get_by');
    if (!validFingerprint(meta.fingerprint)) return this.decline(key, noFingerprintMessage(key), 'warning', null, 'get_by');
    const role = meta.expectedRole;
    if (!role || role === 'unknown') return this.decline(key, roleUnknownMessage(key), 'warning', null, 'role_unknown');
    if (!this.guard()) return this.decline(key, GUARD_REASON, 'warning', null, 'guard');
    const driver: HealDriver = scope ? scope.driver : this.driver;
    const html = await driver.content();
    const [applied, note] = await this.fingerprintTier(key, meta, broken, html, this.clock(), scope);
    if (applied !== null) return applied;   // the heal, or false after the dry run declined it
    return this.decline(key, note + '. ' + NO_MODEL, 'warning', null, 'get_by');
  }

  /** The first tier: [applied selector, or false when dry run declined it, or null to go on
   * to the model; why it did not heal; whether heal.requested was journaled]. scope, when
   * given, is the iframe or the shadow root whose elements it scores and where it saves the
   * heal. Inside a shadow root it scores the elements of html, that shadow root's HTML. */
  private async fingerprintTier(key: string, meta: KeyMeta, broken: string[], html: string, started: number,
                                scope: HealScope | null): Promise<[string | false | null, string, boolean]> {
    const driver: HealDriver = scope ? scope.driver : this.driver;
    const frame = scope && scope.kind === 'frame' ? scope.frame : null;
    const hosts = scope && scope.kind === 'shadow' ? scope.hosts : null;
    const stored = meta.fingerprint;
    if (!validFingerprint(stored)) return [null, noFingerprint(key), false];
    this.journal.event('heal.requested', { key });
    const match = await findMatch(stored, meta.expectedRole, driver, hosts
      ? { html, visible: (xpath) => shadowVisible(driver, html, xpath) } : {});
    if (!match.xpath) {
      return [null, "the fingerprint match for '" + key + "' was not confident: " + match.reason, true];
    }
    const reasoning = 'fingerprint score ' + fmt(match.score) + ', next best ' + fmt(match.runnerUp);
    this.journal.event('heal.proposed', { key, xpath: match.xpath, reasoning, source: FINGERPRINT_PROVIDER });
    if (this.dryRun) {
      const ok = (await validateStatic(driver, match.xpath, html, meta.expectedRole,
        quarantinedBodies(this.store, key, frame ?? '', hosts ?? []))).ok;
      this.emit('info', '[heal dry-run] key=' + key + ' proposed=' + match.xpath + ' (fingerprint) valid=' + ok + ' (not applied)');
      return [this.decline(key, "dry-run mode is on, so the fingerprint match for '" + key + "' was checked but not "
        + 'applied. Turn off dryRun to let mendwright apply and retry.', 'info', null, 'dry_run'), '', true];
    }
    const [applied, reason] = await apply(this.store, key, { xpath: match.xpath, fallbacks: [], reasoning }, html, meta,
      driver, true, frame, hosts);
    if (applied) {
      this.recordApplied(key, broken, applied, started, FINGERPRINT_PROVIDER, null, FINGERPRINT_PROVIDER, hosts);
      return [applied, '', true];
    }
    this.journal.event('heal.rejected', { key, xpath: match.xpath, reason, source: FINGERPRINT_PROVIDER });
    return [null, "the fingerprint match for '" + key + "', " + match.xpath + ", failed mendwright's checks (" + reason + ')', true];
  }

  /** Journal, remember and log a heal that was applied. hosts are those of the shadow root it
   * was applied in, which heal.applied gets in shadow, and the line names. */
  private recordApplied(key: string, broken: string[], applied: string, started: number, provider: string,
                        model: string | null, source: string, hosts: string[] | null = null): void {
    const seconds = Math.max(0, (this.clock() - started) / 1000);
    this.journal.event('heal.applied', { key, xpath: applied, old: broken, provider, model,
                                         latency_ms: Math.round(seconds * 1000), source: source !== 'model' ? source : null,
                                         ...(hosts && hosts.length ? { shadow: [...hosts] } : {}) });
    this.healReasons.delete(key);
    this.heals.push({ key, old: [...broken], new: applied, confirmed: null, source });
    const timing = ' (' + provider + ', ' + seconds.toFixed(1) + 's)';
    if (source === 'intent') {
      this.emit('warning', 'found ' + pyRepr(key) + ' from its intent: ' + applied + timing);
      return;
    }
    const was = !broken.length ? '(no stored selector)' : broken.length === 1 ? broken[0]
      : broken[0] + ' (+' + (broken.length - 1) + ' more)';
    const where = hosts && hosts.length ? ' inside ' + placeText(hosts) : '';
    this.emit('warning', 'healed ' + pyRepr(key) + where + ': ' + was + ' -> ' + applied + timing);
  }

  /** Journal, remember and log that the key's own selector old matched only inside the open
   * shadow root that hosts lead to, and is saved there as alt from now on, the chain of the
   * hosts and a CSS selector (the Python package's record_shadow). It counts as a heal with
   * source 'shadow': strict mode fails on it, and the canary checks it. */
  recordShadow(key: string, old: string, alt: string, hosts: string[]): void {
    this.journal.event('heal.applied', { key, xpath: alt, old: [old], provider: SHADOW_PROVIDER, model: null,
                                         latency_ms: 0, source: SHADOW_PROVIDER, shadow: [...hosts] });
    this.healReasons.delete(key);
    this.heals.push({ key, old: [old], new: alt, confirmed: null, source: SHADOW_PROVIDER });
    this.emit('warning', 'found ' + pyRepr(key) + ' inside ' + placeText(hosts) + ': ' + old + ' matches nothing on the '
      + 'page, so it is looked up there as ' + alt + ' from now on');
  }

  /** Journal, remember and log that the key's own selector old matched only inside the iframe
   * frame, and is saved there as alt from now on (the Python package's record_frame). It counts
   * as a heal with source 'frame': strict mode fails on it, and the canary checks it. */
  recordFrame(key: string, old: string, alt: string, frame: string): void {
    this.journal.event('heal.applied', { key, xpath: alt, old: [old], provider: FRAME_PROVIDER, model: null,
                                         latency_ms: 0, source: FRAME_PROVIDER, frame });
    this.healReasons.delete(key);
    this.heals.push({ key, old: [old], new: alt, confirmed: null, source: FRAME_PROVIDER });
    this.emit('warning', 'found ' + pyRepr(key) + ' inside the iframe ' + frame + ': ' + old + ' matches nothing on the '
      + 'page, so it is looked up in that iframe from now on');
  }

  private providerModel(): string | null {
    const model = (this.provider as { model?: unknown }).model;
    return typeof model === 'string' && model ? model : null;
  }

  private providerError(): string {
    const error = (this.provider as { lastError?: unknown }).lastError;
    return typeof error === 'string' && error.trim() ? error.trim() : '';
  }

  /** The canary: report a heal's outcome, and quarantine a heal whose check failed. A heal
   * from the fingerprint tier, or a key followed into an iframe or a shadow root, is not
   * reported to the provider, which did not propose it. by says what decided it ('test',
   * 'expect', 'confirm' or 'action'). */
  confirm(key: string, xpath: string, success: boolean, options: { reason?: string; by?: string; test?: string } = {}): void {
    const heal = [...this.heals].reverse().find((h) => h.key === key && h.new === xpath && h.confirmed === null);
    if (!heal || ![FINGERPRINT_PROVIDER, FRAME_PROVIDER, SHADOW_PROVIDER].includes(heal.source)) {
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
 * Re-raises the miss with the reason when nothing was healed. refusal and scope are
 * tryHeal()'s. */
export async function resolveWithHeal<T>(resolveFn: () => Promise<T>, coordinator: HealCoordinator, key: string,
                                         context: FlowContext, missed: AttentionRequired,
                                         refusal: Refusal | null = null, scope: HealScope | null = null): Promise<T> {
  const store = coordinator.store;
  const broken = store.candidates(key).map((c) => c.xpath);
  const applied = await coordinator.tryHeal(key, store.metaOf(key), broken, context, refusal, scope);
  if (!applied) {
    const reason = coordinator.explain(key);
    if (reason) throw new AttentionRequired(missed.message.trimEnd() + '\n  -> ' + reason);
    throw missed;
  }
  coordinator.pending.set(key, applied);
  return resolveFn();
}
