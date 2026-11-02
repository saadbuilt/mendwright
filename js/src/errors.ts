import type { errors } from '@playwright/test';

import { pyRepr } from './py';

/** mendwright could not or would not heal a step. The message says why and what to do
 * next. Nothing was acted on through a selector that failed a check.
 *
 * A GetByNotFound, what a heal.page.getBy*() call throws when it cannot find or heal its
 * element, is an AttentionRequired too, though it is Playwright's TimeoutError: instanceof
 * AttentionRequired takes it. instanceof one of the subclasses, such as InterruptionLoop, is as
 * before. */
export class AttentionRequired extends Error {
  override name = 'AttentionRequired';

  static override [Symbol.hasInstance]<T>(this: abstract new (...args: any[]) => T, value: unknown): value is T {
    if ((this as unknown) === AttentionRequired && value instanceof GetByNotFound) return true;
    return Function.prototype[Symbol.hasInstance].call(this, value);
  }
}

/** A selector that can never match: it is not a string mendwright can store, it is empty,
 * or the browser cannot parse it. Raised at once instead of being polled until the timeout,
 * and never sent to a model. */
export class InvalidSelector extends Error {
  override name = 'InvalidSelector';
  readonly selector: string | null;
  readonly reason: string;
  readonly hint: string;

  constructor(message: string, options: { selector?: string | null; reason?: string; hint?: string } = {}) {
    super(message);
    this.selector = options.selector ?? null;
    this.reason = options.reason ?? message;
    this.hint = options.hint ?? '';
  }

  static build(selector: string | null, reason: string, hint: string): InvalidSelector {
    return new InvalidSelector('mendwright: ' + reason + ' ' + hint, { selector, reason, hint });
  }

  /** The same error, with the selector key named in the message. */
  forKey(key: string): InvalidSelector {
    const message = ('mendwright: the selector for ' + pyRepr(key) + ' is not usable: '
      + this.reason + ' ' + this.hint).trimEnd();
    return new InvalidSelector(message, { selector: this.selector, reason: this.reason, hint: this.hint });
  }
}

/** A store file exists but cannot be used: it is not valid JSON, or not a selector file. It
 * is never read as empty, because the next write would replace it and lose every heal. */
export class StoreReadError extends Error {
  override name = 'StoreReadError';
}

/** A step handled the most interruptions one step may, and one is still on the page. */
export class InterruptionLoop extends AttentionRequired {
  override name = 'InterruptionLoop';
}

/** Playwright's errors.TimeoutError, from the first of @playwright/test, playwright and
 * playwright-core this package finds, which is the one a test's Page comes from; Error when
 * there is none. */
function timeoutErrorClass(): typeof errors.TimeoutError {
  for (const name of ['@playwright/test', 'playwright', 'playwright-core']) {
    try {
      const found = require(name)?.errors?.TimeoutError;
      if (typeof found === 'function') return found;
    } catch {
      // not installed here
    }
  }
  return Error as unknown as typeof errors.TimeoutError;
}

/** What a lookup of a heal.page.getBy*() call throws when it cannot find or heal its element
 * (the Python package's getby.GetByNotFound). It is Playwright's TimeoutError, so code that
 * catches that, such as an optional step written as catch (e) { if (e instanceof
 * errors.TimeoutError) ... }, still does, and an AttentionRequired as well (see
 * AttentionRequired): JavaScript has one base class, so it extends Playwright's, and
 * AttentionRequired's instanceof takes it. Its name is 'TimeoutError', as Playwright's is, and
 * its cause the AttentionRequired of the miss. */
export class GetByNotFound extends (timeoutErrorClass()) {
  constructor(message: string, options: { cause?: unknown } = {}) {
    super(message);
    this.name = 'TimeoutError';
    if ('cause' in options) this.cause = options.cause;
  }
}
