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

The `page` fixture is then mendwright's page. `page.locator(selector)` and the page methods that take a selector (`click`, `dblclick`, `fill`, `check`, `uncheck`, `selectOption`, `press`, `type`, `hover`, `isChecked`, `textContent`, `innerText`, `getAttribute`, `inputValue`) heal, and each selector is also the key its heals are saved under. Everything else is the real page's: `goto`, `evaluate`, `route`, `expect(page)`, a locator built with options or chained from another one, and the `getBy*` locators (with `dismiss: true` those get the interruption and popup checks, see [Popups nobody registered](#popups-nobody-registered)).

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
- `expectValue` says what a read must return when the element was found through a heal, such as `'money'`: see [Checking what a read returns](#checking-what-a-read-returns).

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

## Checking what a read returns

A heal passes the checks when the new element has the step's role and is the one visible match. For a read, that can still be the wrong element: after a redesign, a price selector can heal onto the rating next to the price, and a scraper saves 4.5 as the price. `expectValue` says what a read must return:

```ts
const price = await heal.locator('price', '#price', { expectValue: 'money' }).innerText();
```

When the element was found through a selector a heal saved, in this run or an earlier one, mendwright checks the value the read returns:

- A value that passes is returned. It confirms a heal made in this run, as `expect` on an action does.
- A value that fails quarantines the heal, and the read throws `AttentionRequired` instead of returning it. The same text is logged as a warning:

  ```text
  mendwright: 'price' was healed to //span[@class='rating'], and its innerText() there is '4.5', which is not money (expectValue: 'money'). The heal probably found another element, so mendwright quarantined it and did not return the value. The next lookup of 'price' heals it again, and cannot pick that element. If the value is right, change expectValue.
  ```

  The quarantine blocks the element, not only the selector, so neither the fingerprint tier nor the model can pick the rating again.

A read through any other selector, such as the one in your code, is never checked: `expectValue` only ever refuses a heal. A price your own selector reads as "Call for price" comes back as it is.

The reads it checks are `innerText()`, `textContent()`, `inputValue()` and `getAttribute()`, of `heal.locator()`, `heal.page` and `heal.page.locator()`. Other reads, the actions, and what you run on `native` yourself are as before.

| `expectValue` | Passes for |
|---|---|
| `'money'` | an amount with a currency sign or code before or after it, and nothing else: `$1,299.00`, `1.299,00 €`, `129 kr`, `CHF 12.50`, `USD 1299`, `$12.00 USD`, `R1,299.00`, `₹1,29,999.00`, `129,-`, `-$12`. The signs are the Unicode currency signs and `US$`, `C$` and the like; the codes are the common ISO 4217 codes and abbreviations such as `kr`, `zł`, `Kč`, `Ft`, `lei`, `Rs`, `R`, `RM` and `CHF`. A number without one, such as `1,299.00`, is not money: use `'number'` |
| `'number'` | a number and nothing else, with an optional sign: `1,299.00`, `1.299,00`, `1 299,00`, `1'299.00`, `4.5`, `-3` |
| `'integer'` | a number with no decimal part written: `1,299`, `12`, but not `12.00` |
| `'percent'` | a number with `%` before or after it: `12%`, `12.5 %`, `%20` |
| `'date'` | a calendar date (31/02/2026 is not one): `2026-10-15`, with or without a time (`2026-10-15T10:00`, `2026-10-15T10:00:00+02:00`, `2026-10-15 10:00`), `15/10/2026`, `10/15/2026`, `15.10.2026`, `2026年10月15日`, or with the month's name in English, German, French, Spanish, Italian, Dutch or Portuguese, such as `Oct 15, 2026`, `15. Oktober 2026` or `15 de octubre de 2026`, also after a weekday or before a time |
| `'email'` | an email address, also as a `mailto:` link |
| `'url'` | an `http` or `https` URL with a host, one that starts with `//`, or a path that starts with `/`, `./` or `../`, as `getAttribute('href')` gives for a relative link |
| a `RegExp` | a value it finds a match in, as Python's `re.search()` does. Anchor it with `^` and `$` to match the whole value. Its `g` and `y` flags are left out, so its `lastIndex` is never read or moved |
| a function | a value it returns something truthy for, or a promise of something truthy. It gets the value as a string, and one that throws or rejects refuses it |

Numbers are read the way most locales write them. Commas, dots, spaces (non-breaking and narrow ones too) and apostrophes group the digits, in threes, or in India in twos before a last group of three (`1,29,999`). When a number has both `.` and `,`, the one that comes last is the decimal point, and a mark that comes more than once groups. A single `.` or `,` followed by exactly three digits, as in `1.299` or `1,299`, is read as a thousands separator, as in a price; `0,299`, `1234.567` and `1 299,500` have a decimal part. In an amount of a currency with three decimals (`KWD`, `BHD`, `OMR`, `JOD`, `TND`), that `.` is the decimal point, as those amounts are written: `KWD 1.250` is 1.250. That `,` could be either (`1,250 TND` is 1.250 in Tunisia and 1250 in English), so `parseMoney()` gives `null` for it, though it passes `'money'`. The value is read in its compatibility form, so fullwidth digits and the digits of other scripts count, and spaces around it don't. A value of `null`, such as `getAttribute()` of an attribute the element doesn't have, passes no check, and a function isn't called for it.

The checks are the Python package's. This package's tests run both packages on a table of over 450 values, and compare the character classes they rest on (Python's `\d`, `\w` and `\s`, `str.casefold()` and the like) for every character Python's Unicode version knows.

`parseMoney()` and `parseNumber()` read the value you got the same way. They give the amount as a string, as the Python package's `Decimal` prints it, so no digit is lost, or `null`:

```ts
import { parseMoney } from 'mendwright';

parseMoney('1.299,00 €');   // '1299.00'
parseMoney('129 kr');       // '129'
parseMoney('4.5');          // null: no currency
```

The string keeps the trailing zeros, and writes a number below 0.000001 as Python does, such as `'1E-7'`. `Number()` reads both.

`heal.page` takes no `expectValue`, so give the checks for its keys to `open()`, or to the fixtures with `test.use({ mendwrightOptions: { expectValues } })`. The key is the selector:

```ts
const heal = open(page, { expectValues: { '#price': 'money', '#delivery': 'date' } });
await heal.page.innerText('#price');   // checked when '#price' was found through a heal
```

The same object covers the keys of `heal.locator()`, and an `expectValue` passed to it wins over it. Neither is saved, so pass them on each run. A check that can never work, such as `'cash'` or a number, throws `TypeError` from `heal.locator()` or `open()`, before anything touches the page. A function written in place, as in `{ expectValue: (v) => ... }`, is called "the function" in messages; a named one by its name.

In the journal, a refused read writes a `heal.canary` with `success: false` and `by: 'expect_value'` (for a heal made in this run) and a `heal.quarantine`, each with a `reason` such as `expect_value: innerText() is not money`, as the Python package writes them. The value itself is never written. A read that passes confirms a heal made in this run with a `heal.canary` whose `by` is `'expect_value'`.

## Cookie banners and other interruptions

`open(page, { cookies: 'reject' })`, or `test.use({ mendwrightOptions: { cookies: 'reject' } })`, closes the consent banners of common consent managers without accepting: OneTrust, Cookiebot, Didomi, TrustArc, Usercentrics, Quantcast Choice, Osano, cookieconsent, CookieYes and Complianz, and, for others, a reject, decline or necessary-only button inside an element that is about cookies. `cookies: 'accept'` accepts them. It is off by default. The selectors are the Python package's, so a consent screen that isn't about cookies, such as an OAuth screen with Allow and Deny, is never clicked.

`heal.interrupt(key, selector, { action, args })` registers a step of your own that can show up at any point, such as a newsletter modal. mendwright looks for these only when a step needs help: when a lookup misses, before an action of `heal.locator()` or `page` while the page shows one, and when such an action failed because something covered its element, after which the action is tried once more. At most 3 are handled for one step.

### Interruptions that need a form

```ts
heal.interrupt('session_expired', '#session-expired', {
  run: async function relogin(page) {
    await page.fill('#relogin-user', 'ada@example.com');
    await page.fill('#relogin-password', process.env.SHOP_PASSWORD!);
    await page.click('#relogin-submit');
  },
});
```

Some interruptions take more than one click: a session-expired popup that asks you to sign in again, a form for your address or postcode, a confirmation with two fields. Pass the function that answers it as `run`, in place of `action` and `args`. mendwright calls it at the same moments as any interruption, when `selector` matches one visible element: when a lookup misses, before an action or the first read, and after an action was intercepted. It gets `heal.page`, so its own selectors heal as usual. The selector only shows that the interruption is there, so it can be the popup itself.

- `run` is an async function, which mendwright awaits, or a function that returns a promise. One that returns anything else throws `TypeError` when the interruption is handled. `run` with `action` or `args`, a `run` that isn't a function and `heal.interrupt(key, null, { run })` throw `TypeError` at once, before anything touches the page.
- Its own actions never handle its own interruption, which is still on the page while it fills it in, so it is never called from inside itself. They do handle the other registered interruptions, such as a cookie banner that opens over its form. With `dismiss: true`, no popup is dismissed while it runs, by its own actions or by any other step.
- The step that called it waits for it. Another step of the same healer, such as one awaited next to it in `Promise.all()`, waits for it too before it looks up its element or handles an interruption, so the function is never called a second time while it runs. The function's own lookups and actions don't wait: the step they would wait for is the one waiting for them.
- After it returns, mendwright waits for the element to go away, as after an action, and logs `handled interruption 'session_expired' before 'search' with relogin()`. The `flow.interrupt` journal event has `action: 'run'` and `function: 'relogin'`, the function's name. A function written in place, as in `{ run: async (page) => ... }`, takes its name from the option, so mendwright calls it `<anonymous>`, as the Python package calls a lambda `<lambda>`. An interruption still on the page after its function returned counts as handled all the same, so a step handles it at most 3 times and then throws `InterruptionLoop`, whose message names the function.
- What it throws is thrown as it is, the same object, after one WARNING and a `flow.interrupt` event with `outcome: 'failed'` and the error's name in `error`. The step that called it stops there. An `AttentionRequired` from it isn't taken for a miss of that step, so nothing is healed or skipped.
- A promise can't be cancelled, so nothing stops the function once it has started: a test that times out while it runs leaves it running.

## Popups nobody registered

```ts
const heal = open(page, { dismiss: true });
await heal.locator('email', '#email').fill('ada@example.com');   // a newsletter modal opened first
```

`open(page, { dismiss: true })`, or `test.use({ mendwrightOptions: { dismiss: true } })`, dismisses a popup that nobody registered when it is in the way of an action: a newsletter or discount modal, an app-install banner, a survey, a chat invite, or a consent banner when `cookies` isn't set. It is off by default, and then mendwright makes no browser call for it. The rules are the Python package's, and this package's tests run both packages on the same tables.

mendwright acts only when the action's element is covered: before an action, when the browser says another element is on top at the middle of the element (`document.elementFromPoint`, into open shadow roots; an element below the window is scrolled into view first), and when an action fails because something covered its element. The registered interruptions come first, and while one the step handled is still on the page, popups are left alone. `focus()`, `setInputFiles()` and reads are not looked at.

The element on top belongs to a popup when it is in a dialog (`role="dialog"` or `"alertdialog"`, `aria-modal="true"`, or an open `<dialog>`), or else in a fixed or sticky element that takes pointer events and covers at least a quarter of the window. A popup that holds the action's element, a header or toolbar that covers less, and the full-window layer with `pointer-events: none` that toast libraries put a toast in are left alone.

For a popup, mendwright tries these in order, and after each looks again for up to 2 seconds (or the step's timeout, when that is shorter):

1. Escape, but only while the keyboard focus is on the page's body or in the popup, not in an iframe, and never with `cookies: 'accept'`. Escape goes to the focused element, and every key listener of the page gets it.
2. The popup's close button: one whose whole label is Close, or Close and what it closes (Close dialog, Close this banner, Close cart and the like), or an icon whose aria-label, or without one its title, is one of those. A bare ×, ✕ or X is never one.
3. A decline button: one whose label says no thanks, not now, maybe later, later, not interested, or continue without and what it leaves out (accepting, an account, cookies and the like), with at most 4 harmless words more, as in "Remind me later". Skip, skip for now and dismiss count only in a popup whose own text reads as a promotion or onboarding (a newsletter, an offer, % off, get the app, a survey, a tour, welcome, cookies and the like). Reject, reject all and decline count only in a popup that speaks of cookies.

mendwright never:

- presses Escape or clicks anything on an alert dialog (`role="alertdialog"`, or a popup inside one), which asks for an answer
- clicks a button of a popup that has a button that says cancel or keep (it gets Escape only), or a close, skip or dismiss button whose verb the popup's own text says, as Close in "Close your account?"
- clicks a button whose label (its text, the text it hides, its value, aria-label, title, and the text its `aria-labelledby` names) says accept, agree, allow, subscribe, sign up, yes, ok, continue, buy, pay, order, save, submit, delete, remove, install, get, try or one of the other words of the Python package's list, each matched as the start of a word, or has a digit or a currency sign in it, as in "Skip the line - $9.99"
- clicks a link that goes somewhere (only no `href`, `#` and `javascript:` code that does nothing go nowhere), a button in such a link or one that holds one, a disabled button, a label or a button in one, a button that holds a field, or a button that would submit or reset its form
- clicks a radio, checkbox, switch, tab or option, or anything with `aria-checked`, `aria-pressed` or `aria-selected`, and it never types, checks or selects
- with `cookies: 'accept'`, dismisses a popup that looks like a consent banner or has a button that says accept, agree, allow or consent, in English or another language: Escape, a close or a decline button could turn the consent down

Right before a click, the page script checks that the button is still the one chosen, with every fact of it the same. One that changed is not clicked.

A dismissed popup gets one WARNING, a `flow.dismiss` journal event with the Python package's fields (`key: '<popup>'`, `before`, `popup`, `outcome`, `how`, `label` and `tried`), and an entry `{ key: '<popup>', before, popup, how, label }` in `heal.handledInterruptions`, and then the action goes on, or a failed one is tried once more. `how` is `'escape'`, `'close'` or `'decline'`, and `label` is the button's, `null` for Escape. A registered interruption's entry has `null` in all three. `DISMISS_KEY` is `'<popup>'`.

Each popup counts once toward the 3 interruptions a step may handle. A popup that comes back each time it is dismissed throws `InterruptionLoop` once the step has handled 3, after a `flow.stop` event with the code `popup_loop`. A popup mendwright can't dismiss is named in one WARNING and not tried again in that step, and the action goes ahead and fails with the browser's own error if the popup blocks it. When the action's element can't be looked at as before after a step (it left the page, or it is hidden now), or a click threw after it may have reached the page (a navigation it started did not commit in time), mendwright can't tell what that step did: it stops with a WARNING, writes a `flow.dismiss` event with `outcome: 'lost'`, and doesn't try a failed action again.

With `dismiss: true`, the locators `heal.page` doesn't heal are guarded too: the `getBy*` locators, a locator built with options, chained locators, `frameLocator()`, `and()`, `or()`, `first()`, `last()`, `nth()`, `filter()`, and the locators `heal.page.locator(selector).all()` gives. Their actions get the interruption and popup checks. They aren't healed and nothing about them is saved, `expect()` takes them, and `native` is the real locator. Log lines name their step after the calls that built it, in TypeScript, such as `"page.getByRole('button', { name: 'Continue' })"`. With `dismiss` off they are the real locators.

What it can't do:

- A popup that needs data before it closes, or whose only way out is a button mendwright never clicks, such as Subscribe or OK. Register the button you want with `heal.interrupt()`.
- A popup that opens while Playwright waits on an action is found only when the action fails, after its timeout.
- A popup that hides the element instead of covering it: the lookup then misses. Register it with `heal.interrupt()`, which is also handled when a lookup misses.
- Actions on your own page or the locators you build from it, `page.mouse`, `page.keyboard` and `ElementHandle`s get no popup check.
- In an iframe, mendwright looks in the element's own document: a heal the Python package saved inside an iframe is used, and a popup inside that iframe is dismissed there, without Escape. A popup of the page around the iframe isn't.

## Store and journal

Heals are saved in `.mendwright/overrides.json` next to the Playwright config (`mendwrightStore`, or `MENDWRIGHT_STORE`; for `open()`, `storePath`, else `./.mendwright/overrides.json`), and each request, proposal, rejection and outcome goes to `journal.jsonl` next to it. A leading `~` in any of these is your home directory, as in the Python package, so a CI workflow can set `MENDWRIGHT_STORE: ~/.cache/mendwright/overrides.json` for both packages. It is the Python package's file format, schema 2: a team that uses both packages can share one store, and the Python package's `mendwright report` lists what this package saved. Writes take a lock file and merge with what other processes wrote, so Playwright Test workers can share one store. What only the Python package uses stays in the file as it wrote it: list heals, and where a key's element is inside an iframe or an open shadow root (`frameXPath`, `shadowHosts`).

The journal has the Python package's events, with the same fields: each line has the id of the run that wrote it in `run` (`MENDWRIGHT_RUN_ID`, or on GitHub Actions `gh-<run id>.<attempt>`), a heal that was not tried or did not apply is a `heal.declined` with the Python package's `code` and the `reason` (a key last found inside a shadow root or an iframe, where this package does not heal, is `shadow` or `frame`, and `dry_run` or `disabled` in dry run and under `MENDWRIGHT_DISABLE=1`, which `mendwright notify` counts as not repaired on purpose), an interruption that kept coming back is a `flow.stop` with the code `interruption_loop`, and with `dismiss: true` a popup is a `flow.dismiss`, and one that kept coming back a `flow.stop` with the code `popup_loop`. So the Python package's `mendwright notify` posts the summary of a run of this package as it does its own. A `MENDWRIGHT_RUN_ID` that is not 1 to 100 letters, digits, `.`, `_`, `:`, `/` or `-` is ignored, with one warning from `open()`.

## Settings

| `open()` option | Environment variable | Default |
|---|---|---|
| `provider` | the provider variables above | from the environment |
| `storePath` | `MENDWRIGHT_STORE` | `./.mendwright/overrides.json` |
| `timeoutMs` | `MENDWRIGHT_TIMEOUT_MS` | 10000 |
| `dryRun` (check proposals, apply none) | `MENDWRIGHT_DRY_RUN` | off |
| `fingerprint` | `MENDWRIGHT_FINGERPRINT` | on |
| `journal` (where the events go) | `MENDWRIGHT_RUN_ID` (the run id of each line) | `journal.jsonl` next to the store; the run id on GitHub Actions is `gh-<run id>.<attempt>`, else none |
| `destructiveKeys`, `guard`, `healAcrossPages`, `cookies`, `dismiss`, `expectValues`, `ratePerKeyPerHour`, `rateGlobalPerHour`, `log` | | |

`MENDWRIGHT_DISABLE=1` resolves selectors but never heals or calls a provider, even one passed as `provider`. The fixtures take `mendwrightProvider`, `mendwrightStore`, `mendwrightOptions` (any other `open()` option), `mendwrightPage` and `mendwrightStrict` through `test.use()`.

## Not in 0.1.0

These are in the Python package and not in this one yet:

- Selenium. This package works with Playwright only.
- `heal.step()` and `heal.run_steps()`, for steps the site shows in another order.
- `heal.locator_all()` and list fingerprints.
- Healing `getBy*` locators. They go to Playwright unhealed.
- `mendwright check`, the drift check before a run.
- `mendwright fix`, which writes confirmed heals back into your code, and the GitHub Action that opens a pull request with them.

And, compared with the Python package's `heal.locator()` and `heal.interrupt()`:

- An iframe is not followed into: a selector that matches only inside an iframe is not looked up there, and no heal is made inside one. A heal the Python package saved inside an iframe is used, but a key last found inside an iframe is not healed when it breaks (after the checks for a key marked destructive and the page guard, which decline it first).
- No heal is made inside an open shadow root. Each time a key's selector matches, where its element is gets saved as the key's `shadowHosts`, as the Python package saves it, and a key last found inside a shadow root is not healed when it breaks, so an element that looks the same on the page around its host is never picked. A heal the Python package saved inside a shadow root runs as the Python package runs it: its CSS through its hosts, and only a match inside that shadow root counts, never one in the host's light DOM, a shadow root inside it or another host. That is checked when the key is looked up and again right before each action and read of the `HealLocator`, except that `isVisible()` and `isHidden()` answer `false` and `true` at once when the Locator matches nothing at all, as Playwright's do; what you run on its `native` Locator yourself isn't checked. When the Python package 0.9.0 has written the store, which drops every `shadowHosts`, the key's and each heal's, a heal's chain still names its hosts, so that key is still not healed; a key that only your own selector found inside a shadow root has nothing left to say so until it matches again. Don't share a store with the Python package 0.9.0.
- Interruptions are not registered with `page.addLocatorHandler()`, so they are handled before the steps mendwright routes only, not before a `getBy*` or chained locator's actions unless `dismiss: true` guards them.
- Gemini, Bedrock, LiteLLM and the gateway as providers, and seed files.
- A page whose cleaned HTML is longer than 60,000 characters is cut at the end for a hosted model, instead of having unrelated parts left out, so the element can be cut off. A heal of such a page can't use a position such as `[2]`.

## Links

- [The Python package](https://github.com/saadbuilt/mendwright), and its [guide](https://github.com/saadbuilt/mendwright/blob/main/docs/guide.md), which explains the fingerprint tier, the checks, the page guard and the store in more detail
- [Changelog](https://github.com/saadbuilt/mendwright/blob/main/js/CHANGELOG.md)
- [Security](https://github.com/saadbuilt/mendwright/blob/main/SECURITY.md)

[MIT license](https://github.com/saadbuilt/mendwright/blob/main/LICENSE).
