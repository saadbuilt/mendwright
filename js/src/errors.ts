import { pyRepr } from './py';

/** mendwright could not or would not heal a step. The message says why and what to do
 * next. Nothing was acted on through a selector that failed a check. */
export class AttentionRequired extends Error {
  override name = 'AttentionRequired';
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
