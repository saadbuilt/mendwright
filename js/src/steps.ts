// Steps in any order (Python's mendwright/steps.py): a flow (a checkout, a signup, a booking)
// whose steps the site may show in another order, put on one page, or leave out, run without
// a change to the script.
//
//   heal.step('shipping', { when: '#ship-name' }, async (page) => {
//     await page.fill('#ship-name', 'Ada');
//     await page.click('#next');
//   });
//   heal.step('payment', { when: '#card-number' }, async (page) => { ... });
//   heal.step('review', { when: 'text=Place order', last: true }, (page) => page.click('text=Place order'));
//   await heal.runSteps();   // ['shipping', 'payment', 'review'], or in the site's order
//
// heal.step(name, { when, url, last, optional }, fn) registers a step: what shows it is on the
// page, and the function that does it. when is a landmark, one selector or several, and any
// visible match shows the step; url is a pattern for the page's path (see urlPattern()),
// instead of a landmark or as one more condition. runSteps() then waits for a step that has
// not run yet to show, runs it, and looks again, until the step marked last: true ran, or,
// with no last step, until every step that is not optional ran. Each step runs at most once.
// Steps that show at the same time (a one-page checkout) run in the order they were
// registered, and the last step only once no other step that has not run shows.
//
// This module holds what needs no page: the arguments, the url patterns, the landmarks'
// targets, the messages, and what one runSteps() call knows and decides (StepRun). The healer
// (healer.ts) looks at the page, handles the interruptions while it waits, and runs the step
// functions.
//
// What mendwright does not guess:
// - A landmark is looked up with count() and isVisible() only. It is never healed and nothing
//   is saved for it: a landmark that is missing is how a step that is not on the page looks.
// - A step that ran and is on the page again, with no new step for the whole timeout, stops
//   the flow with AttentionRequired: it is never run twice. A page where no step shows stops
//   it too, and the message names the page and every landmark runSteps() looked for.
// - runSteps() never runs more steps than 3 times the number registered when it started.

import type { Page } from '@playwright/test';

import { AttentionRequired, InvalidSelector } from './errors';
import { errorType, isId, listed, pagePath, valueText } from './flow';
import { nthTargets } from './interruptions';
import type { JournalLike } from './journal';
import type { LogLevel } from './log';
import { pyFixed, pyFormatG, pyRepr, pyRound, pyStrip } from './py';
import { XPATH, checkSyntax, classify, normalize, playwrightSelector } from './selectors';

/** What an interruption handled while runSteps() waits is logged as handled before: no step
 * shows yet, so there is no step to name. The angle brackets keep it from reading as a key. */
export const WAITING_STEP = '<next step>';

/** runSteps() runs at most this many times as many steps as were registered when it started
 * (each step runs once; this stops a step function that keeps registering new ones). */
export const MAX_RUNS_PER_STEP = 3;

/** How many matches of a landmark that matches several elements are looked at for one that is
 * visible. */
export const LANDMARK_MATCHES = 5;

/** The function that does a step: it gets heal.page and returns a promise, as an async
 * function does. */
export type StepFunction = (page: Page) => PromiseLike<unknown>;

/** What heal.step(name, options, fn) takes besides the name and the function. */
export interface StepOptions {
  /** What shows the step is on the page, its landmark: one selector or a list, and any visible
   * match counts. It is never healed, so pick one that stays, such as a heading's text. */
  when?: string | string[] | null;
  /** A pattern for the page's path, such as '/checkout/payment' or '*\/payment*': instead of
   * when, or as one more condition with it. */
  url?: string | null;
  /** The step that ends the flow, such as placing the order. A flow has at most one. */
  last?: boolean;
  /** A step that may not show at all, such as an upsell. */
  optional?: boolean;
}

/** What heal.runSteps() takes. */
export interface RunStepsOptions {
  /** How long to wait for a step that has not run to show, in ms. Default: the healer's
   * timeoutMs. */
  timeoutMs?: number | null;
  /** How often to look, in ms (at most every 20 ms). Default: 200. */
  pollMs?: number;
}

/** One step registered with heal.step(): its name, its function, its landmarks (normalized
 * selectors), its url pattern (normalized, see urlPattern()) and that pattern as written in
 * urlText, whether it ends the flow (last) and whether it may not show at all (optional). */
export interface Step {
  readonly name: string;
  readonly fn: StepFunction;
  readonly when: readonly string[];
  readonly url: string | null;
  readonly urlText: string | null;
  readonly last: boolean;
  readonly optional: boolean;
}

// --- the arguments of heal.step() and runSteps() ----------------------------------------------

const STEP_OPTIONS = ['when', 'url', 'last', 'optional'];
const RUN_OPTIONS = ['timeoutMs', 'pollMs'];
const EXAMPLE = "{ when: '#card-number' }, async (page) => { ... }";

function isOptions(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

/** The keys of options that are not in known, as a message lists them. */
function unknownOptions(options: Record<string, unknown>, known: string[]): string {
  return listed(Object.keys(options).filter((k) => !known.includes(k))
    .map((k) => (/^[A-Za-z_$][\w$]*$/.test(k) ? k : pyRepr(k))));
}

function flag(name: string, value: unknown, option: string): boolean {
  if (value === undefined) return false;
  if (typeof value !== 'boolean') {
    // A string such as 'no' would read as true.
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', ...) takes ' + option + ': true or ' + option
      + ': false, got ' + option + ': ' + valueText(value) + '.');
  }
  return value;
}

/** The path pattern of step name's url, normalized as flow.pagePath() normalizes a page's
 * path: lower case, without empty parts, with parts that look like record ids as '*'. '*'
 * matches any characters, '/' included, and '?' one. A pattern that starts with '/' starts at
 * the root of the path; one that starts with '*' is kept as written, so '*\/payment' matches
 * /payment and /checkout/payment. Throws TypeError for a pattern that is not a string, that is
 * not a path (it must start with '/' or '*'), or that names the site root, which has no page. */
export function urlPattern(name: string, url: unknown): string {
  if (typeof url !== 'string') {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ", { url }) takes the path of the step's page as a "
      + "string, such as '/checkout/payment', got " + valueText(url) + '.');
  }
  const text = pyStrip(url);
  if (!text.startsWith('/') && !text.startsWith('*')) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', { url: ' + pyRepr(url) + " }) takes the path of the "
      + "step's page, starting with / or *, such as '/checkout/payment' or '*/payment', not a whole address.");
  }
  const parts = text.toLowerCase().split('/').filter(Boolean).map((part) => (isId(part) ? '*' : part));
  if (!parts.length) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', { url: ' + pyRepr(url) + ' }) names the site root, '
      + 'which has no page path to compare. Use when for a step on the site root.');
  }
  return (text.startsWith('*') ? '' : '/') + parts.join('/');
}

// fnmatch.fnmatchcase() of Python 3.12, as a regular expression of code points: '*' any
// characters, '?' one, [seq] one of seq and [!seq] one not in it (with ranges such as a-z), and
// a '[' that is not closed is itself. Built once per pattern.
const PATTERNS = new Map<string, RegExp>();
const STAR = Symbol('star');
// What a character outside a class is written as: escaped when it means something there.
const SPECIAL = new Set([...'^$\\.*+?()[]{}|/']);
// What a character inside a class is written as: escaped when it means something there.
const CLASS_SPECIAL = new Set([...'\\]-[^']);

function literal(c: string, special: Set<string>): string {
  return special.has(c) ? '\\' + c : c;
}

/** The regular expression of fnmatch's [seq], from pat[i:j] (the text between [ and ]), as
 * Python's fnmatch.translate() reads it: '!' first negates it, a '-' between two characters
 * is a range, any other '-' is itself, and a range that goes down is left out. */
function charClass(pat: string[], i: number, j: number): string {
  let chunks: string[][];
  if (!pat.slice(i, j).includes('-')) {
    chunks = [pat.slice(i, j)];
  } else {
    chunks = [];
    let k = pat[i] === '!' ? i + 2 : i + 1;
    for (;;) {
      k = pat.indexOf('-', k);
      if (k < 0 || k >= j) break;
      chunks.push(pat.slice(i, k));
      i = k + 1;
      k += 3;
    }
    const chunk = pat.slice(i, j);
    if (chunk.length) chunks.push(chunk);
    else chunks[chunks.length - 1].push('-');
    for (let n = chunks.length - 1; n > 0; n--) {
      const before = chunks[n - 1];
      if (before[before.length - 1].codePointAt(0)! > chunks[n][0].codePointAt(0)!) {
        chunks[n - 1] = [...before.slice(0, -1), ...chunks[n].slice(1)];
        chunks.splice(n, 1);
      }
    }
  }
  if (chunks.length === 1 && !chunks[0].length) return '(?!)';   // an empty range: never matches
  const negated = chunks[0][0] === '!';
  if (negated) chunks[0] = chunks[0].slice(1);
  if (negated && chunks.length === 1 && !chunks[0].length) return '.';   // [!] of an empty range: anything
  return '[' + (negated ? '^' : '') + chunks.map((c) => c.map((ch) => literal(ch, CLASS_SPECIAL)).join('')).join('-') + ']';
}

function fnmatchRegex(pattern: string): RegExp {
  let regex = PATTERNS.get(pattern);
  if (regex) return regex;
  const pat = Array.from(pattern);
  const parts: (string | typeof STAR)[] = [];
  let i = 0;
  while (i < pat.length) {
    const c = pat[i++];
    if (c === '*') {
      if (parts[parts.length - 1] !== STAR) parts.push(STAR);
    } else if (c === '?') {
      parts.push('.');
    } else if (c === '[') {
      let j = i;
      if (j < pat.length && pat[j] === '!') j++;
      if (j < pat.length && pat[j] === ']') j++;
      while (j < pat.length && pat[j] !== ']') j++;
      if (j >= pat.length) {
        parts.push('\\[');
      } else {
        parts.push(charClass(pat, i, j));
        i = j + 1;
      }
    } else {
      parts.push(literal(c, SPECIAL));
    }
  }
  // A star between fixed parts matches as little as it can, and is not tried again: each
  // fixed part has one character per position, so its first place is the one to take.
  let source = '';
  let n = 0;
  while (n < parts.length && parts[n] !== STAR) source += parts[n++] as string;
  let group = 0;
  while (n < parts.length) {
    n++;   // the star
    let fixed = '';
    while (n < parts.length && parts[n] !== STAR) fixed += parts[n++] as string;
    if (n === parts.length) {
      source += '.*' + fixed;
    } else {
      group += 1;
      source += '(?=(.*?' + fixed + '))\\' + group;
    }
  }
  regex = new RegExp('^(?:' + source + ')$', 'su');
  if (PATTERNS.size >= 256) PATTERNS.clear();
  PATTERNS.set(pattern, regex);
  return regex;
}

/** Whether the page at url is on the step's url pattern (see urlPattern()). Never for a page
 * with no path (about:blank, a data: URL, the site root). */
export function urlMatches(pattern: string, url: unknown): boolean {
  const path = pagePath(url);
  return path !== null && fnmatchRegex(pattern).test(path);
}

/** The landmark selectors of when, checked and normalized, without the ones given twice. */
function landmarks(name: string, when: unknown): string[] {
  if (when === undefined || when === null) return [];
  const items = Array.isArray(when) ? when : [when];
  if (!items.length) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', { when: [] }) got an empty list. Pass the selector '
      + "of something only that step shows, for example '#card-number'.");
  }
  const out: string[] = [];
  for (const item of items) {
    try {
      checkSyntax(item);
    } catch (error) {
      if (error instanceof InvalidSelector) throw error.forKey(name);
      throw error;
    }
    out.push(normalize(item as string));
  }
  return [...new Set(out)];
}

/** Check heal.step(name, options, fn) and build its Step, before anything touches the page.
 * Throws TypeError, or InvalidSelector for a landmark that can never work, each naming the
 * fix. An XPath landmark the browser cannot parse throws InvalidSelector from runSteps(),
 * when it is first looked at. */
export function makeStep(name: unknown, options: unknown, fn: unknown): Step {
  if (typeof name !== 'string' || !pyStrip(name)) {
    throw new TypeError("mendwright: heal.step() needs a name, a non-empty string such as 'payment', got "
      + valueText(name) + '.');
  }
  if (!isOptions(options)) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', ...) takes its options as an object and the function '
      + 'after them, such as heal.step(' + pyRepr(name) + ', ' + EXAMPLE + '), got ' + valueText(options) + '.');
  }
  const unknown = unknownOptions(options, STEP_OPTIONS);
  if (unknown) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', ...) got ' + unknown + ', which it does not take. '
      + 'Its options are when, url, last and optional, and the function comes after them: heal.step(' + pyRepr(name)
      + ', ' + EXAMPLE + ').');
  }
  const last = flag(name, options.last, 'last');
  const optional = flag(name, options.optional, 'optional');
  if (last && optional) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', { last: true, optional: true }): the last step ends '
      + 'the flow, so it cannot be optional. Drop optional: true.');
  }
  const when = landmarks(name, options.when);
  const url = options.url === undefined || options.url === null ? null : urlPattern(name, options.url);
  if (!when.length && url === null) {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ') needs when, a selector of something only that step '
      + 'shows (a heading, a field), or url, the path of its page: heal.step(' + pyRepr(name) + ", { when: '#card-number' }, "
      + 'fn) or heal.step(' + pyRepr(name) + ", { url: '/checkout/payment' }, fn).");
  }
  if (typeof fn !== 'function') {
    throw new TypeError('mendwright: heal.step(' + pyRepr(name) + ', ...) takes the function that does the step, got '
      + valueText(fn) + '. Pass it after the options: heal.step(' + pyRepr(name) + ', ' + EXAMPLE + ').');
  }
  return {
    name, fn: fn as StepFunction, when, url, urlText: url === null ? null : pyStrip(options.url as string), last,
    optional,
  };
}

/** Put step in book (by name, in the order registered). A name registered again is replaced
 * and keeps its place. Throws TypeError for a second last step. */
export function registerStep(book: Map<string, Step>, step: Step): void {
  if (step.last) {
    for (const other of book.values()) {
      if (other.last && other.name !== step.name) {
        throw new TypeError('mendwright: heal.step(' + pyRepr(step.name) + ', { last: true }): ' + pyRepr(other.name)
          + ' is the last step already, and a flow has at most one. Steps stay on the healer for every later runSteps(), '
          + 'so for another flow, open a new healer. To change the last step of this flow, register '
          + pyRepr(other.name) + ' again without last: true first, or drop last: true here.');
      }
    }
  }
  book.set(step.name, step);
}

/** Check runSteps()'s options and the steps registered: [timeoutMs, pollMs]. Throws TypeError
 * before anything touches the page. timeoutMs left out, or null, is defaultMs. */
export function runArgs(book: Map<string, Step>, options: unknown, defaultMs: number): [number, number] {
  if (!isOptions(options)) {
    throw new TypeError('mendwright: runSteps() takes its options as an object, such as { timeoutMs: 5000 }, got '
      + valueText(options) + '.');
  }
  const unknown = unknownOptions(options, RUN_OPTIONS);
  if (unknown) {
    throw new TypeError('mendwright: runSteps() got ' + unknown + ', which it does not take. Its options are timeoutMs '
      + 'and pollMs.');
  }
  const timeoutMs = options.timeoutMs ?? defaultMs;
  const pollMs = options.pollMs === undefined ? 200 : options.pollMs;
  for (const [label, value] of [['timeoutMs', timeoutMs], ['pollMs', pollMs]] as const) {
    if (typeof value !== 'number' || Number.isNaN(value)) {
      throw new TypeError('mendwright: runSteps() takes ' + label + ' as a number of milliseconds, got ' + label + ': '
        + valueText(value) + '.');
    }
    if (value < 0) {
      throw new TypeError('mendwright: runSteps() takes ' + label + ' of 0 or more, got ' + label + ': ' + valueText(value)
        + '.');
    }
  }
  if (!book.size) {
    throw new TypeError('mendwright: runSteps() has no steps to run. Register them first with heal.step(), for example '
      + "heal.step('payment', " + EXAMPLE + ').');
  }
  const steps = [...book.values()];
  if (!steps.some((s) => s.last) && steps.every((s) => s.optional)) {
    throw new TypeError('mendwright: every step is optional and none has last: true, so runSteps() would have nothing '
      + 'to wait for. Mark the step that ends the flow with last: true.');
  }
  return [timeoutMs as number, pollMs as number];
}

// --- looking at the page -----------------------------------------------------------------------

/** The selectors to ask isVisible() of for a landmark that matches count elements: the
 * landmark itself for one match; for several, one per match of the first LANDMARK_MATCHES, as
 * '(xpath)[n]' for an XPath and 'selector >> nth=n' for any other. */
export function landmarkTargets(selector: string, count: number): string[] {
  if (count === 1) return [selector];
  if (count < 1) return [];
  if (classify(selector) === XPATH) return nthTargets(selector, count, LANDMARK_MATCHES);
  const base = playwrightSelector(selector);
  const out: string[] = [];
  for (let i = 0; i < Math.min(count, LANDMARK_MATCHES); i++) out.push(base + ' >> nth=' + i);
  return out;
}

/** What shows() looks at the page with: the driver's count() and isVisible(). */
export interface LandmarkDriver {
  count(selector: string): Promise<number>;
  isVisible(selector: string): Promise<boolean>;
}

/** Called for a step that does not show, for its first landmark that matches count elements of
 * which only the first looked were looked at, none visible (the healer names it in one WARNING
 * per page). */
export type SeveralLandmark = (name: string, selector: string, count: number, looked: number) => unknown;

/** Whether step shows on the page now: its url pattern matches the page's path (when it has
 * one), and one of its landmarks has a visible match (when it has any). Any error but
 * InvalidSelector reads as not there; InvalidSelector is thrown, naming the step. several,
 * when given, hears of a landmark that matches more elements than were looked at, when the
 * step does not show; a landmark after it that shows the step leaves it unnamed. */
export async function shows(driver: LandmarkDriver, step: Step, url: string,
                            several: SeveralLandmark | null = null): Promise<boolean> {
  if (step.url !== null && !urlMatches(step.url, url)) return false;
  if (!step.when.length) return true;
  let ambiguous: [string, number, number] | null = null;
  for (const selector of step.when) {
    try {
      const count = await driver.count(selector);
      const targets = landmarkTargets(selector, count);
      for (const target of targets) {
        if (await driver.isVisible(target)) return true;
      }
      if (count > targets.length && ambiguous === null) ambiguous = [selector, count, targets.length];
    } catch (error) {
      if (error instanceof InvalidSelector) throw error.forKey(step.name);
    }
  }
  if (several && ambiguous) {
    try {
      await several(step.name, ...ambiguous);
    } catch {
      // a WARNING that cannot be written never stops the wait
    }
  }
  return false;
}

// --- messages ------------------------------------------------------------------------------------

// What a message tells you to write for a step the site added.
const ADD_STEP = 'heal.step("<name>", { when: "<selector of something only that step shows>" }, fn)';

function onPage(page: string | null): string {
  return page ? page : 'this page';
}

/** The INFO line for a step runSteps() ran. */
export function ranMessage(name: string, page: string | null, seconds: number): string {
  return 'ran step ' + pyRepr(name) + (page ? ' on ' + page : '') + ' (' + pyFixed(seconds, 1) + 's)';
}

/** The WARNING for step name, which runs after the steps in earlier although it was
 * registered before them. */
export function orderMessage(earlier: string[], name: string): string {
  return 'ran ' + listed(earlier.map(pyRepr)) + ' before ' + pyRepr(name) + ': the site changed the order of the steps';
}

/** The WARNING for a step that is not optional and never showed before the last step ran. */
export function droppedMessage(name: string, last: string | null): string {
  return pyRepr(name) + ' never showed before the flow ended with ' + pyRepr(last) + ': the site may have dropped it. '
    + 'If it did, delete its heal.step(). If it shows only sometimes, pass optional: true to heal.step(' + pyRepr(name)
    + ', ...).';
}

/** The INFO line for an optional step that never showed. */
export function notShownMessage(name: string): string {
  return 'step ' + pyRepr(name) + ' did not show, and it is optional (optional: true)';
}

function landmarkText(step: Step): string {
  const parts = [...step.when];
  if (step.urlText) parts.push('url ' + step.urlText);
  return pyRepr(step.name) + ' (' + parts.join(', ') + ')';
}

function ranText(ran: string[]): string {
  return ran.length ? 'Steps that ran: ' + ran.map(pyRepr).join(', ') + '.' : 'No step ran yet.';
}

/** The AttentionRequired text for a step that ran and is on the page again, with no new step
 * for the whole timeout. */
export function againMessage(name: string, page: string | null, timeoutMs: number, ran: string[]): string {
  return 'mendwright: ' + pyRepr(name) + ' is on the page again after it ran, and no step that has not run showed on '
    + onPage(page) + ' within ' + pyFormatG(timeoutMs) + ' ms: its submit probably failed, for example because the card '
    + 'was declined or the site rejected a field. mendwright runs each step once, so it stopped instead of running '
    + pyRepr(name) + " again. Look at the page for the site's message. " + ranText(ran);
}

/** The AttentionRequired text for a step that ran on the page ranOn and shows on another page,
 * with no new step for the whole timeout: the site probably added a step that shows part of
 * it, such as its heading. */
export function changedMessage(name: string, ranOn: string, page: string, timeoutMs: number, ran: string[]): string {
  return 'mendwright: ' + pyRepr(name) + ' shows on ' + page + ', and it ran on ' + ranOn + ', so the page changed after '
    + 'it ran, and no step that has not run showed within ' + pyFormatG(timeoutMs) + ' ms. This is probably a step the '
    + 'site added that shows part of ' + pyRepr(name) + ', such as its heading. If it is, add it with heal.step(): '
    + ADD_STEP + ', or give ' + pyRepr(name) + ' a landmark only its own page shows. If its submit failed instead, look '
    + "at the page for the site's message. mendwright runs each step once, so it stopped instead of running "
    + pyRepr(name) + ' again. ' + ranText(ran);
}

/** The AttentionRequired text for a page where no step showed within the timeout. waiting are
 * the steps runSteps() looked for, ran the names of those that ran. noLast: steps ran and none
 * has last: true, so a step the site dropped is waited for. */
export function unknownMessage(page: string | null, waiting: Step[], ran: string[], timeoutMs: number,
                               noLast = false): string {
  return 'mendwright: no step showed on ' + onPage(page) + ' within ' + pyFormatG(timeoutMs) + ' ms. mendwright looked '
    + 'for ' + listed(waiting.map(landmarkText)) + '. ' + ranText(ran) + ' If the site added a step here, add it with '
    + 'heal.step(): ' + ADD_STEP + ". If a step's landmark changed, update its when option: landmarks are never healed."
    + (noLast ? ' No step has last: true, so runSteps() waits until every step that is not optional ran, and the site '
      + 'may have dropped one: mark the step that ends the flow with last: true, and the flow ends there, with a WARNING '
      + 'for a step that never showed.' : '');
}

/** The TypeError text for a step function that returned result, which is not a promise:
 * mendwright waits for the promise it returns before it goes on. */
export function plainMessage(name: string, result: unknown): string {
  return 'mendwright: the function of step ' + pyRepr(name) + ' returned ' + valueText(result) + ", not a promise. "
    + "heal.page's calls return promises, and a function that makes them without await does not wait for them, so "
    + 'mendwright cannot either: make it an async function and await them, or return the promise, as in '
    + "(page) => page.click('#place').";
}

/** The AttentionRequired text when runSteps() reached its limit of step runs. */
export function capMessage(count: number, registered: number): string {
  return 'mendwright: runSteps() ran ' + count + ' steps, ' + MAX_RUNS_PER_STEP + ' times the ' + registered
    + ' registered when it started, and stopped instead of running more. A step function that registers new steps with '
    + 'heal.step() can keep a flow from ending: register every step before runSteps().';
}

/** The INFO line for a key the page guard lets heal on a page it was never found on, because
 * it was found during the step that runs now, and this is the page that step showed on. */
export function scopeMessage(key: string, step: string, page: string, last: string | null): string {
  return 'the page guard lets ' + pyRepr(key) + ' heal on ' + page + ': it was found during step ' + pyRepr(step)
    + ' before' + (last ? ' (last on ' + last + ')' : '') + ', and this is the page ' + pyRepr(step) + ' showed on';
}

// --- one runSteps() call ---------------------------------------------------------------------------

/** Where a StepRun's lines and journal events go: the healer's coordinator. */
export interface StepOutput {
  emit(level: LogLevel, message: string): void;
  readonly journal: JournalLike;
}

/** One flow.* journal event, which never throws, as the Python package's journal_event(). */
function journalEvent(journal: JournalLike, name: string, fields: Record<string, unknown>): void {
  try {
    journal.event(name, fields);
  } catch {
    // a journal that cannot write never stops a flow
  }
}

/** What one runSteps() call knows and decides (Python's steps.Run): which steps ran, which to
 * run next, when the flow is done, and what it reports. book is the healer's, which a step
 * function may add to. */
export class StepRun {
  /** The names of the steps that ran, in order. */
  readonly ran: string[] = [];
  /** name -> the path of the page each step that ran showed on. */
  readonly ranOn = new Map<string, string | null>();
  /** The last step, once it ran. */
  ended: string | null = null;
  readonly cap: number;
  readonly registered: number;

  constructor(private readonly book: Map<string, Step>, private readonly out: StepOutput, readonly timeoutMs: number) {
    this.cap = MAX_RUNS_PER_STEP * book.size;
    this.registered = book.size;
  }

  steps(): Step[] {
    return [...this.book.values()];
  }

  /** True once the last step ran, or, with no last step, every step that is not optional ran. */
  done(): boolean {
    if (this.ended !== null) return true;
    const steps = this.steps();
    if (steps.some((s) => s.last)) return false;
    return steps.every((s) => s.optional || this.ran.includes(s.name));
  }

  /** Throws AttentionRequired, after its flow.stop event, once the flow ran as many steps as it
   * may. */
  checkCap(): void {
    if (this.ran.length >= this.cap) {
      this.stopped(WAITING_STEP, 'step_cap', { ran: this.ran.length });
      throw new AttentionRequired(capMessage(this.ran.length, this.registered));
    }
  }

  /** The flow.stop journal event, right before runSteps() throws because the flow cannot go
   * on: the code and the fields, never the message. */
  private stopped(key: string, code: string, fields: Record<string, unknown>): void {
    journalEvent(this.out.journal, 'flow.stop', { key, code, ...fields });
  }

  /** The steps that have not run, in the order registered. */
  waiting(): Step[] {
    return this.steps().filter((s) => !this.ran.includes(s.name));
  }

  /** The steps that ran and are still registered, most recent first. */
  ranSteps(): Step[] {
    return [...this.ran].reverse().filter((n) => this.book.has(n)).map((n) => this.book.get(n) as Step);
  }

  /** The step to run of those that show and have not run: the first one registered, the last
   * step only when no other shows. */
  static pick(shown: Step[]): Step {
    return shown.find((s) => !s.last) ?? shown[0];
  }

  /** The AttentionRequired for a wait that ran out, which the caller throws, after its
   * flow.stop journal event. again are the steps that ran and show now, most recent first. One
   * that shows on another page than the one it ran on is probably part of a step the site
   * added, not a submit that failed. */
  stuck(again: Step[], page: string | null): AttentionRequired {
    if (again.length) {
      const name = again[0].name;
      const ranOn = this.ranOn.get(name);
      if (ranOn && page && ranOn !== page) {
        this.stopped(name, 'step_moved', { page });
        return new AttentionRequired(changedMessage(name, ranOn, page, this.timeoutMs, this.ran));
      }
      this.stopped(name, 'step_again', { page });
      return new AttentionRequired(againMessage(name, page, this.timeoutMs, this.ran));
    }
    const noLast = this.ran.length > 0 && !this.steps().some((s) => s.last);
    this.stopped(WAITING_STEP, 'no_step', { page });
    return new AttentionRequired(unknownMessage(page, this.waiting(), this.ran, this.timeoutMs, noLast));
  }

  private position(name: string): number | null {
    const names = [...this.book.keys()];
    return names.includes(name) ? names.indexOf(name) + 1 : null;
  }

  /** Before step runs: the WARNING when steps registered after it ran already. Not for the last
   * step, which runs once no other step shows wherever it was registered. */
  starting(step: Step): void {
    const names = [...this.book.keys()];
    if (step.last || !names.includes(step.name)) return;
    const mine = names.indexOf(step.name);
    const earlier = this.ran.filter((n) => names.includes(n) && names.indexOf(n) > mine);
    if (earlier.length) this.out.emit('warning', orderMessage(earlier, step.name));
  }

  /** The INFO line and the flow.step journal event for one step that ran, or failed (only the
   * error's type is journaled: an error text can quote the page). */
  record(step: Step, page: string | null, seconds: number, failed = false, error?: unknown): void {
    this.ran.push(step.name);
    this.ranOn.set(step.name, page);
    if (step.last && !failed) this.ended = step.name;
    journalEvent(this.out.journal, 'flow.step', {
      key: step.name, position: this.ran.length, registered: this.position(step.name), page,
      seconds: pyRound(seconds, 3), outcome: failed ? 'failed' : 'ran', error: failed ? errorType(error) : null,
    });
    if (!failed) this.out.emit('info', ranMessage(step.name, page, seconds));
  }

  /** Once the flow is done: one WARNING for each step that is not optional and never showed
   * (only a last step can end the flow before they ran), one INFO line for each optional step
   * that did not show. Returns the names that ran, in order. */
  finish(): string[] {
    for (const step of this.waiting()) {
      if (step.optional) {
        this.out.emit('info', notShownMessage(step.name));
      } else {
        this.out.emit('warning', droppedMessage(step.name, this.ended));
        journalEvent(this.out.journal, 'flow.step', {
          key: step.name, registered: this.position(step.name), outcome: 'missing',
        });
      }
    }
    return [...this.ran];
  }
}
