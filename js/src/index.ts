// mendwright: self-healing selectors for Playwright Test.
//
//   import { open } from 'mendwright';
//   const heal = open(page);
//   await heal.locator('email', '#email').fill('ada@example.com');
//
// or, in Playwright Test, import { test, expect } from 'mendwright/test'.

export { open, Healer, HealLocator, ACTIONS, READS, fingerprintSetting } from './healer';
export type { HealEvent, LocatorOptions, OpenOptions, ExpectOption } from './healer';
export { AttentionRequired, InvalidSelector, InterruptionLoop, StoreReadError } from './errors';
export { AnthropicProvider } from './providers/anthropic';
export type { AnthropicOptions } from './providers/anthropic';
export { OpenAICompatibleProvider } from './providers/openai';
export type { OpenAIOptions } from './providers/openai';
export { LocalModelProvider } from './providers/local';
export type { LocalOptions } from './providers/local';
export { autoProvider } from './providers/detect';
export { NullProvider } from './providers/base';
export type { HealProvider, ProviderTools } from './providers/base';
export type { Http, HttpResponse } from './providers/http';
export type { Bundle, Proposal } from './prompt';
export { loadStore, SelectorStore } from './store';
export { Journal, NullJournal } from './journal';
export type { JournalLike } from './journal';
export { RateLimiter } from './limits';
export type { Guardrails } from './limits';
export { setLogSink } from './log';
export type { LogLevel, LogSink } from './log';
export type { Interruption, InterruptionEvent, CookieChoice } from './interruptions';

export const version = '0.1.0';
