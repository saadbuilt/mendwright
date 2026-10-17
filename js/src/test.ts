// The Playwright Test fixtures, the counterpart of the Python package's pytest plugin.
//
//   import { test, expect } from 'mendwright/test';
//
// - page is the healing page (heal.page): page.locator(selector) and the page methods that
//   take a selector heal when the selector broke, so a test file needs no other change.
//   test.use({ mendwrightPage: false }) gives the plain page back.
// - heal is the healer for the test's page: heal.locator(key, selector, { role }).
// - When a test passes, every heal made during it is confirmed: those of heal and of any
//   healer the test opened itself with open(). When it fails, its heals stay saved and
//   unconfirmed, and the test gets a 'mendwright' annotation for each.
// - test.use({ mendwrightStrict: true }) fails a test that passed only because a selector
//   was healed (the heal is still saved and confirmed), so CI shows selector drift.
//
// Options: mendwrightProvider (default: from the environment), mendwrightStore (default:
// MENDWRIGHT_STORE, else .mendwright/overrides.json next to the config file),
// mendwrightOptions (any other open() option), mendwrightPage and mendwrightStrict.

import * as path from 'node:path';

import { expect, test as base, type Frame, type Page, type TestInfo } from '@playwright/test';

import { addHealerListener, open, removeHealerListener, type Healer, type OpenOptions } from './healer';
import type { HealProvider } from './providers/base';

export { expect };

export interface MendwrightOptions {
  /** The model provider for heal and page. Default: autoProvider(), from the environment. */
  mendwrightProvider: HealProvider | undefined;
  /** The store for heal and page, relative to the config file. */
  mendwrightStore: string | undefined;
  /** Other open() options for heal and page. */
  mendwrightOptions: OpenOptions;
  /** true (default): the page fixture is heal.page. false: it is the plain page. */
  mendwrightPage: boolean;
  /** Fail a test that passed only because a selector was healed. */
  mendwrightStrict: boolean;
}

export interface MendwrightFixtures {
  heal: Healer;
}

const healers = new WeakMap<object, Healer>();

/** Where the fixtures keep heals: the mendwrightStore option (relative to the config file),
 * MENDWRIGHT_STORE (relative to where Playwright was started), then .mendwright/overrides.json
 * next to the config file. */
export function storePathFor(testInfo: TestInfo, option: string | undefined): string {
  const configFile = testInfo.config.configFile;
  const root = configFile ? path.dirname(configFile) : process.cwd();
  if (option) return path.resolve(root, option);
  const env = (process.env.MENDWRIGHT_STORE || '').trim();
  if (env) return path.resolve(env);
  return path.join(root, '.mendwright', 'overrides.json');
}

function healLine(h: { key: string; old: string[]; new: string }): string {
  const was = !h.old.length ? '(no stored selector)' : h.old.length === 1 ? h.old[0] : h.old[0] + ' (+' + (h.old.length - 1) + ' more)';
  return h.key + ': ' + was + ' -> ' + h.new;
}

export const test = base.extend<MendwrightOptions & MendwrightFixtures & { _mendwrightHealers: Healer[] }>({
  mendwrightProvider: [undefined, { option: true }],
  mendwrightStore: [undefined, { option: true }],
  mendwrightOptions: [{}, { option: true }],
  mendwrightPage: [true, { option: true }],
  mendwrightStrict: [false, { option: true }],

  // Every healer opened while the test runs; set up before the others and torn down after
  // them, so it confirms the heals once the test is over.
  _mendwrightHealers: [async ({ mendwrightStrict }, use, testInfo) => {
    const opened: Healer[] = [];
    const listener = (healer: Healer) => opened.push(healer);
    addHealerListener(listener);
    try {
      await use(opened);
    } finally {
      removeHealerListener(listener);
    }
    const passed = testInfo.status === 'passed';
    const made: { key: string; old: string[]; new: string; confirmed: boolean | null }[] = [];
    for (const healer of opened) {
      if (passed) healer.confirmAll(true, { by: 'test', test: testInfo.titlePath.join(' > ').slice(0, 300) });
      made.push(...healer.heals);
    }
    for (const h of made) {
      const state = h.confirmed === true ? 'confirmed' : h.confirmed === false ? 'quarantined: an action on it failed'
        : passed ? 'not confirmed' : 'not confirmed: the test did not pass';
      testInfo.annotations.push({ type: 'mendwright', description: healLine(h) + ' (' + state + ')' });
    }
    if (mendwrightStrict && passed && made.length) {
      const head = made.length === 1
        ? '1 selector was healed. The heal is saved, but the selector in your code no longer matches the page:'
        : made.length + ' selectors were healed. The heals are saved, but the selectors in your code no longer match the page:';
      throw new Error(['mendwright strict: this test passed only because ' + head, ...made.map((h) => '  ' + healLine(h)),
        (made.length === 1 ? 'Update the selector in your code to the new one' : 'Update the selectors in your code to the new ones')
          + ', or turn mendwrightStrict off to let heals pass.'].join('\n'));
    }
  }, { auto: true }],

  page: async ({ page, mendwrightProvider, mendwrightStore, mendwrightOptions, mendwrightPage }, use, testInfo) => {
    const healer = open(page, {
      ...mendwrightOptions,
      provider: mendwrightProvider ?? mendwrightOptions.provider,
      storePath: mendwrightOptions.storePath ? path.resolve(mendwrightOptions.storePath) : storePathFor(testInfo, mendwrightStore),
    });
    const value = mendwrightPage ? healer.page : page;
    healers.set(value, healer);
    await use(value);
  },

  heal: async ({ page }, use) => {
    const healer = healers.get(page as Page | Frame);
    if (!healer) throw new Error('mendwright: the heal fixture found no healer for the page fixture.');
    await use(healer);
  },
});
