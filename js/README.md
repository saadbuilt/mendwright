# mendwright for TypeScript

Self-healing selectors for Playwright Test.

mendwright heals many broken selectors without any API key. Each time a selector matches, it saves a fingerprint of the element: its tag, ids, names, label, text and surroundings. When a site update breaks the selector, mendwright looks for that element first, checks the new XPath against the live page, saves it, and retries the step. It asks a model only when the fingerprint match isn't confident. On the next run the saved selector is used and nothing is asked.

This is the first release of the TypeScript package, 0.1.0. It is a port of the [Python package](https://github.com/saadbuilt/mendwright) for Playwright Test, with its fingerprint scoring, its checks and its store file, but not yet everything the Python package does: see [Not in 0.1.0](#not-in-010).

## Install

```bash
npm install --save-dev mendwright
```

It needs `@playwright/test` 1.40 or later and Node 18 or later, and has no dependencies of its own. Its types resolve under every TypeScript `moduleResolution`, `node10` (the default with `"module": "commonjs"`) included.

## The one-line change

Import `test` and `expect` from `mendwright/test` instead of `@playwright/test`:

```ts
import { test, expect } from 'mendwright/test';   // was: from '@playwright/test'

test('sign up', async ({ page }) => {
  await page.goto('https://example.com/signup');
  await page.fill('#email', 'ada@example.com');
  await page.locator('#terms').check();
  await page.click('#create-account');
  await expect(page.locator('#welcome')).toBeVisible();
});
```

The `page` fixture is then mendwright's page. `page.locator(selector)` and the page methods that take a selector (`click`, `dblclick`, `fill`, `check`, `uncheck`, `selectOption`, `press`, `type`, `hover`, `isChecked`, `textContent`, `innerText`, `getAttribute`, `inputValue`) heal, and each selector is also the key its heals are saved under. Everything else is the real page's: `goto`, `evaluate`, `route`, `expect(page)`, a locator built with options or chained from another one, and the `getBy*` locators.

A selector that stops matching is waited for 10 seconds (`timeoutMs`, or `MENDWRIGHT_TIMEOUT_MS`) before mendwright tries to heal it. Each applied heal prints one line to stderr:

```text
mendwright: healed '#email': #email -> //input[@id='user-email'] (fingerprint, 0.0s)
```

`test.use({ mendwrightPage: false })` gives the plain page back, and the `heal` fixture stays.

## Keys and roles

`heal.locator(key, selector)` names the element yourself, and takes the options a heal needs:

```ts
test('accept the terms', async ({ page, heal }) => {
  await page.goto('https://example.com/signup');
  await heal.locator('email', '#email').fill('ada@example.com');
  await heal.locator('accept_terms', "//input[@id='terms']", { role: 'checkbox' }).check();
  await heal.locator('pay', '#pay', { destructive: true }).click();
});
```

- `role` is the kind of element: `button`, `checkbox`, `radio`, `link`, `input`, `combobox`, an ARIA name such as `textbox` or `switch`, or a tag name. mendwright reads it from the element the first time the selector matches. Pass it when you add mendwright to a suite whose selectors might already be broken: without a known role, a heal is declined.
- `destructive: true` marks a pay, submit or delete control, which is never healed. It is saved, so later runs keep it. `open(page, { destructiveKeys: [...] })` marks keys for good.
- `optional: true` marks a step that may not be there: if its element doesn't appear, nothing is healed and its actions do nothing.
- `intent` describes the element in words for the model, such as `'the terms checkbox in the signup form'`. With `role` and no selector, a model looks for the element on first use.
- `selector` can be a list of selectors, tried in order. The selectors in your code always come first; when you change them, the old ones are kept as `superseded` and no longer tried.

The locator's actions are Playwright's, with the same arguments, and resolve the key first. `await locator.resolve()` resolves it without acting, then `locator.native` is the real Playwright `Locator` and `locator.present` says whether an optional step was there.

Without Playwright Test, or for a page you opened yourself:

```ts
import { open } from 'mendwright';

const heal = open(page);
await heal.page.fill('#email', 'ada@example.com');
await heal.locator('create', '#create-account', { role: 'button' }).click();
```

## A model for what the fingerprint can't place

A selector that never matched has no fingerprint, and some changes are beyond one, so to let mendwright ask a model, set the same variables as for the Python package:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL="..."   # a current model id from Anthropic's model list
```

There is no default model. These are read in this order, and the first one set is used:

| Set this | Provider |
|---|---|
| `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` | Anthropic's Messages API (`ANTHROPIC_BASE_URL` for a proxy) |
| `OPENAI_API_KEY` and `OPENAI_MODEL` | OpenAI, or a compatible host with `OPENAI_BASE_URL` |
| `MENDWRIGHT_LOCAL_MODEL` | mendwright's own selector model in Ollama, such as `mendwright-selector` |
| `OLLAMA_MODEL` (and `OLLAMA_HOST`) | any model in a local Ollama, with no key |

`MENDWRIGHT_PROVIDER` (`anthropic`, `openai`, `local` or `ollama`) skips the order. `GEMINI_API_KEY` and `MENDWRIGHT_GATEWAY` select providers this package doesn't have yet: when one of them is the first set, mendwright says so instead of sending the page to another provider.

In code, pass a provider to `open()`, or to the fixtures with `test.use({ mendwrightProvider })`:

```ts
import { AnthropicProvider, LocalModelProvider, OpenAICompatibleProvider } from 'mendwright';

new AnthropicProvider({ model: '...' });                       // key from ANTHROPIC_API_KEY
new OpenAICompatibleProvider({ model: '...', baseUrl: 'https://api.groq.com/openai/v1', apiKey: '...' });
OpenAICompatibleProvider.forOllama('...');                      // http://127.0.0.1:11434/v1, no key
new LocalModelProvider();                                       // mendwright-selector in Ollama
```

Any object with `propose(bundle)` that returns `{ xpath, fallbacks, reasoning }` or `null` works as a provider.

A base URL with a user name or password in it, such as `https://user:token@llm-gw.example`, is refused when the provider is built: Node's `fetch` can't send one, and the error names only the scheme and host, so the credentials never reach stderr, the journal or a test's error. For a proxy that needs them, pass the provider an `http` client that sends them in a header. A provider's error text never shows the user info of a URL.

The local selector model is installed with the Python package's command, `mendwright model install` (it downloads the model from the GitHub release, checks its SHA-256 and registers it with Ollama). It is never sent the page: mendwright lists at most 40 elements of the step's role, one short line each, and the model answers with the number of the one the step meant, or says that none fits. mendwright then builds the XPath for that element itself. A pick it is less than 80% sure of is declined (`MENDWRIGHT_LOCAL_MIN_CONFIDENCE` changes that).

With a hosted provider, a cleaned, redacted copy of the page is sent whenever the fingerprint match isn't confident: scripts and styles are removed, and so are quoted `value` attributes, absolute URLs and runs of 12 to 19 digits, but visible text and other attributes stay. A page that can't be cleaned isn't sent at all, and the heal says why. The key's fingerprint goes only to the local model.

mendwright parses the captured page inside the browser. A page whose Content-Security-Policy enforces Trusted Types (`require-trusted-types-for 'script'`) refuses that, so there mendwright makes a Trusted Types policy of its own, named `mendwright`, once for each document. When the page's `trusted-types` directive lists the policies it allows and `mendwright` isn't one of them, nothing is sent to the model and no heal is made on that page, with that reason: add `mendwright` to the directive on your test site, or, in Chromium, set `bypassCSP: true` in the `use` section of your Playwright config. A model heal is limited to 3 attempts per key and 60 in all per hour (`ratePerKeyPerHour`, `rateGlobalPerHour`); a fingerprint heal doesn't count.

## What a heal must pass

Before anything is tried, a heal is declined when the key is marked destructive, when mendwright doesn't know the key's role, when the page isn't one the key was found on (a step that moved to another page looks like a missing element; `healAcrossPages: true` turns this off), or while a `guard` you passed returns false.

The fingerprint tier then scores the elements of the page that have the key's role. It takes the best one, with no model call, only when it scores at least 0.40, no other visible element comes within 0.12 of it, it shares an id, test id, name, link, label or text with the recorded element, and it sits inside every recorded container that is still on the page. Otherwise the model is asked, and a refused proposal goes back to it with the reason, once.

Either way, the XPath is used only if:

- it isn't an index-only path such as `/html[1]/body[1]/div[3]/input[1]`
- the store doesn't hold it as quarantined or superseded for this key, and it doesn't reach an element the key has quarantined
- it matches exactly one element in the captured page, and that element has the key's role
- it matches exactly one element on the live page, and that element is visible (a hidden checkbox is allowed) and still has the role

The scoring, the thresholds, the text rules and these checks are the Python package's. This package's tests run the Python package from the repository and compare the two on the 47 cases of the [benchmark](https://github.com/saadbuilt/mendwright/blob/main/docs/benchmark.md): the same scores, XPaths and reasons from the fingerprint tier, and the same task text and XPaths for the local model. They compare the prompt, the cookie selectors and the page paths too.

## Confirmation and quarantine

A new heal starts unconfirmed. With the fixtures, every heal made during a test is confirmed when the test passes; when it fails, its heals stay saved, unconfirmed, and the test gets a `mendwright` annotation for each. A heal is also confirmed by an `expect` function you pass with an action's options, or by `locator.confirm(true)`:

```ts
await heal.locator('terms', '#terms', { role: 'checkbox' }).check({ expect: () => page.locator('#create').isEnabled() });
```

A heal is quarantined when an action on it fails in the browser, when `expect` returns something falsy, or with `confirm(false)`. A quarantined selector is never tried again, and a later proposal that reaches the same element is refused.

`test.use({ mendwrightStrict: true })` fails a test that passed only because a selector was healed. The heal is still saved and confirmed, so strict CI runs need a fresh store.

## Cookie banners and other interruptions

`open(page, { cookies: 'reject' })`, or `test.use({ mendwrightOptions: { cookies: 'reject' } })`, closes the consent banners of common consent managers without accepting: OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, cookieconsent, CookieYes and Complianz, and, for others, a reject, decline or necessary-only button inside an element that is about cookies. `cookies: 'accept'` accepts them. It is off by default. The selectors are the Python package's, so a consent screen that isn't about cookies, such as an OAuth screen with Allow and Deny, is never clicked.

`heal.interrupt(key, selector, { action, args })` registers a step of your own that can show up at any point, such as a newsletter modal. mendwright looks for these only when a step needs help: when a lookup misses, before an action of `heal.locator()` or `page` while the page shows one, and when such an action failed because something covered its element, after which the action is tried once more. At most 3 are handled for one step.

## Store and journal

Heals are saved in `.mendwright/overrides.json` next to the Playwright config (`mendwrightStore`, or `MENDWRIGHT_STORE`; for `open()`, `storePath`, else `./.mendwright/overrides.json`), and each request, proposal, rejection and outcome goes to `journal.jsonl` next to it. A leading `~` in any of these is your home directory, as in the Python package, so a CI workflow can set `MENDWRIGHT_STORE: ~/.cache/mendwright/overrides.json` for both packages. It is the Python package's file format, schema 2: a team that uses both packages can share one store, and the Python package's `mendwright report` lists what this package saved. Writes take a lock file and merge with what other processes wrote, so Playwright Test workers can share one store. What only the Python package uses stays in the file as it wrote it: list heals, and where a key's element is inside an iframe or an open shadow root (`frameXPath`, `shadowHosts`).

The journal has the Python package's events, with the same fields: each line has the id of the run that wrote it in `run` (`MENDWRIGHT_RUN_ID`, or on GitHub Actions `gh-<run id>.<attempt>`), a heal that was not tried or did not apply is a `heal.declined` with the Python package's `code` and the `reason` (a key last found inside a shadow root or an iframe, where this package does not heal, is `shadow` or `frame`, and `dry_run` or `disabled` in dry run and under `MENDWRIGHT_DISABLE=1`, which `mendwright notify` counts as not repaired on purpose), and an interruption that kept coming back is a `flow.stop` with the code `interruption_loop`. So the Python package's `mendwright notify` posts the summary of a run of this package as it does its own. A `MENDWRIGHT_RUN_ID` that is not 1 to 100 letters, digits, `.`, `_`, `:`, `/` or `-` is ignored, with one warning from `open()`.

## Settings

| `open()` option | Environment variable | Default |
|---|---|---|
| `provider` | the provider variables above | from the environment |
| `storePath` | `MENDWRIGHT_STORE` | `./.mendwright/overrides.json` |
| `timeoutMs` | `MENDWRIGHT_TIMEOUT_MS` | 10000 |
| `dryRun` (check proposals, apply none) | `MENDWRIGHT_DRY_RUN` | off |
| `fingerprint` | `MENDWRIGHT_FINGERPRINT` | on |
| `journal` (where the events go) | `MENDWRIGHT_RUN_ID` (the run id of each line) | `journal.jsonl` next to the store; the run id on GitHub Actions is `gh-<run id>.<attempt>`, else none |
| `destructiveKeys`, `guard`, `healAcrossPages`, `cookies`, `ratePerKeyPerHour`, `rateGlobalPerHour`, `log` | | |

`MENDWRIGHT_DISABLE=1` resolves selectors but never heals or calls a provider, even one passed as `provider`. The fixtures take `mendwrightProvider`, `mendwrightStore`, `mendwrightOptions` (any other `open()` option), `mendwrightPage` and `mendwrightStrict` through `test.use()`.

## Not in 0.1.0

These are in the Python package and not in this one yet:

- Selenium. This package works with Playwright only.
- `dismiss`: dismissing popups nobody registered.
- `heal.step()` and `heal.run_steps()`, for steps the site shows in another order.
- `heal.locator_all()` and list fingerprints.
- Healing `getBy*` locators. They go to Playwright unhealed.
- `mendwright check`, the drift check before a run.
- `mendwright fix`, which writes confirmed heals back into your code, and the GitHub Action that opens a pull request with them.

And, compared with the Python package's `heal.locator()` and `heal.interrupt()`:

- An iframe is not followed into: a selector that matches only inside an iframe is not looked up there, and no heal is made inside one. A heal the Python package saved inside an iframe is used, but a key last found inside an iframe is not healed when it breaks (after the checks for a key marked destructive and the page guard, which decline it first).
- No heal is made inside an open shadow root. Each time a key's selector matches, where its element is gets saved as the key's `shadowHosts`, as the Python package saves it, and a key last found inside a shadow root is not healed when it breaks, so an element that looks the same on the page around its host is never picked. A heal the Python package saved inside a shadow root runs as the Python package runs it: its CSS through its hosts, and only a match inside that shadow root counts, never one in the host's light DOM, a shadow root inside it or another host. That is checked when the key is looked up and again right before each action and read of the `HealLocator`; what you run on its `native` Locator yourself isn't checked. When the Python package 0.9.0 has written the store, which drops every `shadowHosts`, the key's and each heal's, a heal's chain still names its hosts, so that key is still not healed; a key that only your own selector found inside a shadow root has nothing left to say so until it matches again. Don't share a store with the Python package 0.9.0.
- `expect_value` (checking what a read returns), and `run` functions for interruptions that need a form.
- Interruptions are not registered with `page.addLocatorHandler()`, so they are handled before the steps mendwright routes only, not before a `getBy*` or chained locator's actions.
- Gemini, Bedrock, LiteLLM and the gateway as providers, and seed files.
- A page whose cleaned HTML is longer than 60,000 characters is cut at the end for a hosted model, instead of having unrelated parts left out, so the element can be cut off. A heal of such a page can't use a position such as `[2]`.

## Links

- [The Python package](https://github.com/saadbuilt/mendwright), and its [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md), which explains the fingerprint tier, the checks, the page guard and the store in more detail
- [Changelog](https://github.com/saadbuilt/mendwright/blob/main/js/CHANGELOG.md)
- [Security](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md)

[MIT license](https://github.com/saadbuilt/mendwright/blob/main/LICENSE).
